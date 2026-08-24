function drawBitmap(canvas, bitmap) {
  if (!canvas || !bitmap) return;
  if (canvas.width !== bitmap.width) canvas.width = bitmap.width;
  if (canvas.height !== bitmap.height) canvas.height = bitmap.height;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
}

class StereoVR {
  constructor(stream) {
    this.stream = stream;
    this.stereoEyes = true;
    this.active = false;
    this.root = document.getElementById("vr-root");
    this.leftTex = null;
    this.rightTex = null;
    this.leftMesh = null;
    this.rightMesh = null;
    this.dualLeft = null;
    this.dualRight = null;
    this.hud = null;
    this.recording = false;
    this.onToggleRecord = null;
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
    if (!window.THREE) throw new Error("Three.js failed to load");
    if (!navigator.xr) throw new Error("WebXR is not available in this browser");
    const ok = await navigator.xr.isSessionSupported("immersive-vr");
    if (!ok) throw new Error("immersive-vr is not supported. Use HTTPS on Quest 3 Browser.");

    this.root.classList.add("active");
    this.active = true;
    const THREE = window.THREE;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.xr.enabled = true;
    this.root.innerHTML = "";
    this.root.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x000000);
    this.camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.05, 100);
    this.camera.position.set(0, 1.4, 0);

    this.leftCanvas = document.createElement("canvas");
    this.rightCanvas = document.createElement("canvas");
    this.leftTex = new THREE.CanvasTexture(this.leftCanvas);
    this.rightTex = new THREE.CanvasTexture(this.rightCanvas);
    try {
      const space = THREE.SRGBColorSpace || THREE.sRGBEncoding;
      this.leftTex.colorSpace = space;
      this.rightTex.colorSpace = space;
    } catch (err) {
      /* older three.js builds */
    }

    const geo = new THREE.PlaneGeometry(2.4, 1.35);
    const leftMat = new THREE.MeshBasicMaterial({ map: this.leftTex });
    const rightMat = new THREE.MeshBasicMaterial({ map: this.rightTex });
    this.leftMesh = new THREE.Mesh(geo, leftMat);
    this.rightMesh = new THREE.Mesh(geo.clone(), rightMat);
    this.leftMesh.position.set(0, 1.4, -1.7);
    this.rightMesh.position.set(0, 1.4, -1.7);
    this.leftMesh.layers.set(1);
    this.rightMesh.layers.set(2);
    this.scene.add(this.leftMesh, this.rightMesh);

    this.dualLeft = new THREE.Mesh(geo.clone(), leftMat);
    this.dualRight = new THREE.Mesh(geo.clone(), rightMat);
    this.dualLeft.position.set(-1.3, 1.4, -2.4);
    this.dualRight.position.set(1.3, 1.4, -2.4);
    this.dualLeft.visible = false;
    this.dualRight.visible = false;
    this.scene.add(this.dualLeft, this.dualRight);

    this.hudCanvas = document.createElement("canvas");
    this.hudCanvas.width = 1024;
    this.hudCanvas.height = 256;
    this.hudTex = new THREE.CanvasTexture(this.hudCanvas);
    const hudMat = new THREE.MeshBasicMaterial({ map: this.hudTex, transparent: true });
    this.hud = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 0.3), hudMat);
    this.hud.position.set(0, 0.85, -1.35);
    this.scene.add(this.hud);
    this._paintHud();

    this.controller = this.renderer.xr.getController(0);
    this.controller.addEventListener("select", () => {
      if (this.onToggleRecord) this.onToggleRecord();
    });
    this.scene.add(this.controller);
    this._applyMode();

    try {
      const session = await navigator.xr.requestSession("immersive-vr", {
        optionalFeatures: ["local-floor", "bounded-floor", "hand-tracking"],
      });
      await this.renderer.xr.setSession(session);
      session.addEventListener("end", () => this.exit());
    } catch (err) {
      this.exit();
      throw err;
    }

    this.unsub = this.stream.onFrame((frame) => {
      drawBitmap(this.leftCanvas, frame.left);
      drawBitmap(this.rightCanvas, frame.right);
      this.leftTex.needsUpdate = true;
      this.rightTex.needsUpdate = true;
    });

    this.renderer.setAnimationLoop(() => {
      const xrCam = this.renderer.xr.getCamera();
      if (xrCam.cameras && xrCam.cameras.length >= 2) {
        xrCam.cameras[0].layers.enable(1);
        xrCam.cameras[1].layers.enable(2);
      }
      this.renderer.render(this.scene, this.camera);
    });
  }

  exit() {
    this.active = false;
    this.root.classList.remove("active");
    if (this.unsub) this.unsub();
    if (this.renderer) {
      this.renderer.setAnimationLoop(null);
      const session = this.renderer.xr.getSession();
      if (session) session.end().catch(() => {});
      this.renderer.dispose();
    }
    this.root.innerHTML = "";
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
    ctx.font = "700 72px Segoe UI, sans-serif";
    ctx.fillText(this.recording ? "RECORDING  ·  trigger to stop" : "Trigger: start recording", 60, 150);
    if (this.hudTex) this.hudTex.needsUpdate = true;
  }
}
