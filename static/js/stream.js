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
