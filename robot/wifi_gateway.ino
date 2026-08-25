#include <WiFi.h>
const char* ssid = "yourSSID";
const char* pass = "yourPASS";
WiFiServer server(5000);
WiFiClient client;
String uartBuffer = "";
void setup() {
  Serial.begin(115200);
  WiFi.begin(ssid, pass);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    }
    Serial.println(WiFi.localIP());
    server.begin(); // 🔴 ОБЯЗАТЕЛЬНО
    } 
void loop() {
  if (!client || !client.connected()) {
    client = server.available();
  }

  while (Serial.available()) {
    char c = Serial.read();

    if (c == '\n') {
      uartBuffer.trim();  // убираем \r и пробелы
      Serial.println("SEND: " + uartBuffer);

      if (client && client.connected()) {
        client.println(uartBuffer);
      }

      uartBuffer = "";
    } else {
      uartBuffer += c;
    }
  }
}
