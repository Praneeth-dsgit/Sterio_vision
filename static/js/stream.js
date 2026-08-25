function lensVerticalFov(hfovDeg, width, height) {
  const aspect = height / Math.max(width, 1);
  const h = (hfovDeg * Math.PI) / 180;
  return (2 * Math.atan(Math.tan(h / 2) * aspect) * 180) / Math.PI;
}

function drawStreamGrid(ctx, x, y, w, h, hfovDeg) {
  if (!ctx || w < 32 || h < 32) return;
  const hfov = Number(hfovDeg) > 0 ? Number(hfovDeg) : 70;
  const vfov = lensVerticalFov(hfov, w, h);
  const fx = w / 2 / Math.tan((hfov * Math.PI) / 360);
  const fy = h / 2 / Math.tan((vfov * Math.PI) / 360);
  const cx = x + w / 2;
  const cy = y + h / 2;
  const lineW = Math.max(1, w / 900);
  const fontPx = Math.max(12, Math.round(w / 52));
  const step = 10;

  ctx.save();
  ctx.strokeStyle = "rgba(61, 224, 255, 0.38)";
  ctx.fillStyle = "rgba(210, 245, 255, 0.92)";
  ctx.lineWidth = lineW;
  ctx.font = `600 ${fontPx}px sans-serif`;
  ctx.textBaseline = "top";

  const maxH = Math.floor(hfov / 2 / step) * step;
  for (let deg = -maxH; deg <= maxH; deg += step) {
    if (deg === 0) continue;
    const px = cx + fx * Math.tan((deg * Math.PI) / 180);
    ctx.beginPath();
    ctx.moveTo(px, y);
    ctx.lineTo(px, y + h);
    ctx.stroke();
    ctx.textAlign = deg < 0 ? "right" : "left";
    ctx.fillText(`${deg > 0 ? "+" : ""}${deg}°`, px + (deg < 0 ? -6 : 6), y + 8);
  }

  const maxV = Math.floor(vfov / 2 / step) * step;
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  for (let deg = -maxV; deg <= maxV; deg += step) {
    if (deg === 0) continue;
    const py = cy - fy * Math.tan((deg * Math.PI) / 180);
    ctx.beginPath();
    ctx.moveTo(x, py);
    ctx.lineTo(x + w, py);
    ctx.stroke();
    ctx.fillText(`${deg > 0 ? "+" : ""}${deg}°`, x + 8, py - 4);
  }

  ctx.strokeStyle = "rgba(255, 80, 110, 0.92)";
  ctx.lineWidth = Math.max(2, w / 480);
  ctx.beginPath();
  ctx.moveTo(cx, y);
  ctx.lineTo(cx, y + h);
  ctx.moveTo(x, cy);
  ctx.lineTo(x + w, cy);
  ctx.stroke();
  const mark = Math.max(10, w * 0.014);
  ctx.beginPath();
  ctx.arc(cx, cy, mark, 0, Math.PI * 2);
  ctx.moveTo(cx - mark * 1.6, cy);
  ctx.lineTo(cx + mark * 1.6, cy);
  ctx.moveTo(cx, cy - mark * 1.6);
  ctx.lineTo(cx, cy + mark * 1.6);
  ctx.stroke();

  const label = `Lens ${hfov.toFixed(0)}° H × ${vfov.toFixed(0)}° V`;
  ctx.font = `700 ${Math.max(13, Math.round(w / 38))}px sans-serif`;
  const padX = 10;
  const padY = 7;
  const textW = ctx.measureText(label).width;
  const boxH = Math.max(22, Math.round(w / 28));
  const bx = x + 10;
  const by = y + h - boxH - 12;
  ctx.fillStyle = "rgba(8, 12, 20, 0.72)";
  ctx.fillRect(bx, by, textW + padX * 2, boxH);
  ctx.fillStyle = "#3de0ff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(label, bx + padX, by + boxH / 2);
  ctx.restore();
}

class StereoStream {
  constructor() {
    this.ws = null;
    this.listeners = new Set();
    this.connected = false;
    this.swapEyes = false;
  }

  onFrame(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  connect() {
    this.disconnect();
    const socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";
    this.ws = socket;
    socket.onopen = () => {
      this.connected = true;
    };
    socket.onclose = () => {
      this.connected = false;
      setTimeout(() => this.connect(), 800);
    };
    socket.onerror = () => socket.close();
    socket.onmessage = async (event) => {
      try {
        const packet = parseStereoPacket(event.data);
        const leftBlob = new Blob([packet.left], { type: "image/jpeg" });
        const rightBlob = new Blob([packet.right], { type: "image/jpeg" });
        const [leftBmp, rightBmp] = await Promise.all([
          createImageBitmap(leftBlob),
          createImageBitmap(rightBlob),
        ]);
        const frame = {
          left: this.swapEyes ? rightBmp : leftBmp,
          right: this.swapEyes ? leftBmp : rightBmp,
          skewMs: packet.skewMs,
          ts: packet.tsLeft,
        };
        for (const fn of this.listeners) fn(frame);
        try {
          leftBmp.close();
          rightBmp.close();
        } catch (err) {
          /* ImageBitmap.close is optional */
        }
      } catch (err) {
        console.warn("frame decode failed", err);
      }
    };
  }

  disconnect() {
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }
}
