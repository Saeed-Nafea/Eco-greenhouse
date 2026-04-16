const int soilPin = 34;   // S pin -> GPIO34

int dryRaw = -1;
int wetRaw = -1;

int readAverage(int pin, int samples = 20) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delay(10);
  }
  return sum / samples;
}

float getMoisturePercent(int raw, int dryValue, int wetValue) {
  if (dryValue == -1 || wetValue == -1 || dryValue == wetValue) {
    return -1;
  }

  float percent = 100.0 * (raw - dryValue) / (wetValue - dryValue);

  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;

  return percent;
}

void printInstructions() {
  Serial.println();
  Serial.println("=== Temporary Calibration Mode ===");
  Serial.println("1) Leave sensor probe in AIR, then type: d");
  Serial.println("2) Put ONLY probe in WATER, then type: w");
  Serial.println("3) After both are saved, moisture % will be shown");
  Serial.println("----------------------------------");
}

void setup() {
  Serial.begin(115200);

  analogReadResolution(12);
  analogSetPinAttenuation(soilPin, ADC_11db);

  delay(1000);
  printInstructions();
}

void loop() {
  int raw = readAverage(soilPin, 20);
  int mv  = analogReadMilliVolts(soilPin);

  if (Serial.available()) {
    char cmd = Serial.read();

    if (cmd == 'd' || cmd == 'D') {
      dryRaw = raw;
      Serial.println();
      Serial.print("Dry value saved (AIR) = ");
      Serial.println(dryRaw);
    }

    if (cmd == 'w' || cmd == 'W') {
      wetRaw = raw;
      Serial.println();
      Serial.print("Wet value saved (WATER) = ");
      Serial.println(wetRaw);
    }

    if (cmd == 'r' || cmd == 'R') {
      dryRaw = -1;
      wetRaw = -1;
      Serial.println();
      Serial.println("Calibration reset.");
      printInstructions();
    }
  }

  float moisture = getMoisturePercent(raw, dryRaw, wetRaw);

  Serial.print("Raw: ");
  Serial.print(raw);
  Serial.print("   Voltage(mV): ");
  Serial.print(mv);

  if (dryRaw != -1) {
    Serial.print("   Dry: ");
    Serial.print(dryRaw);
  }

  if (wetRaw != -1) {
    Serial.print("   Wet: ");
    Serial.print(wetRaw);
  }

  if (moisture >= 0) {
    Serial.print("   Moisture: ");
    Serial.print(moisture, 1);
    Serial.print("%");
  } else {
    Serial.print("   Moisture: waiting for calibration");
  }

  Serial.println();
  delay(1000);
}