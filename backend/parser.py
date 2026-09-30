"""
parser.py — Modular ESP32 serial line parser.

Design goals:
  - Non-fatal: unknown / malformed lines are logged, never crash the app.
  - Modular: add new line types by adding a handler to LINE_HANDLERS.
  - Returns typed events consumed by serial_manager.py.

ESP32 output format (current real firmware):
  BEAT,<esp32_millis>
  FIRST_BEAT
  RAW_IBI,<int_ms>
  IBI_DATA_SEC,<float_seconds>      ← new: IBI in seconds, converted to ms
  SIMULATED_MOTION=<STATE>          ← new: motion from simulated source
  WINDOW_RESULT                     ← new: marker for final window block
  WINDOW_END

  Window quality fields (UPPERCASE=value):
    VALID_IBIS=<int>
    REJECTED_SHORT=<int>
    REJECTED_LONG=<int>
    TOTAL_IBI_ATTEMPTS=<int>
    REJECTION_RATE=<float>%
    PPG_QUALITY=GOOD|POOR|UNKNOWN

  Window motion fields:
    REST_PERCENT=<float>
    LOW_MOTION_PERCENT=<float>
    HIGH_MOTION_PERCENT=<float>
    DOMINANT_MOTION=<STATE>
    MOTION=<STATE>

  Window model result fields:
    AF_PROBABILITY=<float>
    AF_PERCENT=<float>
    DECISION=NORMAL|INCONCLUSIVE|POSSIBLE_AFIB
    REASON=<string>
    MODEL_USED=YES|NO

  Legacy feature vector fields (old firmware, kept for compatibility):
    mean_ibi=<float>, median_ibi=<float>, ... (lowercase key=value)
    FEATURE_VECTOR_READY

  Silently ignored:
    ====... lines
    ----... lines
    Section headers (PPG QUALITY SUMMARY, MOTION SUMMARY, etc.)
    Starting next window...

To add a new line type: append a new (regex, handler_fn) pair to LINE_HANDLERS.
"""

from __future__ import annotations
import re
import logging
from typing import Any
from dataclasses import dataclass

logger = logging.getLogger("afib.parser")


# ---------------------------------------------------------------------------
# Parsed event dataclasses (internal representation)
# ---------------------------------------------------------------------------

@dataclass
class ParsedBeat:
    esp32_ts: int
    is_first: bool = False

@dataclass
class ParsedIBI:
    ibi_ms: int
    source: str = "IBI_DATA"   # "IBI_DATA" | "RAW_IBI"

@dataclass
class ParsedIBIDataSec:
    """IBI received as fractional seconds from IBI_DATA_SEC — convert to ms."""
    ibi_ms: int   # already converted on parse

@dataclass
class ParsedRejectedIBI:
    ibi_ms: int
    reason: str  # "SHORT" | "LONG" | "ARTIFACT"

@dataclass
class ParsedSimulatedMotion:
    """SIMULATED_MOTION=<STATE> line from ESP32 firmware."""
    state: str  # REST | LOW_MOTION | HIGH_MOTION

@dataclass
class ParsedWindowKV:
    """
    A key-value pair that belongs to the current 30-second window summary.
    Used for quality, motion percentage, and model result fields.
    The key is a normalised lowercase string; value is already converted.
    """
    key: str
    value: Any

@dataclass
class ParsedWindowResultMarker:
    """The bare 'WINDOW_RESULT' line that marks the start of the final summary block."""
    pass

@dataclass
class ParsedFeatureValue:
    """Legacy lowercase key=value feature pair from old ESP32 firmware."""
    key: str
    value: float | int

@dataclass
class ParsedFeatureVectorReady:
    """Legacy FEATURE_VECTOR_READY marker from old firmware."""
    pass

@dataclass
class ParsedWindowEnd:
    """WINDOW_END — signals end of the 30-second window."""
    pass

@dataclass
class ParsedUnknownLine:
    """Any line that doesn't match a known pattern — logged, never fatal."""
    raw: str


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def _parse_beat(m: re.Match) -> ParsedBeat:
    return ParsedBeat(esp32_ts=int(m.group(1)))

def _parse_first_beat(_m: re.Match) -> ParsedBeat:
    return ParsedBeat(esp32_ts=0, is_first=True)

