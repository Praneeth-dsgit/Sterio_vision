from __future__ import annotations

import platform
import re
import signal
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Protocol

from .ffmpeg_util import find_ffmpeg


def list_audio_devices() -> list[dict]:
    """Best-effort capture device list for the UI / auto-pick."""
    devices: list[dict] = []
    devices.extend(_list_sounddevice_inputs())
    if platform.system().lower() == "windows":
        for item in _list_dshow_audio():
            if not any(d["id"] == item["id"] for d in devices):
                devices.append(item)
    else:
        for item in _list_alsa_capture():
            if not any(d["id"] == item["id"] for d in devices):
                devices.append(item)
        if not any(d["id"] == "default" for d in devices):
            devices.append({"id": "default", "name": "ALSA default", "backend": "alsa"})
        if not any(d["id"] == "pulse" for d in devices):
            devices.append({"id": "pulse", "name": "PulseAudio default", "backend": "pulse"})
    return devices


def resolve_audio_ffmpeg_args(device: str) -> Optional[tuple[list[str], str]]:
    """
    Inline ffmpeg mic+video is unreliable on Jetson (silent / stalls).
    Always return None — video is encoded alone; mic is a sidecar WAV muxed on stop.
    """
    raw = (device or "auto").strip()
    if not raw or raw.lower() in {"off", "none", "false", "0"}:
        return None
    return None


def _ffmpeg_has_format(name: str) -> bool:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-formats"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        return bool(re.search(rf"\b{re.escape(name)}\b", text))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _list_alsa_capture() -> list[dict]:
    arecord = shutil.which("arecord")
    if not arecord:
        return []
    try:
        proc = subprocess.run(
            [arecord, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    devices = []
    pattern = re.compile(
        r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\],\s+device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]",
        re.IGNORECASE,
    )
    for line in (proc.stdout or "").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        card, card_id, card_name, dev, _dev_id, dev_name = match.groups()
        plug = f"plughw:{card},{dev}"
        label = f"{card_name.strip()} — {dev_name.strip()}"
        devices.append({"id": plug, "name": f"{label} ({plug})", "backend": "alsa"})
    return devices


def _alsa_score(dev: dict) -> int:
    n = f"{dev.get('name', '')} {dev.get('id', '')}".lower()
    if any(x in n for x in ("hdmi", "tegra", "hda nvidia", "p2964", "gpudemux")):
        return -5
    if any(x in n for x in ("usb", "camera", "webcam", "mic", "uac")):
        return 5
    if "plughw" in n:
        return 1
    return 0


def resolve_alsa_device(device: str) -> Optional[tuple[str, str]]:
    """Pick an ALSA capture device id + label."""
    raw = (device or "auto").strip()
    if not raw or raw.lower() in {"off", "none", "false", "0"}:
        return None
    devices = _list_alsa_capture()
    if raw.lower() in {"auto", "default"}:
        ranked = sorted(devices, key=lambda d: (-_alsa_score(d), d["id"]))
        if ranked and _alsa_score(ranked[0]) > 0:
            return ranked[0]["id"], ranked[0]["name"]
        if ranked:
            return ranked[0]["id"], ranked[0]["name"]
        if _ffmpeg_has_format("pulse"):
            return "pulse", "pulse:default"
        return "default", "alsa:default"
    if raw.lower() == "pulse":
        return "pulse", "pulse:default"
    lowered = raw.lower()
    for d in devices:
        if lowered == d["id"].lower() or lowered in d["name"].lower():
            return d["id"], d["name"]
    # Allow raw plughw:/hw: strings even if not listed.
    if raw.startswith(("plughw:", "hw:", "sysdefault:", "default")):
        return raw, raw
    return None


def _list_dshow_audio() -> list[dict]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (proc.stderr or "") + (proc.stdout or "")
    devices = []
    in_audio = False
    for line in text.splitlines():
        lower = line.lower()
        if "directshow audio devices" in lower:
            in_audio = True
            continue
        if "directshow video devices" in lower:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = re.search(r'"([^"]+)"', line)
        if match:
            name = match.group(1)
            devices.append({"id": name, "name": name, "backend": "dshow"})
    return devices


