#include "DHT.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// DHT11
#define DHTPIN 26
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// MQ135
#define ANALOG_PIN 34
#define DIGITAL_PIN 33

// Buzzer
#define BUZZER_PIN 25

// LCD — address 0x27, 16 columns, 2 rows
LiquidCrystal_I2C lcd(0x27, 16, 2);

int sensorValue;
int digitalValue;

// ─── CHANGE THESE ────────────────────────────────────────
const char* ssid      = "adars";
const char* password  = "123123123";
const char* serverURL = "http://172.28.82.37:5000/data";
// ─────────────────────────────────────────────────────────

void setup() {

  Serial.begin(9600);

  dht.begin();

  pinMode(DIGITAL_PIN, INPUT);

  pinMode(BUZZER_PIN, OUTPUT);

  digitalWrite(BUZZER_PIN, LOW);

  // LCD init
  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Air Quality Mon.");
  lcd.setCursor(0, 1);
  lcd.print("Starting up...");

  Serial.println("ESP32 Air Quality Monitoring");

  // Connect to Wi-Fi
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Connecting WiFi");

  Serial.print("Connecting to Wi-Fi");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("Wi-Fi connected!");
  Serial.print("ESP32 IP address: ");
  Serial.println(WiFi.localIP());

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WiFi Connected!");
  lcd.setCursor(0, 1);
  lcd.print(WiFi.localIP());
  delay(2000);

}

void loop() {

  // DHT11
  float hum = dht.readHumidity();

  float tempC = dht.readTemperature();

  if (isnan(hum) || isnan(tempC)) {

    Serial.println("Failed to read DHT11");

    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("DHT11 Error!");

    delay(2000);

    return;
  }

  // MQ135
  sensorValue = analogRead(ANALOG_PIN);

  digitalValue = digitalRead(DIGITAL_PIN);

  // Buzzer Condition
  if (sensorValue > 1000) {

    digitalWrite(BUZZER_PIN, HIGH);

  } else {

    digitalWrite(BUZZER_PIN, LOW);
  }

  // Display Values on Serial
  Serial.print("Temperature: ");
  Serial.print(tempC);
  Serial.println(" C");

  Serial.print("Humidity: ");
  Serial.print(hum);
  Serial.println(" %");

  Serial.print("Air Quality Value: ");
  Serial.println(sensorValue);

  if (digitalValue == 0) {
    Serial.println("⚠ Gas Detected");
  } else {
    Serial.println("✅ Air Normal");
  }

  Serial.println("----------------");

  // LCD — row 0: temp and humidity
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("T:");
  lcd.print(tempC, 1);
  lcd.print("C H:");
  lcd.print(hum, 1);
  lcd.print("%");

  // LCD — row 1: air quality and status
  lcd.setCursor(0, 1);
  lcd.print("AQ:");
  lcd.print(sensorValue);
  lcd.print(" ");
  if (digitalValue == 0) {
    lcd.print("GAS!");
  } else {
    lcd.print("Normal");
  }

  // Send data to local server
  if (WiFi.status() == WL_CONNECTED) {

    StaticJsonDocument<128> doc;

    doc["temperature"] = tempC;
    doc["humidity"]    = hum;
    doc["air_quality"] = sensorValue;
    doc["gas_status"]  = (digitalValue == 0) ? "Gas Detected" : "Safe";

    String body;
    serializeJson(doc, body);

    HTTPClient http;
    http.begin(serverURL);
    http.addHeader("Content-Type", "application/json");

    int responseCode = http.POST(body);

    Serial.print("Server response: ");
    Serial.println(responseCode);

    http.end();

  } else {

    Serial.println("Wi-Fi disconnected, skipping send");

  }

  delay(3000);

}