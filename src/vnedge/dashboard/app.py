"""Read-only dashboard server (docs/DESIGN.md §6).

Hard invariants, enforced structurally:
- No token, no dashboard: `create_app` refuses an empty token.
- Zero control actions: the only routes are the static page, GET /state,
  and the snapshot WebSocket. There is nothing to POST to.
- Cannot slow the bot: the server only reads whatever snapshot the bot last
  published; a dead or slow browser drops its own socket and nothing else.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


class SnapshotProvider:
    """Holds the latest coalesced snapshot. The bot publishes; the UI reads.
    That is the entire coupling between them."""

    def __init__(self) -> None:
        self._latest: dict | None = None

    def publish(self, snapshot: dict) -> None:
        self._latest = snapshot

    def latest(self) -> dict | None:
        return self._latest


def create_app(
    provider: SnapshotProvider,
    token: str,
    snapshot_hz: float = 1.0,
    history_path: Path | None = None,
    research_path: Path | None = None,
) -> FastAPI:
    if not token or not token.strip():
        raise ValueError("DASHBOARD_TOKEN must be non-empty — no token, no dashboard")

    app = FastAPI(title="VNEDGE dashboard", docs_url=None, redoc_url=None)

    def _authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        candidate = header.removeprefix("Bearer ").strip()
        if not candidate:
            candidate = request.query_params.get("token", "")
        return hmac.compare_digest(candidate, token)

    @app.get("/")
    async def index() -> FileResponse:
        # The shell page contains no data; all data endpoints require the token.
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/state")
    async def state(request: Request) -> JSONResponse:
        if not _authorized(request):
            raise HTTPException(status_code=401, detail="missing or invalid token")
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse({"status": "no snapshot yet"}, status_code=503)
        return JSONResponse(snapshot)

    @app.get("/history")
    async def history(request: Request) -> JSONResponse:
        """Persisted equity curve (survives restarts and page reloads)."""
        if not _authorized(request):
            raise HTTPException(status_code=401, detail="missing or invalid token")
        points: list[dict] = []
        if history_path is not None and history_path.exists():
            import json

            lines = history_path.read_text().strip().splitlines()[-2000:]
            for line in lines:
                try:
                    points.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return JSONResponse(points)

    @app.get("/research")
    async def research(request: Request) -> JSONResponse:
        """Latest rolling walk-forward verdicts from the research loop."""
        if not _authorized(request):
            raise HTTPException(status_code=401, detail="missing or invalid token")
        if research_path is None or not research_path.exists():
            return JSONResponse({"results": []})
        import json

        try:
            return JSONResponse(json.loads(research_path.read_text()))
        except json.JSONDecodeError:
            return JSONResponse({"results": []})  # mid-write race: serve empty

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        candidate = websocket.query_params.get("token", "")
        if not hmac.compare_digest(candidate, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                snapshot = provider.latest()
                if snapshot is not None:
                    await websocket.send_json(snapshot)
                await asyncio.sleep(1.0 / snapshot_hz)
        except (WebSocketDisconnect, ConnectionError):
            return  # dropped client: deregistered by scope exit, bot unaffected
        except Exception as exc:  # noqa: BLE001 — UI must never propagate upward
            logger.warning("dashboard websocket dropped: %s", exc)
            return

    return app
