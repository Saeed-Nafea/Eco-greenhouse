"""
LeafLink Server - WiFi Edition
Run:  python server.py
      python server.py --simulate
      python server.py --port 5000
Open: http://localhost:5000

Updates in this version:
- Dynamic plant list with an Add Plant dashboard flow.
- Custom setpoints are saved for each plant.
- Modern dashboard UI, voice alarms, safe manual control, and protected CSV autosave.
"""

import argparse
import copy
import csv
import io
import json
import os
import random
import re
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path


from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO

SAVE_INTERVAL = 60       # protected CSV autosave interval, in seconds
MAX_HISTORY   = 500
LOG_DIR       = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LED_ON_SECONDS   = 6 * 3600
LED_OFF_SECONDS  = 6 * 3600
SERVER_BOOT_TIME = time.time()

# Temperature actuator hysteresis.
# Example: with a 24-32 C range and 2 C hysteresis:
#   heater starts below 24 C and stays ON until 26 C
#   cooling starts above 32 C and stays ON until 30 C
TEMP_HYSTERESIS_C = 2.0

PLANTS_FILE = Path("plants.json")
SP_FILE     = Path("setpoints.json")

DEFAULT_PLANTS = [
    {
        "name": "Lavender",
        "icon": "🌸",
        "bg": "#f0fdf4",
        "subtitle": "Lavandula angustifolia - Mediterranean · Full sun",
        "setpoints": {
            "temp":       {"min": 18, "max": 26, "target": 22},
            "humidity":   {"min": 35, "max": 70, "target": 40},
            "soil":       {"min": 15, "max": 30, "target": 22},
            "lux":        {"min": 25000, "max": 60000, "target": 42500},
            "water_warn": 20,
        },
    },
    {
        "name": "Sunflower",
        "icon": "🌻",
        "bg": "#fefce8",
        "subtitle": "Helianthus annuus - Annual · Full sun",
        "setpoints": {
            "temp":       {"min": 20, "max": 30, "target": 25},
            "humidity":   {"min": 40, "max": 70, "target": 50},
            "soil":       {"min": 35, "max": 55, "target": 45},
            "lux":        {"min": 30000, "max": 70000, "target": 50000},
            "water_warn": 25,
        },
    },
    {
        "name": "Zanthoxylum fagara",
        "icon": "🌿",
        "bg": "#fdf4ff",
        "subtitle": "Zanthoxylum fagara - Subtropical · Part-full sun",
        "setpoints": {
            "temp":       {"min": 24, "max": 32, "target": 25},
            "humidity":   {"min": 45, "max": 70, "target": 55},
            "soil":       {"min": 50, "max": 70, "target": 30},
            "lux":        {"min": 15000, "max": 60000, "target": 37500},
            "water_warn": 20,
        },
    },
]

CUSTOM_SETPOINTS = {
    "temp":       {"min": 18, "max": 28, "target": 23},
    "humidity":   {"min": 40, "max": 70, "target": 55},
    "soil":       {"min": 30, "max": 60, "target": 45},
    "lux":        {"min": 15000, "max": 60000, "target": 35000},
    "water_warn": 25,
}

app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

plants       = copy.deepcopy(DEFAULT_PLANTS)
histories    = []
setpoints    = []
node_info    = []
active_plant = 0

# Per-plant temperature correction latch: "off", "heating", or "cooling".
# This lets heater/fans keep running past the exact min/max setpoint
# until the temperature returns inside the range by TEMP_HYSTERESIS_C.
temp_control_modes = []

# Per-plant monotonically increasing sample id. This lets CSV autosave append
# only new samples, instead of duplicating or losing rows.
plant_sequences = []
last_saved_sequences = []
log_save_lock = threading.Lock()

# Actuator control:
#   auto   -> server setpoints/sensor logic controls relays
#   manual -> dashboard ON/OFF buttons control relays directly
control_mode = "auto"
manual_actuators = {
    "fan":      False,
    "heater":   False,
    "dist_fan": False,
    "pump":     False,
    "led":      False,
}
VALID_ACTUATORS = set(manual_actuators.keys())

ACTUATOR_LABELS = {
    "fan": "Cooling Fan",
    "heater": "Heater",
    "dist_fan": "Distribution Fan",
    "pump": "Water Pump",
    "led": "LED Grow Light",
}

