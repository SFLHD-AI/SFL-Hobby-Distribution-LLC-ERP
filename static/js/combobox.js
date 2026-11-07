/*
 * SFL ERP System — searchable product picker.
 *
 * Any <select data-combobox> becomes a text box that filters its options as
 * you type, shows the FULL option text (wrapped, never clipped), and stays
 * inside the page. The original <select> is kept (hidden) so forms submit
 * exactly as before and the camera scanner can still set it; a change event
 * on the select updates the box.
 */
(function () {
  "use strict";
  function norm(s) { return (s || "").toLowerCase().replace(/\s+/g, " ").trim(); }

  function build(select) {
    if (select.dataset.comboReady) return;
    select.dataset.comboReady = "1";
    const wrap = document.createElement("div");
    wrap.className = "sfl-combo";
    wrap.style.cssText = "position:relative;width:100%;min-width:0;";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.style.display = "none";
    select.tabIndex = -1;

    const input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    input.className = "sfl-combo-input w-full rounded-lg border-slate-300 text-sm";
    input.style.cssText = "flex:1 1 auto;min-width:0;";      // keeps its width next to the 📷 button
    input.placeholder = select.getAttribute("data-combobox-placeholder") || "Type a SKU, UPC or name to search…";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    wrap.appendChild(input);

    const list = document.createElement("div");
    list.className = "sfl-combo-list";
    // The list is appended to <body> and positioned "fixed" so no scrolling
    // card, table wrapper or overflow:hidden box can clip it — it always
    // renders on top of everything else.
    list.style.cssText = "position:fixed;z-index:1000;background:#fff;border:1px solid #cbd5e1;" +
      "border-radius:10px;box-shadow:0 10px 30px rgba(15,23,42,.15);max-height:min(60vh,22rem);overflow-y:auto;display:none;";
    document.body.appendChild(list);
    function place() {
      const r = input.getBoundingClientRect();
      const vh = window.innerHeight, vw = window.innerWidth;
      const below = vh - r.bottom - 8, above = r.top - 8;
      const maxH = Math.min(22 * 16, vh * 0.6);
      const openUp = below < 160 && above > below;
      list.style.left = Math.max(8, r.left) + "px";
      list.style.width = Math.min(r.width, vw - 16) + "px";
      list.style.maxHeight = Math.max(120, Math.min(maxH, openUp ? above : below)) + "px";
      if (openUp) { list.style.top = ""; list.style.bottom = (vh - r.top + 4) + "px"; }
      else { list.style.bottom = ""; list.style.top = (r.bottom + 4) + "px"; }
    }
    select._comboInput = input;

    const options = Array.from(select.options).filter(o => o.value !== "");
    let active = -1, shown = [];

    function labelFor(o) { return o.text; }
    function render(filter) {
      const q = norm(filter);
      const terms = q ? q.split(" ") : [];
      shown = options.filter(o => {
        if (!terms.length) return true;
        const hay = norm(labelFor(o) + " " + (o.dataset.sku || "") + " " + (o.dataset.upc || ""));
        return terms.every(t => hay.includes(t));
      }).slice(0, 200);
      list.innerHTML = "";
      if (!shown.length) {
        const d = document.createElement("div");
        d.style.cssText = "padding:10px 12px;color:#64748b;font-size:13px";
        d.textContent = q ? "No products match “" + filter + "”" : "No products";
        list.appendChild(d);
      }
      shown.forEach((o, i) => {
        const row = document.createElement("div");
        row.className = "sfl-combo-row";
        row.setAttribute("role", "option");
        row.style.cssText = "padding:9px 12px;cursor:pointer;font-size:14px;line-height:1.35;white-space:normal;word-break:break-word;" +
                            "border-bottom:1px solid #f1f5f9;" + (i === active ? "background:#eef2ff;" : "");
        const sku = o.dataset.sku || labelFor(o).split(" — ")[0];
        const rest = labelFor(o).startsWith(sku) ? labelFor(o).slice(sku.length).replace(/^\s*—\s*/, "") : labelFor(o);
        row.innerHTML = '<span style="font-family:ui-monospace,monospace;font-weight:600">' + escapeHtml(sku) + '</span>' +
                        (rest ? ' <span style="color:#334155">— ' + escapeHtml(rest) + '</span>' : '');
        row.addEventListener("mousedown", (e) => { e.preventDefault(); choose(o); });
        list.appendChild(row);
      });
      if (options.length > 200 && shown.length === 200) {
        const d = document.createElement("div");
        d.style.cssText = "padding:8px 12px;color:#94a3b8;font-size:12px";
        d.textContent = "Showing the first 200 — keep typing to narrow it down";
        list.appendChild(d);
      }
    }
    function open() { render(input.value); place(); list.style.display = "block"; input.setAttribute("aria-expanded", "true"); }
    window.addEventListener("resize", () => { if (list.style.display === "block") place(); });
    window.addEventListener("scroll", () => { if (list.style.display === "block") place(); }, true);
    function close() { list.style.display = "none"; active = -1; input.setAttribute("aria-expanded", "false"); }
    function choose(o) {
      select.value = o.value;
      input.value = labelFor(o);
      close();
      select.dispatchEvent(new Event("change", { bubbles: true }));
      const nextName = select.getAttribute("data-scan-next");
      const next = nextName && select.form ? select.form.querySelector('[name="' + nextName + '"]') : null;
      if (next) next.focus();
    }
    function syncFromSelect() {
      const o = select.options[select.selectedIndex];
      input.value = (o && o.value) ? labelFor(o) : "";
    }
    input.addEventListener("focus", open);
    input.addEventListener("input", () => { active = -1; if (select.value) { select.value = ""; } open(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, shown.length - 1); render(input.value); scrollActive(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); render(input.value); scrollActive(); }
      else if (e.key === "Enter") {
        if (list.style.display === "block" && shown.length) { e.preventDefault(); choose(shown[active >= 0 ? active : 0]); }
        else if (!select.value) { e.preventDefault(); }
      } else if (e.key === "Escape") { close(); }
    });
    input.addEventListener("blur", () => setTimeout(() => { close(); if (!select.value) input.value = ""; else syncFromSelect(); }, 120));
    select.addEventListener("change", () => { if (document.activeElement !== input) syncFromSelect(); });
    function scrollActive() { const el = list.children[active]; if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" }); }
    // form validation: a required select that is hidden can't show the browser bubble; check ourselves
    if (select.form) {
      select.form.addEventListener("submit", (e) => {
        if (select.required && !select.value) { e.preventDefault(); input.focus(); input.setCustomValidity("Pick a product from the list"); input.reportValidity(); setTimeout(() => input.setCustomValidity(""), 1500); }
      });
    }
    syncFromSelect();
    select._comboList = list;
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function init(root) { (root || document).querySelectorAll("select[data-combobox]").forEach(build); }
  window.SFLCombobox = { init };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init()); else init();
})();
