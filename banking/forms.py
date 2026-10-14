"""Forms for the banking module."""
import re
from decimal import Decimal

from django import forms
from django.utils import timezone

from accounting.models import Account, AccountSubtype
from banking import regex_guard
from banking.importers import MAPPING_FIELDS, PRESET_CHOICES
from banking.models import BankAccount, BankTransaction, CategoryRule
from core.models import CompanySettings
from expenses.models import Vendor

DATE = forms.DateInput(attrs={"type": "date"})
MAX_UPLOAD = 8 * 1024 * 1024


def category_groups(include_bank=False):
    """[(group label, [Account, ...])] for review dropdowns. ``include_bank``
    adds bank/card ledger accounts (used by transfer rules)."""
    groups = Account.bank_category_choices()
    if include_bank:
        bank = list(Account.objects.filter(active=True, subtype__in=[
            AccountSubtype.BANK, AccountSubtype.CREDIT_CARD, AccountSubtype.CASH_ON_HAND,
            AccountSubtype.LOAN]).order_by("code"))
        if bank:
            groups = groups + [("Bank & card accounts (transfers)", bank)]
    return groups


def grouped_choices(groups, blank="— pick a category —"):
    out = [("", blank)] if blank is not None else []
    for label, accounts in groups:
        out.append((label, [(a.pk, a.name) for a in accounts]))
    return out


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "institution", "account_type", "last4", "opening_date", "opening_balance",
                  "is_default", "active", "notes"]
        widgets = {"opening_date": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"last4": "Last 4 digits", "is_default": "Default account for expenses",
                  "active": "Active"}
        help_texts = {
            "name": "What you call it, e.g. Chase Business Checking",
            "opening_date": "The day this system starts tracking the account. "
                            "Transactions dated before this are not imported.",
            "opening_balance": "Balance on the opening date. For credit cards and loans, "
                               "enter the amount you OWED as a positive number.",
            "is_default": "Pre-selected as the 'paid from' account when you add an expense.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            start = CompanySettings.get().books_start_date
            if start:
                self.fields["opening_date"].initial = start
                self.fields["opening_date"].help_text = (
                    f"Your books start on {start:%b %-d, %Y}; transactions before the opening "
                    "date are skipped.")

    def clean_last4(self):
        v = (self.cleaned_data.get("last4") or "").strip()
        if v and not re.fullmatch(r"\d{4}", v):
            raise forms.ValidationError("Enter exactly four digits, or leave blank.")
        return v


class CategoryRuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["name", "active", "priority", "match_field", "match_type", "pattern", "direction",
                  "account_filter", "amount_min", "amount_max", "action", "category_account",
                  "vendor", "auto_confirm"]
        labels = {"pattern": "Text to look for", "match_field": "Look in", "match_type": "How to match",
                  "direction": "Direction", "account_filter": "Only this bank account",
                  "amount_min": "Amount at least", "amount_max": "Amount at most",
                  "category_account": "Category", "auto_confirm": "Apply automatically"}
        help_texts = {
            "priority": "Lower numbers run first when more than one rule fits.",
            "pattern": "Not case sensitive. Example: PIRATE SHIP",
            "amount_min": "Leave blank for no limit. Compared ignoring the sign.",
            "auto_confirm": "Off = the rule only suggests, and you confirm each one in the inbox.",
            "category_account": "Required for 'Categorize'. Optional for transfer rules "
                                "(suggests the other account when no twin is found).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account_filter"].queryset = BankAccount.objects.filter(active=True)
        self.fields["account_filter"].empty_label = "Any account"
        self.fields["vendor"].queryset = Vendor.objects.filter(active=True)
        self.fields["vendor"].empty_label = "— none —"
        self.fields["category_account"].queryset = Account.objects.filter(active=True)
        self.fields["category_account"].choices = grouped_choices(category_groups(include_bank=True))

    def clean(self):
        data = super().clean()
        pattern = (data.get("pattern") or "").strip()
        if data.get("match_type") == "regex" and pattern:
            if regex_guard.nested_quantifier(pattern):
                self.add_error("pattern", "That pattern repeats a group that already repeats "
                                          "(like (a+)+ or (\\w+\\s?)*). It could stall every import; "
                                          "simplify it, or use 'Contains' instead.")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    self.add_error("pattern", f"That is not a valid regular expression: {exc}")
                else:
                    if regex_guard.compile_safe(pattern) is None or not regex_guard.is_safe(pattern):
                        self.add_error("pattern", "That pattern is too slow to run safely on every "
                                                  "transaction. Simplify it, or use 'Contains' instead.")
        if data.get("action") == "categorize" and not data.get("category_account"):
            self.add_error("category_account", "Pick the category this rule should assign.")
        lo, hi = data.get("amount_min"), data.get("amount_max")
        if lo is not None and hi is not None and lo > hi:
            self.add_error("amount_max", "'At most' must be greater than 'at least'.")
        data["pattern"] = pattern
        return data


class ManualTransactionForm(forms.Form):
    DIRECTIONS = [("out", "Money out (purchase, fee, withdrawal)"),
                  ("in", "Money in (deposit, refund)")]
    account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(active=True), label="Account")
    date = forms.DateField(widget=DATE, initial=timezone.localdate)
    direction = forms.ChoiceField(choices=DIRECTIONS, widget=forms.RadioSelect, initial="out")
    amount = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, max_digits=12,
                                label="Amount", help_text="Enter a positive number; pick the direction above.")
    description = forms.CharField(max_length=300)
    merchant_name = forms.CharField(max_length=200, required=False, label="Merchant / payee")
    check_number = forms.CharField(max_length=20, required=False, label="Check #")
    notes = forms.CharField(max_length=500, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default = BankAccount.objects.filter(active=True, is_default=True).first()
        if default and not self.is_bound:
            self.fields["account"].initial = default.pk

    def signed_amount(self):
        amt = self.cleaned_data["amount"]
        return -amt if self.cleaned_data["direction"] == "out" else amt

    def clean(self):
        data = super().clean()
        acct, day = data.get("account"), data.get("date")
        if acct and day and day < acct.opening_date:
            self.add_error("date", f"{acct.name} starts on {acct.opening_date:%b %-d, %Y}; "
                                   "pick a date on or after that.")
        return data

    def save(self, user=None):
        acct = self.cleaned_data["account"]
        txn = BankTransaction(
            account=acct, date=self.cleaned_data["date"], amount=self.signed_amount(),
            description=self.cleaned_data["description"].strip(),
            merchant_name=(self.cleaned_data.get("merchant_name") or "").strip(),
            check_number=(self.cleaned_data.get("check_number") or "").strip(),
            notes=(self.cleaned_data.get("notes") or "").strip(), source="manual")
        txn.save()
        return txn


class ManualTransactionEditForm(forms.Form):
    """Edit a hand-entered transaction (date, amount, description) while it
    is still waiting for review."""
    date = forms.DateField(widget=DATE)
    direction = forms.ChoiceField(choices=ManualTransactionForm.DIRECTIONS, initial="out")
    amount = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, max_digits=12,
                                label="Amount", help_text="Positive number; pick the direction.")
    description = forms.CharField(max_length=300)

    def __init__(self, txn, *args, **kwargs):
        self.txn = txn
        kwargs.setdefault("initial", {}).update({
            "date": txn.date, "amount": abs(txn.amount), "description": txn.description,
            "direction": "out" if txn.amount < 0 else "in"})
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        day = data.get("date")
        if day and day < self.txn.account.opening_date:
            self.add_error("date", f"{self.txn.account.name} starts on {self.txn.account.opening_date:%b %-d, %Y}; "
                                   "pick a date on or after that.")
        return data

    def save(self):
        txn = self.txn
        amt = self.cleaned_data["amount"]
        same_desc = txn.original_description == txn.description
        txn.date = self.cleaned_data["date"]
        txn.amount = -amt if self.cleaned_data["direction"] == "out" else amt
        txn.description = self.cleaned_data["description"].strip()
        if same_desc or not txn.original_description:
            txn.original_description = txn.description
        txn.fingerprint = BankTransaction.make_fingerprint(txn.account_id, txn.date, txn.amount, txn.description)
        txn.save()
        return txn


