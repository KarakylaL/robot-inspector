#include <Vex5.h>
#include <DHT11.h>
#include <Ultrasonic.h>

DHT11 dht11(2);
Ultrasonic ultrasonic(12, 13);

#define SAFE_DISTANCE 30

#define GAS_SENSOR_PIN A0
#define LIGHT_SENSOR_PIN A1
#define FIRE_SENSOR_PIN A2

#define LEFT_MOTOR_PORT  VEX5_PORT_12
#define RIGHT_MOTOR_PORT VEX5_PORT_9

Vex5_Motor motorL;
Vex5_Motor motorR;

int16_t goalSpeed = 600;

// Таймеры
unsigned long lastUltrasonic = 0;
unsigned long lastDHT11 = 0;
unsigned long lastSensors = 0;

long distance = 0;

int temperature = 0;
int humidity = 0;

int gas = 0;
int light = 0;
int fire = 0;

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200);

  Vex5.begin();

  motorL.begin(LEFT_MOTOR_PORT);
  motorR.begin(RIGHT_MOTOR_PORT);

  pinMode(GAS_SENSOR_PIN, INPUT);
  pinMode(LIGHT_SENSOR_PIN, INPUT);
  pinMode(FIRE_SENSOR_PIN, INPUT);
}

// Управление моторами
void controlMotors() {
  if (distance > 0 && distance < SAFE_DISTANCE) {
    motorL.setSpeed(0);
    motorR.setSpeed(0);
  } else {
    motorL.setSpeed(-goalSpeed);
    motorR.setSpeed(goalSpeed);
  }
}

// Чтение аналоговых датчиков
void readSensors() {
  gas = analogRead(GAS_SENSOR_PIN);
  light = analogRead(LIGHT_SENSOR_PIN);
  fire = analogRead(FIRE_SENSOR_PIN);
}

// Передача данных
void sendSensorData() {
  Serial.print(temperature);
  Serial.print(", ");
  Serial.print(humidity);
  Serial.print(", ");
  Serial.print(gas);
  Serial.print(", ");
  Serial.print(light);
  Serial.print(", ");
  Serial.println(fire);

  Serial2.print(temperature);
  Serial2.print(", ");
  Serial2.print(humidity);
  Serial2.print(", ");
  Serial2.print(gas);
  Serial2.print(", ");
  Serial2.print(light);
  Serial2.print(", ");
  Serial2.println(fire);
}

void loop() {
  unsigned long now = millis();

  // ===== HC-SR04 каждые 50 мс =====
  if (now - lastUltrasonic >= 50) {
    distance = ultrasonic.read();
    lastUltrasonic = now;
  }

  // ===== DHT11 каждые 2 секунды =====
  if (now - lastDHT11 >= 2000) {
    int result = dht11.readTemperatureHumidity(temperature, humidity);
    lastDHT11 = now;

    if (result == 0) {
      sendSensorData();
    }
  }

  // ===== Аналоговые датчики каждые 100 мс =====
  if (now - lastSensors >= 100) {
    readSensors();
    lastSensors = now;
  }

  // ===== Управление моторами =====
  controlMotors();
}
