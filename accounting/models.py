"""
Chart of accounts for SFL ERP System.

The ledger itself is *virtual*: ``accounting.ledger`` rebuilds balanced
double-entry journal lines on demand from the operational documents that
already exist (invoices, payments, receipts, bank transactions, expenses,
stock movements). Nothing is posted and nothing can drift out of sync —
edit or void a document and every financial statement reflects it.

This module only defines **where** money lands: the chart of accounts.
Most rows are created automatically:

* one *expense* account per ``ExpenseCategory``,
* one *bank* / *credit card* account per ``BankAccount``,
* a fixed set of **system accounts** (AR, AP, inventory, sales, sales tax
  payable, owner equity …) identified by ``system_key`` and created lazily
  by ``Account.system(key)`` or all at once by ``setup_accounts``.
"""
from decimal import Decimal

from django.db import models

CASH, ACCRUAL = "cash", "accrual"
BASIS_CHOICES = [(CASH, "Cash basis"), (ACCRUAL, "Accrual basis")]


class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    COGS = "cogs", "Cost of goods sold"
    EXPENSE = "expense", "Expense"


# Debit-balance account types (increase with a debit)
DEBIT_TYPES = {AccountType.ASSET, AccountType.COGS, AccountType.EXPENSE}
# Account types that roll into the income statement
PNL_TYPES = {AccountType.INCOME, AccountType.COGS, AccountType.EXPENSE}


class AccountSubtype(models.TextChoices):
    # assets
    BANK = "bank", "Bank account"
    CASH_ON_HAND = "cash_on_hand", "Cash on hand"
    UNDEPOSITED = "undeposited", "Undeposited funds"
    AR = "ar", "Accounts receivable"
    INVENTORY = "inventory", "Inventory"
    SUPPLIER_DEPOSITS = "supplier_deposits", "Supplier deposits / prepayments"
    OTHER_ASSET = "other_asset", "Other asset"
    FIXED_ASSET = "fixed_asset", "Fixed asset / equipment"
    # liabilities
    CREDIT_CARD = "credit_card", "Credit card"
    AP = "ap", "Accounts payable — suppliers"
    AP_CARRIERS = "ap_carriers", "Accounts payable — carriers"
    SALES_TAX = "sales_tax", "Sales tax payable"
    CUSTOMER_DEPOSITS = "customer_deposits", "Customer deposits / unapplied payments"
    LOAN = "loan", "Loan / line of credit"
    OTHER_LIABILITY = "other_liability", "Other liability"
    # equity
    OPENING_BALANCE = "opening_balance", "Opening balance equity"
    OWNER_CONTRIBUTION = "owner_contribution", "Owner contributions"
    OWNER_DRAW = "owner_draw", "Owner draws / personal"
    RETAINED = "retained", "Retained earnings"
    # income
    SALES = "sales", "Sales"
    SHIPPING_INCOME = "shipping_income", "Shipping charged to customers"
    SALES_RETURNS = "sales_returns", "Sales returns & allowances"
    OTHER_INCOME = "other_income", "Other income"
    UNCAT_INCOME = "uncat_income", "Uncategorized income"
    # cost of goods sold
    COGS = "cogs", "Cost of goods sold"
    INVENTORY_PURCHASES = "inventory_purchases", "Inventory purchases (supplier payments)"
    INVENTORY_ADJ = "inventory_adj", "Inventory adjustments & shrinkage"
    SHIPPING_EXPENSE = "shipping_expense", "Shipping labels & freight"
    # expenses
    EXPENSE = "expense", "Operating expense"
    UNCAT_EXPENSE = "uncat_expense", "Uncategorized expense"


