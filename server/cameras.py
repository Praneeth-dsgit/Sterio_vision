from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


FLIP_MAP = {
    "none": None,
    "h": 1,
    "v": 0,
    "hv": -1,
}


def _fourcc_int(code: str) -> int:
    code = (code or "MJPG").ljust(4)[:4]
    return cv2.VideoWriter_fourcc(*code)


def apply_flip(frame: np.ndarray, mode: str) -> np.ndarray:
    flag = FLIP_MAP.get((mode or "none").lower())
    if flag is None:
        return frame
    return cv2.flip(frame, flag)


def test_pattern(width: int, height: int, label: str, hue: int, t: float) -> np.ndarray:
    """Synthetic stereo frame so the Quest viewer can be tested without cameras."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    bar = int((t * 80) % width)
    frame[:, :] = (18, 16, 22)
    color = cv2.cvtColor(np.uint8([[[hue, 200, 230]]]), cv2.COLOR_HSV2BGR)[0, 0]
    cv2.rectangle(frame, (bar, 0), (min(bar + 24, width - 1), height), tuple(int(c) for c in color), -1)
    for x in range(0, width, 80):
        cv2.line(frame, (x, 0), (x, height), (40, 40, 48), 1)
    for y in range(0, height, 80):
        cv2.line(frame, (0, y), (width, y), (40, 40, 48), 1)
    cv2.putText(frame, label, (40, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (240, 240, 240), 4, cv2.LINE_AA)
    cv2.putText(
        frame,
        time.strftime("%H:%M:%S"),
        (40, height // 2 + 70),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (180, 180, 190),
        2,
        cv2.LINE_AA,
    )
    return frame


def open_capture(index: int, width: int, height: int, fps: int, fourcc: str) -> cv2.VideoCapture:
    if platform.system() == "Windows":
        backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF)
    else:
        backends = (cv2.CAP_V4L2, cv2.CAP_ANY)

    for backend in backends:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            continue
        cap.set(cv2.CAP_PROP_FOURCC, _fourcc_int(fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap
        cap.release()
    return cv2.VideoCapture()


def list_cameras(max_index: int = 5) -> list:
    # Do not open devices here on Windows — MSMF/DSHOW probes steal the camera
    # and then live capture fails with "can't grab frame".
    return [{"index": i, "name": f"Camera {i}"} for i in range(max_index)]


@dataclass
class Frame:
    image: np.ndarray
    timestamp: float
    index: int
    synthetic: bool = False


class CameraWorker(threading.Thread):
    def __init__(
        self,
        name: str,
        index: int,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        flip: str,
        synthetic_label: str,
        synthetic_hue: int,
    ):
        super().__init__(name=f"cam-{name}", daemon=True)
        self.role = name
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.flip = flip
        self.synthetic_label = synthetic_label
        self.synthetic_hue = synthetic_hue
        self._lock = threading.Lock()
        self._latest: Optional[Frame] = None
        self._halt = threading.Event()
        self.opened = False
        self.synthetic = False
        self.frames = 0
        self.errors = 0

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        cap = open_capture(self.index, self.width, self.height, self.fps, self.fourcc)
        self.opened = bool(cap.isOpened())
        self.synthetic = not self.opened
        period = 1.0 / max(self.fps, 1)
        seq = 0
        try:
            while not self._halt.is_set():
                t0 = time.time()
                if self.synthetic:
                    image = test_pattern(self.width, self.height, self.synthetic_label, self.synthetic_hue, t0)
                    ok = True
                else:
                    ok, image = cap.read()
                    if not ok or image is None:
                        self.errors += 1
                        if self.errors >= 20:
                            self.synthetic = True
                            cap.release()
                        time.sleep(0.02)
                        continue
                    self.errors = 0
                    image = apply_flip(image, self.flip)
                    if image.shape[1] != self.width or image.shape[0] != self.height:
                        image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
                frame = Frame(image=image, timestamp=t0, index=seq, synthetic=self.synthetic)
                with self._lock:
                    self._latest = frame
                self.frames += 1
                seq += 1
                leftover = period - (time.time() - t0)
                if leftover > 0 and self.synthetic:
                    time.sleep(leftover)
        finally:
            cap.release()