def _parse_ibi_data(m: re.Match) -> ParsedIBI:
    return ParsedIBI(ibi_ms=int(m.group(1)), source="IBI_DATA")

def _parse_raw_ibi(m: re.Match) -> ParsedIBI:
    return ParsedIBI(ibi_ms=int(m.group(1)), source="RAW_IBI")

def _parse_ibi_data_sec(m: re.Match) -> ParsedIBIDataSec:
    """Convert IBI_DATA_SEC,<float_seconds> → integer milliseconds."""
    ibi_sec = float(m.group(1))
    ibi_ms = round(ibi_sec * 1000)
    return ParsedIBIDataSec(ibi_ms=ibi_ms)

def _parse_rejected_short(m: re.Match) -> ParsedRejectedIBI:
    return ParsedRejectedIBI(ibi_ms=int(m.group(1)), reason="SHORT")

def _parse_rejected_long(m: re.Match) -> ParsedRejectedIBI:
    return ParsedRejectedIBI(ibi_ms=int(m.group(1)), reason="LONG")

def _parse_simulated_motion(m: re.Match) -> ParsedSimulatedMotion:
    return ParsedSimulatedMotion(state=m.group(1))

def _parse_feature_vector_ready(_m: re.Match) -> ParsedFeatureVectorReady:
    return ParsedFeatureVectorReady()

def _parse_window_end(_m: re.Match) -> ParsedWindowEnd:
    return ParsedWindowEnd()

def _parse_window_result_marker(_m: re.Match) -> ParsedWindowResultMarker:
    return ParsedWindowResultMarker()

def _parse_kv_legacy(m: re.Match) -> ParsedFeatureValue:
    """Legacy lowercase key=value feature pairs (old firmware)."""
    key = m.group(1)
    raw_val = m.group(2)
    if key == "beat_count":
        return ParsedFeatureValue(key=key, value=int(raw_val))
    return ParsedFeatureValue(key=key, value=float(raw_val))


# ---------------------------------------------------------------------------
# LINE_HANDLERS — ordered list of (compiled_regex, handler_fn)
# More specific patterns must come before broader catch-alls.
# ---------------------------------------------------------------------------

