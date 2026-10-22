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


def _facts(v: ProductView) -> str:
    bits = []
    if v.vol30:
        bits.append(f"TPX sold {v.vol30:,} in the last 30 days")
    else:
        bits.append("No TPX trades in the last 30 days")
    if v.chg30 is not None:
        bits.append(f'TPX price <span class="{_cls(v.chg30)}">{"up" if v.chg30 >= 0 else "down"} {_pct(abs(v.chg30), False)}</span> in 30 days')
    if v.tcg:
        bits.append(f"TCGplayer market price {_usd(v.tcg)} (TPX pays {pct_of(v.tpx_bid, v.tcg) or '—'}%, Dealernet pays {pct_of(v.dn_bid, v.tcg) or '—'}% of it)")
    return " · ".join(bits)


def _card(st: Story, rank: int) -> str:
    v = st.v
    spark = sparkline([p["close"] for p in v.closes][-30:], baseline=None)
    steps = "".join(f'<li>{_e(x)}</li>' for x in st.steps)
    return (
        f'<article class="card {st.cls}" data-rank="{rank}">'
        f'<div class="card-top"><span class="badge {st.cls}">{_e(st.badge)}</span><span class="rank">#{rank}</span></div>'
        f'<h3>{_e(v.name)}</h3>'
        f'<p class="headline">{_e(st.headline)}</p>'
        f'<ol class="steps">{steps}</ol>'
        f'<p class="profit">{_e(st.profit)}</p>'
        f'<p class="why">{_e(st.why)}</p>'
        + (f'<p class="why caution">Caution: nothing has traded on TPX in the last 30 days, so this order could sit unfilled for a long time.</p>' if not v.vol30 and st.cls == "post" else "")
        + f'<div class="facts"><span>{_facts(v)}</span>{spark}</div>'
        + '</article>'
    )


def _quiet_row(st: Story) -> str:
    v = st.v
    spark = sparkline([p["close"] for p in v.closes][-30:], baseline=None)
    return (
        f'<div class="qrow"><div class="qname"><b>{_e(v.name)}</b><div class="small muted">{_e(st.why)}</div>'
        f'<div class="small muted">{_facts(v)}</div></div>'
        f'<div class="qprice"><span class="small muted">TPX</span><br>{_usd(v.last or v.tpx_bid)}</div>'
        f'<div class="qprice"><span class="small muted">Dealernet pays / sells</span><br>{_usd(v.dn_bid)} / {_usd(v.dn_ask)}</div>'
        f'<div class="qspk">{spark}</div></div>'
    )


def _summary(stories: list[Story], generated_at: datetime) -> str:
    now = [s for s in stories if s.cls in ("edge", "thin")]
    post = [s for s in stories if s.cls == "post"]
    almost = [s for s in stories if s.cls == "behind"]
    best = max((s for s in now + post), key=lambda s: _profit_cents(s), default=None)
    tiles = [
        ("Ready to trade now", str(len(now)), "both prices are live right now", "pos" if now else ""),
        ("Post an order and wait", str(len(post)), "you would be first in line on TPX", "pos" if post else ""),
        ("Almost", str(len(almost)), "someone is slightly ahead of you", ""),
        ("Best profit per unit", _usd(_profit_cents(best)) if best else "—", _e(best.v.short) if best else "nothing clears the target today", "pos" if best else ""),
    ]
    return "".join(f'<div class="m"><div class="m-l">{l}</div><div class="m-v {c}">{val}</div><div class="m-s">{sub}</div></div>'
                   for l, val, sub, c in tiles)


def _profit_cents(st: Optional[Story]) -> Optional[Decimal]:
    if st is None:
        return None
    v = st.v
    if st.cls in ("edge", "thin") and v.arb:
        return v.arb.edge_per_unit_cents
    best = max((d for d in (v.buy_tpx, v.buy_dn) if d.post), key=lambda d: d.post_rank, default=None)
    return best.post.expected_edge_cents if best and best.post else None


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
    stories = [Story(v) for v in views]
    opps = [s for s in stories if s.is_opportunity]
    quiet = [s for s in stories if not s.is_opportunity]

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
    cards = "".join(_card(s, i + 1) for i, s in enumerate(opps)) or (
        '<div class="empty">Nothing clears the target right now. The list below says why for each product.</div>')
    qrows = "".join(_quiet_row(s) for s in quiet)
    m = f"{Decimal(margin) * 100:.0f}%" if margin is not None else "8%"
    return TEMPLATE.format(
        meta_refresh=meta_refresh, generated=_e(generated_at.strftime("%b %d, %Y · %H:%M UTC")), live=live,
        actions=actions, banner=banner + warn_html, ticker=_ticker(views), summary=_summary(stories, generated_at),
        margin=m, cards=cards, n_opps=len(opps), qrows=qrows or '<p class="muted">Everything has a trade.</p>', n_quiet=len(quiet),
        data=data, refresh_note=(f"Refreshes every {refresh_s} s." if refresh_s else "Re-run <code>edge dashboard</code> to refresh."),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta_refresh}<title>TPX Dealernet Edges</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
