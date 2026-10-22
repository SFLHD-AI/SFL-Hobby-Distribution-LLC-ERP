"""Dealernet page reader (DEALERNET_ACCESS=scrape). Read-only.

Logs in with DEALERNET_USERNAME / DEALERNET_PASSWORD (plain form post, session
cookie kept on disk under .cache/) and reads product listing pages:

  * header: "Current High Bid" / "Current Low Ask" (per box), UPC
  * "Sell To" table -> bids, "Buy From" table -> asks; each row has dealer,
    qty (a CASE badge means qty is in cases), notes, per-box price
  * "N boxes per case" so case products are priced per case

Only GETs of search.php and listing.php after login. It never posts a form
other than the login, never touches the cart, listings or offers, and waits
at least `min_interval_s` (3 s) between page loads.

Which page belongs to a product: mappings.yaml `dealernet_url` wins, then a
cached earlier resolution (.cache/dealernet_urls.json), then Dealernet's own
product search scored with page_match_score (stricter than matching.score:
format words such as Sapphire / Breakers Delight / Binder must agree).
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin

import httpx

from ..matching import Mappings, normalize
from .base import DealernetLevel, DealernetQuote, DealernetSource, dollars_to_cents

log = logging.getLogger("edge.dealernet.scrape")

BASE = "https://www.dealernetx.com/"
MATCH_THRESHOLD = 65.0
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_TD_RE = re.compile(r"(?is)<td\b([^>]*)>(.*?)</td>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _first_price(fragment: str) -> Optional[int]:
    m = _PRICE_RE.search(_text(fragment))
    return dollars_to_cents(m.group(1)) if m else None


def parse_search(page: str) -> list[dict]:
    """search.php results -> [{name, variation, upc, url}]."""
    out = []
    for row in _ROW_RE.findall(page):
        if "product-link" not in row:
            continue
        cells = {m.group(1): m.group(2) for m in re.finditer(r"(?is)<td[^>]*data-label='([^']+)'[^>]*>(.*?)</td>", row)}
        link = re.search(r"href='([^']+)'", cells.get("Product", ""))
        if not link:
            continue
        variation = _text(cells.get("Variation", ""))
        out.append({"name": _text(cells.get("Product", "")),
                    "variation": "" if variation == "-" else variation,
                    "upc": _text(cells.get("UPC", "")),
                    "url": urljoin(BASE, htmllib.unescape(link.group(1)))})
    return out


def _offer_rows(table: str, observed: datetime, boxes_per_case: Optional[int]) -> list[tuple[DealernetLevel, bool]]:
    rows = []
    for row in _ROW_RE.findall(table):
        tds = _TD_RE.findall(row)
        if len(tds) < 5:
            continue
        price = _first_price(tds[-1][1])
        qty_m = re.search(r"\d+", _text(tds[2][1]))
        if price is None or not qty_m:
            continue
        is_case = "uom-case" in row or "CASE" in _text(tds[2][1])
        member = re.search(r'<a [^>]*title="([^"]+)"', tds[1][1])
        rows.append((DealernetLevel(price_cents=price, quantity=int(qty_m.group(0)),
                                    member=member.group(1) if member else None, observed_at=observed), is_case))
    return rows


def parse_listing(page: str, product: str, url: str, *, want_case: bool = False,
                  now: Optional[datetime] = None) -> DealernetQuote:
    """listing.php -> DealernetQuote. Prices on the page are per box; a case
    product gets case-UOM rows only, priced x boxes per case."""
    now = now or datetime.now(timezone.utc)
    bpc_m = re.search(r"(\d+)\s+boxes per case", page)
    bpc = int(bpc_m.group(1)) if bpc_m else None
    bids: list[DealernetLevel] = []
    asks: list[DealernetLevel] = []
    for card in re.split(r'(?i)<div class="offer-card"', page)[1:]:
        title = _text(re.search(r"(?is)<h2[^>]*>(.*?)</h2>", card).group(1)) if "<h2" in card else ""
        side = bids if title.lower().startswith("sell") else asks if title.lower().startswith("buy") else None
        if side is None:
            continue
        for lvl, is_case in _offer_rows(card, now, bpc):
            if want_case:
                if not is_case or not bpc:
                    continue
                lvl.price_cents *= bpc
            elif is_case and bpc:
                lvl.quantity *= bpc  # case qty -> boxes; price is already per box
            side.append(lvl)
    q = DealernetQuote(product=product, bids=bids, asks=asks, fetched_at=now, source="dealernet page", url=url)
    hb = re.search(r"Current High Bid:\s*<strong>([^<]*)</strong>", page)
    la = re.search(r"Current Low Ask:\s*<strong>([^<]*)</strong>", page)
    q_header = {"high_bid": _first_price(hb.group(1)) if hb else None,
                "low_ask": _first_price(la.group(1)) if la else None,
                "upc": (re.search(r"UPC:</strong>\s*([0-9]+)", page) or [None, None])[1],
                "boxes_per_case": bpc}
    # The header is the page's own summary; fall back to it when a side has no parseable rows.
    if not q.bids and q_header["high_bid"] and not want_case:
        q.bids.append(DealernetLevel(price_cents=q_header["high_bid"], quantity=1, observed_at=now))
    if not q.asks and q_header["low_ask"] and not want_case:
        q.asks.append(DealernetLevel(price_cents=q_header["low_ask"], quantity=1, observed_at=now))
    return q


_SERIES = re.compile(r"\b(mega evolution|scarlet (and|&) violet|sword (and|&) shield|sun (and|&) moon|pokemon|"
                     r"nba|mlb|nfl|nhl|split year|single year)\b")
_SYNONYMS = [(re.compile(r"\belite trainer box\b"), "etb"), (re.compile(r"\bpokemon center\b|\bpkc\b"), "pc"),
             (re.compile(r"\btins\b"), "tin"), (re.compile(r"\bbreakers?'? delight\b"), "breakers delight"),
             (re.compile(r"\bsuper[- ]premium\b"), "spc"), (re.compile(r"\bultra[- ]premium\b"), "upc"),
             (re.compile(r"\bbooster \(sleeved\)|\bsleeved booster packs?\b"), "sleeved pack"),
             (re.compile(r"\bbooster \(bundle\)"), "bundle"), (re.compile(r"\bbooster \(unlimited\)"), "booster box"),
             (re.compile(r"\bbooster packs?\b"), "pack")]
_STOP = {"box", "boxes", "edition", "the", "of", "and", "factory", "sealed", "cards", "set", "v1", "v2", "v3"}
_MCODE = re.compile(r"^m\d+$")
# Words that name a different product format; one side having it and the other not is a mismatch.
_FORMAT_WORDS = {"hobby", "jumbo", "blaster", "mega", "hanger", "cello", "retail", "breakers", "delight", "fotl", "choice",
                 "binder", "blister", "bundle", "display", "tin", "mini", "etb", "pc", "pack", "poster", "stickers",
                 "build", "battle", "sleeved", "booster", "sapphire", "logofractor", "black", "update",
                 "cosmic", "definitive", "pristine", "finest", "museum", "spc", "upc"}
_SOFT_FORMAT_WORDS = {"collection", "x", "ex"}
_BOX_FORMATS = _FORMAT_WORDS - {"hobby", "black", "update", "cosmic", "definitive", "pristine", "finest", "museum", "sapphire", "logofractor"}
_SPORTS = {"baseball", "basketball", "football", "soccer", "hockey", "golf", "racing", "tennis", "wrestling", "ufc"}
_CASE_WORDS = re.compile(r"\b(\d+\s*-?\s*box(es)?\s*)?case\b")
_YEAR = re.compile(r"\b(?:(19|20)(\d{2})(?:[-/]\d{2,4})?|(\d{2})/\d{1,2})\b")


def _page_tokens(name: str) -> tuple[Optional[int], set[str]]:
    s = name.lower().replace("&", " & ")
    y = None
    m = _YEAR.search(s)
    if m:
        y = 2000 + int(m.group(3)) if m.group(3) else int(m.group(1) + m.group(2))
        s = s.replace(m.group(0), " ")
    s = _CASE_WORDS.sub(" ", s)
    s = _SERIES.sub(" ", s)
    for pat, rep in _SYNONYMS:
        s = pat.sub(rep, s)
    toks = {t for t in re.split(r"[^\w]+", s) if t and t not in _STOP and not _MCODE.match(t)}
    return y, toks


def page_match_score(tpx_name: str, dn_name: str) -> float:
    """0..100, strict: years must agree, format words must agree, and one side's
    words must be (almost) all present in the other. Case vs box is ignored on
    purpose: Dealernet lists cases on the box page (as a CASE unit)."""
    ya, a = _page_tokens(tpx_name)
    yb, b = _page_tokens(dn_name)
    if not a or not b or (ya and yb and ya != yb):
        return 0.0
    sa, sb = a & _SPORTS, b & _SPORTS
    if sa and sb and sa != sb:
        return 0.0
    inter = len(a & b)
    cov = (inter / len(a) + inter / len(b)) / 2
    mism = (a ^ b) & _FORMAT_WORDS
    if "hobby" in mism:
        # Dealernet says "Hobby Jumbo" / "Sapphire Baseball Hobby" where TPX says "Jumbo Box" /
        # "Sapphire Edition Box": a lone "hobby" only matters against a different box format.
        other = b if "hobby" in a else a
        if not (other & _BOX_FORMATS) or ("jumbo" in a and "jumbo" in b):
            mism = mism - {"hobby"}
            a, b = a - {"hobby"}, b - {"hobby"}
            inter = len(a & b)
            cov = (inter / len(a) + inter / len(b)) / 2 if a and b else 0.0
    return max(0.0, 100.0 * cov - 30.0 * len(mism) - 10.0 * len((a ^ b) & _SOFT_FORMAT_WORDS))


def search_queries(name: str) -> list[str]:
    """Dealernet search is a substring match on its own naming ("... POKEMON Etb"),
    so try the product's core words, then shorter prefixes of them."""
    n = normalize(name)
    words = [w for w in re.split(r"\s+", re.sub(r"[^\w\s/&\-]", " ", name)) if w]
    fmt_words = {"elite", "trainer", "box", "boxes", "hobby", "jumbo", "blaster", "case", "pack", "booster",
                 "display", "bundle", "tin", "collection", "edition", "sealed", "factory", "sleeved", "premium",
                 "ultra-premium", "super-premium", "mini", "exclusive"}
    core = [w for w in words if w.lower() not in fmt_words and not re.fullmatch(r"\d+", w) and not
            re.fullmatch(r"(19|20)\d{2}(/\d{2})?", w)]
    qs = []
    if n.year and core:
        qs.append(" ".join([str(n.year)] + core[:3]))
    for k in (len(core), 3, 2):
        if core[:k]:
            qs.append(" ".join(core[:k]))
    return list(dict.fromkeys(q for q in qs if q))


