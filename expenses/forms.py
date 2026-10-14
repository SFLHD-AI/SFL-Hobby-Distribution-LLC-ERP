"""
Forms for the Expenses module.

Everything money-related is a Decimal; dates use the browser's date picker;
vendor text inputs carry ``list="vendor-list"`` so templates can attach a
<datalist> of saved vendors.
"""
from decimal import Decimal

from django import forms
from django.db.models import Count, Q
from django.utils import timezone

from banking.models import BankAccount
from expenses.models import (Expense, ExpenseCategory, MileageEntry, RecurringExpense,
                             Vendor)

DATE = forms.DateInput(attrs={"type": "date"})
VENDOR_INPUT = forms.TextInput(attrs={"list": "vendor-list", "autocomplete": "off",
                                      "placeholder": "Who you paid"})
PAID_PERSONALLY_LABEL = "Paid personally (my own card or cash)"
PAID_PERSONALLY_HELP = "Paid personally counts as money you put into the business."


# ---------------------------------------------------------------------------
# helpers shared by forms and views
# ---------------------------------------------------------------------------
def get_or_create_category(name):
    """Find a category by name (case-insensitive). A soft-deleted category
    with that name is restored rather than duplicated (the name column is
    unique across live and deleted rows)."""
    name = (name or "").strip()
    if not name:
        return None
    existing = ExpenseCategory.all_objects.filter(name__iexact=name).first()
    if existing is not None:
        if existing.is_deleted:
            existing.restore()
        return existing
    return ExpenseCategory.objects.create(name=name)


def default_bank_account():
    """The account pre-selected as 'paid from'."""
    qs = BankAccount.objects.filter(active=True)
    return qs.filter(is_default=True).first() or None


def match_vendor(text):
    """Exact (case-insensitive) name match first, then alias match."""
    text = (text or "").strip()
    if not text:
        return None
    exact = Vendor.objects.filter(name__iexact=text).first()
    if exact is not None:
        return exact
    return Vendor.find_for_description(text)


def categories_recent_first(limit_days=90):
    """Categories ordered by how often they were used recently, then name.
    Keeps the quick-capture chips relevant."""
    since = timezone.localdate() - timezone.timedelta(days=limit_days)
    counts = {}
    for row in (Expense.objects.filter(date__gte=since, category__isnull=False)
                .values_list("category_id").annotate(n=Count("id"))):
        counts[row[0]] = row[1]
    cats = list(ExpenseCategory.objects.all())
    cats.sort(key=lambda c: (-counts.get(c.pk, 0), c.name.lower()))
    return cats