# System accounts: key -> (code, name, type, subtype, description)
SYSTEM_ACCOUNTS = {
    "cash_on_hand": ("1000", "Cash on hand", AccountType.ASSET, AccountSubtype.CASH_ON_HAND,
                     "Petty cash and cash sales not deposited yet."),
    "undeposited": ("1050", "Payments received, not yet deposited", AccountType.ASSET, AccountSubtype.UNDEPOSITED,
                    "Customer payments recorded but not yet matched to a bank deposit."),
    "ar": ("1200", "Accounts receivable", AccountType.ASSET, AccountSubtype.AR,
           "Open customer invoices (accrual basis only)."),
    "inventory": ("1300", "Inventory", AccountType.ASSET, AccountSubtype.INVENTORY,
                  "Stock on hand at weighted-average landed cost (accrual basis only)."),
    "supplier_deposits": ("1400", "Supplier deposits & prepayments", AccountType.ASSET,
                          AccountSubtype.SUPPLIER_DEPOSITS,
                          "Money paid to suppliers for product not yet received (allocations, pre-orders)."),
    "fixed_assets": ("1500", "Equipment & fixed assets", AccountType.ASSET, AccountSubtype.FIXED_ASSET,
                     "Larger purchases you depreciate rather than expense."),
    "ap": ("2000", "Accounts payable — suppliers", AccountType.LIABILITY, AccountSubtype.AP,
           "Posted supplier receipts not yet paid (accrual basis only)."),
    "ap_carriers": ("2010", "Accounts payable — carriers", AccountType.LIABILITY, AccountSubtype.AP_CARRIERS,
                    "Shipping label / freight costs recorded on shipments but not yet paid."),
    "sales_tax": ("2200", "Sales tax payable", AccountType.LIABILITY, AccountSubtype.SALES_TAX,
                  "Sales tax collected from customers and not yet remitted to the state."),
    "customer_deposits": ("2300", "Customer payments not yet applied", AccountType.LIABILITY,
                          AccountSubtype.CUSTOMER_DEPOSITS,
                          "Payments received that are not applied to an invoice yet."),
    "loans": ("2500", "Loans & lines of credit", AccountType.LIABILITY, AccountSubtype.LOAN,
              "Loan proceeds and principal repayments."),
    "opening_balance": ("3000", "Starting balances (opening balance equity)", AccountType.EQUITY, AccountSubtype.OPENING_BALANCE,
                        "Offset for opening balances brought in from before this system."),
    "owner_contribution": ("3100", "Owner contributions", AccountType.EQUITY, AccountSubtype.OWNER_CONTRIBUTION,
                           "Money you put into the business, including expenses paid personally."),
    "owner_draw": ("3200", "Owner draws / personal", AccountType.EQUITY, AccountSubtype.OWNER_DRAW,
                   "Money taken out of the business and personal purchases on business accounts."),
    "retained": ("3900", "Retained earnings", AccountType.EQUITY, AccountSubtype.RETAINED,
                 "Cumulative profit from prior fiscal years (computed, never posted to)."),
    "sales": ("4000", "Sales", AccountType.INCOME, AccountSubtype.SALES,
              "Merchandise revenue."),
    "shipping_income": ("4100", "Shipping charged to customers", AccountType.INCOME,
                        AccountSubtype.SHIPPING_INCOME, "Shipping & handling billed on invoices."),
    "sales_returns": ("4200", "Sales returns & allowances", AccountType.INCOME, AccountSubtype.SALES_RETURNS,
                      "Credit memos issued to customers (contra-revenue)."),
    "other_income": ("4900", "Other income", AccountType.INCOME, AccountSubtype.OTHER_INCOME,
                     "Interest, rebates, cash-back, one-off income."),
    "uncat_income": ("4950", "Uncategorized income", AccountType.INCOME, AccountSubtype.UNCAT_INCOME,
                     "Deposits that have not been reviewed yet."),
    "cogs": ("5000", "Cost of goods sold", AccountType.COGS, AccountSubtype.COGS,
             "Landed cost of product sold (from the stock ledger, accrual basis)."),
    "inventory_purchases": ("5100", "Inventory purchases", AccountType.COGS, AccountSubtype.INVENTORY_PURCHASES,
                            "Supplier payments for product (cash basis: expensed when paid)."),
    "inventory_adj": ("5200", "Inventory adjustments & shrinkage", AccountType.COGS, AccountSubtype.INVENTORY_ADJ,
                      "Damaged, lost, sample, cycle-count and revaluation differences."),
    "shipping_expense": ("5300", "Shipping labels & freight", AccountType.COGS, AccountSubtype.SHIPPING_EXPENSE,
                         "Postage, labels and freight you pay carriers."),
    "uncat_expense": ("6900", "Uncategorized expense", AccountType.EXPENSE, AccountSubtype.UNCAT_EXPENSE,
                      "Outflows that have not been reviewed yet."),
}

# Accounts a bank transaction may be categorized to directly (besides the
# expense categories). Order = how they appear in the review dropdown.
BANK_CATEGORY_KEYS = [
    "sales", "other_income", "inventory_purchases", "shipping_expense",
    "sales_tax", "owner_draw", "owner_contribution", "loans",
    "fixed_assets", "supplier_deposits", "cash_on_hand",
]


