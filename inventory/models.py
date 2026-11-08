"""
Inventory: products, price lists, receiving with landed cost, the stock
movement ledger (single source of truth for quantities), adjustments and
cycle counts.

Quantity model
--------------
* ``Product.qty_on_hand`` is a cached integer maintained in the same DB
  transaction as every ``StockMovement`` row. The ledger is authoritative:
  the cache can always be rebuilt from movements.
* Committed = sum of unfulfilled quantities on open (Confirmed/Picking/
  Picked) customer POs. Available = on hand − committed.
* Weighted average cost is recalculated on every receipt and snapshotted
  onto each movement so point-in-time valuation is exact.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import SoftDeleteModel, log_activity

FOURPLACES = Decimal("0.0001")
CENTS = Decimal("0.01")


class Category(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Brand(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Supplier(SoftDeleteModel):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(SoftDeleteModel):
    UOM_CHOICES = [("each", "Each"), ("box", "Box"), ("case", "Case")]

    sku = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="products")
    brand = models.ForeignKey(Brand, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="products")
    upc = models.CharField("UPC / barcode", max_length=64, blank=True, db_index=True,
                           help_text="Scannable barcode; may differ from SKU")
    case_pack_qty = models.PositiveIntegerField(default=1)
    unit_of_measure = models.CharField(max_length=10, choices=UOM_CHOICES, default="each")
    weight_lbs = models.DecimalField("Unit weight (lb)", max_digits=8, decimal_places=3,
                                     null=True, blank=True,
                                     help_text="Weight per unit, for shipping estimates")

    # Costs & prices
    avg_cost = models.DecimalField("Weighted average landed cost", max_digits=12,
                                   decimal_places=4, default=0)
    default_price = models.DecimalField("Default wholesale price", max_digits=12,
                                        decimal_places=2, default=0)
    msrp = models.DecimalField("MSRP", max_digits=12, decimal_places=2, null=True, blank=True)

    # TCGplayer market price (sealed TCG product only; refreshed hourly)
    tcgplayer_id = models.PositiveIntegerField("TCGplayer product ID", null=True, blank=True, db_index=True,
                                               help_text="From the product's tcgplayer.com URL: /product/<id>/…")
    market_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                       help_text="TCGplayer market price, per unit")
    market_low = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    market_updated_at = models.DateTimeField(null=True, blank=True)
    market_error = models.CharField(max_length=200, blank=True)
    # The single box / pack inside a case: a second TCGplayer link so a case
    # SKU can be compared against (single market price × case pack qty).
    tcgplayer_unit_id = models.PositiveIntegerField(
        "TCGplayer product ID of the single unit", null=True, blank=True, db_index=True,
        help_text="For case SKUs: the TCGplayer page of ONE box/pack inside the case")
    unit_market_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    unit_market_low = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    unit_market_updated_at = models.DateTimeField(null=True, blank=True)
    unit_market_error = models.CharField(max_length=200, blank=True)
    tradepost_config_id = models.CharField(
        "Tradepost product ID", max_length=40, blank=True, db_index=True,
        help_text="Tradepost asset-configuration UUID; set automatically when a Tradepost fill is imported")

    reorder_point = models.IntegerField(default=0)
    low_stock_alert_muted = models.BooleanField(
        default=False,
        help_text="Muted items stay low-stock in filters but are hidden from "
                  "the dashboard widget and email digest")
    bin_location = models.CharField(max_length=50, blank=True, db_index=True,
                                    help_text="Bin/shelf, e.g. A-03-2")
    image = models.ImageField(upload_to="products/", blank=True, null=True)
    active = models.BooleanField(default=True)

    # Cached quantity — ledger (StockMovement) is the source of truth
    qty_on_hand = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} — {self.name}"

    # --- quantities -------------------------------------------------------
    @property
    def qty_committed(self):
        """Units on open customer POs not yet finalized."""
        from orders.models import POLine, PurchaseOrder
        agg = (POLine.objects
               .filter(product=self, is_deleted=False,
                       po__is_deleted=False,
                       po__status__in=PurchaseOrder.COMMITTED_STATUSES)
               .aggregate(total=models.Sum("qty_ordered")))
        return agg["total"] or 0

    @property
    def qty_available(self):
        return self.qty_on_hand - self.qty_committed

    @property
    def is_low_stock(self):
        return self.qty_available <= self.reorder_point

    @property
    def inventory_value(self):
        return (Decimal(self.qty_on_hand) * self.avg_cost).quantize(CENTS, ROUND_HALF_UP)

    # --- incoming (open supplier orders) ------------------------------------
    def incoming_lines(self):
        """Receipt lines on supplier orders not yet received, newest ETA first."""
        return (self.receipt_lines.filter(is_deleted=False, receipt__is_deleted=False,
                                          receipt__posted=False)
                .select_related("receipt", "receipt__supplier")
                .order_by("receipt__expected_date", "receipt__date"))

    @property
    def incoming_summary(self):
        """{'qty', 'cost', 'avg_cost', 'orders', 'next_eta'} across open supplier orders.
        Cost = merchandise cost (freight/duties are allocated when the order posts)."""
        qty = 0
        cost = Decimal("0.00")
        orders = set()
        next_eta = None
        for line in self.incoming_lines():
            qty += line.qty
            cost += Decimal(line.qty) * line.unit_cost
            orders.add(line.receipt_id)
            eta = line.receipt.tracking_eta or line.receipt.expected_date
            if eta and (next_eta is None or eta < next_eta):
                next_eta = eta
        cost = cost.quantize(CENTS, ROUND_HALF_UP)
        return {"qty": qty, "cost": cost,
                "avg_cost": (cost / qty).quantize(FOURPLACES, ROUND_HALF_UP) if qty else None,
                "orders": len(orders), "next_eta": next_eta}

    @property
    def qty_incoming(self):
        return self.incoming_summary["qty"]

    def barcode_value(self):
        return self.upc or self.sku

    # --- market price (TCGplayer) ---------------------------------------------
    @property
    def market_eligible(self):
        """Only products in the configured 'Sealed TCG Product' category are priced."""
        from inventory.pricing import eligible_category_name
        return bool(self.category_id and self.category.name.strip().lower() == eligible_category_name().lower())

    @property
    def cost_pct_of_market(self):
        """avg landed cost as a percent of TCGplayer market price (None when unknown)."""
        if not self.market_price or self.market_price <= 0 or not self.avg_cost:
            return None
        return (self.avg_cost / self.market_price * Decimal("100")).quantize(Decimal("0.1"), ROUND_HALF_UP)

    @property
    def market_margin_per_unit(self):
        if not self.market_price:
            return None
        return (self.market_price - self.avg_cost).quantize(CENTS, ROUND_HALF_UP)

    # --- case vs. the singles inside it -------------------------------------
    @property
    def case_market_from_units(self):
        """What the case is worth as singles: single-unit market × case pack qty."""
        if not self.unit_market_price or self.unit_market_price <= 0 or not self.case_pack_qty:
            return None
        return (self.unit_market_price * self.case_pack_qty).quantize(CENTS, ROUND_HALF_UP)

    @property
    def cost_pct_of_unit_market(self):
        """avg landed cost as a percent of (single market × case qty)."""
        case_val = self.case_market_from_units
        if not case_val or not self.avg_cost:
            return None
        return (self.avg_cost / case_val * Decimal("100")).quantize(Decimal("0.1"), ROUND_HALF_UP)

    @property
    def unit_market_margin(self):
        case_val = self.case_market_from_units
        if case_val is None:
            return None
        return (case_val - self.avg_cost).quantize(CENTS, ROUND_HALF_UP)

    @property
    def unit_market_badge(self):
        return self._pct_badge(self.cost_pct_of_unit_market, "of singles")

    @staticmethod
    def _pct_badge(pct, suffix="of market"):
        if pct is None:
            return None
        if pct < 70:
            return (f"{pct}% {suffix}", "bg-emerald-100 text-emerald-700")
        if pct < 90:
            return (f"{pct}% {suffix}", "bg-sky-100 text-sky-700")
        if pct < 100:
            return (f"{pct}% {suffix}", "bg-amber-100 text-amber-800")
        return (f"{pct}% {suffix}", "bg-red-100 text-red-700")

    @property
    def market_badge(self):
        """(label, css) from the cost-vs-market ratio."""
        pct = self.cost_pct_of_market
        if pct is None:
            return None
        if pct < 70:
            return (f"{pct}% of market", "bg-emerald-100 text-emerald-700")
        if pct < 90:
            return (f"{pct}% of market", "bg-sky-100 text-sky-700")
        if pct < 100:
            return (f"{pct}% of market", "bg-amber-100 text-amber-800")
        return (f"{pct}% of market", "bg-red-100 text-red-700")

    # --- barcodes ------------------------------------------------------------
    @property
    def all_codes(self):
        """Every code that identifies this product: SKU, primary UPC and extra barcodes."""
        codes = [self.sku]
        if self.upc:
            codes.append(self.upc)
        codes += [b.code for b in self.barcodes.all()]
        return codes

    @property
    def extra_codes_text(self):
        return "\n".join(b.code for b in self.barcodes.all())

    def matches_code(self, code):
        code = (code or "").strip()
        return bool(code) and any(code.lower() == c.lower() for c in self.all_codes)

    @classmethod
    def find_by_code(cls, code, active_only=False):
        """Product for a scanned/typed code: primary UPC, extra barcode, or SKU."""
        code = (code or "").strip()
        if not code:
            return None
        qs = cls.objects.filter(active=True) if active_only else cls.objects.all()
        return (qs.filter(upc=code).first()
                or qs.filter(barcodes__code=code).first()
                or qs.filter(sku__iexact=code).first())

    @staticmethod
    def code_filter(q):
        """Q object matching SKU, name, primary UPC or any extra barcode."""
        return (models.Q(sku__icontains=q) | models.Q(name__icontains=q)
                | models.Q(upc__icontains=q) | models.Q(barcodes__code__icontains=q))

    def revalue_cost(self, new_cost, user=None, note=""):
        """
        Set the weighted average cost directly WITHOUT changing quantity.
        Recorded in the ledger as a zero-quantity 'Cost revaluation' row, so
        the change is fully auditable. Past sales keep their snapshots.
        """
        new_cost = Decimal(new_cost).quantize(FOURPLACES, ROUND_HALF_UP)
        old = self.avg_cost
        movement = self.apply_movement(
            movement_type="revalue", qty_delta=0, user=user,
            unit_cost=new_cost, new_avg_cost=new_cost,
            reference="REVALUE",
            note=(note or f"Average cost set: {old} -> {new_cost}")[:500])
        log_activity(user, "cost_revalued", self,
                     changes={"old": str(old), "new": str(new_cost)}, note=note)
        return movement

    # --- ledger operations --------------------------------------------------
    def apply_movement(self, *, movement_type, qty_delta, user=None, unit_cost=None,
                       reference="", note="", source=None, new_avg_cost=None):
        """
        The ONLY way stock changes. Creates a ledger row and updates the
        cached on-hand quantity atomically. Returns the StockMovement.
        """
        with transaction.atomic():
            locked = Product.all_objects.select_for_update().get(pk=self.pk)
            before = locked.qty_on_hand
            after = before + qty_delta
            if after < 0:
                raise ValidationError(
                    f"Movement would take {locked.sku} below zero "
                    f"({before} on hand, delta {qty_delta}).")
            avg = new_avg_cost if new_avg_cost is not None else locked.avg_cost
            movement = StockMovement.objects.create(
                product=locked,
                movement_type=movement_type,
                qty_delta=qty_delta,
                qty_before=before,
                qty_after=after,
                unit_cost=unit_cost if unit_cost is not None else locked.avg_cost,
                avg_cost_after=avg,
                user=user if getattr(user, "is_authenticated", False) else None,
                reference=reference,
                note=note,
                source_type=source.__class__.__name__ if source is not None else "",
                source_id=str(getattr(source, "pk", "")) if source is not None else "",
            )
            locked.qty_on_hand = after
            update = ["qty_on_hand", "updated_at"]
            if new_avg_cost is not None:
                locked.avg_cost = new_avg_cost
                update.append("avg_cost")
            locked.save(update_fields=update)
            # refresh self in place so callers see new values
            self.qty_on_hand = locked.qty_on_hand
            self.avg_cost = locked.avg_cost
            return movement


class ProductBarcode(models.Model):
    """An extra barcode that maps to a product. Retail packaging often changes
    UPC between print runs while the product (and its cost basis) stays the
    same, so one SKU can carry several codes."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="barcodes")
    code = models.CharField(max_length=64, unique=True, db_index=True)
    note = models.CharField(max_length=100, blank=True, help_text="e.g. 2nd print run, case UPC")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} → {self.product.sku}"


