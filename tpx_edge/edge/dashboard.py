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
        self.buy_tpx = Direction("Buy Tradepost → sell DN", self._pick_maker("bid", "Dealernet bid"), self._pick_arb("arb_b"), self.ref)
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
    return "Dealernet" if venue.startswith("Dealernet") else "Tradepost"


def _market_word(venue: str) -> str:
    return "Tradepost vault market" if "[Vaulted]" in venue else "Tradepost shipped market"


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
                return f"◆ Post Tradepost buy {_usd(m.rest_clean_cents)} → sell DN {_usd(m.counter_clean_cents)} · +{_usd(m.expected_edge_cents)}/{self.unit}"
            return f"◆ Post Tradepost sell {_usd(m.rest_clean_cents)} → buy DN {_usd(m.counter_clean_cents)} · +{_usd(m.expected_edge_cents)}/{self.unit}"
        if self.cls == "behind" and best and best.post:
            return f"Almost: Tradepost {best.post.side} {_usd(best.post.rest_clean_cents)} would work, someone is at {_usd(best.post.current_top_cents)}"
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
                steps = [f"Post a buy order on Tradepost at {_usd(m.rest_clean_cents)} ({where})",
                         f"When it fills, sell to Dealernet at {_usd(m.counter_clean_cents)} (their current buying price)"]
                headline = "Buy on Tradepost, sell to Dealernet"
                top = (f"Your {_usd(m.rest_clean_cents)} would be the highest buy order on Tradepost "
                       + (f"(the next best is {_usd(m.current_top_cents)})" if m.current_top_cents else "(nobody else is bidding there yet)")
                       + ", so you are first in line when someone sells.")
                why = f"Dealernet is paying {_usd(m.counter_clean_cents)} per {u}, more than Tradepost sellers are getting. {top}"
            else:
                steps = [f"Post a sell order on Tradepost at {_usd(m.rest_clean_cents)} ({where})",
                         f"When it fills, buy from Dealernet at {_usd(m.counter_clean_cents)} (their current selling price) to cover it"]
                headline = "Sell on Tradepost, buy from Dealernet"
                top = (f"Your {_usd(m.rest_clean_cents)} would be the lowest sell order on Tradepost "
                       + (f"(the next best is {_usd(m.current_top_cents)})" if m.current_top_cents else "(nobody else is selling there yet)")
                       + ", so you are first in line when someone buys.")
                why = f"Dealernet sells this for {_usd(m.counter_clean_cents)} per {u}, less than Tradepost buyers will pay. {top}"
            profit = f"You make about {_usd(m.expected_edge_cents)} per {u} ({_pct(m.expected_edge_pct, False)}) after fees and shipping, once your order fills."
            return "post", "Post and wait", headline, steps, profit, why
        if sig == "behind":
            m = best.post
            verb = "buy" if m.side == "bid" else "sell"
            steps = [f"A Tradepost {verb} order at {_usd(m.rest_clean_cents)} would make {_pct(v.margin * 100, False)}",
                     f"But someone is already at {_usd(m.current_top_cents)}, ahead of you"]
            why = f"Watch this one. If the Tradepost {verb} orders thin out, it becomes a trade."
            return "behind", "Almost", "Someone is ahead of you", steps, "No profit yet.", why
        # Nothing to do: say why in one line.
        if v.dn is None:
            return "none", "No Dealernet price", "", [], "", "No Dealernet listing matched this product."
        if best and best.post and not best.near_market:
            m = best.post
            verb = "sell" if m.side == "ask" else "buy"
            return ("none", "No profit right now", "", [], "",
                    f"Fees and shipping eat the gap: you would have to {verb} at {_usd(m.rest_clean_cents)} on Tradepost, "
                    f"but it trades around {_usd(v.ref)}.")
        if v.edge is not None:
            return ("none", "No profit right now", "", [], "",
                    f"Prices are too close. Dealernet pays {_usd(v.dn_bid)} and sells at {_usd(v.dn_ask)}; "
                    f"Tradepost pays {_usd(v.tpx_bid)} and sells at {_usd(v.tpx_ask)}.")
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


