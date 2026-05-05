#define BLYNK_TEMPLATE_ID "TMPL2egzT1UzN"
#define BLYNK_TEMPLATE_NAME "LeafLink"
#define BLYNK_AUTH_TOKEN "UNA9XYbYZDORbRpXLcmPQhIS9cgGaN5b"
#define BLYNK_PRINT Serial

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <Wire.h>
#include <RTClib.h>
#include <BlynkSimpleEsp32.h>
#include <Preferences.h>

const char* ssid      = "Nafea";
const char* password  = "19986100";

const char* serverDataUrl    = "http://10.216.30.167:5000/api/data";
const char* serverControlUrl = "http://10.216.30.167:5000/api/control";

// Match a dashboard plant tab. The server mirrors the live readings across all plant tabs.
const int PLANT_ID = 0;

const int DHT_PIN   = 4;
const int LDR_PIN   = 32;
const int SOIL_PIN  = 34;
const int WATER_PIN = 35;

const int RELAY_FAN      = 16;
const int RELAY_HEATER   = 17;
const int RELAY_PUMP     = 18;
const int RELAY_DIST_FAN = 19;
const int RELAY_LED      = 23;

DHTesp     dht;
RTC_DS3231 rtc;
Preferences prefs;

// Hidden soil moisture calibration.
// Send lowercase d in Serial Monitor while the probe is in dry soil/air.
// Send uppercase W in Serial Monitor while the probe is in fully wet soil/water.
const int DEFAULT_SOIL_DRY_RAW = 3500;
const int DEFAULT_SOIL_WET_RAW = 1200;
int soilDryRaw = DEFAULT_SOIL_DRY_RAW;
int soilWetRaw = DEFAULT_SOIL_WET_RAW;

inline void relayOn(int pin)  { digitalWrite(pin, HIGH); }
inline void relayOff(int pin) { digitalWrite(pin, LOW);  }

// Last known fan states, used for lux correction.
bool coolingFanOn = false;
bool distFanOnState = false;

// Blynk manual states.
bool blynkManualMode = false;
bool manualFan       = false;
bool manualDistFan   = false;
bool manualHeater    = false;
bool manualPump      = false;
bool manualLed       = false;

// Lux limits and gradual boost settings.
const int MIN_LUX = 30000;
const int MAX_LUX = 60000;
const int LUX_BOOST_STEP = 500;
int luxBoostAmount = 0;

// Pump timing settings.
const unsigned long PUMP_ON_TIME_MS  = 7000;
const unsigned long PUMP_OFF_TIME_MS = 50000;

bool pumpRunning = false;
bool pumpRequest = false;

unsigned long pumpStartedAt = 0;
unsigned long pumpStoppedAt = 0;

unsigned long lastBlynkReconnectAttempt = 0;

void updatePump() {
  unsigned long nowMs = millis();

  if (pumpRunning) {
    if (!pumpRequest || nowMs - pumpStartedAt >= PUMP_ON_TIME_MS) {
      relayOff(RELAY_PUMP);
      pumpRunning = false;
      pumpStoppedAt = nowMs;
    }
  } else {
    if (pumpRequest && nowMs - pumpStoppedAt >= PUMP_OFF_TIME_MS) {
      relayOn(RELAY_PUMP);
      pumpRunning = true;
      pumpStartedAt = nowMs;
    }
  }
}

int readSoilRawAverage(byte samples = 25) {
  long total = 0;
  for (byte i = 0; i < samples; i++) {
    total += analogRead(SOIL_PIN);
    delay(6);
  }
  return (int)(total / samples);
}

int soilRawToPct(int raw) {
  int span = soilWetRaw - soilDryRaw;
  if (span == 0) return 0;

  float pct = ((float)(raw - soilDryRaw) * 100.0f) / (float)span;
  return constrain((int)round(pct), 0, 100);
}

void printSoilCalibration() {
  Serial.print("Soil calibration -> dry=");
  Serial.print(soilDryRaw);
  Serial.print(" wet=");
  Serial.println(soilWetRaw);
}

void loadSoilCalibration() {
  prefs.begin("leaflink", false);
  soilDryRaw = prefs.getInt("soilDry", DEFAULT_SOIL_DRY_RAW);
  soilWetRaw = prefs.getInt("soilWet", DEFAULT_SOIL_WET_RAW);

  if (soilDryRaw < 0 || soilDryRaw > 4095) soilDryRaw = DEFAULT_SOIL_DRY_RAW;
  if (soilWetRaw < 0 || soilWetRaw > 4095) soilWetRaw = DEFAULT_SOIL_WET_RAW;

  printSoilCalibration();
  Serial.println("Hidden soil calibration: send d = save dry reading, send W = save full wet reading");
}