class StockMovement(models.Model):
    """Immutable audit ledger of every unit in or out."""

    TYPES = [
        ("receipt", "Supplier receipt"),
        ("sale", "Sale (PO finalized)"),
        ("adjustment", "Manual adjustment"),
        ("revalue", "Cost revaluation"),
        ("cycle_count", "Cycle count"),
        ("credit_restock", "Credit memo restock"),
        ("backorder_release", "Backorder release"),
        ("supplier_return", "Returned to supplier"),
        ("write_off", "Damaged / written off"),
    ]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=TYPES, db_index=True)
    qty_delta = models.IntegerField()
    qty_before = models.IntegerField()
    qty_after = models.IntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0,
                                    help_text="Cost per unit relevant to this movement")
    avg_cost_after = models.DecimalField(max_digits=12, decimal_places=4, default=0,
                                         help_text="Weighted average cost after this movement")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="stock_movements")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    reference = models.CharField(max_length=120, blank=True,
                                 help_text="Human-readable source, e.g. PO-0012, RCV-4")
    source_type = models.CharField(max_length=60, blank=True)
    source_id = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["product", "timestamp"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def __str__(self):
        sign = "+" if self.qty_delta >= 0 else ""
        return f"{self.product.sku} {sign}{self.qty_delta} ({self.movement_type}) -> {self.qty_after}"


# ---------------------------------------------------------------------------
# Price lists (per-SKU customer pricing)
# ---------------------------------------------------------------------------
class PriceList(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=250, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def price_for(self, product):
        item = self.items.filter(product=product, is_deleted=False).first()
        return item.price if item else None


class PriceListItem(SoftDeleteModel):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_list_items")
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["price_list", "product"],
            condition=models.Q(is_deleted=False),
            name="uniq_live_pricelist_product")]

    def __str__(self):
        return f"{self.price_list.name}: {self.product.sku} @ {self.price}"


