"""
serial_manager.py — Manages the USB serial connection to the ESP32.

Responsibilities:
  - Open / close the serial port.
  - Read lines in a background thread.
  - Route parsed events to the WebSocket broadcaster.
  - Maintain session state (window progress, quality, motion simulation).
  - Forward real ESP32 model results (AF probability, decision, quality) verbatim.
  - Provide simulated motion state that can be swapped for real MPU6050 data
    without modifying the frontend.

IBI deduplication:
  The ESP32 sends RAW_IBI,<ms> followed immediately by IBI_DATA_SEC,<float>.
  Both refer to the same beat. RAW_IBI is used as the primary event; a
  subsequent IBI_DATA_SEC within 0.5 s of a RAW_IBI for the same beat is
  silently skipped to prevent duplicate IBI entries in the dashboard.

Window lifecycle:
  beats / IBIs  →  motion events  →  WINDOW_RESULT (marker)
  →  quality / motion / model KV pairs  →  WINDOW_END
  →  emit WindowResultMessage  →  reset session state

Simulated motion:
  By default, motion state cycles through REST → LOW_MOTION → HIGH_MOTION
  on a configurable schedule AND is overridden whenever the ESP32 sends
  SIMULATED_MOTION=<STATE>.  When an MPU6050 becomes available and the
  firmware sends real motion lines, set state.simulated_motion=False and
  handle the new line types in parser.py.
"""

from __future__ import annotations
import asyncio
import time
import logging
import threading
from typing import Optional, Callable, Awaitable, Sequence

import serial
import serial.tools.list_ports

from parser import (
    parse_line,
    ParsedBeat,
    ParsedIBI,
    ParsedIBIDataSec,
    ParsedRejectedIBI,
    ParsedSimulatedMotion,
    ParsedWindowKV,
    ParsedWindowResultMarker,
    ParsedFeatureValue,
    ParsedFeatureVectorReady,
    ParsedWindowEnd,
    ParsedUnknownLine,
)
from models import (
    SessionState,
    BeatMessage, IBIMessage, RejectedIBIMessage, HeartRateMessage,
    MotionMessage, QualityMessage, WindowProgressMessage,
    WindowResultMessage, WindowFeatures, SystemStatusMessage,
    LogEventMessage, ErrorMessage,
)

logger = logging.getLogger("afib.serial")

# Type alias for the broadcast callback provided by main.py
BroadcastFn = Callable[[dict], Awaitable[None]]

# Seconds within which a IBI_DATA_SEC is considered a duplicate of a RAW_IBI
_IBI_DEDUP_WINDOW = 0.5

# Map ESP32 DECISION strings → frontend Prediction enum values
_DECISION_MAP: dict[str, str] = {
    "NORMAL":        "NORMAL",
    "POSSIBLE_AFIB": "POSSIBLE_AFIB",
    "INCONCLUSIVE":  "INCONCLUSIVE",
}


