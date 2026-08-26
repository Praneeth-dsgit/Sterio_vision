const stream = new StereoStream();
const vr = new StereoVR(stream);
const leftCanvas = document.getElementById("left-canvas");
const rightCanvas = document.getElementById("right-canvas");
let lensHfov = 70;
let capW = 1280;
let capH = 720;
let lastCams = { leftIndex: 0, rightIndex: 1 };

function applyEyeLabels() {
  const swapped = stream.swapEyes;
  const leftIdx = swapped ? lastCams.rightIndex : lastCams.leftIndex;
  const rightIdx = swapped ? lastCams.leftIndex : lastCams.rightIndex;
  document.getElementById("left-eye-tag").textContent = swapped ? "RIGHT" : "LEFT";
  document.getElementById("right-eye-tag").textContent = swapped ? "LEFT" : "RIGHT";
  document.getElementById("left-cam-label").textContent = `cam ${leftIdx}`;
  document.getElementById("right-cam-label").textContent = `cam ${rightIdx}`;
  document.getElementById("btn-swap").classList.toggle("on", swapped);
  document.getElementById("btn-swap").textContent = swapped ? "Swap Cameras (on)" : "Swap Cameras";
}

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
  const ox = (w - dw) / 2;
  const oy = (h - dh) / 2;
  ctx.drawImage(bitmap, ox, oy, dw, dh);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, w, h);
  ctx.clip();
  drawStreamGrid(ctx, ox, oy, dw, dh, lensHfov);
  ctx.restore();
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
  const hfov = Number(data.config?.cameras?.hfov_deg) || 70;
  lensHfov = hfov;
  if (vr.setLensFov) vr.setLensFov(hfov);
  capW = Number(data.config?.cameras?.width) || 1280;
  capH = Number(data.config?.cameras?.height) || 720;
  lastCams = {
    leftIndex: cams.left?.index ?? 0,
    rightIndex: cams.right?.index ?? 1,
  };
  applyEyeLabels();

  setPill("pill-link", stream.connected ? "Live" : "Reconnecting", stream.connected ? "ok" : "warn");
  setPill("pill-rec", rec.recording ? "REC" : "Idle", rec.recording ? "live" : "");

  const btn = document.getElementById("btn-record");
  btn.textContent = rec.recording ? "Stop" : "Record";
  btn.classList.toggle("live", !!rec.recording);
  vr.setRecording(!!rec.recording);

  if (data.synthetic) {
    document.getElementById("overlay-msg").textContent = "Test pattern — plug in USB cameras on the Jetson";
  }
}

async function refreshRecordings() {
  const data = await api("/api/recordings");
  const list = document.getElementById("recording-list");
  const btn = document.getElementById("btn-recordings");
  const label = btn.querySelector(".btn-label");
  const title = `Saved recordings (${data.files.length}) ▾`;
  if (label) label.textContent = title;
  else btn.textContent = title;
  if (!data.files.length) {
    list.innerHTML = "<li>No recordings yet.</li>";
    return;
  }
  list.innerHTML = data.files
    .map((f) => {
      const mb = (f.size / 1048576).toFixed(1);
      const safeName = String(f.name)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/"/g, "&quot;");
      return `<li>
        <div class="rec-meta">
          <a href="/recordings/${encodeURIComponent(f.name)}">${safeName}</a>
          <span class="rec-size">${mb} MB</span>
        </div>
        <button type="button" class="btn-delete" data-name="${safeName}">Delete</button>
      </li>`;
    })
    .join("");
  list.querySelectorAll(".btn-delete").forEach((el) => {
    el.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const fileName = el.getAttribute("data-name");
      if (!fileName) return;
      if (!confirm(`Delete ${fileName}?`)) return;
      try {
        await api(`/api/recordings/${encodeURIComponent(fileName)}`, { method: "DELETE" });
        await refreshRecordings();
      } catch (err) {
        alert(err.message || err);
      }
    });
  });
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
  applyEyeLabels();
});
document.getElementById("btn-vr").addEventListener("click", async () => {
  try {
    await vr.enter();
  } catch (err) {
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