void handleSerialCalibration() {
  while (Serial.available() > 0) {
    char cmd = Serial.read();

    if (cmd == '\r' || cmd == '\n' || cmd == ' ' || cmd == '\t') {
      continue;
    }

    if (cmd == 'd') {
      soilDryRaw = readSoilRawAverage();
      prefs.putInt("soilDry", soilDryRaw);
      Serial.print("Saved DRY soil reading: ");
      Serial.println(soilDryRaw);
      printSoilCalibration();
    } else if (cmd == 'W') {
      soilWetRaw = readSoilRawAverage();
      prefs.putInt("soilWet", soilWetRaw);
      Serial.print("Saved FULL WET soil reading: ");
      Serial.println(soilWetRaw);
      printSoilCalibration();
    }
  }
}

const char* plantNameFromId(int id) {
  if (id == 0) return "Lavender";
  if (id == 1) return "Sunflower";
  if (id == 2) return "Zanthoxylum fagara";
  if (id == 3) return "New Plant";
  return "Plant";
}

void applyActuators(bool fanOn, bool heaterOn, bool distFanOn, bool ledOn, bool pumpOn) {
  coolingFanOn = fanOn;
  distFanOnState = distFanOn;

  if (fanOn) relayOn(RELAY_FAN);
  else       relayOff(RELAY_FAN);

  if (heaterOn) relayOn(RELAY_HEATER);
  else          relayOff(RELAY_HEATER);

  if (distFanOn) relayOn(RELAY_DIST_FAN);
  else           relayOff(RELAY_DIST_FAN);

  if (ledOn) relayOn(RELAY_LED);
  else       relayOff(RELAY_LED);

  pumpRequest = pumpOn;
  updatePump();
}

void applyBlynkManualOutputs() {
  if (!blynkManualMode) return;
  applyActuators(manualFan, manualHeater, manualDistFan, manualLed, manualPump);
}

void blynkWriteSafe(int vpin, int value) {
  if (Blynk.connected()) Blynk.virtualWrite(vpin, value);
}

void blynkWriteSafe(int vpin, float value) {
  if (Blynk.connected()) Blynk.virtualWrite(vpin, value);
}

void blynkWriteSafe(int vpin, const char* value) {
  if (Blynk.connected()) Blynk.virtualWrite(vpin, value);
}

void postControlMode(bool manualMode) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(serverControlUrl);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2500);

  String body = String("{\"mode\":\"") + (manualMode ? "manual" : "auto") + "\"}";
  int code = http.POST(body);

  Serial.print("Blynk mode -> server: ");
  Serial.print(manualMode ? "manual" : "auto");
  Serial.print(" HTTP=");
  Serial.println(code);

  http.end();
}

bool postControlActuator(const char* actuator, bool state) {
  // Return true only when the server accepted the command. If the server returns
  // HTTP 409, the dashboard/server has detected an unsafe manual action.
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.begin(serverControlUrl);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2500);

  String body = String("{\"actuator\":\"") + actuator + "\",\"state\":" + (state ? "true" : "false") + "}";
  int code = http.POST(body);
  String response = http.getString();

  Serial.print("Blynk actuator -> server: ");
  Serial.print(actuator);
  Serial.print("=");
  Serial.print(state ? "ON" : "OFF");
  Serial.print(" HTTP=");
  Serial.println(code);

  if (code == 409) {
    Serial.println("Unsafe manual action denied by server");
    blynkWriteSafe(V12, "Unsafe manual action denied");
  }

  http.end();
  return code >= 200 && code < 300;
}


BLYNK_CONNECTED() {
  Serial.println("Blynk connected");
  Blynk.virtualWrite(V11, plantNameFromId(PLANT_ID));
  Blynk.virtualWrite(V12, "ESP32 online");

  // Read the latest switch states from the Blynk app.
  Blynk.syncVirtual(V5, V6, V7, V8, V9, V10);
}

BLYNK_WRITE(V5) {
  blynkManualMode = param.asInt() == 1;
  postControlMode(blynkManualMode);

  if (blynkManualMode) {
    Blynk.virtualWrite(V12, "Manual mode");
    applyBlynkManualOutputs();
  } else {
    Blynk.virtualWrite(V12, "Automatic mode");
  }
}

BLYNK_WRITE(V6) {
  bool requested = param.asInt() == 1;
  if (requested && !postControlActuator("fan", true)) {
    manualFan = false;
    blynkWriteSafe(V6, 0);
    return;
  }
  manualFan = requested;
  if (!requested) postControlActuator("fan", false);
  applyBlynkManualOutputs();
}

