"""
Customers, contacts, shipping addresses, resale certificates.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.utils import timezone

from core.models import SoftDeleteModel


PAYMENT_TERMS = [
    ("due_on_receipt", "Due on receipt"),
    ("net15", "Net 15"),
    ("net30", "Net 30"),
    ("net45", "Net 45"),
    ("net60", "Net 60"),
]

TERM_DAYS = {"due_on_receipt": 0, "net15": 15, "net30": 30, "net45": 45, "net60": 60}


class Customer(SoftDeleteModel):
    NO_CERT_POLICY = [
        ("charge_tax", "Charge sales tax"),
        ("block", "Block finalization"),
    ]

    business_name = models.CharField(max_length=200, db_index=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True, help_text="Primary billing email")

    # Billing address
    billing_line1 = models.CharField(max_length=200, blank=True)
    billing_line2 = models.CharField(max_length=200, blank=True)
    billing_city = models.CharField(max_length=100, blank=True)
    billing_state = models.CharField(max_length=50, blank=True)
    billing_zip = models.CharField(max_length=20, blank=True)

    payment_terms = models.CharField(max_length=20, choices=PAYMENT_TERMS, default="net30")
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_list = models.ForeignKey("inventory.PriceList", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="customers")
    discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Extra discount %% applied when no price-list price exists")
    no_cert_policy = models.CharField(
        max_length=20, choices=NO_CERT_POLICY, default="charge_tax",
        help_text="What happens on orders when there is no valid resale certificate")
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["business_name"]

    def __str__(self):
        return self.business_name

    # --- certificates ------------------------------------------------------
    @property
    def current_certificate(self):
        today = timezone.localdate()
        return (self.certificates.filter(is_deleted=False,
                                         expiration_date__gte=today)
                .order_by("-expiration_date").first())

    @property
    def has_valid_certificate(self):
        return self.current_certificate is not None

    @property
    def certificate_status(self):
        """'valid' | 'expiring' (<=30 days) | 'expired' | 'none'"""
        today = timezone.localdate()
        cert = (self.certificates.filter(is_deleted=False)
                .order_by("-expiration_date").first())
        if cert is None:
            return "none"
        if cert.expiration_date < today:
            return "expired"
        if cert.expiration_date <= today + timedelta(days=30):
            return "expiring"
        return "valid"

    # --- money -------------------------------------------------------------
    @property
    def balance(self):
        """Invoiced − paid − credited, over live open/partial invoices."""
        from invoicing.models import Invoice
        total = Decimal("0")
        for inv in self.invoices.filter(is_deleted=False).exclude(status="void"):
            total += inv.balance_due
        return total

    @property
    def term_days(self):
        return TERM_DAYS.get(self.payment_terms, 30)

    def default_shipping_address(self):
        return (self.shipping_addresses.filter(is_deleted=False, is_default=True).first()
                or self.shipping_addresses.filter(is_deleted=False).first())

    def price_for(self, product):
        """Per-SKU price list first, then customer discount off default price."""
        if self.price_list_id:
            p = self.price_list.price_for(product)
            if p is not None:
                return p
        base = product.default_price
        if self.discount_pct:
            base = (base * (Decimal("100") - self.discount_pct) / Decimal("100")).quantize(Decimal("0.01"))
        return base


class Contact(SoftDeleteModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    role = models.CharField(max_length=100, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} ({self.customer.business_name})"


class ShippingAddress(SoftDeleteModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,
                                 related_name="shipping_addresses")
    label = models.CharField(max_length=100, default="Main",
                             help_text="e.g. Store #2, Warehouse")
    attention = models.CharField(max_length=150, blank=True)
    line1 = models.CharField(max_length=200)
    line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    zip_code = models.CharField(max_length=20)
    phone = models.CharField(max_length=50, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "label"]
        verbose_name_plural = "shipping addresses"

    def __str__(self):
        return f"{self.label}: {self.line1}, {self.city} {self.state}"

    def as_text(self):
        parts = [self.attention, self.line1, self.line2,
                 f"{self.city}, {self.state} {self.zip_code}"]
        return "\n".join(p for p in parts if p)


class ResaleCertificate(SoftDeleteModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="certificates")
    file = models.FileField(upload_to="certificates/", blank=True, null=True,
                            help_text="PDF or image of the certificate")
    certificate_number = models.CharField(max_length=100)
    issuing_state = models.CharField(max_length=50)
    issue_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-expiration_date"]

    def __str__(self):
        return f"{self.certificate_number} ({self.issuing_state}) exp {self.expiration_date}"

    @property
    def is_expired(self):
        return self.expiration_date < timezone.localdate()

    @property
    def expires_soon(self):
        today = timezone.localdate()
        return today <= self.expiration_date <= today + timedelta(days=30)