# Direction means the physical tendency of the actuator. The safety checker uses
# both max/min and target values: for example, if temperature is already at the
# target or max, manually switching the heater ON requires confirmation.
ACTUATOR_IMPACTS = {
    "heater": [
        {"sensor": "temperature", "setpoint": "temp", "label": "Temperature", "unit": "°C", "direction": "increase"},
    ],
    "fan": [
        {"sensor": "temperature", "setpoint": "temp", "label": "Temperature", "unit": "°C", "direction": "decrease"},
    ],
    "dist_fan": [
        {"sensor": "temperature", "setpoint": "temp", "label": "Temperature", "unit": "°C", "direction": "decrease"},
        {"sensor": "humidity", "setpoint": "humidity", "label": "Humidity", "unit": "%", "direction": "decrease"},
    ],
    "pump": [
        {"sensor": "soil", "setpoint": "soil", "label": "Soil moisture", "unit": "%", "direction": "increase"},
    ],
    # No light-related warnings are generated for the LED grow light.
    # The LED can still be controlled manually, but it will not trigger
    # light/lux safety popups or voice alarms.
    "led": [],
}

_lock = threading.Lock()


def clean_number(value, fallback):
    try:
        n = float(value)
        return int(n) if n.is_integer() else n
    except (TypeError, ValueError):
        return fallback


def normalize_range(src, fallback):
    src = src if isinstance(src, dict) else {}
    lo = clean_number(src.get("min"), fallback["min"])
    hi = clean_number(src.get("max"), fallback["max"])
    target = clean_number(src.get("target"), fallback["target"])
    if lo > hi:
        lo, hi = hi, lo
    target = min(max(target, lo), hi)
    return {"min": lo, "max": hi, "target": target}


def normalize_setpoints(src=None):
    src = src if isinstance(src, dict) else {}
    base = copy.deepcopy(CUSTOM_SETPOINTS)
    return {
        "temp": normalize_range(src.get("temp"), base["temp"]),
        "humidity": normalize_range(src.get("humidity"), base["humidity"]),
        "soil": normalize_range(src.get("soil"), base["soil"]),
        "lux": normalize_range(src.get("lux"), base["lux"]),
        "water_warn": clean_number(src.get("water_warn"), base["water_warn"]),
    }


def normalize_hex(color, fallback="#f0fdf4"):
    color = str(color or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color
    return fallback


def normalize_plant(src=None, fallback=None):
    src = src if isinstance(src, dict) else {}
    fallback = fallback or {}
    name = str(src.get("name") or fallback.get("name") or "New Plant").strip()[:60]
    if not name:
        name = "New Plant"
    icon = str(src.get("icon") or fallback.get("icon") or "🌱").strip()[:8] or "🌱"
    subtitle = str(
        src.get("subtitle") or src.get("species") or fallback.get("subtitle") or "Custom plant - tune setpoints from dashboard"
    ).strip()[:140]
    bg = normalize_hex(src.get("bg") or fallback.get("bg"), fallback.get("bg", "#f0fdf4"))
    sp = normalize_setpoints(src.get("setpoints") or fallback.get("setpoints"))
    return {"name": name, "icon": icon, "bg": bg, "subtitle": subtitle, "setpoints": sp}


def ensure_state_lengths():
    global active_plant
    count = len(plants)

    while len(setpoints) < count:
        setpoints.append(copy.deepcopy(plants[len(setpoints)].get("setpoints", CUSTOM_SETPOINTS)))
    del setpoints[count:]

    for i, plant in enumerate(plants):
        setpoints[i] = normalize_setpoints(plant.get("setpoints", setpoints[i]))
        plant["setpoints"] = copy.deepcopy(setpoints[i])

    while len(histories) < count:
        histories.append([])
    del histories[count:]

    while len(node_info) < count:
        node_info.append({})
    del node_info[count:]

    while len(temp_control_modes) < count:
        temp_control_modes.append("off")
    del temp_control_modes[count:]

    while len(plant_sequences) < count:
        plant_sequences.append(0)
    del plant_sequences[count:]

    while len(last_saved_sequences) < count:
        last_saved_sequences.append(0)
    del last_saved_sequences[count:]

    if count == 0:
        plants.append(normalize_plant())
        ensure_state_lengths()
    elif active_plant >= count:
        active_plant = 0


def public_plants():
    ensure_state_lengths()
    return [
        {
            "id": i,
            "name": plant["name"],
            "icon": plant["icon"],
            "bg": plant["bg"],
            "subtitle": plant["subtitle"],
            "setpoints": copy.deepcopy(setpoints[i]),
        }
        for i, plant in enumerate(plants)
    ]


def save_plants_file():
    try:
        for i, plant in enumerate(plants):
            plant["setpoints"] = copy.deepcopy(setpoints[i])
        PLANTS_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Plants] Write error: {e}")


def save_setpoints_file():
    try:
        SP_FILE.write_text(json.dumps(setpoints, indent=2, ensure_ascii=False), encoding="utf-8")
        save_plants_file()
    except Exception as e:
        print(f"[SP] Write error: {e}")