BLYNK_WRITE(V7) {
  bool requested = param.asInt() == 1;
  if (requested && !postControlActuator("dist_fan", true)) {
    manualDistFan = false;
    blynkWriteSafe(V7, 0);
    return;
  }
  manualDistFan = requested;
  if (!requested) postControlActuator("dist_fan", false);
  applyBlynkManualOutputs();
}

BLYNK_WRITE(V8) {
  bool requested = param.asInt() == 1;
  if (requested && !postControlActuator("heater", true)) {
    manualHeater = false;
    blynkWriteSafe(V8, 0);
    return;
  }
  manualHeater = requested;
  if (!requested) postControlActuator("heater", false);
  applyBlynkManualOutputs();
}

BLYNK_WRITE(V9) {
  bool requested = param.asInt() == 1;
  if (requested && !postControlActuator("pump", true)) {
    manualPump = false;
    blynkWriteSafe(V9, 0);
    return;
  }
  manualPump = requested;
  if (!requested) postControlActuator("pump", false);
  applyBlynkManualOutputs();
}

BLYNK_WRITE(V10) {
  bool requested = param.asInt() == 1;
  if (requested && !postControlActuator("led", true)) {
    manualLed = false;
    blynkWriteSafe(V10, 0);
    return;
  }
  manualLed = requested;
  if (!requested) postControlActuator("led", false);
  applyBlynkManualOutputs();
}

void ensureBlynkConnection() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (Blynk.connected()) return;

  unsigned long nowMs = millis();
  if (nowMs - lastBlynkReconnectAttempt < 10000) return;
  lastBlynkReconnectAttempt = nowMs;

  Serial.println("Connecting to Blynk...");
  Blynk.connect(3000);
}

void setup() {
  Serial.begin(115200);
  loadSoilCalibration();

  dht.setup(DHT_PIN, DHTesp::DHT22);

  Wire.begin(21, 22);
  if (!rtc.begin()) {
    Serial.println("RTC not found!");
  }

  pinMode(RELAY_FAN,      OUTPUT); relayOff(RELAY_FAN);
  pinMode(RELAY_HEATER,   OUTPUT); relayOff(RELAY_HEATER);
  pinMode(RELAY_PUMP,     OUTPUT); relayOff(RELAY_PUMP);
  pinMode(RELAY_DIST_FAN, OUTPUT); relayOff(RELAY_DIST_FAN);
  pinMode(RELAY_LED,      OUTPUT); relayOff(RELAY_LED);

  // Allow the first pump request to start immediately, then use the normal delay cycle.
  pumpStoppedAt = millis() - PUMP_OFF_TIME_MS;

  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.print("Connected. IP: ");
  Serial.println(WiFi.localIP());

  Blynk.config(BLYNK_AUTH_TOKEN);
  Blynk.connect(5000);
}

