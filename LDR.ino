const int ldrPin = 34;   // LDR connected to GPIO34

int darkValue = 800;     // default start value
int lightValue = 4000;   // default start value

// Read average to make values more stable
int readAverage(int times = 20) {
  long sum = 0;
  for (int i = 0; i < times; i++) {
    sum += analogRead(ldrPin);
    delay(5);
  }
  return sum / times;
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("=== LDR Calibration ===");
  Serial.println("Type d = save DARK value");
  Serial.println("Type l = save LIGHT value");
  Serial.println("Type s = show saved values");
  Serial.println();
}

void loop() {
  // Read command from Serial Monitor
  if (Serial.available() > 0) {
    char cmd = Serial.read();

    // ignore enter/newline
    if (cmd == '\n' || cmd == '\r') {
      return;
    }

    if (cmd == 'd' || cmd == 'D') {
      darkValue = readAverage();
      Serial.print("Dark value saved: ");
      Serial.println(darkValue);
    }
    else if (cmd == 'l' || cmd == 'L') {
      lightValue = readAverage();
      Serial.print("Light value saved: ");
      Serial.println(lightValue);
    }
    else if (cmd == 's' || cmd == 'S') {
      Serial.print("Saved dark value: ");
      Serial.println(darkValue);
      Serial.print("Saved light value: ");
      Serial.println(lightValue);
    }
  }

  int raw = readAverage(5);

  // Prevent wrong order
  int minVal = darkValue;
  int maxVal = lightValue;
  if (minVal > maxVal) {
    int temp = minVal;
    minVal = maxVal;
    maxVal = temp;
  }

  // Keep raw inside the calibrated range
  int clipped = constrain(raw, minVal, maxVal);

  int percent = 0;
  if (maxVal != minVal) {
    percent = map(clipped, minVal, maxVal, 0, 100);
  }

  Serial.print("Raw: ");
  Serial.print(raw);
  Serial.print("   Dark: ");
  Serial.print(darkValue);
  Serial.print("   Light: ");
  Serial.print(lightValue);
  Serial.print("   Light %: ");
  Serial.println(percent);

  delay(300);
}