# ---------------------------------------------------------------------------
# Expense (full form)
# ---------------------------------------------------------------------------
class ExpenseForm(forms.ModelForm):
    new_category = forms.CharField(
        required=False, max_length=100, label="…or a new category",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Trade show fees"}))

    class Meta:
        model = Expense
        fields = ["date", "category", "vendor_link", "vendor", "amount", "reference", "note",
                  "due_date", "paid_date", "paid_from", "payment_method", "receipt_file"]
        widgets = {
            "date": DATE, "due_date": DATE, "paid_date": DATE,
            "vendor": VENDOR_INPUT,
            "amount": forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
            "note": forms.TextInput(attrs={"placeholder": "What was it for?"}),
            "reference": forms.TextInput(attrs={"placeholder": "Invoice / receipt #"}),
            "receipt_file": forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf"}),
        }
        labels = {
            "date": "Date (bill / receipt date)", "vendor_link": "Saved vendor",
            "vendor": "Vendor name", "paid_from": "Paid from", "payment_method": "How it was paid",
            "receipt_file": "Receipt (photo or PDF)",
        }
        help_texts = {
            "date": "", "amount": "Enter a negative amount for a refund or credit.",
            "paid_date": "Leave blank if this is still an open bill. If you pick a paid-from account "
                         "or a payment method and leave this blank, the bill date is used.",
            "paid_from": "Leave blank for an open bill, or for something you paid personally.",
            "vendor": "Type any name. Pick a saved vendor to reuse its default category.",
        }

    def __init__(self, *args, linked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.linked = linked
        # live rows, plus whatever this expense already points at (even if since deleted)
        inst = self.instance
        self.fields["category"].queryset = ExpenseCategory.all_objects.filter(
            Q(is_deleted=False) | Q(pk=inst.category_id)).order_by("name")
        self.fields["category"].empty_label = "— pick a category —"
        self.fields["vendor_link"].queryset = Vendor.all_objects.filter(
            Q(is_deleted=False) | Q(pk=inst.vendor_link_id)).order_by("name")
        self.fields["vendor_link"].empty_label = "— none —"
        self.fields["paid_from"].queryset = BankAccount.all_objects.filter(
            Q(is_deleted=False, active=True) | Q(pk=inst.paid_from_id)).order_by("-is_default", "name")
        self.fields["paid_from"].empty_label = "— not from a bank account —"
        # New rows are open bills until the owner says otherwise: nothing is
        # pre-selected as "paid from" (the help text promises blank = unpaid).
        if linked:
            for name in ("amount", "date", "paid_date", "paid_from"):
                self.fields[name].disabled = True

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount == 0:
            raise forms.ValidationError("Amount can't be zero.")
        return amount

    def clean_new_category(self):
        return (self.cleaned_data.get("new_category") or "").strip()

    def clean(self):
        data = super().clean()
        if not data.get("category") and not data.get("new_category"):
            self.add_error("category", "Pick a category (or type a new one) so the year-end "
                                       "report stays complete.")
        if not self.linked and not data.get("paid_date") and self._explicitly_paid(data):
            # The owner actively said money left (picked an account or a payment
            # method): default the paid date to the bill date.
            data["paid_date"] = data.get("date") or timezone.localdate()
        if data.get("vendor_link") and not (data.get("vendor") or "").strip():
            data["vendor"] = data["vendor_link"].name
        return data

    def _explicitly_paid(self, data):
        """True only when the user chose a paid-from account or a payment
        method on this submit — never from a pre-filled default."""
        changed = self.changed_data
        if data.get("paid_from") and "paid_from" in changed:
            return True
        if data.get("payment_method") and "payment_method" in changed:
            return True
        return False

    def save(self, commit=True, user=None, source=None):
        exp = super().save(commit=False)
        data = self.cleaned_data
        if data.get("new_category"):
            exp.category = get_or_create_category(data["new_category"])
        if exp.vendor_link_id:
            exp.vendor = exp.vendor_link.name
        elif exp.vendor:
            exp.vendor_link = match_vendor(exp.vendor)
        if exp.category_id is None and exp.vendor_link_id and exp.vendor_link.default_category_id:
            exp.category = exp.vendor_link.default_category
        if not self.linked and data.get("paid_date"):
            exp.paid_date = data["paid_date"]
        if exp.pk is None:
            exp.created_by = user if getattr(user, "is_authenticated", False) else None
            exp.source = source or "manual"
        if commit:
            exp.save()
        return exp


# ---------------------------------------------------------------------------
# Quick capture (list quick-add + phone page)
# ---------------------------------------------------------------------------
class QuickExpenseForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"),
                                widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal",
                                                                "placeholder": "0.00"}))
    date = forms.DateField(initial=timezone.localdate, widget=DATE)
    category = forms.ModelChoiceField(queryset=ExpenseCategory.objects.none(), required=False,
                                      empty_label="— pick a category —")
    new_category = forms.CharField(required=False, max_length=100,
                                   widget=forms.TextInput(attrs={"placeholder": "…or type a new one"}))
    vendor = forms.CharField(required=False, max_length=200, widget=VENDOR_INPUT)
    paid = forms.BooleanField(required=False, initial=True, label="Already paid")
    paid_from = forms.ModelChoiceField(
        queryset=BankAccount.objects.none(), required=False, label="Paid from",
        empty_label=PAID_PERSONALLY_LABEL, help_text=PAID_PERSONALLY_HELP)
    note = forms.CharField(required=False, max_length=500,
                           widget=forms.TextInput(attrs={"placeholder": "What was it for?"}))
    # No ``capture`` attribute: mobile browsers still offer the camera, and a
    # PDF forwarded from email can be attached too.
    receipt_file = forms.FileField(required=False, label="Receipt",
                                   widget=forms.ClearableFileInput(
                                       attrs={"accept": "image/*,application/pdf"}))

    def __init__(self, *args, camera=True, **kwargs):
        # ``camera`` is kept for callers; the widget is the same either way.
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.all()
        self.fields["paid_from"].queryset = BankAccount.objects.filter(active=True)
        self.initial.setdefault("paid_from", default_bank_account())

    def clean(self):
        data = super().clean()
        if not data.get("category") and not (data.get("new_category") or "").strip():
            self.add_error("category", "Pick a category or type a new one.")
        return data

    def save(self, user=None, source="manual"):
        data = self.cleaned_data
        category = data.get("category") or get_or_create_category(data.get("new_category"))
        vendor_text = (data.get("vendor") or "").strip()
        vendor = match_vendor(vendor_text)
        paid = data.get("paid")
        paid_from = data.get("paid_from") if paid else None
        if paid and paid_from is not None:
            method = "card" if paid_from.account_type == "credit_card" else "ach"
        elif paid:
            method = "personal"
        else:
            method = ""
        exp = Expense.objects.create(
            date=data["date"], category=category,
            # a typed alias ("AMZN Mktp US*2K3") is stored as the vendor's real name
            vendor=vendor.name if vendor is not None else vendor_text,
            vendor_link=vendor, amount=data["amount"], note=data.get("note") or "",
            paid_date=data["date"] if paid else None, paid_from=paid_from,
            payment_method=method, receipt_file=data.get("receipt_file"),
            source=source, is_reviewed=True,
            created_by=user if getattr(user, "is_authenticated", False) else None)
        return exp


