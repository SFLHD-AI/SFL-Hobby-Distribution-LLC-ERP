"""
Customer purchase orders (a "PO" here is an order a customer places with us —
it consumes our inventory) plus the picking workflow.

Status machine
--------------
Draft -> Confirmed -> Picking -> Picked -> Finalized -> Invoiced -> Shipped/Delivered
Any pre-Finalized status -> Cancelled (releases commitments).

* Confirming commits inventory (available drops; on-hand unchanged).
* Finalizing decrements on-hand via the stock ledger and snapshots the
  weighted-average landed cost on every line, so later cost changes never
  rewrite history.
* Short-picked quantities can be split to a backorder child PO.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import NumberSequence, SoftDeleteModel, log_activity

CENTS = Decimal("0.01")


class PurchaseOrder(SoftDeleteModel):
    DRAFT, CONFIRMED, PICKING, PICKED, FINALIZED, INVOICED, SHIPPED, DELIVERED, CANCELLED = (
        "draft", "confirmed", "picking", "picked", "finalized",
        "invoiced", "shipped", "delivered", "cancelled")
    STATUS_CHOICES = [
        (DRAFT, "Draft"), (CONFIRMED, "Confirmed"), (PICKING, "Picking"),
        (PICKED, "Picked"), (FINALIZED, "Finalized"), (INVOICED, "Invoiced"),
        (SHIPPED, "Shipped"), (DELIVERED, "Delivered"), (CANCELLED, "Cancelled")]
    # Statuses in which lines count against available inventory
    COMMITTED_STATUSES = [CONFIRMED, PICKING, PICKED]
    OPEN_STATUSES = [DRAFT, CONFIRMED, PICKING, PICKED, FINALIZED]

    PRIORITIES = [(1, "Rush"), (2, "High"), (3, "Normal"), (4, "Low")]

    number = models.CharField(max_length=30, unique=True, db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT,
                                 related_name="purchase_orders")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    priority = models.PositiveSmallIntegerField(choices=PRIORITIES, default=3)

    ship_to = models.ForeignKey("customers.ShippingAddress", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    customer_po_ref = models.CharField(max_length=100, blank=True,
                                       help_text="Customer's own PO/reference number")
    order_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Shipping charge (checkbox + amount, per your spec)
    charge_shipping = models.BooleanField(default=False)
    shipping_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Sales tax (checkbox + rate; amount can be overridden at invoicing)
    charge_tax = models.BooleanField(default=False)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=0,
                                   help_text="Percent; defaults from Settings when box is checked")

    # Local delivery: fulfilled by our own driver instead of a carrier
    local_delivery = models.BooleanField(
        default=False, help_text="Delivered by our driver — shows on the Deliveries screen")

    notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True, help_text="Never shown to the customer")

    # Backorder chain
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="backorders")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="pos_created")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    picking_started_at = models.DateTimeField(null=True, blank=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} — {self.customer.business_name}"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next_for("po")
        super().save(*args, **kwargs)

    # --- money ---------------------------------------------------------------
    @property
    def lines_subtotal(self):
        return sum((l.line_total for l in self.live_lines), Decimal("0"))

    @property
    def order_discount_amount(self):
        return (self.lines_subtotal * self.order_discount_pct / Decimal("100")).quantize(CENTS, ROUND_HALF_UP)

    @property
    def subtotal(self):
        return self.lines_subtotal - self.order_discount_amount

    @property
    def tax_amount(self):
        if not self.charge_tax:
            return Decimal("0.00")
        taxable = self.subtotal + (self.shipping_charge if self.charge_shipping else 0)
        return (taxable * self.tax_rate / Decimal("100")).quantize(CENTS, ROUND_HALF_UP)

    @property
    def total(self):
        t = self.subtotal + self.tax_amount
        if self.charge_shipping:
            t += self.shipping_charge
        return t

    @property
    def live_lines(self):
        return self.lines.filter(is_deleted=False)

    @property
    def total_units(self):
        return sum(l.qty_ordered for l in self.live_lines)

    @property
    def picked_units(self):
        return sum(l.qty_picked for l in self.live_lines)

    @property
    def pick_progress_pct(self):
        total = self.total_units
        return int(self.picked_units / total * 100) if total else 0

    @property
    def total_weight_lbs(self):
        """Estimated shipping weight from per-unit product weights."""
        total = Decimal("0")
        missing = False
        for line in self.live_lines.select_related("product"):
            qty = line.fulfilled_qty
            if line.product.weight_lbs is None:
                missing = True
                continue
            total += Decimal(qty) * line.product.weight_lbs
        return {"weight": total.quantize(Decimal("0.01")), "missing_weights": missing}

    @property
    def is_editable(self):
        return self.status in (self.DRAFT, self.CONFIRMED)

    # --- status transitions ----------------------------------------------------
    def _require_status(self, *allowed):
        if self.status not in allowed:
            raise ValidationError(
                f"{self.number}: cannot do that from status '{self.get_status_display()}'.")

    def confirm(self, user=None):
        """Draft -> Confirmed. Commits inventory (checked against available)."""
        self._require_status(self.DRAFT)
        if not self.live_lines.exists():
            raise ValidationError("Cannot confirm a PO with no lines.")
        # Cert policy check happens at finalization; warn-level at confirm is done in views.
        with transaction.atomic():
            for line in self.live_lines.select_related("product"):
                avail = line.product.qty_available
                if line.qty_ordered > avail:
                    raise ValidationError(
                        f"{line.product.sku}: ordering {line.qty_ordered} but only "
                        f"{avail} available. Reduce the line or split a backorder.")
            self.status = self.CONFIRMED
            self.confirmed_at = timezone.now()
            self.save(update_fields=["status", "confirmed_at"])
            log_activity(user, "po_confirmed", self)

    def start_picking(self, user=None):
        self._require_status(self.CONFIRMED)
        self.status = self.PICKING
        self.picking_started_at = timezone.now()
        self.save(update_fields=["status", "picking_started_at"])
        log_activity(user, "po_picking", self)

    def complete_pick(self, user=None):
        """Picking -> Picked. Every line must be fully picked or marked short."""
        self._require_status(self.PICKING)
        incomplete = [l for l in self.live_lines
                      if l.qty_picked < l.qty_ordered and not l.short_reason]
        if incomplete:
            raise ValidationError(
                "Lines not fully picked and not marked short: "
                + ", ".join(l.product.sku for l in incomplete))
        self.status = self.PICKED
        self.picked_at = timezone.now()
        self.save(update_fields=["status", "picked_at"])
        log_activity(user, "po_picked", self)

    def finalize(self, user=None):
        """
        Picked (or Confirmed for office-only flow) -> Finalized.
        Decrements on-hand stock by picked qty (or ordered qty if never picked)
        and snapshots weighted average cost per line.
        """
        self._require_status(self.PICKED, self.CONFIRMED)
        cert_ok = self.customer.has_valid_certificate
        if not cert_ok and self.customer.no_cert_policy == "block":
            raise ValidationError(
                f"{self.customer.business_name} has no valid resale certificate and "
                "their policy blocks finalization. Upload a certificate or change the "
                "customer's no-certificate policy.")
        with transaction.atomic():
            for line in self.live_lines.select_related("product"):
                qty = line.fulfilled_qty
                if qty <= 0:
                    continue
                product = line.product
                product.apply_movement(
                    movement_type="sale", qty_delta=-qty, user=user,
                    unit_cost=product.avg_cost,
                    reference=self.number, source=self,
                    note=f"Sale to {self.customer.business_name}")
                line.cost_snapshot = product.avg_cost
                line.save(update_fields=["cost_snapshot"])
            self.status = self.FINALIZED
            self.finalized_at = timezone.now()
            self.save(update_fields=["status", "finalized_at"])
            log_activity(user, "po_finalized", self)

    def unfinalize(self, user=None, reason=""):
        """
        Admin repair tool: reverse a finalization done by mistake (e.g. costs
        weren't loaded yet). Puts the sold units back into stock through the
        ledger, clears the cost snapshots, and returns the PO to Picked (or
        Confirmed if it was never picked) so it can be finalized again after
        the costs are corrected. Blocked while a live invoice references it.
        """
        self._require_status(self.FINALIZED)
        live_inv_lines = self.invoice_lines.filter(
            is_deleted=False, invoice__is_deleted=False).exclude(
            invoice__status="void")
        if live_inv_lines.exists():
            raise ValidationError(
                "This order is on an invoice. Void the invoice first "
                "(unapply any payments), then un-finalize.")
        with transaction.atomic():
            for line in self.live_lines.select_related("product"):
                qty = line.fulfilled_qty
                if qty > 0:
                    line.product.apply_movement(
                        movement_type="adjustment", qty_delta=qty, user=user,
                        reference=self.number, source=self,
                        note=f"Finalization reversed: {reason or 'admin fix'}"[:500])
                if line.cost_snapshot is not None:
                    line.cost_snapshot = None
                    line.save(update_fields=["cost_snapshot"])
            self.status = self.PICKED if self.picking_started_at else self.CONFIRMED
            self.finalized_at = None
            self.save(update_fields=["status", "finalized_at"])
            log_activity(user, "po_unfinalized", self, note=reason)

    def cancel(self, user=None, reason=""):
        self._require_status(self.DRAFT, self.CONFIRMED, self.PICKING, self.PICKED)
        self.status = self.CANCELLED
        self.cancelled_at = timezone.now()
        self.save(update_fields=["status", "cancelled_at"])
        log_activity(user, "po_cancelled", self, note=reason)

    def mark_invoiced(self):
        # A PO that was delivered/shipped before invoicing stays SHIPPED —
        # invoicing must not walk its status backwards.
        if self.status == self.SHIPPED:
            return
        self._require_status(self.FINALIZED)
        self.status = self.INVOICED
        self.save(update_fields=["status"])

    @property
    def has_live_invoice(self):
        """True if any non-void invoice bills this PO."""
        from invoicing.models import Invoice
        return self.invoice_lines.filter(is_deleted=False).exclude(
            invoice__status=Invoice.VOID).exists()

    def mark_shipped(self):
        if self.status in (self.FINALIZED, self.INVOICED):
            self.status = self.SHIPPED
            self.save(update_fields=["status"])

    # --- backorders ---------------------------------------------------------
    def create_backorder(self, user=None):
        """
        Split all shortfalls (ordered − fulfilled) into a child Draft PO.
        Call after short-picks, at/after finalization time.
        """
        shorts = [(l, l.qty_ordered - l.fulfilled_qty) for l in self.live_lines
                  if l.qty_ordered - l.fulfilled_qty > 0]
        if not shorts:
            raise ValidationError("No short quantities to backorder.")
        with transaction.atomic():
            child = PurchaseOrder.objects.create(
                customer=self.customer, ship_to=self.ship_to, parent=self,
                priority=self.priority, created_by=user if getattr(user, "is_authenticated", False) else None,
                charge_shipping=self.charge_shipping, shipping_charge=Decimal("0"),
                charge_tax=self.charge_tax, tax_rate=self.tax_rate,
                order_discount_pct=self.order_discount_pct,
                notes=f"Backorder from {self.number}")
            for line, short_qty in shorts:
                POLine.objects.create(
                    po=child, product=line.product, description=line.description,
                    qty_ordered=short_qty, unit_price=line.unit_price,
                    discount_pct=line.discount_pct)
                line.backordered_qty = short_qty
                line.save(update_fields=["backordered_qty"])
            log_activity(user, "backorder_created", child,
                         note=f"From {self.number}: {len(shorts)} lines")
            return child


class POLine(SoftDeleteModel):
    SHORT_REASONS = [
        ("oos", "Out of stock"),
        ("damaged", "Damaged in bin"),
        ("not_found", "Not found"),
        ("other", "Other"),
    ]
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT,
                                related_name="po_lines")
    description = models.CharField(max_length=250, blank=True)
    qty_ordered = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Picking
    qty_picked = models.PositiveIntegerField(default=0)
    short_reason = models.CharField(max_length=12, choices=SHORT_REASONS, blank=True)
    short_note = models.CharField(max_length=250, blank=True)
    backordered_qty = models.PositiveIntegerField(default=0)

    # Snapshotted at finalization — this is the COGS basis forever
    cost_snapshot = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.po.number}: {self.product.sku} x{self.qty_ordered}"

    @property
    def line_total(self):
        gross = Decimal(self.qty_ordered) * self.unit_price
        if self.discount_pct:
            gross = gross * (Decimal("100") - self.discount_pct) / Decimal("100")
        return gross.quantize(CENTS, ROUND_HALF_UP)

    @property
    def fulfilled_qty(self):
        """Quantity actually leaving stock: picked if picking happened, else ordered."""
        if self.po.status in (PurchaseOrder.PICKING, PurchaseOrder.PICKED,
                              PurchaseOrder.FINALIZED, PurchaseOrder.INVOICED,
                              PurchaseOrder.SHIPPED, PurchaseOrder.DELIVERED):
            if self.po.picking_started_at:
                return self.qty_picked
        return self.qty_ordered

    @property
    def fulfilled_total(self):
        gross = Decimal(self.fulfilled_qty) * self.unit_price
        if self.discount_pct:
            gross = gross * (Decimal("100") - self.discount_pct) / Decimal("100")
        return gross.quantize(CENTS, ROUND_HALF_UP)

    @property
    def is_complete(self):
        return self.qty_picked >= self.qty_ordered or bool(self.short_reason)

    @property
    def line_profit(self):
        """Profit using the finalization cost snapshot. None before finalization."""
        if self.cost_snapshot is None:
            return None
        qty = self.fulfilled_qty
        revenue = self.fulfilled_total
        cogs = (Decimal(qty) * self.cost_snapshot).quantize(CENTS, ROUND_HALF_UP)
        return revenue - cogs


class PickEvent(models.Model):
    """Every scan / tap during picking, for the audit trail."""

    ACTIONS = [("scan", "Scan"), ("manual_add", "Manual +"), ("manual_remove", "Manual −"),
               ("short", "Marked short"), ("reset", "Reset")]
    line = models.ForeignKey(POLine, on_delete=models.CASCADE, related_name="pick_events")
    action = models.CharField(max_length=15, choices=ACTIONS)
    qty = models.IntegerField(default=1)
    barcode = models.CharField(max_length=64, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.line} {self.action} {self.qty:+d}"
