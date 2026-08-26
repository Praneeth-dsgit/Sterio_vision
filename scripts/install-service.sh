#!/usr/bin/env bash
# Install Stereo Vision as a systemd service (start on boot).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="stereo-vision"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
RUN="$ROOT/scripts/run.sh"
FORCE_PY="${1:-}"

if [[ ! -f "$RUN" ]]; then
  echo "ERROR: missing $RUN"
  exit 1
fi
chmod +x "$RUN"

python_ok() {
  local py="$1"
  [[ -x "$py" ]] || return 1
  "$py" -c "import cv2, fastapi" 2>/dev/null
}

# Optional: ./scripts/install-service.sh /path/to/python
if [[ -n "$FORCE_PY" ]]; then
  if [[ ! -x "$FORCE_PY" ]]; then
    echo "ERROR: not an executable Python: $FORCE_PY"
    exit 1
  fi
  if ! python_ok "$FORCE_PY"; then
    echo "ERROR: $FORCE_PY cannot import cv2 and fastapi."
    echo "  Install deps into that interpreter, then retry."
    exit 1
  fi
  PY="$FORCE_PY"
else
  CANDIDATES=()
  # Active conda env (if already activated)
  if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    CANDIDATES+=("${CONDA_PREFIX}/bin/python")
  fi
  # Common env locations
  for base in "$HOME/miniforge3" "$HOME/mambaforge" "$HOME/miniconda3" "$HOME/anaconda3"; do
    CANDIDATES+=("$base/envs/stereo/bin/python")
    CANDIDATES+=("$base/bin/python")
  done
  # Project venvs (env_stream is the usual Jetson name for this repo)
  CANDIDATES+=("$ROOT/env_stream/bin/python")
  CANDIDATES+=("$ROOT/.venv/bin/python")
  CANDIDATES+=("$ROOT/venv/bin/python")
  # Every python3 on PATH (user may have put conda first in interactive shells only)
  while IFS= read -r p; do
    [[ -n "$p" ]] && CANDIDATES+=("$p")
  done < <(command -v -a python3 2>/dev/null || true)
  CANDIDATES+=("/usr/bin/python3")

  PY=""
  for cand in "${CANDIDATES[@]}"; do
    if python_ok "$cand"; then
      PY="$cand"
      break
    fi
  done
fi

if [[ -z "${PY:-}" ]]; then
  SYS_PY="$(command -v python3 || echo /usr/bin/python3)"
  echo "ERROR: no Python found that can import both cv2 and fastapi."
  echo
  echo "Checked env_stream, .venv, conda stereo, and system python3."
  echo "System interpreter: $SYS_PY"
  if [[ -x "$SYS_PY" ]]; then
    echo "  cv2:     $($SYS_PY -c 'import cv2; print(cv2.__version__)' 2>&1 || echo MISSING)"
    echo "  fastapi: $($SYS_PY -c 'import fastapi; print(fastapi.__version__)' 2>&1 || echo MISSING)"
    echo "  version: $($SYS_PY -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>&1)"
  fi
  ENV_PY="$ROOT/env_stream/bin/python"
  if [[ -x "$ENV_PY" ]]; then
    echo
    echo "Found $ENV_PY but it is missing packages:"
    echo "  cv2:     $($ENV_PY -c 'import cv2; print(cv2.__version__)' 2>&1 || echo MISSING)"
    echo "  fastapi: $($ENV_PY -c 'import fastapi; print(fastapi.__version__)' 2>&1 || echo MISSING)"
    echo "  Fix:"
    echo "    source $ROOT/env_stream/bin/activate"
    echo "    pip install -r $ROOT/requirements.jetson.txt"
    echo "    ./scripts/install-service.sh $ENV_PY"
  fi
  echo
  echo "Fix — pick ONE path:"
  echo
  echo "A) Project venv env_stream (recommended if you already use it):"
  echo "   source $ROOT/env_stream/bin/activate"
  echo "   pip install -r $ROOT/requirements.jetson.txt"
  echo "   ./scripts/install-service.sh $ROOT/env_stream/bin/python"
  echo
  echo "B) System Python 3.8+ (JetPack 5 / Orin):"
  echo "   cd $ROOT"
  echo "   ./scripts/setup_jetson.sh"
  echo "   ./scripts/install-service.sh"
  echo
  echo "C) Conda (JetPack 4 / Nano with Python 3.6):"
  echo "   source \$HOME/miniforge3/etc/profile.d/conda.sh"
  echo "   conda activate stereo"
  echo "   pip install -r $ROOT/requirements.jetson.txt"
  echo "   cd $ROOT && ./scripts/install-service.sh \"\$(which python)\""
  echo
  echo "D) Point at a known-good Python:"
  echo "   ./scripts/install-service.sh $ROOT/env_stream/bin/python"
  exit 1
fi

echo "==> Repo:    $ROOT"
echo "==> Python:  $PY ($("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
echo "==> Service: $UNIT"

# systemd User= services get a minimal env; ensure user site-packages work for --user installs.
HOME_DIR="$(getent passwd "$USER" | cut -d: -f6)"
USER_SITE="$("$PY" -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
EXTRA_ENV="Environment=PYTHONUNBUFFERED=1"
EXTRA_ENV+=$'\n'"Environment=HOME=${HOME_DIR}"
if [[ -n "$USER_SITE" ]]; then
  EXTRA_ENV+=$'\n'"Environment=PYTHONPATH=${USER_SITE}"
fi

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
${EXTRA_ENV}

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
echo "  Logs:    journalctl -u stereo-vision -f"
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
