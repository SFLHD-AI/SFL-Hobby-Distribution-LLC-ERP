"""
Tradepost Partner Exchange (TPX) — pull the fills you BOUGHT and turn them
into supplier orders (RCVs).

API facts (https://tpx.tradepost.co/v2/integration/overview):
  * one GraphQL endpoint, POST https://platform-api.tradepost.co/graphql
  * auth header  X-API-Key: tp_live_…   (Terminal → Settings → API Keys)
  * money is integer cents; IDs are UUIDs; timestamps ISO-8601 UTC
  * listOrderExecutions pages with a keyset cursor (pageInfo.endCursor →
    pagination.after); "side: BUY" limits it to our purchases
  * includeProductData folds in assetConfiguration / asset / key;
    includePnl gives the buyer's fees-in unit price

Everything network-related lives in TradepostClient so tests can swap it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import requests
from django.utils import timezone

log = logging.getLogger(__name__)

ENDPOINT = "https://platform-api.tradepost.co/graphql"
CENTS = Decimal("0.01")

# Fields we always ask for. The optional trio (externalId slug data — all
# JSON scalars; externalId carries the TCGplayer ID) was verified against the
# live schema; if Tradepost ever drops them the query is retried without.
_OPTIONAL_CFG_FIELDS = "externalId slug data"
_FILL_FIELDS = """
  id orderNumber takerSide quantity executionPriceCents grossAmountCents
  status executedAt settledAt assetConfigurationId
  assetConfiguration { id name %s }
  asset { name }
  key { name }
  pnl { boughtQuantity buyUnitPriceCents buyFeesCents }
  transfers { id status shipmentGroup { id shippingLabels { id trackingCode carrier currentStatus estimatedDeliveryDate updatedAt } } }
