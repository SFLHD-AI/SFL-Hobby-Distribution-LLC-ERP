"""TCGplayer market price, used as a reference for sealed TCG products.

Read-only: OAuth client-credentials token + GET /pricing/product. Nothing here
lists, buys or touches inventory.

* Auth: POST {api}/token with grant_type=client_credentials, client_id = public
  key, client_secret = private key -> bearer token (cached on disk until it
  expires, roughly two weeks).
* Prices: GET {api}/pricing/product/{ids} -> per productId and subTypeName
  (sealed is "Normal"): marketPrice, lowPrice, midPrice, directLowPrice (USD).
* TPX -> TCGplayer product id comes from TPX's own SEALED_TCG catalog feed
  (externalIds.TCG_PLAYER_ID), so there is no name matching. Sports wax has no
  TCGplayer listing and stays blank.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

import httpx

log = logging.getLogger("edge.tcgplayer")

FEED_MAX_AGE_S = 24 * 3600


@dataclass
class TCGPrice:
    product_id: str
    market_cents: Optional[int]
    low_cents: Optional[int]
    mid_cents: Optional[int]
    direct_low_cents: Optional[int]
    sub_type: Optional[str] = None


def _cents(v) -> Optional[int]:
    if v is None:
        return None
    return int((Decimal(str(v)) * 100).quantize(Decimal("1")))


def pct_of(price_cents: Optional[int], ref_cents: Optional[int]) -> Optional[Decimal]:
    """price as % of the reference (100 == at market). None when either is missing."""
    if price_cents is None or not ref_cents:
        return None
    return (Decimal(price_cents) * 100 / Decimal(ref_cents)).quantize(Decimal("0.1"))


class TCGplayerClient:
    def __init__(self, public_key: str, private_key: str, api_url: str, cache_dir: Path, *,
                 transport: Optional[httpx.BaseTransport] = None, timeout_s: float = 20.0):
        if not public_key or not private_key:
            raise RuntimeError("TCGPLAYER_PUBLIC_KEY and TCGPLAYER_PRIVATE_KEY must both be set.")
        self.api = api_url.rstrip("/")
        self._pub, self._priv = public_key, private_key
        self._token_path = Path(cache_dir) / "tcgplayer_token.json"
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _token(self) -> str:
        try:
            cached = json.loads(self._token_path.read_text())
            if cached.get("expires_at", 0) > time.time() + 300:
                return cached["access_token"]
        except (OSError, ValueError, KeyError):
            pass
        r = self._http.post(f"{self.api}/token", data={
            "grant_type": "client_credentials", "client_id": self._pub, "client_secret": self._priv})
        if r.status_code in (400, 401, 403):
            raise RuntimeError(f"TCGplayer rejected the API keys (HTTP {r.status_code}).")
        r.raise_for_status()
        body = r.json()
        tok = body["access_token"]
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({"access_token": tok,
                                                "expires_at": time.time() + int(body.get("expires_in", 3600))}))
        return tok

    def prices(self, product_ids: Iterable[str]) -> dict[str, TCGPrice]:
        ids = [str(i) for i in dict.fromkeys(product_ids) if i]
        out: dict[str, TCGPrice] = {}
        for i in range(0, len(ids), 100):  # API takes comma-separated ids; keep URLs short
            chunk = ids[i:i + 100]
            r = self._http.get(f"{self.api}/pricing/product/{','.join(chunk)}",
                               headers={"Authorization": f"bearer {self._token()}", "Accept": "application/json"})
            if r.status_code == 404:  # none of the ids priced
                continue
            r.raise_for_status()
            for row in r.json().get("results") or []:
                pid = str(row.get("productId"))
                # Sealed products price under "Normal"; keep the first priced row otherwise.
                if pid in out and out[pid].sub_type == "Normal":
                    continue
                if row.get("marketPrice") is None and row.get("lowPrice") is None and pid in out:
                    continue
                out[pid] = TCGPrice(pid, _cents(row.get("marketPrice")), _cents(row.get("lowPrice")),
                                    _cents(row.get("midPrice")), _cents(row.get("directLowPrice")),
                                    row.get("subTypeName"))
        return out


class TCGplayerIdMap:
    """TPX assetConfigurationId -> TCGplayer productId, from TPX's SEALED_TCG catalog feed."""

    def __init__(self, tpx_client, cache_dir: Path):
        from .tpx import queries as Q

        self._q = Q.GET_CATALOG_FEEDS
        self.tpx = tpx_client
        self.path = Path(cache_dir) / "tcgplayer_ids.json"
        self._map: Optional[dict[str, str]] = None

    def get(self, asset_configuration_id: str) -> Optional[str]:
        if self._map is None:
            self._map = self._load()
        return self._map.get(str(asset_configuration_id))

    def _load(self) -> dict[str, str]:
        try:
            if time.time() - self.path.stat().st_mtime < FEED_MAX_AGE_S:
                return json.loads(self.path.read_text())
        except (OSError, ValueError):
            pass
        try:
            payload = self.tpx.execute(self._q, {"input": {"assetTypes": ["SEALED_TCG"]}}, cache_ttl=600)
            feeds = payload.get("feeds") or []
            if not feeds:
                return {}
            raw = httpx.get(feeds[0]["uri"], timeout=120).content
            doc = json.loads(gzip.decompress(raw))
        except Exception as exc:  # noqa: BLE001 - reference data; the dashboard works without it
            log.warning("TPX catalog feed unavailable, no TCGplayer ids: %s", exc)
            return {}
        m = {str(c["assetConfigurationId"]): str(c["externalIds"]["TCG_PLAYER_ID"])
             for a in doc.get("assets") or [] for c in a.get("configurations") or []
             if (c.get("externalIds") or {}).get("TCG_PLAYER_ID")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(m))
        return m