def load_persistent_config():
    global plants, setpoints

    plants = copy.deepcopy(DEFAULT_PLANTS)

    if PLANTS_FILE.exists():
        try:
            raw = json.loads(PLANTS_FILE.read_text(encoding="utf-8"))
            raw_plants = raw.get("plants") if isinstance(raw, dict) else raw
            if isinstance(raw_plants, list) and raw_plants:
                plants = [normalize_plant(p, DEFAULT_PLANTS[i] if i < len(DEFAULT_PLANTS) else None)
                          for i, p in enumerate(raw_plants)]
                print(f"[Plants] Loaded {len(plants)} saved plant(s).")
        except Exception as e:
            print(f"[Plants] Could not load: {e}")

    setpoints = [normalize_setpoints(p.get("setpoints")) for p in plants]

    # Backward compatible loader for the original setpoints.json list.
    if SP_FILE.exists():
        try:
            raw = json.loads(SP_FILE.read_text(encoding="utf-8"))
            raw_sps = None
            if isinstance(raw, list):
                raw_sps = raw
            elif isinstance(raw, dict) and isinstance(raw.get("setpoints"), list):
                raw_sps = raw["setpoints"]
            elif isinstance(raw, dict) and isinstance(raw.get("plants"), list):
                raw_sps = [p.get("setpoints") for p in raw["plants"] if isinstance(p, dict)]

            if raw_sps:
                while len(plants) < len(raw_sps):
                    plants.append(normalize_plant({"name": f"Plant {len(plants) + 1}"}))
                for i, sp in enumerate(raw_sps):
                    setpoints[i] = normalize_setpoints(sp)
                    plants[i]["setpoints"] = copy.deepcopy(setpoints[i])
                print("[SP] Loaded saved setpoints.")
        except Exception as e:
            print(f"[SP] Could not load: {e}")

    ensure_state_lengths()


def plant_name(pi: int) -> str:
    if 0 <= pi < len(plants):
        return plants[pi]["name"]
    return "ESP32"


def get_led_state() -> str:
    elapsed = time.time() - SERVER_BOOT_TIME
    phase   = elapsed % (LED_ON_SECONDS + LED_OFF_SECONDS)
    return "ON" if phase < LED_ON_SECONDS else "OFF"


def led_phase_info() -> dict:
    elapsed   = time.time() - SERVER_BOOT_TIME
    cycle_len = LED_ON_SECONDS + LED_OFF_SECONDS
    phase     = elapsed % cycle_len
    if phase < LED_ON_SECONDS:
        return {"state": "ON",  "remaining": int(LED_ON_SECONDS - phase)}
    return {"state": "OFF", "remaining": int(cycle_len - phase)}