void loop() {
  static unsigned long lastSend = 0;

  handleSerialCalibration();

  if (WiFi.status() == WL_CONNECTED) {
    ensureBlynkConnection();
    Blynk.run();
  }

  updatePump();

  if (millis() - lastSend < 2000) {
    return;
  }

  lastSend = millis();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost - reconnecting...");
    WiFi.disconnect();
    WiFi.begin(ssid, password);
    delay(5000);
    return;
  }

  float temperature = dht.getTemperature();
  float humidity    = dht.getHumidity();

  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("DHT read failed - skipping");
    blynkWriteSafe(V12, "DHT read failed");
    return;
  }

  int soilRaw = analogRead(SOIL_PIN);
  int soilPct = soilRawToPct(soilRaw);

  int ldrRaw = analogRead(LDR_PIN);
  float lux  = 0.0f;

  if (ldrRaw > 10) {
    lux = pow(42690.0f / (100.0f * (4095.0f / (float)ldrRaw - 1.0f)), 1.479f);
  }

  int finalLux = (int)(lux * 320.0f);

  // Fan correction:
  // Both fans ON         -> minus 90000
  // Cooling fan only ON  -> minus 20000
  // Dist fan only ON     -> minus 20000
  if (coolingFanOn && distFanOnState) {
    finalLux -= 90000;
  } else if (coolingFanOn && !distFanOnState) {
    finalLux -= 20000;
  } else if (!coolingFanOn && distFanOnState) {
    finalLux -= 20000;
  }

  // If LDR is fully dark/disconnected, or corrected lux reaches zero,
  // keep it at 0 and do not add boost.
  if (ldrRaw <= 10 || finalLux <= 0) {
    finalLux = 0;
    luxBoostAmount = 0;
  } else {
    if (finalLux < MIN_LUX) {
      luxBoostAmount += LUX_BOOST_STEP;
      finalLux += luxBoostAmount;
    } else {
      luxBoostAmount = 0;
    }

    if (finalLux < MIN_LUX) finalLux = MIN_LUX;
    if (finalLux > MAX_LUX) finalLux = MAX_LUX;
  }

  int waterRaw = analogRead(WATER_PIN);
  float waterPct = constrain((waterRaw / 4095.0f) * 100.0f, 0.0f, 100.0f);

  DateTime now = rtc.now();
  char timestamp[20];

  snprintf(timestamp, sizeof(timestamp), "%02d:%02d:%02d",
           now.hour(), now.minute(), now.second());

  float tempRounded = round(temperature * 10.0f) / 10.0f;
  float humRounded  = round(humidity    * 10.0f) / 10.0f;
  float waterRounded = round(waterPct   * 10.0f) / 10.0f;

  // Send live values to Blynk.
  blynkWriteSafe(V0, tempRounded);
  blynkWriteSafe(V1, humRounded);
  blynkWriteSafe(V2, soilPct);
  blynkWriteSafe(V3, finalLux);
  blynkWriteSafe(V4, waterRounded);
  blynkWriteSafe(V11, plantNameFromId(PLANT_ID));

  StaticJsonDocument<256> doc;

  doc["plant_id"]    = PLANT_ID;
  doc["temperature"] = tempRounded;
  doc["humidity"]    = humRounded;
  doc["soil"]        = soilPct;
  doc["lux"]         = finalLux;
  doc["water"]       = waterRaw;
  doc["timestamp"]   = timestamp;

  String payload;
  serializeJson(doc, payload);

  HTTPClient http;

  http.begin(serverDataUrl);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(3000);

  int code = http.POST(payload);

  if (code > 0) {
    String response = http.getString();

    StaticJsonDocument<1024> res;
    DeserializationError err = deserializeJson(res, response);

    if (!err) {
      JsonObject acts = res["actuators"].as<JsonObject>();

      bool fanOn     = acts["fan"]      | false;
      bool heaterOn  = acts["heater"]   | false;
      bool distFanOn = acts["dist_fan"] | false;
      bool pumpOn    = acts["pump"]     | false;

      const char* ledStr = res["led_state"] | "OFF";
      bool ledOn = (strcmp(ledStr, "ON") == 0) || (acts["led"] | false);

      const char* modeStr = res["control_mode"] | "auto";
      blynkManualMode = (strcmp(modeStr, "manual") == 0);

      int activeId = res["active_id"] | PLANT_ID;

      // The server decides the real outputs in automatic mode.
      // In manual mode, the server returns the manual states sent by Blynk/dashboard.
      applyActuators(fanOn, heaterOn, distFanOn, ledOn, pumpOn);

      // Update Blynk switches so the app displays actual relay requests.
      blynkWriteSafe(V5, blynkManualMode ? 1 : 0);
      blynkWriteSafe(V6, fanOn ? 1 : 0);
      blynkWriteSafe(V7, distFanOn ? 1 : 0);
      blynkWriteSafe(V8, heaterOn ? 1 : 0);
      blynkWriteSafe(V9, pumpOn ? 1 : 0);
      blynkWriteSafe(V10, ledOn ? 1 : 0);
      blynkWriteSafe(V11, plantNameFromId(activeId));
      blynkWriteSafe(V12, blynkManualMode ? "Manual mode" : "Automatic mode");

      Serial.print("T=");      Serial.print(tempRounded);
      Serial.print(" H=");     Serial.print(humRounded);
      Serial.print(" Soil=");  Serial.print(soilPct);
      Serial.print(" SoilRaw="); Serial.print(soilRaw);
      Serial.print(" Lux=");   Serial.print(finalLux);
      Serial.print(" WaterRaw="); Serial.print(waterRaw);
      Serial.print(" WaterPct="); Serial.print(waterRounded);

      Serial.print(" | Mode="); Serial.print(blynkManualMode ? "MANUAL" : "AUTO");
      Serial.print(" Fan=");    Serial.print(fanOn ? "ON" : "OFF");
      Serial.print(" Heat=");   Serial.print(heaterOn ? "ON" : "OFF");
      Serial.print(" Dist=");   Serial.print(distFanOn ? "ON" : "OFF");
      Serial.print(" PumpRequest="); Serial.print(pumpRequest ? "ON" : "OFF");
      Serial.print(" PumpActual=");  Serial.print(pumpRunning ? "ON" : "OFF");
      Serial.print(" LED=");    Serial.println(ledOn ? "ON" : "OFF");

    } else {
      Serial.print("JSON error: ");
      Serial.println(err.c_str());
      blynkWriteSafe(V12, "Server JSON error");
    }

  } else {
    Serial.print("HTTP error: ");
    Serial.println(code);
    blynkWriteSafe(V12, "Server offline");

    // If Blynk manual mode is ON, keep Blynk manual control working even if the server is offline.
    applyBlynkManualOutputs();
  }

  http.end();
}
