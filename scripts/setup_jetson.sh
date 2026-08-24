#!/usr/bin/env bash
# Jetson Nano / Jetson Linux setup for Stereo Vision
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y \
  python3-pip python3-dev \
  python3-opencv \
  v4l-utils ffmpeg \
  libssl-dev pkg-config \
  git wget

echo "==> Raising USB FS buffer (needed for two UVC cameras)"
if [[ -e /sys/module/usbcore/parameters/usbfs_memory_mb ]]; then
  echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb >/dev/null
fi
echo "options usbcore usbfs_memory_mb=1000" | sudo tee /etc/modprobe.d/stereo-vision-usb.conf >/dev/null

echo "==> Checking Python"
PY=python3
VER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    $PY is $VER"

need_newer=0
$PY - <<'PY' || need_newer=1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY

if [[ "$need_newer" -ne 0 ]]; then
  cat <<EOF

JetPack 4 on Jetson Nano ships Python $VER. This app needs Python 3.8+.

Install Miniforge (aarch64), then recreate the env:

  wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
  bash Miniforge3-Linux-aarch64.sh -b -p \$HOME/miniforge3
  source \$HOME/miniforge3/etc/profile.d/conda.sh
  conda create -y -n stereo python=3.10
  conda activate stereo
  pip install -r $ROOT/requirements.jetson.txt
  # USB cameras: keep system OpenCV if import cv2 works, otherwise:
  pip install opencv-python-headless

Then from $ROOT:

  python -m server

EOF
  exit 1
fi

echo "==> Installing Python packages (do NOT pip-install opencv-python on Jetson)"
$PY -m pip install --user --upgrade pip
$PY -m pip install --user -r "$ROOT/requirements.jetson.txt"

echo
echo "Setup finished."
echo "  1. Plug both cameras (USB 3.0, powered hub if needed)"
echo "  2. v4l2-ctl --list-devices"
echo "  3. sudo nvpmodel -m 0 && sudo jetson_clocks    # max performance"
echo "  4. python3 -m server"
echo "  5. On Quest 3 Browser open the HTTPS URL (port 8443) and accept the cert"
