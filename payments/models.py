"""
Payments and application of payments across one or more invoices.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import SoftDeleteModel, log_activity


class Payment(SoftDeleteModel):
    METHODS = [
        ("check", "Check"), ("wire", "Wire"), ("ach", "ACH"),
        ("zelle", "Zelle"), ("card", "Card"), ("cash", "Cash"), ("other", "Other"),
    ]
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT,
                                 related_name="payments")
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=METHODS)
    reference = models.CharField(max_length=100, blank=True,
                                 help_text="Check #, wire confirmation, etc.")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.customer.business_name} ${self.amount} {self.get_method_display()} {self.date}"

    @property
    def amount_applied(self):
        agg = self.applications.aggregate(t=models.Sum("amount"))
        return agg["t"] or Decimal("0.00")

    @property
    def amount_unapplied(self):
        return self.amount - self.amount_applied

    def apply_to_invoice(self, invoice, amount, user=None):
        """Apply part (or all) of this payment to an invoice."""
        amount = Decimal(amount)
        if amount <= 0:
            raise ValidationError("Application amount must be positive.")
        if amount > self.amount_unapplied:
            raise ValidationError(
                f"Only ${self.amount_unapplied} of this payment is unapplied.")
        if invoice.customer_id != self.customer_id:
            raise ValidationError("Invoice belongs to a different customer.")
        if amount > invoice.balance_due:
            raise ValidationError(
                f"{invoice.number} balance is ${invoice.balance_due}; cannot over-apply.")
        with transaction.atomic():
            app = PaymentApplication.objects.create(
                payment=self, invoice=invoice, amount=amount)
            invoice.refresh_status()
            log_activity(user, "payment_applied", invoice,
                         changes={"payment_id": self.pk, "amount": str(amount)})
            return app

    def unapply(self, application, user=None):
        invoice = application.invoice
        application.delete()
        invoice.refresh_status()
        log_activity(user, "payment_unapplied", invoice,
                     changes={"payment_id": self.pk})


class PaymentApplication(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="applications")
    invoice = models.ForeignKey("invoicing.Invoice", on_delete=models.PROTECT,
                                related_name="payment_applications")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-applied_at"]

    def __str__(self):
        return f"${self.amount} of {self.payment} -> {self.invoice.number}"