LINE_HANDLERS: list[tuple[re.Pattern, Any]] = [

    # ---- Beat events ----
    (re.compile(r"^BEAT,(\d+)$"),               _parse_beat),
    (re.compile(r"^FIRST_BEAT$"),               _parse_first_beat),

    # ---- IBI events ----
    (re.compile(r"^RAW_IBI,(\d+)$"),            _parse_raw_ibi),
    (re.compile(r"^IBI_DATA,(\d+)$"),           _parse_ibi_data),
    (re.compile(r"^IBI_DATA_SEC,([\d.]+)$"),    _parse_ibi_data_sec),

    # ---- Rejected IBI (comma format from old firmware) ----
    (re.compile(r"^REJECTED_SHORT,(\d+)$"),     _parse_rejected_short),
    (re.compile(r"^REJECTED_LONG,(\d+)$"),      _parse_rejected_long),

    # ---- Live simulated motion ----
    (re.compile(r"^SIMULATED_MOTION=(REST|LOW_MOTION|HIGH_MOTION)$"),
     _parse_simulated_motion),

    # ---- Window markers ----
    (re.compile(r"^WINDOW_RESULT$"),            _parse_window_result_marker),
    (re.compile(r"^FEATURE_VECTOR_READY$"),     _parse_feature_vector_ready),
    (re.compile(r"^WINDOW_END$"),               _parse_window_end),

    # ---- Window quality fields (UPPERCASE=value) ----
    (re.compile(r"^VALID_IBIS=(\d+)$"),
     lambda m: ParsedWindowKV("valid_ibis", int(m.group(1)))),

    (re.compile(r"^REJECTED_SHORT=(\d+)$"),
     lambda m: ParsedWindowKV("rejected_short", int(m.group(1)))),

    (re.compile(r"^REJECTED_LONG=(\d+)$"),
     lambda m: ParsedWindowKV("rejected_long", int(m.group(1)))),

    (re.compile(r"^TOTAL_IBI_ATTEMPTS=(\d+)$"),
     lambda m: ParsedWindowKV("total_ibi_attempts", int(m.group(1)))),

    (re.compile(r"^REJECTION_RATE=([\d.]+)%$"),
     lambda m: ParsedWindowKV("rejection_rate_pct", float(m.group(1)))),

    (re.compile(r"^PPG_QUALITY=(GOOD|POOR|UNKNOWN)$"),
     lambda m: ParsedWindowKV("ppg_quality", m.group(1))),

    # ---- Window motion fields ----
    (re.compile(r"^REST_PERCENT=([\d.]+)$"),
     lambda m: ParsedWindowKV("rest_percent", float(m.group(1)))),

    (re.compile(r"^LOW_MOTION_PERCENT=([\d.]+)$"),
     lambda m: ParsedWindowKV("low_motion_percent", float(m.group(1)))),

    (re.compile(r"^HIGH_MOTION_PERCENT=([\d.]+)$"),
     lambda m: ParsedWindowKV("high_motion_percent", float(m.group(1)))),

    (re.compile(r"^DOMINANT_MOTION=(REST|LOW_MOTION|HIGH_MOTION)$"),
     lambda m: ParsedWindowKV("dominant_motion", m.group(1))),

    # MOTION= inside WINDOW_RESULT block
    (re.compile(r"^MOTION=(REST|LOW_MOTION|HIGH_MOTION)$"),
     lambda m: ParsedWindowKV("motion", m.group(1))),

    # ---- Window model result fields ----
    (re.compile(r"^AF_PROBABILITY=([\d.eE+\-]+)$"),
     lambda m: ParsedWindowKV("af_probability", float(m.group(1)))),

    (re.compile(r"^AF_PERCENT=([\d.]+)$"),
     lambda m: ParsedWindowKV("af_percent", float(m.group(1)))),

    (re.compile(r"^DECISION=(NORMAL|INCONCLUSIVE|POSSIBLE_AFIB)$"),
     lambda m: ParsedWindowKV("decision", m.group(1))),

    (re.compile(r"^REASON=(\w+)$"),
     lambda m: ParsedWindowKV("reason", m.group(1))),

    (re.compile(r"^MODEL_USED=(YES|NO)$"),
     lambda m: ParsedWindowKV("model_used", m.group(1) == "YES")),

    # ---- Legacy lowercase key=value feature pairs (old firmware) ----
    (re.compile(
        r"^(mean_ibi|median_ibi|std_ibi|min_ibi|max_ibi|iqr_ibi|cv_ibi|"
        r"mean_abs_diff|std_diff|rmssd|beat_count|suspicious_ibi_fraction|missing_fraction)"
        r"=([\d.eE+\-]+)$"
    ), _parse_kv_legacy),

    # ---- Silently ignored lines ----
    # Separator lines (====... or ----...)
    (re.compile(r"^=+$"),                       lambda _m: None),
    (re.compile(r"^-+$"),                       lambda _m: None),

    # Section header lines
    (re.compile(r"^30 SECOND WINDOW RESULT$"),  lambda _m: None),
    (re.compile(r"^PPG QUALITY SUMMARY$"),      lambda _m: None),
    (re.compile(r"^MOTION SUMMARY$"),           lambda _m: None),
    (re.compile(r"^MODEL_RESULT$"),             lambda _m: None),
    (re.compile(r"^Starting next window\.\.\.$"), lambda _m: None),

    # Legacy header from old firmware
    (re.compile(r"^[\d\-A-Z ]+FEATURE VECTOR[\d\-A-Z ]*$"), lambda _m: None),
]


def parse_line(raw_line: str) -> Any:
    """
    Parse a single line from the ESP32 serial stream.

    Returns one of:
      ParsedBeat | ParsedIBI | ParsedIBIDataSec | ParsedRejectedIBI
      | ParsedSimulatedMotion | ParsedWindowKV | ParsedWindowResultMarker
      | ParsedFeatureValue | ParsedFeatureVectorReady | ParsedWindowEnd
      | ParsedUnknownLine | None

    None   → line intentionally ignored (separator / section header).
    ParsedUnknownLine → unrecognised line; caller should log, never crash.
    """
    line = raw_line.strip()
    if not line:
        return None  # blank line — ignore silently

    for pattern, handler in LINE_HANDLERS:
        m = pattern.match(line)
        if m:
            try:
                return handler(m)
            except (ValueError, IndexError) as exc:
                logger.warning("Parser handler failed for line %r: %s", line, exc)
                return ParsedUnknownLine(raw=line)

    return ParsedUnknownLine(raw=line)
