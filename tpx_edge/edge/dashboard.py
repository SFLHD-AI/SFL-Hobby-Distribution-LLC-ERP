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
import re
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
# plain-English page pieces. Ross is not a trader: every row says in words what
# to buy, where, at what price, and what he makes.
def _ticker(views: list[ProductView]) -> str:
    chips = []
    for v in views:
        px = v.last or v.fv or v.dn_bid or v.tpx_bid
        chips.append(f'<span class="tk"><span class="tk-n">{_e(v.short)}</span>'
                     f'<span class="tk-p">{_usd(px)}</span>'
                     f'<span class="tk-c {_cls(v.chg30)}">{_pct(v.chg30)}</span></span>')
    strip = "".join(chips) or '<span class="tk muted">no products</span>'
    return f'<div class="tape" aria-label="ticker"><div class="tape-track">{strip}{strip}</div></div>'


def _unit(name: str) -> str:
    words = set(re.findall(r"[a-z]+", name.lower()))
    for word in ("case", "pack", "tin", "display", "collection", "bundle", "box"):
        if word in words:
            return "box" if word == "collection" else word
    return "unit"


def _venue_word(venue: str) -> str:
    return "Dealernet" if venue.startswith("Dealernet") else "TPX"


def _market_word(venue: str) -> str:
    return "TPX vault market" if "[Vaulted]" in venue else "TPX shipped market"


class Story:
    """What the page says about one product."""

    def __init__(self, v: ProductView):
        self.v = v
        self.unit = _unit(v.name)
        self.cls, self.badge, self.headline, self.steps, self.profit, self.why = self._build()

    @property
    def is_opportunity(self) -> bool:
        return self.cls in ("edge", "thin", "post", "behind")

    @property
    def compact(self) -> str:
        """One line for a tile."""
        v = self.v
        if self.cls in ("edge", "thin"):
            a = v.arb
            return (f"▲ Buy {_venue_word(a.buy_venue)} {_usd(a.buy_clean_cents)} → sell {_venue_word(a.sell_venue)} "
                    f"{_usd(a.sell_clean_cents)} · +{_usd(a.edge_per_unit_cents)}/{self.unit} now")
        best = max((d for d in (v.buy_tpx, v.buy_dn) if d.post), key=lambda d: d.post_rank, default=None)
        if self.cls == "post" and best and best.post:
            m = best.post
            if m.side == "bid":
                return f"◆ Post TPX buy {_usd(m.rest_clean_cents)} → sell DN {_usd(m.counter_clean_cents)} · +{_usd(m.expected_edge_cents)}/{self.unit}"
            return f"◆ Post TPX sell {_usd(m.rest_clean_cents)} → buy DN {_usd(m.counter_clean_cents)} · +{_usd(m.expected_edge_cents)}/{self.unit}"
        if self.cls == "behind" and best and best.post:
            return f"Almost: TPX {best.post.side} {_usd(best.post.rest_clean_cents)} would work, someone is at {_usd(best.post.current_top_cents)}"
        if v.dn is None:
            return "No Dealernet listing matched"
        if best and best.post and not best.near_market:
            return "No profit: fees & shipping eat the gap"
        return "No profit: prices too close"

    def _build(self):
        v = self.v
        sig = v.signal[0]
        u = self.unit
        if sig in ("edge", "thin"):
            a = v.arb
            buy, sell = _venue_word(a.buy_venue), _venue_word(a.sell_venue)
            steps = [f"Buy on {buy} at {_usd(a.buy_clean_cents)}", f"Sell on {sell} at {_usd(a.sell_clean_cents)}"]
            profit = (f"You make about {_usd(a.edge_per_unit_cents)} per {u} ({_pct(a.edge_pct, False)}) after fees and shipping, "
                      f"on up to {a.max_size} {u}{'' if a.max_size == 1 else 's'} at these prices.")
            why = (f"{sell} is paying more than {buy} is asking right now, so both sides of the trade are already there. "
                   "These prices can move fast.")
            return sig, ("Ready now" if sig == "edge" else "Small profit now"), f"Buy on {buy}, sell on {sell}", steps, profit, why
        best = max((d for d in (v.buy_tpx, v.buy_dn) if d.post), key=lambda d: d.post_rank, default=None)
        if sig == "post":
            m = best.post
            where = _market_word(m.venue)
            if m.side == "bid":
                steps = [f"Post a buy order on TPX at {_usd(m.rest_clean_cents)} ({where})",
                         f"When it fills, sell to Dealernet at {_usd(m.counter_clean_cents)} (their current buying price)"]
                headline = "Buy on TPX, sell to Dealernet"
                top = (f"Your {_usd(m.rest_clean_cents)} would be the highest buy order on TPX "
                       + (f"(the next best is {_usd(m.current_top_cents)})" if m.current_top_cents else "(nobody else is bidding there yet)")
                       + ", so you are first in line when someone sells.")
                why = f"Dealernet is paying {_usd(m.counter_clean_cents)} per {u}, more than TPX sellers are getting. {top}"
            else:
                steps = [f"Post a sell order on TPX at {_usd(m.rest_clean_cents)} ({where})",
                         f"When it fills, buy from Dealernet at {_usd(m.counter_clean_cents)} (their current selling price) to cover it"]
                headline = "Sell on TPX, buy from Dealernet"
                top = (f"Your {_usd(m.rest_clean_cents)} would be the lowest sell order on TPX "
                       + (f"(the next best is {_usd(m.current_top_cents)})" if m.current_top_cents else "(nobody else is selling there yet)")
                       + ", so you are first in line when someone buys.")
                why = f"Dealernet sells this for {_usd(m.counter_clean_cents)} per {u}, less than TPX buyers will pay. {top}"
            profit = f"You make about {_usd(m.expected_edge_cents)} per {u} ({_pct(m.expected_edge_pct, False)}) after fees and shipping, once your order fills."
            return "post", "Post and wait", headline, steps, profit, why
        if sig == "behind":
            m = best.post
            verb = "buy" if m.side == "bid" else "sell"
            steps = [f"A TPX {verb} order at {_usd(m.rest_clean_cents)} would make {_pct(v.margin * 100, False)}",
                     f"But someone is already at {_usd(m.current_top_cents)}, ahead of you"]
            why = f"Watch this one. If the TPX {verb} orders thin out, it becomes a trade."
            return "behind", "Almost", "Someone is ahead of you", steps, "No profit yet.", why
        # Nothing to do: say why in one line.
        if v.dn is None:
            return "none", "No Dealernet price", "", [], "", "No Dealernet listing matched this product."
        if best and best.post and not best.near_market:
            m = best.post
            verb = "sell" if m.side == "ask" else "buy"
            return ("none", "No profit right now", "", [], "",
                    f"Fees and shipping eat the gap: you would have to {verb} at {_usd(m.rest_clean_cents)} on TPX, "
                    f"but it trades around {_usd(v.ref)}.")
        if v.edge is not None:
            return ("none", "No profit right now", "", [], "",
                    f"Prices are too close. Dealernet pays {_usd(v.dn_bid)} and sells at {_usd(v.dn_ask)}; "
                    f"TPX pays {_usd(v.tpx_bid)} and sells at {_usd(v.tpx_ask)}.")
        return "none", "No prices to trade against", "", [], "", "One side has no buyers or sellers right now."


