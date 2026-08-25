function drawBitmap(canvas, bitmap, hfovDeg) {
  if (!canvas || !bitmap) return;
  if (canvas.width !== bitmap.width) canvas.width = bitmap.width;
  if (canvas.height !== bitmap.height) canvas.height = bitmap.height;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  if (typeof drawStreamGrid === "function") {
    drawStreamGrid(ctx, 0, 0, canvas.width, canvas.height, hfovDeg);
  }
}

function paintPlaceholder(canvas, label) {
  canvas.width = 960;
  canvas.height = 540;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#151920";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#3de0ff";
  ctx.font = "bold 72px sans-serif";
  ctx.fillText(label, 80, 280);
  ctx.fillStyle = "#9aa6c2";
  ctx.font = "32px sans-serif";
  ctx.fillText("Waiting for camera stream…", 80, 360);
}

function copyCanvas(dst, src) {
  if (!dst || !src || src.width < 320) return false;
  if (dst.width !== src.width) dst.width = src.width;
  if (dst.height !== src.height) dst.height = src.height;
  dst.getContext("2d").drawImage(src, 0, 0, dst.width, dst.height);
  return true;
}

class StereoVR {
  constructor(stream) {
    this.stream = stream;
    this.stereoEyes = true;
    this.active = false;
    this.root = document.getElementById("vr-root");
    this.recording = false;
    this.onToggleRecord = null;
    this.hfovDeg = 70;
    this.zoom = 1;
    this._zoomMin = 0.45;
    this._zoomMax = 2.8;
    this._dropY = -0.7;
  }

  setLensFov(deg) {
    const value = Number(deg);
    if (value > 0) this.hfovDeg = value;
  }

  setStereoEyes(on) {
    this.stereoEyes = on;
    this._applyMode();
  }

  setRecording(on) {
    this.recording = on;
    this._paintHud();
  }

  async enter() {
    if (typeof ensureXRWebGLLayer === "function") ensureXRWebGLLayer();
    if (!window.THREE) throw new Error("Three.js failed to load");
    if (!navigator.xr) throw new Error("WebXR is not available in this browser");
    const ok = await navigator.xr.isSessionSupported("immersive-vr");
    if (!ok) throw new Error("immersive-vr is not supported. Use HTTPS on Quest 3 Browser, or enable Immersive Web Emulator for this site.");

    this.root.classList.add("active");
    this.active = true;
    const THREE = window.THREE;
    const canvas = document.createElement("canvas");
    canvas.style.display = "block";
    const glAttrs = { antialias: true, alpha: false, xrCompatible: true, depth: true };
    const gl =
      canvas.getContext("webgl", glAttrs) || canvas.getContext("experimental-webgl", glAttrs);
    this.renderer = new THREE.WebGLRenderer({
      canvas: canvas,
      context: gl,
      antialias: true,
      alpha: false,
    });
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.xr.enabled = true;
    this.renderer.xr.setReferenceSpaceType("local");
    this.root.innerHTML = "";
    this.root.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x10141c);
    this.camera = new THREE.PerspectiveCamera(80, window.innerWidth / window.innerHeight, 0.02, 50);
    this.scene.add(this.camera);

    this.leftCanvas = document.createElement("canvas");
    this.rightCanvas = document.createElement("canvas");
    paintPlaceholder(this.leftCanvas, "LEFT");
    paintPlaceholder(this.rightCanvas, "RIGHT");
    copyCanvas(this.leftCanvas, document.getElementById("left-canvas"));
    copyCanvas(this.rightCanvas, document.getElementById("right-canvas"));

    this.leftTex = new THREE.CanvasTexture(this.leftCanvas);
    this.rightTex = new THREE.CanvasTexture(this.rightCanvas);
    this.leftTex.minFilter = THREE.LinearFilter;
    this.rightTex.minFilter = THREE.LinearFilter;
    this.leftTex.generateMipmaps = false;
    this.rightTex.generateMipmaps = false;

