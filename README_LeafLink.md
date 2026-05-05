# LeafLink Dashboard

LeafLink is the updated plant monitoring dashboard based on your original GrowSense files.

## Run

```bash
pip install -r requirements.txt
python server.py --simulate
# or: python server.py --port 5000
```

Open: `http://localhost:5000`

## Main updates

- Rocker-style dark admin dashboard layout.
- Project name changed to LeafLink.
- Expandable sidebar:
  - Dashboards
    - Plant
      - each plant
        - Graphs
        - Setpoints
        - Live Data
      - Add Plant
    - Manual Mode
- Big graphs for Temperature, Humidity, Soil, Light, and Water.
- Dashed red min/max setpoint lines and dashed green target setpoint lines.
- Live gauges with percentage/range status.
- System status visible on every tab.
- Calm browser voice alarm for sensor warnings.
- One global Manual Mode page for all actuators.
- Manual ON actions are checked against live readings and setpoints before switching.
- Protected CSV autosave plus a Save CSV button.
- Footer: Capstone 19210 @2026.

## Notes

Browsers require one click before voice audio can play, so click the Alarm button once to enable calm voice alerts.


## Data logging

LeafLink now writes daily per-plant CSV logs in `logs/`. CSV is safer for live sensor logging than XLSX because each save appends rows instead of rewriting a zipped workbook. The save code does not create `.tmp` or `.bak` files; it flushes and fsyncs the CSV after writing so data is committed to disk before the sample sequence is marked as saved.
