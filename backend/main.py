"""
main.py — FastAPI entry point for the AFib TinyML Monitor backend.

Run with:
    python main.py

Or (if you prefer uvicorn directly):
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

HTTP REST endpoints:
  GET  /api/status            → system status
  GET  /api/ports             → list available COM ports
  POST /api/connect           → connect to a serial port
  POST /api/disconnect        → disconnect
  POST /api/motion/state      → override simulated motion state
  GET  /health                → simple health check

WebSocket endpoint:
  WS   /ws                    → stream of JSON events to frontend

CORS is configured for the Vite dev server (http://localhost:5173).
Adjust ALLOWED_ORIGINS for production.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from serial_manager import SerialManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("afib.main")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(application: FastAPI):
    # --- startup ---
    global _serial_manager
    loop = asyncio.get_running_loop()
    _serial_manager = SerialManager(ws_manager.broadcast, loop)
    logger.info("AFib TinyML Monitor backend started")
    logger.info("WebSocket endpoint: ws://localhost:8000/ws")

    default_port = os.environ.get("DEFAULT_PORT", "")
    if default_port:
        baud = int(os.environ.get("DEFAULT_BAUD", "115200"))
        logger.info("AUTO-CONNECT: %s @ %d baud", default_port, baud)
        await _serial_manager.connect(default_port, baud)

    yield  # application runs here

    # --- shutdown ---
    if _serial_manager and _serial_manager.connected:
        await _serial_manager.disconnect()


app = FastAPI(
    title="AFib TinyML Monitor API",
    description="Backend for the ESP32-based PPG AFib screening research prototype.",
    version="1.0.0",
    lifespan=lifespan,
)

# Explicit origin list — both localhost and 127.0.0.1 for Vite dev + preview.
# Do NOT use allow_origins=["*"] here: keep origins enumerated.
ALLOWED_ORIGINS = [
    "http://localhost:5173",   # Vite dev server
    "http://localhost:4173",   # Vite preview
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
]

# Explicit header / method lists prevent the Starlette CORS middleware from
# returning 400 on OPTIONS preflight requests.
#
# Root cause of OPTIONS 400: allow_credentials=True + allow_headers=["*"]
# causes Starlette to omit the Access-Control-Allow-Headers response header
# on preflight, which some browser/Starlette version combinations interpret
# as a failed preflight → 400.
#
# Fix:
#   1. allow_credentials=False  — no auth is used in this local prototype.
#   2. Enumerate exact allow_methods (include OPTIONS explicitly).
#   3. Enumerate exact allow_headers covering all headers the React frontend sends.
#   4. max_age=86400 — cache preflight for 24 h to reduce repeated OPTIONS calls.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
    allow_headers=[
        "Content-Type",
        "Accept",
        "Authorization",
        "X-Requested-With",
        "Origin",
        "Access-Control-Request-Method",
        "Access-Control-Request-Headers",
    ],
    max_age=86400,
)

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Maintains the set of active WebSocket connections."""

    def __init__(self) -> None:
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)
        logger.info("WebSocket client connected — total: %d", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.remove(ws)
        logger.info("WebSocket client disconnected — total: %d", len(self._clients))

    async def broadcast(self, payload: dict) -> None:
        """Broadcast a dict as JSON to all connected clients."""
        if not self._clients:
            return
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for client in self._clients:
            try:
                await client.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(client)
        for ws in dead:
            self._clients.remove(ws)

ws_manager = ConnectionManager()

# ---------------------------------------------------------------------------
# Serial manager (created after the event loop is available)
# ---------------------------------------------------------------------------

_serial_manager: Optional[SerialManager] = None


def get_serial() -> SerialManager:
    if _serial_manager is None:
        raise RuntimeError("Serial manager not initialised")
    return _serial_manager


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws_manager.connect(ws)
    # Send immediate status snapshot to the newly connected client
    serial = get_serial()
    await ws.send_text(json.dumps({
        "type": "system_status",
        "timestamp": time.time(),
        "esp32_connected": serial.connected,
        "serial_port": serial.port,
        "session_start": serial.state.session_start,
        "simulated_motion": serial.state.simulated_motion,
        "motion_state": serial.state.motion_state,
    }))
    try:
        while True:
            # Keep the connection alive; we only broadcast from the server
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/status")
async def api_status() -> dict:
    serial = get_serial()
    return {
        "esp32_connected": serial.connected,
        "serial_port": serial.port,
        "session_start": serial.state.session_start,
        "simulated_motion": serial.state.simulated_motion,
        "motion_state": serial.state.motion_state,
        "window_id": serial.state.window_id,
        "valid_ibi_count": serial.state.valid_ibi_count,
        "rejected_ibi_count": serial.state.rejected_ibi_count,
    }


@app.get("/api/ports")
async def api_ports() -> dict:
    ports = SerialManager.list_ports()
    return {"ports": ports}


class ConnectRequest(BaseModel):
    port: str
    baud: int = 115200


@app.post("/api/connect")
async def api_connect(req: ConnectRequest) -> dict:
    serial = get_serial()
    await serial.connect(req.port, req.baud)
    return {"connected": serial.connected, "port": serial.port}


@app.post("/api/disconnect")
async def api_disconnect() -> dict:
    serial = get_serial()
    await serial.disconnect()
    return {"connected": serial.connected}


class MotionStateRequest(BaseModel):
    state: str  # REST | LOW_MOTION | HIGH_MOTION


@app.post("/api/motion/state")
async def api_motion_state(req: MotionStateRequest) -> dict:
    """Manually set the simulated motion state."""
    serial = get_serial()
    try:
        serial.set_simulated_motion_state(req.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"motion_state": serial.state.motion_state}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