class ImportUploadForm(forms.Form):
    account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(active=True),
                                     label="Import into")
    file = forms.FileField(label="Statement file (.csv)")
    preset = forms.ChoiceField(choices=PRESET_CHOICES, initial="auto", label="Bank format")

    def clean_file(self):
        f = self.cleaned_data["file"]
        name = (f.name or "").lower()
        if not (name.endswith(".csv") or name.endswith(".txt") or name.endswith(".tsv")):
            raise forms.ValidationError("Upload the CSV export from your bank (.csv).")
        if f.size > MAX_UPLOAD:
            raise forms.ValidationError("That file is larger than 8 MB. Export a shorter date range.")
        return f


class GenericMappingForm(forms.Form):
    """Column picker for the generic preset; built from the file's columns."""
    positive_is_charge = forms.BooleanField(
        required=False, label="Amounts are positive for charges (money out)",
        help_text="Tick this for card exports that list purchases as positive numbers.")

    def __init__(self, column_labels, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— not in this file —")] + [(str(i), label) for i, label in column_labels]
        for key, label, required in MAPPING_FIELDS:
            self.fields[f"col_{key}"] = forms.ChoiceField(choices=choices, required=required, label=label)
        # keep the checkbox last
        self.fields["positive_is_charge"] = self.fields.pop("positive_is_charge")

    def clean(self):
        data = super().clean()
        amount, debit, credit = data.get("col_amount"), data.get("col_debit"), data.get("col_credit")
        if not amount and not (debit or credit):
            self.add_error("col_amount", "Pick either one amount column or the money out / money in columns.")
        return data


class ReconcileForm(forms.Form):
    statement_date = forms.DateField(widget=DATE, label="Statement ending date")
    ending_balance = forms.DecimalField(decimal_places=2, max_digits=12, label="Ending balance on the statement",
                                        help_text="For a card, the amount owed as a positive number.")


class ReceiptUploadForm(forms.Form):
    receipt_file = forms.FileField(label="Receipt image or PDF")

    def clean_receipt_file(self):
        f = self.cleaned_data["receipt_file"]
        if f.size > 15 * 1024 * 1024:
            raise forms.ValidationError("Keep receipts under 15 MB.")
        return f