# ---------------------------------------------------------------------------
# Receiving (supplier shipments) with landed cost
# ---------------------------------------------------------------------------
class Receipt(SoftDeleteModel):
    """An incoming supplier shipment. Posting updates stock + avg cost."""

    supplier = models.ForeignKey(Supplier, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="receipts")
    date = models.DateField(default=timezone.localdate,
                            help_text="Order date")
    expected_date = models.DateField(null=True, blank=True,
                                     help_text="Expected arrival")
    reference = models.CharField(max_length=100, blank=True,
                                 help_text="Supplier invoice / tracking ref")
    freight_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    duties_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False)
    posted_at = models.DateTimeField(null=True, blank=True)
    # Supplier bill tracking (feeds accounts payable on the accrual statements
    # and cost of goods on the cash statements once paid)
    supplier_invoice_number = models.CharField(max_length=100, blank=True,
                                               help_text="Supplier's invoice / bill #")
    due_date = models.DateField(null=True, blank=True, help_text="When the supplier bill is due")
    paid_date = models.DateField(null=True, blank=True, db_index=True,
                                 help_text="Set automatically when matched to a bank transaction")
    paid_from = models.ForeignKey("banking.BankAccount", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="supplier_bills",
                                  help_text="Account the supplier was paid from")
    credit_refunded_date = models.DateField(
        null=True, blank=True,
        help_text="When the supplier refunded credits granted after you had already paid")
    payment_reference = models.CharField(max_length=100, blank=True,
                                         help_text="Check # / wire confirmation")
    # Carriers offered for inbound packages (see ReceiptPackage)
    CARRIERS = [("", "—"), ("usps", "USPS"), ("ups", "UPS"), ("fedex", "FedEx"),
                ("dhl_express", "DHL Express"), ("ontrac", "OnTrac"), ("lasership", "LaserShip"),
                ("amazon", "Amazon Logistics"), ("other", "Other / freight")]
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="receipts_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"RCV-{self.pk} {self.supplier or ''} {self.date}"

    @property
    def merchandise_total(self):
        return sum((l.qty * l.unit_cost for l in self.lines.all()), Decimal("0"))

    @property
    def landed_total(self):
        return self.merchandise_total + self.freight_cost + self.duties_cost

    # --- after-the-fact credits: damaged boxes, returns -------------------------
    def _active_adjustments(self):
        return [a for a in self.adjustments.all() if not a.reversed]

    @property
    def credits_total(self):
        """Supplier credits granted on this order (damage allowances, returns)."""
        return sum((a.credit_amount for a in self._active_adjustments()), Decimal("0.00"))

    @property
    def credits_after_payment(self):
        """Credits recorded after the bill was paid by hand → supplier owes a refund."""
        if self.paid_date is None:
            return Decimal("0.00")
        return sum((a.credit_amount for a in self._active_adjustments() if a.date > self.paid_date),
                   Decimal("0.00"))

    @property
    def bill_total(self):
        """What the order really costs you: landed total less supplier credits."""
        return self.landed_total - self.credits_total

    @property
    def refund_due(self):
        """Money the supplier still owes you back (credits that came after you paid)."""
        if self.credit_refunded_date is not None:
            return Decimal("0.00")
        return max(Decimal("0.00"), -self.balance_due)

    @property
    def units_written_off(self):
        return sum(a.qty for a in self._active_adjustments() if a.kind == "damaged")

    @property
    def units_returned(self):
        return sum(a.qty for a in self._active_adjustments() if a.kind == "return")

    @property
    def total_qty(self):
        return sum(l.qty for l in self.lines.all())

    # --- inbound tracking (aggregate of packages) -----------------------------
    @property
    def tracking_numbers(self):
        return [p.tracking_number for p in self.packages.all()]

    @property
    def tracking_badge(self):
        """(label, css) summarising every package on the order, or None."""
        pkgs = list(self.packages.all())
        if not pkgs:
            return None
        statuses = {p.status for p in pkgs}
        n = len(pkgs)
        if statuses == {"DELIVERED"}:
            return ("Delivered" + (f" ({n} boxes)" if n > 1 else ""), "bg-emerald-100 text-emerald-700")
        delivered = sum(1 for p in pkgs if p.is_delivered)
        if delivered:
            return (f"{delivered} of {n} delivered", "bg-sky-100 text-sky-700")
        if "RETURNED" in statuses or "FAILURE" in statuses:
            return ("Problem", "bg-red-100 text-red-700")
        if "TRANSIT" in statuses:
            return ("In transit" + (f" ({n} boxes)" if n > 1 else ""), "bg-sky-100 text-sky-700")
        if "PRE_TRANSIT" in statuses:
            return ("Label created", "bg-slate-100 text-slate-600")
        if "UNKNOWN" in statuses:
            return ("No scans yet", "bg-slate-100 text-slate-500")
        return ("Tracking added", "bg-slate-100 text-slate-600")

    @property
    def tracking_eta(self):
        """When the whole order should be here: the latest ETA among boxes
        not yet delivered (or the latest delivery if all have arrived)."""
        pkgs = list(self.packages.all())
        pending = [p.eta for p in pkgs if not p.is_delivered and p.eta]
        if pending:
            return max(pending)
        done = [timezone.localtime(p.event_at).date() for p in pkgs if p.is_delivered and p.event_at]
        return max(done) if done else None

    @property
    def tracking_detail(self):
        pkgs = [p for p in self.packages.all() if p.detail]
        return "; ".join(f"{p.tracking_number[-6:]}: {p.detail}" for p in pkgs)[:300]

    # --- supplier bill state -------------------------------------------------
    @property
    def amount_paid(self):
        """Paid via matched bank transactions, else the whole bill if a paid
        date was entered by hand."""
        from banking.models import BankTransaction
        linked = self.supplier_payments.filter(is_deleted=False).exclude(
            status=BankTransaction.IGNORED)
        if linked.exists():
            return sum((abs(t.amount) for t in linked), Decimal("0.00"))
        if self.paid_date is not None:
            # paid by hand: you paid what the bill was on that day
            paid = self.landed_total - (self.credits_total - self.credits_after_payment)
            if self.credit_refunded_date is not None:
                paid -= self.credits_after_payment      # supplier sent the difference back
            return paid
        return Decimal("0.00")

    @property
    def balance_due(self):
        """Positive: you still owe. Negative: the supplier owes you (see refund_due)."""
        return self.bill_total - self.amount_paid

    @property
    def is_paid(self):
        return self.posted and self.balance_due <= 0

    @property
    def is_overdue(self):
        return (self.posted and not self.is_paid and self.due_date is not None
                and self.due_date < timezone.localdate())

    def post(self, user=None):
        """
        Allocate freight+duties across lines proportionally to merchandise
        value, compute landed unit cost, receive stock and roll the weighted
        average cost per product.
        """
        if self.posted:
            raise ValidationError("Receipt already posted.")
        lines = list(self.lines.select_related("product"))
        if not lines:
            raise ValidationError("Receipt has no lines.")
        merch_total = self.merchandise_total
        extra = self.freight_cost + self.duties_cost

        with transaction.atomic():
            for line in lines:
                line_merch = line.qty * line.unit_cost
                if merch_total > 0:
                    allocated = (extra * line_merch / merch_total)
                else:  # zero-cost receipt: allocate by quantity
                    total_qty = sum(l.qty for l in lines)
                    allocated = extra * line.qty / total_qty if total_qty else Decimal("0")
                landed_unit = ((line_merch + allocated) / line.qty).quantize(FOURPLACES, ROUND_HALF_UP)
                line.landed_unit_cost = landed_unit
                line.save(update_fields=["landed_unit_cost"])

                product = line.product
                # Weighted average cost roll-forward
                on_hand = Product.all_objects.get(pk=product.pk).qty_on_hand
                if on_hand <= 0:
                    new_avg = landed_unit
                else:
                    new_avg = (
                        (Decimal(on_hand) * product.avg_cost + Decimal(line.qty) * landed_unit)
                        / Decimal(on_hand + line.qty)
                    ).quantize(FOURPLACES, ROUND_HALF_UP)
                product.apply_movement(
                    movement_type="receipt", qty_delta=line.qty, user=user,
                    unit_cost=landed_unit, new_avg_cost=new_avg,
                    reference=f"RCV-{self.pk}", source=self,
                    note=f"Received from {self.supplier or 'supplier'}")
            self.posted = True
            self.posted_at = timezone.now()
            # the Tradepost order (if any) is done once the boxes are in
            self.tradepost_fills.update(closed=True)
            # It's here: the arrival date is today and tracking is settled,
            # whatever the carrier last said.
            self.expected_date = timezone.localdate()
            self.save(update_fields=["posted", "posted_at", "expected_date"])
            self.packages.exclude(status="DELIVERED").update(
                status="DELIVERED", detail="Marked received", event_at=self.posted_at,
                eta=self.expected_date, error="")
            log_activity(user, "post", self, note=f"Posted receipt, {len(lines)} lines")