"""

_LIST_QUERY = """
query Buys($input: ListOrderExecutionsInput!) {
  listOrderExecutions(input: $input) {
    orderExecutions { %s }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class TradepostError(Exception):
    pass


@dataclass
class Fill:
    id: str
    order_number: str
    status: str
    executed_at: datetime | None
    quantity: int
    unit_cost: Decimal            # fees-in per unit when Tradepost reports it
    price_cents: int              # raw cross price per unit
    fees_cents: int
    config_id: str
    product_name: str
    set_name: str = ""
    condition: str = ""
    tcgplayer_id: int | None = None
    labels: list = field(default_factory=list)   # [{"tracking", "carrier", "tp_status", "tp_eta"}]
    raw: dict = field(default_factory=dict)

    @property
    def line_total(self):
        return (self.unit_cost * self.quantity).quantize(CENTS)

    @property
    def is_final(self):
        return self.status in ("SETTLED", "SETTLING", "PENDING", "IN_DISPUTE")


class TradepostClient:
    def __init__(self, api_key, endpoint=ENDPOINT, timeout=30):
        self.api_key = (api_key or "").strip()
        self.endpoint = endpoint
        self.timeout = timeout
        self._optional_supported = True

    def is_configured(self):
        return bool(self.api_key)

    # --- transport -------------------------------------------------------
    def _post(self, query, variables):
        try:
            resp = requests.post(self.endpoint, json={"query": query, "variables": variables},
                                 headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
                                 timeout=self.timeout)
        except requests.RequestException as e:
            raise TradepostError(f"Could not reach Tradepost: {e}") from e
        if resp.status_code >= 500:
            raise TradepostError(f"Tradepost returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise TradepostError("Tradepost returned something that is not JSON")
        return body

    def _query(self, query, variables):
        body = self._post(query, variables)
        if isinstance(body.get("error"), str):          # REST-style refusal, e.g. HTTP 401
            code = body.get("code") or ""
            if "API_KEY" in code or "key" in body["error"].lower():
                raise TradepostError("Tradepost rejected the API key — check Settings → Tradepost API key.")
            raise TradepostError(f"Tradepost: {body['error']} ({code})" if code else f"Tradepost: {body['error']}")
        errors = body.get("errors") or []
        if errors:
            msg = "; ".join(f"{e.get('message', '?')} {e.get('code', '')}".strip() for e in errors)
            if any("Authentication" in (e.get("message") or "") for e in errors):
                raise TradepostError("Tradepost rejected the API key — check Settings → Tradepost API key.")
            raise TradepostError(f"Tradepost error: {msg}")
        data = body.get("data") or {}
        payload = next(iter(data.values()), None) if data else None
        if payload is None:
            raise TradepostError("Tradepost returned no data")
        err = payload.get("error") if isinstance(payload, dict) else None
        if err:
            raise TradepostError(f"Tradepost: {err.get('message')} ({err.get('code')})")
        return payload

    # --- public ------------------------------------------------------------
    def list_buys(self, since=None, status=None, max_pages=20, page_size=100):
        """All fills where we were the buyer, newest first."""
        since = since or (timezone.now() - timedelta(days=120))
        fills, after = [], None
        for _ in range(max_pages):
            inp = {"side": "BUY", "executedAfter": since.isoformat().replace("+00:00", "Z"),
                   "includeProductData": True, "includePnl": True, "includeTransfers": True,
                   "pagination": {"first": page_size}}
            if status:
                inp["status"] = status
            if after:
                inp["pagination"]["after"] = after
            payload = self._list(inp)
            for row in payload.get("orderExecutions") or []:
                fills.append(self.parse_fill(row))
            info = payload.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        return fills

    def _list(self, inp):
        extra = _OPTIONAL_CFG_FIELDS if self._optional_supported else ""
        try:
            return self._query(_LIST_QUERY % (_FILL_FIELDS % extra), {"input": inp})
        except TradepostError as e:
            # schema change / invalid document → retry once with the lean selection
            if extra and ("INVALID_REQUEST" in str(e) or "Cannot query field" in str(e)):
                self._optional_supported = False
                return self._query(_LIST_QUERY % (_FILL_FIELDS % ""), {"input": inp})
            raise

    @staticmethod
    def parse_fill(row):
        cfg = row.get("assetConfiguration") or {}
        pnl = row.get("pnl") or {}
        qty = int(row.get("quantity") or 0)
        price_cents = int(row.get("executionPriceCents") or 0)
        fees_cents = int(pnl.get("buyFeesCents") or 0) if pnl.get("buyFeesCents") not in (None, "") else 0
        unit_cents = pnl.get("buyUnitPriceCents")
        unit_cents = int(unit_cents) if unit_cents not in (None, "") else price_cents
        tcg = _tcgplayer_id(cfg.get("externalId"), cfg.get("externalIds"), cfg.get("data"))
        executed = None
        if row.get("executedAt"):
            try:
                executed = datetime.fromisoformat(row["executedAt"].replace("Z", "+00:00"))
            except ValueError:
                executed = None
        labels, seen = [], set()
        for tr in row.get("transfers") or []:
            grp = tr.get("shipmentGroup") or {}
            for lb in grp.get("shippingLabels") or []:
                code = (lb.get("trackingCode") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                eta = lb.get("estimatedDeliveryDate") or ""
                labels.append({"tracking": code, "carrier": normalize_carrier(lb.get("carrier"), code),
                               "tp_status": lb.get("currentStatus") or "", "tp_eta": eta[:10] if eta else ""})
        return Fill(
            labels=labels,
            id=str(row.get("id") or ""), order_number=row.get("orderNumber") or "",
            status=row.get("status") or "", executed_at=executed, quantity=qty,
            unit_cost=(Decimal(unit_cents) / 100).quantize(Decimal("0.0001")),
            price_cents=price_cents, fees_cents=fees_cents,
            config_id=str(cfg.get("id") or row.get("assetConfigurationId") or ""),
            product_name=cfg.get("name") or "", set_name=(row.get("asset") or {}).get("name") or "",
            condition=(row.get("key") or {}).get("name") or "", tcgplayer_id=tcg, raw=row,
        )


def normalize_carrier(tp_carrier, tracking=""):
    """Tradepost's carrier token (UPSDAP, USPS, FEDEX, …) → our carrier key."""
    from inventory.tracking import guess_carrier
    c = (tp_carrier or "").strip().upper()
    if c.startswith("UPS"):
        return "ups"
    if c.startswith("USPS"):
        return "usps"
    if c.startswith("FEDEX"):
        return "fedex"
    if c.startswith("DHL"):
        return "dhl_express"
    if c.startswith("ONTRAC"):
        return "ontrac"
    if c.startswith("LASERSHIP"):
        return "lasership"
    if c.startswith("AMAZON"):
        return "amazon"
    return guess_carrier(tracking) or ""


def _tcgplayer_id(*sources):
    """TCGplayer product ID from externalId / externalIds / data, in any shape
    Tradepost uses: {"TCGPLAYER": "285229"}, {"TCG_PLAYER_ID": "..."},
    a JSON string of either, or a bare numeric string."""
    import json
    keys = ("TCGPLAYER", "TCG_PLAYER_ID", "TCGPLAYER_ID", "tcgplayer", "tcgplayerId", "tcg_player_id")
    for src in sources:
        if src in (None, "", {}, []):
            continue
        if isinstance(src, str):
            txt = src.strip()
            if txt.isdigit():
                return int(txt)
            try:
                src = json.loads(txt)
            except ValueError:
                continue
        if isinstance(src, dict):
            for k in keys:
                v = src.get(k)
                if isinstance(v, list) and v:
                    v = v[0]
                if v not in (None, "") and str(v).strip().isdigit():
                    return int(str(v).strip())
    return None


# ---------------------------------------------------------------------------
# Matching fills to products and building the RCV
# ---------------------------------------------------------------------------
def match_product(fill, Product):
    """Existing product for a fill, by Tradepost config id, TCGplayer id, then exact name."""
    if fill.config_id:
        p = Product.objects.filter(tradepost_config_id=fill.config_id).first()
        if p:
            return p
    if fill.tcgplayer_id:
        p = Product.objects.filter(tcgplayer_id=fill.tcgplayer_id).first()
        if p:
            return p
    if fill.product_name:
        p = Product.objects.filter(name__iexact=fill.product_name).first()
        if p:
            return p
        full = f"{fill.set_name} {fill.product_name}".strip()
        p = Product.objects.filter(name__iexact=full).first() if fill.set_name else None
        if p:
            return p
    return None


def product_for_fill(fill, Product, create=True):
    """Match, or create a new product named after the Tradepost listing."""
    p = match_product(fill, Product)
    if p:
        changed = False
        if fill.config_id and not p.tradepost_config_id:
            p.tradepost_config_id = fill.config_id
            changed = True
        if fill.tcgplayer_id and not p.tcgplayer_id:
            p.tcgplayer_id = fill.tcgplayer_id
            changed = True
        if changed:
            p.save(update_fields=["tradepost_config_id", "tcgplayer_id"])
        return p, False
    if not create:
        return None, False
    name = fill.product_name or f"Tradepost {fill.order_number}"
    if fill.set_name and fill.set_name.lower() not in name.lower():
        name = f"{fill.set_name} — {name}"
    sku = f"TP-{fill.tcgplayer_id}" if fill.tcgplayer_id else f"TP-{fill.config_id[:8].upper()}"
    base, n = sku, 1
    while Product.all_objects.filter(sku=sku).exists():
        n += 1
        sku = f"{base}-{n}"
    p = Product.objects.create(
        sku=sku, name=name[:250], tcgplayer_id=fill.tcgplayer_id,
        tradepost_config_id=fill.config_id,
        default_price=(fill.unit_cost * Decimal("1.25")).quantize(CENTS))
    return p, True


def sync_fills(client, since=None):
    """Pull fills from Tradepost into TradepostFill rows (upsert). Returns (new, updated)."""
    from inventory.models import TradepostFill
    new = updated = 0
    for f in client.list_buys(since=since):
        if not f.id:
            continue
        # keep our own Shippo results for packages we already know
        existing = TradepostFill.objects.filter(execution_id=f.id).first()
        known = {p.get("tracking"): p for p in (existing.packages if existing else [])}
        packages = []
        for lb in f.labels:
            row = dict(known.get(lb["tracking"]) or {})
            row.update(lb)
            packages.append(row)
        fields = dict(order_number=f.order_number, status=f.status, executed_at=f.executed_at,
                      quantity=f.quantity, unit_cost=f.unit_cost, price_cents=f.price_cents,
                      fees_cents=f.fees_cents, config_id=f.config_id, product_name=f.product_name[:250],
                      set_name=f.set_name[:250], condition=f.condition[:40], tcgplayer_id=f.tcgplayer_id,
                      packages=packages)
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.save()
            updated += 1
        else:
            TradepostFill.objects.create(execution_id=f.id, **fields)
            new += 1
    return new, updated


def link_logged_fills(Receipt, ReceiptLine):
    """Attach unlinked fills to supplier orders you already logged by hand:
    a receipt whose Supplier invoice # or Reference mentions the Tradepost
    order number, or a line that was imported from that fill. Returns count."""
    from django.db.models import Q
    from inventory.models import TradepostFill
    linked = 0
    for fill in TradepostFill.objects.filter(receipt__isnull=True, closed=False):
        receipt = None
        line = ReceiptLine.objects.filter(tradepost_execution_id=fill.execution_id,
                                          receipt__is_deleted=False).select_related("receipt").first()
        if line:
            receipt = line.receipt
        elif fill.order_number:
            receipt = (Receipt.objects.filter(is_deleted=False)
                       .filter(Q(supplier_invoice_number__icontains=fill.order_number)
                               | Q(reference__icontains=fill.order_number)
                               | Q(notes__icontains=fill.order_number))
                       .order_by("-id").first())
        if receipt:
            fill.receipt = receipt
            fill.save(update_fields=["receipt"])
            mirror_packages(fill)
            linked += 1
    return linked


def mirror_packages(fill):
    """Make sure the linked supplier order has a ReceiptPackage for every
    Tradepost tracking number on this fill (so the Receiving page and the
    hourly tracker see them too)."""
    from inventory.models import ReceiptPackage
    if not fill.receipt_id:
        return
    for p in fill.packages or []:
        pkg, created = ReceiptPackage.objects.get_or_create(
            receipt_id=fill.receipt_id, tracking_number=p["tracking"],
            defaults={"carrier": p.get("carrier") or ""})
        if created and p.get("status"):
            _apply_tracking(pkg, p)


def _apply_tracking(pkg, p):
    from datetime import date
    pkg.status = p.get("status") or ""
    pkg.detail = (p.get("detail") or "")[:300]
    pkg.location = (p.get("location") or "")[:150]
    pkg.eta = date.fromisoformat(p["eta"]) if p.get("eta") else None
    pkg.checked_at = timezone.now()
    pkg.save()


def refresh_fill_tracking(fill, provider=None, force=False):
    """Ask Shippo about every package on the fill (skipping ones already
    delivered). Updates the JSON and, when the fill is logged, the receipt's
    packages and expected date. Returns number of packages refreshed."""
    from inventory.tracking import SHIPPO_CARRIERS, fetch_status, sync_expected_date
    from shipping.providers import ProviderError, ProviderNotConfigured, get_provider
    if not fill.packages:
        return 0
    provider = provider or get_provider("shippo")
    if not provider.is_configured():
        return 0
    n = 0
    for p in fill.packages:
        if p.get("status") == "DELIVERED" and not force:
            continue
        carrier = p.get("carrier") or normalize_carrier("", p["tracking"])
        if carrier not in SHIPPO_CARRIERS:
            continue
        try:
            info = fetch_status(carrier, p["tracking"], provider)
        except (ProviderNotConfigured, ProviderError) as e:
            p["error"] = str(e)[:200]
            p["checked_at"] = timezone.now().isoformat()
            continue
        p.update({"carrier": carrier, "status": info["status"], "detail": info["detail"],
                  "location": info["location"], "eta": info["eta"].isoformat() if info["eta"] else "",
                  "checked_at": timezone.now().isoformat(), "error": ""})
        n += 1
    fill.save(update_fields=["packages"])
    if fill.receipt_id:
        mirror_packages(fill)
        by_code = {p["tracking"]: p for p in fill.packages}
        for pkg in fill.receipt.packages.all():
            if pkg.tracking_number in by_code and by_code[pkg.tracking_number].get("status"):
                _apply_tracking(pkg, by_code[pkg.tracking_number])
        sync_expected_date(fill.receipt)
    return n


def build_receipt(fills, user, Receipt, ReceiptLine, Product, Supplier, reference=""):
    """One RCV (supplier Tradepost) with one line per TradepostFill; links the
    fills and copies their tracking numbers. Returns (receipt, new_products)."""
    from django.db import transaction
    supplier, _ = Supplier.objects.get_or_create(name="Tradepost")
    orders = sorted({f.order_number for f in fills if f.order_number})
    joined = ", ".join(orders)
    dates = [f.executed_at for f in fills if f.executed_at]
    with transaction.atomic():
        receipt = Receipt.objects.create(
            supplier=supplier, reference=(reference or joined or "Tradepost")[:100],
            supplier_invoice_number=joined[:100], created_by=user,
            date=timezone.localtime(min(dates)).date() if dates else timezone.localdate(),
            notes="Imported from Tradepost: " + joined)
        new_products = 0
        for f in fills:
            product, created = product_for_fill(f, Product)
            new_products += created
            ReceiptLine.objects.create(receipt=receipt, product=product, qty=f.quantity,
                                       unit_cost=f.unit_cost, tradepost_execution_id=f.execution_id,
                                       tradepost_order_number=f.order_number)
            f.receipt = receipt
            f.save(update_fields=["receipt"])
            mirror_packages(f)
        from inventory.tracking import sync_expected_date
        sync_expected_date(receipt)
    return receipt, new_products
