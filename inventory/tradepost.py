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
                   "includeProductData": True, "includePnl": True,
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
        return Fill(
            id=str(row.get("id") or ""), order_number=row.get("orderNumber") or "",
            status=row.get("status") or "", executed_at=executed, quantity=qty,
            unit_cost=(Decimal(unit_cents) / 100).quantize(Decimal("0.0001")),
            price_cents=price_cents, fees_cents=fees_cents,
            config_id=str(cfg.get("id") or row.get("assetConfigurationId") or ""),
            product_name=cfg.get("name") or "", set_name=(row.get("asset") or {}).get("name") or "",
            condition=(row.get("key") or {}).get("name") or "", tcgplayer_id=tcg, raw=row,
        )


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


def already_imported(fill_ids, ReceiptLine):
    return set(ReceiptLine.all_objects.filter(tradepost_execution_id__in=list(fill_ids))
               .exclude(receipt__is_deleted=True)
               .values_list("tradepost_execution_id", flat=True))


def build_receipt(fills, user, Receipt, ReceiptLine, Product, Supplier, reference=""):
    """One RCV (supplier Tradepost) with one line per fill. Returns (receipt, new_products)."""
    from django.db import transaction
    supplier, _ = Supplier.objects.get_or_create(name="Tradepost")
    orders = sorted({f.order_number for f in fills if f.order_number})
    ref = reference or (", ".join(orders)[:100] if orders else "Tradepost")
    dates = [f.executed_at for f in fills if f.executed_at]
    with transaction.atomic():
        receipt = Receipt.objects.create(
            supplier=supplier, reference=ref, created_by=user,
            date=timezone.localtime(min(dates)).date() if dates else timezone.localdate(),
            notes="Imported from Tradepost: " + ", ".join(orders))
        new_products = 0
        for f in fills:
            product, created = product_for_fill(f, Product)
            new_products += created
            ReceiptLine.objects.create(receipt=receipt, product=product, qty=f.quantity,
                                       unit_cost=f.unit_cost, tradepost_execution_id=f.id,
                                       tradepost_order_number=f.order_number)
    return receipt, new_products
