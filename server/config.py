from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
LOCAL_CONFIG_PATH = ROOT / "config.local.yaml"

DEFAULTS: dict[str, Any] = {
    "cameras": {
        "left_index": 0,
        "right_index": 1,
        "width": 1280,
        "height": 720,
        "fps": 30,
        "fourcc": "MJPG",
        "left_flip": "none",
        "right_flip": "none",
        "hfov_deg": 70,
    },
    "stream": {
        "jpeg_quality": 85,
        "max_width": 1920,
        "max_skew_ms": 40,
    },
    "server": {
        "host": "0.0.0.0",
        "http_port": 8080,
        "https_port": 8443,
        "https": True,
    },
    "record": {
        "auto_start": False,
        "directory": "recordings",
        "segment_minutes": 5,
        "stereo_layout": "sbs",
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            cfg = _deep_merge(cfg, loaded)
    return cfg


def save_local_config(cfg: dict[str, Any]) -> None:
    with LOCAL_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False)


def recordings_dir(cfg: Optional[dict[str, Any]] = None) -> Path:
    cfg = cfg or load_config()
    path = Path(cfg["record"]["directory"])
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path
