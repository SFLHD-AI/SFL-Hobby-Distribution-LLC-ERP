"""Forms for the chart of accounts and the period lock."""
from django import forms

from accounting.models import Account, AccountSubtype, AccountType, FiscalPeriodLock

# Which subtypes make sense for each account type. The engine uses subtypes to
# decide how an account behaves, so a mismatch would produce nonsense statements.
SUBTYPES_BY_TYPE = {
    AccountType.ASSET: [AccountSubtype.BANK, AccountSubtype.CASH_ON_HAND, AccountSubtype.UNDEPOSITED,
                        AccountSubtype.AR, AccountSubtype.INVENTORY, AccountSubtype.SUPPLIER_DEPOSITS,
                        AccountSubtype.FIXED_ASSET, AccountSubtype.OTHER_ASSET],
    AccountType.LIABILITY: [AccountSubtype.CREDIT_CARD, AccountSubtype.AP, AccountSubtype.AP_CARRIERS,
                            AccountSubtype.SALES_TAX, AccountSubtype.CUSTOMER_DEPOSITS,
                            AccountSubtype.LOAN, AccountSubtype.OTHER_LIABILITY],
    AccountType.EQUITY: [AccountSubtype.OPENING_BALANCE, AccountSubtype.OWNER_CONTRIBUTION,
                         AccountSubtype.OWNER_DRAW, AccountSubtype.RETAINED],
    AccountType.INCOME: [AccountSubtype.SALES, AccountSubtype.SHIPPING_INCOME, AccountSubtype.SALES_RETURNS,
                         AccountSubtype.OTHER_INCOME, AccountSubtype.UNCAT_INCOME],
    AccountType.COGS: [AccountSubtype.COGS, AccountSubtype.INVENTORY_PURCHASES, AccountSubtype.INVENTORY_ADJ,
                       AccountSubtype.SHIPPING_EXPENSE],
    AccountType.EXPENSE: [AccountSubtype.EXPENSE, AccountSubtype.UNCAT_EXPENSE],
}

# Suggested code ranges per type, used to prefill a free code on the create form.
CODE_RANGES = {
    AccountType.ASSET: (1600, 1999),
    AccountType.LIABILITY: (2600, 2999),
    AccountType.EQUITY: (3300, 3899),
    AccountType.INCOME: (4300, 4899),
    AccountType.COGS: (5400, 5899),
    AccountType.EXPENSE: (6000, 6899),
}


def subtype_groups():
    """[(type label, [(value, label), ...]), ...] for a grouped <select>."""
    labels = dict(AccountSubtype.choices)
    return [(AccountType(t).label, [(s.value, labels[s.value]) for s in subs])
            for t, subs in SUBTYPES_BY_TYPE.items()]


class AccountForm(forms.ModelForm):
    """Create / edit a non-system account."""

    class Meta:
        model = Account
        fields = ["code", "name", "type", "subtype", "description", "active"]
        widgets = {
            "code": forms.TextInput(attrs={"inputmode": "numeric", "maxlength": 10}),
            "name": forms.TextInput(attrs={"autofocus": True}),
            "description": forms.TextInput(attrs={"placeholder": "Short note on what belongs here"}),
        }
        help_texts = {
            "code": "Short sort code. Statements list accounts in code order.",
            "subtype": "Controls where the account appears on statements.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subtype"].choices = subtype_groups()
        self.fields["code"].required = True
        self.fields["name"].required = True

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        if not code:
            raise forms.ValidationError("Enter a code.")
        clash = Account.objects.filter(code=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Code {code} is already used by {clash.first().name}.")
        return code

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a name.")
        clash = Account.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("An account with this name already exists.")
        return name

    def clean(self):
        data = super().clean()
        type_, subtype = data.get("type"), data.get("subtype")
        if type_ and subtype and subtype not in SUBTYPES_BY_TYPE.get(type_, []):
            self.add_error("subtype", f"That subtype does not belong to the "
                                      f"{AccountType(type_).label.lower()} type.")
        return data


class SystemAccountForm(forms.ModelForm):
    """System accounts keep their code/type/subtype; only the label is editable."""

    class Meta:
        model = Account
        fields = ["name", "description"]
        widgets = {"description": forms.TextInput()}

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a name.")
        return name


class PeriodLockForm(forms.ModelForm):
    class Meta:
        model = FiscalPeriodLock
        fields = ["locked_through", "note"]
        widgets = {
            "locked_through": forms.DateInput(attrs={"type": "date"}),
            "note": forms.TextInput(attrs={"placeholder": "e.g. 2025 return filed with CPA on Mar 3"}),
        }
        labels = {"locked_through": "Books closed through",
                  "note": "Note"}

    def clean_locked_through(self):
        day = self.cleaned_data.get("locked_through")
        if day is not None:
            from django.utils import timezone
            if day > timezone.localdate():
                raise forms.ValidationError("You can only close periods that have already ended.")
        return day
