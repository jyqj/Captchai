
  // ---- WebGL: keep the WHOLE capability surface coherent with the spoofed
  // GPU, not just UNMASKED vendor/renderer. A GPU-less headless host falls
  // back to SwiftShader, whose param values + extension list read as software
  // rendering and contradict the discrete-GPU strings the layer spoofs; align
  // the high-signal params + extensions so the surface is internally coherent.
  const _WEBKIT_GL_PARAMS = {
    3379: 16384, 34024: 16384, 34076: 16384, 36347: 4096,
    36349: 1024, 36348: 30, 34930: 16, 35660: 16, 35661: 32, 34921: 16
  };
  const _WEBKIT_GL_EXTS = %s;
  const _patchGL = (proto) => {
    if (!proto) return;
    const getParameter = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return _WEBGL_VENDOR;   // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return _WEBGL_RENDERER; // UNMASKED_RENDERER_WEBGL
      if (p === 3386) return new Int32Array([32767, 32767]);  // MAX_VIEWPORT_DIMS
      if (p === 33901) return new Float32Array([1, 1024]);    // ALIASED_POINT_SIZE_RANGE
      if (p === 33902) return new Float32Array([1, 1]);       // ALIASED_LINE_WIDTH_RANGE
      if (Object.prototype.hasOwnProperty.call(_WEBKIT_GL_PARAMS, p)) return _WEBKIT_GL_PARAMS[p];
      return getParameter.call(this, p);
    };
    const _getExts = proto.getSupportedExtensions;
    if (_getExts) { proto.getSupportedExtensions = function () { return _WEBKIT_GL_EXTS.slice(); }; }
  };
  try { _patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { _patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}

  // ---- window.screen coherence: a headless host frequently reports 0/odd
  // screen dims that contradict the spoofed viewport + platform.
  try {
    const _screenDefs = {width: %d, height: %d, availWidth: %d, availHeight: %d, colorDepth: 24, pixelDepth: 24};
    for (const _k in _screenDefs) {
      try { Object.defineProperty(window.screen, _k, {get: ((v) => () => v)(_screenDefs[_k]), configurable: true}); } catch (e) {}
    }
  } catch (e) {}

  // ---- navigator.connection: present on real Chrome; its absence is a
  // headless tell. Expose a plausible 4G effective type.
  try {
    const _conn = {effectiveType: '4g', rtt: 50, downlink: 10, saveData: false,
      onchange: null, addEventListener: function () {}, removeEventListener: function () {}};
    Object.defineProperty(navigator, 'connection', {get: () => _conn, configurable: true});
  } catch (e) {}

  // ---- AudioContext: idempotent, inaudible per-fingerprint perturbation so
  // the audio fingerprint is stable-but-unique (mirrors the canvas approach).
  // A WeakSet guards each backing buffer so repeated reads don't drift.
  try {
    const _AOFF = %d;
    const _audioSeen = new WeakSet();
    const _gcd = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function (ch) {
      const d = _gcd.call(this, ch);
      if (!_audioSeen.has(d)) {
        _audioSeen.add(d);
        for (let i = 0; i < d.length; i += 971) { d[i] = d[i] + ((_AOFF - 32) * 1e-7); }
      }
      return d;
    };
    if (window.AnalyserNode && AnalyserNode.prototype.getFloatFrequencyData) {
      const _ffd = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = function (arr) {
        _ffd.call(this, arr);
        for (let i = 0; i < arr.length; i += 131) { arr[i] = arr[i] + ((_AOFF - 32) * 1e-5); }
      };
    }
  } catch (e) {}

  // ---- Canvas: idempotent sparse LSB perturbation applied to BOTH read paths
  // (getImageData AND toDataURL/toBlob). The previous toDataURL override was a
  // no-op, so the most common canvas-fingerprint path saw an UN-noised canvas.
  // The step is prime (not a divisor of common widths) so the noise never
  // aligns to a column; the offset is per-fingerprint so the hash is stable
  // for one identity but uncorrelated across identities. LSB-forcing (not
  // add-and-wrap) is idempotent so repeated reads return the SAME bytes.
  try {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _toBlob = HTMLCanvasElement.prototype.toBlob;
    const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
    const _step = %d;
    const _offset = %d;
    const _noisify = (data) => {
      for (let i = 0; i < data.length; i += _step) { data[i] = (data[i] & 0xfe) | ((i + _offset) & 1); }
      return data;
    };
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {
      const img = _getImageData.call(this, x, y, w, h);
      _noisify(img.data);
      return img;
    };
    const _applyCanvasNoise = (canvas) => {
      try {
        const ctx = canvas.getContext && canvas.getContext('2d');
        if (!ctx || !canvas.width || !canvas.height) return;
        const img = _getImageData.call(ctx, 0, 0, canvas.width, canvas.height);
        _noisify(img.data);
        ctx.putImageData(img, 0, 0);
      } catch (e) {}
    };
    HTMLCanvasElement.prototype.toDataURL = function (...args) { _applyCanvasNoise(this); return _toDataURL.apply(this, args); };
    if (_toBlob) { HTMLCanvasElement.prototype.toBlob = function (...args) { _applyCanvasNoise(this); return _toBlob.apply(this, args); }; }
  } catch (e) {}
