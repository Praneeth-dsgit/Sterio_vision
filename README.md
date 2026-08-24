# Stereo Vision

Live **dual USB camera** feed from a **Jetson Nano** to a **Meta Quest 3**, with frame-sync, VR stereo viewing, recording, and auto-save.

The Jetson captures both cameras, pairs frames by timestamp, streams them over your LAN, and writes a side-by-side stereo file to disk while you watch.

```
USB cam L ─┐                    Wi‑Fi 6 / Ethernet
USB cam R ─┴─ Jetson Nano  ──────────────────────►  Meta Quest 3 Browser (WebXR)
                 │
                 └─ recordings/*.mp4  (auto-saved, crash-safe fragments)
```

## What you get

- Two USB cameras captured in parallel (MJPEG to stay within USB bandwidth)
- Left/right pairing with a max skew gate (default 40 ms)
- One WebSocket packet per synced pair so the headset never displays unmatched frames
- Quest 3 **immersive VR**: left camera → left eye, right camera → right eye (or dual-screen mode)
- Record / stop from the desktop page or with the Quest **trigger**
- **Auto-save while streaming** (on by default), rotating files every 5 minutes
- Works without cameras: a labeled LEFT/RIGHT test pattern so you can verify VR first

## Hardware

| Piece | Notes |
|---|---|
| Jetson Nano (or NX / Orin) | USB 3.0 recommended. Use a **powered USB hub** if both cameras starve. |
| 2× UVC USB cameras | Same model/resolution if this is a stereo pair. Enable MJPEG. |
| Meta Quest 3 | Quest Browser. Same LAN as the Jetson. |
| Network | 5 GHz / Wi‑Fi 6, or wired: Jetson Ethernet + Quest USB‑C Ethernet adapter on the same switch. |

Two uncompressed 1080p cameras will fail on USB. This app forces `MJPG`. Start at **1280×720 @ 30** and drop to 720×480 if the Nano or Wi‑Fi cannot keep up.

## Windows (try it before the Jetson)

You can develop with two webcams on this PC.

```powershell
cd C:\Users\Praneeth.kr\Desktop\Sterio_vision
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m server
```

Install [ffmpeg](https://ffmpeg.org/) and put it on PATH for MP4 output. Without it, recordings fall back to Motion-JPEG AVI.

Python **3.8+** is required (3.10 recommended). Jetson Nano JetPack 4 ships 3.6 — install a newer Python or use JetPack 5 / a newer Jetson.

Open `http://127.0.0.1:8080` on the PC. Quest 3 **must** use the HTTPS URL (WebXR requires a secure context).

## Jetson Nano

Copy this repo onto the Nano with git (from the same GitHub/GitLab remote you push to on the PC):

```bash
sudo apt-get update && sudo apt-get install -y git
git clone <YOUR_REPO_URL> ~/Sterio_vision
cd ~/Sterio_vision
chmod +x scripts/setup_jetson.sh scripts/run.sh
./scripts/setup_jetson.sh
python3 -m server
```

List cameras:

```bash
v4l2-ctl --list-devices
```

If the wrong `/dev/video*` nodes are selected (many cameras expose extra metadata nodes), pick the real capture indices in the web UI and click **Apply & restart capture**.

Optional systemd service: edit `WorkingDirectory` in `scripts/stereo-vision.service`, then:

```bash
sudo cp scripts/stereo-vision.service /etc/systemd/system/
sudo systemctl enable --now stereo-vision
```

## Quest 3

1. Put the headset on the same network as the Jetson (Wi‑Fi or USB‑C Ethernet).
2. In **Quest Browser**, open the **HTTPS** URL shown on the desktop page, for example `https://192.168.1.40:8443`.
3. Certificate warning: **Advanced → Proceed** (a self-signed cert is generated on first launch, with your LAN IPs in the SAN).
4. Tap **Enter VR**.
5. Pull the trigger to start/stop recording. Files land in `recordings/` on the Jetson and in the **Saved recordings** list.

If Enter VR fails, you are almost always on HTTP instead of HTTPS, or the cert was dismissed. Use the HTTPS URL.

## Sync and recording

- Capture threads stamp every frame. A pair is published only if `|t_left − t_right| ≤ max_skew_ms`.
- The live VR feed is JPEG (quality/width are configurable). Disk recording uses the **full capture frames**, not the compressed preview.
- ffmpeg writes **fragmented MP4**, so a crash or unplug still leaves a playable file.
- `record.auto_start: true` starts saving as soon as cameras are live. `segment_minutes: 5` rolls a new file on a timer.

## Config

`config.yaml` is the default. The UI writes overrides to `config.local.yaml`.

| Key | Meaning |
|---|---|
| `cameras.left_index` / `right_index` | OpenCV device index |
| `cameras.width` `height` `fps` | Capture format |
| `cameras.fourcc` | Keep `MJPG` for dual USB |
| `stream.max_skew_ms` | Reject unsynced pairs |
| `stream.max_width` | Preview downscale (recording stays full res) |
| `record.auto_start` | Save whenever the stream is running |
| `record.stereo_layout` | `sbs` side-by-side or `tb` top-bottom |

If left and right feel swapped in the headset, use **Swap eyes**. If a camera is upside down, set `left_flip` / `right_flip` to `h`, `v`, or `hv`.

## Network tips

- Prefer 5 GHz / Wi‑Fi 6, Jetson and Quest on the same SSID, no guest isolation.
- Wired is best: Jetson RJ45 → switch → Quest USB‑C Ethernet dongle.
- Open TCP **8080** (HTTP) and **8443** (HTTPS) on the Jetson firewall.
- If preview FPS collapses, lower `stream.max_width` and `jpeg_quality` first; keep capture res high for the saved file.

## Project layout

```
server/          capture, sync, recorder, HTTPS, WebSocket
static/          Quest / desktop UI + WebXR viewer
recordings/      auto-saved stereo files
config.yaml      defaults
scripts/         Jetson setup + systemd unit
```

Start command is always:

```bash
python3 -m server
```