def water_raw_to_pct(raw) -> float:
    try:
        return round(min(max((float(raw) / 4095.0) * 100.0, 0.0), 100.0), 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def compute_actuators(data: dict, sp: dict, plant_id: int = 0) -> dict:
    T = float(data.get("temperature", 0))
    S = float(data.get("soil", 0))

    temp_min = float(sp["temp"]["min"])
    temp_max = float(sp["temp"]["max"])

    # If the configured range is very narrow, shrink the hysteresis so the
    # heater and cooling fan do not fight each other.
    temp_span = max(temp_max - temp_min, 0.0)
    hysteresis = min(TEMP_HYSTERESIS_C, temp_span / 2.0) if temp_span else 0.0
    heater_stop_temp = temp_min + hysteresis
    cooler_stop_temp = temp_max - hysteresis

    # Stateful temperature control:
    #   - Start heating below min; keep heating until min + hysteresis.
    #   - Start cooling above max; keep cooling until max - hysteresis.
    # This prevents relay chatter right at the setpoint boundary.
    try:
        mode = temp_control_modes[plant_id]
    except (IndexError, TypeError):
        mode = "off"

    if T < temp_min:
        mode = "heating"
    elif T > temp_max:
        mode = "cooling"
    elif mode == "heating" and T >= heater_stop_temp:
        mode = "off"
    elif mode == "cooling" and T <= cooler_stop_temp:
        mode = "off"
    elif mode not in ("off", "heating", "cooling"):
        mode = "off"

    if 0 <= plant_id < len(temp_control_modes):
        temp_control_modes[plant_id] = mode

    soil_dry = S < sp["soil"]["min"]

    fan_on      = mode == "cooling"
    heater_on   = mode == "heating"
    # Use the distribution fan in both temperature correction modes.
    # Cooling: cooling fan + distribution fan ON, heater OFF.
    # Heating: heater + distribution fan ON, cooling fan OFF.
    dist_fan_on = mode in ("cooling", "heating")
    pump_on     = soil_dry
    led_on      = data.get("led_state", "OFF") == "ON"

    return {
        "fan":      fan_on,
        "heater":   heater_on,
        "dist_fan": dist_fan_on,
        "pump":     pump_on,
        "led":      led_on,
    }


def effective_actuators(data: dict, sp: dict, plant_id: int = 0) -> dict:
    """Return the actuator states that should actually be sent/displayed."""
    if control_mode == "manual":
        return dict(manual_actuators)
    return compute_actuators(data, sp, plant_id)


def control_payload() -> dict:
    return {
        "mode": control_mode,
        "manual_actuators": dict(manual_actuators),
    }


def add_sensor_warning(warnings: list, code: str, param: str, wtype: str, msg: str, severity: str = "warning"):
    warnings.append({
        "code": code,
        "param": param,
        "type": wtype,
        "severity": severity,
        "msg": msg,
    })


def build_warnings(data: dict, sp: dict) -> list:
    warnings  = []
    T         = float(data.get("temperature", 0))
    H         = float(data.get("humidity", 0))
    S         = float(data.get("soil", 0))
    water_pct = float(data.get("water_pct", 0))

    if T > sp["temp"]["max"]:
        add_sensor_warning(warnings, "temp_high", "Temperature", "high",
            f"Temperature {T:.1f}°C is above max {sp['temp']['max']}°C - cooling fan + dist fan ON", "critical")
    elif T < sp["temp"]["min"]:
        add_sensor_warning(warnings, "temp_low", "Temperature", "low",
            f"Temperature {T:.1f}°C is below min {sp['temp']['min']}°C - heater + dist fan ON", "critical")

    if H > sp["humidity"]["max"]:
        add_sensor_warning(warnings, "humidity_high", "Humidity", "high",
            f"Humidity {H:.1f}% is above max {sp['humidity']['max']}%", "warning")
    elif H < sp["humidity"]["min"]:
        add_sensor_warning(warnings, "humidity_low", "Humidity", "low",
            f"Humidity {H:.1f}% is below min {sp['humidity']['min']}%", "warning")

    if S < sp["soil"]["min"]:
        add_sensor_warning(warnings, "soil_low", "Soil", "low",
            f"Soil moisture {S:.1f}% is below min {sp['soil']['min']}% - pump ON", "warning")
    elif S > sp["soil"]["max"]:
        add_sensor_warning(warnings, "soil_high", "Soil", "high",
            f"Soil moisture {S:.1f}% is above max {sp['soil']['max']}%", "critical")

    if water_pct < sp["water_warn"]:
        add_sensor_warning(warnings, "water_low", "Water Level", "low",
            f"Water tank at {water_pct:.1f}% - refill needed", "critical")

    return warnings

def ingest(plant_id: int, data: dict, remote_ip: str = ""):
    ensure_state_lengths()

    raw_water = data.get("water", 0)
    data["water_raw"]    = int(raw_water)
    data["water_pct"]    = water_raw_to_pct(raw_water)
    data["water"]        = data["water_pct"]
    data["received_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data["led_state"] = get_led_state()
    data["led_phase"] = led_phase_info()

    latest_actuators = {}

    for pi in range(len(plants)):
        sp    = setpoints[pi]
        pdata = dict(data)
        pdata["plant_id"]  = pi
        pdata["warnings"]  = build_warnings(pdata, sp)
        pdata["actuators"] = effective_actuators(pdata, sp, pi)
        latest_actuators = pdata["actuators"]

        node_info[pi] = {"ip": remote_ip, "last_seen": pdata["received_at"]}

        with _lock:
            plant_sequences[pi] += 1
            pdata["_seq"] = plant_sequences[pi]
            histories[pi].append(pdata)
            if len(histories[pi]) > MAX_HISTORY:
                histories[pi].pop(0)

        socketio.emit("sensor_update", {"plant_id": pi, "data": pdata})

    src = plant_name(int(plant_id))
    acts = latest_actuators
    print(
        f"[{src:18s}] "
        f"T={data.get('temperature','?')}°C  "
        f"H={data.get('humidity','?')}%  "
        f"Soil={data.get('soil','?')}%  "
        f"lux={data.get('lux','?')}  "
        f"water={data['water_pct']:.1f}%  "
        f"LED={data['led_state']}  "
        f"Heater={'ON' if acts.get('heater') else 'off'}  "
        f"CoolFan={'ON' if acts.get('fan') else 'off'}  "
        f"DistFan={'ON' if acts.get('dist_fan') else 'off'}  "
        f"Pump={'ON' if acts.get('pump') else 'off'}  "
        f"from {remote_ip}"
    )


def simulator():
    state = {
        "temperature": 22.0,
        "humidity":    55.0,
        "soil":        40.0,
        "lux":         40000.0,
        "water":       2500.0,
    }
    DRIFT = [
        ("temperature",  10,    40,      0.4),
        ("humidity",     20,    95,      0.9),
        ("soil",          0,   100,      1.5),
        ("lux",           0, 120000,   900.0),
        ("water",       500,  4095,     50.0),
    ]
    while True:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for key, lo, hi, step in DRIFT:
            state[key] = max(lo, min(hi, state[key] + (random.random() - 0.5) * step * 2))
        data = {k: round(v, 2) for k, v in state.items()}
        data.update({"plant_id": 0, "timestamp": ts})
        ingest(0, data, remote_ip="simulator")
        time.sleep(2)


CSV_HEADERS = [
    "Sample ID", "Received At", "Timestamp (ESP32)",
    "Temperature (C)", "Humidity (%RH)", "Soil (%)",
    "Light (lux)", "Water (%)", "Water (raw)", "LED State",
    "Cooling Fan", "Heater", "Dist. Fan", "Pump",
    "Temp OK", "Humidity OK", "Soil OK", "Water OK",
    "Node IP", "Warnings",
]


def log_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def safe_filename_part(name: str, index: int) -> str:
    """Return a filesystem-safe plant name for CSV filenames."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", str(name or "")).strip(" .-")
    if not cleaned:
        cleaned = f"Plant-{index + 1}"
    return cleaned[:60]


def csv_path_for_plant(plant_id: int, plant: dict = None, date_str: str = None) -> Path:
    """Daily per-plant CSV path. One file per plant avoids workbook corruption risk."""
    date_str = date_str or log_date()
    plant = plant or (plants[plant_id] if 0 <= plant_id < len(plants) else {})
    safe_name = safe_filename_part(plant.get("name", "Plant"), plant_id)
    return LOG_DIR / f"leaflink_{date_str}_{plant_id + 1:02d}_{safe_name}.csv"


def csv_log_paths(date_str: str = None) -> list:
    """Return the CSV logs for one day."""
    date_str = date_str or log_date()
    return sorted(LOG_DIR.glob(f"leaflink_{date_str}_*.csv"))


def ensure_csv_ready(path: Path):
    """Prepare a CSV file without using temp files.

    - Creates the logs folder if needed.
    - Adds the header to new/empty files.
    - If a previous crash left the final line without a newline, appends one
      before writing the next row so future rows cannot merge into it.
    """
    path.parent.mkdir(exist_ok=True)
    needs_header = (not path.exists()) or path.stat().st_size == 0

    if path.exists() and path.stat().st_size > 0:
        with path.open("a+b") as raw:
            raw.seek(-1, os.SEEK_END)
            last_byte = raw.read(1)
            if last_byte not in (b"\n", b"\r"):
                raw.write(b"\n")
                raw.flush()
                os.fsync(raw.fileno())

    if needs_header:
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
            f.flush()
            os.fsync(f.fileno())


def build_csv_row(plant_id: int, data: dict) -> list:
    """Convert one sensor sample to a stable CSV row."""
    sp = setpoints[plant_id]
    T = float(data.get("temperature", 0))
    H = float(data.get("humidity", 0))
    S = float(data.get("soil", 0))
    L = float(data.get("lux", 0))
    water_pct = float(data.get("water_pct", water_raw_to_pct(data.get("water_raw", 0))))
    water_raw = data.get("water_raw", 0)
    acts = data.get("actuators", {})

    t_ok = "OK" if sp["temp"]["min"] <= T <= sp["temp"]["max"] else "!"
    h_ok = "OK" if sp["humidity"]["min"] <= H <= sp["humidity"]["max"] else "!"
    s_ok = "OK" if sp["soil"]["min"] <= S <= sp["soil"]["max"] else "!"
    w_ok = "OK" if water_pct >= sp["water_warn"] else "Low"
    warns = "; ".join(w.get("msg", "") for w in data.get("warnings", [])) or "-"

    return [
        int(data.get("_seq", 0)),
        data.get("received_at", "-"),
        data.get("timestamp", "-"),
        T, H, S,
        L,
        round(water_pct, 1),
        int(water_raw),
        data.get("led_state", "-"),
        "ON" if acts.get("fan") else "OFF",
        "ON" if acts.get("heater") else "OFF",
        "ON" if acts.get("dist_fan") else "OFF",
        "ON" if acts.get("pump") else "OFF",
        t_ok, h_ok, s_ok, w_ok,
        node_info[plant_id].get("ip", "-"),
        warns,
    ]


def save_to_csv():
    """Append only new samples to per-plant CSV files.

    CSV is used instead of XLSX for live logging because XLSX is a ZIP-based
    workbook that must be rewritten as a whole file. Appending CSV rows avoids
    .tmp/.bak files and greatly reduces corruption risk during frequent saves.
    Each successful write is flushed and fsynced before sequences are marked as
    saved.
    """
    ensure_state_lengths()

    if not log_save_lock.acquire(blocking=False):
        print("[CSV] Save already running - skipped duplicate request")
        return

    try:
        with _lock:
            snapshot = [list(h) for h in histories]
            saved_before = list(last_saved_sequences)

        saved_rows = 0
        touched_paths = []
        max_saved_by_plant = saved_before[:]

        for pi, plant in enumerate(list(plants)):
            hist = snapshot[pi] if pi < len(snapshot) else []
            prev_seq = int(saved_before[pi]) if pi < len(saved_before) else 0
            new_rows = [d for d in hist if int(d.get("_seq", 0)) > prev_seq]
            if not new_rows:
                continue

            path = csv_path_for_plant(pi, plant)
            ensure_csv_ready(path)

            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for d in new_rows:
                    writer.writerow(build_csv_row(pi, d))
                    max_saved_by_plant[pi] = max(max_saved_by_plant[pi], int(d.get("_seq", 0)))
                    saved_rows += 1
                f.flush()
                os.fsync(f.fileno())

            touched_paths.append(str(path))

        if saved_rows == 0:
            return

        with _lock:
            for pi, seq in enumerate(max_saved_by_plant):
                if pi < len(last_saved_sequences):
                    last_saved_sequences[pi] = max(last_saved_sequences[pi], seq)

        ts_now = datetime.now().strftime("%H:%M:%S")
        first_path = touched_paths[0] if len(touched_paths) == 1 else str(LOG_DIR)
        print(f"[CSV] Autosaved {saved_rows} row(s) -> {first_path}  ({ts_now})")
        payload = {"paths": touched_paths, "path": first_path, "time": ts_now, "rows": saved_rows}
        socketio.emit("csv_saved", payload)

    except Exception as e:
        print(f"[CSV] Save error: {e}")
        socketio.emit("csv_error", {"error": str(e)})
    finally:
        log_save_lock.release()


def hourly_saver():
    print(f"[CSV] Protected autosave every {SAVE_INTERVAL}s")
    time.sleep(SAVE_INTERVAL)
    while True:
        save_to_csv()
        time.sleep(SAVE_INTERVAL)

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/plants", methods=["GET", "POST"])
def api_plants():
    global active_plant

    if request.method == "GET":
        return jsonify({"plants": public_plants(), "active_plant": active_plant})

    data = request.get_json(force=True) or {}
    plant = normalize_plant(data)
    plants.append(plant)
    setpoints.append(copy.deepcopy(plant["setpoints"]))
    histories.append([])
    node_info.append({})
    save_setpoints_file()
    active_plant = len(plants) - 1
    payload = {"plants": public_plants(), "active_plant": active_plant}
    socketio.emit("plants_updated", payload)
    socketio.emit("active_plant_changed", {"plant_id": active_plant})
    print(f"[Plants] Added plant {active_plant}: {plant['name']}")
    return jsonify({"ok": True, **payload})


@app.route("/api/data", methods=["POST"])
def api_data():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no JSON"}), 400

    ensure_state_lengths()
    pi = int(data.get("plant_id", 0))
    if pi < 0 or pi >= len(plants):
        return jsonify({"error": f"invalid plant_id: use 0-{len(plants) - 1}"}), 400

    ingest(pi, data, request.remote_addr)

    sp   = setpoints[active_plant]
    acts = effective_actuators(data, sp, active_plant)

    return jsonify({
        "ok": True,
        "active_id": active_plant,
        "setpoints": sp,
        "control_mode": control_mode,
        "manual_actuators": dict(manual_actuators),
        "actuators": acts,
        # The ESP32 sketch reads LED from this string, so manual mode overrides it here.
        "led_state": "ON" if acts.get("led") else "OFF",
        "led_phase": led_phase_info(),
    })


@app.route("/api/set_active_plant", methods=["POST"])
def set_active_plant():
    global active_plant
    data     = request.get_json(force=True)
    plant_id = int(data.get("plant_id", 0))
    if plant_id < 0 or plant_id >= len(plants):
        return jsonify({"error": f"invalid plant_id: use 0-{len(plants) - 1}"}), 400
    active_plant = plant_id
    print(f"[System] Active plant -> {plant_name(active_plant)}")
    socketio.emit("active_plant_changed", {"plant_id": active_plant})
    return jsonify({
        "ok": True,
        "active_plant": active_plant,
        "plant_name": plant_name(active_plant),
    })


@app.route("/api/active_plant")
def api_active_plant():
    return jsonify({
        "active_plant": active_plant,
        "plant_name": plant_name(active_plant),
    })


@app.route("/api/history")
def api_history():
    ensure_state_lengths()
    pi = int(request.args.get("plant_id", 0))
    if pi < 0 or pi >= len(plants):
        return jsonify([])
    with _lock:
        return jsonify(list(histories[pi]))


@app.route("/api/setpoints", methods=["GET", "POST"])
def api_setpoints():
    ensure_state_lengths()
    pi = int(request.args.get("plant_id", 0))
    if pi < 0 or pi >= len(plants):
        return jsonify({"error": f"invalid plant_id: use 0-{len(plants) - 1}"}), 400
    if request.method == "POST":
        data = request.get_json(force=True)
        setpoints[pi].update(normalize_setpoints({**setpoints[pi], **data}))
        plants[pi]["setpoints"] = copy.deepcopy(setpoints[pi])
        save_setpoints_file()
        socketio.emit("setpoints_updated", {"plant_id": pi, "setpoints": setpoints[pi]})
        socketio.emit("plants_updated", {"plants": public_plants(), "active_plant": active_plant})
        return jsonify({"ok": True})
    return jsonify(setpoints[pi])




def latest_data_for_safety(plant_id: int):
    ensure_state_lengths()
    if plant_id < 0 or plant_id >= len(plants):
        return None
    with _lock:
        if not histories[plant_id]:
            return None
        return dict(histories[plant_id][-1])


def format_sensor_value(value: float, unit: str) -> str:
    if unit == "lux":
        return f"{value:.0f} lux"
    return f"{value:.1f}{unit}"


def actuator_safety_warnings(actuator: str, state: bool, plant_id: int = None) -> list:
    if not state:
        return []
    if plant_id is None:
        plant_id = active_plant
    sp = setpoints[plant_id]
    latest = latest_data_for_safety(plant_id)
    label = ACTUATOR_LABELS.get(actuator, actuator)

    if not latest:
        return [{
            "actuator": actuator,
            "severity": "warning",
            "msg": f"No live sensor reading is available for {plant_name(plant_id)}. Confirm before turning {label} ON manually.",
        }]

    warnings = []

    for impact in ACTUATOR_IMPACTS.get(actuator, []):
        sensor_key = impact["sensor"]
        sp_key = impact["setpoint"]
        direction = impact["direction"]
        unit = impact["unit"]
        sensor_label = impact["label"]

        try:
            value = float(latest.get(sensor_key, 0))
            lo = float(sp[sp_key]["min"])
            hi = float(sp[sp_key]["max"])
            target = float(sp[sp_key].get("target", (lo + hi) / 2))
        except Exception:
            continue

        value_txt = format_sensor_value(value, unit)
        target_txt = format_sensor_value(target, unit)
        min_txt = format_sensor_value(lo, unit)
        max_txt = format_sensor_value(hi, unit)

        if direction == "increase":
            if value >= hi:
                warnings.append({
                    "actuator": actuator,
                    "severity": "critical",
                    "msg": f"{label} may damage the plant: {sensor_label} is already {value_txt}, at/above the max setpoint {max_txt}.",
                })
            elif value >= target:
                warnings.append({
                    "actuator": actuator,
                    "severity": "warning",
                    "msg": f"{label} may stress the plant: {sensor_label} is already {value_txt}, at/above the target {target_txt}.",
                })
        elif direction == "decrease":
            if value <= lo:
                warnings.append({
                    "actuator": actuator,
                    "severity": "critical",
                    "msg": f"{label} may damage the plant: {sensor_label} is already {value_txt}, at/below the min setpoint {min_txt}.",
                })
            elif value <= target:
                warnings.append({
                    "actuator": actuator,
                    "severity": "warning",
                    "msg": f"{label} may stress the plant: {sensor_label} is already {value_txt}, at/below the target {target_txt}.",
                })

    if actuator == "pump":
        try:
            water_pct = float(latest.get("water_pct", latest.get("water", 0)))
            water_warn = float(sp.get("water_warn", 25))
            if water_pct <= water_warn:
                warnings.append({
                    "actuator": actuator,
                    "severity": "critical",
                    "msg": f"Water Pump may run dry: water tank is {water_pct:.1f}%, at/below warning level {water_warn:.1f}%.",
                })
        except Exception:
            pass

    return warnings


def validate_manual_update(data: dict) -> list:
    confirm = bool(data.get("confirm_unsafe", False))
    if confirm:
        return []

    checks = []
    actuator = data.get("actuator")
    if actuator is not None:
        actuator = str(actuator)
        if actuator in VALID_ACTUATORS:
            checks.extend(actuator_safety_warnings(actuator, bool(data.get("state", False)), active_plant))

    updates = data.get("manual_actuators")
    if isinstance(updates, dict):
        for key, val in updates.items():
            if key in VALID_ACTUATORS:
                checks.extend(actuator_safety_warnings(key, bool(val), active_plant))

    return checks

@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    global control_mode

    if request.method == "GET":
        return jsonify(control_payload())

    data = request.get_json(force=True) or {}

    mode = data.get("mode")
    if mode is not None:
        mode = str(mode).lower()
        if mode not in ("auto", "manual"):
            return jsonify({"error": "mode must be 'auto' or 'manual'"}), 400
        control_mode = mode

    if control_mode == "manual":
        warnings = validate_manual_update(data)
        if warnings:
            return jsonify({
                "ok": False,
                "requires_confirmation": True,
                "warnings": warnings,
                "warning": "\n".join(w["msg"] for w in warnings),
                **control_payload(),
            }), 409

    # Accept one actuator update:
    #   {"actuator": "fan", "state": true}
    actuator = data.get("actuator")
    if actuator is not None:
        actuator = str(actuator)
        if actuator not in VALID_ACTUATORS:
            return jsonify({"error": f"invalid actuator: {actuator}"}), 400
        manual_actuators[actuator] = bool(data.get("state", False))

    # Or accept multiple actuator updates:
    #   {"manual_actuators": {"fan": true, "pump": false}}
    updates = data.get("manual_actuators")
    if isinstance(updates, dict):
        for key, val in updates.items():
            if key in VALID_ACTUATORS:
                manual_actuators[key] = bool(val)

    payload = control_payload()
    socketio.emit("control_updated", payload)
    print(f"[Control] mode={control_mode} manual={manual_actuators}")
    return jsonify({"ok": True, **payload})

@app.route("/api/save_now", methods=["POST"])
def api_save_now():
    threading.Thread(target=save_to_csv, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/nodes")
def api_nodes():
    ensure_state_lengths()
    return jsonify(node_info)


@app.route("/api/led_state")
def api_led_state():
    return jsonify(led_phase_info())


@app.route("/download/csv")
def download_csv():
    save_to_csv()
    paths = csv_log_paths()
    if not paths:
        return "No data saved yet.", 404

    if len(paths) == 1:
        path = paths[0]
        return send_file(
            path,
            as_attachment=True,
            download_name=path.name,
            mimetype="text/csv",
        )

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            zf.write(path, arcname=path.name)
    mem.seek(0)
    return send_file(
        mem,
        as_attachment=True,
        download_name=f"leaflink_logs_{log_date()}.zip",
        mimetype="application/zip",
    )


@app.route("/download/excel")
def download_excel():
    """Backward-compatible route: downloads the CSV logs now used by LeafLink."""
    return download_csv()


@socketio.on("connect")
def on_connect():
    print(f"[WS] Browser connected: {request.sid}")
    socketio.emit("plants_updated", {"plants": public_plants(), "active_plant": active_plant}, to=request.sid)
    socketio.emit("nodes_status", {"nodes": node_info}, to=request.sid)
    socketio.emit("active_plant_changed", {"plant_id": active_plant}, to=request.sid)
    socketio.emit("led_phase", led_phase_info(), to=request.sid)
    socketio.emit("control_updated", control_payload(), to=request.sid)


@socketio.on("disconnect")
def on_disconnect():
    print(f"[WS] Browser disconnected: {request.sid}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LeafLink WiFi Server")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args()

    load_persistent_config()

    if args.simulate:
        print("[Sim] Starting simulator - no ESP32 needed")
        threading.Thread(target=simulator, daemon=True).start()
    else:
        print("[WiFi] Waiting for ESP32 POSTs on /api/data ...")
        try:
            import socket as _sock
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"  Your local IP -> {s.getsockname()[0]}")
            s.close()
        except Exception:
            print("  Run ipconfig / ifconfig to find your local IP")

    threading.Thread(target=hourly_saver, daemon=True).start()

    phase = led_phase_info()
    print(f"\n{'='*60}")
    print(f"  LeafLink UI -> http://localhost:{args.port}")
    print(f"  ESP32 POST -> http://<PC-IP>:{args.port}/api/data")
    print(f"  CSV logs   -> {LOG_DIR.resolve()}")
    print(f"  LED now    -> {phase['state']}  "
          f"({phase['remaining'] // 3600}h "
          f"{(phase['remaining'] % 3600) // 60}m remaining in phase)")
    print(f"{'='*60}\n")

    socketio.run(app, host=args.host, port=args.port, debug=False)
