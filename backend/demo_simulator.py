"""Deterministic backend-driven demo stream for the AFib dashboard."""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Awaitable, Callable, Literal, Optional

from models import (
    BeatMessage,
    HeartRateMessage,
    IBIMessage,
    LogEventMessage,
    MotionMessage,
    QualityMessage,
    SessionState,
    WindowProgressMessage,
    WindowResultMessage,
)

BroadcastFn = Callable[[dict], Awaitable[None]]
DemoScenario = Literal["clean", "poor", "high_motion"]


class DemoSimulator:
    """Produces plausible sensor events without claiming hardware or inference."""

    WINDOW_SECONDS = 30.0
    BEAT_INTERVAL_SECONDS = 0.8

    def __init__(self, broadcast: BroadcastFn) -> None:
        self._broadcast = broadcast
        self._task: Optional[asyncio.Task[None]] = None
        self._scenario: DemoScenario = "clean"
        self._rng = random.Random()
        self._bpm_noise = 0.0
        self.state = SessionState()

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def scenario(self) -> DemoScenario:
        return self._scenario

    async def start(self, scenario: DemoScenario = "clean") -> None:
        if self.active:
            return
        self._scenario = scenario
        self._rng = random.Random()
        self._bpm_noise = 0.0
        self.state = SessionState()
        self._task = asyncio.create_task(self._run(), name="simulated-demo")
        await self._emit_log("INFO", f"SIMULATED_DEMO started ({scenario})")
        await self._emit_status()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._emit_status()

    async def _run(self) -> None:
        started = time.monotonic()
        next_beat = started
        last_motion: Optional[str] = None
        last_window_id = 0
        try:
            while True:
                now = time.monotonic()
                elapsed = now - started
                window_elapsed = elapsed % self.WINDOW_SECONDS
                window_id = int(elapsed // self.WINDOW_SECONDS)
                if window_id != last_window_id:
                    if elapsed > 0.2:
                        await self._emit_window_result(last_window_id, last_motion or motion)
                    last_window_id = window_id
                    self.state.window_id = window_id
                    self.state.window_start = time.time() - window_elapsed

                motion = self._motion_for(elapsed)
                if motion != last_motion:
                    self.state.motion_state = motion  # type: ignore[assignment]
                    await self._broadcast(MotionMessage(
                        timestamp=time.time(), state=motion, source="simulated"
                    ).model_dump())
                    last_motion = motion

                await self._broadcast(WindowProgressMessage(
                    timestamp=time.time(),
                    elapsed_seconds=round(window_elapsed, 1),
                    window_id=window_id,
                ).model_dump())

                if now >= next_beat:
                    await self._emit_beat(elapsed, window_elapsed)
                    next_beat = now + self.BEAT_INTERVAL_SECONDS

                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise

    async def _emit_beat(self, elapsed: float, window_elapsed: float) -> None:
        self._bpm_noise = (
            self._bpm_noise * 0.82 + self._rng.uniform(-0.7, 0.7)
        )
        bpm = 78.0 + 6.0 * math.sin(elapsed / 9.0) + 1.5 * math.sin(
            elapsed / 2.7
        ) + self._bpm_noise
        bpm = max(68.0, min(88.0, bpm))
        ibi_ms = max(650, round(60000.0 / bpm))
        if self._scenario == "poor" and int(window_elapsed) % 4 == 0:
            self.state.rejected_ibi_count += 1
            self.state.quality = "POOR"
        else:
            self.state.valid_ibi_count += 1
            self.state.last_ibi_ms = ibi_ms
            self.state.last_bpm = 60000.0 / ibi_ms
            self.state.quality = "GOOD"
            await self._broadcast(IBIMessage(
                timestamp=time.time(), ibi_ms=ibi_ms, valid=True
            ).model_dump())
            await self._broadcast(HeartRateMessage(
                timestamp=time.time(), bpm=round(60000.0 / ibi_ms, 1)
            ).model_dump())

        total = self.state.valid_ibi_count + self.state.rejected_ibi_count
        await self._broadcast(QualityMessage(
            timestamp=time.time(),
            quality=self.state.quality,  # type: ignore[arg-type]
            valid_ibi_count=self.state.valid_ibi_count,
            rejected_ibi_count=self.state.rejected_ibi_count,
            rejection_rate=self.state.rejection_rate(),
        ).model_dump())
        await self._broadcast(BeatMessage(
            timestamp=time.time(),
            esp32_ts=round(time.monotonic() * 1000),
            ibi_ms=ibi_ms,
        ).model_dump())

    async def _emit_window_result(self, window_id: int, motion: str) -> None:
        if self._scenario == "poor":
            quality, prediction, reason, probability, model_used = (
                "POOR", "INCONCLUSIVE", "POOR_PPG_QUALITY", None, False
            )
        elif self._scenario == "high_motion":
            quality, prediction, reason, probability, model_used = (
                "GOOD", "INCONCLUSIVE", "HIGH_MOTION", None, False
            )
            motion = "HIGH_MOTION"
        else:
            quality, prediction, reason, probability, model_used = (
                "GOOD", "NORMAL", "SIGNAL_USABLE", 0.12, True
            )

        total = self.state.valid_ibi_count + self.state.rejected_ibi_count
        rejection_rate = (
            self.state.rejected_ibi_count / total * 100 if total else 0.0
        )
        result = WindowResultMessage(
            timestamp=time.time(),
            window_id=window_id,
            af_probability=probability,
            prediction=prediction,  # type: ignore[arg-type]
            quality=quality,  # type: ignore[arg-type]
            motion_context=motion,  # type: ignore[arg-type]
            motion_source="simulated",
            inference_source="SIMULATED_DEMO",
            reason=reason,
            model_used=model_used,
            rest_percent=100.0 if motion == "REST" else 0.0,
            low_motion_percent=100.0 if motion == "LOW_MOTION" else 0.0,
            high_motion_percent=100.0 if motion == "HIGH_MOTION" else 0.0,
            valid_ibis=self.state.valid_ibi_count,
            rejected_short=0,
            rejected_long=self.state.rejected_ibi_count,
            total_ibi_attempts=total,
            rejection_rate_pct=round(rejection_rate, 2),
        )
        await self._broadcast(result.model_dump())
        await self._emit_log(
            "DECISION", f"SIMULATED_DEMO {prediction} — reason={reason}"
        )
        self.state.reset_window()

    def _motion_for(self, elapsed: float) -> str:
        if self._scenario == "high_motion":
            return "HIGH_MOTION"
        phase = int(elapsed // 10) % 3
        return ("REST", "LOW_MOTION", "HIGH_MOTION")[phase]

    async def _emit_log(self, level: str, message: str) -> None:
        await self._broadcast(LogEventMessage(
            timestamp=time.time(), level=level, message=message  # type: ignore[arg-type]
        ).model_dump())

    async def _emit_status(self) -> None:
        await self._broadcast({
            "type": "system_status",
            "timestamp": time.time(),
            "esp32_connected": False,
            "serial_port": None,
            "session_start": self.state.session_start,
            "simulated_motion": True,
            "motion_state": self.state.motion_state,
            "mode": "SIMULATED_DEMO" if self.active else "LIVE",
        })