def _num(v):
    """Decimal/None -> JSON-friendly."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def product_payload(st: Story) -> dict:
    """Everything the page needs for one product, as plain JSON."""
    from .outlook import analyze

    v = st.v
    m = _meta(v)
    dn = v.dn
    dn_raw = dn.raw if dn else {}
    closes = [{"t": c.get("t"), "close": c.get("close"), "vwap": c.get("vwap"), "volume": c.get("volume") or 0} for c in v.closes]
    notional = sum((c.get("vwap") or c.get("close") or 0) * (c.get("volume") or 0) for c in v.closes) or ((v.vwap30 or v.last or 0) * v.vol30)
    out = analyze(closes, tpx_bid_units=v.primary.tpx.bid_depth_units, tpx_ask_units=v.primary.tpx.ask_depth_units,
                  dn_bid_cents=v.dn_bid, dn_ask_cents=v.dn_ask, last_cents=v.last)
    return {
        "id": v.primary.tpx.product_id, "name": v.name, "group": _group(v), "series": _series_label(v),
        "image": m.get("image"), "initials": _initials(v.name), "unit": st.unit, "is_case": bool(m.get("is_case")),
        "price": v.last or v.fv or v.tpx_bid or v.dn_bid, "last": v.last, "last_at": v.last_at.isoformat() if v.last_at else None,
        "chg30": _num(v.chg30), "vol30": v.vol30, "trades30": v.trades30, "notional": int(notional), "vwap30": v.vwap30,
        "hi24": v.hi24, "lo24": v.lo24, "fv": v.fv, "closes": closes,
        "tpx": {"bid": v.tpx_bid, "ask": v.tpx_ask, "bid_units": v.primary.tpx.bid_depth_units, "ask_units": v.primary.tpx.ask_depth_units,
                "markets": [{"name": a.tpx_market.replace("TPX:", ""), "vaulted": bool(a.tpx_is_vaulted),
                             "bid": a.tpx.best_bid, "ask": a.tpx.best_ask, "bid_units": a.tpx.bid_depth_units, "ask_units": a.tpx.ask_depth_units}
                            for a in v.items]},
        "dn": ({"bid": v.dn_bid, "ask": v.dn_ask, "bid_qty": dn.bid_depth_units, "ask_qty": dn.ask_depth_units,
                "case_bid": dn_raw.get("case_bid"), "case_ask": dn_raw.get("case_ask"), "bpc": dn_raw.get("boxes_per_case"),
                "url": dn_raw.get("url"), "page": dn_raw.get("product")} if dn else None),
        "tcg": ({"market": v.tcg, "low": v.tcg_low} if v.tcg else None),
        "cls": st.cls, "call": st.compact,
        "story": {"badge": st.badge, "headline": st.headline, "steps": st.steps, "profit": st.profit, "why": st.why},
        "outlook": out.as_dict(),
    }


# ---------------------------------------------------------------------------
def render(ranked: Sequence[Assessment], *, source: str, actions_enabled: bool = False,
           history: Optional[dict] = None, margin: Optional[Decimal] = None,
           generated_at: Optional[datetime] = None, refresh_s: Optional[int] = None,
           warnings: Sequence[str] = (), universe: Optional[Sequence[dict]] = None) -> str:
    history = history or {}
    generated_at = generated_at or datetime.now(timezone.utc)
    by_product: dict[str, list[Assessment]] = defaultdict(list)
    for a in ranked:
        by_product[a.product_name].append(a)
    views = [ProductView(p, items, margin, history) for p, items in by_product.items()]
    stories = [Story(v) for v in views]
    products = [product_payload(st) for st in stories]
    # The search/hottest catalogue: every product Tradepost lists, ranked by 30-day dollar volume.
    uni = [{"id": r["id"], "name": r["name"], "group": "sports" if "wax" in str(r.get("asset_type", "")) else "tcg",
            "vol": r.get("volume_cents") or 0, "units": r.get("units") or 0, "last": r.get("last_cents"),
            "chg": _num(r.get("change_pct")), "set": r.get("set")} for r in (universe or [])]
    known = {p["id"] for p in products}
    for p in products:  # make sure every priced product is searchable even if the scan missed it
        if p["id"] not in known or not any(u["id"] == p["id"] for u in uni):
            uni.append({"id": p["id"], "name": p["name"], "group": p["group"], "vol": p["notional"], "units": p["vol30"],
                        "last": p["last"], "chg": p["chg30"], "set": p["series"]})
    uni.sort(key=lambda u: -(u["vol"] or 0))

    banner = ""
    if source != "live":
        banner = ('<div class="banner">Sample data: these prices come from the built-in demo fixtures, '
                  'not from Tradepost or Dealernet. Run without <code>--demo</code> once Tradepost access is set up.</div>')
    warn_html = "".join(f'<div class="banner warn">{_e(w)}</div>' for w in warnings)
    actions = ('<span class="pill on">Actions: ON</span>' if actions_enabled
               else '<span class="pill" title="Set ACTIONS_ENABLED=true to allow features that act on a marketplace">Actions: OFF (view only)</span>')
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    live = ('<span class="live"><i></i>LIVE</span>' if source == "live" else '<span class="live demo"><i></i>DEMO</span>')
    payload = {"generated_at": generated_at.isoformat(), "source": source,
               "margin_pct": float(Decimal(margin) * 100) if margin is not None else 8.0,
               "products": products, "universe": uni, "warnings": list(warnings)}
    return TEMPLATE.format(
        meta_refresh=meta_refresh, generated=_e(generated_at.strftime("%b %d, %Y · %H:%M UTC")), live=live,
        actions=actions, banner=banner + warn_html, data=json.dumps(payload, separators=(",", ":")).replace("</", "<\\/"),
        margin=f"{Decimal(margin) * 100:.0f}%" if margin is not None else "8%",
        refresh_note=(f"Refreshes every {refresh_s} s." if refresh_s else "Re-run <code>edge dashboard</code> to refresh."),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta_refresh}<title>Live Sealed Product Radar</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
/* Always dark: a trading screen, by choice. Green/red for direction, one amber for "do this". */
:root{{--bg:#0a0a0b;--tile:#1c1c20;--tile-2:#242428;--ink:#f2f2f3;--muted:#8c8c95;--line:#2a2a30;
--up:#3ddc84;--down:#ff4d6a;--flat:#8c8c95;--accent:#f5b63a;--accent-ink:#1a1200;--best:#17331f;--banner:#2a2410;
--sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:dark}}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%}}
body{{background:var(--bg);color:var(--ink);font:14px/1.4 var(--sans);padding-left:max(12px,env(safe-area-inset-left,0px));padding-right:max(12px,env(safe-area-inset-right,0px));padding-block:0 8px;font-variant-numeric:tabular-nums;display:flex;flex-direction:column;min-height:100vh}}
main{{flex:1;display:flex;flex-direction:column;min-height:0}}
h1{{font-size:17px;font-weight:700;margin:0;white-space:nowrap}}
.muted{{color:var(--muted)}}.up{{color:var(--up)}}.down{{color:var(--down)}}.flat{{color:var(--flat)}}.small{{font-size:12px}}
.top{{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;justify-content:space-between;padding:10px 0 8px}}
.top-l,.top-r{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.live{{font:600 11px/1 var(--sans);letter-spacing:.12em;color:var(--up);display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border:1px solid var(--up);border-radius:3px}}
.live i{{width:7px;height:7px;border-radius:50%;background:var(--up);animation:blink 1.6s infinite}}
.live.demo{{color:var(--accent);border-color:var(--accent)}}.live.demo i{{background:var(--accent)}}
@keyframes blink{{50%{{opacity:.25}}}}
.pill{{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:3px 8px;font-size:11px;font-weight:600;color:var(--muted);white-space:nowrap}}
.pill.on{{border-color:var(--down);color:var(--down)}}
.banner{{background:var(--banner);border:1px solid var(--line);border-radius:4px;padding:8px 12px;margin:0 0 8px;font-size:13px}}
.tb{{font:600 12px var(--sans);color:var(--ink);background:var(--tile);border:1px solid var(--line);border-radius:6px;padding:6px 11px;cursor:pointer;white-space:nowrap}}
.tb:hover{{background:var(--tile-2)}}.tb.primary{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}.tb.x{{color:var(--down)}}
.seg{{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}}.seg .tb{{border:0;border-radius:0;background:transparent}}.seg .tb.on{{background:var(--tile-2);color:var(--accent)}}
.counts{{display:flex;gap:10px;font-size:12px;font-weight:600}}.counts .warn{{color:var(--accent)}}
/* board */
#board{{position:relative;flex:1;min-height:320px}}
.tile{{position:absolute;background:var(--tile);border:3px solid var(--bg);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;min-width:0;padding:10px 12px;gap:4px;cursor:pointer;transition:background .15s}}
.tile:hover{{background:var(--tile-2)}}.tile:focus-visible{{outline:2px solid var(--accent);outline-offset:-2px}}
.tile header{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;position:relative;z-index:2}}
.who{{display:flex;gap:10px;min-width:0;align-items:flex-start}}
.thumb{{width:38px;height:38px;border-radius:7px;object-fit:cover;flex:none;background:var(--tile-2)}}
.thumb.ph{{display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:var(--muted);border:1px solid var(--line)}}
.names{{min-width:0}}.series{{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.name{{font-weight:700;font-size:15px;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.px{{text-align:right;flex:none}}.price{{font-weight:700;font-size:16px}}.chg{{font-size:12px;font-weight:600}}
.tchart{{position:absolute;left:0;right:0;bottom:0;top:0;width:100%;height:100%;z-index:0;pointer-events:none}}
.tchart .vol rect{{fill:var(--ink);opacity:.08}}.tchart .line{{fill:none;stroke-width:2.5}}
.tchart.up .line{{stroke:var(--up)}}.tchart.up .area{{fill:url(#g-up)}}.tchart.down .line{{stroke:var(--down)}}.tchart.down .area{{fill:url(#g-down)}}
.nodata{{fill:var(--muted);font-size:9px}}
.strip{{margin-top:auto;position:relative;z-index:2;display:flex;flex-direction:column;gap:4px;background:rgba(28,28,32,.8);border-radius:8px;padding:6px 8px;margin-left:-4px;margin-right:-4px;backdrop-filter:blur(2px)}}
.cmp{{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center}}.lbl{{font-size:11px;font-weight:600;color:var(--muted);margin-right:2px}}
.cmp .pill{{background:var(--tile);color:var(--ink);font-weight:500}}.cmp .pill.best{{background:var(--best);border-color:var(--up);color:var(--up);font-weight:700}}
.cmp .pill.case{{color:var(--accent);border-color:var(--accent)}}.cmp .pill i{{font-style:normal;color:var(--muted);margin-left:4px;font-size:.9em}}.cmp .vrow{{display:inline-flex;align-items:center;gap:4px;margin-right:6px}}.cmp .vrow .lbl{{min-width:58px}}.tile.m .cmp .vrow{{flex-wrap:wrap;margin-right:0}}.tile.m .cmp .vrow .lbl{{min-width:0}}.tile.m .cmp{{row-gap:2px}}.cmp .pill.best i{{color:var(--up)}}
.act{{font-size:12px;font-weight:600;color:var(--muted)}}.act.edge,.act.thin{{color:var(--up)}}.act.post{{color:var(--accent)}}.act.behind{{color:var(--ink)}}
.tile footer{{display:flex;justify-content:space-between;gap:10px;font-size:13px;font-weight:600;position:relative;z-index:2;margin-top:2px;text-shadow:0 1px 3px var(--bg)}}
.tile footer i{{display:block;font:500 10px/1.3 var(--sans);color:var(--muted)}}
.tile.s header{{flex-direction:column;gap:4px}}.tile.s .px{{text-align:left;margin-top:auto}}.tile.s .series,.tile.s .strip,.tile.s footer,.tile.s .nodata{{display:none}}.tile.s .name{{font-size:12px;-webkit-line-clamp:3}}.tile.s .thumb{{width:30px;height:30px}}.tile.s .price{{font-size:14px}}
.tile.m .series,.tile.m footer,.tile.m .lbl.muted,.tile.m .pill.case,.tile.m .nodata{{display:none}}.tile.m .name{{font-size:13px}}.tile.m .act{{font-size:11px}}.tile.m .cmp .pill{{padding:2px 6px;font-size:10px}}.tile.m .strip{{padding:4px 6px;gap:2px}}
.tile.l footer{{display:none}}.tile.xl .name{{font-size:17px}}.tile.xl .price{{font-size:18px}}.tile.xl .chg{{font-size:13px}}
.tile.pending{{border-style:dashed;border-color:var(--line);background:transparent}}.tile.pending .name{{color:var(--muted)}}
.empty{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;color:var(--muted);padding:20px}}
.empty h2{{margin:0;font-size:22px;color:var(--ink)}}.empty p{{margin:0;max-width:46ch}}
/* overlays */
.overlay[hidden]{{display:none!important}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:50;display:flex;align-items:flex-start;justify-content:center;padding:max(14px,env(safe-area-inset-top,0px)) max(14px,env(safe-area-inset-right,0px)) 14px max(14px,env(safe-area-inset-left,0px));overflow:auto}}
.panel{{background:var(--tile);border:1px solid var(--line);border-radius:12px;padding:16px 18px;max-width:760px;width:100%;margin-top:3vh;font-size:14px}}
.panel.wide{{max-width:1080px}}
.ph{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}}.ph h3{{margin:0;font-size:18px}}
.guide{{padding-left:20px;display:flex;flex-direction:column;gap:9px;margin:0}}.guide li{{line-height:1.5}}.guide .act{{display:inline;font-weight:600}}
.srch{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}}.srch input{{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}}
.rows{{display:flex;flex-direction:column;gap:3px;max-height:52vh;overflow:auto;margin-top:6px}}
.r{{display:grid;grid-template-columns:minmax(0,1fr) 90px 80px 90px;gap:10px;align-items:center;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px}}
.r .rn{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.r .rs{{font-size:11px;color:var(--muted)}}.r .num{{text-align:right}}.r.on{{border-color:var(--accent)}}
.r .tb{{padding:4px 8px;font-size:11px}}
.cadence{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:10px;margin-top:10px}}
select{{padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--ink);font:inherit}}
#setStatus{{margin-top:6px;min-height:1.2em;font-size:12px}}
/* detail */
.dhead{{display:flex;gap:14px;align-items:center}}.dhead .thumb{{width:56px;height:56px}}.dhead h3{{font-size:20px}}
.dprice{{margin-left:auto;text-align:right}}.dprice b{{font-size:26px;display:block}}
.dgrid{{display:grid;grid-template-columns:minmax(0,3fr) minmax(280px,2fr);gap:14px;margin-top:12px}}
.dchart{{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px;min-width:0}}.dchart svg{{width:100%;height:auto;display:block}}
.dchart .axis{{fill:var(--muted);font-size:10px}}.dchart .grid{{stroke:var(--line)}}.dchart .vol rect{{fill:var(--ink);opacity:.12}}.dchart .line{{fill:none;stroke-width:2}}
.dchart .ref{{stroke-dasharray:5 4;stroke-width:1.3}}.dchart .ref.dn{{stroke:var(--accent)}}.dchart .ref.tcg{{stroke:#bc8cff}}.dchart .ref.fv{{stroke:var(--muted)}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-top:4px}}.legend i{{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;border-radius:2px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
.m{{background:var(--tile);padding:8px 10px;min-width:0}}.m i{{display:block;font:500 10px/1.3 var(--sans);color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}.m b{{font-size:15px}}.m .sub{{font-size:11px;color:var(--muted)}}
.outlook{{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-top:12px}}
.outlook h4{{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
.olabel{{font-size:22px;font-weight:700}}.olabel.Rising{{color:var(--up)}}.olabel.Falling{{color:var(--down)}}
.outlook ul{{margin:8px 0 0;padding-left:18px;display:flex;flex-direction:column;gap:4px}}.outlook .fine{{font-size:11px;color:var(--muted);margin-top:8px}}
.callbox{{border-left:4px solid var(--line);padding:8px 12px;margin-top:12px;background:var(--bg);border-radius:0 8px 8px 0}}
.callbox.edge,.callbox.thin{{border-left-color:var(--up)}}.callbox.post{{border-left-color:var(--accent)}}
.callbox ol{{margin:6px 0;padding-left:20px}}.callbox .profit{{font-weight:700;color:var(--up)}}
@media (max-width:760px){{#board{{position:static;min-height:0;display:flex;flex-direction:column;gap:8px}}.tile{{position:static;width:auto!important;height:200px!important;border:0}}.tile.s .series,.tile.s .strip,.tile.m .series{{display:flex}}.tile.s header{{flex-direction:row}}.dgrid{{grid-template-columns:1fr}}.r{{grid-template-columns:minmax(0,1fr) 80px}}.r .hide-sm{{display:none}}}}
html.tv .top .tb,html.tv .counts,html.tv .seg{{display:none}}html.tv h1{{font-size:19px}}html.tv .name{{font-size:17px}}html.tv .tile.xl .name{{font-size:21px}}html.tv .price{{font-size:19px}}html.tv .tile.xl .price{{font-size:24px}}html.tv .cmp .pill{{font-size:13px}}html.tv .act{{font-size:14px}}
</style></head><body><main>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<linearGradient id="g-up" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3ddc84" stop-opacity=".45"/><stop offset="1" stop-color="#3ddc84" stop-opacity="0"/></linearGradient>
<linearGradient id="g-down" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff4d6a" stop-opacity=".45"/><stop offset="1" stop-color="#ff4d6a" stop-opacity="0"/></linearGradient>
</defs></svg>
<div class="top"><div class="top-l"><h1>Live Sealed Product Radar</h1>{live}<span class="muted small">Updated {generated}</span><span class="counts" id="counts"></span></div>
<div class="top-r"><span class="seg" id="groupSeg"><button type="button" class="tb on" data-g="all">All</button><button type="button" class="tb" data-g="tcg">Pokémon &amp; One Piece</button><button type="button" class="tb" data-g="sports">Sports</button></span>
<button type="button" class="tb" id="btnRefresh" title="Pull fresh prices now">↻ Refresh</button><button type="button" class="tb primary" id="btnAdd">+ Add products</button><button type="button" class="tb" id="btnHelp">? How to read</button>{actions}</div></div>
{banner}
<div id="board"></div>
<div class="overlay" id="help" hidden><div class="panel">
<div class="ph"><h3>How to read this board</h3><button type="button" class="tb" data-close="help">Close</button></div>
<ol class="guide">
<li><b>Each tile is one product you chose.</b> Bigger tile = more money traded on Tradepost in the last 30 days. The chart behind it is the Tradepost price over 30 days; the faint bars are daily units sold. <span class="up">Green</span> = up over 30 days, <span class="down">red</span> = down. <b>Click a tile</b> for the full chart, the numbers and the outlook.</li>
<li><b>Top right of a tile:</b> last Tradepost sale price and the 30-day change.</li>
<li><b>Pokémon &amp; One Piece tiles</b> show <i>Buy Tradepost $x</i> and <i>Buy DN $y</i>: the cheapest place to buy one now. The <span class="up">green pill</span> is the cheaper one. With TCGplayer keys set, each also shows as a % of TCGplayer market (under 100% = cheaper than market).</li>
<li><b>Sports tiles</b> show what each venue <i>pays</i> (their best buy offer, where you would sell) and what it <i>sells</i> for (their best sell offer, where you would buy). The <span class="up">green pill</span> is the best price on each side.</li>
<li><b>Amber "Case" pill:</b> Dealernet case-only offers, shown per box. A case needs a full case to fill and usually pays more than singles, so it never sets the box price.</li>
<li><b>The line under the pills is the call:</b> <span class="act edge">▲ Buy … → sell … · +$ now</span> both prices are live, you can do the round trip now. <span class="act post">◆ Post Tradepost buy $x → sell DN $y · +$z/box</span> post that order on Tradepost; when it fills, sell to Dealernet for the profit shown. <span class="muted">No profit: …</span> nothing to do, and why.</li>
<li><b>Outlook</b> (inside a tile): Rising / Falling / Sideways from the last 30 days of Tradepost trades: trend, momentum, volume, range position and today's order book. It describes the past; it is not advice.</li>
<li><b>Every dollar figure is after fees and shipping</b>, aiming for the {margin} target. Nothing on this page places an order. Add <code>#tv</code> to the link for the big-screen view.</li>
</ol></div></div>
<div class="overlay" id="add" hidden><div class="panel">
<div class="ph"><h3>Add products</h3><div><span class="seg" id="addSeg"><button type="button" class="tb on" data-tab="hot">🔥 Hottest</button><button type="button" class="tb" data-tab="search">Search</button><button type="button" class="tb" data-tab="mine">On my board</button></span> <button type="button" class="tb" data-close="add">Done</button></div></div>
<div class="srch"><input type="search" id="q" placeholder="Search every product Tradepost lists…" aria-label="Search products"><span class="seg" id="addGroup"><button type="button" class="tb on" data-g="all">All</button><button type="button" class="tb" data-g="tcg">Pokémon &amp; OP</button><button type="button" class="tb" data-g="sports">Sports</button></span></div>
<div class="rows" id="rows"></div>
<div class="cadence"><b>Refresh the board</b> <select id="cadence" aria-label="Refresh cadence"><option value="1">every hour</option><option value="2">every 2 hours</option><option value="4">every 4 hours</option><option value="12">every 12 hours</option><option value="24">once a day</option><option value="0">paused</option></select>
<span class="muted small">Hourly is the fastest the cloud scheduler allows; for a 2-minute live feed run <code>edge dashboard --refresh 120 --open</code> on your own machine.</span></div>
<div id="setStatus"></div>
</div></div>
<div class="overlay" id="detail" hidden><div class="panel wide" id="detailBody"></div></div>
<footer class="muted small" id="foot">View only. Nothing here places, lists or cancels anything. {refresh_note}</footer>
</main>
<script id="data" type="application/json">{data}</script>
<script>
(function(){{
var D=JSON.parse(document.getElementById('data').textContent);
var P=D.products, U=D.universe, byId={{}}, byName={{}};
P.forEach(function(p){{byId[p.id]=p;byName[p.name.toLowerCase()]=p;}});
var uniById={{}};U.forEach(function(u){{uniById[u.id]=u;}});
var $=function(id){{return document.getElementById(id);}};
var state={{selected:[],hours:1,group:'all',tab:'hot',addGroup:'all',db:null,canWrite:null,loaded:false}};
if(location.hash==='#tv')document.documentElement.classList.add('tv');
window.addEventListener('hashchange',function(){{document.documentElement.classList.toggle('tv',location.hash==='#tv');}});
function usd(c,d){{if(c==null)return '—';d=(d==null)?2:d;return '$'+(c/100).toLocaleString('en-US',{{minimumFractionDigits:d,maximumFractionDigits:d}});}}
function pct(v,d){{if(v==null)return '—';d=(d==null)?1:d;return (v>=0?'+':'')+Number(v).toFixed(d)+'%';}}
function compact(c,money){{if(c==null)return '—';var v=money?c/100:c,s=money?'$':'';var a=Math.abs(v);if(a>=1e9)return s+(v/1e9).toFixed(1)+'B';if(a>=1e6)return s+(v/1e6).toFixed(1)+'M';if(a>=1e3)return s+(v/1e3).toFixed(1)+'K';return s+Math.round(v).toLocaleString('en-US');}}
function el(tag,cls,text){{var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}}
function esc(s){{return String(s==null?'':s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
/* ---- treemap (squarified) ---- */
function squarify(items,x,y,w,h,out){{if(!items.length)return;if(items.length===1){{out[items[0].k]=[x,y,w,h];return;}}
var vertical=w>=h,len=vertical?h:w,row=[],rest=items.slice();
function worst(r){{var s=0,mx=0,mn=Infinity;r.forEach(function(i){{s+=i.a;mx=Math.max(mx,i.a);mn=Math.min(mn,i.a);}});if(!s||!len)return Infinity;return Math.max(len*len*mx/(s*s),s*s/(len*len*mn));}}
while(rest.length){{var cand=row.concat([rest[0]]);if(!row.length||worst(cand)<=worst(row)){{row=cand;rest.shift();}}else break;}}
var area=0;row.forEach(function(i){{area+=i.a;}});
if(vertical){{var cw=area/h,yy=y;row.forEach(function(i){{var hh=i.a/cw;out[i.k]=[x,yy,cw,hh];yy+=hh;}});squarify(rest,x+cw,y,w-cw,h,out);}}
else{{var rh=area/w,xx=x;row.forEach(function(i){{var ww=i.a/rh;out[i.k]=[xx,y,ww,rh];xx+=ww;}});squarify(rest,x,y+rh,w,h-rh,out);}}}}
function layout(list,W,H){{var total=0;list.forEach(function(p){{total+=p.w;}});total=total||1;
var items=list.map(function(p){{return {{k:p.id,a:Math.min(Math.max(p.w,total*0.03),total*0.22)}};}});var t2=0;items.forEach(function(i){{t2+=i.a;}});
items.forEach(function(i){{i.a=i.a/t2*W*H;}});items.sort(function(a,b){{return b.a-a.a;}});var out={{}};squarify(items,0,0,W,H,out);return out;}}
/* ---- tile pieces ---- */
function chartSvg(closes,up,cls){{var pts=closes.filter(function(c){{return c.close!=null;}}).slice(-30);
if(pts.length<2)return '<svg class="'+cls+'" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="price trend"><text x="150" y="55" text-anchor="middle" class="nodata">No price history</text></svg>';
var pr=pts.map(function(c){{return c.close;}}),vo=pts.map(function(c){{return c.volume||0;}}),lo=Math.min.apply(null,pr),hi=Math.max.apply(null,pr),span=(hi-lo)||1,n=pts.length,step=300/(n-1),vmax=Math.max.apply(null,vo)||1,bw=Math.max(step*0.7,2);
var xs=pr.map(function(_,i){{return i*step;}}),ys=pr.map(function(p){{return 92-(p-lo)/span*74;}});
var bars=vo.map(function(v,i){{return v?'<rect x="'+(xs[i]-bw/2).toFixed(1)+'" y="'+(100-v/vmax*40).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(v/vmax*40).toFixed(1)+'"/>':'';}}).join('');
var line=xs.map(function(x,i){{return (i?'L':'M')+x.toFixed(1)+','+ys[i].toFixed(1);}}).join(' ');
return '<svg class="'+cls+' '+(up?'up':'down')+'" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="price trend"><g class="vol">'+bars+'</g><path class="area" d="'+line+' L300,100 L0,100 Z"/><path class="line" d="'+line+'" vector-effect="non-scaling-stroke"/></svg>';}}
function strip(p){{var dn=p.dn||{{}},tpx=p.tpx||{{}};
var caseP=(dn.case_bid||dn.case_ask)?'<span class="pill case" title="Dealernet case offers, per box; a case needs a full case to fill">Case'+(dn.bpc?' ×'+dn.bpc:'')+': pays '+usd(dn.case_bid)+' · sells '+usd(dn.case_ask)+'</span>':'';
var bids=[tpx.bid,dn.bid].filter(Boolean),asks=[tpx.ask,dn.ask].filter(Boolean),hb=bids.length?Math.max.apply(null,bids):null,la=asks.length?Math.min.apply(null,asks):null;
var tm=p.group==='tcg'&&p.tcg&&p.tcg.market;var pc=function(v){{return tm&&v?'<i>'+(v/tm*100).toFixed(0)+'%</i>':'';}};
var cell=function(l,v,b,t){{return '<span class="pill'+(b?' best':'')+'" title="'+t+'">'+l+' '+usd(v)+pc(v)+'</span>';}};
var h='<span class="vrow"><span class="lbl">Tradepost</span>'+cell('pays',tpx.bid,tpx.bid&&tpx.bid===hb,'Highest Tradepost buy order')+cell('sells',tpx.ask,tpx.ask&&tpx.ask===la,'Lowest Tradepost sell order')+'</span>'
+'<span class="vrow"><span class="lbl">Dealernet</span>'+cell('pays',dn.bid,dn.bid&&dn.bid===hb,'Dealernet buy price')+cell('sells',dn.ask,dn.ask&&dn.ask===la,'Dealernet sell price')+'</span>'+caseP;
if(p.group==='tcg')h+='<span class="lbl'+(tm?'':' muted')+'">'+(tm?'vs TCGplayer market '+usd(tm)+' (% = share of market)':'TCGplayer: keys not set')+'</span>';
return '<div class="strip"><div class="cmp">'+h+'</div><div class="act '+p.cls+'">'+esc(p.call)+'</div></div>';}}
function tile(p,r,W,H){{var up=(p.chg30||0)>=0,wpx=r[2]/100*W,hpx=r[3]/100*H,size=(wpx>=420&&hpx>=300)?'xl':(wpx>=280&&hpx>=190)?'l':(wpx>=160&&hpx>=130)?'m':'s';
var a=el('article','tile '+size+' '+p.cls);a.tabIndex=0;a.style.cssText='left:'+r[0]+'%;top:'+r[1]+'%;width:'+r[2]+'%;height:'+r[3]+'%';a.dataset.id=p.id;
var img=p.image?'<img class="thumb" src="'+p.image+'" alt="">':'<span class="thumb ph">'+esc(p.initials)+'</span>';
var chg=p.chg30==null?'<span class="chg flat">—</span>':'<span class="chg '+(up?'up':'down')+'">'+(up?'↗':'↘')+' '+Math.abs(p.chg30).toFixed(2)+'%</span>';
a.innerHTML='<header><div class="who">'+img+'<div class="names"><div class="series">'+esc(p.series)+'</div><div class="name">'+esc(p.name)+'</div></div></div><div class="px"><div class="price">'+usd(p.price)+'</div>'+chg+'</div></header>'+chartSvg(p.closes,up,'tchart')+strip(p)+'<footer><span><i>Volume</i>'+compact(p.notional,true)+'</span><span><i>Last sale</i><b class="'+(up?'up':'down')+'">'+usd(p.last)+'</b></span><span><i>30D sold</i>'+compact(p.vol30,false)+'</span></footer>';
a.addEventListener('click',function(){{openDetail(p);}});a.addEventListener('keydown',function(e){{if(e.key==='Enter')openDetail(p);}});return a;}}
function pendingTile(u,r){{var a=el('article','tile s pending');a.style.cssText='left:'+r[0]+'%;top:'+r[1]+'%;width:'+r[2]+'%;height:'+r[3]+'%';
a.innerHTML='<header><div class="who"><span class="thumb ph">…</span><div class="names"><div class="name">'+esc(u.name)+'</div></div></div></header><div class="small muted">Prices arrive on the next refresh</div>';return a;}}
/* ---- board ---- */
function selectedProducts(){{var out=[];state.selected.forEach(function(s){{var p=(s.tpx_id&&byId[s.tpx_id])||byName[(s.name||'').toLowerCase()];
if(p){{if(state.group==='all'||p.group===state.group)out.push({{p:p,id:p.id,w:p.notional||1}});}}
else{{var u=(s.tpx_id&&uniById[s.tpx_id])||null;var g=u?u.group:null;if(state.group==='all'||g===state.group)out.push({{u:u||{{name:s.name,id:s.tpx_id||s.name}},id:s.tpx_id||s.name,w:1}});}}}});return out;}}
function renderBoard(){{var b=$('board');b.innerHTML='';var list=selectedProducts();
var now=0,post=0;list.forEach(function(x){{if(x.p){{if(x.p.cls==='edge'||x.p.cls==='thin')now++;else if(x.p.cls==='post')post++;}}}});
$('counts').innerHTML=list.length?'<span class="up">'+now+' ready now</span><span class="warn">'+post+' post &amp; wait</span><span class="muted">'+(list.length-now-post)+' nothing to do</span>':'';
if(!list.length){{var e=el('div','empty');e.innerHTML='<h2>'+(state.loaded?'Your board is empty':'Loading your board…')+'</h2><p>'+(state.loaded?'Add the products you trade. Pick from the hottest products on Tradepost by 30-day volume, or search every product Tradepost lists.':'')+'</p>';
if(state.loaded){{var btn=el('button','tb primary','+ Add products');btn.type='button';btn.addEventListener('click',function(){{openAdd('hot');}});e.appendChild(btn);}}b.appendChild(e);return;}}
var W=b.clientWidth||1400,H=b.clientHeight||700;var rects=layout(list,W,H);
list.forEach(function(x){{var r=rects[x.id];if(!r)return;var pr=[r[0]/W*100,r[1]/H*100,r[2]/W*100,r[3]/H*100];b.appendChild(x.p?tile(x.p,pr,W,H):pendingTile(x.u,pr));}});}}
window.addEventListener('resize',function(){{clearTimeout(window.__rt);window.__rt=setTimeout(renderBoard,150);}});
/* ---- detail ---- */
function bigChart(p){{var pts=p.closes.filter(function(c){{return c.close!=null;}});if(pts.length<2)return '<div class="muted" style="padding:30px;text-align:center">No price history in the last 30 days</div>';
var W=640,H=240,L=46,R=10,T=12,B=28;var pr=pts.map(function(c){{return c.close;}}),vo=pts.map(function(c){{return c.volume||0;}});
var refs=[];if(p.dn&&p.dn.bid)refs.push({{v:p.dn.bid,c:'dn',l:'DN pays'}});if(p.dn&&p.dn.ask)refs.push({{v:p.dn.ask,c:'dn',l:'DN sells'}});if(p.tcg&&p.tcg.market)refs.push({{v:p.tcg.market,c:'tcg',l:'TCGplayer market'}});if(p.fv)refs.push({{v:p.fv,c:'fv',l:'Tradepost fair value'}});
var all=pr.concat(refs.map(function(r){{return r.v;}}));var lo=Math.min.apply(null,all),hi=Math.max.apply(null,all);var pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;
var x=function(i){{return L+i/(pts.length-1)*(W-L-R);}},y=function(v){{return T+(hi-v)/(hi-lo)*(H-T-B);}};
var vmax=Math.max.apply(null,vo)||1,bw=Math.max((W-L-R)/pts.length*0.6,2);
var s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="30-day Tradepost price and volume">';
for(var g=0;g<4;g++){{var v=lo+(hi-lo)*g/3;s+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'"/><text class="axis" x="'+(L-4)+'" y="'+(y(v)+3).toFixed(1)+'" text-anchor="end">'+usd(v,0)+'</text>';}}
s+='<g class="vol">'+vo.map(function(v,i){{return v?'<rect x="'+(x(i)-bw/2).toFixed(1)+'" y="'+(H-B-v/vmax*60).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(v/vmax*60).toFixed(1)+'"/>':'';}}).join('')+'</g>';
refs.forEach(function(r){{s+='<line class="ref '+r.c+'" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(r.v).toFixed(1)+'" y2="'+y(r.v).toFixed(1)+'"/>';}});
var up=(p.chg30||0)>=0;s+='<path class="line" stroke="'+(up?'#3ddc84':'#ff4d6a')+'" d="'+pr.map(function(v,i){{return (i?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1);}}).join(' ')+'"/>';
[0,Math.floor((pts.length-1)/2),pts.length-1].forEach(function(i){{s+='<text class="axis" x="'+x(i).toFixed(1)+'" y="'+(H-8)+'" text-anchor="'+(i===0?'start':i===pts.length-1?'end':'middle')+'">'+esc((pts[i].t||'').slice(5))+'</text>';}});
s+='</svg><div class="legend"><span><i style="background:'+(up?'#3ddc84':'#ff4d6a')+'"></i>Tradepost daily close</span>'+refs.map(function(r){{return '<span><i style="background:'+(r.c==='dn'?'#f5b63a':r.c==='tcg'?'#bc8cff':'#8c8c95')+'"></i>'+r.l+' '+usd(r.v)+'</span>';}}).join('')+'<span><i style="background:#8c8c95;opacity:.5"></i>units sold</span></div>';return s;}}
function openDetail(p){{var o=p.outlook||{{}},dn=p.dn,st=p.story||{{}};var up=(p.chg30||0)>=0;
var img=p.image?'<img class="thumb" src="'+p.image+'" alt="">':'<span class="thumb ph">'+esc(p.initials)+'</span>';
var ms=[['Last Tradepost sale',usd(p.last),p.last_at?new Date(p.last_at).toLocaleString():'no trades in 30d'],['30-day change',pct(p.chg30,2),''],['30-day volume',compact(p.notional,true),(p.vol30||0).toLocaleString()+' units · '+(p.trades30||0).toLocaleString()+' trades'],['30-day avg price',usd(p.vwap30),'volume-weighted'],
['Tradepost pays / sells',usd(p.tpx.bid)+' / '+usd(p.tpx.ask),(p.tpx.bid_units||0).toLocaleString()+' bid × '+(p.tpx.ask_units||0).toLocaleString()+' offered'],
['Dealernet pays / sells',dn?usd(dn.bid)+' / '+usd(dn.ask):'no listing',dn?((dn.bid_qty||0)+' × '+(dn.ask_qty||0)+(dn.case_bid||dn.case_ask?' · case '+usd(dn.case_bid)+' / '+usd(dn.case_ask)+'/box':'')):''],
['TCGplayer market',p.tcg?usd(p.tcg.market):'—',p.tcg?('Tradepost '+(p.tpx.bid?(p.tpx.bid/p.tcg.market*100).toFixed(0)+'%':'—')+' · DN '+(dn&&dn.bid?(dn.bid/p.tcg.market*100).toFixed(0)+'%':'—')+' of market'):'keys not set'],
['Tradepost fair value',p.fv?usd(p.fv):'—',''],['24h range',p.lo24&&p.hi24?usd(p.lo24)+' – '+usd(p.hi24):'—','']];
var h='<div class="ph"><div class="dhead">'+img+'<div><div class="series">'+esc(p.series)+'</div><h3 style="margin:0">'+esc(p.name)+'</h3></div><div class="dprice"><b>'+usd(p.price)+'</b><span class="chg '+(up?'up':'down')+'">'+(up?'↗':'↘')+' '+Math.abs(p.chg30||0).toFixed(2)+'% · 30d</span></div></div><div><button type="button" class="tb x" id="btnRemove">Remove from board</button> <button type="button" class="tb" data-close="detail">Close</button></div></div>';
h+='<div class="dgrid"><div><div class="dchart">'+bigChart(p)+'</div>';
h+='<div class="callbox '+p.cls+'"><b>'+esc(st.badge||'')+'</b>'+(st.headline?' · '+esc(st.headline):'')+(st.steps&&st.steps.length?'<ol>'+st.steps.map(function(x){{return '<li>'+esc(x)+'</li>';}}).join('')+'</ol>':'')+(st.profit?'<div class="profit">'+esc(st.profit)+'</div>':'')+'<div class="muted small">'+esc(st.why||'')+'</div></div></div>';
h+='<div><div class="metrics">'+ms.map(function(m){{return '<div class="m"><i>'+m[0]+'</i><b>'+m[1]+'</b><div class="sub">'+esc(m[2])+'</div></div>';}}).join('')+'</div>';
h+='<div class="outlook"><h4>Outlook · next 7 days</h4><div class="olabel '+esc(o.label||'')+'">'+esc(o.label||'—')+'</div><div class="small muted">'+esc(o.confidence||'')+' confidence'+(o.range_low_cents?' · likely range '+usd(o.range_low_cents)+' – '+usd(o.range_high_cents):'')+'</div><ul>'+(o.reasons||[]).map(function(r){{return '<li>'+esc(r)+'</li>';}}).join('')+'</ul><div class="fine">Based only on the last 30 days of Tradepost trades and today\\'s order books. A description of the past, not advice.</div></div></div></div>';
$('detailBody').innerHTML=h;$('detail').hidden=false;
$('btnRemove').addEventListener('click',function(){{removeProduct(p.id,p.name);$('detail').hidden=true;}});
Array.prototype.forEach.call($('detailBody').querySelectorAll('[data-close]'),function(b){{b.addEventListener('click',function(){{$('detail').hidden=true;}});}});}}
/* ---- add panel ---- */
function isSelected(id,name){{return state.selected.some(function(s){{return (s.tpx_id&&s.tpx_id===id)||(s.name||'').toLowerCase()===(name||'').toLowerCase();}});}}
function openAdd(tab){{state.tab=tab||state.tab;$('add').hidden=false;renderRows();$('q').focus();}}
function renderRows(){{var box=$('rows');box.innerHTML='';Array.prototype.forEach.call($('addSeg').children,function(b){{b.classList.toggle('on',b.dataset.tab===state.tab);}});
var q=$('q').value.trim().toLowerCase();var list;
if(state.tab==='mine'){{list=state.selected.map(function(s){{var u=(s.tpx_id&&uniById[s.tpx_id])||null;return u||{{id:s.tpx_id||s.name,name:s.name,group:null,vol:null}};}});}}
else{{list=U.filter(function(u){{return state.addGroup==='all'||u.group===state.addGroup;}});if(state.tab==='search'||q){{if(!q){{list=list.slice(0,60);}}else{{var terms=q.split(/\\s+/);list=list.filter(function(u){{var n=u.name.toLowerCase()+' '+(u.set||'').toLowerCase();return terms.every(function(t){{return n.indexOf(t)>=0;}});}}).slice(0,80);}}}}else list=list.slice(0,25);}}
if(!list.length){{box.appendChild(el('div','muted small',q?'No Tradepost product matches "'+$('q').value+'".':'Nothing here yet.'));return;}}
list.forEach(function(u,i){{var on=isSelected(u.id,u.name);var r=el('div','r'+(on?' on':''));
r.innerHTML='<div class="rn"><b>'+(state.tab==='hot'?(i+1)+'. ':'')+esc(u.name)+'</b><div class="rs">'+esc(u.set||'')+(u.group?' · '+(u.group==='tcg'?'Pokémon/OP':'Sports'):'')+'</div></div><div class="num hide-sm"><div>'+compact(u.vol,true)+'</div><div class="rs">30d volume</div></div><div class="num hide-sm">'+(u.last?usd(u.last):'—')+'</div><div class="num"></div>';
var b=el('button','tb'+(on?' x':' primary'),on?'Remove':'+ Add');b.type='button';b.addEventListener('click',function(){{if(on)removeProduct(u.id,u.name);else addProduct(u);}});r.lastChild.appendChild(b);box.appendChild(r);}});}}
function addProduct(u){{if(isSelected(u.id,u.name))return;state.selected.push({{name:u.name,tpx_id:u.id}});save('Added '+u.name);}}
function removeProduct(id,name){{state.selected=state.selected.filter(function(s){{return !((s.tpx_id&&s.tpx_id===id)||(s.name||'').toLowerCase()===(name||'').toLowerCase());}});save('Removed '+name);}}
var saving=false,pendingSave=false;
function save(what){{renderBoard();renderRows();if(!state.db){{status('Settings cannot be saved from this view.');return;}}if(saving){{pendingSave=true;return;}}saving=true;
state.db.doc('settings/board').set({{products:state.selected,hours:state.hours,updated:new Date().toISOString()}}).then(function(){{status(what+'. Saved.');}})
.catch(function(e){{status(e&&e.code==='invalid_argument'?'Only the board owner can change this.':'Could not save ('+(e&&e.code||'error')+'). Try again.');if(e&&e.code==='invalid_argument'){{state.canWrite=false;}}}}).then(function(){{saving=false;if(pendingSave){{pendingSave=false;save('Saved');}}}});}}
function status(t){{$('setStatus').textContent=t;}}
/* ---- wiring ---- */
$('btnAdd').addEventListener('click',function(){{openAdd('hot');}});$('btnHelp').addEventListener('click',function(){{$('help').hidden=false;}});
Array.prototype.forEach.call(document.querySelectorAll('[data-close]'),function(b){{b.addEventListener('click',function(){{$(b.dataset.close).hidden=true;}});}});
document.addEventListener('keydown',function(e){{if(e.key==='Escape'){{['help','add','detail'].forEach(function(id){{$(id).hidden=true;}});}}}});
Array.prototype.forEach.call($('groupSeg').children,function(b){{b.addEventListener('click',function(){{state.group=b.dataset.g;Array.prototype.forEach.call($('groupSeg').children,function(x){{x.classList.toggle('on',x===b);}});renderBoard();}});}});
Array.prototype.forEach.call($('addSeg').children,function(b){{b.addEventListener('click',function(){{state.tab=b.dataset.tab;renderRows();}});}});
Array.prototype.forEach.call($('addGroup').children,function(b){{b.addEventListener('click',function(){{state.addGroup=b.dataset.g;Array.prototype.forEach.call($('addGroup').children,function(x){{x.classList.toggle('on',x===b);}});renderRows();}});}});
$('q').addEventListener('input',function(){{if($('q').value&&state.tab==='hot')state.tab='search';renderRows();}});
$('cadence').addEventListener('change',function(){{state.hours=parseInt($('cadence').value,10);save('Refresh set');}});
try{{if(!localStorage.getItem('seenHelp')&&location.hash!=='#tv'){{$('help').hidden=false;localStorage.setItem('seenHelp','1');}}}}catch(e){{}}
renderBoard();
var settled=false;function settle(){{if(settled)return;settled=true;state.loaded=true;renderBoard();}}
setTimeout(settle,4000);
$('btnRefresh').onclick=function(){{var b=this;b.disabled=true;b.textContent='↻ Refreshing…';var done=function(msg){{b.textContent=msg;setTimeout(function(){{b.disabled=false;b.textContent='↻ Refresh';}},6000);}};var fallback=function(){{done('↻ Reloading…');setTimeout(function(){{location.reload();}},800);}};if(!(window.claude&&claude.use))return fallback();claude.use('comments').then(function(c){{if(!c||!c.canSendToClaude)return fallback();return Promise.resolve(c.canSendToClaude()).then(function(st){{if(st!=='available')return fallback();return c.anchorFor(b).then(function(a){{return c.sendToClaude({{anchor:a,text:'Refresh the board now: pull fresh Tradepost, Dealernet and TCGplayer prices and republish.'}});}}).then(function(){{done('✓ Refresh requested · new prices in ~10 min');}});}});}}).catch(function(){{fallback();}});}};
if(window.claude&&claude.use){{claude.use('db').then(function(db){{if(!db){{settle();return;}}state.db=db;
db.doc('settings/board').onSnapshot(function(snap){{var d=snap.exists?snap.data():null;if(d){{if(Array.isArray(d.products))state.selected=d.products.slice();if(typeof d.hours==='number'){{state.hours=d.hours;$('cadence').value=String(d.hours);}}}}settle();renderBoard();if(!$('add').hidden)renderRows();}},function(){{settle();}});}});
claude.use('user').then(function(u){{if(!u)return;Promise.resolve(u.can('data.write')).then(function(w){{state.canWrite=w;if(w===false)status('You can view this board; only its owner can change it.');}});}});}}else settle();
}})();
</script></body></html>"""