def _compact(n, money: bool = True) -> str:
    """$2.9M / 23.5K style figures for tile footers."""
    if n is None:
        return "—"
    v = float(n) / (100 if money else 1)
    sign = "$" if money else ""
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= cutoff:
            return f"{sign}{v / cutoff:.1f}{suffix}"
    return f"{sign}{v:,.0f}" if money else f"{sign}{v:,.0f}"


def _meta(v: ProductView) -> dict:
    return v.primary.tpx.raw.get("product_meta") or {}


def _group(v: ProductView) -> str:
    t = str(_meta(v).get("asset_type") or "").lower()
    return "sports" if "wax" in t else "tcg"


def _series_label(v: ProductView) -> str:
    m = _meta(v)
    if _group(v) == "tcg":
        bits = [m.get("brand"), m.get("set")]
    else:
        bits = [m.get("set"), m.get("format")]
    return " · ".join(str(b) for b in bits if b)


def _initials(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in ("the", "of", "and")]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def squarify(weights: list[tuple[str, float]], x: float, y: float, w: float, h: float) -> dict[str, tuple[float, float, float, float]]:
    """Squarified treemap: rects (in the units of w/h) for weights sorted descending."""
    out: dict[str, tuple[float, float, float, float]] = {}
    total = sum(a for _, a in weights) or 1.0
    scaled = [(k, a / total * w * h) for k, a in sorted(weights, key=lambda t: -t[1])]
    _squarify_layout(scaled, x, y, w, h, out)
    return out


def _worst(row: list[float], length: float) -> float:
    s = sum(row)
    if not s or not length:
        return float("inf")
    return max(length * length * max(row) / (s * s), s * s / (length * length * min(row)))


def _squarify_layout(items, x, y, w, h, out) -> None:
    if not items:
        return
    if len(items) == 1:
        out[items[0][0]] = (x, y, w, h)
        return
    vertical = w >= h  # lay a column along the short side
    length = h if vertical else w
    row: list = []
    rest = list(items)
    while rest:
        cand = row + [rest[0]]
        if not row or _worst([c[1] for c in cand], length) <= _worst([c[1] for c in row], length):
            row = cand
            rest = rest[1:]
        else:
            break
    area = sum(c[1] for c in row)
    if vertical:
        cw = area / h
        yy = y
        for k, a in row:
            hh = a / cw
            out[k] = (x, yy, cw, hh)
            yy += hh
        _squarify_layout(rest, x + cw, y, w - cw, h, out)
    else:
        rh = area / w
        xx = x
        for k, a in row:
            ww = a / rh
            out[k] = (xx, y, ww, rh)
            xx += ww
        _squarify_layout(rest, x, y + rh, w, h - rh, out)