class ReceiptPackage(models.Model):
    """One tracked parcel on a supplier order. An order can arrive in several
    boxes with their own tracking numbers; the order is 'here' when the last
    one lands."""

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="packages")
    carrier = models.CharField(max_length=20, choices=Receipt.CARRIERS, blank=True)
    tracking_number = models.CharField(max_length=100, db_index=True)
    status = models.CharField(max_length=20, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    location = models.CharField(max_length=150, blank=True)
    event_at = models.DateTimeField(null=True, blank=True)
    eta = models.DateField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    BADGES = {"DELIVERED": ("Delivered", "bg-emerald-100 text-emerald-700"),
              "TRANSIT": ("In transit", "bg-sky-100 text-sky-700"),
              "PRE_TRANSIT": ("Label created", "bg-slate-100 text-slate-600"),
              "RETURNED": ("Returned", "bg-red-100 text-red-700"),
              "FAILURE": ("Problem", "bg-red-100 text-red-700"),
              "UNKNOWN": ("No scans yet", "bg-slate-100 text-slate-500")}

    class Meta:
        ordering = ["eta", "id"]
        constraints = [models.UniqueConstraint(fields=["receipt", "tracking_number"],
                                               name="uniq_receipt_tracking_number")]

    def __str__(self):
        return f"{self.tracking_number} ({self.get_carrier_display() or 'carrier?'})"

    @property
    def tracking_url(self):
        from inventory.tracking import carrier_url
        return carrier_url(self.carrier, self.tracking_number)

    @property
    def badge(self):
        return self.BADGES.get(self.status, ("Tracking added", "bg-slate-100 text-slate-600"))

    @property
    def is_delivered(self):
        return self.status == "DELIVERED"


class ReceiptLine(SoftDeleteModel):
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="receipt_lines")
    qty = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4,
                                    help_text="Supplier unit cost before freight/duties")
    landed_unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    tradepost_execution_id = models.CharField(
        max_length=40, blank=True, default="", db_index=True,
        help_text="Tradepost fill (order execution) this line was imported from — stops double imports")
    tradepost_order_number = models.CharField(max_length=20, blank=True, default="")

    def __str__(self):
        return f"{self.product.sku} x{self.qty} @ {self.unit_cost}"

    @property
    def line_total(self):
        return (Decimal(self.qty) * self.unit_cost).quantize(CENTS, ROUND_HALF_UP)


