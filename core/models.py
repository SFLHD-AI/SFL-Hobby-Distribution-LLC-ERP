"""
Core models: company settings, document numbering, activity log, soft delete.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------
class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        return super().update(is_deleted=True, deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()

    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)


class SoftDeleteManager(models.Manager):
    """Default manager returns only live rows."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)


class AllObjectsManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteModel(models.Model):
    """Nothing in SFL ERP System is ever hard-deleted."""

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False, user=None):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        if user is not None:
            self.deleted_by = user
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def hard_delete(self, using=None, keep_parents=False):
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])


# ---------------------------------------------------------------------------
# Company settings (singleton)
# ---------------------------------------------------------------------------
class CompanySettings(models.Model):
    """Single-row table editable from the Settings screen."""

    company_name = models.CharField(max_length=200, default="My Company")
    logo = models.ImageField(upload_to="company/", blank=True, null=True,
                             help_text="PNG logo used on invoices, POs and statements")
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    website = models.CharField(max_length=200, blank=True)
    points_of_contact = models.TextField(
        blank=True, help_text="One per line: Name — role — phone/email")

    # Tax
    default_tax_rate = models.DecimalField(
        max_digits=6, decimal_places=3, default=0,
        help_text="Percent, e.g. 6.625. Applied to taxable orders; can be overridden per invoice.")

    # Invoice appearance
    INVOICE_TEMPLATES = [("modern", "Modern minimal"), ("classic", "Classic business")]
    invoice_template = models.CharField(max_length=20, choices=INVOICE_TEMPLATES, default="modern")
    invoice_footer = models.TextField(blank=True, help_text="Shown at the bottom of every invoice")
    default_payment_terms = models.CharField(max_length=20, default="net30")

    # Alerts
    low_stock_email_digest = models.BooleanField(default=False)
    alert_email = models.EmailField(blank=True, help_text="Where daily digests are sent")

    # Shipping integration keys (values here override the .env file)
    shipstation_api_key = models.CharField(max_length=200, blank=True)
    shippo_api_token = models.CharField(max_length=200, blank=True)
    shipstation_api_secret = models.CharField(max_length=200, blank=True)
    uship_api_key = models.CharField(max_length=300, blank=True)

    # Accounting
    BASIS_CHOICES = [("cash", "Cash basis (what the tax return uses)"),
                     ("accrual", "Accrual basis (true economic picture)")]
    fiscal_year_start_month = models.PositiveSmallIntegerField(
        default=1, help_text="1 = January. Retained earnings roll over at this month.")
    accounting_basis_default = models.CharField(
        max_length=10, choices=BASIS_CHOICES, default="cash",
        help_text="Basis shown first on the dashboard and financial statements")
    books_start_date = models.DateField(
        null=True, blank=True,
        help_text="First day this system is the book of record. Opening balances are dated here.")
    mileage_rate = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal("0.700"),
        help_text="IRS standard mileage rate, $/mile (2025: 0.70)")
    sales_tax_filing_frequency = models.CharField(
        max_length=10, default="quarterly",
        choices=[("monthly", "Monthly"), ("quarterly", "Quarterly"), ("annual", "Annual")])

    # Bank feed (Plaid) — values here override the .env file
    plaid_client_id = models.CharField(max_length=100, blank=True)
    plaid_secret = models.CharField(max_length=100, blank=True)
    PLAID_ENVS = [("sandbox", "Sandbox (test data)"), ("production", "Production (real banks)")]
    plaid_env = models.CharField(max_length=12, choices=PLAID_ENVS, default="sandbox")
    bank_sync_enabled = models.BooleanField(
        default=True, help_text="Pull new transactions from connected banks automatically")
    expense_review_digest = models.BooleanField(
        default=False, help_text="Weekly email: transactions awaiting review, bills due, missing receipts")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Company settings"
        verbose_name_plural = "Company settings"

    def __str__(self):
        return self.company_name

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def full_address(self):
        parts = [self.address_line1]
        if self.address_line2:
            parts.append(self.address_line2)
        parts.append(f"{self.city}, {self.state} {self.zip_code}".strip(", "))
        return "\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Roles: admin-definable module access
