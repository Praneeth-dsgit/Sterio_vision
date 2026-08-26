from __future__ import annotations

import struct
import threading
import time
from typing import Optional

import cv2
import numpy as np

from .cameras import CameraWorker, Frame, list_cameras, resolve_camera_indices
from .config import load_config, save_local_config
from .recorder import StereoRecorder, stack_stereo
from .sync import pair_frames

MAGIC = b"SV01"


def encode_jpeg(image: np.ndarray, quality: int, max_width: int) -> bytes:
    frame = image
    h, w = frame.shape[:2]
    if max_width and w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def pack_pair(left_jpeg: bytes, right_jpeg: bytes, left: Frame, right: Frame, skew_ms: float) -> bytes:
    header = struct.pack(
        "<4sddII",
        MAGIC,
        left.timestamp,
        right.timestamp,
        len(left_jpeg),
        len(right_jpeg),
    )
    # extra: skew_ms as float64
    header += struct.pack("<d", skew_ms)
    return header + left_jpeg + right_jpeg


class StereoEngine:
    def __init__(self):
        self.cfg = load_config()
        self.left: Optional[CameraWorker] = None
        self.right: Optional[CameraWorker] = None
        self.recorder = StereoRecorder(
            fps=int(self.cfg["cameras"]["fps"]),
            layout=self.cfg["record"]["stereo_layout"],
            segment_minutes=int(self.cfg["record"]["segment_minutes"]),
            scale=float(self.cfg["record"].get("scale", 1.0)),
            audio=bool(self.cfg["record"].get("audio", True)),
            audio_device=str(self.cfg["record"].get("audio_device", "auto")),
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._preview_lock = threading.Lock()
        self._preview: Optional[bytes] = None
        self._left_jpeg: Optional[bytes] = None
        self._right_jpeg: Optional[bytes] = None
        self._mjpeg_stereo: Optional[bytes] = None
        self.stats = {
            "preview_fps": 0.0,
            "capture_fps_left": 0.0,
            "capture_fps_right": 0.0,
            "skew_ms": 0.0,
            "dropped_skew": 0,
            "clients": 0,
            "synthetic": False,
        }
        self._last_pair_ids = (-1, -1)
        self._fps_count = 0
        self._fps_t = time.time()
        self._available_cameras: list[dict] = []
        self._last_rebind = 0.0

    def start(self) -> None:
        self.stop()
        self.cfg = load_config()
        cam = self.cfg["cameras"]
        left_idx, right_idx = resolve_camera_indices(cam["left_index"], cam["right_index"])
        self.cfg["cameras"]["left_index"] = left_idx
        self.cfg["cameras"]["right_index"] = right_idx
        self._available_cameras = list_cameras()
        self.left = CameraWorker(
            "left",
            left_idx,
            int(cam["width"]),
            int(cam["height"]),
            int(cam["fps"]),
            cam["fourcc"],
            cam.get("left_flip", "none"),
            "LEFT",
            0,
        )
        self.right = CameraWorker(
            "right",
            right_idx,
            int(cam["width"]),
            int(cam["height"]),
            int(cam["fps"]),
            cam["fourcc"],
            cam.get("right_flip", "none"),
            "RIGHT",
            125,
        )
        self.left.start()
        self.right.start()
        self.recorder = StereoRecorder(
            fps=int(cam["fps"]),
            layout=self.cfg["record"]["stereo_layout"],
            segment_minutes=int(self.cfg["record"]["segment_minutes"]),
            scale=float(self.cfg["record"].get("scale", 1.0)),
            audio=bool(self.cfg["record"].get("audio", True)),
            audio_device=str(self.cfg["record"].get("audio_device", "auto")),
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stereo-engine", daemon=True)
        self._thread.start()
        if self.cfg["record"]["auto_start"]:
            # Delay slightly so the first real frames exist
            threading.Timer(0.8, self.start_recording).start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self.left:
            self.left.stop()
            self.left.join(timeout=2)
        if self.right:
            self.right.stop()
            self.right.join(timeout=2)
        self.recorder.stop()
        self.left = None
        self.right = None

    def start_recording(self) -> dict:
        if self._stop.is_set() or self.left is None:
            return {"ok": False, "error": "engine stopped"}
        cam = self.cfg["cameras"]
        path = self.recorder.start(int(cam["width"]), int(cam["height"]))
        return {"ok": True, "file": str(path)}

    def stop_recording(self) -> dict:
        path = self.recorder.stop()
        return {"ok": True, "file": str(path) if path else None}

    def restart_cameras(self) -> dict:
        """Full camera reopen — use after USB unplug/replug if auto-reconnect stalls."""
        was_recording = self.recorder.recording
        self.start()
        if was_recording or self.cfg["record"]["auto_start"]:
            threading.Timer(0.8, self.start_recording).start()
        return {"ok": True, "cameras": self.status()["cameras"]}

    def _rebind_if_needed(self) -> None:
        """When cameras are missing, re-discover V4L indices (USB remaps on Jetson)."""
        if self.left is None or self.right is None:
            return
        left_missing = bool(self.left.synthetic or not self.left.opened)
        right_missing = bool(self.right.synthetic or not self.right.opened)
        if not (left_missing or right_missing):
            return
        now = time.time()
        if now - self._last_rebind < 3.0:
            return
        self._last_rebind = now
        # Only re-probe device nodes when both are released — probing steals open devices.
        if left_missing and right_missing:
            cam = self.cfg["cameras"]
            left_idx, right_idx = resolve_camera_indices(cam["left_index"], cam["right_index"])
            self.cfg["cameras"]["left_index"] = left_idx
            self.cfg["cameras"]["right_index"] = right_idx
            try:
                self._available_cameras = list_cameras()
            except Exception:
                pass
            print(f"[engine] rebind cameras → {left_idx}/{right_idx}", flush=True)
            self.left.set_index(left_idx)
            self.right.set_index(right_idx)
        else:
            if left_missing:
                self.left.request_reopen()
            if right_missing:
                self.right.request_reopen()

    def apply_settings(self, patch: dict) -> dict:
        cfg = load_config()
        restart = False
        for section in ("cameras", "stream", "record", "server"):
            if section in patch and isinstance(patch[section], dict):
                before = dict(cfg[section])
                cfg[section].update(patch[section])
                if section in ("cameras", "stream") and cfg[section] != before:
                    restart = True
        save_local_config(cfg)
        self.cfg = cfg
        self.recorder.segment_minutes = int(cfg["record"]["segment_minutes"])
        self.recorder.layout = cfg["record"]["stereo_layout"]
        self.recorder.scale = float(cfg["record"].get("scale", 1.0))
        self.recorder.audio_enabled = bool(cfg["record"].get("audio", True))
        self.recorder.audio_device = str(cfg["record"].get("audio_device", "auto"))
        if restart:
            self.start()
        elif cfg["record"]["auto_start"] and not self.recorder.recording:
            self.start_recording()
        return cfg

    def preview_packet(self) -> Optional[bytes]:
        with self._preview_lock:
            return self._preview

    def mjpeg_left(self) -> Optional[bytes]:
        with self._preview_lock:
            return self._left_jpeg

    def mjpeg_right(self) -> Optional[bytes]:
        with self._preview_lock:
            return self._right_jpeg

    def mjpeg_stereo(self) -> Optional[bytes]:
        with self._preview_lock:
            return self._mjpeg_stereo

    def status(self) -> dict:
        left_open = bool(self.left and self.left.opened)
        right_open = bool(self.right and self.right.opened)
        synthetic = bool((self.left and self.left.synthetic) or (self.right and self.right.synthetic))
        return {
            "cameras": {
                "left": {
                    "index": self.cfg["cameras"]["left_index"],
                    "opened": left_open,
                    "frames": self.left.frames if self.left else 0,
                    "errors": self.left.errors if self.left else 0,
                    "synthetic": bool(self.left and self.left.synthetic),
                },
                "right": {
                    "index": self.cfg["cameras"]["right_index"],
                    "opened": right_open,
                    "frames": self.right.frames if self.right else 0,
                    "errors": self.right.errors if self.right else 0,
                    "synthetic": bool(self.right and self.right.synthetic),
                },
                "available": self._available_cameras,
            },
            "stream": self.stats,
            "record": self.recorder.snapshot(),
            "config": self.cfg,
            "synthetic": synthetic,
        }

    def _loop(self) -> None:
        quality = int(self.cfg["stream"]["jpeg_quality"])
        max_width = int(self.cfg["stream"]["max_width"])
        max_skew = float(self.cfg["stream"]["max_skew_ms"])
        cam = self.cfg["cameras"]
        width, height = int(cam["width"]), int(cam["height"])
        period = 1.0 / max(int(cam["fps"]), 1)
        while not self._stop.is_set():
            t0 = time.time()
            self._rebind_if_needed()
            left = self.left.latest() if self.left else None
            right = self.right.latest() if self.right else None
            paired = pair_frames(left, right, max_skew)
            if paired is None:
                if left is not None and right is not None:
                    self.stats["dropped_skew"] += 1
                time.sleep(0.002)
                continue
            ids = (paired.left.index, paired.right.index)
            if ids == self._last_pair_ids:
                time.sleep(0.001)
                continue
            self._last_pair_ids = ids
            self.stats["skew_ms"] = round(paired.skew_ms, 2)
            self.stats["synthetic"] = paired.left.synthetic or paired.right.synthetic

            recording = self.recorder.recording
            if recording:
                self.recorder.maybe_rotate(width, height)
                self.recorder.write(paired.left.image, paired.right.image)

            # While recording, skip/lighten JPEG so H.264 encode does not drop frames.
            clients = int(self.stats.get("clients") or 0)
            if recording:
                do_preview = clients > 0 and (self._fps_count % 4 == 0)
                preview_quality = 40
                preview_width = min(max_width, 640) if max_width else 640
            else:
                do_preview = True
                preview_quality = quality
                preview_width = max_width
            if do_preview:
                left_jpeg = encode_jpeg(paired.left.image, preview_quality, preview_width)
                right_jpeg = encode_jpeg(paired.right.image, preview_quality, preview_width)
                packet = pack_pair(left_jpeg, right_jpeg, paired.left, paired.right, paired.skew_ms)
                with self._preview_lock:
                    self._preview = packet
                    self._left_jpeg = left_jpeg
                    self._right_jpeg = right_jpeg

            if (not recording) and self._fps_count % 2 == 0:
                stereo = stack_stereo(
                    paired.left.image, paired.right.image, self.cfg["record"]["stereo_layout"]
                )
                stereo_jpeg = encode_jpeg(stereo, quality, max_width * 2 if max_width else 0)
                with self._preview_lock:
                    self._mjpeg_stereo = stereo_jpeg

            self._fps_count += 1
            now = time.time()
            if now - self._fps_t >= 1.0:
                self.stats["preview_fps"] = round(self._fps_count / (now - self._fps_t), 1)
                self.stats["capture_fps_left"] = float(self.left.frames if self.left else 0)
                self.stats["capture_fps_right"] = float(self.right.frames if self.right else 0)
                if self.left:
                    self.left.frames = 0
                if self.right:
                    self.right.frames = 0
                self._fps_count = 0
                self._fps_t = now

            # Don't pace-sleep while recording — push frames to disk as they arrive.
            if not recording:
                leftover = period - (time.time() - t0)
                if leftover > 0:
                    time.sleep(leftover)
