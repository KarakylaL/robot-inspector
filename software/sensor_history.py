import csv
import os
import sys
from datetime import datetime, timedelta

from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

import psycopg2



def load_local_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def env_first(*keys, default=None):
    for key in keys:
        value = os.getenv(key)
        if value not in (None, ""):
            return value
    return default


load_local_env()

PG_CONFIG = {
    "host": env_first("PGHOST", "host", default="127.0.0.1"),
    "port": int(env_first("PGPORT", "port", default="5432")),
    "dbname": env_first("PGDATABASE", "dbname", default="postgres"),
    "user": env_first("PGUSER", "user", default="postgres"),
    "password": env_first("PGPASSWORD", "password", default="postgres"),
}


class SensorHistoryWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.db_conn = psycopg2.connect(**PG_CONFIG)
        self.current_rows = []

        main_layout = QtWidgets.QVBoxLayout(self)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Дата:"))

        self.date_edit = QtWidgets.QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QtCore.QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        controls.addWidget(self.date_edit)

        controls.addWidget(QtWidgets.QLabel("С:"))
        self.time_from_edit = QtWidgets.QTimeEdit(QtCore.QTime(0, 0, 0))
        self.time_from_edit.setDisplayFormat("HH:mm:ss")
        controls.addWidget(self.time_from_edit)

        controls.addWidget(QtWidgets.QLabel("По:"))
        self.time_to_edit = QtWidgets.QTimeEdit(QtCore.QTime(23, 59, 59))
        self.time_to_edit.setDisplayFormat("HH:mm:ss")
        controls.addWidget(self.time_to_edit)

        self.load_btn = QtWidgets.QPushButton("Показать")
        self.load_btn.clicked.connect(self.load_data_for_date)
        controls.addWidget(self.load_btn)

        self.export_btn = QtWidgets.QPushButton("Экспорт CSV")
        self.export_btn.clicked.connect(self.export_csv)
        controls.addWidget(self.export_btn)

        self.delete_btn = QtWidgets.QPushButton("Удалить интервал")
        self.delete_btn.clicked.connect(self.delete_interval_data)
        controls.addWidget(self.delete_btn)

        self.status_label = QtWidgets.QLabel("")
        controls.addWidget(self.status_label, stretch=1)
        main_layout.addLayout(controls)

        self.graph_widget = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.graph_widget)

        axis_temp = pg.DateAxisItem(orientation="bottom")
        axis_hum = pg.DateAxisItem(orientation="bottom")
        axis_gas = pg.DateAxisItem(orientation="bottom")
        axis_light = pg.DateAxisItem(orientation="bottom")

        self.plot_t = self.graph_widget.addPlot(
            row=0, col=0, title="Температура (°C)", axisItems={"bottom": axis_temp}
        )
        self.plot_t.showGrid(x=True, y=True)
        self.curve_t = self.plot_t.plot(pen=pg.mkPen("r", width=2))

        self.plot_h = self.graph_widget.addPlot(
            row=1, col=0, title="Влажность (%)", axisItems={"bottom": axis_hum}
        )
        self.plot_h.showGrid(x=True, y=True)
        self.curve_h = self.plot_h.plot(pen=pg.mkPen("b", width=2))

        self.plot_g = self.graph_widget.addPlot(
            row=2, col=0, title="Газ", axisItems={"bottom": axis_gas}
        )
        self.plot_g.showGrid(x=True, y=True)
        self.curve_g = self.plot_g.plot(pen=pg.mkPen("w", width=2))

        self.plot_g2 = self.graph_widget.addPlot(
            row=3, col=0, title="Освещённость (лм)", axisItems={"bottom": axis_light}
        )
        self.plot_g2.showGrid(x=True, y=True)
        self.curve_g2 = self.plot_g2.plot(pen=pg.mkPen("w", width=2))

        self.load_data_for_date()

    def close_db(self):
        try:
            self.db_conn.close()
        except Exception:
            pass

    def closeEvent(self, event):
        self.close_db()
        super().closeEvent(event)

    def _selected_interval(self):
        selected_date = self.date_edit.date().toPyDate()
        start_time = self.time_from_edit.time().toPyTime()
        end_time = self.time_to_edit.time().toPyTime()

        start_dt = datetime.combine(selected_date, start_time)
        end_dt = datetime.combine(selected_date, end_time)

        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        return start_dt, end_dt

    def load_data_for_date(self):
        try:
            start_dt, end_dt = self._selected_interval()

            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT recorded_at, temperature, humidity, gas, light, alarm
                    FROM sensor_data
                    WHERE recorded_at >= %s AND recorded_at <= %s
                    ORDER BY recorded_at
                    """,
                    (start_dt, end_dt),
                )
                rows = cur.fetchall()

            self.current_rows = rows

            if not rows:
                self.curve_t.setData([], [])
                self.curve_h.setData([], [])
                self.curve_g.setData([], [])
                self.curve_g2.setData([], [])
                self.status_label.setText("Нет данных за выбранный интервал")
                return

            x_values = [row[0].timestamp() for row in rows]
            temperatures = [row[1] for row in rows]
            humidities = [row[2] for row in rows]
            gases = [row[3] for row in rows]
            lights = [row[4] for row in rows]

            self.curve_t.setData(x_values, temperatures)
            self.curve_h.setData(x_values, humidities)
            self.curve_g.setData(x_values, gases)
            self.curve_g2.setData(x_values, lights)

            self.status_label.setText(f"Загружено записей: {len(rows)}")

        except Exception as exc:
            self.status_label.setText(f"Ошибка загрузки: {exc}")

    def delete_interval_data(self):
        try:
            start_dt, end_dt = self._selected_interval()
            answer = QtWidgets.QMessageBox.question(
                self,
                "Подтверждение удаления",
                (
                    "Удалить данные в выбранном интервале?\n"
                    f"С: {start_dt}\nПо: {end_dt}"
                ),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return

            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM sensor_data
                    WHERE recorded_at >= %s AND recorded_at <= %s
                    """,
                    (start_dt, end_dt),
                )
                deleted_count = cur.rowcount
            self.db_conn.commit()

            self.status_label.setText(f"Удалено записей: {deleted_count}")
            self.load_data_for_date()
        except Exception as exc:
            self.db_conn.rollback()
            self.status_label.setText(f"Ошибка удаления: {exc}")

    def export_csv(self):
        if not self.current_rows:
            self.status_label.setText("Нет данных для экспорта")
            return

        default_name = f"sensor_data_{self.date_edit.date().toString('yyyy-MM-dd')}.csv"
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Сохранить CSV",
            default_name,
            "CSV Files (*.csv);;All Files (*)",
        )

        if not file_path:
            return

        try:
            with open(file_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["recorded_at", "temperature", "humidity", "gas", "light", "alarm"])
                for row in self.current_rows:
                    writer.writerow([row[0].isoformat(), row[1], row[2], row[3], row[4], row[5]])

            self.status_label.setText(f"CSV сохранён: {file_path}")
        except Exception as exc:
            self.status_label.setText(f"Ошибка экспорта: {exc}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = QtWidgets.QMainWindow()
    window.setWindowTitle("Просмотр данных датчиков")
    window.resize(1100, 700)
    widget = SensorHistoryWidget()
    window.setCentralWidget(widget)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
