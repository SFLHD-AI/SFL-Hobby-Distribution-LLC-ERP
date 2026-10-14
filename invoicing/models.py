"""
Invoices (single-PO or consolidated multi-PO), credit memos / returns.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import CompanySettings, NumberSequence, SoftDeleteModel, log_activity

CENTS = Decimal("0.01")


class Invoice(SoftDeleteModel):
    OPEN, PARTIAL, PAID, VOID = "open", "partial", "paid", "void"
    STATUS_CHOICES = [(OPEN, "Open"), (PARTIAL, "Partially paid"),
                      (PAID, "Paid"), (VOID, "Void")]

    number = models.CharField(max_length=30, unique=True, db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT,
                                 related_name="invoices")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN, db_index=True)

    date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    terms = models.CharField(max_length=20, default="net30")

    # Bill-to / ship-to snapshots (immutable even if customer later edited)
    bill_to = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    # Money — stored, not recomputed, so history never shifts
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_overridden = models.BooleanField(
        default=False, help_text="Tax amount was manually overridden on this invoice")
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    notes = models.TextField(blank=True)
    # Opening-balance transfers / AR corrections: real receivables, but NOT
    # revenue earned this period — excluded from P&L, profit and tax reports.
    is_ar_adjustment = models.BooleanField(default=False)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="invoices_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.number} — {self.customer.business_name} (${self.total})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next_for("invoice")
        if not self.due_date:
            self.due_date = self.date + timedelta(days=self.customer.term_days)
        super().save(*args, **kwargs)

    # --- money ---------------------------------------------------------------
    @property
    def amount_paid(self):
        agg = self.payment_applications.filter(payment__is_deleted=False).aggregate(
            t=models.Sum("amount"))
        return agg["t"] or Decimal("0.00")

    @property
    def amount_credited(self):
        agg = self.credit_memos.filter(is_deleted=False, status="applied").aggregate(
            t=models.Sum("total"))
        return agg["t"] or Decimal("0.00")

    @property
    def balance_due(self):
        if self.status == self.VOID:
            return Decimal("0.00")
        return self.total - self.amount_paid - self.amount_credited

    @property
    def is_overdue(self):
        return self.balance_due > 0 and self.due_date < timezone.localdate()

    @property
    def days_past_due(self):
        return max(0, (timezone.localdate() - self.due_date).days)

    @property
    def po_numbers(self):
        return sorted({l.po.number for l in self.lines.filter(is_deleted=False) if l.po_id})

    def refresh_status(self):
        if self.status == self.VOID:
            return
        bal = self.balance_due
        if bal <= 0:
            self.status = self.PAID
        elif self.amount_paid > 0 or self.amount_credited > 0:
            self.status = self.PARTIAL
        else:
            self.status = self.OPEN
        self.save(update_fields=["status"])

    def compute_totals(self, tax_amount_override=None):
        """Recompute stored totals from lines. Call while drafting only."""
        lines = list(self.lines.filter(is_deleted=False))
        subtotal = sum((l.line_total for l in lines), Decimal("0.00"))
        self.subtotal = subtotal
        taxable = subtotal + self.shipping_charge
        if tax_amount_override is not None:
            self.tax_amount = Decimal(tax_amount_override).quantize(CENTS)
            self.tax_overridden = True
        else:
            self.tax_amount = (taxable * self.tax_rate / Decimal("100")).quantize(CENTS, ROUND_HALF_UP)
        self.total = subtotal + self.shipping_charge + self.tax_amount
        self.save(update_fields=["subtotal", "tax_amount", "tax_overridden", "total"])

    # --- profit (admin only in UI) --------------------------------------------
    @property
    def cogs(self):
        total = Decimal("0.00")
        for line in self.lines.filter(is_deleted=False):
            if line.cost_snapshot is not None:
                total += (Decimal(line.qty) * line.cost_snapshot)
        return total.quantize(CENTS, ROUND_HALF_UP)

    @property
    def gross_profit(self):
        return self.subtotal - self.cogs

    # --- builders --------------------------------------------------------------
    @classmethod
    def build_from_pos(cls, pos, user=None, shipping_charge=None, tax_rate=None,
                       tax_amount_override=None, notes=""):
        """
        Create one invoice from one or more FINALIZED POs of the same customer.
        Line groups keep their PO reference.

        Tax rule: a customer with a VALID resale certificate is invoiced at 0%
        no matter what the PO's tax checkbox says — the certificate wins.
        Without a valid certificate the PO's rate applies. An explicit
        ``tax_rate`` or ``tax_amount_override`` argument beats both.
        """
        pos = list(pos)
        if not pos:
            raise ValidationError("No POs given.")
        customer = pos[0].customer
        if any(po.customer_id != customer.pk for po in pos):
            raise ValidationError("All POs on one invoice must belong to the same customer.")
        for po in pos:
            # Invoiceable: FINALIZED, or already SHIPPED/delivered but never
            # billed (local deliveries can ship before the invoice is cut).
            if po.status == po.SHIPPED:
                if po.has_live_invoice:
                    raise ValidationError(f"{po.number} is already invoiced.")
            elif po.status != po.FINALIZED:
                raise ValidationError(f"{po.number} is not finalized.")

        settings_row = CompanySettings.get()
        with transaction.atomic():
            ship_to_text = ""
            if pos[0].ship_to:
                ship_to_text = pos[0].ship_to.as_text()
            bill_parts = [customer.business_name, customer.billing_line1,
                          customer.billing_line2,
                          f"{customer.billing_city}, {customer.billing_state} {customer.billing_zip}"]
            invoice = cls.objects.create(
                customer=customer,
                date=timezone.localdate(),
                due_date=timezone.localdate() + timedelta(days=customer.term_days),
                terms=customer.payment_terms,
                bill_to="\n".join(p for p in bill_parts if p and p.strip(", ")),
                ship_to=ship_to_text,
                shipping_charge=(shipping_charge if shipping_charge is not None
                                 else sum((po.shipping_charge for po in pos if po.charge_shipping),
                                          Decimal("0.00"))),
                tax_rate=(tax_rate if tax_rate is not None
                          else Decimal("0") if customer.has_valid_certificate
                          else (pos[0].tax_rate if pos[0].charge_tax else Decimal("0"))),
                notes=notes,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            for po in pos:
                for line in po.live_lines.select_related("product"):
                    qty = line.fulfilled_qty
                    if qty <= 0:
                        continue
                    InvoiceLine.objects.create(
                        invoice=invoice, po=po, product=line.product,
                        description=line.description or line.product.name,
                        qty=qty, unit_price=line.unit_price,
                        discount_pct=line.discount_pct,
                        cost_snapshot=line.cost_snapshot)
                po.mark_invoiced()
            invoice.compute_totals(tax_amount_override=tax_amount_override)
            log_activity(user, "invoice_created", invoice,
                         note=f"From POs: {', '.join(p.number for p in pos)}")
            return invoice

    @classmethod
    def build_manual(cls, customer, user=None, shipping_charge=None,
                     tax_rate=None, notes=""):
        """Create an empty invoice with NO purchase order — a direct AR
        charge (storage fees, adjustments, one-off sales). Line items are
        then added with ``add_manual_line``.

        Tax follows the same rule as PO invoices: a valid resale
        certificate forces 0%; otherwise the company default rate applies
        unless an explicit ``tax_rate`` is passed."""
        settings_row = CompanySettings.get()
        bill_parts = [customer.business_name, customer.billing_line1,
                      customer.billing_line2,
                      f"{customer.billing_city}, {customer.billing_state} {customer.billing_zip}"]
        invoice = cls.objects.create(
            customer=customer,
            date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=customer.term_days),
            terms=customer.payment_terms,
            bill_to="\n".join(p for p in bill_parts if p and p.strip(", ")),
            shipping_charge=shipping_charge or Decimal("0.00"),
            tax_rate=(tax_rate if tax_rate is not None          # explicit wins
                      else Decimal("0") if customer.has_valid_certificate
                      else settings_row.default_tax_rate),
            notes=notes,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        invoice.compute_totals()
        log_activity(user, "invoice_created", invoice, note="Manual invoice (no PO)")
        return invoice

    def add_manual_line(self, *, description, qty, unit_price, unit_cost=None,
                        category_name="", user=None):
        """Add a one-off item that isn't in inventory (fees, supplies,
        singles/breaks sold ad hoc). Price + cost are entered by hand;
        the category (created on the fly if new) feeds the
        profit-by-category report. Recomputes totals, honoring an
        existing manual tax override."""
        if self.status == self.VOID:
            raise ValidationError("Can't add items to a void invoice.")
        if self.amount_paid > 0:
            raise ValidationError("This invoice already has payments applied — "
                                  "issue a new invoice for extra items instead.")
        category = None
        category_name = (category_name or "").strip()
        if category_name:
            from inventory.models import Category
            category = Category.all_objects.filter(name__iexact=category_name).first()
            if category is None:
                category = Category.objects.create(name=category_name)
            elif category.is_deleted:
                category.is_deleted = False
                category.save(update_fields=["is_deleted"])
        line = InvoiceLine.objects.create(
            invoice=self, description=description, qty=qty,
            unit_price=Decimal(unit_price),
            cost_snapshot=Decimal(unit_cost) if unit_cost not in (None, "") else None,
            category=category, is_manual=True)
        self.compute_totals(
            tax_amount_override=self.tax_amount if self.tax_overridden else None)
        log_activity(user, "invoice_manual_line", self,
                     note=f"{description} x{qty} @ ${unit_price}"
                          + (f" [{category_name}]" if category_name else ""))
        return line

    def remove_manual_line(self, line_id, user=None):
        """Remove a manual line while the invoice is still unpaid."""
        if self.amount_paid > 0:
            raise ValidationError("This invoice has payments applied.")
        line = self.lines.get(pk=line_id, is_manual=True, is_deleted=False)
        line.delete(user=user)
        self.compute_totals(
            tax_amount_override=self.tax_amount if self.tax_overridden else None)
        log_activity(user, "invoice_manual_line_removed", self, note=line.description)

    def void(self, user=None, reason=""):
        if self.amount_paid > 0:
            raise ValidationError("Cannot void an invoice with payments applied. "
                                  "Unapply the payments first.")
        with transaction.atomic():
            self.status = self.VOID
            self.save(update_fields=["status"])
            # Release the POs so they can be re-invoiced (or un-finalized).
            pos = {l.po for l in self.lines.filter(is_deleted=False) if l.po_id}
            for po in pos:
                if po.status == po.INVOICED:
                    po.status = po.FINALIZED
                    po.save(update_fields=["status"])
            log_activity(user, "invoice_voided", self, note=reason)


class InvoiceLine(SoftDeleteModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    po = models.ForeignKey("orders.PurchaseOrder", null=True, blank=True,
                           on_delete=models.SET_NULL, related_name="invoice_lines")
    product = models.ForeignKey("inventory.Product", null=True, blank=True,
                                on_delete=models.PROTECT, related_name="invoice_lines")
    description = models.CharField(max_length=250)
    qty = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cost_snapshot = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    # For MANUAL (one-off) lines: no product, so the category lives on the
    # line itself — this is what feeds the profit-by-category report.
    category = models.ForeignKey("inventory.Category", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="invoice_lines")
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["po_id", "id"]

    def __str__(self):
        return f"{self.invoice.number}: {self.description} x{self.qty}"

    @property
    def line_total(self):
        gross = Decimal(self.qty) * self.unit_price
        if self.discount_pct:
            gross = gross * (Decimal("100") - self.discount_pct) / Decimal("100")
        return gross.quantize(CENTS, ROUND_HALF_UP)

    @property
    def line_profit(self):
        if self.cost_snapshot is None:
            return None
        return self.line_total - (Decimal(self.qty) * self.cost_snapshot).quantize(CENTS, ROUND_HALF_UP)


class CreditMemo(SoftDeleteModel):
    DRAFT, APPLIED, VOID = "draft", "applied", "void"
    STATUS_CHOICES = [(DRAFT, "Draft"), (APPLIED, "Applied"), (VOID, "Void")]

    number = models.CharField(max_length=30, unique=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_memos")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT,
                                 related_name="credit_memos")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=DRAFT)
    date = models.DateField(default=timezone.localdate)
    reason = models.CharField(max_length=250, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.number} against {self.invoice.number} (${self.total})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next_for("credit")
        if not self.customer_id and self.invoice_id:
            self.customer = self.invoice.customer
        super().save(*args, **kwargs)

    def apply(self, user=None):
        """Apply the memo: optionally restock lines, reduce customer balance."""
        if self.status != self.DRAFT:
            raise ValidationError("Credit memo is not draft.")
        with transaction.atomic():
            total = Decimal("0.00")
            for line in self.lines.filter(is_deleted=False).select_related("product"):
                total += line.line_total
                if line.restock and line.product:
                    line.product.apply_movement(
                        movement_type="credit_restock", qty_delta=line.qty, user=user,
                        unit_cost=line.cost_snapshot or line.product.avg_cost,
                        reference=self.number, source=self,
                        note=f"Return on {self.invoice.number}")
            self.total = total
            self.status = self.APPLIED
            self.save(update_fields=["total", "status"])
            self.invoice.refresh_status()
            log_activity(user, "credit_applied", self)


class CreditMemoLine(SoftDeleteModel):
    credit_memo = models.ForeignKey(CreditMemo, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", null=True, blank=True,
                                on_delete=models.PROTECT)
    description = models.CharField(max_length=250)
    qty = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    restock = models.BooleanField(default=True, help_text="Return units to inventory")
    cost_snapshot = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    def __str__(self):
        return f"{self.credit_memo.number}: {self.description} x{self.qty}"

    @property
    def line_total(self):
        return (Decimal(self.qty) * self.unit_price).quantize(CENTS, ROUND_HALF_UP)
