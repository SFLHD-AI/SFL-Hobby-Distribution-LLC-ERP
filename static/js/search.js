/*
 * SFL ERP System — top-bar search with a live dropdown.
 *
 * As you type, the box asks /search/suggest/ for pages, reports, products,
 * customers, orders, invoices and suppliers that match, and shows them in a
 * dropdown you can click or arrow through. Enter opens the highlighted
 * result; with nothing highlighted it runs the full search page as before.
 * A scanned barcode still submits straight away (scanner.js handles that).
 */
(function () {
  "use strict";
  const input = document.getElementById("globalSearch");
  if (!input) return;
  const url = input.dataset.suggestUrl;
  const list = document.createElement("div");
  list.className = "sfl-search-list";
  list.style.cssText = "position:fixed;z-index:1000;background:#fff;border:1px solid #cbd5e1;border-radius:12px;" +
    "box-shadow:0 12px 32px rgba(15,23,42,.18);max-height:min(70vh,28rem);overflow-y:auto;display:none;";
  document.body.appendChild(list);
  let items = [], active = -1, timer = null, lastQ = "", controller = null;

  function place() {
    const r = input.getBoundingClientRect();
    const vw = window.innerWidth;
    const width = Math.max(r.width, Math.min(520, vw - 16));
    let left = r.left;
    if (left + width > vw - 8) left = Math.max(8, vw - 8 - width);
    list.style.left = left + "px";
    list.style.width = width + "px";
    list.style.top = (r.bottom + 6) + "px";
    list.style.maxHeight = Math.max(160, window.innerHeight - r.bottom - 16) + "px";
  }
  function close() { list.style.display = "none"; active = -1; }
  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  function render(data) {
    list.innerHTML = "";
    items = [];
    const groups = [["pages", "Go to"], ["products", "Products"], ["customers", "Customers"], ["orders", "Orders"],
                    ["invoices", "Invoices"], ["suppliers", "Suppliers"], ["receipts", "Supplier orders"]];
    groups.forEach(([key, title]) => {
      const rows = data[key] || [];
      if (!rows.length) return;
      const h = document.createElement("div");
      h.textContent = title;
      h.style.cssText = "padding:8px 12px 2px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#94a3b8";
      list.appendChild(h);
      rows.forEach(row => {
        const a = document.createElement("a");
        a.href = row.url;
        a.className = "sfl-search-row";
        a.style.cssText = "display:flex;justify-content:space-between;gap:10px;padding:9px 12px;text-decoration:none;color:#0f172a;font-size:14px;line-height:1.3;";
        a.innerHTML = '<span style="min-width:0"><span style="font-weight:600">' + esc(row.label) + '</span>' +
                      (row.sub ? ' <span style="color:#64748b">' + esc(row.sub) + '</span>' : '') + '</span>' +
                      (row.right ? '<span style="color:#64748b;white-space:nowrap">' + esc(row.right) + '</span>' : '');
        a.addEventListener("mousedown", e => { e.preventDefault(); window.location.href = row.url; });
        list.appendChild(a);
        items.push(a);
      });
    });
    if (!items.length) {
      const d = document.createElement("div");
      d.style.cssText = "padding:10px 12px;color:#64748b;font-size:13px";
      d.textContent = "Nothing matches yet — press Enter to search everything.";
      list.appendChild(d);
    } else {
      const d = document.createElement("div");
      d.style.cssText = "padding:8px 12px;color:#94a3b8;font-size:11px;border-top:1px solid #f1f5f9";
      d.textContent = "↑↓ to move · Enter to open · Enter with nothing picked searches everything";
      list.appendChild(d);
    }
    highlight();
    place();
    list.style.display = "block";
  }
  function highlight() {
    items.forEach((el, i) => { el.style.background = i === active ? "#eef2ff" : ""; });
    if (active >= 0 && items[active].scrollIntoView) items[active].scrollIntoView({ block: "nearest" });
  }
  function fetchSuggestions() {
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    if (q === lastQ && list.style.display === "block") return;
    lastQ = q;
    if (controller) controller.abort();
    controller = new AbortController();
    fetch(url + "?q=" + encodeURIComponent(q), { signal: controller.signal, headers: { "Accept": "application/json" } })
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data && input.value.trim() === q) render(data); })
      .catch(() => {});
  }
  input.addEventListener("input", () => { active = -1; clearTimeout(timer); timer = setTimeout(fetchSuggestions, 140); });
  input.addEventListener("focus", () => { if (input.value.trim().length >= 2) fetchSuggestions(); });
  input.addEventListener("keydown", e => {
    if (list.style.display !== "block") return;
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); highlight(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, -1); highlight(); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); window.location.href = items[active].href; }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  window.addEventListener("resize", () => { if (list.style.display === "block") place(); });
  window.addEventListener("scroll", () => { if (list.style.display === "block") place(); }, true);
})();
