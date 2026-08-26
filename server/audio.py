from __future__ import annotations

import platform
import re
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Optional

from .ffmpeg_util import find_ffmpeg


def list_audio_devices() -> list[dict]:
    """Best-effort capture device list for the UI / auto-pick."""
    devices: list[dict] = []
    devices.extend(_list_sounddevice_inputs())
    if platform.system().lower() == "windows":
        # Also surface DirectShow names when ffmpeg is present.
        for item in _list_dshow_audio():
            if not any(d["id"] == item["id"] for d in devices):
                devices.append(item)
    else:
        for item in _list_alsa_capture():
            if not any(d["id"] == item["id"] for d in devices):
                devices.append(item)
        if not devices:
            devices.append({"id": "default", "name": "ALSA default", "backend": "alsa"})
            devices.append({"id": "pulse", "name": "PulseAudio default", "backend": "pulse"})
    return devices


def resolve_audio_ffmpeg_args(device: str) -> Optional[tuple[list[str], str]]:
    """
    Build ffmpeg input args for a capture device (Linux ALSA/Pulse, Windows dshow).
    Returns None when audio should be captured another way (or disabled).
    """
    raw = (device or "auto").strip()
    if not raw or raw.lower() in {"off", "none", "false", "0"}:
        return None

    system = platform.system().lower()
    # On Windows prefer sounddevice sidecar capture — dshow is flaky / often missing.
    if system == "windows":
        return None

    if raw.lower() == "auto":
        return _auto_audio_args_linux()

    backend = "alsa"
    source = raw
    if raw.lower() == "pulse" or raw.lower().startswith("pulse:"):
        backend = "pulse"
        source = "default" if raw.lower() == "pulse" else raw.split(":", 1)[1]
    elif raw.lower() == "default":
        if _ffmpeg_has_format("pulse"):
            return (["-f", "pulse", "-thread_queue_size", "1024", "-i", "default"], "pulse:default")
        source = "default"
    return (
        ["-f", backend, "-thread_queue_size", "1024", "-i", source],
        f"{backend}:{source}",
    )


def _auto_audio_args_linux() -> Optional[tuple[list[str], str]]:
    alsa = _list_alsa_capture()
    usbish = [
        d
        for d in alsa
        if any(k in d["name"].lower() for k in ("usb", "camera", "webcam", "mic"))
    ]
    pick = usbish or alsa
    if pick:
        return (
            ["-f", "alsa", "-thread_queue_size", "1024", "-i", pick[0]["id"]],
            f"alsa:{pick[0]['id']} ({pick[0]['name']})",
        )
    if _ffmpeg_has_format("pulse"):
        return (["-f", "pulse", "-thread_queue_size", "1024", "-i", "default"], "pulse:default")
    if _ffmpeg_has_format("alsa"):
        return (["-f", "alsa", "-thread_queue_size", "1024", "-i", "default"], "alsa:default")
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
        devices.append(
            {
                "id": plug,
                "name": f"{card_name.strip()} — {dev_name.strip()} ({plug})",
                "backend": "alsa",
            }
        )
        devices.append(
            {
                "id": f"hw:{card},{dev}",
                "name": f"{card_name.strip()} raw ({card_id}:{dev})",
                "backend": "alsa",
            }
        )
    return devices


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
        if "directshow audio devices" in lower or "audio devices" == lower.strip():
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
            devices.append(
                {
                    "id": str(idx),
                    "name": name,
                    "backend": "sounddevice",
                    "index": idx,
                }
            )
    except Exception:
        return []
    return devices


def resolve_sounddevice_device(device: str) -> tuple[Optional[int], str]:
    """Pick a sounddevice input index. Returns (index_or_None_for_default, label)."""
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
        # Prefer real mics / webcams; avoid matching "mic" inside "Microsoft".
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
            if "usb" in n and "array" in n:
                return 1
            return 0

        ranked = sorted(
            (( _score(d["name"]), int(d["id"]), d["name"]) for d in devices),
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

    # Match by substring name
    lowered = raw.lower()
    for d in devices:
        if lowered in d["name"].lower():
            return int(d["id"]), d["name"]
    return None, f"not-found:{raw}"


class MicRecorder:
    """PortAudio mic capture → WAV (works on Windows without ffmpeg dshow)."""

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
        if label == "off":
            return False
        if label.startswith("not-found") or label == "no-input":
            self.error = label
            return False
        if label == "sounddevice-missing":
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
                if status:
                    pass
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