class ReceiptAdjustment(models.Model):
    """Something that happened to a received order after the fact:

    * ``damaged`` — boxes arrived unsellable. They stay received (they were
      really delivered) but are written off: stock goes down at average cost
      and the value lands in *Inventory adjustments & shrinkage* as a loss,
      less whatever credit the supplier gave.
    * ``return`` — boxes sent back to the supplier. Stock goes down at the
      landed cost they came in at and the weighted average cost is rolled
      back accordingly; the credit reduces what you owe (or is a refund due).
    """

    KINDS = [("damaged", "Damaged — written off"), ("return", "Returned to supplier")]
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="adjustments")
    line = models.ForeignKey(ReceiptLine, on_delete=models.PROTECT, related_name="adjustments")
    kind = models.CharField(max_length=10, choices=KINDS)
    qty = models.PositiveIntegerField()
    unit_cost_removed = models.DecimalField(max_digits=12, decimal_places=4, default=0,
                                            help_text="Cost per unit taken out of inventory")
    credit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                        help_text="Total credit / discount the supplier gave for these units")
    date = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    movement = models.OneToOneField(StockMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="receipt_adjustment")
    reversed = models.BooleanField(default=False)
    reversal = models.OneToOneField(StockMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="receipt_adjustment_reversal")

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.qty} × {self.line.product.sku} on RCV-{self.receipt_id}"

    @property
    def value_removed(self):
        return (self.unit_cost_removed * self.qty).quantize(CENTS, ROUND_HALF_UP)

    @property
    def loss(self):
        """What the business lost: inventory value gone minus the supplier's credit."""
        return self.value_removed - self.credit_amount

    @classmethod
    def create_and_apply(cls, *, receipt, line, kind, qty, credit_amount=0, date=None, note="", user=None):
        if not receipt.posted:
            raise ValidationError("Mark the order received first — only received boxes can be damaged or returned.")
        if line.receipt_id != receipt.pk:
            raise ValidationError("That line is not on this order.")
        line = ReceiptLine.objects.get(pk=line.pk)      # fresh: landed cost is set at receive time
        qty = int(qty)
        if qty <= 0:
            raise ValidationError("Quantity must be at least 1.")
        already = sum(a.qty for a in line.adjustments.all() if not a.reversed)
        if already + qty > line.qty:
            raise ValidationError(f"Only {line.qty - already} of the {line.qty} received on that line are left to adjust.")
        credit_amount = Decimal(credit_amount or 0).quantize(CENTS, ROUND_HALF_UP)
        if credit_amount < 0:
            raise ValidationError("The supplier credit can't be negative.")
        product = line.product
        with transaction.atomic():
            on_hand = Product.all_objects.get(pk=product.pk).qty_on_hand
            if on_hand < qty:
                raise ValidationError(f"Only {on_hand} of {product.sku} on hand — some have already been sold.")
            if kind == "return":
                unit = line.landed_unit_cost or line.unit_cost
                remaining = on_hand - qty
                if remaining > 0:
                    new_avg = ((Decimal(on_hand) * product.avg_cost - Decimal(qty) * unit)
                               / Decimal(remaining)).quantize(FOURPLACES, ROUND_HALF_UP)
                    if new_avg < 0:
                        new_avg = Decimal("0.0000")
                else:
                    new_avg = product.avg_cost      # nothing left: keep the last known cost
                mtype, label = "supplier_return", "Returned to supplier"
            else:
                unit = product.avg_cost
                new_avg = None
                mtype, label = "write_off", "Damaged, written off"
            adj = cls.objects.create(receipt=receipt, line=line, kind=kind, qty=qty,
                                     unit_cost_removed=unit, credit_amount=credit_amount,
                                     date=date or timezone.localdate(), note=note[:300],
                                     created_by=user if getattr(user, "is_authenticated", False) else None)
            adj.movement = product.apply_movement(
                movement_type=mtype, qty_delta=-qty, user=user, unit_cost=unit, new_avg_cost=new_avg,
                reference=f"RCV-{receipt.pk}", source=adj,
                note=f"{label}: {note}"[:500] if note else label)
            adj.save(update_fields=["movement"])
            log_activity(user, "receipt_adjustment", receipt,
                         changes={"kind": kind, "qty": qty, "sku": product.sku,
                                  "credit": str(credit_amount)}, note=note)
        return adj

    def undo(self, user=None):
        """Put the units back exactly as they were taken out."""
        if self.reversed:
            return
        product = self.line.product
        with transaction.atomic():
            on_hand = Product.all_objects.get(pk=product.pk).qty_on_hand
            unit = self.unit_cost_removed
            if self.kind == "return":
                new_avg = ((Decimal(on_hand) * product.avg_cost + Decimal(self.qty) * unit)
                           / Decimal(on_hand + self.qty)).quantize(FOURPLACES, ROUND_HALF_UP)
            else:
                new_avg = None
            self.reversal = product.apply_movement(
                movement_type=self.movement.movement_type if self.movement else "adjustment",
                qty_delta=self.qty, user=user, unit_cost=unit, new_avg_cost=new_avg,
                reference=f"RCV-{self.receipt_id}", source=self, note=f"Undo {self.get_kind_display()}")
            self.reversed = True
            self.save(update_fields=["reversed", "reversal"])
            log_activity(user, "receipt_adjustment_undo", self.receipt,
                         changes={"kind": self.kind, "qty": self.qty, "sku": product.sku})


