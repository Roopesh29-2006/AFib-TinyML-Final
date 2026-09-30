"""
models.py — Pydantic data models for all WebSocket messages and internal state.

These models define the JSON protocol between the Python backend and the React frontend.
If the ESP32 firmware evolves, update parser.py and add/extend types here.
"""

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel
import time


# ---------------------------------------------------------------------------
# WebSocket message types (sent to frontend)
# ---------------------------------------------------------------------------

class BeatMessage(BaseModel):
    """Fired each time the ESP32 reports a heartbeat."""
    type: Literal["beat"] = "beat"
    timestamp: float          # UNIX epoch, seconds
    esp32_ts: int             # raw ESP32 millis() timestamp
    ibi_ms: Optional[int] = None  # None if this is the FIRST_BEAT


class IBIMessage(BaseModel):
    """A validated IBI reading accepted by the ESP32."""
    type: Literal["ibi"] = "ibi"
    timestamp: float
    ibi_ms: int
    valid: bool = True


class RejectedIBIMessage(BaseModel):
    """An IBI reading that was rejected by the ESP32 firmware."""
    type: Literal["rejected_ibi"] = "rejected_ibi"
    timestamp: float
    ibi_ms: int
    reason: Literal["SHORT", "LONG", "ARTIFACT"]


class HeartRateMessage(BaseModel):
    """Derived heart rate from the most recent valid IBI."""
    type: Literal["heart_rate"] = "heart_rate"
    timestamp: float
    bpm: float


class MotionMessage(BaseModel):
    """Motion state — currently always 'simulated' until MPU6050 is available."""
    type: Literal["motion"] = "motion"
    timestamp: float
    state: Literal["REST", "LOW_MOTION", "HIGH_MOTION"]
    source: Literal["simulated", "mpu6050"]


class QualityMessage(BaseModel):
    """PPG signal quality snapshot."""
    type: Literal["quality"] = "quality"
    timestamp: float
    quality: Literal["GOOD", "POOR", "UNKNOWN"]
    valid_ibi_count: int
    rejected_ibi_count: int
    rejection_rate: float  # 0.0–1.0


class WindowProgressMessage(BaseModel):
    """Progress through the current 30-second analysis window."""
    type: Literal["window_progress"] = "window_progress"
    timestamp: float
    elapsed_seconds: float   # 0–30
    window_id: int


class WindowFeatures(BaseModel):
    """Feature vector extracted from a 30-second window (legacy firmware format)."""
    mean_ibi: float
    median_ibi: float
    std_ibi: float
    min_ibi: float
    max_ibi: float
    iqr_ibi: float
    cv_ibi: float
    mean_abs_diff: float
    std_diff: float
    rmssd: float
    beat_count: int
    suspicious_ibi_fraction: float
    missing_fraction: float


class WindowResultMessage(BaseModel):
    """
    Result for a completed 30-second window.

    Core fields (consumed by existing frontend TypeScript interfaces):
      - af_probability : float | None  — from ESP32 TinyML model; None if MODEL_USED=NO
      - prediction     : str           — maps from ESP32 DECISION field
      - quality        : str           — PPG quality from ESP32 (GOOD/POOR/UNKNOWN)
      - motion_context : str           — dominant motion state for this window
      - motion_source  : str           — always 'simulated' until MPU6050 is live
      - features       : WindowFeatures | None — populated only from legacy firmware

    Extended fields (forwarded from ESP32, displayed via event log / future UI):
      - reason               : WHY the ESP32 made this decision (e.g. SIGNAL_USABLE)
      - model_used           : whether the TinyML model ran for this window
      - rest_percent         : % of window classified as REST by simulated motion
      - low_motion_percent   : % LOW_MOTION
      - high_motion_percent  : % HIGH_MOTION
      - valid_ibis           : count from ESP32 quality summary
      - rejected_short       : short-rejected IBI count from ESP32
      - rejected_long        : long-rejected IBI count from ESP32
      - total_ibi_attempts   : total IBI attempts from ESP32
      - rejection_rate_pct   : rejection rate (0–100) from ESP32

    IMPORTANT: af_probability and prediction come directly from the ESP32 TinyML model.
    The backend never fabricates these values.
    """
    type: Literal["window_result"] = "window_result"
    timestamp: float
    window_id: int

    # Core fields (existing frontend interface)
    features: Optional[WindowFeatures] = None
    af_probability: Optional[float] = None   # None when MODEL_USED=NO
    prediction: Literal["NORMAL", "POSSIBLE_AFIB", "INCONCLUSIVE", "WAITING"] = "WAITING"
    quality: Literal["GOOD", "POOR", "UNKNOWN"] = "UNKNOWN"
    motion_context: Literal["REST", "LOW_MOTION", "HIGH_MOTION"] = "REST"
    motion_source: Literal["simulated", "mpu6050"] = "simulated"

    # Extended ESP32 fields (forwarded verbatim — never computed in Python)
    reason: Optional[str] = None              # e.g. SIGNAL_USABLE, POOR_PPG_QUALITY, HIGH_MOTION
    model_used: Optional[bool] = None         # True = TinyML ran; False = skipped
    rest_percent: Optional[float] = None
    low_motion_percent: Optional[float] = None
    high_motion_percent: Optional[float] = None
    valid_ibis: Optional[int] = None
    rejected_short: Optional[int] = None
    rejected_long: Optional[int] = None
    total_ibi_attempts: Optional[int] = None
    rejection_rate_pct: Optional[float] = None


