# AFib TinyML Monitor

> **Research prototype — not a medical diagnostic device.**

A professional real-time web dashboard for a low-cost ESP32-based AFib screening research system.

```
ESP32 + MAX30100
      ↓  USB Serial
Python FastAPI backend  (port 8000)
      ↓  WebSocket
React + Vite frontend   (port 5173)
```

---

## Project Structure

```
embedded/
├── backend/
│   ├── main.py             # FastAPI entry point + WebSocket broadcast
│   ├── serial_manager.py   # Serial reader thread + event routing
│   ├── parser.py           # Modular ESP32 line parser
│   ├── models.py           # Pydantic data models / session state
│   └── requirements.txt
│
├── frontend/
│   ├── src/
│   │   ├── components/     # UI building blocks
│   │   ├── pages/          # DashboardPage, ResearchPage
│   │   ├── hooks/          # useDashboardState (central state)
│   │   ├── types/          # TypeScript interfaces (centralized)
│   │   └── services/       # websocket.ts, api.ts
│   └── package.json
│
└── README.md
```

---

## 1. Installation

### 1a. Python environment

```powershell
# Create a virtual environment (recommended)
cd embedded\backend
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 1b. Node environment

```powershell
cd embedded\frontend
npm install
```

---

## 2. How to connect the ESP32

1. Plug the ESP32 into a USB port.
2. Note the COM port shown in Device Manager (e.g. `COM16`).
3. Make sure the ESP32 firmware is running and outputting serial data.

---

## 3. How to select the COM port

**Option A — from the dashboard UI (recommended):**
- Open the **Connection Control** panel in the dashboard.
- Click **Refresh** to list available ports.
- Select the correct port from the dropdown or type it in the text field.
- Click **Connect**.

**Option B — auto-connect via environment variable:**
```powershell
$env:DEFAULT_PORT = "COM16"
$env:DEFAULT_BAUD = "115200"
python main.py
```

---

## 4. How to start the backend

```powershell
cd embedded\backend
python main.py
```

Backend starts at: `http://localhost:8000`
WebSocket: `ws://localhost:8000/ws`
API docs: `http://localhost:8000/docs`

---

## 5. How to start the frontend

```powershell
cd embedded\frontend
npm run dev
```

Frontend starts at: `http://localhost:5173`

Open the URL in your browser.

---

## 6. Serial Protocol

The backend parses these lines from the ESP32:

| Line | Meaning |
|---|---|
| `BEAT,<millis>` | Heartbeat detected at ESP32 timestamp |
| `FIRST_BEAT` | First beat (no IBI available yet) |
| `IBI_DATA,<ms>` | Valid interbeat interval |
| `RAW_IBI,<ms>` | Raw IBI (pre-validation) |
| `REJECTED_SHORT,<ms>` | IBI rejected — too short |
| `REJECTED_LONG,<ms>` | IBI rejected — too long |
| `FEATURE_VECTOR_READY` | Feature extraction complete |
| `mean_ibi=<float>` | Feature: mean IBI |
| `median_ibi=<float>` | Feature: median IBI |
| `std_ibi=<float>` | Feature: std dev IBI |
| `min_ibi=<float>` | Feature: min IBI |
| `max_ibi=<float>` | Feature: max IBI |
| `iqr_ibi=<float>` | Feature: interquartile range |
| `cv_ibi=<float>` | Feature: coefficient of variation |
| `mean_abs_diff=<float>` | Feature: mean absolute difference |
| `std_diff=<float>` | Feature: std dev of differences |
| `rmssd=<float>` | Feature: RMSSD |
| `beat_count=<int>` | Feature: beat count |
| `suspicious_ibi_fraction=<float>` | Feature: suspicious IBI fraction |
| `missing_fraction=<float>` | Feature: missing fraction |
| `WINDOW_END` | 30-second window complete |

**Unknown lines** are logged as `INFO` and never crash the application.

**To add new line types:**
Edit `backend/parser.py` → append a `(regex, handler_fn)` tuple to `LINE_HANDLERS`.

---

## 7. WebSocket Protocol

The backend broadcasts JSON messages to all connected frontend clients.

### Beat event
```json
{ "type": "beat", "timestamp": 1234567890.0, "esp32_ts": 25339, "ibi_ms": 702 }
```

### Valid IBI
```json
{ "type": "ibi", "timestamp": 1234567890.0, "ibi_ms": 702, "valid": true }
```

