from __future__ import annotations

import queue
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .audio import start_mic_capture
from .config import recordings_dir
from .ffmpeg_util import find_ffmpeg


def _soft_encoder() -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-crf",
        "28",
        "-g",
        "60",
        "-bf",
        "0",
        "-threads",
        "0",
    ]


def encoder_args() -> list[str]:
    """Prefer known-good Jetson HW encoders; otherwise cheap software x264."""
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
                return ["-c:v", "h264_nvenc", "-preset", "ll", "-b:v", "8M", "-bf", "0"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _soft_encoder()


def stack_stereo(left: np.ndarray, right: np.ndarray, layout: str = "sbs") -> np.ndarray:
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
    if layout == "tb":
        return np.vstack((left, right))
    return np.hstack((left, right))


class StereoRecorder:
    """
    Writes unique video frames + optional mic audio (USB camera mic via ALSA/Pulse/dshow).
    On stop, remuxes so file FPS matches real capture rate.
    """

    def __init__(
        self,
        fps: int,
        layout: str = "sbs",
        segment_minutes: int = 5,
        scale: float = 1.0,
        audio: bool = True,
        audio_device: str = "auto",
    ):
        self.fps = max(int(fps), 1)
        self.layout = layout
        self.segment_minutes = max(int(segment_minutes), 0)
        self.scale = float(scale) if scale else 1.0
        if self.scale <= 0:
            self.scale = 1.0
        self.audio_enabled = bool(audio)
        self.audio_device = audio_device or "auto"
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._active_path: Optional[Path] = None
        self._started_at = 0.0
        self._frames = 0
        self._bytes = 0
        self._ffmpeg = find_ffmpeg()
        self._fallback_writer: Optional[cv2.VideoWriter] = None
        self.error: Optional[str] = None
        self._q: queue.Queue = queue.Queue(maxsize=120)
        self._worker: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        self._recording = False
        self.dropped = 0
        self._stereo_w = 0
        self._stereo_h = 0
        self._eye_w = 0
        self._eye_h = 0
        self._enc = encoder_args()
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail = ""
        self._audio_label: Optional[str] = None
        self._audio_active = False
        self._mic = None
        self._wav_path: Optional[Path] = None
        # Inline ffmpeg audio disabled — always sidecar WAV + mux.
        self._ffmpeg_inline_audio = False

    @property
    def recording(self) -> bool:
        return self._recording

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
        eye_w = max(2, int(round(width * self.scale)) & ~1)
        eye_h = max(2, int(round(height * self.scale)) & ~1)
        stereo_w = eye_w * 2 if self.layout != "tb" else eye_w
        stereo_h = eye_h * 2 if self.layout == "tb" else eye_h
        self._stereo_w = stereo_w
        self._stereo_h = stereo_h
        self._eye_w = eye_w
        self._eye_h = eye_h
        self._enc = encoder_args()
        self._stderr_tail = ""
        self._audio_label = None
        self._audio_active = False
        self._ffmpeg_inline_audio = False
        self._wav_path = None
        self._ffmpeg = find_ffmpeg()
        path = out_dir / f"{stamp}_stereo.mp4"

        # Video-only ffmpeg (mic is captured separately and muxed on stop).
        if self._ffmpeg:
            self._proc = self._spawn_ffmpeg(path, stereo_w, stereo_h, self._enc, None)
            if self._proc is None or self._proc.poll() is not None:
                self._cleanup_proc()
                self._enc = _soft_encoder()
                self._proc = self._spawn_ffmpeg(path, stereo_w, stereo_h, self._enc, None)

        if self._proc is None or self._proc.poll() is not None:
            self._cleanup_proc()
            path = out_dir / f"{stamp}_stereo.avi"
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            self._fallback_writer = cv2.VideoWriter(str(path), fourcc, float(self.fps), (stereo_w, stereo_h))
            if not self._fallback_writer.isOpened():
                self._fallback_writer = None
                raise RuntimeError(
                    "Could not start recorder. Install ffmpeg (or: pip install imageio-ffmpeg)."
                )
            self._audio_active = False
            self._audio_label = None

        if self.audio_enabled:
            wav_path = path.with_suffix(".wav")
            mic = start_mic_capture(self.audio_device, wav_path)
            if mic is not None:
                self._mic = mic
                self._wav_path = wav_path
                self._audio_active = True
                self._audio_label = mic.label
            else:
                self._audio_active = False
                self._audio_label = "mic unavailable — check: arecord -l"

        self._active_path = path
        self._started_at = time.time()
        self._frames = 0
        self._bytes = 0
        self.dropped = 0
        self.error = None
        self._recording = True
        self._worker_stop.clear()
        self._drain_queue()
        self._worker = threading.Thread(target=self._worker_loop, name="stereo-recorder", daemon=True)
        self._worker.start()
        if self._audio_active:
            print(f"[record] audio: {self._audio_label}", flush=True)
        elif self.audio_enabled:
            print("[record] recording without audio (mic not available)", flush=True)
        return path

    def _spawn_ffmpeg(
        self,
        path: Path,
        stereo_w: int,
        stereo_h: int,
        enc: list[str],
        audio_args: Optional[tuple[list[str], str]],
    ) -> Optional[subprocess.Popen]:
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
            "-thread_queue_size",
            "64",
            "-i",
            "-",
        ]
        have_audio = False
        if audio_args:
            args, label = audio_args
            cmd.extend(args)
            self._audio_label = label
            have_audio = True

        cmd.extend(enc)
        cmd.extend(["-pix_fmt", "yuv420p"])
        # Always write video-only here; mic is muxed from WAV on stop.
        cmd.append("-an")
        self._audio_active = False
        self._ffmpeg_inline_audio = False
        if not have_audio:
            self._audio_label = None

        cmd.extend(
            [
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(path),
            ]
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            self.error = str(exc)
            self._audio_active = False
            return None
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name="ffmpeg-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        time.sleep(0.35)
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            if not proc.stderr:
                return
            chunks: list[str] = []
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if text:
                    chunks.append(text)
                    self._stderr_tail = " | ".join(chunks[-4:])[:400]
        except Exception:
            pass

    def maybe_rotate(self, width: int, height: int) -> Optional[Path]:
        if not self.recording or self.segment_minutes <= 0:
            return None
        if time.time() - self._started_at < self.segment_minutes * 60:
            return None
        return self.start(width, height)

    def write(self, left: np.ndarray, right: np.ndarray, timestamp: Optional[float] = None) -> None:
        if not self.recording:
            return
        item = (left.copy(), right.copy())
        try:
            self._q.put(item, timeout=0.08)
            return
        except queue.Full:
            pass
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
                left, right = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            self._write_one(left, right)
        while True:
            try:
                left, right = self._q.get_nowait()
            except queue.Empty:
                break
            self._write_one(left, right)

    def _write_one(self, left: np.ndarray, right: np.ndarray) -> None:
        if self._eye_w and self._eye_h and (left.shape[1] != self._eye_w or left.shape[0] != self._eye_h):
            left = cv2.resize(left, (self._eye_w, self._eye_h), interpolation=cv2.INTER_AREA)
            right = cv2.resize(right, (self._eye_w, self._eye_h), interpolation=cv2.INTER_AREA)
        stereo = stack_stereo(left, right, self.layout)
        if not stereo.flags["C_CONTIGUOUS"]:
            stereo = np.ascontiguousarray(stereo)
        payload = stereo.tobytes()
        with self._lock:
            if self._proc and self._proc.stdin and self._proc.poll() is None:
                try:
                    self._proc.stdin.write(payload)
                    self._proc.stdin.flush()
                    self._frames += 1
                    self._bytes += len(payload)
                except (BrokenPipeError, OSError):
                    self.error = self._stderr_tail or "ffmpeg pipe closed"
                    self._cleanup_proc()
            elif self._fallback_writer is not None:
                self._fallback_writer.write(stereo)
                self._frames += 1
                self._bytes += stereo.nbytes
            elif self._recording:
                self.error = self._stderr_tail or "recorder stopped unexpectedly"

    def stop(self) -> Optional[Path]:
        path = self._active_path
        started = self._started_at
        had_inline_audio = self._ffmpeg_inline_audio
        self._worker_stop.set()
        self._recording = False
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=12)
        self._worker = None
        wav_path = None
        if self._mic is not None:
            wav_path = self._mic.stop()
            self._mic = None
        with self._lock:
            self._cleanup_proc()
            if self._fallback_writer is not None:
                self._fallback_writer.release()
                self._fallback_writer = None
        frames = self._frames
        self._active_path = None
        self._audio_active = False
        self._ffmpeg_inline_audio = False
        self._wav_path = None
        final_path = path
        if path and path.exists() and frames > 1 and started > 0:
            elapsed = max(time.time() - started, 0.001)
            actual_fps = frames / elapsed
            if wav_path and wav_path.exists() and wav_path.stat().st_size > 44:
                muxed = self._mux_wav(path, wav_path, actual_fps)
                if muxed:
                    try:
                        wav_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    if path.suffix.lower() != ".mp4":
                        final_path = path.with_suffix(".mp4")
                else:
                    print(f"[record] kept sidecar audio: {wav_path}", flush=True)
            else:
                self._finalize(path, actual_fps, keep_audio=had_inline_audio)
                if wav_path and wav_path.exists():
                    try:
                        wav_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        elif wav_path and wav_path.exists():
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
        return final_path

    def _mux_wav(self, video_path: Path, wav_path: Path, actual_fps: float) -> bool:
        """Attach WAV mic track onto the video file (MP4 preferred)."""
        if not self._ffmpeg:
            return False
        fps = max(1.0, min(float(self.fps) * 1.15, actual_fps))
        out_mp4 = video_path if video_path.suffix.lower() == ".mp4" else video_path.with_suffix(".mp4")
        tmp = out_mp4.with_name(out_mp4.stem + "_mux" + out_mp4.suffix)
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-r",
            f"{fps:.4f}",
            "-i",
            str(video_path),
            "-i",
            str(wav_path),
            "-c:v",
            "copy" if video_path.suffix.lower() == ".mp4" else "libx264",
        ]
        if video_path.suffix.lower() != ".mp4":
            cmd.extend(["-preset", "ultrafast", "-crf", "23"])
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "1",
                "-ar",
                "44100",
                "-shortest",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                if out_mp4 != video_path and video_path.exists():
                    try:
                        video_path.unlink()
                    except OSError:
                        pass
                tmp.replace(out_mp4)
                return True
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            if proc.stderr:
                print(f"[record] mux failed: {proc.stderr.strip()[:240]}", flush=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[record] mux error: {exc}", flush=True)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return False

    def _finalize(self, path: Path, actual_fps: float, keep_audio: bool = False) -> None:
        """Defrag + retag timing. Keep AAC track when present."""
        if not self._ffmpeg or not path.exists() or path.suffix.lower() != ".mp4":
            return
        if path.stat().st_size < 1024:
            return
        fps = max(1.0, min(float(self.fps) * 1.15, actual_fps))
        tmp = path.with_name(path.stem + "_final" + path.suffix)
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-r",
            f"{fps:.4f}",
            "-i",
            str(path),
            "-c",
            "copy",
        ]
        if not keep_audio:
            cmd.append("-an")
        cmd.extend(["-movflags", "+faststart", str(tmp)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(path)
            elif tmp.exists():
                tmp.unlink(missing_ok=True)
        except (OSError, subprocess.TimeoutExpired):
            if tmp.exists():
                tmp.unlink(missing_ok=True)

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
                try:
                    self._proc.stdin.flush()
                except Exception:
                    pass
                self._proc.stdin.close()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def snapshot(self) -> dict:
        elapsed = round(time.time() - self._started_at, 1) if self.recording else 0
        write_fps = round(self._frames / max(elapsed, 0.001), 1) if self.recording and elapsed else 0
        file_size = 0
        if self._active_path and self._active_path.exists():
            try:
                file_size = self._active_path.stat().st_size
            except OSError:
                file_size = 0
        return {
            "recording": self.recording,
            "file": self.current_file,
            "frames": self._frames,
            "bytes_in": self._bytes,
            "file_size": file_size,
            "dropped": self.dropped,
            "queue": self._q.qsize(),
            "write_fps": write_fps,
            "target_fps": self.fps,
            "scale": self.scale,
            "encoder": self._enc[1] if len(self._enc) > 1 else None,
            "audio": self._audio_active if self.recording else self.audio_enabled,
            "audio_device": self._audio_label or self.audio_device,
            "ffmpeg": bool(self._ffmpeg),
            "error": self.error,
            "elapsed_sec": elapsed,
        }