class TradepostFill(models.Model):
    """A purchase (order execution) pulled from Tradepost's API. Cached so the
    Tradepost screen loads instantly, remembers which supplier order each
    fill went to, which ones are closed, and the latest package tracking."""

    execution_id = models.CharField(max_length=40, unique=True)
    order_number = models.CharField(max_length=20, db_index=True)
    status = models.CharField(max_length=20, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    price_cents = models.IntegerField(default=0)
    fees_cents = models.IntegerField(default=0)
    config_id = models.CharField(max_length=40, blank=True, db_index=True)
    product_name = models.CharField(max_length=250, blank=True)
    set_name = models.CharField(max_length=250, blank=True)
    condition = models.CharField(max_length=40, blank=True)
    tcgplayer_id = models.PositiveIntegerField(null=True, blank=True)
    disputed = models.BooleanField(default=False,
                                   help_text="A dispute is open on this fill; unit_cost is the original price paid")
    dispute_note = models.CharField(max_length=200, blank=True)
    # [{"tracking": "1Z…", "carrier": "ups", "tp_status": "IN_TRANSIT", "tp_eta": "2026-11-09",
    #   "status": "TRANSIT", "detail": "…", "location": "…", "eta": "2026-11-09", "checked_at": "…"}]
    packages = models.JSONField(default=list, blank=True)
    receipt = models.ForeignKey(Receipt, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="tradepost_fills")
    matched_product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="tradepost_fills",
                                        help_text="Product you matched this fill to by hand")
    closed = models.BooleanField(default=False, db_index=True,
                                 help_text="Done with this order (received, or nothing left to do)")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-executed_at", "-id"]

    def __str__(self):
        return f"{self.order_number} {self.product_name} x{self.quantity}"

    @property
    def line_total(self):
        return (self.unit_cost * self.quantity).quantize(CENTS, ROUND_HALF_UP)

    @property
    def is_logged(self):
        return self.receipt_id is not None

    @property
    def product(self):
        if not hasattr(self, "_product"):
            from inventory.tradepost import match_product
            self._product = self.matched_product or match_product(self, Product)
        return self._product

    # --- tracking summary (mirrors Receipt.tracking_badge for the fill's own packages) ---
    @property
    def tracking_badge(self):
        pkgs = self.packages or []
        if not pkgs:
            return None
        statuses = {p.get("status") or _tp_status(p.get("tp_status")) for p in pkgs}
        n = len(pkgs)
        if statuses == {"DELIVERED"}:
            return ("Delivered" + (f" ({n} boxes)" if n > 1 else ""), "bg-emerald-100 text-emerald-700")
        delivered = sum(1 for p in pkgs if (p.get("status") or _tp_status(p.get("tp_status"))) == "DELIVERED")
        if delivered:
            return (f"{delivered} of {n} delivered", "bg-sky-100 text-sky-700")
        if "RETURNED" in statuses or "FAILURE" in statuses:
            return ("Problem", "bg-red-100 text-red-700")
        if "TRANSIT" in statuses:
            return ("In transit" + (f" ({n} boxes)" if n > 1 else ""), "bg-sky-100 text-sky-700")
        if "PRE_TRANSIT" in statuses:
            return ("Label created", "bg-slate-100 text-slate-600")
        return ("No scans yet", "bg-slate-100 text-slate-500")

    @property
    def tracking_eta(self):
        """Latest ETA among boxes still on the way; else the last delivery date."""
        from datetime import date
        pending, done = [], []
        for p in self.packages or []:
            st = p.get("status") or _tp_status(p.get("tp_status"))
            eta = p.get("eta") or p.get("tp_eta")
            if not eta:
                continue
            try:
                d = date.fromisoformat(eta[:10])
            except ValueError:
                continue
            (done if st == "DELIVERED" else pending).append(d)
        if pending:
            return max(pending)
        return max(done) if done else None


