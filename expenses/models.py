"""
Expense tracking — the register of everything the business spends outside
of inventory purchases.

Design goals (see docs/ACCOUNTING.md):

* **Never backtrack at year end.** Expenses arrive continuously: from the
  bank/card feed (``banking``), from quick mobile capture with a receipt
  photo, from recurring templates, or typed in. Each one is categorized
  the moment it's seen and carries its own receipt.
* **Cash *and* accrual aware.** ``date`` is when the expense was incurred
  (bill date); ``paid_date`` is when cash left. Cash-basis statements use
  ``paid_date``; accrual statements use ``date`` and show the unpaid part
  as accounts payable.
* **One category = one ledger account.** Every ``ExpenseCategory`` owns an
  ``accounting.Account`` of type *expense*, so the P&L, the bank review
  dropdown and the expense register all speak the same language.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import SoftDeleteModel

# IRS Schedule C line hints — purely informational, used by the year-end package.
SCHEDULE_C_LINES = [
    ("", "— not mapped —"),
    ("8", "8 · Advertising"),
    ("9", "9 · Car and truck expenses"),
    ("10", "10 · Commissions and fees"),
    ("11", "11 · Contract labor"),
    ("13", "13 · Depreciation"),
    ("15", "15 · Insurance (other than health)"),
    ("16b", "16b · Interest (other)"),
    ("17", "17 · Legal and professional services"),
    ("18", "18 · Office expense"),
    ("20b", "20b · Rent — other business property"),
    ("21", "21 · Repairs and maintenance"),
    ("22", "22 · Supplies"),
    ("23", "23 · Taxes and licenses"),
    ("24a", "24a · Travel"),
    ("24b", "24b · Deductible meals"),
    ("25", "25 · Utilities"),
    ("26", "26 · Wages"),
    ("27a", "27a · Other expenses"),
    ("cogs", "Part III · Cost of goods sold"),
]


class ExpenseCategory(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=250, blank=True)
    account = models.OneToOneField("accounting.Account", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="expense_category",
                                   help_text="Ledger account this category posts to (auto-created)")
    tax_line = models.CharField(max_length=5, choices=SCHEDULE_C_LINES, blank=True,
                                help_text="Schedule C line, for the year-end package")
    deductible_pct = models.PositiveSmallIntegerField(
        default=100, help_text="Percent deductible for tax (meals are typically 50)")
    monthly_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                         help_text="Optional default monthly budget")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.account_id is None:
            self.ensure_account()
        elif self.account.name != self.name:
            self.account.name = self.name
            self.account.save(update_fields=["name"])

    def ensure_account(self):
        """Create (once) the ledger account backing this category."""
        from accounting.models import Account, AccountSubtype, AccountType
        if self.account_id:
            return self.account
        account = Account.objects.create(
            code=Account.next_code(6000, 6900), name=self.name,
            type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE,
            description=self.description[:300])
        type(self).all_objects.filter(pk=self.pk).update(account=account)
        self.account = account
        return account

    @classmethod
    def for_account(cls, account):
        """Reverse lookup used by the bank review screen."""
        return cls.all_objects.filter(account=account).first()


class Vendor(SoftDeleteModel):
    """Who you pay. Lets the bank feed recognise merchants, pre-fills the
    category, and powers the 1099 vendor list at year end."""

    name = models.CharField(max_length=200, db_index=True)
    default_category = models.ForeignKey(ExpenseCategory, null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="vendors")
    aliases = models.TextField(
        blank=True,
        help_text="Other ways this vendor appears on bank statements, one per line "
                  "(e.g. 'AMZN Mktp', 'AMAZON.COM')")
    is_1099 = models.BooleanField("1099 contractor", default=False,
                                  help_text="Independent contractor — totals ≥ $600/yr need a 1099-NEC")
    tax_id_last4 = models.CharField("Tax ID (last 4)", max_length=4, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.CharField(max_length=200, blank=True)
    account_number = models.CharField(max_length=100, blank=True,
                                      help_text="Your customer/account # with this vendor")
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def alias_list(self):
        return [a.strip() for a in self.aliases.splitlines() if a.strip()]

    def matches(self, text):
        """True when ``text`` (a bank description) looks like this vendor."""
        hay = (text or "").lower()
        if not hay:
            return False
        if self.name.lower() in hay:
            return True
        return any(a.lower() in hay for a in self.alias_list)

    @classmethod
    def find_for_description(cls, text):
        for v in cls.objects.filter(active=True):
            if v.matches(text):
                return v
        return None

    def total_paid(self, start=None, end=None):
        qs = self.expenses.filter(is_deleted=False)
        if start:
            qs = qs.filter(date__gte=start)
        if end:
            qs = qs.filter(date__lte=end)
        return qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")


class Expense(SoftDeleteModel):
    METHODS = [
        ("card", "Credit / debit card"), ("ach", "ACH / bank transfer"),
        ("check", "Check"), ("cash", "Cash"), ("personal", "Paid personally (owner)"),
        ("other", "Other"),
    ]
    SOURCES = [("manual", "Entered by hand"), ("bank", "From bank feed"),
               ("recurring", "Recurring template"), ("import", "CSV import"),
               ("quick", "Quick capture")]

    date = models.DateField(default=timezone.localdate, db_index=True,
                            help_text="Date incurred (bill / receipt date)")
    category = models.ForeignKey(ExpenseCategory, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="expenses")
    vendor = models.CharField(max_length=200, blank=True,
                              help_text="Free-text vendor name (kept in sync with the vendor record)")
    vendor_link = models.ForeignKey(Vendor, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="expenses")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Negative = refund / credit from the vendor")
    note = models.CharField(max_length=500, blank=True)
    reference = models.CharField(max_length=100, blank=True,
                                 help_text="Vendor invoice / receipt #")
    receipt_file = models.FileField(upload_to="expense_receipts/%Y/%m/", blank=True, null=True)

    # Payment tracking (cash vs accrual)
    due_date = models.DateField(null=True, blank=True)
    paid_date = models.DateField(null=True, blank=True, db_index=True,
                                 help_text="Blank = unpaid bill")
    paid_from = models.ForeignKey("banking.BankAccount", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="expenses",
                                  help_text="Account the money left from. Blank + 'paid personally' "
                                            "counts as an owner contribution.")
    payment_method = models.CharField(max_length=10, choices=METHODS, blank=True)

    source = models.CharField(max_length=10, choices=SOURCES, default="manual")
    recurring = models.ForeignKey("expenses.RecurringExpense", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="generated")
    is_reviewed = models.BooleanField(default=True,
                                      help_text="False for auto-created rows awaiting a human look")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} {self.vendor} ${self.amount}"

    def save(self, *args, **kwargs):
        if self.vendor_link_id and not self.vendor:
            self.vendor = self.vendor_link.name
        if self.paid_from_id and self.payment_method == "personal":
            self.payment_method = "other"
        super().save(*args, **kwargs)

    # --- state ---------------------------------------------------------------
    @property
    def bank_transaction(self):
        """The bank/card transaction this expense was created from (if any)."""
        return getattr(self, "bank_txn", None)

    @property
    def is_paid(self):
        return self.paid_date is not None or self.bank_transaction is not None

    @property
    def effective_paid_date(self):
        txn = self.bank_transaction
        if txn is not None:
            return txn.date
        return self.paid_date

    @property
    def is_overdue(self):
        return (not self.is_paid and self.due_date is not None
                and self.due_date < timezone.localdate())

    @property
    def has_receipt(self):
        return bool(self.receipt_file)

    @property
    def paid_personally(self):
        return self.is_paid and self.paid_from_id is None and self.bank_transaction is None

    def mark_paid(self, paid_date=None, paid_from=None, method=""):
        self.paid_date = paid_date or timezone.localdate()
        if paid_from is not None:
            self.paid_from = paid_from
        if method:
            self.payment_method = method
        self.save(update_fields=["paid_date", "paid_from", "payment_method", "updated_at"])


class RecurringExpense(SoftDeleteModel):
    """Template that spawns an Expense on a schedule (rent, software, insurance)."""

    FREQUENCIES = [("weekly", "Weekly"), ("biweekly", "Every 2 weeks"),
                   ("monthly", "Monthly"), ("quarterly", "Quarterly"), ("yearly", "Yearly")]

    name = models.CharField(max_length=150)
    vendor_link = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    category = models.ForeignKey(ExpenseCategory, null=True, blank=True, on_delete=models.SET_NULL)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default="monthly")
    next_date = models.DateField(help_text="Next occurrence to create")
    end_date = models.DateField(null=True, blank=True)
    paid_from = models.ForeignKey("banking.BankAccount", null=True, blank=True,
                                  on_delete=models.SET_NULL)
    auto_mark_paid = models.BooleanField(
        default=False, help_text="Create as already paid (auto-pay) instead of an open bill")
    note = models.CharField(max_length=300, blank=True)
    active = models.BooleanField(default=True)
    last_generated = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_date", "name"]

    def __str__(self):
        return f"{self.name} ${self.amount} {self.get_frequency_display()}"

    def advance(self, from_date):
        from datetime import timedelta
        if self.frequency == "weekly":
            return from_date + timedelta(days=7)
        if self.frequency == "biweekly":
            return from_date + timedelta(days=14)
        month_step = {"monthly": 1, "quarterly": 3, "yearly": 12}[self.frequency]
        y, m = from_date.year, from_date.month + month_step
        while m > 12:
            m -= 12
            y += 1
        import calendar
        day = min(from_date.day, calendar.monthrange(y, m)[1])
        return from_date.replace(year=y, month=m, day=day)

    def generate_due(self, today=None, user=None):
        """Create every Expense whose date is on/before ``today``. Returns list."""
        today = today or timezone.localdate()
        created = []
        while self.active and self.next_date <= today and \
                (self.end_date is None or self.next_date <= self.end_date):
            exp = Expense.objects.create(
                date=self.next_date, category=self.category,
                vendor_link=self.vendor_link,
                vendor=self.vendor_link.name if self.vendor_link else self.name,
                amount=self.amount, note=self.note or f"Recurring: {self.name}",
                paid_date=self.next_date if self.auto_mark_paid else None,
                paid_from=self.paid_from if self.auto_mark_paid else None,
                payment_method=("ach" if (self.auto_mark_paid and self.paid_from_id) else ""),
                source="recurring", recurring=self, is_reviewed=False,
                created_by=user if getattr(user, "is_authenticated", False) else None)
            created.append(exp)
            self.last_generated = self.next_date
            self.next_date = self.advance(self.next_date)
        if created:
            self.save(update_fields=["next_date", "last_generated"])
        return created


class Budget(models.Model):
    """Planned spend per category per month (month null = whole year)."""

    category = models.ForeignKey(ExpenseCategory, on_delete=models.CASCADE, related_name="budgets")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["category", "year", "month"],
                                               name="uniq_budget_cat_period")]
        ordering = ["year", "month", "category__name"]

    def __str__(self):
        when = f"{self.year}-{self.month:02d}" if self.month else str(self.year)
        return f"{self.category} {when}: ${self.amount}"


class MileageEntry(SoftDeleteModel):
    """Business miles driven (deliveries, shows, supplier pickups). Deducted
    at the IRS standard rate in the year-end package; not a cash movement."""

    date = models.DateField(default=timezone.localdate, db_index=True)
    miles = models.DecimalField(max_digits=8, decimal_places=1)
    purpose = models.CharField(max_length=250)
    from_location = models.CharField(max_length=200, blank=True)
    to_location = models.CharField(max_length=200, blank=True)
    round_trip = models.BooleanField(default=False)
    rate = models.DecimalField(max_digits=5, decimal_places=3,
                               help_text="$/mile snapshotted from Settings when entered")
    vehicle = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name_plural = "mileage entries"

    def __str__(self):
        return f"{self.date} {self.total_miles} mi — {self.purpose}"

    @property
    def total_miles(self):
        return self.miles * 2 if self.round_trip else self.miles

    @property
    def deduction(self):
        return (self.total_miles * self.rate).quantize(Decimal("0.01"))