def tile_chart(closes: list[dict], up: bool) -> str:
    """Price line with a soft fill and volume bars behind it, stretched to the tile."""
    pts = [(c.get("close"), c.get("volume") or 0) for c in closes if c.get("close") is not None][-30:]
    if len(pts) < 2:
        return '<svg class="tchart" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="price trend"><text x="150" y="55" text-anchor="middle" class="nodata">No price history</text></svg>'
    prices = [p for p, _ in pts]
    vols = [v for _, v in pts]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1
    n = len(pts)
    step = 300 / (n - 1)
    xs = [i * step for i in range(n)]
    ys = [92 - (p - lo) / span * 74 for p in prices]
    vmax = max(vols) or 1
    bw = max(step * 0.7, 2)
    bars = "".join(f'<rect x="{x - bw / 2:.1f}" y="{100 - v / vmax * 40:.1f}" width="{bw:.1f}" height="{v / vmax * 40:.1f}"/>'
                   for x, v in zip(xs, vols) if v)
    line = " ".join(f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}' for i, (x, y) in enumerate(zip(xs, ys)))
    fill = f'{line} L300,100 L0,100 Z'
    cls = "up" if up else "down"
    return (f'<svg class="tchart {cls}" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="price trend">'
            f'<g class="vol">{bars}</g><path class="area" d="{fill}"/><path class="line" d="{line}" vector-effect="non-scaling-stroke"/></svg>')


def _strip(st: Story) -> str:
    """The decision strip: the venue-vs-venue comparison this group cares about,
    then one line on what (if anything) to do."""
    v = st.v
    g = _group(v)
    dn_raw = v.dn.raw if v.dn else {}
    case = ""
    if dn_raw.get("case_bid") or dn_raw.get("case_ask"):
        bpc = dn_raw.get("boxes_per_case")
        case = (f'<span class="pill case" title="Dealernet case offers, priced per box; a case trades at a premium and needs a full case to fill">'
                f'Case{f" ×{bpc}" if bpc else ""}: pays {_usd(dn_raw.get("case_bid"))} · sells {_usd(dn_raw.get("case_ask"))}</span>')
    if g == "tcg":
        tpx_pct, dn_pct = pct_of(v.tpx_ask, v.tcg), pct_of(v.dn_ask, v.tcg)
        best = None
        if v.tpx_ask and v.dn_ask:
            best = "tpx" if v.tpx_ask <= v.dn_ask else "dn"
        elif v.tpx_ask or v.dn_ask:
            best = "tpx" if v.tpx_ask else "dn"
        head = (f'<span class="lbl">vs TCGplayer {_usd(v.tcg)}</span>' if v.tcg
                else '<span class="lbl muted" title="Add TCGPLAYER_PUBLIC_KEY / PRIVATE_KEY to compare against market">TCGplayer market: keys not set</span>')
        cmp_ = (head
                + f'<span class="pill {"best" if best == "tpx" else ""}">Buy TPX {_usd(v.tpx_ask)}{f" · {tpx_pct}%" if tpx_pct is not None else ""}</span>'
                + f'<span class="pill {"best" if best == "dn" else ""}">Buy DN {_usd(v.dn_ask)}{f" · {dn_pct}%" if dn_pct is not None else ""}</span>'
                + case)
    else:
        bids = [b for b in (v.tpx_bid, v.dn_bid) if b]
        asks = [a for a in (v.tpx_ask, v.dn_ask) if a]
        hb, la = (max(bids) if bids else None), (min(asks) if asks else None)

        def cell(label, val, best_):
            return f'<span class="pill {"best" if best_ else ""}">{label} {_usd(val)}</span>'
        cmp_ = ('<span class="lbl">TPX</span>' + cell("pays", v.tpx_bid, v.tpx_bid and v.tpx_bid == hb) + cell("sells", v.tpx_ask, v.tpx_ask and v.tpx_ask == la)
                + '<span class="lbl">DN</span>' + cell("pays", v.dn_bid, v.dn_bid and v.dn_bid == hb) + cell("sells", v.dn_ask, v.dn_ask and v.dn_ask == la)
                + case)
    act = st.compact
    return f'<div class="strip"><div class="cmp">{cmp_}</div><div class="act {st.cls}">{act}</div></div>'