def _tp_status(tp):
    """Tradepost label status → our package status vocabulary."""
    return {"DELIVERED": "DELIVERED", "IN_TRANSIT": "TRANSIT", "OUT_FOR_DELIVERY": "TRANSIT",
            "AVAILABLE_FOR_PICKUP": "TRANSIT", "PRE_TRANSIT": "PRE_TRANSIT",
            "RETURN_TO_SENDER": "RETURNED", "FAILURE": "FAILURE"}.get(tp or "", "UNKNOWN")


# ---------------------------------------------------------------------------
# Manual adjustments
# ---------------------------------------------------------------------------
class StockAdjustment(models.Model):
    REASONS = [
        ("opening", "Opening balance / initial count"),
        ("received", "Received (untracked)"),
        ("damaged", "Damaged"),
        ("shrinkage", "Shrinkage"),
        ("sample", "Sample"),
        ("correction", "Correction"),
        ("personal", "Personal use"),
    ]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="adjustments")
    qty_delta = models.IntegerField(help_text="Positive to add stock, negative to remove")
    reason = models.CharField(max_length=20, choices=REASONS)
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text="Optional. When ADDING stock: units come in at this cost and the "
                  "weighted average rolls (like a receipt). Blank = current avg cost.")
    note = models.CharField(max_length=500, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(default=timezone.now)
    movement = models.OneToOneField(StockMovement, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="adjustment")

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.product.sku} {self.qty_delta:+d} ({self.reason})"

    @classmethod
    def create_and_apply(cls, *, product, qty_delta, reason, note="", user=None,
                         unit_cost=None):
        """
        Apply an adjustment. If ``unit_cost`` is given and stock is being ADDED,
        the weighted average cost rolls exactly like a supplier receipt — this
        is how opening balances from an existing business are entered.
        Removals always leave the average cost unchanged (units go out at avg).
        """
        with transaction.atomic():
            adj = cls.objects.create(product=product, qty_delta=qty_delta,
                                     reason=reason, note=note, unit_cost=unit_cost,
                                     user=user if getattr(user, "is_authenticated", False) else None)
            new_avg = None
            move_cost = None
            if unit_cost is not None and qty_delta > 0:
                unit_cost = Decimal(unit_cost)
                move_cost = unit_cost
                on_hand = Product.all_objects.get(pk=product.pk).qty_on_hand
                if on_hand <= 0:
                    new_avg = unit_cost.quantize(FOURPLACES, ROUND_HALF_UP)
                else:
                    new_avg = (
                        (Decimal(on_hand) * product.avg_cost + Decimal(qty_delta) * unit_cost)
                        / Decimal(on_hand + qty_delta)
                    ).quantize(FOURPLACES, ROUND_HALF_UP)
            movement = product.apply_movement(
                movement_type="adjustment", qty_delta=qty_delta, user=user,
                unit_cost=move_cost, new_avg_cost=new_avg,
                reference=f"ADJ-{adj.pk}", source=adj,
                note=f"{adj.get_reason_display()}: {note}"[:500])
            adj.movement = movement
            adj.save(update_fields=["movement"])
            log_activity(user, "adjustment", product,
                         changes={"delta": qty_delta, "reason": reason,
                                  "unit_cost": str(unit_cost) if unit_cost else None},
                         note=note)
            return adj


