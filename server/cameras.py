from __future__ import annotations

import os
import platform
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_DEVICE_CAPS = 0x80000000
# VIDIOC_QUERYCAP = _IOWR('V', 0, struct v4l2_capability)  (104 bytes)
_VIDIOC_QUERYCAP = (3 << 30) | (104 << 16) | (ord("V") << 8)


FLIP_MAP = {
    "none": None,
    "h": 1,
    "v": 0,
    "hv": -1,
}


def _fourcc_int(code: str) -> int:
    code = (code or "MJPG").ljust(4)[:4]
    return cv2.VideoWriter_fourcc(*code)


def _fourcc_str(value: int) -> str:
    try:
        return "".join(chr((int(value) >> (8 * i)) & 0xFF) for i in range(4))
    except Exception:
        return "????"


def is_v4l_capture(index: int) -> bool:
    """True if /dev/videoN is a real capture node, not UVC metadata."""
    if platform.system() == "Windows":
        return True
    node = Path(f"/dev/video{index}")
    if not node.exists():
        return False
    name_path = Path(f"/sys/class/video4linux/video{index}/name")
    try:
        label = name_path.read_text(encoding="utf-8", errors="ignore").lower()
        if "metadata" in label:
            return False
    except OSError:
        pass
    try:
        import fcntl

        fd = os.open(str(node), os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, buf, True)
        caps = struct.unpack_from("<I", buf, 84)[0]
        device_caps = struct.unpack_from("<I", buf, 88)[0]
        flags = device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps
        return bool(flags & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE))
    except OSError:
        return False
    finally:
        os.close(fd)


def looks_like_video(frame) -> bool:
    if frame is None or getattr(frame, "size", 0) < 160 * 120 * 3:
        return False
    h, w = frame.shape[:2]
    return w >= 160 and h >= 120 and frame.ndim == 3


def fit_frame(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Scale with aspect ratio preserved (letterbox), never squash."""
    ih, iw = image.shape[:2]
    if iw == width and ih == height:
        return image
    scale = min(width / float(iw), height / float(ih))
    nw = max(1, int(round(iw * scale)))
    nh = max(1, int(round(ih * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(image, (nw, nh), interpolation=interp)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    x = (width - nw) // 2
    y = (height - nh) // 2
    canvas[y : y + nh, x : x + nw] = resized
    return canvas


def open_capture(index: int, width: int, height: int, fps: int, fourcc: str) -> cv2.VideoCapture:
    if platform.system() != "Windows" and not is_v4l_capture(index):
        print(f"[cam {index}] skip /dev/video{index}: not a V4L capture node", flush=True)
        return cv2.VideoCapture()

    if platform.system() == "Windows":
        backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF)
    else:
        backends = (cv2.CAP_V4L2, cv2.CAP_ANY)

    sizes = []
    for pair in ((width, height), (1280, 720), (960, 540), (800, 600), (640, 480)):
        if pair not in sizes:
            sizes.append(pair)
    codecs = []
    for code in (fourcc, "MJPG", "YUY2", "YUYV"):
        if code and code not in codecs:
            codecs.append(code)

    for backend in backends:
        for code in codecs:
            for w, h in sizes:
                cap = cv2.VideoCapture(index, backend)
                if not cap.isOpened():
                    break
                cap.set(cv2.CAP_PROP_FOURCC, _fourcc_int(code))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                cap.set(cv2.CAP_PROP_FPS, fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, frame = cap.read()
                if ok and looks_like_video(frame):
                    got_w, got_h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    got_cc = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
                    print(
                        f"[cam {index}] {got_w}x{got_h} @ {code}/{got_cc} (requested {w}x{h} {code})",
                        flush=True,
                    )
                    return cap
                cap.release()
    return cv2.VideoCapture()


def discover_capture_indices(max_index: int = 10) -> list:
    """Linux: skip V4L metadata nodes; keep devices that actually return a video frame."""
    if platform.system() == "Windows":
        return list(range(max_index))
    found = []
    for index in range(max_index):
        if not is_v4l_capture(index):
            continue
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            continue
        cap.set(cv2.CAP_PROP_FOURCC, _fourcc_int("MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, frame = cap.read()
        cap.release()
        if ok and looks_like_video(frame):
            found.append(index)
    return found


def resolve_camera_indices(left: int, right: int) -> tuple:
    if platform.system() == "Windows":
        return int(left), int(right)
    good = discover_capture_indices()
    left, right = int(left), int(right)
    if left in good and right in good and left != right:
        return left, right
    if len(good) >= 2:
        print(
            f"Camera indices {left}/{right} are not both capture nodes. "
            f"Using {good[0]} (left) and {good[1]} (right). "
            "Check with: v4l2-ctl --list-devices",
            flush=True,
        )
        return good[0], good[1]
    return left, right


def list_cameras(max_index: int = 5) -> list:
    if platform.system() != "Windows":
        return [{"index": i, "name": f"/dev/video{i}"} for i in discover_capture_indices(max_index + 4)]
    return [{"index": i, "name": f"Camera {i}"} for i in range(max_index)]


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
                    image = fit_frame(image, self.width, self.height)
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