class SerialManager:
    """
    Encapsulates the serial reader thread and event-to-WebSocket routing.

    Usage:
        mgr = SerialManager(broadcast_fn, loop)
        await mgr.connect("COM16", 115200)
        ...
        await mgr.disconnect()

    The broadcast_fn is an async callable that sends a dict to all connected
    WebSocket clients.  It must be safe to call from a non-async thread via
    asyncio.run_coroutine_threadsafe().
    """

    # Motion simulation cycle (seconds per state) — used as background filler
    # between real SIMULATED_MOTION updates from the ESP32.
    MOTION_CYCLE: Sequence[tuple[str, float]] = [
        ("REST",        20.0),
        ("LOW_MOTION",  10.0),
        ("HIGH_MOTION",  5.0),
        ("LOW_MOTION",  10.0),
    ]
    WINDOW_SECONDS: float = 30.0

    def __init__(self, broadcast_fn: BroadcastFn, loop: asyncio.AbstractEventLoop) -> None:
        self._broadcast = broadcast_fn
        self._loop = loop
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.state = SessionState()

        # Motion simulation
        self._motion_thread: Optional[threading.Thread] = None
        self._motion_stop = threading.Event()

        # Window progress ticker
        self._window_ticker_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def port(self) -> Optional[str]:
        return self._serial.port if self._serial else None

    async def connect(self, port: str, baud: int = 115200) -> None:
        if self.connected:
            await self.disconnect()

        logger.info("Opening serial port %s @ %d baud", port, baud)
        try:
            self._serial = serial.Serial(port, baud, timeout=1)
        except serial.SerialException as exc:
            logger.error("Failed to open %s: %s", port, exc)
            await self._emit(ErrorMessage(
                timestamp=time.time(),
                message=f"Cannot open {port}: {exc}",
            ).model_dump())
            return

        self.state = SessionState()
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="serial-reader"
        )
        self._thread.start()

        # Window progress ticker (async, runs in event loop)
        self._window_ticker_task = asyncio.ensure_future(
            self._window_ticker(), loop=self._loop
        )

        await self._emit_status()
        await self._log("INFO", f"Connected to {port} @ {baud} baud")

    async def disconnect(self) -> None:
        logger.info("Disconnecting serial port")
        self._stop_event.set()
        self._motion_stop.set()

        if self._window_ticker_task:
            self._window_ticker_task.cancel()
            self._window_ticker_task = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

        if self._serial and self._serial.is_open:
            self._serial.close()
        self._serial = None

        await self._emit_status()
        await self._log("INFO", "Serial port disconnected")

    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    # ------------------------------------------------------------------
    # Internal: serial reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Runs in a daemon thread; reads lines from the serial port."""
        logger.debug("Serial reader thread started")
        while not self._stop_event.is_set():
            try:
                if not self._serial or not self._serial.is_open:
                    break
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                self._dispatch(line)
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                self._fire(self._emit(ErrorMessage(
                    timestamp=time.time(),
                    message=f"Serial error: {exc}",
                ).model_dump()))
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unexpected error in reader loop: %s", exc)

        logger.debug("Serial reader thread exiting")

    def _dispatch(self, raw_line: str) -> None:
        """Parse one serial line and fan out to the appropriate handler."""
        event = parse_line(raw_line)

        if event is None:
            return  # blank or silently-ignored line

        if isinstance(event, ParsedBeat):
            self._handle_beat(event)
        elif isinstance(event, ParsedIBI):
            self._handle_ibi(event)
        elif isinstance(event, ParsedIBIDataSec):
            self._handle_ibi_data_sec(event)
        elif isinstance(event, ParsedRejectedIBI):
            self._handle_rejected(event)
        elif isinstance(event, ParsedSimulatedMotion):
            self._handle_simulated_motion(event)
        elif isinstance(event, ParsedWindowKV):
            self._handle_window_kv(event)
        elif isinstance(event, ParsedWindowResultMarker):
            # WINDOW_RESULT marker line — data collection continues until WINDOW_END
            logger.debug("WINDOW_RESULT marker received")
        elif isinstance(event, ParsedFeatureValue):
            # Legacy lowercase key=value from old firmware
            self._handle_feature_value(event)
        elif isinstance(event, ParsedFeatureVectorReady):
            # Legacy FEATURE_VECTOR_READY from old firmware
            self._fire(self._log("WINDOW", "Feature vector ready — waiting for WINDOW_END"))
        elif isinstance(event, ParsedWindowEnd):
            self._handle_window_end()
        elif isinstance(event, ParsedUnknownLine):
            logger.info("Unknown serial line: %r", event.raw)
        else:
            logger.debug("Unhandled parser event type: %r", type(event))

    # ------------------------------------------------------------------
    # Event handlers — called from reader thread via _fire()
    # ------------------------------------------------------------------

    def _handle_beat(self, event: ParsedBeat) -> None:
        ts = time.time()
        ibi_ms = None if event.is_first else self.state.last_ibi_ms

        self._fire(self._emit(BeatMessage(
            timestamp=ts,
            esp32_ts=event.esp32_ts,
            ibi_ms=ibi_ms,
        ).model_dump()))

        label = "FIRST_BEAT" if event.is_first else f"BEAT  IBI={ibi_ms} ms"
        self._fire(self._log("BEAT", label))

    def _handle_ibi(self, event: ParsedIBI) -> None:
        """Handle RAW_IBI or IBI_DATA (comma format) — primary IBI source."""
        ts = time.time()

        # Mark timestamp so a subsequent IBI_DATA_SEC for the same beat is skipped
        if event.source == "RAW_IBI":
            self.state.last_raw_ibi_ts = ts

        self.state.last_ibi_ms = event.ibi_ms
        self.state.valid_ibi_count += 1

        bpm = 60000.0 / event.ibi_ms
        self.state.last_bpm = bpm

        self._fire(self._emit(IBIMessage(
            timestamp=ts, ibi_ms=event.ibi_ms, valid=True,
        ).model_dump()))

        self._fire(self._emit(HeartRateMessage(
            timestamp=ts, bpm=round(bpm, 1),
        ).model_dump()))

        self._update_quality()

    def _handle_ibi_data_sec(self, event: ParsedIBIDataSec) -> None:
        """
        Handle IBI_DATA_SEC,<float_seconds>.

        Skipped if a RAW_IBI was received within _IBI_DEDUP_WINDOW seconds
        (they refer to the same beat — RAW_IBI takes precedence).
        """
        since_raw = time.time() - self.state.last_raw_ibi_ts
        if since_raw < _IBI_DEDUP_WINDOW:
            logger.debug(
                "IBI_DATA_SEC=%d ms skipped — duplicate of RAW_IBI (%.3f s ago)",
                event.ibi_ms, since_raw,
            )
            return

        # No recent RAW_IBI — treat as primary IBI source
        ts = time.time()
        self.state.last_ibi_ms = event.ibi_ms
        self.state.valid_ibi_count += 1
        bpm = 60000.0 / event.ibi_ms
        self.state.last_bpm = bpm

        self._fire(self._emit(IBIMessage(
            timestamp=ts, ibi_ms=event.ibi_ms, valid=True,
        ).model_dump()))

        self._fire(self._emit(HeartRateMessage(
            timestamp=ts, bpm=round(bpm, 1),
        ).model_dump()))

        self._update_quality()

    def _handle_rejected(self, event: ParsedRejectedIBI) -> None:
        ts = time.time()
        self.state.rejected_ibi_count += 1

        self._fire(self._emit(RejectedIBIMessage(
            timestamp=ts,
            ibi_ms=event.ibi_ms,
            reason=event.reason,  # type: ignore[arg-type]
        ).model_dump()))

        self._fire(self._log("REJECTED",
            f"IBI={event.ibi_ms} ms  reason={event.reason}"))

        self._update_quality()

    def _handle_simulated_motion(self, event: ParsedSimulatedMotion) -> None:
        """
        Handle SIMULATED_MOTION=<STATE> from the ESP32 firmware.

        Updates the current motion state immediately and broadcasts it so the
        dashboard reflects what the firmware is simulating right now.
        This does NOT imply the MPU6050 is connected.
        """
        ts = time.time()
        self.state.motion_state = event.state  # type: ignore[assignment]
        # simulated_motion stays True — source is always 'simulated'
        self._fire(self._emit(MotionMessage(
            timestamp=ts,
            state=event.state,  # type: ignore[arg-type]
            source="simulated",
        ).model_dump()))
        logger.debug("Motion state from ESP32 firmware: %s (simulated)", event.state)

    def _handle_window_kv(self, event: ParsedWindowKV) -> None:
        """
        Store a window-level key-value pair into the appropriate accumulator dict.
        These are emitted as a batch in _handle_window_end().
        """
        key = event.key
        val = event.value

        # Route to the correct accumulator based on key name
        if key in ("valid_ibis", "rejected_short", "rejected_long",
                   "total_ibi_attempts", "rejection_rate_pct", "ppg_quality"):
            self.state.window_quality_data[key] = val

        elif key in ("rest_percent", "low_motion_percent", "high_motion_percent",
                     "dominant_motion", "motion"):
            self.state.window_motion_data[key] = val

        elif key in ("af_probability", "af_percent", "decision",
                     "reason", "model_used"):
            self.state.window_model_data[key] = val

        else:
            logger.debug("Unrouted window KV: %s=%r", key, val)

    def _handle_feature_value(self, event: ParsedFeatureValue) -> None:
        """Legacy lowercase key=value from old firmware."""
        self.state.pending_features[event.key] = event.value

    def _handle_window_end(self) -> None:
        """
        Finalise the current 30-second window and emit a WindowResultMessage.

        All data originates exclusively from the ESP32 serial stream.
        The backend never fabricates AF probability, quality, or decision values.
        """
        ts = time.time()
        qd = self.state.window_quality_data
        md = self.state.window_motion_data
        model = self.state.window_model_data

        # ---------------------------------------------------------------
        # 1. Resolve PPG quality
        #    Priority: ESP32 PPG_QUALITY field (real firmware decision)
        #    Fallback: live quality tracked by the backend
        # ---------------------------------------------------------------
        esp32_quality = qd.get("ppg_quality", None)
        quality: str = esp32_quality if esp32_quality else self.state.quality

        # ---------------------------------------------------------------
        # 2. Resolve motion context for this window
        #    Priority: DOMINANT_MOTION field from the ESP32 motion summary
        #    Fallback: MOTION field from the WINDOW_RESULT block
        #    Final fallback: current live motion state
        # ---------------------------------------------------------------
        dominant_motion: str = (
            md.get("dominant_motion")
            or md.get("motion")
            or self.state.motion_state
        )

        # ---------------------------------------------------------------
        # 3. Resolve model result
        #    These values come ONLY from the ESP32 TinyML model output.
        #    The backend never runs inference or generates probabilities.
        # ---------------------------------------------------------------
        model_used: Optional[bool] = model.get("model_used", None)
        esp32_decision: str = model.get("decision", "WAITING")
        prediction: str = _DECISION_MAP.get(esp32_decision, "WAITING")

        # If MODEL_USED=NO, do not forward an AF probability
        if model_used is False:
            af_probability: Optional[float] = None
        else:
            af_probability = model.get("af_probability", None)

        reason: Optional[str] = model.get("reason", None)

        # ---------------------------------------------------------------
        # 4. Resolve quality counts
        #    Use ESP32 quality summary if available, else live counters.
        # ---------------------------------------------------------------
        valid_ibis: Optional[int] = qd.get("valid_ibis", None)
        rejected_short: Optional[int] = qd.get("rejected_short", None)
        rejected_long: Optional[int] = qd.get("rejected_long", None)
        total_ibi_attempts: Optional[int] = qd.get("total_ibi_attempts", None)
        rejection_rate_pct: Optional[float] = qd.get("rejection_rate_pct", None)

        # ---------------------------------------------------------------
        # 5. Motion percentages
        # ---------------------------------------------------------------
        rest_pct: Optional[float] = md.get("rest_percent", None)
        low_pct: Optional[float] = md.get("low_motion_percent", None)
        high_pct: Optional[float] = md.get("high_motion_percent", None)

        # ---------------------------------------------------------------
        # 6. Legacy feature vector (old firmware format)
        # ---------------------------------------------------------------
        feats = self.state.pending_features.copy()
        window_features: Optional[WindowFeatures] = None
        legacy_required = {
            "mean_ibi", "median_ibi", "std_ibi", "min_ibi", "max_ibi",
            "iqr_ibi", "cv_ibi", "mean_abs_diff", "std_diff", "rmssd",
            "beat_count", "suspicious_ibi_fraction", "missing_fraction",
        }
        if legacy_required.issubset(feats.keys()):
            try:
                window_features = WindowFeatures(**feats)  # type: ignore[arg-type]
            except Exception as exc:
                logger.warning("Could not build legacy WindowFeatures: %s", exc)

        # ---------------------------------------------------------------
        # 7. Build and broadcast the window result
        # ---------------------------------------------------------------
        result = WindowResultMessage(
            timestamp=ts,
            window_id=self.state.window_id,
            features=window_features,
            af_probability=af_probability,
            prediction=prediction,  # type: ignore[arg-type]
            quality=quality,        # type: ignore[arg-type]
            motion_context=dominant_motion,  # type: ignore[arg-type]
            motion_source="simulated" if self.state.simulated_motion else "mpu6050",
            # Extended ESP32 fields
            reason=reason,
            model_used=model_used,
            rest_percent=rest_pct,
            low_motion_percent=low_pct,
            high_motion_percent=high_pct,
            valid_ibis=valid_ibis,
            rejected_short=rejected_short,
            rejected_long=rejected_long,
            total_ibi_attempts=total_ibi_attempts,
            rejection_rate_pct=rejection_rate_pct,
        )
        self._fire(self._emit(result.model_dump()))

        # ---------------------------------------------------------------
        # 8. Also emit a QualityMessage with ESP32's quality summary
        #    so the SignalQualityPanel reflects the window's actual values.
        # ---------------------------------------------------------------
        live_valid = valid_ibis if valid_ibis is not None else self.state.valid_ibi_count
        live_rejected = (
            (rejected_short or 0) + (rejected_long or 0)
            if (rejected_short is not None or rejected_long is not None)
            else self.state.rejected_ibi_count
        )
        total = live_valid + live_rejected
        rate = live_rejected / total if total > 0 else 0.0

        self._fire(self._emit(QualityMessage(
            timestamp=ts,
            quality=quality,  # type: ignore[arg-type]
            valid_ibi_count=live_valid,
            rejected_ibi_count=live_rejected,
            rejection_rate=round(rate, 4),
        ).model_dump()))

        # ---------------------------------------------------------------
        # 9. Log the window completion
        # ---------------------------------------------------------------
        prob_str = f"P(AF)={af_probability * 100:.2f}%" if af_probability is not None else "P(AF)=N/A"
        self._fire(self._log("WINDOW",
            f"Window {self.state.window_id} complete  "
            f"quality={quality}  {prob_str}  decision={prediction}"
            f"  reason={reason or '—'}  model_used={model_used}"))

        if prediction not in ("WAITING",):
            self._fire(self._log("MODEL",
                f"{prob_str}  →  {prediction}  [{reason or '—'}]"))

        if prediction == "INCONCLUSIVE":
            self._fire(self._log("DECISION",
                f"INCONCLUSIVE — reason: {reason or 'unknown'}"))
        elif prediction in ("NORMAL", "POSSIBLE_AFIB"):
            self._fire(self._log("DECISION", prediction))

        # ---------------------------------------------------------------
        # 10. Reset for the next window
        # ---------------------------------------------------------------
        self.state.reset_window()

    # ------------------------------------------------------------------
    # Quality helper (live — updated beat-by-beat for real-time display)
    # ------------------------------------------------------------------

    def _update_quality(self) -> None:
        """
        Compute live signal quality from IBI counts so far in this window.
        This gives real-time feedback between window results.
        At window end, the ESP32's own PPG_QUALITY field overrides this.
        """
        rate = self.state.rejection_rate()
        if self.state.valid_ibi_count < 5:
            q = "UNKNOWN"
        elif rate > 0.30:
            q = "POOR"
        else:
            q = "GOOD"

        self.state.quality = q  # type: ignore[assignment]

        self._fire(self._emit(QualityMessage(
            timestamp=time.time(),
            quality=q,  # type: ignore[arg-type]
            valid_ibi_count=self.state.valid_ibi_count,
            rejected_ibi_count=self.state.rejected_ibi_count,
            rejection_rate=round(rate, 4),
        ).model_dump()))

    # ------------------------------------------------------------------
    # Motion state
    # ------------------------------------------------------------------

    def set_simulated_motion_state(self, state: str) -> None:
        """Manually override simulated motion state from the HTTP API."""
        valid = ("REST", "LOW_MOTION", "HIGH_MOTION")
        if state not in valid:
            raise ValueError(f"Invalid motion state: {state}")
        self.state.motion_state = state  # type: ignore[assignment]
        self._fire(self._emit(MotionMessage(
            timestamp=time.time(),
            state=state,  # type: ignore[arg-type]
            source="simulated",
        ).model_dump()))

    # ------------------------------------------------------------------
    # Window progress ticker (async coroutine)
    # ------------------------------------------------------------------

    async def _window_ticker(self) -> None:
        """Sends window progress updates every second."""
        while True:
            await asyncio.sleep(1.0)
            if not self.connected:
                continue
            elapsed = time.time() - self.state.window_start
            elapsed = min(elapsed, self.WINDOW_SECONDS)
            await self._emit(WindowProgressMessage(
                timestamp=time.time(),
                elapsed_seconds=round(elapsed, 1),
                window_id=self.state.window_id,
            ).model_dump())

    # ------------------------------------------------------------------
    # Emit helpers
    # ------------------------------------------------------------------

    async def _emit(self, payload: dict) -> None:
        await self._broadcast(payload)

    async def _emit_status(self) -> None:
        await self._emit(SystemStatusMessage(
            timestamp=time.time(),
            esp32_connected=self.connected,
            serial_port=self.port,
            session_start=self.state.session_start,
            simulated_motion=self.state.simulated_motion,
            motion_state=self.state.motion_state,
        ).model_dump())

    async def _log(self, level: str, message: str) -> None:
        await self._emit(LogEventMessage(
            timestamp=time.time(),
            level=level,  # type: ignore[arg-type]
            message=message,
        ).model_dump())

    def _fire(self, coro) -> None:
        """Schedule a coroutine from the reader thread."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)