# ---------------------------------------------------------------------------
# Cycle counts
# ---------------------------------------------------------------------------
class CycleCount(SoftDeleteModel):
    STATUS = [("open", "Open"), ("applied", "Applied"), ("cancelled", "Cancelled")]
    name = models.CharField(max_length=120, default="Cycle count")
    status = models.CharField(max_length=12, choices=STATUS, default="open")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    def apply(self, user=None):
        if self.status != "open":
            raise ValidationError("Cycle count is not open.")
        with transaction.atomic():
            for line in self.lines.filter(counted_qty__isnull=False).select_related("product"):
                delta = line.counted_qty - line.snapshot_qty
                line.delta = delta
                line.save(update_fields=["delta"])
                if delta != 0:
                    line.product.apply_movement(
                        movement_type="cycle_count", qty_delta=delta, user=user,
                        reference=f"CC-{self.pk}", source=self,
                        note=f"Cycle count: {line.snapshot_qty} -> {line.counted_qty}")
            self.status = "applied"
            self.applied_at = timezone.now()
            self.save(update_fields=["status", "applied_at"])
            log_activity(user, "cycle_count_applied", self)


class CycleCountLine(models.Model):
    cycle_count = models.ForeignKey(CycleCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    snapshot_qty = models.IntegerField(help_text="On-hand at count creation")
    counted_qty = models.IntegerField(null=True, blank=True)
    delta = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = [("cycle_count", "product")]

    def __str__(self):
        return f"{self.product.sku}: {self.snapshot_qty} -> {self.counted_qty}"