def _list_sounddevice_inputs() -> list[dict]:
    try:
        import sounddevice as sd
    except Exception:
        return []
    devices = []
    try:
        for idx, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels") or 0) <= 0:
                continue
            name = str(info.get("name") or f"Input {idx}")
            devices.append({"id": str(idx), "name": name, "backend": "sounddevice", "index": idx})
    except Exception:
        return []
    return devices


def resolve_sounddevice_device(device: str) -> tuple[Optional[int], str]:
    raw = (device or "auto").strip()
    if not raw or raw.lower() in {"off", "none", "false", "0"}:
        return None, "off"
    try:
        import sounddevice as sd
    except Exception:
        return None, "sounddevice-missing"

    devices = _list_sounddevice_inputs()
    if raw.isdigit():
        idx = int(raw)
        label = next((d["name"] for d in devices if int(d["id"]) == idx), f"device {idx}")
        return idx, label

    if raw.lower() in {"auto", "default"}:
        def _score(name: str) -> int:
            n = name.lower()
            if "microsoft sound mapper" in n or "primary sound capture" in n:
                return -1
            if "microphone" in n:
                return 3
            if "webcam" in n or "camera" in n or "smartcam" in n:
                return 2
            if n.startswith("mic ") or " mic" in n or n.endswith(" mic"):
                return 2
            return 0

        ranked = sorted(
            ((_score(d["name"]), int(d["id"]), d["name"]) for d in devices),
            key=lambda t: (-t[0], t[1]),
        )
        if ranked and ranked[0][0] > 0:
            return ranked[0][1], ranked[0][2]
        try:
            idx = sd.default.device[0]
            if idx is not None and int(idx) >= 0:
                info = sd.query_devices(idx)
                return int(idx), str(info.get("name") or f"default {idx}")
        except Exception:
            pass
        if devices:
            return int(devices[0]["id"]), devices[0]["name"]
        return None, "no-input"

    lowered = raw.lower()
    for d in devices:
        if lowered in d["name"].lower():
            return int(d["id"]), d["name"]
    return None, f"not-found:{raw}"


class _MicLike(Protocol):
    label: Optional[str]
    error: Optional[str]

    def start(self, wav_path: Path) -> bool: ...
    def stop(self) -> Optional[Path]: ...

    @property
    def active(self) -> bool: ...


