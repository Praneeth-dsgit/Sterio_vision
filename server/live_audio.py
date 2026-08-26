from __future__ import annotations

import platform
import queue
import signal
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional

from .audio import resolve_alsa_device, resolve_sounddevice_device
from .ffmpeg_util import find_ffmpeg


SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # int16
CHUNK_BYTES = 2400 * SAMPLE_WIDTH  # 100 ms @ 24 kHz mono


class LiveAudioHub:
    """
    Single shared mic capture for live WebSocket playback + recording WAV.
    USB mics on Jetson are usually exclusive — do not open the device twice.
    """

    def __init__(self, device: str = "auto", enabled: bool = True):
        self.device = device or "auto"
        self.enabled = bool(enabled)
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.label: Optional[str] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._listeners: list[queue.Queue] = []
        self._wav = None
        self._wav_path: Optional[Path] = None
        self._proc: Optional[subprocess.Popen] = None
        self._sd_stream = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = False

    @property
    def active(self) -> bool:
        return self._running

    @property
    def live_clients(self) -> int:
        with self._lock:
            return len(self._listeners)

    def configure(self, device: Optional[str] = None, enabled: Optional[bool] = None) -> None:
        restart = False
        with self._lock:
            if device is not None and device != self.device:
                self.device = device
                restart = self._running
            if enabled is not None and bool(enabled) != self.enabled:
                self.enabled = bool(enabled)
                restart = True
        if restart:
            self._restart()
        elif self.enabled and (self._listeners or self._wav is not None):
            self._ensure_running()
        elif not self.enabled:
            self._stop_capture()

    def acquire_live(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=40)
        with self._lock:
            self._listeners.append(q)
        if self.enabled:
            self._ensure_running()
        return q

    def release_live(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
            idle = not self._listeners and self._wav is None
        if idle:
            self._stop_capture()

    def start_wav(self, path: Path) -> bool:
        """Tap the shared capture into a WAV for recording."""
        if not self.enabled:
            self.error = "audio muted"
            return False
        path = Path(path)
        try:
            wf = wave.open(str(path), "wb")
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
        except OSError as exc:
            self.error = str(exc)
            return False
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
            self._wav = wf
            self._wav_path = path
        ok = self._ensure_running()
        if not ok:
            with self._lock:
                try:
                    self._wav.close()
                except Exception:
                    pass
                self._wav = None
                self._wav_path = None
            return False
        return True

    def stop_wav(self) -> Optional[Path]:
        path = None
        with self._lock:
            path = self._wav_path
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
            self._wav = None
            self._wav_path = None
            idle = not self._listeners
        if idle:
            self._stop_capture()
        return path

    def snapshot(self) -> dict:
        return {
            "live_audio": self._running and self.enabled,
            "live_clients": self.live_clients,
            "device": self.label or self.device,
            "sample_rate": self.sample_rate,
            "enabled": self.enabled,
            "error": self.error,
        }

    def _restart(self) -> None:
        need = False
        with self._lock:
            need = bool(self._listeners or self._wav is not None)
        self._stop_capture()
        if need and self.enabled:
            self._ensure_running()

    def _ensure_running(self) -> bool:
        with self._lock:
            if not self.enabled:
                self.error = "audio muted"
                return False
            if self._running:
                return True
        return self._start_capture()

    def _start_capture(self) -> bool:
        self._stop_capture()
        self.error = None
        self.label = None
        self._stop.clear()
        system = platform.system().lower()
        if system == "windows":
            ok = self._start_sounddevice()
        else:
            ok = self._start_ffmpeg_alsa() or self._start_arecord() or self._start_sounddevice()
        if not ok:
            print(f"[live-audio] failed: {self.error}", flush=True)
            return False
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, name="live-audio", daemon=True)
        self._thread.start()
        print(f"[live-audio] {self.label} @ {SAMPLE_RATE} Hz", flush=True)
        return True

    def _start_ffmpeg_alsa(self) -> bool:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.error = "ffmpeg missing"
            return False
        resolved = resolve_alsa_device(self.device)
        if not resolved:
            self.error = "no alsa device"
            return False
        alsa_id, label = resolved
        if alsa_id == "pulse" or alsa_id.startswith("pulse:"):
            src = "default" if alsa_id == "pulse" else alsa_id.split(":", 1)[1]
            input_args = ["-f", "pulse", "-i", src]
        else:
            input_args = ["-f", "alsa", "-i", alsa_id]
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-ac",
            str(CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=CHUNK_BYTES * 4,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        time.sleep(0.35)
        if self._proc.poll() is not None:
            err = ""
            try:
                if self._proc.stderr:
                    err = self._proc.stderr.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            self.error = err or "ffmpeg audio exited"
            self._proc = None
            return False
        self.label = f"ffmpeg+alsa:{label}"
        return True

    def _start_arecord(self) -> bool:
        import shutil

        arecord = shutil.which("arecord")
        if not arecord:
            self.error = "arecord missing"
            return False
        resolved = resolve_alsa_device(self.device)
        if not resolved:
            self.error = "no alsa device"
            return False
        alsa_id, label = resolved
        dev = "pulse" if alsa_id == "pulse" else alsa_id
        cmd = [
            arecord,
            "-q",
            "-D",
            dev,
            "-f",
            "S16_LE",
            "-r",
            str(SAMPLE_RATE),
            "-c",
            str(CHANNELS),
            "-t",
            "raw",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=CHUNK_BYTES * 4,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        time.sleep(0.3)
        if self._proc.poll() is not None:
            err = ""
            try:
                if self._proc.stderr:
                    err = self._proc.stderr.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            self.error = err or "arecord exited"
            self._proc = None
            return False
        self.label = f"arecord:{label}"
        return True

    def _start_sounddevice(self) -> bool:
        try:
            import sounddevice as sd
            import numpy as np
        except Exception as exc:
            self.error = f"sounddevice missing ({exc})"
            return False
        idx, label = resolve_sounddevice_device(self.device)
        if label in {"off", "sounddevice-missing", "no-input"} or str(label).startswith("not-found"):
            self.error = label
            return False
        pcm_q: queue.Queue = queue.Queue(maxsize=80)

        def _callback(indata, frames, time_info, status):  # noqa: ARG001
            try:
                pcm_q.put_nowait(bytes(indata))
            except queue.Full:
                try:
                    pcm_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    pcm_q.put_nowait(bytes(indata))
                except queue.Full:
                    pass

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=idx,
                callback=_callback,
                blocksize=1200,
            )
            stream.start()
        except Exception as exc:
            self.error = str(exc)
            return False
        self._sd_stream = stream
        self._sd_queue = pcm_q
        self.label = f"sounddevice:{label}"
        return True

    def _read_loop(self) -> None:
        try:
            if getattr(self, "_sd_stream", None) is not None:
                while not self._stop.is_set():
                    try:
                        chunk = self._sd_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    self._fanout(chunk)
                return
            proc = self._proc
            if not proc or not proc.stdout:
                return
            while not self._stop.is_set():
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                self._fanout(chunk)
        finally:
            self._running = False

    def _fanout(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            wav = self._wav
            listeners = list(self._listeners)
        if wav is not None:
            try:
                wav.writeframes(chunk)
            except Exception:
                pass
        for q in listeners:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass

    def _stop_capture(self) -> None:
        self._stop.set()
        self._running = False
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        stream = getattr(self, "_sd_stream", None)
        self._sd_stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
