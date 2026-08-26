const stream = new StereoStream();
const vr = new StereoVR(stream);
const liveAudio = new LiveAudioPlayer();
const leftCanvas = document.getElementById("left-canvas");
const rightCanvas = document.getElementById("right-canvas");
let lensHfov = 70;
let capW = 1280;
let capH = 720;
let lastCams = { leftIndex: 0, rightIndex: 1 };
let audioEnabled = true;
let camerasEnabled = true;

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

function setIconToggle(btn, on, titleOn, titleOff) {
  if (!btn) return;
  btn.classList.toggle("on", !!on);
  btn.classList.toggle("off", !on);
  btn.classList.toggle("muted", btn.id === "btn-mute" && !on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.title = on ? titleOn : titleOff;
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

  if (typeof data.cameras_enabled === "boolean") camerasEnabled = data.cameras_enabled;
  setPill(
    "pill-link",
    !camerasEnabled ? "Cams Off" : stream.connected ? "Live" : "Reconnecting",
    !camerasEnabled ? "warn" : stream.connected ? "ok" : "warn"
  );
  setPill("pill-rec", rec.recording ? "REC" : "Idle", rec.recording ? "live" : "");

  const cfgAudio = data.config?.record?.audio;
  if (typeof cfgAudio === "boolean") audioEnabled = cfgAudio;
  else if (typeof rec.audio === "boolean" && !rec.recording) audioEnabled = !!rec.audio;
  const live = data.live_audio || {};
  setPill(
    "pill-mic",
    audioEnabled ? (live.live_audio || liveAudio.connected ? "Mic Live" : "Mic") : "Muted",
    audioEnabled ? "ok" : "muted"
  );
  liveAudio.setMuted(!audioEnabled);

  setIconToggle(
    document.getElementById("btn-mute"),
    audioEnabled,
    "Mute mic",
    "Unmute mic"
  );
  setIconToggle(
    document.getElementById("btn-cam"),
    camerasEnabled,
    "Turn cameras off",
    "Turn cameras on"
  );

  const btn = document.getElementById("btn-record");
  btn.textContent = rec.recording ? "Stop" : "Record";
  btn.classList.toggle("live", !!rec.recording);
  btn.disabled = !camerasEnabled && !rec.recording;
  vr.setRecording(!!rec.recording);

  const filePath = rec.file || "";
  activeRecordingName = rec.recording && filePath ? filePath.split(/[/\\]/).pop() : null;
  const overlay = document.getElementById("overlay-msg");
  if (rec.error) {
    overlay.style.display = "";
    overlay.textContent = `Record error: ${rec.error}`;
  } else if (!camerasEnabled) {
    overlay.style.display = "";
    overlay.textContent = "Cameras off — tap the camera icon to turn them back on";
  } else if (data.synthetic) {
    overlay.style.display = "";
    overlay.textContent =
      "Camera disconnected — reconnecting… (plug USB back in; feed returns automatically)";
  }
}

function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

let activeRecordingName = null;

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
      const safeName = String(f.name)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/"/g, "&quot;");
      const isActive = activeRecordingName && f.name === activeRecordingName;
      const sizeLabel = isActive
        ? (f.size > 0 ? `${formatSize(f.size)} · recording…` : "recording…")
        : formatSize(f.size);
      return `<li>
        <div class="rec-meta">
          <a href="/recordings/${encodeURIComponent(f.name)}">${safeName}</a>
          <span class="rec-size">${sizeLabel}</span>
        </div>
        <button type="button" class="btn-delete" data-name="${safeName}" ${isActive ? "disabled" : ""}>Delete</button>
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
  if (!vr.active) refreshRecordings();
}

async function toggleMute() {
  const next = !audioEnabled;
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ record: { audio: next } }),
  });
  audioEnabled = next;
  liveAudio.setMuted(!next);
  if (next) await liveAudio.resume();
  renderStatus(await api("/api/status"));
}

async function toggleCameras() {
  const next = !camerasEnabled;
  const res = await api("/api/cameras/enable", {
    method: "POST",
    body: JSON.stringify({ enabled: next }),
  });
  camerasEnabled = !!res.cameras_enabled;
  if (!camerasEnabled) {
    const ctxL = leftCanvas.getContext("2d");
    const ctxR = rightCanvas.getContext("2d");
    if (ctxL) {
      ctxL.fillStyle = "#05070c";
      ctxL.fillRect(0, 0, leftCanvas.width || 640, leftCanvas.height || 360);
    }
    if (ctxR) {
      ctxR.fillStyle = "#05070c";
      ctxR.fillRect(0, 0, rightCanvas.width || 640, rightCanvas.height || 360);
    }
  }
  renderStatus(await api("/api/status"));
}

function unlockAudio() {
  liveAudio.resume().catch(() => {});
}

stream.onFrame((frame) => {
  if (!camerasEnabled) return;
  document.getElementById("overlay-msg").style.display = "none";
  drawPreview(leftCanvas, frame.left);
  drawPreview(rightCanvas, frame.right);
});

vr.onToggleRecord = toggleRecord;

document.getElementById("btn-record").addEventListener("click", () => {
  unlockAudio();
  toggleRecord();
});
document.getElementById("btn-mute").addEventListener("click", () => {
  unlockAudio();
  toggleMute().catch((err) => alert(err.message || err));
});
document.getElementById("btn-cam").addEventListener("click", () => {
  unlockAudio();
  toggleCameras().catch((err) => alert(err.message || err));
});
document.getElementById("btn-swap").addEventListener("click", () => {
  stream.swapEyes = !stream.swapEyes;
  applyEyeLabels();
});
document.getElementById("btn-vr").addEventListener("click", async () => {
  try {
    await liveAudio.resume();
    await vr.enter();
  } catch (err) {
    alert(err.message || err);
  }
});

const recBtn = document.getElementById("btn-recordings");
const recPanel = document.getElementById("recordings-panel");
recBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  unlockAudio();
  recPanel.hidden = !recPanel.hidden;
});
document.addEventListener("click", (event) => {
  unlockAudio();
  if (!recPanel.hidden && !event.target.closest(".recordings-menu")) recPanel.hidden = true;
});

stream.connect();
liveAudio.connect();
api("/api/status").then(renderStatus).catch(console.error);
refreshRecordings();
setInterval(async () => {
  try {
    const data = await api("/api/status");
    renderStatus(data);
    if (data.record?.recording && !vr.active) await refreshRecordings();
  } catch (_) {}
}, 2000);
setInterval(() => {
  if (!vr.active) refreshRecordings().catch(() => {});
}, 8000);
