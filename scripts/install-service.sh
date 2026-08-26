#!/usr/bin/env bash
# Install Stereo Vision as a systemd service (start on boot).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="stereo-vision"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
RUN="$ROOT/scripts/run.sh"

if [[ ! -f "$RUN" ]]; then
  echo "ERROR: missing $RUN"
  exit 1
fi
chmod +x "$RUN"

# Pick Python: conda env "stereo", local .venv, or system python3.
PY=""
if [[ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/miniforge3/etc/profile.d/conda.sh"
  if conda env list | awk '{print $1}' | grep -qx stereo; then
    PY="$HOME/miniforge3/envs/stereo/bin/python"
  fi
fi
if [[ -z "$PY" && -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
fi
if [[ -z "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
  echo "ERROR: python3 not found. Run scripts/setup_jetson.sh first."
  exit 1
fi
if ! "$PY" -c "import cv2, fastapi" 2>/dev/null; then
  echo "ERROR: $PY is missing dependencies (cv2 / fastapi)."
  echo "  Run: ./scripts/setup_jetson.sh"
  exit 1
fi

echo "==> Repo:    $ROOT"
echo "==> Python:  $PY"
echo "==> Service: $UNIT"

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Stereo Vision dual-camera Quest 3 streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=${USER}
WorkingDirectory=${ROOT}
ExecStart=${PY} -m server
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"

if systemctl is-active --quiet "${SERVICE_NAME}"; then
  sudo systemctl restart "${SERVICE_NAME}"
else
  sudo systemctl start "${SERVICE_NAME}"
fi

sleep 1
sudo systemctl --no-pager status "${SERVICE_NAME}" || true

echo
echo "Stereo Vision will start on every boot."
echo "  Status:  sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Stop:    sudo systemctl stop ${SERVICE_NAME}"
echo "  Disable: sudo systemctl disable ${SERVICE_NAME}"
echo
echo "On Quest Browser open the HTTPS URL (port 8443), e.g.:"
"$PY" - <<'PY' 2>/dev/null || true
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    print(f"  https://{ip}:8443")
except Exception:
    print("  https://<jetson-ip>:8443")
PY
