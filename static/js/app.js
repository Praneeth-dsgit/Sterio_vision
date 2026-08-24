const stream = new StereoStream();
const vr = new StereoVR(stream);
const leftCanvas = document.getElementById("left-canvas");
const rightCanvas = document.getElementById("right-canvas");

function drawPreview(canvas, bitmap) {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = Math.round(rect.width * dpr);
  let h = Math.round(rect.height * dpr);
  if (w < 64 || h < 64) {
    w = bitmap.width;
    h = bitmap.height;
  }
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const ctx = canvas.getContext("2d");
  const scale = Math.max(w / bitmap.width, h / bitmap.height);
  const dw = bitmap.width * scale;
  const dh = bitmap.height * scale;
  ctx.drawImage(bitmap, (w - dw) / 2, (h - dh) / 2, dw, dh);
}

function setPill(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = "pill " + (cls || "");
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderStatus(data) {
  const rec = data.record || {};
  const cams = data.cameras || {};
  document.getElementById("left-cam-label").textContent = `cam ${cams.left?.index ?? 0}`;
  document.getElementById("right-cam-label").textContent = `cam ${cams.right?.index ?? 1}`;

  setPill("pill-link", stream.connected ? "Live" : "Reconnecting", stream.connected ? "ok" : "warn");
  setPill("pill-rec", rec.recording ? "REC" : "Idle", rec.recording ? "live" : "");

  const btn = document.getElementById("btn-record");
  btn.textContent = rec.recording ? "Stop recording" : "Start recording";
  btn.classList.toggle("live", !!rec.recording);
  vr.setRecording(!!rec.recording);

  if (data.synthetic) {
    document.getElementById("overlay-msg").textContent = "Test pattern — plug in USB cameras on the Jetson";
  }

  const httpsUrl =
    (data.urls || []).find((u) => u.https && !String(u.ip).startsWith("127.")) ||
    (data.urls || []).find((u) => u.https);
  if (!location.protocol.startsWith("https")) {
    document.getElementById("vr-hint").textContent =
      "WebXR needs HTTPS. On Quest 3 open " +
      (httpsUrl ? httpsUrl.https : "the HTTPS URL") +
      " and accept the certificate warning.";
  }
}

async function refreshRecordings() {
  const data = await api("/api/recordings");
  const list = document.getElementById("recording-list");
  const btn = document.getElementById("btn-recordings");
  btn.textContent = `Saved recordings (${data.files.length}) ▾`;
  if (!data.files.length) {
    list.innerHTML = "<li>No recordings yet.</li>";
    return;
  }
  list.innerHTML = data.files
    .map((f) => {
      const mb = (f.size / 1048576).toFixed(1);
      return `<li><a href="/recordings/${encodeURIComponent(f.name)}">${f.name}</a><span>${mb} MB</span></li>`;
    })
    .join("");
}

async function toggleRecord() {
  const status = await api("/api/status");
  if (status.record?.recording) await api("/api/record/stop", { method: "POST", body: "{}" });
  else await api("/api/record/start", { method: "POST", body: "{}" });
  renderStatus(await api("/api/status"));
  refreshRecordings();
}

stream.onFrame((frame) => {
  document.getElementById("overlay-msg").style.display = "none";
  drawPreview(leftCanvas, frame.left);
  drawPreview(rightCanvas, frame.right);
});

vr.onToggleRecord = toggleRecord;

document.getElementById("btn-record").addEventListener("click", toggleRecord);
document.getElementById("btn-swap").addEventListener("click", () => {
  stream.swapEyes = !stream.swapEyes;
});
document.getElementById("btn-vr").addEventListener("click", async () => {
  try {
    await vr.enter();
  } catch (err) {
    document.getElementById("vr-hint").textContent = String(err.message || err);
    alert(err.message || err);
  }
});

const recBtn = document.getElementById("btn-recordings");
const recPanel = document.getElementById("recordings-panel");
recBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  recPanel.hidden = !recPanel.hidden;
});
document.addEventListener("click", (event) => {
  if (!recPanel.hidden && !event.target.closest(".recordings-menu")) recPanel.hidden = true;
});

stream.connect();
api("/api/status").then(renderStatus).catch(console.error);
refreshRecordings();
setInterval(() => api("/api/status").then(renderStatus).catch(() => {}), 2000);
setInterval(() => refreshRecordings().catch(() => {}), 8000);
