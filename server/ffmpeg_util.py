from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg: PATH, imageio-ffmpeg bundle, then common install folders."""
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("FFMPEG_BINARY", "")),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ]
    for path in candidates:
        if path and str(path) not in {".", ""} and path.is_file():
            return str(path)
    return None
