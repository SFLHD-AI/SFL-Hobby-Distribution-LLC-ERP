"""Static, self-contained HTML dashboard: where the edges are and how prices trend.

Read-only. The page has no buttons that act on a marketplace; it only shows
what `edge` computed. Everything (CSS, JS, charts) is inline so the file can be
opened straight from disk or dropped anywhere.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from .edge import Assessment

MARKET_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]


def _usd(c) -> str:
    if c is None:
        return "—"
    v = Decimal(c) / 100
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _pct(v) -> str:
    return "—" if v is None else f"{Decimal(v):+.1f}%"


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


# ---------------------------------------------------------------------------
# charts (inline SVG)
def _scale(vals: list[float], lo_px: float, hi_px: float, pad: float = 0.06):
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    lo, hi = lo - span * pad, hi + span * pad
    return (lambda v: hi_px - (v - lo) / (hi - lo) * (hi_px - lo_px)), lo, hi


def price_chart(series: dict[str, list[dict]], refs: dict[str, Optional[int]], w: int = 560, h: int = 190) -> str:
    """Daily TPX closes per market, with Dealernet bid/ask and fair value as reference lines."""
    series = {k: v for k, v in series.items() if v}
    ref_vals = [v for v in refs.values() if v is not None]
    if not series and not ref_vals:
        return '<p class="muted small">No price history from TPX for this product yet.</p>'
    dates = sorted({p["t"] for pts in series.values() for p in pts})
    vals = [p["close"] for pts in series.values() for p in pts] + ref_vals
    left, right, top, bottom = 52, w - 8, 10, h - 22
    y, lo, hi = _scale(vals, top, bottom)
    n = max(len(dates) - 1, 1)
    xi = {d: left + i / n * (right - left) for i, d in enumerate(dates)}
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="price trend">']
    for frac in (0, 0.5, 1):
        v = lo + (hi - lo) * frac
        yy = y(v)
        parts.append(f'<line x1="{left}" x2="{right}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{yy + 4:.1f}" class="axis" text-anchor="end">${v / 100:,.0f}</text>')
    if dates:
        parts.append(f'<text x="{left}" y="{h - 4}" class="axis">{_e(dates[0][5:])}</text>')
        parts.append(f'<text x="{right}" y="{h - 4}" class="axis" text-anchor="end">{_e(dates[-1][5:])}</text>')
    ref_style = {"DN bid": "ref-bid", "DN ask": "ref-ask", "Fair value": "ref-fv"}
    for label, v in refs.items():
        if v is None:
            continue
        yy = y(v)
        parts.append(f'<line x1="{left}" x2="{right}" y1="{yy:.1f}" y2="{yy:.1f}" class="{ref_style.get(label, "ref-fv")}">'
                     f'<title>{_e(label)} {_usd(v)}</title></line>')
    for i, (name, pts) in enumerate(series.items()):
        color = MARKET_COLORS[i % len(MARKET_COLORS)]
        if len(pts) == 1:
            p = pts[0]
            parts.append(f'<circle cx="{xi[p["t"]]:.1f}" cy="{y(p["close"]):.1f}" r="3" fill="{color}"/>')
            continue
        d = " ".join(f'{"M" if j == 0 else "L"}{xi[p["t"]]:.1f},{y(p["close"]):.1f}' for j, p in enumerate(pts))
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round">'
                     f'<title>{_e(name)}</title></path>')
        last = pts[-1]
        parts.append(f'<circle cx="{xi[last["t"]]:.1f}" cy="{y(last["close"]):.1f}" r="3" fill="{color}">'
                     f'<title>{_e(name)} {last["t"]}: {_usd(last["close"])}</title></circle>')
    parts.append("</svg>")
    legend = [f'<span class="key"><i style="background:{MARKET_COLORS[i % len(MARKET_COLORS)]}"></i>{_e(n)} close</span>'
              for i, n in enumerate(series)]
    for label, cls in (("DN bid", "k-bid"), ("DN ask", "k-ask"), ("Fair value", "k-fv")):
        if refs.get(label) is not None:
            legend.append(f'<span class="key"><i class="{cls}"></i>{label} {_usd(refs[label])}</span>')
    return "".join(parts) + f'<div class="legend">{"".join(legend)}</div>'


def sparkline(values: list[float], w: int = 120, h: int = 28) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    y, _, _ = _scale(vals + [0.0], 3, h - 3)
    step = (w - 4) / (len(vals) - 1)
    d = " ".join(f'{"M" if i == 0 else "L"}{2 + i * step:.1f},{y(v):.1f}' for i, v in enumerate(vals))
    zero = y(0.0)
    cls = "spark-pos" if vals[-1] > 0 else "spark-neg"
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" aria-label="edge trend"><line x1="0" x2="{w}" y1="{zero:.1f}" '
            f'y2="{zero:.1f}" class="grid"/><path d="{d}" class="{cls}" fill="none" stroke-width="1.5"/></svg>')


# ---------------------------------------------------------------------------
def _row(a: Assessment, hist: list[dict]) -> str:
    arb = a.best_arb
    epu = arb.edge_per_unit_cents if arb else None
    positive = epu is not None and epu > 0
    flags = list(dict.fromkeys(a.tpx_liq.flags + (a.dn_liq.flags if a.dn_liq else []) + a.fv.flags))
    chg = a.tpx.price_change_30d_pct
    if chg is None and a.tpx.candles_30d is not None:
        chg = a.tpx.candles_30d.change_pct
    spark = sparkline([r.get("edge_per_unit") for r in hist][-30:])
    edge_cls = "pos" if positive else ("neg" if epu is not None else "muted")
    move = _e(arb.label) if arb else '<span class="muted">needs Dealernet price</span>'
    return (
        f'<tr data-edge="{1 if positive else 0}" data-search="{_e((a.product_name + " " + a.tpx_market).lower())}">'
        f'<td><b>{_e(a.product_name)}</b><div class="muted small">{_e(a.tpx_market)}</div></td>'
        f'<td>{move}</td>'
        f'<td class="num {edge_cls}" data-v="{float(epu) if epu is not None else -1e12}">{_usd(epu)}</td>'
        f'<td class="num {edge_cls}" data-v="{float(arb.edge_pct) if arb and arb.edge_pct is not None else -1e12}">'
        f'{_pct(arb.edge_pct) if arb else "—"}</td>'
        f'<td class="num">{arb.max_size if arb else "—"}</td>'
        f'<td class="num" data-v="{float(a.risk_adjusted_score)}">{_usd(a.risk_adjusted_score * 100)}</td>'
        f'<td class="num">{_usd(a.tpx.best_bid)} / {_usd(a.tpx.best_ask)}</td>'
        f'<td class="num">{(_usd(a.dn.best_bid) + " / " + _usd(a.dn.best_ask)) if a.dn else "—"}</td>'
        f'<td class="num">{_usd(a.fv.fair_value_cents) if a.fv.has_fv else "no FV"}</td>'
        f'<td class="num" data-v="{float(chg) if chg is not None else -1e12}">{_pct(chg)}</td>'
        f'<td>{spark}</td>'
        f'<td class="small muted">{_e("; ".join(flags))}</td></tr>'
    )


def _card(product: str, items: list[Assessment]) -> str:
    series = {a.tpx_market: a.tpx.raw.get("candles_d1") or [] for a in items}
    dn = next((a.dn for a in items if a.dn), None)
    fv = next((a.fv.fair_value_cents for a in items if a.fv.has_fv), None)
    chart = price_chart(series, {"DN bid": dn.best_bid if dn else None, "DN ask": dn.best_ask if dn else None,
                                 "Fair value": fv})
    best = max((a for a in items if a.best_arb), key=lambda a: a.best_arb.edge_per_unit_cents, default=None)
    if best and best.best_arb.edge_per_unit_cents > 0:
        tag = f'<span class="pill pos">edge {_usd(best.best_arb.edge_per_unit_cents)}/unit</span>'
    elif best:
        tag = '<span class="pill">no edge right now</span>'
    else:
        tag = '<span class="pill">no Dealernet price</span>'
    stats = []
    for a in items:
        c30 = a.tpx.candles_30d
        stats.append(f'<li><b>{_e(a.tpx_market)}</b>: 30d {_pct(a.tpx.price_change_30d_pct if a.tpx.price_change_30d_pct is not None else (c30.change_pct if c30 else None))}'
                     f', {a.tpx.units_30d if a.tpx.units_30d is not None else "?"} units / {a.tpx.trades_30d if a.tpx.trades_30d is not None else "?"} trades'
                     f', spread {_pct(a.tpx.spread_pct).lstrip("+")}</li>')
    return (f'<section class="card"><header><h3>{_e(product)}</h3>{tag}</header>{chart}'
            f'<ul class="stats small">{"".join(stats)}</ul></section>')


def render(ranked: Sequence[Assessment], *, source: str, actions_enabled: bool = False,
           history: Optional[dict] = None, margin: Optional[Decimal] = None,
           generated_at: Optional[datetime] = None, refresh_s: Optional[int] = None,
           warnings: Sequence[str] = ()) -> str:
    history = history or {}
    generated_at = generated_at or datetime.now(timezone.utc)
    arbs = [a.best_arb for a in ranked if a.best_arb]
    positive = [x for x in arbs if x.is_positive]
    best = max(positive, key=lambda x: x.edge_per_unit_cents, default=None)
    products = list(dict.fromkeys(a.product_name for a in ranked))
    with_dn = len({a.product_name for a in ranked if a.dn})
    by_product: dict[str, list[Assessment]] = defaultdict(list)
    for a in ranked:
        by_product[a.product_name].append(a)

    tiles = [
        ("Products tracked", str(len(products)), f"{with_dn} with a Dealernet price"),
        ("Edges right now", str(len(positive)), f"of {len(arbs)} comparable market pairs"),
        ("Best edge / unit", _usd(best.edge_per_unit_cents) if best else "—", _e(best.label) if best else "none above $0 all-in"),
        ("Target margin", f"{Decimal(margin) * 100:.0f}%" if margin is not None else "—", "on capital deployed"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="muted small">{t}</div><div class="big">{v}</div>'
                         f'<div class="muted small">{s}</div></div>' for t, v, s in tiles)
    rows = "".join(_row(a, history.get((a.tpx.product_id, a.tpx_market), [])) for a in ranked)
    cards = "".join(_card(p, by_product[p]) for p in products)
    banner = ""
    if source != "live":
        banner = ('<div class="banner">Sample data: these prices come from the built-in demo fixtures, '
                  'not from TPX or Dealernet. Run without <code>--demo</code> once TPX access is set up.</div>')
    warn_html = "".join(f'<div class="banner warn">{_e(w)}</div>' for w in warnings)
    actions = ('<span class="pill pos">Actions: ON</span>' if actions_enabled
               else '<span class="pill" title="Set ACTIONS_ENABLED=true to allow features that act on a marketplace">Actions: OFF (view only)</span>')
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    data = json.dumps({"generated_at": generated_at.isoformat(), "source": source})
    return TEMPLATE.format(meta_refresh=meta_refresh, generated=_e(generated_at.strftime("%Y-%m-%d %H:%M UTC")),
                           source=_e(source.upper()), actions=actions, banner=banner + warn_html, tiles=tiles_html,
                           rows=rows or '<tr><td colspan="12" class="muted">No products.</td></tr>', cards=cards,
                           data=data)


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta_refresh}<title>TPX Dealernet Edges</title>
<style>
:root{{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--pos:#1a7f4b;--neg:#b3412e;
--s1:#2f6fd6;--s2:#c2741a;--s3:#7a4cc2;--s4:#2a9d8f;--bid:#1a7f4b;--ask:#b3412e;--fv:#6b6b66;--banner:#fff4d6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#151514;--panel:#1f1f1d;--ink:#ecebe6;--muted:#9d9c95;
--line:#34332f;--pos:#4cc38a;--neg:#ef7b66;--s1:#6ea2ff;--s2:#f0a04b;--s3:#b28cf0;--s4:#56c7b8;--bid:#4cc38a;--ask:#ef7b66;--fv:#9d9c95;--banner:#3a3220}}}}
:root[data-theme="dark"]{{--bg:#151514;--panel:#1f1f1d;--ink:#ecebe6;--muted:#9d9c95;--line:#34332f;--pos:#4cc38a;--neg:#ef7b66;
--s1:#6ea2ff;--s2:#f0a04b;--s3:#b28cf0;--s4:#56c7b8;--bid:#4cc38a;--ask:#ef7b66;--fv:#9d9c95;--banner:#3a3220}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:22px;margin:0}}h2{{font-size:16px;margin:28px 0 10px}}h3{{font-size:15px;margin:0}}
.top{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}}
.muted{{color:var(--muted)}}.small{{font-size:12px}}.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}
.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;background:var(--panel)}}
.pill.pos{{border-color:var(--pos)}}
.banner{{background:var(--banner);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:14px 0 0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}}
.tile,.card,.tablewrap{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}
.tile{{padding:12px 14px}}.big{{font-size:24px;font-weight:600;margin:2px 0}}
.controls{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:8px}}
.controls input[type=search]{{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);min-width:220px}}
.tablewrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:1100px}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}}
th{{font-size:12px;color:var(--muted);font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}}
tr:last-child td{{border-bottom:0}}
td:nth-child(2){{min-width:200px}}td:first-child{{min-width:190px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:12px}}
.card{{padding:12px 14px}}.card header{{display:flex;gap:8px;justify-content:space-between;align-items:center;margin-bottom:6px}}
.chart{{width:100%;height:auto;display:block}}.grid{{stroke:var(--line);stroke-width:1}}
.axis{{fill:var(--muted);font-size:10px}}
.ref-bid{{stroke:var(--bid);stroke-dasharray:5 4;stroke-width:1.5}}.ref-ask{{stroke:var(--ask);stroke-dasharray:5 4;stroke-width:1.5}}
.ref-fv{{stroke:var(--fv);stroke-dasharray:2 3;stroke-width:1.5}}
.legend{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted)}}
.key i{{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;border-radius:2px}}
.k-bid{{background:var(--bid)}}.k-ask{{background:var(--ask)}}.k-fv{{background:var(--fv)}}
.stats{{margin:8px 0 0;padding-left:18px}}.spark{{width:120px;height:28px}}
.spark-pos{{stroke:var(--pos)}}.spark-neg{{stroke:var(--neg)}}
code{{font-size:12px}}footer{{margin-top:28px}}
</style></head><body><main>
<div class="top"><div><h1>TPX ↔ Dealernet edges</h1>
<div class="muted small">Updated {generated} · data: {source} · all prices all-in after fees and shipping unless marked clean</div></div>
<div>{actions}</div></div>
{banner}
<div class="tiles">{tiles}</div>
<h2>Where you have an edge</h2>
<div class="controls"><input type="search" id="q" placeholder="Filter products"><label><input type="checkbox" id="edgesOnly"> Edges only</label>
<span class="muted small">Click a column to sort.</span></div>
<div class="tablewrap"><table id="edges"><thead><tr>
<th>Product / TPX market</th><th>Best move</th><th class="num">Edge / unit</th><th class="num">Edge %</th><th class="num">Size</th>
<th class="num">Score</th><th class="num">TPX bid / ask (clean)</th><th class="num">DN bid / ask</th><th class="num">Fair value</th>
<th class="num">TPX 30d</th><th>Edge trend</th><th>Flags</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Price trends</h2>
<div class="cards">{cards}</div>
<footer class="muted small">View only. This page never places, lists or cancels anything. Edge trend fills in as the dashboard is re-run.</footer>
</main>
<script id="meta" type="application/json">{data}</script>
<script>
(function(){{
var q=document.getElementById('q'),eo=document.getElementById('edgesOnly'),tb=document.querySelector('#edges tbody');
try{{eo.checked=localStorage.getItem('edgesOnly')==='1';}}catch(e){{}}
function apply(){{var t=q.value.toLowerCase();Array.prototype.forEach.call(tb.rows,function(r){{
var ok=(!t||(r.dataset.search||'').indexOf(t)>=0)&&(!eo.checked||r.dataset.edge==='1');r.style.display=ok?'':'none';}});
try{{localStorage.setItem('edgesOnly',eo.checked?'1':'0');}}catch(e){{}}}}
q.addEventListener('input',apply);eo.addEventListener('change',apply);apply();
document.querySelectorAll('#edges th').forEach(function(th,i){{var dir=-1;th.addEventListener('click',function(){{dir=-dir;
var rows=Array.prototype.slice.call(tb.rows);rows.sort(function(a,b){{var x=a.cells[i],y=b.cells[i];
var vx=x.dataset.v,vy=y.dataset.v;if(vx!==undefined&&vy!==undefined)return (parseFloat(vx)-parseFloat(vy))*dir;
return x.textContent.localeCompare(y.textContent)*dir;}});rows.forEach(function(r){{tb.appendChild(r);}});}});}});
}})();
</script></body></html>"""
