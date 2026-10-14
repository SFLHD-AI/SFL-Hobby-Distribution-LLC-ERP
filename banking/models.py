"""
Banking: the bridge between your bank/credit-card activity and the books.

Flow
----
1. Transactions arrive — from a **Plaid** connection (automatic), a **CSV**
   statement export (any bank; presets for the big ones), or typed in.
2. Every row is de-duplicated (``external_id`` or a content fingerprint),
   then run through the **category rules** you've taught the system
   ("anything containing PIRATE SHIP → Shipping labels"). Vendors you've
   saved are recognised too.
3. Whatever the rules couldn't settle lands in the **review inbox**. One
   click categorizes it, matches it to a customer payment, a supplier
   receipt or an expense, pairs it with its transfer twin, or writes it
   off as personal / ignored. "Always do this" creates a rule on the spot.
4. Categorized outflows to an expense category become ``Expense`` rows
   automatically, so the expense register is always complete and every
   financial statement (cash *and* accrual) is built from the same data.

Sign convention: ``amount`` is **signed from the account's point of view**.
Positive = money into the account (deposit, refund, card payment received).
Negative = money out (purchase, withdrawal, card charge).
"""
import hashlib
import re
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from core.models import SoftDeleteModel, log_activity


class BankConnection(SoftDeleteModel):
    """One login at one institution (a Plaid *Item*). Several accounts can
    hang off a single connection."""

    PROVIDERS = [("plaid", "Plaid"), ("manual", "Manual / CSV only")]
    STATUS = [("active", "Active"), ("error", "Needs attention"),
              ("relink", "Re-authentication required"), ("disconnected", "Disconnected")]

    provider = models.CharField(max_length=10, choices=PROVIDERS, default="manual")
    institution_name = models.CharField(max_length=150)
    institution_id = models.CharField(max_length=100, blank=True)
    item_id = models.CharField(max_length=120, blank=True, db_index=True)
    access_token = models.CharField(max_length=300, blank=True,
                                    help_text="Provider access token (never shown in the UI)")
    sync_cursor = models.TextField(blank=True, help_text="Provider pagination cursor")
    status = models.CharField(max_length=15, choices=STATUS, default="active")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["institution_name"]

    def __str__(self):
        return f"{self.institution_name} ({self.get_provider_display()})"


class BankAccount(SoftDeleteModel):
    TYPES = [("checking", "Checking"), ("savings", "Savings"),
             ("credit_card", "Credit card"), ("cash", "Cash drawer / petty cash"),
             ("loan", "Loan / line of credit"), ("payment_processor", "Payment processor (Square, PayPal, Stripe)"),
             ("other", "Other")]
    LIABILITY_TYPES = ("credit_card", "loan")

    name = models.CharField(max_length=120, help_text="e.g. Chase Business Checking")
    institution = models.CharField(max_length=150, blank=True)
    account_type = models.CharField(max_length=20, choices=TYPES, default="checking")
    last4 = models.CharField("Last 4 digits", max_length=4, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    connection = models.ForeignKey(BankConnection, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="accounts")
    external_account_id = models.CharField(max_length=120, blank=True, db_index=True,
                                           help_text="Provider account id")
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Balance on the opening date. For credit cards enter the amount OWED as a positive number.")
    opening_date = models.DateField(default=timezone.localdate,
                                    help_text="Transactions before this date are not imported")
    ledger_account = models.OneToOneField("accounting.Account", null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="bank_account")
    is_default = models.BooleanField(default=False,
                                     help_text="Pre-selected as the 'paid from' account for expenses")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name}{' ••••' + self.last4 if self.last4 else ''}"

    def save(self, *args, **kwargs):
        if self.is_default:
            type(self).all_objects.exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)
        self.ensure_ledger_account()

    @property
    def is_liability(self):
        return self.account_type in self.LIABILITY_TYPES

    def ensure_ledger_account(self):
        from accounting.models import Account, AccountSubtype, AccountType
        if self.ledger_account_id:
            acct = self.ledger_account
            if acct.name != str(self):
                acct.name = str(self)
                acct.save(update_fields=["name"])
            return acct
        if self.is_liability:
            type_, subtype, lo, hi = (AccountType.LIABILITY,
                                      AccountSubtype.CREDIT_CARD if self.account_type == "credit_card"
                                      else AccountSubtype.LOAN, 2100, 2200)
        else:
            type_, subtype, lo, hi = AccountType.ASSET, AccountSubtype.BANK, 1010, 1050
            if self.account_type == "cash":
                subtype = AccountSubtype.CASH_ON_HAND
        acct = Account.objects.create(code=Account.next_code(lo, hi), name=str(self),
                                      type=type_, subtype=subtype,
                                      description=f"{self.get_account_type_display()} at {self.institution}".strip())
        type(self).all_objects.filter(pk=self.pk).update(ledger_account=acct)
        self.ledger_account = acct
        return acct

    # --- balances ------------------------------------------------------------
    def balance_as_of(self, day=None):
        """Opening balance + every posted transaction through ``day``.
        Returned in the account's natural sign: positive = money in the bank,
        or for a credit card, positive = amount owed."""
        qs = self.transactions.filter(is_deleted=False, pending=False,
                                      status__in=BankTransaction.COUNTED_STATUSES)
        if day:
            qs = qs.filter(date__lte=day)
        total = qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")
        if self.is_liability:
            # charges are negative amounts -> increase what is owed
            return self.opening_balance - total
        return self.opening_balance + total

    @property
    def current_balance(self):
        return self.balance_as_of()

    @property
    def unreviewed_count(self):
        return self.transactions.filter(is_deleted=False, status=BankTransaction.UNREVIEWED).count()

    @property
    def last_transaction_date(self):
        row = self.transactions.filter(is_deleted=False).order_by("-date").values_list("date", flat=True).first()
        return row