class MarkPaidForm(forms.Form):
    paid_date = forms.DateField(initial=timezone.localdate, widget=DATE)
    paid_from = forms.ModelChoiceField(queryset=BankAccount.objects.none(), required=False,
                                       empty_label=PAID_PERSONALLY_LABEL)
    payment_method = forms.ChoiceField(choices=[("", "— method —")] + Expense.METHODS, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paid_from"].queryset = BankAccount.objects.filter(active=True)
        self.initial.setdefault("paid_from", default_bank_account())


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ["name", "default_category", "aliases", "is_1099", "tax_id_last4", "email",
                  "phone", "website", "account_number", "notes", "active"]
        widgets = {
            "aliases": forms.Textarea(attrs={"rows": 3, "placeholder": "AMZN Mktp\nAMAZON.COM"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {"default_category": "Default category", "is_1099": "1099 contractor"}
        help_texts = {
            "aliases": "How this vendor shows up on bank and card statements, one per line. "
                       "The bank feed uses these to recognise the vendor.",
            "is_1099": "Independent contractor (not a store). Yearly payments at or over the "
                       "threshold need a 1099-NEC: $600 through tax year 2025, $2,000 from 2026. "
                       "Confirm with your preparer.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_category"].queryset = ExpenseCategory.objects.all()
        self.fields["default_category"].empty_label = "— none —"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        qs = Vendor.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A vendor with this name already exists.")
        return name

    def clean_tax_id_last4(self):
        v = (self.cleaned_data.get("tax_id_last4") or "").strip()
        if v and (len(v) != 4 or not v.isdigit()):
            raise forms.ValidationError("Enter the last 4 digits only.")
        return v


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "description", "tax_line", "deductible_pct", "monthly_budget"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "w-full"}),
            "description": forms.TextInput(attrs={"class": "w-full", "placeholder": "Short note on what belongs here"}),
            "tax_line": forms.Select(attrs={"class": "w-full"}),
            "deductible_pct": forms.NumberInput(attrs={"min": 0, "max": 100, "step": 1, "class": "w-full"}),
            "monthly_budget": forms.NumberInput(attrs={"step": "0.01", "min": 0, "class": "w-full",
                                                       "placeholder": "optional"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._restore = None

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a name.")
        clash = ExpenseCategory.all_objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        clash = clash.first()
        if clash is not None:
            if clash.is_deleted and self.instance.pk is None:
                self._restore = clash          # bring the old one back instead
            else:
                raise forms.ValidationError("A category with this name already exists.")
        return name

    def clean_deductible_pct(self):
        pct = self.cleaned_data.get("deductible_pct")
        if pct is None or pct > 100:
            raise forms.ValidationError("Enter a percent from 0 to 100.")
        return pct

    def validate_unique(self):
        # Handled in clean_name (the default check ignores soft-deleted rows
        # and would let a duplicate through to an IntegrityError).
        return

    def save(self, commit=True):
        if self._restore is not None:
            cat = self._restore
            cat.restore()
            for f in self.Meta.fields:
                setattr(cat, f, self.cleaned_data[f])
            if commit:
                cat.save()
            return cat
        return super().save(commit=commit)


# ---------------------------------------------------------------------------
# Recurring templates
# ---------------------------------------------------------------------------
class RecurringExpenseForm(forms.ModelForm):
    class Meta:
        model = RecurringExpense
        fields = ["name", "vendor_link", "category", "amount", "frequency", "next_date", "end_date",
                  "paid_from", "auto_mark_paid", "note", "active"]
        widgets = {"next_date": DATE, "end_date": DATE,
                   "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"})}
        labels = {"vendor_link": "Vendor", "next_date": "Next date", "end_date": "Stop after",
                  "paid_from": "Paid from", "auto_mark_paid": "Auto-pay (create as already paid)"}
        help_texts = {
            "next_date": "The first expense is created on this date; later ones follow the frequency.",
            "end_date": "Optional. No expenses are created after this date.",
            "auto_mark_paid": "Turn on for things that auto-draft (software, insurance). "
                              "Off = created as an open bill you mark paid later.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor_link"].queryset = Vendor.objects.all()
        self.fields["vendor_link"].empty_label = "— none —"
        self.fields["category"].queryset = ExpenseCategory.objects.all()
        self.fields["category"].required = True
        self.fields["category"].empty_label = "— pick a category —"
        self.fields["paid_from"].queryset = BankAccount.objects.filter(active=True)
        self.fields["paid_from"].empty_label = "— none —"

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Amount must be more than zero.")
        return amount

    def clean(self):
        data = super().clean()
        if data.get("end_date") and data.get("next_date") and data["end_date"] < data["next_date"]:
            self.add_error("end_date", "Stop date is before the next date.")
        if data.get("auto_mark_paid") and not data.get("paid_from"):
            self.add_error("paid_from", "Pick the account auto-pay comes out of.")
        return data


# ---------------------------------------------------------------------------
# Mileage
# ---------------------------------------------------------------------------
class MileageForm(forms.ModelForm):
    class Meta:
        model = MileageEntry
        fields = ["date", "miles", "round_trip", "purpose", "from_location", "to_location", "vehicle"]
        widgets = {"date": DATE,
                   "miles": forms.NumberInput(attrs={"step": "0.1", "min": "0.1", "inputmode": "decimal"}),
                   "purpose": forms.TextInput(attrs={"placeholder": "Card show, supplier pickup, post office…"}),
                   "from_location": forms.TextInput(attrs={"placeholder": "From"}),
                   "to_location": forms.TextInput(attrs={"placeholder": "To"})}
        labels = {"round_trip": "Round trip (doubles the miles)", "from_location": "From",
                  "to_location": "To"}

    def clean_miles(self):
        miles = self.cleaned_data.get("miles")
        if miles is not None and miles <= 0:
            raise forms.ValidationError("Miles must be more than zero.")
        return miles