class Account(models.Model):
    """One row of the chart of accounts."""

    code = models.CharField(max_length=10, db_index=True,
                            help_text="Short sort code, e.g. 6010")
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=10, choices=AccountType.choices, db_index=True)
    subtype = models.CharField(max_length=25, choices=AccountSubtype.choices,
                               default=AccountSubtype.EXPENSE, db_index=True)
    description = models.CharField(max_length=300, blank=True)
    system_key = models.CharField(max_length=40, unique=True, null=True, blank=True,
                                  help_text="Set for accounts the ledger engine relies on")
    is_system = models.BooleanField(default=False, editable=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "name"]

    def __str__(self):
        return f"{self.code} {self.name}"

    # --- behaviour -------------------------------------------------------
    @property
    def is_debit_normal(self):
        return self.type in DEBIT_TYPES

    @property
    def is_pnl(self):
        return self.type in PNL_TYPES

    @property
    def is_bank_like(self):
        return self.subtype in (AccountSubtype.BANK, AccountSubtype.CREDIT_CARD,
                                AccountSubtype.CASH_ON_HAND)

    def signed_balance(self, debit_minus_credit):
        """Turn a raw (debits − credits) figure into the natural-sign balance
        for this account: positive for an asset with money in it, positive
        for a liability you owe, positive for income earned."""
        value = Decimal(debit_minus_credit)
        return value if self.is_debit_normal else -value

    # --- factories ---------------------------------------------------------
    @classmethod
    def system(cls, key):
        """Return the system account for ``key``, creating it if needed."""
        try:
            return cls.objects.get(system_key=key)
        except cls.DoesNotExist:
            code, name, type_, subtype, desc = SYSTEM_ACCOUNTS[key]
            obj, _ = cls.objects.get_or_create(
                system_key=key,
                defaults={"code": code, "name": name, "type": type_,
                          "subtype": subtype, "description": desc, "is_system": True})
            return obj

    @classmethod
    def ensure_system_accounts(cls):
        """Create every system account (idempotent). Used by setup_accounts."""
        return {key: cls.system(key) for key in SYSTEM_ACCOUNTS}

    @classmethod
    def next_code(cls, prefix_start, prefix_end):
        """Smallest unused numeric code in [prefix_start, prefix_end)."""
        used = set()
        for c in cls.objects.filter(code__regex=r"^\d+$").values_list("code", flat=True):
            try:
                used.add(int(c))
            except ValueError:
                continue
        for n in range(prefix_start, prefix_end):
            if n not in used:
                return str(n)
        return str(prefix_end)

    @classmethod
    def bank_category_choices(cls):
        """Accounts offered in the bank-transaction review dropdown, grouped.

        Returns [(group_label, [Account, ...]), ...]. Expense categories come
        first because they're what most card transactions are."""
        from expenses.models import ExpenseCategory
        expense_accounts = []
        for cat in ExpenseCategory.objects.filter(is_deleted=False).order_by("name"):
            expense_accounts.append(cat.ensure_account())
        specials = [cls.system(k) for k in BANK_CATEGORY_KEYS]
        return [("Expense categories", expense_accounts),
                ("Income, inventory & owner", specials)]


class FiscalPeriodLock(models.Model):
    """Optional soft lock: dates on/before ``locked_through`` are considered
    closed. The UI warns (and admins can override) when a document inside a
    closed period is edited. Single row."""

    locked_through = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def is_locked(self, day):
        return bool(self.locked_through and day and day <= self.locked_through)


class MonthClose(models.Model):
    """The month-end routine, tracked so it becomes a habit: a row per month
    with the manual checkboxes; the automatic checks (inbox empty, receipts,
    bills) are computed live by ``accounting.closing``."""

    MANUAL_STEPS = [
        ("statements_checked", "Checked each bank/card account against its statement"),
        ("statements_saved", "Saved the month's financial statements (PDF/CSV)"),
        ("notes_reviewed", "Looked over the P&L for anything odd"),
    ]

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    steps = models.JSONField(default=dict, blank=True, help_text="manual step key -> true/false")
    notes = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["year", "month"], name="uniq_month_close")]
        ordering = ["-year", "-month"]

    def __str__(self):
        return f"Month close {self.year}-{self.month:02d}"

    @property
    def is_closed(self):
        return self.closed_at is not None
