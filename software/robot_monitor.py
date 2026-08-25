import atexit
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtMultimedia import QSoundEffect
from PyQt5.QtCore import QUrl
import pyqtgraph as pg

from sensor_history import PG_CONFIG, SensorHistoryWidget

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError as exc:
    raise SystemExit(
        "Не найден пакет psycopg2. Установите: pip install psycopg2-binary"
    ) from exc

ESP_IP = "your ip"
ESP_PORT = 5000
MAX_POINTS = 120
PLOT_INTERVAL_MS = 100
BATCH_SIZE = 50
FLUSH_INTERVAL_SEC = 1.0
MAX_PENDING_RECORDS = 20000
ALARM_REPEAT_SEC = 5.0
ALARM_SOUND_PATH_FIRE = "fire-alarm.wav"
ALARM_SOUND_PATH_GAS = "industrial-alarm.wav"


sock = None
socket_thread = None
db_thread = None
resources_closed = False

stop_event = threading.Event()
db_flush_event = threading.Event()

latest_sample = None
latest_lock = threading.Lock()
last_plotted_seq = -1
sample_seq = 0
alarm_active = False
last_alarm_beep = 0.0
ui = None
last_thg = None
last_light_alarm = None

pending_records = deque()
pending_lock = threading.Lock()


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_data (
                id BIGSERIAL PRIMARY KEY,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                temperature DOUBLE PRECISION NOT NULL,
                humidity DOUBLE PRECISION NOT NULL,
                gas DOUBLE PRECISION NOT NULL,
                light DOUBLE PRECISION NOT NULL DEFAULT 0,
                alarm DOUBLE PRECISION NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE sensor_data
            ADD COLUMN IF NOT EXISTS light DOUBLE PRECISION NOT NULL DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE sensor_data
            ADD COLUMN IF NOT EXISTS alarm DOUBLE PRECISION NOT NULL DEFAULT 0
            """
        )
    conn.commit()


def enqueue_record(record):
    with pending_lock:
        if len(pending_records) >= MAX_PENDING_RECORDS:
            pending_records.popleft()
            print("DB buffer overflow: dropped oldest record")
        pending_records.append(record)
        if len(pending_records) >= BATCH_SIZE:
            db_flush_event.set()


def _take_chunk(size):
    with pending_lock:
        take = min(size, len(pending_records))
        if take == 0:
            return []
        return [pending_records.popleft() for _ in range(take)]


def _requeue_front(records):
    with pending_lock:
        for rec in reversed(records):
            pending_records.appendleft(rec)


def flush_records(conn, force=False):
    while True:
        chunk = _take_chunk(BATCH_SIZE)
        if not chunk:
            return
        try:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO sensor_data (recorded_at, temperature, humidity, gas, light, alarm)
                    VALUES %s
                    """,
                    chunk,
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _requeue_front(chunk)
            print(f"DB insert error: {exc}")
            return

        if not force:
            return


def db_worker_loop():
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        conn.autocommit = False
        init_db(conn)
        print("PostgreSQL: connected")
    except Exception as exc:
        print(f"PostgreSQL connection error: {exc}")
        return

    try:
        while not stop_event.is_set():
            db_flush_event.wait(FLUSH_INTERVAL_SEC)
            db_flush_event.clear()
            flush_records(conn, force=False)

        flush_records(conn, force=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def socket_worker_loop():
    global sock, latest_sample, sample_seq, last_thg, last_light_alarm

    buffer = ""
    value_re = re.compile(r"-?\d+(?:\.\d+)?")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.connect((ESP_IP, ESP_PORT))
        sock.settimeout(0.2)
        print("ESP32: connected")
    except Exception as exc:
        print(f"ESP32 connection error: {exc}")
        stop_event.set()
        return

    while not stop_event.is_set():
        try:
            data = sock.recv(4096)
            if not data:
                print("ESP32 disconnected")
                stop_event.set()
                break

            # Drain kernel socket buffer quickly and keep only freshest data in this cycle.
            chunks = [data]
            sock.setblocking(False)
            try:
                while True:
                    extra = sock.recv(4096)
                    if not extra:
                        break
                    chunks.append(extra)
            except BlockingIOError:
                pass
            finally:
                sock.setblocking(True)
                sock.settimeout(0.2)

            buffer += b"".join(chunks).decode(errors="ignore")
            lines = buffer.split("\n")
            buffer = lines[-1]
            complete = [ln.strip() for ln in lines[:-1] if ln.strip()]
            if not complete:
                continue

            for line in complete:
                values = value_re.findall(line)
                if not values:
                    continue

                nums = [float(v) for v in values]

                temperature = humidity = gas = light = alarm_value = None
                if len(nums) >= 5:
                    temperature, humidity, gas, light, alarm_value = nums[:5]
                elif len(nums) == 3:
                    last_thg = (nums[0], nums[1], nums[2])
                    continue
                elif len(nums) == 2:
                    last_light_alarm = (nums[0], nums[1])
                    if not last_thg:
                        continue
                    temperature, humidity, gas = last_thg
                    light, alarm_value = last_light_alarm
                else:
                    continue

                if None in (temperature, humidity, gas, light, alarm_value):
                    continue
                recorded_at = datetime.now(timezone.utc)
                received_monotonic = time.monotonic()

                enqueue_record((recorded_at, temperature, humidity, gas, light, alarm_value))

                with latest_lock:
                    sample_seq += 1
                    latest_sample = (
                        sample_seq,
                        received_monotonic,
                        temperature,
                        humidity,
                        gas,
                        light,
                        alarm_value,
                    )

        except socket.timeout:
            continue
        except OSError as exc:
            if not stop_event.is_set():
                print(f"Socket error: {exc}")
                stop_event.set()
            break
        except Exception as exc:
            print(f"Socket parse error: {exc}")


def close_resources():
    global resources_closed

    if resources_closed:
        return
    resources_closed = True

    stop_event.set()
    db_flush_event.set()

    try:
        if socket_thread is not None and socket_thread.is_alive():
            socket_thread.join(timeout=2)
    except Exception:
        pass

    try:
        if db_thread is not None and db_thread.is_alive():
            db_thread.join(timeout=5)
    except Exception:
        pass

    try:
        if sock is not None:
            sock.close()
    except Exception:
        pass


def update_plots():
    global last_plotted_seq, alarm_active, last_alarm_beep

    if stop_event.is_set():
        app.quit()
        return

    if ui is None:
        return

    with latest_lock:
        sample = latest_sample

    if not sample:
        return

    seq, received_monotonic, temperature, humidity, gas, light, alarm_value = sample
    if seq == last_plotted_seq:
        return

    last_plotted_seq = seq

    temps.append(temperature)
    hums.append(humidity)
    gases.append(gas)
    lights.append(light)

    curve_t.setData(list(temps))
    curve_h.setData(list(hums))
    curve_g.setData(list(gases))
    curve_g2.setData(list(lights))

    latency_ms = (time.monotonic() - received_monotonic) * 1000.0
    ui.status_label.setText(f"Realtime latency: {latency_ms:.1f} ms")

    fire_alarm = alarm_value < 500
    gas_alarm = gas > 500
    new_alarm = fire_alarm or gas_alarm
    ui.alarm_label.setVisible(new_alarm)
    if new_alarm:
        if gas_alarm and not fire_alarm:
            ui.alarm_label.setText("ТРЕВОГА, УТЕЧКА ГАЗА")
        elif fire_alarm and not gas_alarm:
            ui.alarm_label.setText("ТРЕВОГА, ПОЖАР")
        else:
            ui.alarm_label.setText("ТРЕВОГА, ПОЖАР / УТЕЧКА ГАЗА")
    if new_alarm and (not alarm_active or (time.monotonic() - last_alarm_beep) >= ALARM_REPEAT_SEC):
        if fire_alarm and ui.alarm_player_fire is not None:
            if not ui.alarm_player_fire.isPlaying():
                ui.alarm_player_fire.play()
            if ui.alarm_player_gas is not None:
                ui.alarm_player_gas.stop()
        elif gas_alarm and ui.alarm_player_gas is not None:
            if not ui.alarm_player_gas.isPlaying():
                ui.alarm_player_gas.play()
            if ui.alarm_player_fire is not None:
                ui.alarm_player_fire.stop()
        else:
            QtWidgets.QApplication.beep()
        last_alarm_beep = time.monotonic()
    if not new_alarm and alarm_active:
        if ui.alarm_player_fire is not None:
            ui.alarm_player_fire.stop()
        if ui.alarm_player_gas is not None:
            ui.alarm_player_gas.stop()
    alarm_active = new_alarm


atexit.register(close_resources)

socket_thread = threading.Thread(target=socket_worker_loop, daemon=True)
socket_thread.start()

db_thread = threading.Thread(target=db_worker_loop, daemon=True)
db_thread.start()

temps = deque(maxlen=MAX_POINTS)
hums = deque(maxlen=MAX_POINTS)
gases = deque(maxlen=MAX_POINTS)
lights = deque(maxlen=MAX_POINTS)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Мониторинг датчиков")
        self.resize(1100, 720)

        tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(tabs)

        realtime_widget = QtWidgets.QWidget()
        realtime_layout = QtWidgets.QVBoxLayout(realtime_widget)

        self.alarm_label = QtWidgets.QLabel("ТРЕВОГА, ПОЖАР")
        self.alarm_label.setAlignment(QtCore.Qt.AlignCenter)
        self.alarm_label.setStyleSheet(
            "color: white; background: #b00020; font-size: 24px; "
            "font-weight: 700; padding: 10px; border: 2px solid #700;"
        )
        self.alarm_label.setVisible(False)
        realtime_layout.addWidget(self.alarm_label)

        self.alarm_player_fire = None
        self.alarm_player_gas = None
        try:
            sound = QSoundEffect()
            sound_path = os.path.abspath(ALARM_SOUND_PATH_FIRE)
            sound.setSource(QUrl.fromLocalFile(sound_path))
            sound.setLoopCount(QSoundEffect.Infinite)
            sound.setVolume(0.8)
            self.alarm_player_fire = sound
        except Exception:
            self.alarm_player_fire = None

        try:
            sound = QSoundEffect()
            sound_path = os.path.abspath(ALARM_SOUND_PATH_GAS)
            sound.setSource(QUrl.fromLocalFile(sound_path))
            sound.setLoopCount(QSoundEffect.Infinite)
            sound.setVolume(0.8)
            self.alarm_player_gas = sound
        except Exception:
            self.alarm_player_gas = None

        graph_widget = pg.GraphicsLayoutWidget()
        realtime_layout.addWidget(graph_widget, stretch=1)

        self.status_label = QtWidgets.QLabel("Realtime latency: -- ms")
        realtime_layout.addWidget(self.status_label)

        global curve_t, curve_h, curve_g, curve_g2
        plot_t = graph_widget.addPlot(title="Температура (°C)")
        plot_t.showGrid(x=True, y=True)
        curve_t = plot_t.plot(pen="r")

        graph_widget.nextRow()
        plot_h = graph_widget.addPlot(title="Влажность (%)")
        plot_h.showGrid(x=True, y=True)
        curve_h = plot_h.plot(pen="b")

        graph_widget.nextRow()
        plot_g = graph_widget.addPlot(title="Газ")
        plot_g.showGrid(x=True, y=True)
        curve_g = plot_g.plot(pen="w")

        graph_widget.nextRow()
        plot_g2 = graph_widget.addPlot(title="Освещённость (лм)")
        plot_g2.showGrid(x=True, y=True)
        curve_g2 = plot_g2.plot(pen="y")

        history_widget = SensorHistoryWidget()
        self.history_widget = history_widget

        tabs.addTab(realtime_widget, "График")
        tabs.addTab(history_widget, "История")

        self.plot_timer = QtCore.QTimer()
        self.plot_timer.setInterval(PLOT_INTERVAL_MS)
        self.plot_timer.timeout.connect(update_plots)
        self.plot_timer.start()

    def closeEvent(self, event):
        try:
            self.history_widget.close_db()
        except Exception:
            pass
        super().closeEvent(event)


app = QtWidgets.QApplication(sys.argv)
app.aboutToQuit.connect(close_resources)

ui = MainWindow()
ui.show()

sys.exit(app.exec_())
