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

    def barcode_value(self):
        return self.upc or self.sku

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
            self.save(update_fields=["posted", "posted_at"])
            log_activity(user, "post", self, note=f"Posted receipt, {len(lines)} lines")


class ReceiptLine(SoftDeleteModel):
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="receipt_lines")
    qty = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4,
                                    help_text="Supplier unit cost before freight/duties")
    landed_unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    def __str__(self):
        return f"{self.product.sku} x{self.qty} @ {self.unit_cost}"

    @property
    def line_total(self):
        return (Decimal(self.qty) * self.unit_cost).quantize(CENTS, ROUND_HALF_UP)


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