class FfmpegAlsaMicRecorder:
    """Linux: ffmpeg reads ALSA/Pulse → WAV (no PortAudio needed on Jetson)."""

    def __init__(self, device: str = "auto", sample_rate: int = 44100):
        self.device = device
        self.sample_rate = sample_rate
        self.path: Optional[Path] = None
        self.label: Optional[str] = None
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, wav_path: Path) -> bool:
        self.stop()
        self.error = None
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.error = "ffmpeg missing"
            return False
        resolved = resolve_alsa_device(self.device)
        if not resolved:
            self.error = "no alsa device"
            return False
        alsa_id, label = resolved
        self.path = Path(wav_path)
        self.label = f"ffmpeg+alsa:{label}"
        # pulse backend uses -f pulse; everything else uses -f alsa
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
            "-y",
            *input_args,
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-c:a",
            "pcm_s16le",
            str(self.path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.error = str(exc)
            self._proc = None
            return False
        time.sleep(0.4)
        if self._proc.poll() is not None:
            err = ""
            try:
                if self._proc.stderr:
                    err = self._proc.stderr.read().decode("utf-8", errors="ignore")[:240]
            except Exception:
                pass
            self.error = err or "ffmpeg mic exited"
            self._proc = None
            return False
        self._active = True
        print(f"[record] mic: {self.label}", flush=True)
        return True

    def stop(self) -> Optional[Path]:
        self._active = False
        path = self.path
        if self._proc is not None:
            try:
                self._proc.send_signal(signal.SIGINT)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self.path = None
        return path


class ArecordMicRecorder:
    """Linux fallback: arecord → WAV."""

    def __init__(self, device: str = "auto", sample_rate: int = 44100):
        self.device = device
        self.sample_rate = sample_rate
        self.path: Optional[Path] = None
        self.label: Optional[str] = None
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, wav_path: Path) -> bool:
        self.stop()
        self.error = None
        arecord = shutil.which("arecord")
        if not arecord:
            self.error = "arecord missing"
            return False
        resolved = resolve_alsa_device(self.device)
        if not resolved:
            self.error = "no alsa device"
            return False
        alsa_id, label = resolved
        self.path = Path(wav_path)
        self.label = f"arecord:{label}"
        cmd = [
            arecord,
            "-q",
            "-D",
            alsa_id if alsa_id != "pulse" else "pulse",
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            "1",
            "-t",
            "wav",
            str(self.path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        time.sleep(0.35)
        if self._proc.poll() is not None:
            err = ""
            try:
                if self._proc.stderr:
                    err = self._proc.stderr.read().decode("utf-8", errors="ignore")[:240]
            except Exception:
                pass
            self.error = err or "arecord exited"
            self._proc = None
            return False
        self._active = True
        print(f"[record] mic: {self.label}", flush=True)
        return True

    def stop(self) -> Optional[Path]:
        self._active = False
        path = self.path
        if self._proc is not None:
            try:
                self._proc.send_signal(signal.SIGINT)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self.path = None
        return path


class SounddeviceMicRecorder:
    """PortAudio mic capture → WAV (Windows + optional Linux)."""

    def __init__(self, device: str = "auto", sample_rate: int = 44100):
        self.device = device
        self.sample_rate = sample_rate
        self.path: Optional[Path] = None
        self.label: Optional[str] = None
        self.error: Optional[str] = None
        self._stream = None
        self._wav = None
        self._lock = threading.Lock()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, wav_path: Path) -> bool:
        self.stop()
        self.error = None
        try:
            import sounddevice as sd
        except Exception as exc:
            self.error = f"sounddevice not installed ({exc})"
            return False

        idx, label = resolve_sounddevice_device(self.device)
        if label in {"off", "sounddevice-missing", "no-input"} or label.startswith("not-found"):
            self.error = label
            return False

        self.path = Path(wav_path)
        self.label = label if idx is None else f"{label} (#{idx})"
        try:
            self._wav = wave.open(str(self.path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.sample_rate)

            def _callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
                with self._lock:
                    if self._wav is not None:
                        self._wav.writeframes(indata.tobytes())

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=idx,
                callback=_callback,
                blocksize=1024,
            )
            self._stream.start()
            self._active = True
            print(f"[record] mic: {self.label}", flush=True)
            return True
        except Exception as exc:
            self.error = str(exc)
            self.stop()
            return False

    def stop(self) -> Optional[Path]:
        self._active = False
        path = self.path
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        with self._lock:
            try:
                if self._wav is not None:
                    self._wav.close()
            except Exception:
                pass
            self._wav = None
        self.path = None
        return path


# Back-compat alias used by older imports
MicRecorder = SounddeviceMicRecorder


def start_mic_capture(device: str, wav_path: Path) -> Optional[_MicLike]:
    """
    Start the best available mic backend.
    Linux/Jetson: ffmpeg+ALSA → arecord → sounddevice
    Windows: sounddevice
    """
    raw = (device or "auto").strip()
    if not raw or raw.lower() in {"off", "none", "false", "0"}:
        return None

    system = platform.system().lower()
    backends: list[_MicLike]
    if system == "windows":
        backends = [SounddeviceMicRecorder(device=device)]
    else:
        backends = [
            FfmpegAlsaMicRecorder(device=device),
            ArecordMicRecorder(device=device),
            SounddeviceMicRecorder(device=device),
        ]

    errors = []
    for backend in backends:
        if backend.start(wav_path):
            return backend
        errors.append(f"{type(backend).__name__}: {backend.error}")
        try:
            if wav_path.exists() and wav_path.stat().st_size < 128:
                wav_path.unlink(missing_ok=True)
        except OSError:
            pass

    print("[record] all mic backends failed: " + " | ".join(errors), flush=True)
    return None
