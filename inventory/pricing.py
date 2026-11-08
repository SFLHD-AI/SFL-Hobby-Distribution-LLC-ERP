"""
TCGplayer market prices for sealed product.

Two ways to get a price for a TCGplayer product id:

* **Official API** (api.tcgplayer.com) when a public/private key pair is set in
  Settings — the supported path. TCGplayer stopped issuing new keys to the
  general public, so most stores will not have these.
* **Public price points** (mpapi.tcgplayer.com/v2/product/<id>/pricepoints),
  the same endpoint tcgplayer.com's own product page uses. No key needed;
  unofficial, so it may change — failures are recorded on the product and
  never raise.

Only products in the category named in Settings (default "Sealed TCG
Product") are refreshed; the hourly job runs at the top of every hour.
"""
import logging
import re
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.conf import settings as dj_settings
from django.utils import timezone

log = logging.getLogger(__name__)
CENTS = Decimal("0.01")


class PricingError(Exception):
    pass


def eligible_category_name():
    from core.models import CompanySettings
    return (CompanySettings.get().market_price_category or "Sealed TCG Product").strip()


def parse_tcgplayer_id(text):
    """Accept a bare id or any tcgplayer.com URL (…/product/123456/…)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = re.search(r"/product/(\d+)", text)
    return int(m.group(1)) if m else None


class TcgplayerSiteProvider:
    """Unofficial: the price points endpoint behind tcgplayer.com product pages."""
    name = "tcgplayer.com"
    base = "https://mpapi.tcgplayer.com/v2/product/{id}/pricepoints"

    def is_configured(self):
        return True

    def fetch(self, product_id):
        try:
            resp = requests.get(self.base.format(id=product_id), timeout=20,
                                headers={"Accept": "application/json",
                                         "User-Agent": "SFL-ERP/4.0 (+market price check)"})
        except requests.RequestException as e:
            raise PricingError(f"TCGplayer unreachable: {e}") from e
        if resp.status_code == 404:
            raise PricingError("TCGplayer product id not found.")
        if resp.status_code >= 400:
            raise PricingError(f"TCGplayer {resp.status_code}")
        rows = resp.json() or []
        # one row per printing/condition; sealed product has a single "Normal" row
        pick = next((r for r in rows if (r.get("printingType") or "Normal") == "Normal"), rows[0] if rows else None)
        if not pick or pick.get("marketPrice") in (None, 0):
            raise PricingError("No market price listed yet.")
        return {"market": _money(pick.get("marketPrice")), "low": _money(pick.get("listedMedianPrice") or pick.get("lowestPrice") or pick.get("marketPrice"))}


class TcgplayerApiProvider:
    """Official API with a bearer token from a public/private key pair."""
    name = "TCGplayer API"
    base = "https://api.tcgplayer.com"

    def __init__(self, public_key, private_key):
        self.public_key, self.private_key = public_key, private_key
        self._token = None

    def is_configured(self):
        return bool(self.public_key and self.private_key)

    def _auth(self):
        if self._token:
            return self._token
        try:
            resp = requests.post(f"{self.base}/token", timeout=20, data={
                "grant_type": "client_credentials", "client_id": self.public_key,
                "client_secret": self.private_key})
        except requests.RequestException as e:
            raise PricingError(f"TCGplayer unreachable: {e}") from e
        if resp.status_code >= 400:
            raise PricingError(f"TCGplayer token {resp.status_code}: check the keys in Settings")
        self._token = resp.json().get("access_token")
        return self._token

    def fetch(self, product_id):
        token = self._auth()
        try:
            resp = requests.get(f"{self.base}/pricing/product/{product_id}", timeout=20,
                                headers={"Authorization": f"bearer {token}", "Accept": "application/json"})
        except requests.RequestException as e:
            raise PricingError(f"TCGplayer unreachable: {e}") from e
        if resp.status_code >= 400:
            raise PricingError(f"TCGplayer {resp.status_code}")
        results = (resp.json() or {}).get("results") or []
        pick = next((r for r in results if r.get("subTypeName") == "Normal" and r.get("marketPrice")), None) \
            or next((r for r in results if r.get("marketPrice")), None)
        if not pick:
            raise PricingError("No market price listed yet.")
        return {"market": _money(pick["marketPrice"]), "low": _money(pick.get("lowPrice") or pick["marketPrice"])}


def _money(v):
    return Decimal(str(v)).quantize(CENTS, ROUND_HALF_UP)


def get_provider():
    from core.models import CompanySettings
    cs = CompanySettings.get()
    pub = cs.tcgplayer_public_key or getattr(dj_settings, "TCGPLAYER_PUBLIC_KEY", "")
    priv = cs.tcgplayer_private_key or getattr(dj_settings, "TCGPLAYER_PRIVATE_KEY", "")
    if pub and priv:
        return TcgplayerApiProvider(pub, priv)
    return TcgplayerSiteProvider()


def refresh_product(product, provider=None):
    """Fetch and store the market price for one product. Returns (ok, message)."""
    if not product.tcgplayer_id:
        return False, "No TCGplayer id."
    provider = provider or get_provider()
    try:
        info = provider.fetch(product.tcgplayer_id)
    except PricingError as e:
        product.market_error = str(e)[:200]
        product.market_updated_at = timezone.now()
        product.save(update_fields=["market_error", "market_updated_at"])
        return False, str(e)
    product.market_price, product.market_low = info["market"], info["low"]
    product.market_updated_at = timezone.now()
    product.market_error = ""
    product.save(update_fields=["market_price", "market_low", "market_updated_at", "market_error"])
    pct = product.cost_pct_of_market
    msg = f"Market ${info['market']}" + (f" · your cost is {pct}% of market" if pct is not None else "")
    if product.tcgplayer_unit_id:
        ok_u, msg_u = refresh_unit_price(product, provider)
        msg += f" · {msg_u}" if ok_u else f" · single unit: {msg_u}"
    return True, msg


def refresh_unit_price(product, provider=None):
    """Fetch the market price of the single unit inside a case SKU."""
    if not product.tcgplayer_unit_id:
        return False, "No single-unit TCGplayer id."
    provider = provider or get_provider()
    try:
        info = provider.fetch(product.tcgplayer_unit_id)
    except PricingError as e:
        product.unit_market_error = str(e)[:200]
        product.unit_market_updated_at = timezone.now()
        product.save(update_fields=["unit_market_error", "unit_market_updated_at"])
        return False, str(e)
    product.unit_market_price, product.unit_market_low = info["market"], info["low"]
    product.unit_market_updated_at = timezone.now()
    product.unit_market_error = ""
    product.save(update_fields=["unit_market_price", "unit_market_low", "unit_market_updated_at", "unit_market_error"])
    pct = product.cost_pct_of_unit_market
    case_val = product.case_market_from_units
    return True, (f"single ${info['market']} × {product.case_pack_qty} = ${case_val}"
                  + (f" · your cost is {pct}% of singles" if pct is not None else ""))


def eligible_products():
    from inventory.models import Product
    from django.db.models import Q
    return (Product.objects.filter(active=True, category__name__iexact=eligible_category_name())
            .filter(Q(tcgplayer_id__isnull=False) | Q(tcgplayer_unit_id__isnull=False))
            .select_related("category"))


def refresh_all(provider=None):
    provider = provider or get_provider()
    ok = fail = 0
    for p in eligible_products():
        if p.tcgplayer_id:
            good, _ = refresh_product(p, provider)        # also refreshes the single unit
        else:
            good, _ = refresh_unit_price(p, provider)
        ok += good
        fail += (not good)
    return f"Market prices: {ok} updated, {fail} failed."