    this._dist = 3.8;
    this._planeW = 2.8;
    this._planeH = 1.575;
    const geo = new THREE.PlaneGeometry(this._planeW, this._planeH);
    const leftMat = new THREE.MeshBasicMaterial({ map: this.leftTex, depthTest: false, side: THREE.DoubleSide });
    const rightMat = new THREE.MeshBasicMaterial({ map: this.rightTex, depthTest: false, side: THREE.DoubleSide });

    this.screen = new THREE.Group();
    this.scene.add(this.screen);

    this.leftMesh = new THREE.Mesh(geo, leftMat);
    this.rightMesh = new THREE.Mesh(geo.clone(), rightMat);
    this.leftMesh.layers.set(1);
    this.rightMesh.layers.set(2);
    this.leftMesh.frustumCulled = false;
    this.rightMesh.frustumCulled = false;
    this.screen.add(this.leftMesh, this.rightMesh);

    this.dualLeft = new THREE.Mesh(geo.clone(), leftMat);
    this.dualRight = new THREE.Mesh(geo.clone(), rightMat);
    this.dualLeft.visible = false;
    this.dualRight.visible = false;
    this.dualLeft.frustumCulled = false;
    this.dualRight.frustumCulled = false;
    this.screen.add(this.dualLeft, this.dualRight);

    this.hudCanvas = document.createElement("canvas");
    this.hudCanvas.width = 1024;
    this.hudCanvas.height = 256;
    this.hudTex = new THREE.CanvasTexture(this.hudCanvas);
    this.hud = new THREE.Mesh(
      new THREE.PlaneGeometry(1.1, 0.28),
      new THREE.MeshBasicMaterial({ map: this.hudTex, transparent: true, depthTest: false })
    );
    this.hud.frustumCulled = false;
    this.screen.add(this.hud);

    this._worldPos = new THREE.Vector3();
    this._worldQuat = new THREE.Quaternion();
    this._tmp = new THREE.Vector3();
    this._right = new THREE.Vector3();
    this._up = new THREE.Vector3();
    this._forward = new THREE.Vector3();
    this._grabOffset = new THREE.Vector3();
    this._placed = false;
    this._grabbing = false;
    this._grabCtrl = null;

    this._bindOverlayControls();
    this.setZoom(this.zoom);

    this.camera.layers.enable(1);
    this.camera.layers.enable(2);
    this._controllers = [];
    for (let i = 0; i < 2; i++) {
      const ctrl = this.renderer.xr.getController(i);
      ctrl.addEventListener("squeezestart", () => this._beginGrab(ctrl));
      ctrl.addEventListener("squeezeend", () => this._endGrab(ctrl));
      if (i === 0) {
        ctrl.addEventListener("select", () => {
          if (this._grabbing) return;
          if (this.onToggleRecord) this.onToggleRecord();
        });
      }
      this.scene.add(ctrl);
      this._controllers.push(ctrl);
    }
    this.controller = this._controllers[0];
    this._applyMode();

    this.unsub = this.stream.onFrame((frame) => {
      drawBitmap(this.leftCanvas, frame.left, this.hfovDeg);
      drawBitmap(this.rightCanvas, frame.right, this.hfovDeg);
      this.leftTex.needsUpdate = true;
      this.rightTex.needsUpdate = true;
    });

    this._xrSession = null;
    this._exiting = false;

    const onXrFrame = (time, xrFrame) => {
      if (!this.renderer || !this.renderer.xr) return;
      if (this.renderer.xr.isPresenting) {
        try {
          this._pollZoom(time, xrFrame);
          if (this.renderer.xr.updateCamera) this.renderer.xr.updateCamera(this.camera);
          const xrCam = this.renderer.xr.getCamera();
          if (xrCam && xrCam.getWorldPosition) {
            if (!this._placed) this._placeInFront(xrCam);
            if (xrCam.cameras && xrCam.cameras.length >= 2) {
              xrCam.cameras[0].layers.enable(1);
              xrCam.cameras[1].layers.enable(2);
            }
          }
          this._updateGrab();
        } catch (err) {
          /* emulator can throw on the first XR frames */
        }
      }
      this.renderer.render(this.scene, this.camera);
    };