def _tile(st: Story, rect: tuple[float, float, float, float], size: str) -> str:
    v = st.v
    x, y, w, h = rect
    px = v.last or v.fv or v.tpx_bid or v.dn_bid
    up = (v.chg30 or ZERO) >= 0
    m = _meta(v)
    img = (f'<img class="thumb" src="{m["image"]}" alt="">' if m.get("image")
           else f'<span class="thumb ph">{_e(_initials(v.name))}</span>')
    chg = (f'<span class="chg {"up" if up else "down"}">{"↗" if up else "↘"} {_pct(abs(v.chg30), False, 2)}</span>'
           if v.chg30 is not None else '<span class="chg flat">—</span>')
    notional = sum((c.get("vwap") or c.get("close") or 0) * (c.get("volume") or 0) for c in v.closes) or ((v.vwap30 or v.last or 0) * v.vol30)
    return (
        f'<article class="tile {size} {st.cls}" style="left:{x:.3f}%;top:{y:.3f}%;width:{w:.3f}%;height:{h:.3f}%" data-search="{_e(v.name.lower())}">'
        f'<header><div class="who">{img}<div class="names"><div class="series">{_e(_series_label(v))}</div><div class="name">{_e(v.name)}</div></div></div>'
        f'<div class="px"><div class="price">{_usd(px)}</div>{chg}</div></header>'
        f'{tile_chart(v.closes, up)}'
        f'{_strip(st)}'
        f'<footer><span><i>Volume</i>{_compact(notional)}</span><span><i>Last sale</i><b class="{"up" if up else "down"}">{_usd(v.last)}</b></span><span><i>30D sold</i>{_compact(v.vol30, False)}</span></footer>'
        f'</article>'
    )


def _board(stories: list[Story], aspect: float) -> str:
    """One treemap: tiles sized by 30-day TPX dollar volume (floored so every product shows)."""
    if not stories:
        return '<div class="empty">No products in this group.</div>'
    weights = []
    for st in stories:
        v = st.v
        notional = sum((c.get("vwap") or c.get("close") or 0) * (c.get("volume") or 0) for c in v.closes) or ((v.vwap30 or v.last or 0) * v.vol30)
        weights.append((v.name, float(notional)))
    total = sum(a for _, a in weights) or 1.0
    # Floor so every product shows; cap so one runaway product cannot own the board.
    weights = [(k, min(max(a, total * 0.03), total * 0.22)) for k, a in weights]
    rects = squarify(weights, 0, 0, 100, 100 * aspect)
    by_name = {st.v.name: st for st in stories}
    tiles = []
    for name, (x, y, w, h) in rects.items():
        # size class from the rough pixel box on a 1400px-wide board
        wpx, hpx = w / 100 * 1400, h / (100 * aspect) * 1400 * aspect
        size = "xl" if wpx >= 420 and hpx >= 300 else "l" if wpx >= 280 and hpx >= 190 else "m" if wpx >= 160 and hpx >= 130 else "s"
        tiles.append(_tile(by_name[name], (x, y / aspect, w, h / aspect), size))
    return f'<div class="board" style="aspect-ratio:{1 / aspect:.4f}">{"".join(tiles)}</div>'


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
    stories = [Story(v) for v in views]
    tcg = [s for s in stories if _group(s.v) == "tcg"]
    sports = [s for s in stories if _group(s.v) == "sports"]
    now = [s for s in stories if s.cls in ("edge", "thin")]
    post = [s for s in stories if s.cls == "post"]

    banner = ""
    if source != "live":
        banner = ('<div class="banner">Sample data: these prices come from the built-in demo fixtures, '
                  'not from TPX or Dealernet. Run without <code>--demo</code> once TPX access is set up.</div>')
    warn_html = "".join(f'<div class="banner warn">{_e(w)}</div>' for w in warnings)
    actions = ('<span class="pill on">Actions: ON</span>' if actions_enabled
               else '<span class="pill" title="Set ACTIONS_ENABLED=true to allow features that act on a marketplace">Actions: OFF (view only)</span>')
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    live = ('<span class="live"><i></i>LIVE</span>' if source == "live" else '<span class="live demo"><i></i>DEMO</span>')
    data = json.dumps({"generated_at": generated_at.isoformat(), "source": source,
                       "products": [v.name for v in views]})

    def aspect_for(n: int) -> float:
        return 0.42 if n <= 4 else 0.5 if n <= 8 else 0.58

    sections = []
    if tcg:
        sections.append(f'<section><h2>Pokémon &amp; One Piece <span class="sub">which venue is cheaper to buy, judged against TCGplayer market</span></h2>{_board(tcg, aspect_for(len(tcg)))}</section>')
    if sports:
        sections.append(f'<section><h2>Sports <span class="sub">Dealernet pays / sells vs TPX pays / sells</span></h2>{_board(sports, aspect_for(len(sports)))}</section>')
    if not sections:
        sections.append('<p class="muted">No products.</p>')
    counts = (f'<span class="count pos">{len(now)} ready now</span><span class="count warn">{len(post)} post &amp; wait</span>'
              f'<span class="count muted">{len(stories) - len(now) - len(post)} nothing to do</span>')
    return TEMPLATE.format(
        meta_refresh=meta_refresh, generated=_e(generated_at.strftime("%b %d, %Y · %H:%M UTC")), live=live,
        actions=actions, banner=banner + warn_html, ticker=_ticker(views), counts=counts,
        margin=f"{Decimal(margin) * 100:.0f}%" if margin is not None else "8%", sections="".join(sections), data=data,
        n_sections={1: "one", 2: "two"}.get(len([x for x in (tcg, sports) if x]), "one"),
        refresh_note=(f"Refreshes every {refresh_s} s." if refresh_s else "Re-run <code>edge dashboard</code> to refresh."),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta_refresh}<title>TPX Dealernet Edges</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