class ImportBatch(models.Model):
    SOURCES = [("csv", "CSV upload"), ("plaid", "Plaid sync"), ("manual", "Manual entry")]

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="imports")
    source = models.CharField(max_length=10, choices=SOURCES, default="csv")
    file = models.FileField(upload_to="bank_imports/%Y/%m/", blank=True, null=True)
    filename = models.CharField(max_length=200, blank=True)
    preset = models.CharField(max_length=40, blank=True, help_text="Column-mapping preset used")
    rows_total = models.PositiveIntegerField(default=0)
    rows_imported = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    rows_skipped = models.PositiveIntegerField(default=0)
    rows_auto_categorized = models.PositiveIntegerField(default=0)
    date_min = models.DateField(null=True, blank=True)
    date_max = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.account} import {self.created_at:%Y-%m-%d %H:%M} ({self.rows_imported} rows)"


def normalize_description(text):
    """Lower-case, collapse whitespace, strip long digit runs (auth codes,
    card suffixes) so the same merchant hashes the same way every month."""
    t = (text or "").lower()
    t = re.sub(r"\d{5,}", "", t)          # authorisation / reference numbers
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class BankTransaction(SoftDeleteModel):
    UNREVIEWED, CATEGORIZED, MATCHED, TRANSFER, IGNORED = (
        "unreviewed", "categorized", "matched", "transfer", "ignored")
    STATUS_CHOICES = [
        (UNREVIEWED, "Needs review"),
        (CATEGORIZED, "Categorized"),
        (MATCHED, "Matched to a document"),
        (TRANSFER, "Transfer between accounts"),
        (IGNORED, "Ignored / not business"),
    ]
    # Statuses whose amounts count toward the account balance (everything
    # but ignored rows, which are duplicates or truly not this business).
    COUNTED_STATUSES = [UNREVIEWED, CATEGORIZED, MATCHED, TRANSFER]
    SOURCES = [("csv", "CSV"), ("plaid", "Plaid"), ("manual", "Manual")]

    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transactions")
    date = models.DateField(db_index=True, help_text="Transaction date")
    posted_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Signed: + into the account, − out of it")
    description = models.CharField(max_length=300)
    original_description = models.CharField(max_length=500, blank=True)
    merchant_name = models.CharField(max_length=200, blank=True)
    check_number = models.CharField(max_length=20, blank=True)
    external_id = models.CharField(max_length=120, blank=True, db_index=True,
                                   help_text="Provider transaction id (Plaid)")
    fingerprint = models.CharField(max_length=40, blank=True, db_index=True,
                                   help_text="sha1(account|date|amount|normalized description) for de-duplication")
    pending = models.BooleanField(default=False)
    source = models.CharField(max_length=10, choices=SOURCES, default="csv")
    import_batch = models.ForeignKey(ImportBatch, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="transactions")

    # --- review outcome -----------------------------------------------------
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=UNREVIEWED, db_index=True)
    category_account = models.ForeignKey("accounting.Account", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="bank_transactions",
                                         help_text="What this money was for")
    vendor = models.ForeignKey("expenses.Vendor", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="bank_transactions")
    expense = models.OneToOneField("expenses.Expense", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="bank_txn",
                                   help_text="Expense register row created from / matched to this transaction")
    payments = models.ManyToManyField("payments.Payment", blank=True, related_name="deposits",
                                      help_text="Customer payments included in this deposit")
    receipts = models.ManyToManyField("inventory.Receipt", blank=True, related_name="supplier_payments",
                                      help_text="Supplier receipts (bills) this payment covers")
    transfer_peer = models.OneToOneField("self", null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="transfer_twin",
                                         help_text="The matching transaction in the other account")
    rule_applied = models.ForeignKey("banking.CategoryRule", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="transactions")
    auto_categorized = models.BooleanField(default=False)
    suggested_account = models.ForeignKey("accounting.Account", null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="+",
                                          help_text="Rule/vendor suggestion awaiting confirmation")
    notes = models.CharField(max_length=500, blank=True)
    receipt_file = models.FileField(upload_to="bank_receipts/%Y/%m/", blank=True, null=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["account", "date"]),
                   models.Index(fields=["status", "date"])]
        constraints = [
            models.UniqueConstraint(fields=["account", "external_id"],
                                    condition=models.Q(external_id__gt=""),
                                    name="uniq_bank_txn_external_id"),
        ]

    def __str__(self):
        return f"{self.date} {self.description[:40]} {self.amount:+.2f} ({self.account.name})"

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def make_fingerprint(account_id, date, amount, description):
        raw = f"{account_id}|{date}|{Decimal(amount):.2f}|{normalize_description(description)}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def save(self, *args, **kwargs):
        if not self.fingerprint:
            self.fingerprint = self.make_fingerprint(self.account_id, self.date,
                                                     self.amount, self.description)
        if not self.original_description:
            self.original_description = self.description
        super().save(*args, **kwargs)

    @property
    def is_outflow(self):
        return self.amount < 0

    @property
    def abs_amount(self):
        return abs(self.amount)

    @property
    def needs_review(self):
        return self.status == self.UNREVIEWED

    @property
    def expense_category(self):
        from expenses.models import ExpenseCategory
        if self.category_account_id:
            return ExpenseCategory.for_account(self.category_account)
        return None

    def _stamp_review(self, user, status):
        self.status = status
        self.reviewed_by = user if getattr(user, "is_authenticated", False) else None
        self.reviewed_at = timezone.now()

    # --- link bookkeeping -------------------------------------------------------
    def _release_documents(self, user=None, keep_expense=False):
        """Undo every side effect of a previous review decision so the next
        one starts clean. Paid-by-hand fields that THIS transaction wrote onto
        a receipt or expense are cleared (otherwise the ledger would book the
        payment twice: once from the bank row, once from the document)."""
        # supplier bills
        for r in self.receipts.all():
            if r.paid_date == self.date and r.paid_from_id == self.account_id:
                r.paid_date = None
                r.paid_from = None
                r.save(update_fields=["paid_date", "paid_from"])
        self.receipts.clear()
        # customer payments
        self.payments.clear()
        # transfer twin
        peer = self.transfer_peer
        if peer is not None:
            self.transfer_peer = None
            if peer.transfer_peer_id == self.pk:
                peer.transfer_peer = None
                peer.category_account = None
                peer.status = self.UNREVIEWED
                peer.save(update_fields=["transfer_peer", "category_account", "status"])
        # split parts (and the expense rows they created)
        for sp in self.splits.select_related("expense"):
            if sp.expense_id and not sp.expense.is_deleted and sp.expense.source == "bank":
                sp.expense.delete(user=user)
            sp.delete()
        # expense register row
        exp = self.expense if self.expense_id else None
        if exp is not None and not keep_expense:
            if exp.source == "bank" and not exp.is_deleted:
                exp.delete(user=user)          # auto-created from this row: retire it
            elif exp.paid_date == self.date and exp.paid_from_id == self.account_id:
                exp.paid_date = None           # hand-entered bill matched here: un-pay it
                exp.paid_from = None
                if exp.payment_method in ("card", "ach"):
                    exp.payment_method = ""
                exp.save(update_fields=["paid_date", "paid_from", "payment_method", "updated_at"])
            self.expense = None

    def delete(self, using=None, keep_parents=False, user=None):
        """Soft-delete a transaction and undo everything it did to other documents."""
        with transaction.atomic():
            self._release_documents(user=user)
            self.save()
            super().delete(user=user)

    def split(self, parts, user=None, note=None):
        """Divide this transaction across several categories.

        ``parts`` = [(Account, Decimal amount, note), ...] whose amounts sum
        to |amount|. Expense-category parts become Expense rows (source
        "bank") linked through BankTransactionSplit so the register stays
        complete and nothing is counted twice."""
        from accounting.models import AccountType
        from expenses.models import Expense, ExpenseCategory
        parts = [(a, Decimal(amt), (n or "")) for a, amt, n in parts if Decimal(amt) != 0]
        if len(parts) < 2:
            raise ValueError("A split needs at least two parts.")
        if any(amt <= 0 for _, amt, _ in parts):
            raise ValueError("Each part must be a positive amount.")
        if sum((amt for _, amt, _ in parts), Decimal("0")) != abs(self.amount):
            raise ValueError(f"Parts must add up to ${abs(self.amount):,.2f}.")
        sign = -1 if self.amount < 0 else 1
        with transaction.atomic():
            self._release_documents(user=user)
            self.category_account = None
            if note is not None:
                self.notes = note
            self.suggested_account = None
            self._stamp_review(user, self.CATEGORIZED)
            self.save()
            for account, amt, part_note in parts:
                sp = BankTransactionSplit.objects.create(
                    transaction=self, account=account, amount=amt, note=part_note[:200])
                category = (ExpenseCategory.for_account(account)
                            if account.type == AccountType.EXPENSE else None)
                if category is not None:
                    sp.expense = Expense.objects.create(
                        date=self.date, category=category, vendor_link=self.vendor,
                        vendor=self.vendor.name if self.vendor_id else (self.merchant_name or self.description[:200]),
                        amount=amt * (1 if sign < 0 else -1),   # outflow -> expense, inflow -> refund
                        note=part_note or f"Split of {self.description[:120]}",
                        paid_date=self.date, paid_from=self.account,
                        payment_method="card" if self.account.account_type == "credit_card" else "ach",
                        source="bank", is_reviewed=True,
                        created_by=user if getattr(user, "is_authenticated", False) else None)
                    sp.save(update_fields=["expense"])
            log_activity(user, "bank_txn_split", self,
                         note="; ".join(f"{a.name} ${amt}" for a, amt, _ in parts))
        return self

    @property
    def is_split(self):
        return self.splits.exists()

    # --- review actions (the only way status should change) -----------------
    def categorize(self, account, user=None, vendor=None, note=None, create_expense=True,
                   rule=None, auto=False):
        """Assign a ledger account. For expense-category accounts an
        ``Expense`` row is created (or updated) so the register stays complete."""
        from accounting.models import AccountType
        from expenses.models import Expense, ExpenseCategory
        with transaction.atomic():
            category = ExpenseCategory.for_account(account) if account.type == AccountType.EXPENSE else None
            # Keep an existing expense row only when the new account is still an
            # expense category (its category is updated below); otherwise release it.
            self._release_documents(user=user, keep_expense=bool(category is not None and self.expense_id))
            self.category_account = account
            if vendor is not None:
                self.vendor = vendor
            if note is not None:
                self.notes = note
            self.rule_applied = rule
            self.auto_categorized = auto
            self.suggested_account = None
            self._stamp_review(user, self.CATEGORIZED)
            if category is not None and create_expense:
                if self.expense_id and not self.expense.is_deleted:
                    exp = self.expense
                    exp.category = category
                    exp.amount = -self.amount
                    exp.date = self.date
                    exp.paid_date = self.date
                    exp.paid_from = self.account
                    if self.vendor_id:
                        exp.vendor_link = self.vendor
                        exp.vendor = self.vendor.name
                    exp.save()
                else:
                    exp = Expense.objects.create(
                        date=self.date, category=category,
                        vendor_link=self.vendor,
                        vendor=self.vendor.name if self.vendor_id else (self.merchant_name or self.description[:200]),
                        amount=-self.amount,     # outflow -> positive expense; refund -> negative
                        note=self.notes or "", paid_date=self.date, paid_from=self.account,
                        payment_method="card" if self.account.account_type == "credit_card" else "ach",
                        source="bank", is_reviewed=not auto,
                        created_by=user if getattr(user, "is_authenticated", False) else None)
                    self.expense = exp
            self.save()
            if rule is not None:
                type(rule).objects.filter(pk=rule.pk).update(
                    times_applied=models.F("times_applied") + 1, last_applied_at=timezone.now())
            if not auto:
                log_activity(user, "bank_txn_categorized", self, note=str(account))
        return self

    def link_expense(self, expense, user=None):
        """Match to an expense that was entered by hand before the card
        charge showed up. Marks the expense paid from this account."""
        with transaction.atomic():
            self._release_documents(user=user)
            self.expense = expense
            self.category_account = expense.category.ensure_account() if expense.category_id else None
            self.vendor = expense.vendor_link
            self._stamp_review(user, self.MATCHED)
            self.save()
            expense.paid_date = self.date
            expense.paid_from = self.account
            if not expense.payment_method or expense.payment_method == "personal":
                expense.payment_method = "card" if self.account.account_type == "credit_card" else "ach"
            expense.save()
            log_activity(user, "bank_txn_matched_expense", self, note=str(expense))
        return self

    def link_payments(self, payments, user=None):
        """A deposit = one or more customer payments."""
        from accounting.models import Account
        with transaction.atomic():
            self._release_documents(user=user)
            self.payments.set(list(payments))
            self.category_account = Account.system("undeposited")
            self._stamp_review(user, self.MATCHED)
            self.save()
            log_activity(user, "bank_txn_matched_payments", self,
                         note=", ".join(str(p.pk) for p in payments))
        return self

    def link_receipts(self, receipts, user=None):
        """An outflow that pays one or more posted supplier receipts (bills)."""
        from accounting.models import Account
        with transaction.atomic():
            self._release_documents(user=user)
            receipts = list(receipts)
            self.receipts.set(receipts)
            self.category_account = Account.system("inventory_purchases")
            self._stamp_review(user, self.MATCHED)
            self.save()
            for r in receipts:
                if r.paid_date is None:
                    r.paid_date = self.date
                    r.paid_from = self.account
                    r.save(update_fields=["paid_date", "paid_from"])
            log_activity(user, "bank_txn_matched_receipts", self,
                         note=", ".join(f"RCV-{r.pk}" for r in receipts))
        return self

    def mark_transfer(self, peer, user=None):
        """Pair with the opposite-sign transaction in another account."""
        if peer.account_id == self.account_id:
            raise ValueError("A transfer needs two different accounts.")
        if (peer.amount > 0) == (self.amount > 0):
            raise ValueError("Transfer sides must have opposite signs.")
        with transaction.atomic():
            self._release_documents(user=user)
            peer._release_documents(user=user)
            for side, other in ((self, peer), (peer, self)):
                side.transfer_peer = other
                side.category_account = other.account.ensure_ledger_account()
                side._stamp_review(user, self.TRANSFER)
                side.save()
            log_activity(user, "bank_txn_transfer", self, note=str(peer))
        return self

    def mark_ignored(self, user=None, note=""):
        with transaction.atomic():
            self._release_documents(user=user)
            self.category_account = None
            if note:
                self.notes = note
            self._stamp_review(user, self.IGNORED)
            self.save()
            log_activity(user, "bank_txn_ignored", self, note=note)
        return self

    def reset_review(self, user=None):
        """Back to the inbox."""
        with transaction.atomic():
            self._release_documents(user=user)
            self.category_account = None
            self.rule_applied = None
            self.auto_categorized = False
            self.status = self.UNREVIEWED
            self.reviewed_by = None
            self.reviewed_at = None
            self.save()
        return self