class RawPPGMessage(BaseModel):
    """
    Raw PPG sample.  The ESP32 firmware does not currently send raw samples;
    this type is here so the frontend knows what to expect when it does.
    """
    type: Literal["raw_ppg"] = "raw_ppg"
    timestamp: float
    sample: int
    sample_rate_hz: int = 100


class SystemStatusMessage(BaseModel):
    """Overall system / connection status."""
    type: Literal["system_status"] = "system_status"
    timestamp: float
    esp32_connected: bool
    serial_port: Optional[str]
    session_start: float        # UNIX epoch of session start
    simulated_motion: bool      # True until MPU6050 is live
    motion_state: Literal["REST", "LOW_MOTION", "HIGH_MOTION"] = "REST"


class LogEventMessage(BaseModel):
    """Human-readable log event for the event log panel."""
    type: Literal["log_event"] = "log_event"
    timestamp: float
    level: Literal["INFO", "WARN", "ERROR", "BEAT", "WINDOW", "MODEL", "REJECTED", "DECISION"]
    message: str


class ErrorMessage(BaseModel):
    """Non-fatal backend error forwarded to the frontend."""
    type: Literal["error"] = "error"
    timestamp: float
    message: str


# ---------------------------------------------------------------------------
# Internal state (not sent over WebSocket directly)
# ---------------------------------------------------------------------------

class SessionState:
    """Mutable session state maintained by the serial manager."""

    def __init__(self) -> None:
        self.session_start: float = time.time()
        self.window_id: int = 0
        self.window_start: float = time.time()

        # Live IBI counters (updated beat-by-beat)
        self.valid_ibi_count: int = 0
        self.rejected_ibi_count: int = 0
        self.last_ibi_ms: Optional[int] = None
        self.last_bpm: Optional[float] = None

        # Live quality state (updated beat-by-beat for live display)
        self.quality: Literal["GOOD", "POOR", "UNKNOWN"] = "UNKNOWN"

        # Motion state
        self.motion_state: Literal["REST", "LOW_MOTION", "HIGH_MOTION"] = "REST"
        self.simulated_motion: bool = True   # flipped when MPU6050 data arrives

        # IBI deduplication: track when the last RAW_IBI was processed.
        # If IBI_DATA_SEC arrives within this window after a RAW_IBI, it is skipped.
        self.last_raw_ibi_ts: float = 0.0

        # Window-level accumulator dicts (populated by ParsedWindowKV events)
        # Cleared at each window reset.
        self.window_quality_data: dict = {}   # VALID_IBIS, PPG_QUALITY, etc.
        self.window_motion_data: dict = {}    # REST_PERCENT, DOMINANT_MOTION, etc.
        self.window_model_data: dict = {}     # AF_PROBABILITY, DECISION, REASON, etc.

        # Legacy feature accumulator (old lowercase key=value firmware)
        self.pending_features: dict = {}

    def reset_window(self) -> None:
        self.window_id += 1
        self.window_start = time.time()
        self.valid_ibi_count = 0
        self.rejected_ibi_count = 0
        self.pending_features = {}
        self.window_quality_data = {}
        self.window_motion_data = {}
        self.window_model_data = {}

    def rejection_rate(self) -> float:
        total = self.valid_ibi_count + self.rejected_ibi_count
        return self.rejected_ibi_count / total if total > 0 else 0.0
