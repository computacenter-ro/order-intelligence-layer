"""[5] Core Backend — WebSocket feed (CLAUDE.md [5] "WS /ws").

A tiny in-process pub/sub hub plus the ``/ws`` endpoint the dashboard connects
to. The hub is transport-agnostic: it just holds the set of connected clients
and fan-outs events to them. Nothing here talks to RabbitMQ or the DB — the
consumers will call :func:`ConnectionManager.broadcast` later; this module only
provides the hub and the endpoint. Importing it performs no I/O.

Every pushed message uses one consistent envelope::

    {"type": <str>, "data": <dict>}

with ``type`` one of ``alert.new`` / ``journey.updated`` / ``journey.completed``
(CLAUDE.md: ``alert.new | journey.updated | journey.completed``).
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from backend.auth import COOKIE_NAME, decode_token

# --- event envelope ----------------------------------------------------------

EVENT_ALERT_NEW = "alert.new"
EVENT_JOURNEY_UPDATED = "journey.updated"
EVENT_JOURNEY_COMPLETED = "journey.completed"


def make_event(type_: str, data: dict) -> dict:
    """Build the consistent WS envelope ``{"type": ..., "data": ...}``."""
    return {"type": type_, "data": data}


# --- connection hub ----------------------------------------------------------


class ConnectionManager:
    """Holds the set of live WebSocket clients and fans events out to them.

    Broadcast is tolerant to dead clients: a client whose ``send_json`` raises
    (already gone) is dropped from the set rather than aborting the fan-out.
    """

    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept the handshake and register the client."""
        await websocket.accept()
        self._active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Deregister a client (idempotent — unknown clients are ignored)."""
        self._active.discard(websocket)

    async def broadcast(self, event: dict) -> None:
        """Send ``event`` to every connected client, evicting dead ones."""
        dead: list[WebSocket] = []
        for websocket in list(self._active):
            try:
                await websocket.send_json(event)
            except Exception:  # noqa: BLE001 — a dropped client must not stop the fan-out
                dead.append(websocket)
        for websocket in dead:
            self._active.discard(websocket)

    def __len__(self) -> int:
        return len(self._active)


# --- endpoint ----------------------------------------------------------------

# Module-level singleton hub: the app mounts this router and (later) the
# consumers broadcast through this same instance.
manager = ConnectionManager()

router = APIRouter()


def _ws_session_token(websocket: WebSocket) -> str | None:
    """Extract the session token from the handshake.

    Browsers can't set custom headers on a WS upgrade, so the httpOnly session
    cookie (sent automatically) is the primary source; ``?token=`` is a fallback
    for non-browser clients. Returns None when neither is present.
    """
    cookie = websocket.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    return websocket.query_params.get("token")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Authenticate, register the client, then keep the socket open until it
    disconnects.

    The WS carries the same alert/journey data as the REST API, so it enforces
    the same session: a missing/invalid token closes the handshake with policy
    code 1008 before the client is registered. The server only pushes (via
    :meth:`ConnectionManager.broadcast`); inbound frames are read solely to
    detect the disconnect, then discarded.
    """
    token = _ws_session_token(websocket)
    if token is None or decode_token(token) is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
