const HEADER_BYTES = 36;

function parseStereoPacket(buffer) {
  const view = new DataView(buffer);
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  if (magic !== "SV01") throw new Error("bad packet magic");
  const tsLeft = view.getFloat64(4, true);
  const tsRight = view.getFloat64(12, true);
  const leftLen = view.getUint32(20, true);
  const rightLen = view.getUint32(24, true);
  const skewMs = view.getFloat64(28, true);
  const left = buffer.slice(HEADER_BYTES, HEADER_BYTES + leftLen);
  const right = buffer.slice(HEADER_BYTES + leftLen, HEADER_BYTES + leftLen + rightLen);
  return { tsLeft, tsRight, skewMs, left, right };
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/stream`;
}