/* Dark-first board: one green for "money", one amber for "do something", everything else quiet. */
:root{{--bg:#0d1117;--panel:#161b22;--panel-2:#1c232d;--ink:#e6edf3;--muted:#8b98a8;--line:#2a3340;--line-2:#3a4656;
--pos:#3fb950;--pos-soft:#12301c;--neg:#f85149;--flat:#8b98a8;--accent:#f0b429;--accent-soft:#33280c;--accent-ink:#1a1200;--banner:#2a2410;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
color-scheme:dark}}
@media (prefers-color-scheme:light){{:root:not([data-theme="dark"]){{--bg:#f3f5f8;--panel:#ffffff;--panel-2:#eef1f5;--ink:#141a22;--muted:#5d6b7c;
--line:#d9dfe7;--line-2:#c3ccd7;--pos:#1a7f37;--pos-soft:#e3f5e8;--neg:#cf222e;--flat:#5d6b7c;--accent:#b7791f;--accent-soft:#fff1cf;--accent-ink:#ffffff;--banner:#fff4d6;color-scheme:light}}}}
:root[data-theme="light"]{{--bg:#f3f5f8;--panel:#ffffff;--panel-2:#eef1f5;--ink:#141a22;--muted:#5d6b7c;
--line:#d9dfe7;--line-2:#c3ccd7;--pos:#1a7f37;--pos-soft:#e3f5e8;--neg:#cf222e;--flat:#5d6b7c;--accent:#b7791f;--accent-soft:#fff1cf;--accent-ink:#ffffff;--banner:#fff4d6;color-scheme:light}}
*{{box-sizing:border-box}}html,body{{margin:0}}
body{{background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:0 48px}}
main{{max-width:1480px;margin:0 auto}}
h1{{font-size:20px;font-weight:700;margin:0;letter-spacing:.01em}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:26px 0 12px;font-weight:600}}
.mono,.m-v,.tk-p,.tk-c,.qprice{{font-family:var(--mono);font-variant-numeric:tabular-nums}}
.muted{{color:var(--muted)}}.small{{font-size:13px}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.flat{{color:var(--flat)}}
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
.tape-track{{display:inline-flex;white-space:nowrap;animation:tape 90s linear infinite;will-change:transform}}
.tape:hover .tape-track{{animation-play-state:paused}}
@keyframes tape{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tk{{display:inline-flex;gap:10px;align-items:baseline;padding:9px 18px;border-right:1px solid var(--line);font-size:13px}}
.tk-n{{color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:500}}.tk-p{{font-weight:600}}
@media (prefers-reduced-motion:reduce){{.tape-track{{animation:none;white-space:normal;flex-wrap:wrap}}.tape-track .tk:nth-child(n+1000){{display:none}}.live i{{animation:none}}}}
/* summary tiles */
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-top:16px}}
.m{{background:var(--panel);padding:12px 16px;min-width:0}}
.m-l{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.m-v{{font-size:30px;font-weight:600;line-height:1.2;margin:4px 0 2px}}
.m-s{{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
/* opportunity cards */
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}}
.card{{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--line-2);border-radius:8px;padding:16px 18px 14px;min-width:0;display:flex;flex-direction:column;gap:8px}}
.card.edge,.card.thin{{border-left-color:var(--pos)}}.card.post{{border-left-color:var(--accent)}}.card.behind{{border-left-color:var(--line-2)}}
.card[data-rank="1"],.card[data-rank="2"]{{grid-column:span 1}}
.card-top{{display:flex;justify-content:space-between;align-items:center}}
.badge{{display:inline-block;font:600 12px/1 var(--sans);letter-spacing:.04em;padding:6px 10px;border-radius:4px;border:1px solid var(--line-2);color:var(--muted);text-transform:uppercase}}
.badge.edge{{background:var(--pos);color:#04200b;border-color:var(--pos)}}.badge.thin{{background:var(--pos-soft);color:var(--pos);border-color:var(--pos)}}
.badge.post{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}.badge.behind{{color:var(--muted)}}
.rank{{font:600 13px var(--mono);color:var(--muted)}}
.card h3{{font-size:20px;font-weight:700;margin:0;line-height:1.25;text-wrap:balance}}
.headline{{margin:0;font-size:15px;font-weight:600;color:var(--muted)}}
.steps{{margin:0;padding-left:0;list-style:none;counter-reset:s;display:flex;flex-direction:column;gap:6px}}
.steps li{{counter-increment:s;display:flex;gap:10px;align-items:baseline;font-size:16px}}
.steps li::before{{content:counter(s);flex:none;width:22px;height:22px;border-radius:50%;background:var(--panel-2);border:1px solid var(--line-2);font:600 12px/20px var(--mono);text-align:center;color:var(--ink)}}
.profit{{margin:2px 0 0;font-size:17px;font-weight:600;color:var(--pos)}}.card.behind .profit{{color:var(--muted);font-weight:500}}
.why{{margin:0;color:var(--muted);font-size:14px}}.caution{{color:var(--accent)}}
.facts{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:8px;margin-top:2px}}
.empty{{background:var(--panel);border:1px dashed var(--line-2);border-radius:8px;padding:22px;color:var(--muted)}}
.spark{{width:96px;height:26px;flex:none}}.spark-pos{{stroke:var(--pos)}}.spark-neg{{stroke:var(--neg)}}.spark-pos-dot{{fill:var(--pos)}}.spark-neg-dot{{fill:var(--neg)}}.grid{{stroke:var(--line);stroke-width:1}}
/* quiet list */
.quiet{{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
.qrow{{display:grid;grid-template-columns:minmax(0,3fr) 110px 190px 200px;gap:0 16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}}
.qrow:last-child{{border-bottom:0}}.qname{{min-width:0}}.qname b{{font-weight:600}}
.qprice{{text-align:right;white-space:nowrap;font-size:14px}}.qspk{{display:flex;justify-content:flex-end;gap:6px}}
@media (max-width:760px){{.qrow{{grid-template-columns:1fr 1fr}}.qname{{grid-column:1/-1}}.qspk{{grid-column:1/-1;justify-content:flex-start}}.cards{{grid-template-columns:1fr}}.m-v{{font-size:24px}}}}
footer{{margin-top:28px}}code{{font-family:var(--mono);font-size:12px}}
/* big-screen mode: #tv */
html.tv body{{font-size:17px}}html.tv main{{max-width:none}}html.tv footer,html.tv .banner{{display:none}}
html.tv .cards{{grid-template-columns:repeat(auto-fill,minmax(480px,1fr))}}html.tv .card h3{{font-size:23px}}html.tv .steps li{{font-size:18px}}html.tv .profit{{font-size:20px}}
html.tv .m-v{{font-size:38px}}html.tv .tk{{font-size:15px;padding:12px 22px}}html.tv h2{{margin-top:18px}}
</style></head><body><main>
<div class="top"><div class="top-l"><h1>Where you can make money today</h1>{live}<span class="muted small">Updated {generated}</span></div>
<div class="top-l"><span class="muted small">TPX vs Dealernet · {margin} profit target · every number is after fees &amp; shipping</span>{actions}</div></div>
{ticker}
{banner}
<div class="metrics">{summary}</div>
<h2>Do this · {n_opps} products</h2>
<div class="cards">{cards}</div>
<h2>Nothing to do right now · {n_quiet} products</h2>
<div class="quiet">{qrows}</div>
<footer class="muted small">View only. This page never places, lists or cancels anything. {refresh_note} Add <code>#tv</code> to the link for the big-screen view.</footer>
</main>
<script id="meta" type="application/json">{data}</script>
<script>
(function(){{
if(location.hash==='#tv')document.documentElement.classList.add('tv');
window.addEventListener('hashchange',function(){{document.documentElement.classList.toggle('tv',location.hash==='#tv');}});
}})();
</script></body></html>"""