    try {
      const session = await navigator.xr.requestSession("immersive-vr", {
        optionalFeatures: ["local-floor"],
      });
      this._xrSession = session;
      session.addEventListener("end", () => this.exit());
      await this.renderer.xr.setSession(session);
      this.renderer.setAnimationLoop(onXrFrame);
    } catch (err) {
      this.exit();
      throw err;
    }
  }

  exit() {
    if (this._exiting) return;
    this._exiting = true;
    this.active = false;
    this.root.classList.remove("active");
    this._unbindOverlayControls();
    if (this.unsub) {
      this.unsub();
      this.unsub = null;
    }
    const renderer = this.renderer;
    this.renderer = null;
    const session = this._xrSession;
    this._xrSession = null;
    if (renderer) {
      try {
        renderer.setAnimationLoop(null);
      } catch (err) {
        /* IWE: cancelAnimationFrame on a null handle */
      }
      try {
        if (session && renderer.xr && renderer.xr.isPresenting) {
          session.end().catch(() => {});
        }
      } catch (err) {
        /* IWE session.end can re-enter Three.js stop() */
      }
      try {
        renderer.dispose();
      } catch (err) {
        /* ignore */
      }
    }
    this.root.innerHTML = "";
    this._exiting = false;
  }

  setZoom(value) {
    const next = Math.min(this._zoomMax, Math.max(this._zoomMin, value));
    this.zoom = next;
    if (!this.leftMesh) return;
    const s = next;
    this.leftMesh.scale.set(s, s, 1);
    this.rightMesh.scale.set(s, s, 1);
    this.dualLeft.scale.set(s, s, 1);
    this.dualRight.scale.set(s, s, 1);
    const w = this._planeW * s;
    const h = this._planeH * s;
    this.dualLeft.position.set(-(w / 2 + 0.25), 0, -0.05);
    this.dualRight.position.set(w / 2 + 0.25, 0, -0.05);
    this.hud.position.set(0, -(h / 2 + 0.22), 0.04);
    if (this._zoomLabel) this._zoomLabel.textContent = `${Math.round(s * 100)}%`;
    this._paintHud();
  }

  _placeInFront(cam) {
    if (!this.screen || !cam) return;
    if (cam.getWorldPosition) {
      cam.getWorldPosition(this._worldPos);
      cam.getWorldQuaternion(this._worldQuat);
    } else {
      this.camera.getWorldPosition(this._worldPos);
      this.camera.getWorldQuaternion(this._worldQuat);
    }
    this._forward.set(0, 0, -1).applyQuaternion(this._worldQuat);
    this.screen.position.copy(this._worldPos).addScaledVector(this._forward, this._dist);
    this.screen.position.y += this._dropY;
    this.screen.quaternion.copy(this._worldQuat);
    this._placed = true;
  }

  _beginGrab(ctrl) {
    if (!this.screen || !ctrl) return;
    this._grabbing = true;
    this._grabCtrl = ctrl;
    ctrl.getWorldPosition(this._tmp);
    this._grabOffset.copy(this.screen.position).sub(this._tmp);
  }

  _endGrab(ctrl) {
    if (this._grabCtrl === ctrl) {
      this._grabbing = false;
      this._grabCtrl = null;
    }
  }

  _updateGrab() {
    if (!this._grabbing || !this._grabCtrl || !this.screen) return;
    this._grabCtrl.getWorldPosition(this._tmp);
    this.screen.position.copy(this._tmp).add(this._grabOffset);
  }

  _nudgeScreen(dx, dy, dz) {
    if (!this.screen) return;
    this.camera.getWorldQuaternion(this._worldQuat);
    this._right.set(1, 0, 0).applyQuaternion(this._worldQuat);
    this._up.set(0, 1, 0).applyQuaternion(this._worldQuat);
    this._forward.set(0, 0, -1).applyQuaternion(this._worldQuat);
    this.screen.position.addScaledVector(this._right, dx);
    this.screen.position.addScaledVector(this._up, dy);
    this.screen.position.addScaledVector(this._forward, dz);
  }

  _bindOverlayControls() {
    const bar = document.createElement("div");
    bar.className = "vr-zoom-bar";
    bar.innerHTML =
      '<button type="button" data-zoom="out" aria-label="Zoom out">−</button>' +
      '<span class="vr-zoom-label">100%</span>' +
      '<button type="button" data-zoom="in" aria-label="Zoom in">+</button>' +
      '<button type="button" data-zoom="reset" class="vr-zoom-reset">Reset zoom</button>' +
      '<button type="button" data-place="reset" class="vr-zoom-reset">Reset place</button>';
    this.root.appendChild(bar);
    this._zoomBar = bar;
    this._zoomLabel = bar.querySelector(".vr-zoom-label");

    const step = (factor) => this.setZoom(this.zoom * factor);
    const startHold = (factor) => {
      step(factor);
      this._stopZoomHold();
      this._zoomHold = setInterval(() => step(factor), 90);
    };
    bar.addEventListener("pointerdown", (event) => {
      const btn = event.target.closest("[data-zoom], [data-place]");
      if (!btn) return;
      event.preventDefault();
      event.stopPropagation();
      if (btn.getAttribute("data-place") === "reset") {
        const xrCam = this.renderer && this.renderer.xr && this.renderer.xr.getCamera();
        this._placeInFront(xrCam || this.camera);
        return;
      }
      const action = btn.getAttribute("data-zoom");
      if (action === "reset") this.setZoom(1);
      else startHold(action === "in" ? 1.08 : 1 / 1.08);
    });
    const stopHold = () => this._stopZoomHold();
    bar.addEventListener("pointerup", stopHold);
    bar.addEventListener("pointerleave", stopHold);
    bar.addEventListener("pointercancel", stopHold);

    this._onWheel = (event) => {
      if (event.target.closest(".vr-zoom-bar")) return;
      event.preventDefault();
      if (event.shiftKey) {
        this._nudgeScreen(0, 0, event.deltaY > 0 ? -0.12 : 0.12);
      } else {
        this.setZoom(this.zoom * (event.deltaY > 0 ? 0.92 : 1.08));
      }
    };
    this._onKey = (event) => {
      if (!this.active) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        this.setZoom(this.zoom * 1.1);
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        this.setZoom(this.zoom / 1.1);
      } else if (event.key === "0") {
        event.preventDefault();
        this.setZoom(1);
      } else if (event.key === "Home") {
        event.preventDefault();
        const xrCam = this.renderer && this.renderer.xr && this.renderer.xr.getCamera();
        this._placeInFront(xrCam || this.camera);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        this._nudgeScreen(event.shiftKey ? 0 : -0.08, 0, event.shiftKey ? -0.08 : 0);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        this._nudgeScreen(event.shiftKey ? 0 : 0.08, 0, event.shiftKey ? 0.08 : 0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this._nudgeScreen(0, 0.08, 0);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        this._nudgeScreen(0, -0.08, 0);
      }
    };
    this._onPtrDown = (event) => {
      if (event.button !== 0) return;
      if (event.target.closest(".vr-zoom-bar")) return;
      this._mouseDrag = true;
      this._ptrShift = event.shiftKey;
      try {
        event.target.setPointerCapture(event.pointerId);
      } catch (err) {
        /* ignore */
      }
    };
    this._onPtrMove = (event) => {
      if (!this._mouseDrag || !this.screen) return;
      const k = 0.0035 * this._dist;
      if (event.shiftKey || this._ptrShift) this._nudgeScreen(0, 0, -event.movementY * k);
      else this._nudgeScreen(event.movementX * k, -event.movementY * k, 0);
    };
    this._onPtrUp = () => {
      this._mouseDrag = false;
    };
    this.root.addEventListener("wheel", this._onWheel, { passive: false });
    this.root.addEventListener("pointerdown", this._onPtrDown);
    this.root.addEventListener("pointermove", this._onPtrMove);
    this.root.addEventListener("pointerup", this._onPtrUp);
    this.root.addEventListener("pointercancel", this._onPtrUp);
    window.addEventListener("keydown", this._onKey);
  }

  _stopZoomHold() {
    if (this._zoomHold) {
      clearInterval(this._zoomHold);
      this._zoomHold = null;
    }
  }

  _unbindOverlayControls() {
    this._stopZoomHold();
    this._mouseDrag = false;
    this._grabbing = false;
    this._grabCtrl = null;
    if (this._onWheel) this.root.removeEventListener("wheel", this._onWheel);
    if (this._onPtrDown) this.root.removeEventListener("pointerdown", this._onPtrDown);
    if (this._onPtrMove) this.root.removeEventListener("pointermove", this._onPtrMove);
    if (this._onPtrUp) {
      this.root.removeEventListener("pointerup", this._onPtrUp);
      this.root.removeEventListener("pointercancel", this._onPtrUp);
    }
    if (this._onKey) window.removeEventListener("keydown", this._onKey);
    this._onWheel = null;
    this._onPtrDown = null;
    this._onPtrMove = null;
    this._onPtrUp = null;
    this._onKey = null;
    this._zoomBar = null;
    this._zoomLabel = null;
  }

  _pollZoom(time, xrFrame) {
    if (this._grabbing) return;
    if (!xrFrame || !xrFrame.session) return;
    const dt = this._lastZoomT ? Math.min(0.05, (time - this._lastZoomT) / 1000) : 0.016;
    this._lastZoomT = time;
    let axisY = 0;
    const sources = xrFrame.session.inputSources || [];
    for (let i = 0; i < sources.length; i++) {
      const pad = sources[i].gamepad;
      if (!pad || !pad.axes || !pad.axes.length) continue;
      const y3 = pad.axes.length > 3 ? pad.axes[3] : 0;
      const y1 = pad.axes.length > 1 ? pad.axes[1] : 0;
      const y = Math.abs(y3) >= Math.abs(y1) ? y3 : y1;
      if (Math.abs(y) > Math.abs(axisY)) axisY = y;
    }
    if (Math.abs(axisY) < 0.18) return;
    this.setZoom(this.zoom * Math.exp(-axisY * 1.35 * dt));
  }

  _applyMode() {
    if (!this.leftMesh) return;
    const stereo = this.stereoEyes;
    this.leftMesh.visible = stereo;
    this.rightMesh.visible = stereo;
    this.dualLeft.visible = !stereo;
    this.dualRight.visible = !stereo;
  }

  _paintHud() {
    if (!this.hudCanvas) return;
    const ctx = this.hudCanvas.getContext("2d");
    ctx.clearRect(0, 0, 1024, 256);
    ctx.fillStyle = "rgba(8,12,20,0.72)";
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(20, 20, 984, 216, 24);
      ctx.fill();
    } else {
      ctx.fillRect(20, 20, 984, 216);
    }
    ctx.fillStyle = this.recording ? "#ff3b5c" : "#3de0ff";
    ctx.font = "700 42px sans-serif";
    ctx.fillText(this.recording ? "RECORDING  ·  trigger to stop" : "Grip: drag screen   ·   trigger: record", 48, 110);
    ctx.fillStyle = "#c5d0e8";
    ctx.font = "600 40px sans-serif";
    ctx.fillText(`Zoom ${Math.round((this.zoom || 1) * 100)}%  ·  stick / drag / arrows`, 56, 180);
    if (this.hudTex) this.hudTex.needsUpdate = true;
  }
}
