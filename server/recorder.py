from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import recordings_dir


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def encoder_args() -> list[str]:
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            probe = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            listing = (probe.stdout or "") + (probe.stderr or "")
            if "h264_nvmpi" in listing:
                return ["-c:v", "h264_nvmpi", "-b:v", "8M"]
            if "h264_nvenc" in listing:
                return ["-c:v", "h264_nvenc", "-preset", "ll", "-b:v", "8M"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "23"]


def stack_stereo(left: np.ndarray, right: np.ndarray, layout: str = "sbs") -> np.ndarray:
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
    if layout == "tb":
        return np.vstack((left, right))
    return np.hstack((left, right))


class StereoRecorder:
    """Disk writer runs on a worker thread so the live preview loop is not blocked."""

    def __init__(self, fps: int, layout: str = "sbs", segment_minutes: int = 5):
        self.fps = max(int(fps), 1)
        self.layout = layout
        self.segment_minutes = max(int(segment_minutes), 0)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._active_path: Optional[Path] = None
        self._started_at = 0.0
        self._pace_origin = 0.0
        self._frames = 0
        self._bytes = 0
        self._ffmpeg = find_ffmpeg()
        self._fallback_writer: Optional[cv2.VideoWriter] = None
        self.error: Optional[str] = None
        self._q: queue.Queue = queue.Queue(maxsize=60)
        self._worker: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        self.dropped = 0

    @property
    def recording(self) -> bool:
        return self._proc is not None or self._fallback_writer is not None

    @property
    def current_file(self) -> Optional[str]:
        return str(self._active_path) if self._active_path else None

    @property
    def frames_written(self) -> int:
        return self._frames

    def start(self, width: int, height: int) -> Path:
        self.stop()
        out_dir = recordings_dir()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        stereo_w = width * 2 if self.layout != "tb" else width
        stereo_h = height * 2 if self.layout == "tb" else height
        if self._ffmpeg:
            path = out_dir / f"{stamp}_stereo.mp4"
            cmd = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-s",
                f"{stereo_w}x{stereo_h}",
                "-r",
                str(self.fps),
                "-i",
                "-",
                *encoder_args(),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(path),
            ]
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as exc:
                self.error = str(exc)
                self._proc = None
        if self._proc is None:
            path = out_dir / f"{stamp}_stereo.avi"
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            self._fallback_writer = cv2.VideoWriter(str(path), fourcc, float(self.fps), (stereo_w, stereo_h))
            if not self._fallback_writer.isOpened():
                self._fallback_writer = None
                raise RuntimeError("Could not start recorder. Install ffmpeg for MP4 output.")
        self._active_path = path
        self._started_at = time.time()
        self._pace_origin = 0.0
        self._frames = 0
        self.dropped = 0
        self.error = None
        self._worker_stop.clear()
        self._drain_queue()
        self._worker = threading.Thread(target=self._worker_loop, name="stereo-recorder", daemon=True)
        self._worker.start()
        return path

    def maybe_rotate(self, width: int, height: int) -> Optional[Path]:
        if not self.recording or self.segment_minutes <= 0:
            return None
        if time.time() - self._started_at < self.segment_minutes * 60:
            return None
        return self.start(width, height)

    def write(self, left: np.ndarray, right: np.ndarray, timestamp: Optional[float] = None) -> None:
        """Enqueue a frame copy for the writer thread."""
        if not self.recording:
            return
        now = timestamp if timestamp is not None else time.time()
        item = (left.copy(), right.copy(), now)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            # Drop oldest only when the writer is badly behind (~2s buffer).
            self.dropped += 1
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
            except queue.Full:
                self.dropped += 1

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                left, right, now = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            self._write_blocking(left, right, now)

    def _write_blocking(self, left: np.ndarray, right: np.ndarray, now: float) -> None:
        stereo = stack_stereo(left, right, self.layout)
        payload = stereo.tobytes()
        with self._lock:
            repeats = self._cfr_repeats(now)
            if repeats <= 0:
                return
            for _ in range(repeats):
                if self._proc and self._proc.stdin:
                    try:
                        self._proc.stdin.write(payload)
                        self._frames += 1
                        self._bytes += len(payload)
                    except BrokenPipeError:
                        self.error = "ffmpeg pipe closed"
                        self._cleanup_proc()
                        return
                elif self._fallback_writer is not None:
                    self._fallback_writer.write(stereo)
                    self._frames += 1
                else:
                    return

    def _cfr_repeats(self, now: float) -> int:
        if self._frames == 0 or self._pace_origin <= 0:
            self._pace_origin = now
            return 1
        elapsed = max(0.0, now - self._pace_origin)
        expected = int(elapsed * self.fps + 0.5)
        repeats = expected - self._frames
        if repeats < 1:
            return 0
        return min(repeats, self.fps * 2)

    def stop(self) -> Optional[Path]:
        path = self._active_path
        self._worker_stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)
        self._worker = None
        self._drain_queue()
        with self._lock:
            self._cleanup_proc()
            if self._fallback_writer is not None:
                self._fallback_writer.release()
                self._fallback_writer = None
        self._active_path = None
        return path

    def _drain_queue(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _cleanup_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=8)
        except Exception:
            self._proc.kill()
        self._proc = None

    def snapshot(self) -> dict:
        return {
            "recording": self.recording,
            "file": self.current_file,
            "frames": self._frames,
            "dropped": self.dropped,
            "queue": self._q.qsize(),
            "ffmpeg": bool(self._ffmpeg),
            "error": self.error,
            "elapsed_sec": round(time.time() - self._started_at, 1) if self.recording else 0,
        }
