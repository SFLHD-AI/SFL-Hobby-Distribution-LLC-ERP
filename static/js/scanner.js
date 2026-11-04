/*
 * SFL ERP System — camera barcode scanner.
 *
 * Adds a 📷 button next to every element that opts in, and fills it from the
 * phone camera. Works on iPhone Safari (iOS 17+ exposes BarcodeDetector) and
 * on everything else through a lazily loaded ZXing fallback. Needs HTTPS
 * (browsers only allow the camera on secure pages) — Railway, nginx+certbot
 * and localhost all qualify.
 *
 *   <input data-scan>                         fill the field
 *   <input data-scan data-scan-submit>        fill, then submit the form
 *   <input data-scan data-scan-enter>         fill, then press Enter (keyboard-wedge pages)
 *   <input data-scan data-scan-continuous>    stay open and keep scanning (pick screen)
 *   <select data-scan-select>                 pick the <option> whose data-sku / data-upc
 *                                             (or leading text) matches, then focus the
 *                                             element named by data-scan-next
 *
 * Buttons are inserted automatically on page load; call window.SFLScanner.init(root)
 * again for content added later.
 */
(function () {
  "use strict";
  const ZXING_URL = "https://unpkg.com/@zxing/library@0.21.3/umd/index.min.js";
  const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "itf", "qr_code", "data_matrix"];

  const supportsCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

  // ---- overlay -----------------------------------------------------------
  let overlay, video, hint, stream, stopFlag, zxingReader, lastCode = "", lastAt = 0;

  function buildOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "sflScanOverlay";
    overlay.style.cssText = "position:fixed;inset:0;z-index:9999;background:#000;display:none;flex-direction:column;";
    overlay.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;color:#fff;font:600 16px system-ui">' +
      '<span>Scan a barcode</span>' +
      '<button type="button" id="sflScanClose" style="background:#334155;color:#fff;border:0;border-radius:10px;padding:10px 16px;font:600 15px system-ui">Close</button></div>' +
      '<div style="position:relative;flex:1;overflow:hidden">' +
      '<video id="sflScanVideo" playsinline autoplay muted style="width:100%;height:100%;object-fit:cover"></video>' +
      '<div style="position:absolute;left:8%;right:8%;top:35%;height:30%;border:3px solid #22c55e;border-radius:14px;box-shadow:0 0 0 2000px rgba(0,0,0,.35)"></div></div>' +
      '<div id="sflScanHint" style="padding:12px 16px 20px;color:#cbd5e1;font:14px system-ui;text-align:center;min-height:48px">Point the camera at the barcode</div>';
    document.body.appendChild(overlay);
    video = overlay.querySelector("#sflScanVideo");
    hint = overlay.querySelector("#sflScanHint");
    overlay.querySelector("#sflScanClose").addEventListener("click", close);
    return overlay;
  }

  function setHint(text, ok) {
    hint.textContent = text;
    hint.style.color = ok ? "#86efac" : "#cbd5e1";
  }

  function beep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.frequency.value = 1200; gain.gain.value = 0.15;
      osc.start(); osc.stop(ctx.currentTime + 0.08);
    } catch (e) { /* no audio */ }
    if (navigator.vibrate) navigator.vibrate(60);
  }

  async function open(onCode, opts) {
    opts = opts || {};
    buildOverlay();
    if (!supportsCamera) {
      alert("This browser can't use the camera. Open the page over https:// on your phone, or type the code.");
      return;
    }
    overlay.style.display = "flex";
    stopFlag = false;
    setHint("Starting camera…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    } catch (e) {
      setHint("Camera blocked. Allow camera access for this site in your browser settings, then try again.");
      return;
    }
    video.srcObject = stream;
    try { await video.play(); } catch (e) { /* autoplay quirks */ }
    setHint(opts.continuous ? "Scanning… each code is sent as soon as it's read" : "Point the camera at the barcode");

    const handle = (code) => {
      code = (code || "").trim();
      if (!code) return;
      const now = Date.now();
      if (code === lastCode && now - lastAt < 2500) return;   // same label still in frame
      lastCode = code; lastAt = now;
      beep();
      setHint("Read: " + code, true);
      onCode(code);
      if (!opts.continuous) close();
    };

    if ("BarcodeDetector" in window) {
      let detector;
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        detector = new window.BarcodeDetector({ formats: FORMATS.filter(f => supported.includes(f)) });
      } catch (e) { detector = null; }
      if (detector) { loopNative(detector, handle); return; }
    }
    loopZXing(handle);
  }

  function loopNative(detector, handle) {
    const tick = async () => {
      if (stopFlag) return;
      if (video.readyState >= 2) {
        try {
          const codes = await detector.detect(video);
          if (codes && codes.length) handle(codes[0].rawValue);
        } catch (e) { /* frame not ready */ }
      }
      if (!stopFlag) requestAnimationFrame(() => setTimeout(tick, 120));
    };
    tick();
  }

  function loadZXing() {
    return new Promise((resolve, reject) => {
      if (window.ZXing) return resolve(window.ZXing);
      const s = document.createElement("script");
      s.src = ZXING_URL; s.onload = () => resolve(window.ZXing); s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  async function loopZXing(handle) {
    let ZX;
    try { ZX = await loadZXing(); } catch (e) {
      setHint("Couldn't load the barcode reader (offline?). Type the code instead."); return;
    }
    if (stopFlag) return;
    zxingReader = new ZX.BrowserMultiFormatReader();
    try {
      await zxingReader.decodeFromStream(stream, video, (result) => {
        if (result && !stopFlag) handle(result.getText());
      });
    } catch (e) {
      setHint("Barcode reader failed to start. Type the code instead.");
    }
  }

  function close() {
    stopFlag = true;
    if (zxingReader) { try { zxingReader.reset(); } catch (e) {} zxingReader = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (video) video.srcObject = null;
    if (overlay) overlay.style.display = "none";
  }

  // ---- wiring ----------------------------------------------------------------
  function fire(el, type, init) { el.dispatchEvent(new Event(type, Object.assign({ bubbles: true }, init || {}))); }

  function fillInput(input, code) {
    input.value = code;
    fire(input, "input"); fire(input, "change");
    if (input.hasAttribute("data-scan-submit") && input.form) {
      input.form.requestSubmit ? input.form.requestSubmit() : input.form.submit();
    } else if (input.hasAttribute("data-scan-enter")) {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    } else {
      input.focus();
    }
  }

  function pickOption(select, code) {
    const needle = code.toLowerCase();
    const opts = Array.from(select.options);
    let match = opts.find(o => (o.dataset.sku || "").toLowerCase() === needle || (o.dataset.upc || "").toLowerCase() === needle)
             || opts.find(o => o.text.toLowerCase().startsWith(needle + " ") || o.text.toLowerCase() === needle)
             || opts.find(o => o.text.toLowerCase().includes(needle));
    if (!match) {
      alert("No product with barcode/SKU " + code + " in this list.");
      return;
    }
    select.value = match.value;
    fire(select, "change");
    if (select._comboInput) select._comboInput.value = match.text;
    const nextName = select.getAttribute("data-scan-next");
    const next = nextName && select.form ? select.form.querySelector('[name="' + nextName + '"]') : null;
    (next || select).focus();
  }

  function makeButton(target) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sfl-scan-btn";
    btn.title = "Scan with camera";
    btn.setAttribute("aria-label", "Scan barcode with camera");
    btn.innerHTML = "📷";
    btn.style.cssText = "flex:none;margin-left:6px;border:1px solid #cbd5e1;background:#fff;border-radius:10px;" +
                        "min-width:44px;min-height:40px;font-size:20px;line-height:1;cursor:pointer;vertical-align:middle";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const continuous = target.hasAttribute("data-scan-continuous");
      open((code) => {
        if (target.tagName === "SELECT") pickOption(target, code); else fillInput(target, code);
      }, { continuous });
    });
    return btn;
  }

  function attach(target) {
    if (target.dataset.scanReady) return;
    target.dataset.scanReady = "1";
    const btn = makeButton(target);
    // a searchable picker shows an input instead of the (hidden) select: sit next to that
    const visible = (target.tagName === "SELECT" && target._comboInput) ? target._comboInput : target;
    const wrap = document.createElement("span");
    wrap.style.cssText = "display:inline-flex;align-items:center;width:100%;min-width:0;";
    visible.parentNode.insertBefore(wrap, visible);
    wrap.appendChild(visible);
    wrap.appendChild(btn);
    if (!supportsCamera) btn.style.display = "none";
  }

  function init(root) {
    (root || document).querySelectorAll("[data-scan], [data-scan-select]").forEach(attach);
  }

  window.SFLScanner = { init, open, close };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init());
  else init();
})();