### Rejected IBI
```json
{ "type": "rejected_ibi", "timestamp": 1234567890.0, "ibi_ms": 2968, "reason": "LONG" }
```

### Heart rate
```json
{ "type": "heart_rate", "timestamp": 1234567890.0, "bpm": 85.5 }
```

### Motion state
```json
{ "type": "motion", "timestamp": 1234567890.0, "state": "REST", "source": "simulated" }
```

### Signal quality
```json
{ "type": "quality", "timestamp": 1234567890.0, "quality": "GOOD", "valid_ibi_count": 42, "rejected_ibi_count": 2, "rejection_rate": 0.045 }
```

### Window progress
```json
{ "type": "window_progress", "timestamp": 1234567890.0, "elapsed_seconds": 15.0, "window_id": 3 }
```

### Window result (30-second window complete)
```json
{
  "type": "window_result",
  "timestamp": 1234567890.0,
  "window_id": 3,
  "features": { "mean_ibi": 762.08, "rmssd": 163.84, ... },
  "af_probability": null,
  "prediction": "WAITING",
  "quality": "GOOD",
  "motion_context": "REST",
  "motion_source": "simulated"
}
```

> **Note:** `af_probability` is `null` and `prediction` is `"WAITING"` until the ESP32 TinyML model produces output.
> The backend **never fabricates** model probabilities.

### Raw PPG sample (future)
```json
{ "type": "raw_ppg", "timestamp": 1234567890.0, "sample": 12345, "sample_rate_hz": 100 }
```

### System status
```json
{
  "type": "system_status",
  "timestamp": 1234567890.0,
  "esp32_connected": true,
  "serial_port": "COM16",
  "session_start": 1234567000.0,
  "simulated_motion": true,
  "motion_state": "REST"
}
```

---

## 8. Simulated Motion Mode

Because the physical MPU6050 is currently **unavailable** (damaged/overheating),
motion state is simulated and clearly labeled in the UI as **⚠ MOTION SOURCE: SIMULATED**.

### Toggle simulated state from the UI
Use the motion override buttons in the **Connection Control** panel:
`REST` | `LOW_MOTION` | `HIGH_MOTION`

### Toggle via REST API
```http
POST http://localhost:8000/api/motion/state
Content-Type: application/json

{ "state": "LOW_MOTION" }
```

### Turn simulated motion on/off
```python
# In serial_manager.py
self._start_motion_simulation()   # starts the cycling thread (default)
self._motion_stop.set()           # stop cycling
```

---

## 9. How to replace simulated motion with real MPU6050

When the MPU6050 becomes available:

1. **ESP32 firmware:** Send motion lines over serial, e.g.:
   ```
   MOTION,REST
   MOTION,LOW_MOTION
   MOTION,HIGH_MOTION
   ```

2. **Add parser handler** in `backend/parser.py`:
   ```python
   (re.compile(r"^MOTION,(REST|LOW_MOTION|HIGH_MOTION)$"), _parse_motion),
   ```
   With a `ParsedMotion` dataclass and `_parse_motion` handler.

3. **Add dispatch** in `serial_manager.py` `_dispatch()`:
   ```python
   elif isinstance(event, ParsedMotion):
       self.state.simulated_motion = False
       self._handle_motion(event)
   ```

4. **Stop simulation thread** in `_handle_motion()` if `simulated_motion` was True:
   ```python
   self._motion_stop.set()
   ```

The frontend requires **no changes** — it already handles both
`"source": "simulated"` and `"source": "mpu6050"`.

---

## 10. REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/status` | System status snapshot |
| GET | `/api/ports` | List available serial ports |
| POST | `/api/connect` | Connect to serial port `{ "port": "COM16", "baud": 115200 }` |
| POST | `/api/disconnect` | Disconnect |
| POST | `/api/motion/state` | Override simulated motion `{ "state": "REST" }` |

Interactive docs: `http://localhost:8000/docs`

---

## Safety / Disclaimer

This system is a **research and educational prototype**.

- It is **not** a certified medical device.
- It does **not** provide clinical diagnosis.
- Results are labeled "Possible AFib", "Model probability", "Screening result".
- The ML model runs on the ESP32 — no inference is fabricated by the backend.
- Simulated motion is always explicitly labeled.

---

## Hardware

| Component | Status |
|---|---|
| ESP32 | ✅ Working |
| MAX30100 PPG sensor | ✅ Working |
| MPU6050 accelerometer | ⚠ Unavailable (damaged/overheating) — simulated |
| MicroSD | ✗ Not used |
