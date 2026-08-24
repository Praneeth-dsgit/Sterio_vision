(function (global) {
  function Viewport(x, y, width, height) {
    this.x = x;
    this.y = y;
    this.width = width;
    this.height = height;
  }

  function XRWebGLLayerShim(session, context, layerInit) {
    layerInit = layerInit || {};
    this.session = session;
    this.context = context;
    this.antialias = layerInit.antialias !== false;
    this.ignoreDepthValues = Boolean(layerInit.ignoreDepthValues);
    this.fixedFoveation = layerInit.fixedFoveation || 0;
    this.framebuffer = null;
    try {
      this.framebuffer = context.getParameter(context.FRAMEBUFFER_BINDING);
    } catch (err) {
      this.framebuffer = null;
    }
  }

  Object.defineProperty(XRWebGLLayerShim.prototype, "framebufferWidth", {
    get: function () {
      return this.context.drawingBufferWidth;
    },
  });
  Object.defineProperty(XRWebGLLayerShim.prototype, "framebufferHeight", {
    get: function () {
      return this.context.drawingBufferHeight;
    },
  });
  XRWebGLLayerShim.prototype.getViewport = function (view) {
    if (view && view.viewport) return view.viewport;
    const width = this.framebufferWidth;
    const height = this.framebufferHeight;
    const eye = view && view.eye;
    if (eye === "right") return new Viewport(width / 2, 0, width / 2, height);
    if (eye === "left") return new Viewport(0, 0, width / 2, height);
    return new Viewport(0, 0, width, height);
  };
  XRWebGLLayerShim.getNativeFramebufferScaleFactor = function () {
    return 1;
  };

  global.ensureXRWebGLLayer = function () {
    if (global.XRWebGLLayer && global.XRWebGLLayer.__stereoShim) return;
    const NativeLayer = global.XRWebGLLayer;

    function XRWebGLLayer(session, context, layerInit) {
      if (NativeLayer && NativeLayer !== XRWebGLLayer) {
        try {
          return new NativeLayer(session, context, layerInit);
        } catch (err) {
          /* native ctor rejects Immersive Web Emulator's polyfill XRSession */
        }
      }
      const layer = Object.create(XRWebGLLayerShim.prototype);
      XRWebGLLayerShim.call(layer, session, context, layerInit);
      return layer;
    }

    XRWebGLLayer.prototype = XRWebGLLayerShim.prototype;
    XRWebGLLayer.getNativeFramebufferScaleFactor =
      (NativeLayer && NativeLayer.getNativeFramebufferScaleFactor) ||
      XRWebGLLayerShim.getNativeFramebufferScaleFactor;
    XRWebGLLayer.__stereoShim = true;
    global.XRWebGLLayer = XRWebGLLayer;
  };

  global.ensureXRWebGLLayer();
})(typeof window !== "undefined" ? window : globalThis);
