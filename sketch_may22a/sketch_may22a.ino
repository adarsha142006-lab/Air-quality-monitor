#include <DHT.h>

#define DHTPIN 2
#define DHTTYPE DHT11

#define MQ135 A0
#define BUZZER 3

DHT dht(DHTPIN, DHTTYPE);

void setup() {

  Serial.begin(9600);

  dht.begin();

  pinMode(BUZZER, OUTPUT);

  Serial.println("Air Quality Monitoring");
}

void loop() {

  int air = analogRead(MQ135);

  float temp = dht.readTemperature();

  float hum = dht.readHumidity();

  Serial.print("Air Quality: ");
  Serial.println(air);

  Serial.print("Temperature: ");
  Serial.println(temp);

  Serial.print("Humidity: ");
  Serial.println(hum);

  // BUZZER CONDITION

  if (air > 100) {

    Serial.println("BAD AIR ALERT");

    tone(BUZZER, 10);

  }
  else {

    Serial.println("AIR NORMAL");

    noTone(BUZZER);
  }

  Serial.println("----------------");

  delay(2000);
}