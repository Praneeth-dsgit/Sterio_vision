const stream = new StereoStream();
const vr = new StereoVR(stream);
const leftCanvas = document.getElementById("left-canvas");
const rightCanvas = document.getElementById("right-canvas");

function setPill(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = "pill " + (cls || "");
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function fillSelect(select, cameras, current) {
  select.innerHTML = "";
  const seen = new Set();
  cameras.forEach((cam) => {
    const opt = document.createElement("option");
    opt.value = cam.index;
    opt.textContent = cam.name;
    select.appendChild(opt);
    seen.add(cam.index);
  });
  [0, 1, 2, 3, 4, 5].forEach((idx) => {
    if (seen.has(idx)) return;
    const opt = document.createElement("option");
    opt.value = idx;
    opt.textContent = `Index ${idx}`;
    select.appendChild(opt);
  });
  select.value = String(current);
}

function renderStatus(data) {
  const rec = data.record || {};
  const st = data.stream || {};
  const cams = data.cameras || {};
  document.getElementById("left-cam-label").textContent = `cam ${cams.left?.index ?? 0}`;
  document.getElementById("right-cam-label").textContent = `cam ${cams.right?.index ?? 1}`;
  document.getElementById("stat-fps").textContent = `${st.preview_fps ?? 0} fps`;
  document.getElementById("stat-skew").textContent = `${st.skew_ms ?? 0} ms`;
  document.getElementById("stat-clients").textContent = String(st.clients ?? 0);
  document.getElementById("stat-rec").textContent = rec.recording ? `${rec.elapsed_sec}s` : "off";

  setPill("pill-link", stream.connected ? "Live" : "Reconnecting", stream.connected ? "ok" : "warn");
  const skew = st.skew_ms ?? 99;
  setPill("pill-sync", `Sync ${skew} ms`, skew <= 20 ? "ok" : "warn");
  setPill("pill-rec", rec.recording ? "REC" : "Idle", rec.recording ? "live" : "");

  const btn = document.getElementById("btn-record");
  btn.textContent = rec.recording ? "Stop recording" : "Start recording";
  btn.classList.toggle("live", !!rec.recording);
  vr.setRecording(!!rec.recording);

  if (data.synthetic) {
    document.getElementById("overlay-msg").textContent = "Test pattern — plug in USB cameras and Apply";
  }

  const cfg = data.config || {};
  const form = document.getElementById("settings-form");
  if (cfg.cameras) {
    fillSelect(form.left_index, cams.available || [], cfg.cameras.left_index);
    fillSelect(form.right_index, cams.available || [], cfg.cameras.right_index);
    form.width.value = cfg.cameras.width;
    form.height.value = cfg.cameras.height;
    form.fps.value = cfg.cameras.fps;
  }
  if (cfg.stream) {
    form.jpeg_quality.value = cfg.stream.jpeg_quality;
    form.max_width.value = cfg.stream.max_width;
    form.max_skew_ms.value = cfg.stream.max_skew_ms;
  }
  if (cfg.record) document.getElementById("auto-record").checked = !!cfg.record.auto_start;

  const box = document.getElementById("url-box");
  const httpsUrl = (data.urls || []).find((u) => u.https && !String(u.ip).startsWith("127.")) || (data.urls || []).find((u) => u.https);
  box.innerHTML = (data.urls || [])
    .map((u) => `<div>Quest 3 (HTTPS): <code>${u.https}</code><br>Desktop test: <code>${u.http}</code></div>`)
    .join("");
  if (!location.protocol.startsWith("https")) {
    document.getElementById("vr-hint").textContent =
      "WebXR needs HTTPS. On Quest 3 open " + (httpsUrl ? httpsUrl.https : "the HTTPS URL") + " and accept the certificate warning.";
  }
}

async function refreshRecordings() {
  const data = await api("/api/recordings");
  const list = document.getElementById("recording-list");
  if (!data.files.length) {
    list.innerHTML = "<li>No recordings yet. They appear here automatically.</li>";
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
  drawBitmap(leftCanvas, frame.left);
  drawBitmap(rightCanvas, frame.right);
  document.getElementById("stat-skew").textContent = `${frame.skewMs.toFixed(1)} ms`;
});

vr.onToggleRecord = toggleRecord;

document.getElementById("btn-record").addEventListener("click", toggleRecord);
document.getElementById("btn-swap").addEventListener("click", () => {
  stream.swapEyes = !stream.swapEyes;
});
document.getElementById("mode-stereo").addEventListener("change", (e) => vr.setStereoEyes(e.target.checked));
document.getElementById("auto-record").addEventListener("change", async (e) => {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ record: { auto_start: e.target.checked } }),
  });
});
document.getElementById("btn-vr").addEventListener("click", async () => {
  try {
    await vr.enter();
  } catch (err) {
    document.getElementById("vr-hint").textContent = String(err.message || err);
    alert(err.message || err);
  }
});
document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      cameras: {
        left_index: Number(form.left_index.value),
        right_index: Number(form.right_index.value),
        width: Number(form.width.value),
        height: Number(form.height.value),
        fps: Number(form.fps.value),
      },
      stream: {
        jpeg_quality: Number(form.jpeg_quality.value),
        max_width: Number(form.max_width.value),
        max_skew_ms: Number(form.max_skew_ms.value),
      },
    }),
  });
  renderStatus(await api("/api/status"));
});

stream.connect();
api("/api/status").then(renderStatus).catch(console.error);
refreshRecordings();
setInterval(() => api("/api/status").then(renderStatus).catch(() => {}), 2000);
setInterval(() => refreshRecordings().catch(() => {}), 8000);