/* Heat-board: near-black ground, charcoal tiles, red/green for direction, one amber for "do this". */
:root{{--bg:#0b0b0c;--tile:#1f1f22;--tile-2:#26262a;--ink:#f2f2f3;--muted:#8d8d94;--line:#2c2c31;
--up:#3ddc84;--down:#ff4d6a;--flat:#8d8d94;--accent:#f5b63a;--accent-ink:#1a1200;--best:#1f3a2a;--banner:#2a2410;
--sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:dark}}
@media (prefers-color-scheme:light){{:root:not([data-theme="dark"]){{--bg:#eef0f3;--tile:#ffffff;--tile-2:#f3f4f7;--ink:#141518;--muted:#656a75;--line:#dcdfe5;
--up:#15934f;--down:#d8243f;--flat:#656a75;--accent:#b7791f;--accent-ink:#ffffff;--best:#dff3e6;--banner:#fff4d6;color-scheme:light}}}}
:root[data-theme="light"]{{--bg:#eef0f3;--tile:#ffffff;--tile-2:#f3f4f7;--ink:#141518;--muted:#656a75;--line:#dcdfe5;
--up:#15934f;--down:#d8243f;--flat:#656a75;--accent:#b7791f;--accent-ink:#ffffff;--best:#dff3e6;--banner:#fff4d6;color-scheme:light}}
*{{box-sizing:border-box}}html,body{{margin:0}}
body{{background:var(--bg);color:var(--ink);font:14px/1.4 var(--sans);padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:0 40px;font-variant-numeric:tabular-nums}}
main{{max-width:1600px;margin:0 auto}}
h1{{font-size:18px;font-weight:700;margin:0}}
h2{{font-size:15px;font-weight:700;margin:18px 0 8px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}}
h2 .sub{{font-size:12px;font-weight:500;color:var(--muted)}}
.muted{{color:var(--muted)}}.pos,.up{{color:var(--up)}}.neg,.down{{color:var(--down)}}.flat{{color:var(--flat)}}
.top{{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;justify-content:space-between;padding:12px 0 10px}}
.top-l{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.live{{font:600 11px/1 var(--sans);letter-spacing:.12em;color:var(--up);display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border:1px solid var(--up);border-radius:3px}}
.live i{{width:7px;height:7px;border-radius:50%;background:var(--up);animation:blink 1.6s infinite}}
.live.demo{{color:var(--accent);border-color:var(--accent)}}.live.demo i{{background:var(--accent)}}
@keyframes blink{{50%{{opacity:.25}}}}
.pill{{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:3px 8px;font-size:11px;font-weight:600;color:var(--muted);white-space:nowrap}}
.pill.on{{border-color:var(--down);color:var(--down)}}
.count{{font-size:12px;font-weight:600;margin-left:12px}}.count.warn{{color:var(--accent)}}
.banner{{background:var(--banner);border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin:10px 0 0}}
/* tape */
.tape{{overflow:hidden;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:0 calc(-1 * max(16px,env(safe-area-inset-left,0px))) 0 calc(-1 * max(16px,env(safe-area-inset-right,0px)));padding:0 16px}}
.tape-track{{display:inline-flex;white-space:nowrap;animation:tape 90s linear infinite;will-change:transform}}
.tape:hover .tape-track{{animation-play-state:paused}}
@keyframes tape{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tk{{display:inline-flex;gap:8px;align-items:baseline;padding:7px 16px;border-right:1px solid var(--line);font-size:12px}}
.tk-n{{color:var(--muted);font-weight:500}}.tk-p{{font-weight:600}}.tk-c.pos{{color:var(--up)}}.tk-c.neg{{color:var(--down)}}
@media (prefers-reduced-motion:reduce){{.tape-track{{animation:none;white-space:normal;flex-wrap:wrap}}.tape-track .tk:nth-child(n+1000){{display:none}}.live i{{animation:none}}}}
/* board */
.board{{position:relative;width:100%;max-width:100%}}
.tile{{position:absolute;background:var(--tile);border:3px solid var(--bg);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;min-width:0;padding:10px 12px;gap:4px}}
.tile:hover{{background:var(--tile-2)}}
.tile header{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;position:relative;z-index:2}}
.who{{display:flex;gap:10px;min-width:0;align-items:flex-start}}
.thumb{{width:38px;height:38px;border-radius:7px;object-fit:cover;flex:none;background:var(--tile-2)}}
.thumb.ph{{display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:var(--muted);border:1px solid var(--line)}}
.names{{min-width:0}}.series{{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.name{{font-weight:700;font-size:15px;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.px{{text-align:right;flex:none}}.price{{font-weight:700;font-size:16px}}.chg{{font-size:12px;font-weight:600}}
.tchart{{position:absolute;left:0;right:0;bottom:0;top:0;width:100%;height:100%;z-index:0;pointer-events:none}}
.tchart .vol rect{{fill:var(--ink);opacity:.08}}.tchart .line{{fill:none;stroke-width:2.5}}.tchart .dot{{stroke:var(--bg);stroke-width:2}}
.tchart.up .line{{stroke:var(--up)}}.tchart.up .dot{{fill:var(--up)}}.tchart.up .area{{fill:url(#g-up)}}
.tchart.down .line{{stroke:var(--down)}}.tchart.down .dot{{fill:var(--down)}}.tchart.down .area{{fill:url(#g-down)}}
.nodata{{fill:var(--muted);font-size:9px}}
.strip{{margin-top:auto;position:relative;z-index:2;display:flex;flex-direction:column;gap:4px;background:color-mix(in srgb,var(--tile) 78%,transparent);border-radius:8px;padding:6px 8px;margin-left:-4px;margin-right:-4px;backdrop-filter:blur(2px)}}
.cmp{{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center}}.lbl{{font-size:11px;font-weight:600;color:var(--muted);margin-right:2px}}
.cmp .pill{{background:var(--tile);color:var(--ink);font-weight:500}}.cmp .pill.best{{background:var(--best);border-color:var(--up);color:var(--up);font-weight:700}}
.cmp .pill.case{{color:var(--accent);border-color:var(--accent)}}
.act{{font-size:12px;font-weight:600;color:var(--muted)}}.act.edge,.act.thin{{color:var(--up)}}.act.post{{color:var(--accent)}}.act.behind{{color:var(--ink)}}
.tile footer{{display:flex;justify-content:space-between;gap:10px;font-size:13px;font-weight:600;position:relative;z-index:2;margin-top:2px;text-shadow:0 1px 3px var(--bg)}}
.tile footer i{{display:block;font:500 10px/1.3 var(--sans);color:var(--muted)}}.tile footer b{{font-weight:700}}
/* what each size shows */
.tile.s header{{flex-direction:column;gap:4px}}.tile.s .px{{text-align:left;margin-top:auto}}.tile.s .series,.tile.s .strip,.tile.s footer{{display:none}}.tile.s .name{{font-size:12px;-webkit-line-clamp:3}}.tile.s .thumb{{width:30px;height:30px}}.tile.s .price{{font-size:14px}}
.tile.m .series{{display:none}}.tile.m footer{{display:none}}.tile.m .name{{font-size:13px}}.tile.m .act{{font-size:11px}}.tile.m .cmp .pill{{padding:2px 6px;font-size:10px}}
.tile.m .lbl.muted,.tile.m .pill.case,.tile.m .nodata,.tile.s .nodata{{display:none}}.tile.m .strip{{padding:4px 6px;gap:2px}}
.tile.l footer{{display:none}}
.tile.xl .name{{font-size:17px}}.tile.xl .price{{font-size:18px}}.tile.xl .chg{{font-size:13px}}
.empty{{background:var(--tile);border:1px dashed var(--line);border-radius:8px;padding:22px;color:var(--muted)}}
footer.page{{margin-top:22px;font-size:12px;color:var(--muted)}}code{{font-size:12px}}
.tb{{font:600 12px var(--sans);color:var(--ink);background:var(--tile);border:1px solid var(--line);border-radius:5px;padding:5px 10px;cursor:pointer}}
.tb:hover{{background:var(--tile-2)}}.tb.primary{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}
.overlay[hidden]{{display:none!important}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;display:flex;align-items:flex-start;justify-content:center;padding:max(16px,env(safe-area-inset-top,0px)) max(16px,env(safe-area-inset-right,0px)) 16px max(16px,env(safe-area-inset-left,0px));overflow:auto}}
.panel{{background:var(--tile);border:1px solid var(--line);border-radius:12px;padding:18px 20px;max-width:720px;width:100%;margin-top:4vh;font-size:14px}}
.ph{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}}.ph h3{{margin:0;font-size:18px}}
.guide{{padding-left:20px;display:flex;flex-direction:column;gap:10px;margin:0}}.guide li{{line-height:1.5}}.guide .act{{display:inline;font-weight:600}}
.addrow{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}}.addrow input[type=text]{{flex:1;min-width:200px;padding:7px 10px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--ink);font:inherit}}
.plist{{list-style:none;padding:0;margin:0 0 14px;display:flex;flex-direction:column;gap:4px;max-height:40vh;overflow:auto}}
.plist li{{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:6px 8px;border:1px solid var(--line);border-radius:6px}}
.plist .x{{color:var(--down)}}.cadence{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:12px}}
.cadence select{{padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--ink);font:inherit}}
#setStatus{{margin-top:8px;min-height:1.2em}}
html.tv .top .tb{{display:none}}
@media (max-width:760px){{.board{{aspect-ratio:auto!important;display:flex;flex-direction:column;gap:8px}}.tile{{position:static;width:auto!important;height:200px!important;border:0}}.tile.s .series,.tile.s .strip,.tile.m .series{{display:flex}}.tile.s header{{flex-direction:row}}}}
/* big-screen mode: #tv */
html.tv body{{font-size:16px}}html.tv main{{max-width:none}}html.tv footer.page,html.tv .banner{{display:none}}
html.tv .name{{font-size:17px}}html.tv .tile.xl .name{{font-size:21px}}html.tv .price{{font-size:19px}}html.tv .tile.xl .price{{font-size:24px}}
html.tv .cmp .pill{{font-size:13px}}html.tv .act{{font-size:14px}}
html.tv main.two .board{{aspect-ratio:auto!important;height:calc(50vh - 78px)}}html.tv main.one .board{{aspect-ratio:auto!important;height:calc(100vh - 150px)}}
html.tv .tape{{display:none}}
</style></head><body><main class="{n_sections}">
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<linearGradient id="g-up" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3ddc84" stop-opacity=".45"/><stop offset="1" stop-color="#3ddc84" stop-opacity="0"/></linearGradient>
<linearGradient id="g-down" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff4d6a" stop-opacity=".45"/><stop offset="1" stop-color="#ff4d6a" stop-opacity="0"/></linearGradient>
</defs></svg>
<div class="top"><div class="top-l"><h1>TPX × Dealernet edge board</h1>{live}<span class="muted">Updated {generated}</span>{counts}</div>
<div class="top-l"><span class="muted">tiles sized by 30-day TPX volume · {margin} profit target after fees &amp; shipping</span>{actions}
<button type="button" class="tb" id="btnSettings" title="Choose the products on the board and how often it refreshes">⚙ My products</button>
<button type="button" class="tb" id="btnHelp" title="How to read this board">? How to read</button></div></div>
{ticker}
{banner}
{sections}
<div class="overlay" id="help" hidden><div class="panel">
<div class="ph"><h3>How to read this board</h3><button type="button" class="tb" data-close="help">Close</button></div>
<ol class="guide">
<li><b>Each tile is one product.</b> Bigger tile = more money traded on TPX in the last 30 days. The chart behind it is the TPX price over 30 days; the faint bars are how much sold each day. <span class="up">Green</span> = price up over 30 days, <span class="down">red</span> = down.</li>
<li><b>Top right of a tile:</b> the last TPX sale price and the 30-day change.</li>
<li><b>Pokémon &amp; One Piece tiles</b> show <i>Buy TPX $x</i> and <i>Buy DN $y</i>: the cheapest place to buy one right now. The <span class="up">green pill</span> is the cheaper one. With TCGplayer keys set, each price also shows as a % of TCGplayer market (under 100% = cheaper than market).</li>
<li><b>Sports tiles</b> show what each venue <i>pays</i> (their best buy offer, where you would sell) and what it <i>sells</i> for (their best sell offer, where you would buy). The <span class="up">green pill</span> is the best price on each side.</li>
<li><b>Amber "Case" pill:</b> Dealernet has case-only offers for this product, shown per box. A case needs a full case to fill and usually pays more than singles, so it is never used as the box price.</li>
<li><b>The line under the pills is the call:</b><br><span class="act edge">▲ Buy … → sell … · +$ now</span> both prices are live, you can do the round trip now.<br><span class="act post">◆ Post TPX buy $x → sell DN $y · +$z/box</span> post that order on TPX; when it fills, sell to Dealernet at their price for the profit shown.<br><span class="act post">◆ Post TPX sell $x → buy DN $y</span> the reverse: post a sell on TPX, cover from Dealernet.<br><span class="muted">No profit: …</span> nothing to do on this product right now, and why.</li>
<li><b>Every dollar figure is after fees and shipping</b>, aiming for the profit target in the header. Nothing on this page places an order.</li>
<li><b>Big screen:</b> add <code>#tv</code> to the link. <b>Your products:</b> the ⚙ button lets you add or remove products and set how often the board refreshes.</li>
</ol></div></div>
<div class="overlay" id="settings" hidden><div class="panel">
<div class="ph"><h3>My products</h3><button type="button" class="tb" data-close="settings">Close</button></div>
<p class="muted small" id="setNote">Type a product name as it appears on TPX (for example "2026 Topps Finest Baseball Hobby Box" or "Surging Sparks Elite Trainer Box"). Changes are saved and picked up by the next refresh.</p>
<div class="addrow"><input type="text" id="newProd" placeholder="Product name as on TPX" aria-label="Product name"><label class="small"><input type="checkbox" id="newCase"> it's a case</label><button type="button" class="tb primary" id="btnAdd">Add</button></div>
<ul class="plist" id="plist"></ul>
<div class="cadence"><b>Refresh the board</b> <select id="cadence" aria-label="Refresh cadence"><option value="1">every hour</option><option value="2">every 2 hours</option><option value="4">every 4 hours</option><option value="12">every 12 hours</option><option value="24">once a day</option><option value="0">paused</option></select>
<span class="muted small">Hourly is the fastest the cloud scheduler allows. For a live 2-minute refresh run it on your own machine: <code>edge dashboard watchlist.live.yaml --refresh 120 --open</code>.</span></div>
<div class="small" id="setStatus"></div>
</div></div>
<footer class="page">View only. This page never places, lists or cancels anything. {refresh_note} Green pill = the better price right now. Add <code>#tv</code> to the link for the big-screen view.</footer>
</main>
<script id="meta" type="application/json">{data}</script>
<script>
(function(){{
if(location.hash==='#tv')document.documentElement.classList.add('tv');
window.addEventListener('hashchange',function(){{document.documentElement.classList.toggle('tv',location.hash==='#tv');}});
var $=function(id){{return document.getElementById(id);}};
function show(id,on){{$(id).hidden=!on;}}
$('btnHelp').addEventListener('click',function(){{show('help',true);}});
$('btnSettings').addEventListener('click',function(){{show('settings',true);}});
Array.prototype.forEach.call(document.querySelectorAll('[data-close]'),function(b){{b.addEventListener('click',function(){{show(b.dataset.close,false);}});}});
document.addEventListener('keydown',function(e){{if(e.key==='Escape'){{show('help',false);show('settings',false);}}}});
try{{if(!localStorage.getItem('seenHelp')&&location.hash!=='#tv'){{show('help',true);localStorage.setItem('seenHelp','1');}}}}catch(e){{}}
/* My products + cadence: shared settings kept in the page's database (owner/editors write). */
var meta=JSON.parse($('meta').textContent||'{{}}');
var state={{products:(meta.products||[]).map(function(n){{return {{name:n}};}}),hours:1,canWrite:null,db:null}};
function renderList(){{var ul=$('plist');ul.innerHTML='';state.products.forEach(function(p,i){{var li=document.createElement('li');
var sp=document.createElement('span');sp.textContent=p.name+(p.is_case?'  (case)':'');li.appendChild(sp);
if(state.canWrite!==false){{var b=document.createElement('button');b.type='button';b.className='tb x';b.textContent='Remove';b.addEventListener('click',function(){{state.products.splice(i,1);save('Removed');}});li.appendChild(b);}}
ul.appendChild(li);}});$('cadence').value=String(state.hours);}}
function status(t){{$('setStatus').textContent=t;}}
var saving=false;
function save(what){{renderList();if(!state.db){{status('Settings cannot be saved from this view.');return;}}if(saving)return;saving=true;
state.db.doc('settings/board').set({{products:state.products,hours:state.hours,updated:new Date().toISOString()}}).then(function(){{status(what+'. Saved; the next refresh uses it.');}})
.catch(function(e){{status(e&&e.code==='invalid_argument'?'Only the board owner can change this.':'Could not save ('+(e&&e.code||'error')+'). Try again.');if(e&&e.code==='invalid_argument'){{state.canWrite=false;renderList();}}}}).then(function(){{saving=false;}});}}
$('btnAdd').addEventListener('click',function(){{var n=$('newProd').value.trim();if(!n)return;if(state.products.some(function(p){{return p.name.toLowerCase()===n.toLowerCase();}})){{status('Already on the list.');return;}}
state.products.push($('newCase').checked?{{name:n,is_case:true}}:{{name:n}});$('newProd').value='';$('newCase').checked=false;save('Added '+n);}});
$('newProd').addEventListener('keydown',function(e){{if(e.key==='Enter')$('btnAdd').click();}});
$('cadence').addEventListener('change',function(){{state.hours=parseInt($('cadence').value,10);save('Refresh set');}});
renderList();
if(window.claude&&claude.use){{claude.use('db').then(function(db){{if(!db)return;state.db=db;
db.doc('settings/board').onSnapshot(function(snap){{var d=snap.exists?snap.data():null;if(d){{if(Array.isArray(d.products))state.products=d.products.slice();if(typeof d.hours==='number')state.hours=d.hours;renderList();}}}},function(){{}});}});
claude.use('user').then(function(u){{if(!u)return;Promise.resolve(u.can('data.write')).then(function(w){{state.canWrite=w;if(w===false){{$('btnAdd').disabled=true;$('cadence').disabled=true;status('You can view the list; only the board owner can change it.');}}renderList();}});}});}}
}})();
</script></body></html>"""
