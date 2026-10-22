"""Static, self-contained HTML terminal: where the edges are and how prices move.

Read-only. The page has no buttons that act on a marketplace; it only shows
what `edge` computed. Everything (CSS, JS, charts) is inline so the file can be
opened straight from disk, dropped anywhere, or published as a page.

Layout: ticker tape -> headline metrics -> screener (one row per product,
expandable into a trade desk with the chart, both ladders and the price levels
that would clear the margin) -> notes.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from .edge import Assessment, MakerLevel, TakerArb
from .tcgplayer import pct_of

MARKET_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# formatting
def _usd(c, decimals: int = 2) -> str:
    if c is None:
        return "—"
    v = Decimal(c) / 100
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.{decimals}f}"


def _usd_signed(c) -> str:
    if c is None:
        return "—"
    v = Decimal(c) / 100
    return f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def _pct(v, signed: bool = True, dec: int = 1) -> str:
    if v is None:
        return "—"
    d = Decimal(v)
    return f"{d:+.{dec}f}%" if signed else f"{d:.{dec}f}%"


def _int(v) -> str:
    return "—" if v is None else f"{int(v):,}"


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _cls(v) -> str:
    if v is None:
        return "muted"
    return "pos" if Decimal(v) > 0 else "neg" if Decimal(v) < 0 else "flat"


def _sortv(v, missing: float = -1e12) -> str:
    return str(float(v)) if v is not None else str(missing)


def _short(name: str, n: int = 26) -> str:
    s = name.replace("Elite Trainer Box", "ETB").replace("Hobby Box", "Hobby").replace("Booster Pack", "Pack")
    s = s.replace("Basketball", "BK").replace("Baseball", "BB").replace("Football", "FB").replace("Collection", "Coll.")
    s = s.replace("Topps ", "").replace("Edition", "Ed.")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _ratio_pct(a: Optional[int], b: Optional[int]) -> Optional[Decimal]:
    """a vs b as a signed percent (a/b - 1)."""
    if a is None or not b:
        return None
    return ((Decimal(a) / Decimal(b)) - 1) * 100


# ---------------------------------------------------------------------------
# charts (inline SVG)
def _scale(vals: list[float], lo_px: float, hi_px: float, pad: float = 0.08):
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    lo, hi = lo - span * pad, hi + span * pad
    return (lambda v: hi_px - (v - lo) / (hi - lo) * (hi_px - lo_px)), lo, hi


def price_chart(series: dict[str, list[dict]], refs: dict[str, Optional[int]], w: int = 640, h: int = 220) -> str:
    """Daily TPX closes per market (line + wash), with Dealernet bid/ask, fair value
    and the TCGplayer market price as reference lines."""
    series = {k: v for k, v in series.items() if v}
    ref_vals = [v for v in refs.values() if v is not None]
    if not series and not ref_vals:
        return '<p class="muted small">No price history: no TPX trades in the last 30 days.</p>'
    dates = sorted({p["t"] for pts in series.values() for p in pts})
    vals = [p["close"] for pts in series.values() for p in pts] + ref_vals
    left, right, top, bottom = 56, w - 12, 12, h - 24
    y, lo, hi = _scale(vals, top, bottom)
    n = max(len(dates) - 1, 1)
    xi = {d: left + i / n * (right - left) for i, d in enumerate(dates)}
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="30-day TPX price with Dealernet levels">']
    for frac in (0, 0.5, 1):
        v = lo + (hi - lo) * frac
        yy = y(v)
        parts.append(f'<line x1="{left}" x2="{right}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 8}" y="{yy + 4:.1f}" class="axis" text-anchor="end">{_usd(v, 0)}</text>')
    if dates:
        parts.append(f'<text x="{left}" y="{h - 6}" class="axis">{_e(dates[0][5:])}</text>')
        if len(dates) > 1:
            parts.append(f'<text x="{right}" y="{h - 6}" class="axis" text-anchor="end">{_e(dates[-1][5:])}</text>')
    ref_style = {"DN bid": "ref-bid", "DN ask": "ref-ask", "Fair value": "ref-fv", "TCG market": "ref-tcg"}
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
            parts.append(f'<circle cx="{xi[p["t"]]:.1f}" cy="{y(p["close"]):.1f}" r="4" fill="{color}" class="dot">'
                         f'<title>{_e(name)} {p["t"]}: {_usd(p["close"])}</title></circle>')
            continue
        pts_xy = [(xi[p["t"]], y(p["close"])) for p in pts]
        d = " ".join(f'{"M" if j == 0 else "L"}{x:.1f},{yy:.1f}' for j, (x, yy) in enumerate(pts_xy))
        if i == 0:
            area = d + f' L{pts_xy[-1][0]:.1f},{bottom} L{pts_xy[0][0]:.1f},{bottom} Z'
            parts.append(f'<path d="{area}" fill="{color}" opacity="0.1" stroke="none"/>')
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" '
                     f'stroke-linecap="round"><title>{_e(name)}</title></path>')
        last = pts[-1]
        parts.append(f'<circle cx="{pts_xy[-1][0]:.1f}" cy="{pts_xy[-1][1]:.1f}" r="4" fill="{color}" class="dot">'
                     f'<title>{_e(name)} {last["t"]}: {_usd(last["close"])}</title></circle>')
    parts.append("</svg>")
    legend = [f'<span class="key"><i style="background:{MARKET_COLORS[i % len(MARKET_COLORS)]}"></i>{_e(n)}</span>'
              for i, n in enumerate(series)]
    for label, cls in (("DN bid", "k-bid"), ("DN ask", "k-ask"), ("Fair value", "k-fv"), ("TCG market", "k-tcg")):
        if refs.get(label) is not None:
            legend.append(f'<span class="key"><i class="{cls}"></i>{label} {_usd(refs[label])}</span>')
    return "".join(parts) + f'<div class="legend">{"".join(legend)}</div>'


def sparkline(values: list[Optional[float]], w: int = 96, h: int = 26, *, baseline: Optional[float] = 0.0) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    y, _, _ = _scale(vals + ([baseline] if baseline is not None else []), 3, h - 3)
    step = (w - 6) / (len(vals) - 1)
    pts = [(3 + i * step, y(v)) for i, v in enumerate(vals)]
    d = " ".join(f'{"M" if i == 0 else "L"}{x:.1f},{yy:.1f}' for i, (x, yy) in enumerate(pts))
    cls = "spark-pos" if vals[-1] >= (vals[0] if baseline is None else baseline) else "spark-neg"
    base = f'<line x1="0" x2="{w}" y1="{y(baseline):.1f}" y2="{y(baseline):.1f}" class="grid"/>' if baseline is not None else ""
    label = "edge trend" if baseline is not None else "price trend"
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" aria-label="{label}">{base}'
            f'<path d="{d}" class="{cls}" fill="none" stroke-width="1.5" stroke-linejoin="round"/>'
            f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.5" class="{cls}-dot"/></svg>')


# ---------------------------------------------------------------------------
# per-product view model
class ProductView:
    def __init__(self, name: str, items: list[Assessment], margin: Optional[Decimal], history: dict):
        self.name = name
        self.items = items
        self.margin = Decimal(margin) if margin is not None else Decimal("0.08")
        # Primary market: the one with the best edge, else the most liquid.
        with_arb = [a for a in items if a.best_arb and a.best_arb.edge_per_unit_cents is not None]
        if with_arb:
            self.primary = max(with_arb, key=lambda a: a.best_arb.edge_per_unit_cents)
        else:
            self.primary = max(items, key=lambda a: (a.tpx.bid_depth_units + a.tpx.ask_depth_units, a.tpx.units_30d or 0))
        self.dn = next((a.dn for a in items if a.dn), None)
        self.arb: Optional[TakerArb] = self.primary.best_arb
        self.edge = self.arb.edge_per_unit_cents if self.arb else None
        self.edge_pct = self.arb.edge_pct if self.arb else None
        self.fv = next((a.fv.fair_value_cents for a in items if a.fv.has_fv), None)
        self.tcg = next((a.tpx.tcg_market for a in items if a.tpx.tcg_market is not None), None)
        self.tcg_low = next((a.tpx.tcg_low for a in items if a.tpx.tcg_low is not None), None)
        # Price action: prefer the market that actually trades.
        traded = max(items, key=lambda a: (a.tpx.units_30d or 0))
        t = traded.tpx
        self.last = t.last_trade_price or (t.candles_30d.close_cents if t.candles_30d else None)
        self.last_at = t.last_trade_at
        self.chg30 = t.price_change_30d_pct if t.price_change_30d_pct is not None else (t.candles_30d.change_pct if t.candles_30d else None)
        self.vol30 = sum((a.tpx.units_30d or 0) for a in items)
        self.trades30 = sum((a.tpx.trades_30d or 0) for a in items)
        self.vwap30 = t.vwap_30d
        self.vol24 = sum((a.tpx.vol_24h or 0) for a in items)
        self.hi24 = max((a.tpx.high_24h for a in items if a.tpx.high_24h), default=None)
        self.lo24 = min((a.tpx.low_24h for a in items if a.tpx.low_24h), default=None)
        # Best TPX touch across markets (clean).
        self.tpx_bid = max((a.tpx.best_bid for a in items if a.tpx.best_bid), default=None)
        self.tpx_ask = min((a.tpx.best_ask for a in items if a.tpx.best_ask), default=None)
        self.dn_bid = self.dn.best_bid if self.dn else None
        self.dn_ask = self.dn.best_ask if self.dn else None
        self.dn_prem_bid = _ratio_pct(self.dn_bid, self.tpx_bid)  # DN bid vs TPX bid
        self.dn_prem_ask = _ratio_pct(self.dn_ask, self.tpx_ask)
        self.flags = list(dict.fromkeys(f for a in items for f in (a.tpx_liq.flags + (a.dn_liq.flags if a.dn_liq else []) + a.fv.flags)))
        self.hist = history.get((self.primary.tpx.product_id, self.primary.tpx_market), [])
        self.closes = self.primary.tpx.raw.get("candles_d1") or traded.tpx.raw.get("candles_d1") or []
        # A post level only counts when it is near where TPX actually trades; a
        # $19 ask on a $9 pack "clears the margin" but will never fill.
        dn_mid = ((self.dn_bid + self.dn_ask) // 2) if self.dn_bid and self.dn_ask else (self.dn_bid or self.dn_ask)
        self.ref = self.last or self.fv or self.vwap30 or dn_mid or self.tpx_bid or self.tpx_ask
        # The two trades Ross cares about, each on its best TPX market:
        #   buy_tpx: post a bid on TPX, sell into the Dealernet bid
        #   buy_dn:  hit the Dealernet ask, post an ask on TPX
        self.buy_tpx = Direction("Buy TPX → sell DN", self._pick_maker("bid", "Dealernet bid"), self._pick_arb("arb_b"), self.ref)
        self.buy_dn = Direction("Buy DN → sell TPX", self._pick_maker("ask", "Dealernet ask"), self._pick_arb("arb_a"), self.ref)
        self.maker = max((d.post for d in (self.buy_tpx, self.buy_dn) if d.post), key=lambda m: m.expected_edge_pct or -999, default=None)

    def _pick_maker(self, side: str, counter_hint: str) -> Optional[MakerLevel]:
        cands = [m for a in self.items for m in a.makers
                 if m.side == side and m.venue.startswith("TPX") and counter_hint in m.label and m.rest_clean_cents]
        # Prefer a level near the market, then one that would sit at the top of its book,
        # then the most aggressive price (highest bid / lowest ask).
        sign = 1 if side == "bid" else -1
        return max(cands, key=lambda m: (_near_market(m, self.ref), bool(m.would_be_best),
                                        m.improves_by_ticks if m.improves_by_ticks is not None else 0,
                                        sign * m.rest_clean_cents), default=None)

    def _pick_arb(self, attr: str) -> Optional[TakerArb]:
        cands = [getattr(a, attr) for a in self.items if getattr(a, attr) and getattr(a, attr).edge_per_unit_cents is not None]
        return max(cands, key=lambda x: x.edge_per_unit_cents, default=None)

    @property
    def signal(self) -> tuple[str, str, str]:
        """(class, label, one-line plan)."""
        if self.dn is None:
            return "none", "NO DN", "No Dealernet price for this product yet."
        if self.edge is not None and self.edge > 0:
            strong = self.edge_pct is not None and self.edge_pct >= self.margin * 100
            return ("edge" if strong else "thin"), ("TAKE NOW" if strong else "THIN EDGE"), _plan(self.arb)
        best = max((d for d in (self.buy_tpx, self.buy_dn) if d.post), key=lambda d: d.post_rank, default=None)
        if best and best.post and best.post.would_be_best and best.near_market:
            m = best.post
            return "post", f"POST {m.side.upper()}", (
                f"{best.name}: post a TPX {m.side} at {_usd(m.rest_clean_cents)}"
                + (f" (top of book, current best {_usd(m.current_top_cents)})" if m.current_top_cents else " (book is empty, you would be alone)")
                + f", then {'sell into' if m.side == 'bid' else 'cover from'} the Dealernet {'bid' if m.side == 'bid' else 'ask'} at "
                + f"{_usd(m.counter_clean_cents)} for {_pct(m.expected_edge_pct)} after fees and shipping.")
        if best and best.post and not best.near_market:
            m = best.post
            return "none", "OFF MARKET", (
                f"{best.name}: a TPX {m.side} at {_usd(m.rest_clean_cents)} would clear {self.margin * 100:.0f}%, but that is "
                f"{_pct(best.off_market_pct)} from where TPX trades ({_usd(self.ref)}). It will not fill; shipping and fees eat the gap.")
        if best and best.post:
            m = best.post
            return "behind", "BEHIND", (
                f"{best.name}: a TPX {m.side} at {_usd(m.rest_clean_cents)} would clear {self.margin * 100:.0f}%, but the book is already at "
                f"{_usd(m.current_top_cents)} ({_usd_signed((m.rest_clean_cents or 0) - (m.current_top_cents or 0))} away). Wait for it to come back.")
        if self.edge is not None:
            return "none", "NO EDGE", _plan(self.arb)
        return "none", "NO BOOK", "One side has no bids or asks to trade against."

    @property
    def short(self) -> str:
        return _short(self.name)


class Direction:
    """One of the two round trips, with the taker version (take now) and the maker
    version (post on TPX and wait)."""

    def __init__(self, name: str, post: Optional[MakerLevel], take: Optional[TakerArb], ref: Optional[int] = None):
        self.name, self.post, self.take, self.ref = name, post, take, ref

    @property
    def near_market(self) -> bool:
        return _near_market(self.post, self.ref)

    @property
    def off_market_pct(self) -> Optional[Decimal]:
        """Signed distance of the post level from the TPX reference price (bid below, ask above)."""
        m = self.post
        if not m or m.rest_clean_cents is None or not self.ref:
            return None
        return _ratio_pct(m.rest_clean_cents, self.ref)

    @property
    def gap_cents(self) -> Optional[int]:
        """How far the post level sits from the current TPX top: positive = you would be best."""
        m = self.post
        if not m or m.rest_clean_cents is None or m.current_top_cents is None:
            return None
        return (m.rest_clean_cents - m.current_top_cents) if m.side == "bid" else (m.current_top_cents - m.rest_clean_cents)

    @property
    def post_rank(self) -> tuple:
        m = self.post
        return (self.near_market, bool(m and m.would_be_best), self.gap_cents if self.gap_cents is not None else 10 ** 9, m.expected_edge_pct if m else ZERO)


NEAR_MARKET_PCT = Decimal("10")  # a post level more than this far from the TPX reference is not fillable


def _near_market(m: Optional[MakerLevel], ref: Optional[int]) -> bool:
    if not m or m.rest_clean_cents is None:
        return False
    if not ref:
        return True  # nothing traded yet: no basis to reject it
    off = _ratio_pct(m.rest_clean_cents, ref)
    return off is not None and (off >= -NEAR_MARKET_PCT if m.side == "bid" else off <= NEAR_MARKET_PCT)


def _plan(arb: Optional[TakerArb]) -> str:
    if arb is None:
        return ""
    e = arb.edge_per_unit_cents
    return (f"Buy {_e(arb.buy_venue)} at {_usd(arb.buy_clean_cents)}, sell {_e(arb.sell_venue)} at {_usd(arb.sell_clean_cents)}: "
            f"{_usd_signed(e)}/unit after fees and shipping ({_pct(arb.edge_pct)}), up to {arb.max_size} units.")


def _best_maker(items: list[Assessment], margin: Decimal) -> Optional[MakerLevel]:
    cands = [m for a in items for m in a.makers if m.rest_clean_cents and m.expected_edge_pct is not None]
    return max(cands, key=lambda m: (m.expected_edge_pct, bool(m.would_be_best)), default=None)


# ---------------------------------------------------------------------------
# page pieces
def _ticker(views: list[ProductView]) -> str:
    chips = []
    for v in views:
        px = v.last or v.fv or v.dn_bid or v.tpx_bid
        chips.append(f'<span class="tk"><span class="tk-n">{_e(v.short)}</span>'
                     f'<span class="tk-p">{_usd(px)}</span>'
                     f'<span class="tk-c {_cls(v.chg30)}">{_pct(v.chg30)}</span></span>')
    strip = "".join(chips) or '<span class="tk muted">no products</span>'
    return f'<div class="tape" aria-label="ticker"><div class="tape-track">{strip}{strip}</div></div>'


def _metrics(views: list[ProductView], margin: Optional[Decimal], generated_at: datetime) -> str:
    edges = [v for v in views if v.edge is not None and v.edge > 0]
    rests = [v for v in views if v.signal[0] == "post"]
    best = max(edges, key=lambda v: v.edge, default=None)
    with_dn = [v for v in views if v.dn]
    prem = sorted(v.dn_prem_bid for v in with_dn if v.dn_prem_bid is not None and abs(v.dn_prem_bid) < 300)
    avg_prem = prem[len(prem) // 2] if prem else None
    notional = sum((v.vwap30 or v.last or 0) * v.vol30 for v in views)
    movers = sorted((v for v in views if v.chg30 is not None), key=lambda v: v.chg30)
    up = movers[-1] if movers else None
    down = movers[0] if movers else None

    def tile(label, value, sub, cls="", hero=False):
        return (f'<div class="m {"hero" if hero else ""}"><div class="m-l">{label}</div>'
                f'<div class="m-v {cls}">{value}</div><div class="m-s">{sub}</div></div>')

    return "".join([
        tile("Live edges", str(len(edges)), f"of {len(with_dn)} products with both prices" if with_dn else "no Dealernet prices yet",
             "pos" if edges else "", hero=True),
        tile("Best edge", _usd_signed(best.edge) if best else "—",
             f"{_e(best.short)} · {_pct(best.edge_pct)}" if best else f"none clears $0 at {Decimal(margin or 0) * 100:.0f}% target"),
        tile("Postable now", str(len(rests)), "TPX bids/asks you could post at top of book" if rests else "no top-of-book level clears the margin",
             "pos" if rests else ""),
        tile("DN bid vs TPX bid", _pct(avg_prem), "median across matched products", _cls(avg_prem)),
        tile("TPX 30d volume", _usd(notional, 0), f"{sum(v.vol30 for v in views):,} units · {sum(v.trades30 for v in views):,} trades"),
        tile("Top mover 30d", f'{_pct(up.chg30)}' if up else "—", _e(up.short) if up else "", _cls(up.chg30) if up else ""),
        tile("Worst mover 30d", f'{_pct(down.chg30)}' if down else "—", _e(down.short) if down else "", _cls(down.chg30) if down else ""),
    ])


def _ladder(levels, side: str, n: int = 5, unit_mult: int = 1) -> str:
    rows = []
    for lv in levels[:n]:
        rows.append(f'<tr><td class="num">{_usd(lv.price_cents)}</td><td class="num muted">{lv.quantity:,}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="2" class="muted">empty</td></tr>')
    return (f'<table class="ladder {side}"><thead><tr><th>{"Bid" if side == "bid" else "Ask"}</th><th class="num">Qty</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _desk(v: ProductView) -> str:
    """The expanded trade desk for one product."""
    series = {a.tpx_market.replace("TPX:", ""): a.tpx.raw.get("candles_d1") or [] for a in v.items}
    chart = price_chart(series, {"DN bid": v.dn_bid, "DN ask": v.dn_ask, "Fair value": v.fv, "TCG market": v.tcg})
    sig_cls, sig_label, plan = v.signal

    # Price levels that matter.
    levels = [
        ("TPX bid / ask", f'{_usd(v.tpx_bid)} / {_usd(v.tpx_ask)}', f"spread {_pct(v.primary.tpx.spread_pct, False)}" if v.primary.tpx.spread_pct is not None else "one-sided"),
        ("Dealernet bid / ask", f'{_usd(v.dn_bid)} / {_usd(v.dn_ask)}', f"spread {_pct(v.dn.spread_pct, False)}" if v.dn and v.dn.spread_pct is not None else ("no Dealernet price" if not v.dn else "one-sided")),
        ("Last TPX trade", _usd(v.last), v.last_at.strftime("%b %d %H:%M UTC") if v.last_at else "no trades in 30d"),
        ("30d VWAP", _usd(v.vwap30), f"{v.vol30:,} units · {v.trades30:,} trades"),
        ("TPX fair value", _usd(v.fv) if v.fv else "no FV", (f"TPX bid {_pct(v.primary.fv.tpx_bid_gap_pct)} · DN bid {_pct(v.primary.fv.dn_bid_gap_pct)} vs FV") if v.fv else "TPX has not priced this product"),
        ("TCGplayer market", _usd(v.tcg) if v.tcg else "—",
         (f"TPX bid {pct_of(v.tpx_bid, v.tcg) or '—'}% · DN bid {pct_of(v.dn_bid, v.tcg) or '—'}% of market") if v.tcg else "needs TCGplayer keys"),
    ]
    levels_html = "".join(f'<div class="lv"><div class="lv-l">{l}</div><div class="lv-v">{val}</div><div class="lv-s">{sub}</div></div>'
                          for l, val, sub in levels)

    # Both directions, all markets.
    moves = []
    for a in v.items:
        for arb in (a.arb_a, a.arb_b):
            if arb and arb.edge_per_unit_cents is not None:
                moves.append(arb)
    moves.sort(key=lambda x: x.edge_per_unit_cents, reverse=True)
    move_rows = "".join(
        f'<tr><td>{_e(m.label)}</td><td class="num">{_usd(m.buy_clean_cents)}</td><td class="num">{_usd(m.sell_clean_cents)}</td>'
        f'<td class="num {_cls(m.edge_per_unit_cents)}">{_usd_signed(m.edge_per_unit_cents)}</td>'
        f'<td class="num {_cls(m.edge_pct)}">{_pct(m.edge_pct)}</td><td class="num">{m.max_size}</td></tr>' for m in moves
    ) or '<tr><td colspan="6" class="muted">Needs a Dealernet price and a TPX book on the other side.</td></tr>'

    makers = sorted((m for a in v.items for m in a.makers if m.rest_clean_cents), key=lambda m: -(m.expected_edge_pct or -999))
    maker_rows = "".join(
        f'<tr><td>{_e(m.label)}</td><td class="num">{_usd(m.rest_clean_cents)}</td><td class="num">{_usd(m.current_top_cents)}</td>'
        f'<td>{"best in book" if m.would_be_best else ("behind" if m.would_be_best is not None else "—")}</td>'
        f'<td class="num {_cls(m.expected_edge_pct)}">{_pct(m.expected_edge_pct)}</td></tr>' for m in makers[:4]
    ) or '<tr><td colspan="5" class="muted">No resting level clears the margin.</td></tr>'

    dn_ladders = (f'<div class="ladders"><div><div class="lad-t">Dealernet · buyers ({len(v.dn.bids)})</div>{_ladder(v.dn.bids, "bid")}</div>'
                  f'<div><div class="lad-t">Dealernet · sellers ({len(v.dn.asks)})</div>{_ladder(v.dn.asks, "ask")}</div></div>') if v.dn else ""
    tpx_lad = []
    for a in v.items:
        tpx_lad.append(f'<div><div class="lad-t">{_e(a.tpx_market.replace("TPX:", ""))} · bids ({a.tpx.bid_depth_units:,})</div>{_ladder(a.tpx.bids, "bid")}</div>'
                       f'<div><div class="lad-t">{_e(a.tpx_market.replace("TPX:", ""))} · asks ({a.tpx.ask_depth_units:,})</div>{_ladder(a.tpx.asks, "ask")}</div>')
    tpx_ladders = f'<div class="ladders">{"".join(tpx_lad)}</div>'
    flags = f'<p class="small muted flags">{_e(" · ".join(v.flags))}</p>' if v.flags else ""
    edge_hist = sparkline([r.get("edge_per_unit") for r in v.hist][-40:])
    hist_html = f'<div class="hist"><span class="small muted">Edge over past runs</span>{edge_hist}</div>' if edge_hist else ""

    return (f'<div class="desk">'
            f'<div class="desk-top"><div class="plan"><span class="sig {sig_cls}">{sig_label}</span> <span>{plan}</span></div>{hist_html}</div>'
            f'<div class="desk-grid"><div class="desk-chart">{chart}</div><div class="levels">{levels_html}</div></div>'
            f'<div class="desk-cols"><section><h4>Take now (both directions, all markets)</h4><div class="tw"><table class="mini"><thead><tr>'
            f'<th>Move</th><th class="num">Buy at</th><th class="num">Sell at</th><th class="num">Edge/unit</th><th class="num">Edge %</th><th class="num">Size</th></tr></thead>'
            f'<tbody>{move_rows}</tbody></table></div>'
            f'<h4>Post and wait</h4><div class="tw"><table class="mini"><thead><tr><th>Level</th><th class="num">Post at</th><th class="num">Current top</th><th>Queue</th><th class="num">Edge %</th></tr></thead>'
            f'<tbody>{maker_rows}</tbody></table></div></section>'
            f'<section><h4>Order books</h4>{dn_ladders}{tpx_ladders}</section></div>{flags}</div>')


def _row(v: ProductView) -> str:
    sig_cls, sig_label, _ = v.signal
    spark = sparkline([p["close"] for p in v.closes][-30:], baseline=None)
    tcg_bid = pct_of(v.tpx_bid, v.tcg)
    tcg_dn = pct_of(v.dn_bid, v.tcg)
    tcg_cell = (f'<div class="two"><span>{_usd(v.tcg)}</span><span class="small muted">TPX {tcg_bid or "—"}% · DN {tcg_dn or "—"}%</span></div>'
                if v.tcg else '<span class="muted">—</span>')
    markets = " ".join(f'<span class="chip">{"Vault" if a.tpx_is_vaulted else "Ship"}</span>' for a in v.items)

    def post_cell(d: Direction) -> str:
        m = d.post
        if not m:
            return '<div class="c num muted" data-v="-1e12">—</div>'
        gap = d.gap_cents
        if not d.near_market:
            return (f'<div class="c num post far" data-v="{_sortv(gap, -1e11)}"><b>{_usd(m.rest_clean_cents)}</b>'
                    f'<div class="small">{_pct(d.off_market_pct, dec=0)} off mkt · no fill</div></div>')
        state = ("top" if m.would_be_best else "behind") if m.current_top_cents is not None else "top"
        mkt = "Vault" if "[Vaulted]" in m.venue else "Ship"
        return (f'<div class="c num post {state}" data-v="{_sortv(gap, -1e12)}"><b>{_usd(m.rest_clean_cents)}</b>'
                f'<div class="small">{("top " + _usd(m.current_top_cents) + " · " + _usd_signed(gap)) if m.current_top_cents is not None else "empty book · you\'d be alone"}'
                f'</div><div class="small muted">{mkt} market</div></div>')

    return (
        f'<details class="prod" data-edge="{1 if v.edge is not None and v.edge > 0 else 0}" data-sig="{sig_cls}" '
        f'data-search="{_e(v.name.lower())}">'
        f'<summary class="row">'
        f'<div class="c name"><b>{_e(v.name)}</b><div class="small muted">{markets}{" · " if markets else ""}{_e(v.primary.tpx_market.replace("TPX:", ""))}</div></div>'
        f'<div class="c sig-c"><span class="sig {sig_cls}">{sig_label}</span></div>'
        f'<div class="c num edge {_cls(v.edge)}" data-v="{_sortv(v.edge)}"><b>{_usd_signed(v.edge)}</b><div class="small">{_pct(v.edge_pct)}</div></div>'
        f'{post_cell(v.buy_tpx)}{post_cell(v.buy_dn)}'
        f'<div class="c num" data-v="{_sortv(v.last)}">{_usd(v.last)}<div class="small {_cls(v.chg30)}">{_pct(v.chg30)} 30d</div></div>'
        f'<div class="c num" data-v="{_sortv(v.tpx_bid)}"><span class="bid">{_usd(v.tpx_bid)}</span> / <span class="ask">{_usd(v.tpx_ask)}</span>'
        f'<div class="small muted">{_int(v.primary.tpx.bid_depth_units)} × {_int(v.primary.tpx.ask_depth_units)}</div></div>'
        f'<div class="c num" data-v="{_sortv(v.dn_bid)}"><span class="bid">{_usd(v.dn_bid)}</span> / <span class="ask">{_usd(v.dn_ask)}</span>'
        f'<div class="small muted">{(_int(v.dn.bid_depth_units) + " × " + _int(v.dn.ask_depth_units)) if v.dn else "no price"}</div></div>'
        f'<div class="c num" data-v="{_sortv(v.vol30)}">{v.vol30:,}<div class="small muted">{v.trades30:,} trades</div></div>'
        f'<div class="c num" data-v="{_sortv(v.fv)}">{_usd(v.fv) if v.fv else "<span class=muted>no FV</span>"}</div>'
        f'<div class="c num" data-v="{_sortv(tcg_bid)}">{tcg_cell}</div>'
        f'<div class="c spk">{spark}</div>'
        f'</summary>{_desk(v)}</details>'
    )


# ---------------------------------------------------------------------------
def render(ranked: Sequence[Assessment], *, source: str, actions_enabled: bool = False,
           history: Optional[dict] = None, margin: Optional[Decimal] = None,
           generated_at: Optional[datetime] = None, refresh_s: Optional[int] = None,
           warnings: Sequence[str] = ()) -> str:
    history = history or {}
    generated_at = generated_at or datetime.now(timezone.utc)
    by_product: dict[str, list[Assessment]] = defaultdict(list)
    for a in ranked:
        by_product[a.product_name].append(a)
    views = [ProductView(p, items, margin, history) for p, items in by_product.items()]
    order = {"edge": 0, "thin": 1, "post": 2, "behind": 3, "none": 4}

    def key(v: ProductView):
        sig = v.signal[0]
        best = max((d for d in (v.buy_tpx, v.buy_dn) if d.post), key=lambda d: d.post_rank, default=None)
        gap = best.gap_cents if best and best.gap_cents is not None else -10 ** 9
        # Taker edges by $ per unit; postable levels by how likely they are to fill (TPX volume), then by gap.
        return (order[sig], -(v.edge if v.edge is not None else Decimal(-10 ** 9)) if sig in ("edge", "thin") else ZERO,
                -v.vol30, -gap if sig in ("post", "behind") else 0)
    views.sort(key=key)

    banner = ""
    if source != "live":
        banner = ('<div class="banner">Sample data: these prices come from the built-in demo fixtures, '
                  'not from TPX or Dealernet. Run without <code>--demo</code> once TPX access is set up.</div>')
    warn_html = "".join(f'<div class="banner warn">{_e(w)}</div>' for w in warnings)
    actions = ('<span class="pill on">Actions: ON</span>' if actions_enabled
               else '<span class="pill" title="Set ACTIONS_ENABLED=true to allow features that act on a marketplace">Actions: OFF (view only)</span>')
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    live = ('<span class="live"><i></i>LIVE</span>' if source == "live" else '<span class="live demo"><i></i>DEMO</span>')
    data = json.dumps({"generated_at": generated_at.isoformat(), "source": source})
    rows = "".join(_row(v) for v in views)
    return TEMPLATE.format(
        meta_refresh=meta_refresh, generated=_e(generated_at.strftime("%b %d, %Y · %H:%M UTC")), live=live,
        actions=actions, banner=banner + warn_html, ticker=_ticker(views), metrics=_metrics(views, margin, generated_at),
        margin=f"{Decimal(margin) * 100:.0f}%" if margin is not None else "8%",
        rows=rows or '<p class="muted">No products.</p>', n=len(views), data=data,
        refresh_note=(f"Refreshes every {refresh_s} s." if refresh_s else "Re-run <code>edge dashboard</code> to refresh."),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta_refresh}<title>TPX Dealernet Edges</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
/* Terminal: dark-first, numbers in mono, one amber accent for the thing that needs your eyes. */
:root{{--bg:#0d1117;--panel:#161b22;--panel-2:#1c232d;--ink:#e6edf3;--muted:#8b98a8;--line:#2a3340;--line-2:#3a4656;
--pos:#3fb950;--neg:#f85149;--flat:#8b98a8;--accent:#f0b429;--accent-ink:#1a1200;
--s1:#58a6ff;--s2:#f0883e;--s3:#bc8cff;--s4:#39d2c0;--bid:#3fb950;--ask:#f85149;--fv:#8b98a8;--banner:#2a2410;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
color-scheme:dark}}
@media (prefers-color-scheme:light){{:root:not([data-theme="dark"]){{--bg:#f3f5f8;--panel:#ffffff;--panel-2:#eef1f5;--ink:#141a22;--muted:#5d6b7c;
--line:#d9dfe7;--line-2:#c3ccd7;--pos:#1a7f37;--neg:#cf222e;--flat:#5d6b7c;--accent:#b7791f;--accent-ink:#ffffff;
--s1:#0969da;--s2:#bc4c00;--s3:#8250df;--s4:#0f8f80;--bid:#1a7f37;--ask:#cf222e;--fv:#5d6b7c;--banner:#fff4d6;color-scheme:light}}}}
:root[data-theme="light"]{{--bg:#f3f5f8;--panel:#ffffff;--panel-2:#eef1f5;--ink:#141a22;--muted:#5d6b7c;
--line:#d9dfe7;--line-2:#c3ccd7;--pos:#1a7f37;--neg:#cf222e;--flat:#5d6b7c;--accent:#b7791f;--accent-ink:#ffffff;
--s1:#0969da;--s2:#bc4c00;--s3:#8250df;--s4:#0f8f80;--bid:#1a7f37;--ask:#cf222e;--fv:#5d6b7c;--banner:#fff4d6;color-scheme:light}}
*{{box-sizing:border-box}}html,body{{margin:0}}
body{{background:var(--bg);color:var(--ink);font:14px/1.45 var(--sans);padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:0 48px}}
main{{max-width:1440px;margin:0 auto}}
h1{{font-size:18px;font-weight:600;margin:0;letter-spacing:.01em}}h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:26px 0 10px;font-weight:600}}
h4{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 6px;font-weight:600}}
.num,.mono,.m-v,.lv-v,.tk-p,.tk-c,.ladder,.mini td.num{{font-family:var(--mono);font-variant-numeric:tabular-nums}}
.num{{text-align:right;white-space:nowrap}}
.muted{{color:var(--muted)}}.small{{font-size:12px}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.flat{{color:var(--flat)}}
.bid{{color:var(--bid)}}.ask{{color:var(--ask)}}
.top{{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;justify-content:space-between;padding:14px 0 10px;border-bottom:1px solid var(--line)}}
.top-l{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.live{{font:600 11px/1 var(--mono);letter-spacing:.12em;color:var(--pos);display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border:1px solid var(--pos);border-radius:3px}}
.live i{{width:7px;height:7px;border-radius:50%;background:var(--pos);animation:blink 1.6s infinite}}
.live.demo{{color:var(--accent);border-color:var(--accent)}}.live.demo i{{background:var(--accent)}}
@keyframes blink{{50%{{opacity:.25}}}}
.pill{{display:inline-block;border:1px solid var(--line-2);border-radius:3px;padding:4px 10px;font:500 11px/1 var(--mono);letter-spacing:.06em;color:var(--muted);text-transform:uppercase}}
.pill.on{{border-color:var(--neg);color:var(--neg)}}
.banner{{background:var(--banner);border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin:14px 0 0}}
/* ticker tape */
.tape{{overflow:hidden;border-bottom:1px solid var(--line);background:var(--panel);margin:0 calc(-1 * max(16px,env(safe-area-inset-left,0px))) 0 calc(-1 * max(16px,env(safe-area-inset-right,0px)));padding:0 16px}}
.tape-track{{display:inline-flex;gap:0;white-space:nowrap;animation:tape 90s linear infinite;will-change:transform}}
.tape:hover .tape-track{{animation-play-state:paused}}
@keyframes tape{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tk{{display:inline-flex;gap:10px;align-items:baseline;padding:9px 18px;border-right:1px solid var(--line);font-size:12px}}
.tk-n{{color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:500}}.tk-p{{font-weight:600}}
@media (prefers-reduced-motion:reduce){{.tape-track{{animation:none;white-space:normal;flex-wrap:wrap}}.tape-track .tk:nth-child(n+1000){{display:none}}.live i{{animation:none}}}}
/* metrics */
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-top:16px}}
.m{{background:var(--panel);padding:12px 14px;min-width:0}}.m.hero{{background:var(--panel-2)}}
.m-l{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.m-v{{font-size:26px;font-weight:600;line-height:1.2;margin:4px 0 2px}}.m.hero .m-v{{font-size:34px}}
.m-s{{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
/* screener */
.controls{{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:8px}}
.controls input[type=search]{{padding:7px 10px;border:1px solid var(--line-2);border-radius:4px;background:var(--panel);color:var(--ink);min-width:220px;font:inherit}}
.controls label{{display:inline-flex;gap:6px;align-items:center;cursor:pointer}}
.screener{{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow-x:auto}}
.hdr,.row{{display:grid;grid-template-columns:minmax(200px,2fr) 96px 120px 200px 200px 108px 168px 168px 96px 86px 120px 96px;gap:0 14px;align-items:center;min-width:1760px;padding:0 14px}}
.hdr-s{{display:block;font-size:10px;letter-spacing:.02em;text-transform:none;color:var(--muted);font-weight:400}}
.post b{{font-size:14px}}.post.top b{{color:var(--pos)}}.post.behind b{{color:var(--ink)}}.post.behind .small{{color:var(--neg)}}.post.top .small{{color:var(--muted)}}.post.far b{{color:var(--muted);font-weight:500}}.post.far .small{{color:var(--muted)}}
.hdr{{position:sticky;top:0;z-index:2;background:var(--panel-2);border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}}
.hdr div{{padding:9px 0;cursor:pointer;user-select:none;white-space:nowrap}}.hdr div:hover{{color:var(--ink)}}
.hdr .num{{text-align:right}}.hdr .sorted::after{{content:" ▾";color:var(--accent)}}.hdr .sorted.asc::after{{content:" ▴"}}
details.prod{{border-bottom:1px solid var(--line)}}details.prod:last-child{{border-bottom:0}}
summary.row{{list-style:none;cursor:pointer;padding-top:10px;padding-bottom:10px;line-height:1.3}}
summary.row::-webkit-details-marker{{display:none}}summary.row:hover{{background:var(--panel-2)}}
details[open] summary.row{{background:var(--panel-2);border-left:3px solid var(--accent);padding-left:11px}}
.c{{min-width:0}}.name b{{font-weight:600}}.name .small{{margin-top:2px}}
.chip{{display:inline-block;font:500 10px/1 var(--mono);padding:2px 5px;border:1px solid var(--line-2);border-radius:3px;color:var(--muted);margin-right:4px;letter-spacing:.04em}}
.edge b{{font-size:15px}}
.sig{{display:inline-block;font:600 10px/1 var(--mono);letter-spacing:.1em;padding:5px 7px;border-radius:3px;border:1px solid var(--line-2);color:var(--muted);white-space:nowrap}}
.sig.edge{{background:var(--pos);color:#04200b;border-color:var(--pos)}}.sig.thin{{color:var(--pos);border-color:var(--pos)}}
.sig.post{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}.sig.behind{{color:var(--accent);border-color:var(--accent)}}
.spk{{display:flex;justify-content:flex-end}}.spark{{width:96px;height:26px}}
.spark-pos{{stroke:var(--pos)}}.spark-neg{{stroke:var(--neg)}}.spark-pos-dot{{fill:var(--pos)}}.spark-neg-dot{{fill:var(--neg)}}
.two{{display:flex;flex-direction:column;align-items:flex-end}}
/* desk */
.desk{{padding:12px 14px 18px;background:var(--bg);border-top:1px solid var(--line)}}
.desk-top{{display:flex;flex-wrap:wrap;gap:10px 24px;align-items:center;justify-content:space-between}}
.plan{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:14px}}.hist{{display:flex;gap:8px;align-items:center}}
.desk-grid{{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,2fr);gap:14px;margin-top:12px}}
.desk-chart{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px 8px;min-width:0}}
.chart{{width:100%;height:auto;display:block}}.grid{{stroke:var(--line);stroke-width:1}}.axis{{fill:var(--muted);font-size:10px;font-family:var(--mono)}}
.dot{{stroke:var(--panel);stroke-width:2}}
.ref-bid{{stroke:var(--bid);stroke-dasharray:5 4;stroke-width:1.5}}.ref-ask{{stroke:var(--ask);stroke-dasharray:5 4;stroke-width:1.5}}
.ref-fv{{stroke:var(--fv);stroke-dasharray:2 3;stroke-width:1.5}}.ref-tcg{{stroke:var(--s3);stroke-dasharray:7 3;stroke-width:1.5}}
.legend{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11px;color:var(--muted);margin-top:6px}}
.key i{{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;border-radius:2px}}
.k-bid{{background:var(--bid)}}.k-ask{{background:var(--ask)}}.k-fv{{background:var(--fv)}}.k-tcg{{background:var(--s3)}}
.levels{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden;align-content:start}}
.lv{{background:var(--panel);padding:10px 12px;min-width:0}}.lv-l{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
.lv-v{{font-size:17px;font-weight:600;margin:3px 0 1px;white-space:nowrap}}.lv-s{{font-size:11px;color:var(--muted)}}
.desk-cols{{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:14px 24px;margin-top:4px}}
.tw{{overflow-x:auto}}.mini{{border-collapse:collapse;width:100%;font-size:13px}}
.mini th,.mini td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
.mini th{{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}}.mini th.num{{text-align:right}}
.ladders{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px}}
.lad-t{{font-size:11px;color:var(--muted);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.ladder{{border-collapse:collapse;width:100%;font-size:12px}}.ladder th,.ladder td{{padding:3px 6px;border-bottom:1px solid var(--line);text-align:right}}
.ladder th:first-child,.ladder td:first-child{{text-align:left}}.ladder th{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
.ladder.bid td:first-child{{color:var(--bid)}}.ladder.ask td:first-child{{color:var(--ask)}}
.flags{{margin:10px 0 0}}
footer{{margin-top:28px}}code{{font-family:var(--mono);font-size:12px}}
@media (max-width:900px){{.desk-grid,.desk-cols{{grid-template-columns:1fr}}.m-v{{font-size:22px}}.m.hero .m-v{{font-size:28px}}}}
input:focus-visible,summary:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
/* big-screen mode: #tv */
html.tv body{{font-size:16px}}html.tv main{{max-width:none}}html.tv .controls,html.tv footer,html.tv .banner{{display:none}}
html.tv .m-v{{font-size:32px}}html.tv .m.hero .m-v{{font-size:42px}}html.tv .tk{{font-size:15px;padding:12px 22px}}
html.tv summary.row{{padding-top:13px;padding-bottom:13px}}html.tv .edge b,html.tv .post b{{font-size:18px}}html.tv .small{{font-size:13px}}
html.tv h2{{margin-top:18px}}
</style></head><body><main>
<div class="top"><div class="top-l"><h1>TPX ↔ Dealernet edge terminal</h1>{live}<span class="muted small">{generated}</span></div>
<div class="top-l"><span class="muted small">target {margin} on capital · all edges after fees &amp; shipping</span>{actions}</div></div>
{ticker}
{banner}
<div class="metrics">{metrics}</div>
<h2>Screener · {n} products</h2>
<div class="controls"><input type="search" id="q" placeholder="Filter products" aria-label="Filter products">
<label><input type="checkbox" id="edgesOnly"> Edges only</label><label><input type="checkbox" id="withDn"> Has Dealernet price</label>
<span class="muted small">Click a column to sort · click a row for the trade desk · prices are clean (pre-fee), edges are all-in · add <code>#tv</code> to the link for the big-screen view.</span></div>
<div class="screener"><div class="hdr" id="hdr">
<div>Product</div><div>Signal</div><div class="num" title="Hit both books right now">Take now</div>
<div class="num" title="Highest TPX bid you can post and still clear the target margin when you sell into the Dealernet bid">Post TPX bid<br><span class="hdr-s">buy TPX → sell DN bid</span></div>
<div class="num" title="Lowest TPX ask you can post and still clear the target margin when you buy from the Dealernet ask">Post TPX ask<br><span class="hdr-s">buy DN ask → sell TPX</span></div>
<div class="num">Last · 30d</div><div class="num">TPX bid / ask</div>
<div class="num">DN bid / ask</div><div class="num">TPX vol 30d</div><div class="num">Fair value</div>
<div class="num" title="High bid as % of TCGplayer market price">vs TCGplayer mkt</div><div class="num">30d trend</div></div>
<div id="rows">{rows}</div></div>
<footer class="muted small">View only. This page never places, lists or cancels anything. {refresh_note} The edge-over-runs line fills in as the dashboard is re-run.</footer>
</main>
<script id="meta" type="application/json">{data}</script>
<script>
(function(){{
if(location.hash==='#tv')document.documentElement.classList.add('tv');
window.addEventListener('hashchange',function(){{document.documentElement.classList.toggle('tv',location.hash==='#tv');}});
var q=document.getElementById('q'),eo=document.getElementById('edgesOnly'),wd=document.getElementById('withDn'),box=document.getElementById('rows');
try{{eo.checked=localStorage.getItem('edgesOnly')==='1';wd.checked=localStorage.getItem('withDn')==='1';}}catch(e){{}}
function rows(){{return Array.prototype.slice.call(box.querySelectorAll('details.prod'));}}
function apply(){{var t=q.value.toLowerCase();rows().forEach(function(r){{
var ok=(!t||(r.dataset.search||'').indexOf(t)>=0)&&(!eo.checked||r.dataset.edge==='1')&&(!wd.checked||r.dataset.sig!=='none'||r.querySelector('.c:nth-child(6) .small').textContent!=='no price');
r.hidden=!ok;}});try{{localStorage.setItem('edgesOnly',eo.checked?'1':'0');localStorage.setItem('withDn',wd.checked?'1':'0');}}catch(e){{}}}}
q.addEventListener('input',apply);eo.addEventListener('change',apply);wd.addEventListener('change',apply);apply();
var hdr=document.getElementById('hdr');Array.prototype.forEach.call(hdr.children,function(h,i){{var dir=-1;h.addEventListener('click',function(){{
dir=-dir;Array.prototype.forEach.call(hdr.children,function(x){{x.classList.remove('sorted','asc');}});h.classList.add('sorted');if(dir>0)h.classList.add('asc');
var rs=rows();rs.sort(function(a,b){{var x=a.querySelector('summary').children[i],y=b.querySelector('summary').children[i];
var vx=x.dataset.v,vy=y.dataset.v;if(vx!==undefined&&vy!==undefined)return (parseFloat(vx)-parseFloat(vy))*dir;
return x.textContent.localeCompare(y.textContent)*dir;}});rs.forEach(function(r){{box.appendChild(r);}});}});}});
}})();
</script></body></html>"""