# ---------------------------------------------------------------------------
# Every gated area of the app. (key, human label, landing URL name)
MODULES = [
    ("dashboard", "Dashboard", "dashboard"),
    ("orders", "Orders (POs)", "po_list"),
    ("invoices", "Invoices & credits", "invoice_list"),
    ("payments", "Payments / AR", "payment_list"),
    ("customers", "Customers", "customer_list"),
    ("inventory", "Inventory & price lists", "product_list"),
    ("receiving", "Receiving & cycle counts", "receipt_list"),
    ("picking", "Warehouse picking", "pick_queue"),
    ("shipping", "Shipping", "shipment_list"),
    ("deliveries", "Local deliveries", "delivery_list"),
    ("reports", "Reports", "report_index"),
    ("expenses", "Expenses", "expense_list"),
    ("banking", "Banking & transactions", "bank_inbox"),
    ("accounting", "Accounting & financial statements", "accounting_index"),
]
MODULE_KEYS = [m[0] for m in MODULES]
MODULE_LANDING = {m[0]: m[2] for m in MODULES}


class Role(models.Model):
    """Admin-created role: which modules its users can open.

    System roles (Admin/Office/Warehouse/Fulfillment Manager) are seeded by
    ``setup_roles`` and can be edited but not deleted. ``is_admin_role``
    grants everything including Settings and the Users module.
    """

    name = models.CharField(max_length=80, unique=True)
    modules = models.JSONField(default=list,
                               help_text="List of module keys this role can access")
    can_see_costs = models.BooleanField(
        default=False, help_text="May view costs, margins and profit numbers")
    is_admin_role = models.BooleanField(
        default=False, help_text="Full access to everything incl. Settings and Users")
    is_system = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def module_labels(self):
        labels = dict((k, l) for k, l, _ in MODULES)
        return [labels[m] for m in self.modules if m in labels]


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="profile")
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="users")
    phone = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.role or 'no role'})"


# ---------------------------------------------------------------------------
# Configurable sequential document numbers
# ---------------------------------------------------------------------------
class NumberSequence(models.Model):
    """PO-1042, INV-2001, CM-11 ... prefix and next number are configurable."""

    KEYS = [("po", "Purchase order"), ("invoice", "Invoice"), ("credit", "Credit memo")]
    key = models.CharField(max_length=20, choices=KEYS, unique=True)
    prefix = models.CharField(max_length=10, default="")
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=4, help_text="Zero-pad width, e.g. 4 -> 0042")

    def __str__(self):
        return f"{self.key}: {self.prefix}{str(self.next_number).zfill(self.padding)}"

    @classmethod
    def next_for(cls, key):
        """Atomically claim the next number for a document type."""
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(
                key=key,
                defaults={"prefix": {"po": "PO-", "invoice": "INV-", "credit": "CM-"}.get(key, "")})
            number = seq.next_number
            seq.next_number = number + 1
            seq.save(update_fields=["next_number"])
            return f"{seq.prefix}{str(number).zfill(seq.padding)}"


# ---------------------------------------------------------------------------
# Global activity log
# ---------------------------------------------------------------------------
class ActivityLog(models.Model):
    """Who did what and when, across the whole app."""

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="activity")
    action = models.CharField(max_length=30, db_index=True)  # create/update/delete/status/login...
    model = models.CharField(max_length=60, db_index=True)
    object_id = models.CharField(max_length=40, blank=True)
    object_repr = models.CharField(max_length=250, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["model", "object_id"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.user} {self.action} {self.model} {self.object_repr}"


def log_activity(user, action, obj=None, changes=None, note="", model=None, object_id=""):
    """Convenience helper used throughout the app."""
    return ActivityLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action,
        model=model or (obj.__class__.__name__ if obj is not None else ""),
        object_id=str(getattr(obj, "pk", object_id) or object_id),
        object_repr=str(obj)[:250] if obj is not None else "",
        changes=changes or {},
        note=note,
    )