class ScrapeSource(DealernetSource):
    name = "scrape"

    def __init__(self, username: str, password: str, cache_dir: Path, mappings: Optional[Mappings] = None, *,
                 min_interval_s: float = 3.0, transport: Optional[httpx.BaseTransport] = None):
        if not username or not password:
            raise RuntimeError("DEALERNET_ACCESS=scrape needs DEALERNET_USERNAME and DEALERNET_PASSWORD.")
        self._user, self._pass = username, password
        self.mappings = mappings
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._urls_path = Path(cache_dir) / "dealernet_urls.json"
        try:
            self._urls: dict[str, str] = json.loads(self._urls_path.read_text())
        except (OSError, ValueError):
            self._urls = {}
        self._http = httpx.Client(follow_redirects=True, timeout=30, transport=transport,
                                  headers={"User-Agent": "Mozilla/5.0 (edge-finder; read-only)"})
        self._logged_in = False

    def close(self) -> None:
        self._http.close()

    # --- http -------------------------------------------------------------
    def _wait(self) -> None:
        gap = self.min_interval_s - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def _login(self) -> None:
        self._wait()
        self._http.get(urljoin(BASE, "login.php"))
        self._wait()
        r = self._http.post(urljoin(BASE, "login.php"), data={
            "userName": self._user, "userPass": self._pass, "tz": "America/New_York", "loginBtn": "Go"})
        if not _logged_in(r.text):
            raise RuntimeError("Dealernet login failed (check DEALERNET_USERNAME / DEALERNET_PASSWORD).")
        self._logged_in = True

    def _get(self, url: str) -> str:
        if not self._logged_in:
            self._login()
        self._wait()
        r = self._fetch(url)
        if not _logged_in(r.text):  # session expired
            self._login()
            self._wait()
            r = self._fetch(url)
        r.raise_for_status()
        return r.text

    def _fetch(self, url: str) -> httpx.Response:
        # Dealernet drops the odd keep-alive connection mid-run; one retry is enough.
        for attempt in range(3):
            try:
                return self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise
                log.debug("retrying %s after %s", url, exc)
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    # --- product -> page ----------------------------------------------------
    def resolve_url(self, product: str) -> Optional[str]:
        if self.mappings:
            for p in self.mappings.products:
                if product.strip().lower() in ((p.get("dealernet_name") or "").lower(), (p.get("tpx_name") or "").lower()):
                    if p.get("dealernet_url"):
                        return p["dealernet_url"]
        if product in self._urls:
            return self._urls[product] or None
        best: Optional[tuple[float, dict]] = None
        seen: set[str] = set()
        for q in search_queries(product):
            for hit in parse_search(self._get(urljoin(BASE, "search.php?" + urlencode({"keywordsearch": q})))):
                if hit["url"] in seen:
                    continue
                seen.add(hit["url"])
                sc = page_match_score(product, f'{hit["name"]} {hit["variation"]}'.strip())
                if best is None or sc > best[0]:
                    best = (sc, hit)
            if best and best[0] >= 90:
                break
        url = best[1]["url"] if best and best[0] >= MATCH_THRESHOLD else ""
        if best:
            log.info("Dealernet match for %r: %r (%.0f)%s", product, best[1]["name"], best[0], "" if url else " - rejected")
        self._urls[product] = url
        self._urls_path.parent.mkdir(parents=True, exist_ok=True)
        self._urls_path.write_text(json.dumps(self._urls, indent=2))
        return url or None

    def get_quote(self, product: str) -> Optional[DealernetQuote]:
        url = self.resolve_url(product)
        if not url:
            return None
        return parse_listing(self._get(url), product, url, want_case=normalize(product).is_case)


def _logged_in(page: str) -> bool:
    return ">Logout<" in page or "logout.php" in page
