from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .certs import ensure_certs, public_urls
from .config import ROOT, load_config, recordings_dir
from .engine import StereoEngine
from .audio import list_audio_devices

class NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response


STATIC = ROOT / "static"
engine = StereoEngine()
app = FastAPI(title="Stereo Vision", version="1.0.0")
app.mount("/static", NoStoreStaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/api/status")
async def status() -> dict:
    cfg = load_config()
    payload = engine.status()
    payload["urls"] = public_urls(cfg["server"]["https_port"], cfg["server"]["http_port"])
    return payload


@app.post("/api/record/start")
async def record_start() -> dict:
    return engine.start_recording()


@app.post("/api/record/stop")
async def record_stop() -> dict:
    return engine.stop_recording()


@app.post("/api/cameras/restart")
async def cameras_restart() -> dict:
    return engine.restart_cameras()


@app.get("/api/audio/devices")
async def audio_devices() -> dict:
    return {"devices": list_audio_devices()}


@app.post("/api/settings")
async def settings(patch: dict = Body(...)) -> dict:
    return engine.apply_settings(patch)


@app.get("/api/recordings")
async def recordings() -> dict:
    folder = recordings_dir()
    files = []
    for path in sorted(folder.glob("*"), reverse=True):
        if path.is_file() and path.suffix.lower() in {".mp4", ".avi", ".mkv"}:
            files.append({"name": path.name, "size": path.stat().st_size, "mtime": path.stat().st_mtime})
    return {"directory": str(folder), "files": files}


@app.get("/recordings/{name}")
async def download_recording(name: str):
    path = recordings_dir() / Path(name).name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename=path.name)


@app.delete("/api/recordings/{name}")
async def delete_recording(name: str) -> dict:
    safe = Path(name).name
    if safe != name or ".." in name or "/" in name or "\\" in name:
        return JSONResponse({"ok": False, "error": "invalid name"}, status_code=400)
    path = recordings_dir() / safe
    if path.suffix.lower() not in {".mp4", ".avi", ".mkv"}:
        return JSONResponse({"ok": False, "error": "unsupported type"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    active = engine.recorder.current_file
    if active and Path(active).resolve() == path.resolve():
        return JSONResponse({"ok": False, "error": "recording in progress"}, status_code=409)
    try:
        path.unlink()
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "name": safe}


async def _mjpeg(getter):
    boundary = b"frame"
    while True:
        frame = getter()
        if frame:
            yield (
                b"--" + boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            )
        await asyncio.sleep(0.03)


@app.get("/stream/left")
async def stream_left():
    return StreamingResponse(_mjpeg(engine.mjpeg_left), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/stream/right")
async def stream_right():
    return StreamingResponse(_mjpeg(engine.mjpeg_right), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/stream/stereo")
async def stream_stereo():
    return StreamingResponse(_mjpeg(engine.mjpeg_stereo), media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    await ws.accept()
    engine.stats["clients"] = int(engine.stats.get("clients") or 0) + 1
    last = None
    try:
        while True:
            packet = engine.preview_packet()
            if packet is not None and packet is not last:
                await ws.send_bytes(packet)
                last = packet
            else:
                await asyncio.sleep(0.008)
    except (WebSocketDisconnect, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
        pass
    finally:
        engine.stats["clients"] = max(int(engine.stats.get("clients") or 1) - 1, 0)


def _ignore_windows_disconnect(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Browser refresh/close trips WinError 10054 inside the Proactor event loop."""
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return
    loop.default_exception_handler(context)


def run() -> None:
    cfg = load_config()["server"]
    host = cfg["host"]
    engine.start()
    try:
        if cfg.get("https"):
            cert, key = ensure_certs()
            https = uvicorn.Config(
                app,
                host=host,
                port=int(cfg["https_port"]),
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                log_level="info",
            )
            http = uvicorn.Config(app, host=host, port=int(cfg["http_port"]), log_level="info")

            print("Stereo Vision", flush=True)
            for item in public_urls(int(cfg["https_port"]), int(cfg["http_port"])):
                print(f"  Quest 3 (HTTPS): {item['https']}", flush=True)
                print(f"  Desktop  (HTTP): {item['http']}", flush=True)
            print("  Quest: accept the certificate warning, then tap Enter VR.", flush=True)

            async def serve_both() -> None:
                asyncio.get_running_loop().set_exception_handler(_ignore_windows_disconnect)
                https_server = uvicorn.Server(https)
                http_server = uvicorn.Server(http)
                http_server.install_signal_handlers = False
                try:
                    await asyncio.gather(https_server.serve(), http_server.serve())
                except SystemExit as exc:
                    raise RuntimeError(
                        f"Could not bind HTTP {cfg['http_port']} / HTTPS {cfg['https_port']}. "
                        "Another Stereo Vision process is probably still running."
                    ) from exc

            asyncio.run(serve_both())
        else:
            uvicorn.run(app, host=host, port=int(cfg["http_port"]))
    except OSError as exc:
        raise SystemExit(
            f"Port already in use ({exc}). Stop the other python -m server process and retry."
        ) from exc
    finally:
        engine.stop()