class BankTransactionSplit(models.Model):
    """One slice of a transaction that was split across categories."""

    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="splits")
    account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="splits")
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Positive share of the transaction")
    note = models.CharField(max_length=200, blank=True)
    expense = models.OneToOneField("expenses.Expense", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="bank_split")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.transaction_id}: {self.account} ${self.amount}"


class CategoryRule(SoftDeleteModel):
    """'When a transaction looks like X, it is Y.' Applied to every new
    transaction (import or sync) and on demand to the inbox."""

    FIELDS = [("description", "Description"), ("merchant", "Merchant name"), ("any", "Either")]
    MATCH = [("contains", "Contains"), ("starts_with", "Starts with"),
             ("exact", "Is exactly"), ("regex", "Matches regex")]
    DIRECTION = [("any", "Money in or out"), ("out", "Money out only"), ("in", "Money in only")]
    ACTIONS = [("categorize", "Categorize"), ("ignore", "Ignore (not business)"),
               ("transfer", "Flag as transfer (find twin)")]

    name = models.CharField(max_length=150)
    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower runs first")
    active = models.BooleanField(default=True)
    match_field = models.CharField(max_length=12, choices=FIELDS, default="any")
    match_type = models.CharField(max_length=12, choices=MATCH, default="contains")
    pattern = models.CharField(max_length=200)
    direction = models.CharField(max_length=4, choices=DIRECTION, default="any")
    account_filter = models.ForeignKey(BankAccount, null=True, blank=True, on_delete=models.SET_NULL,
                                       help_text="Only apply to this bank account")
    amount_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    action = models.CharField(max_length=12, choices=ACTIONS, default="categorize")
    category_account = models.ForeignKey("accounting.Account", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="rules")
    vendor = models.ForeignKey("expenses.Vendor", null=True, blank=True, on_delete=models.SET_NULL)
    auto_confirm = models.BooleanField(
        default=True, help_text="Apply automatically. Off = only suggest, a person confirms.")
    times_applied = models.PositiveIntegerField(default=0)
    last_applied_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "name"]

    def __str__(self):
        return self.name

    def matches(self, txn):
        if not self.active:
            return False
        if self.account_filter_id and txn.account_id != self.account_filter_id:
            return False
        if self.direction == "out" and txn.amount >= 0:
            return False
        if self.direction == "in" and txn.amount <= 0:
            return False
        amt = abs(txn.amount)
        if self.amount_min is not None and amt < self.amount_min:
            return False
        if self.amount_max is not None and amt > self.amount_max:
            return False
        fields = []
        if self.match_field in ("description", "any"):
            fields += [txn.description, txn.original_description]
        if self.match_field in ("merchant", "any"):
            fields.append(txn.merchant_name)
        pat = self.pattern.strip()
        for text in fields:
            hay = (text or "").strip()
            if not hay:
                continue
            h, p = hay.lower(), pat.lower()
            if self.match_type == "contains" and p in h:
                return True
            if self.match_type == "starts_with" and h.startswith(p):
                return True
            if self.match_type == "exact" and h == p:
                return True
            if self.match_type == "regex":
                from banking.regex_guard import compile_safe
                rx = compile_safe(pat)
                if rx is None:          # invalid or unsafe pattern: never matches
                    return False
                if rx.search(hay):
                    return True
        return False
