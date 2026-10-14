from django import forms

from inventory.models import (CycleCount, PriceList, PriceListItem, Product,
                              Receipt, ReceiptLine, StockAdjustment, Supplier,
                              Category, Brand)


class ProductForm(forms.ModelForm):
    new_category = forms.CharField(required=False, label="…or new category")
    new_brand = forms.CharField(required=False, label="…or new brand")
    # Opening stock (new products only): enter what you have and what it cost you
    initial_qty = forms.IntegerField(
        required=False, min_value=1, label="Opening quantity on hand")
    initial_unit_cost = forms.DecimalField(
        required=False, min_value=0, decimal_places=4, max_digits=12,
        label="Your cost per unit $")

    class Meta:
        model = Product
        fields = ["sku", "name", "description", "category", "brand", "upc",
                  "case_pack_qty", "unit_of_measure", "weight_lbs",
                  "default_price", "msrp",
                  "reorder_point", "bin_location", "image", "active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def save(self, commit=True):
        obj = super().save(commit=False)
        nc = self.cleaned_data.get("new_category", "").strip()
        nb = self.cleaned_data.get("new_brand", "").strip()
        if nc:
            obj.category, _ = Category.objects.get_or_create(name=nc)
        if nb:
            obj.brand, _ = Brand.objects.get_or_create(name=nb)
        if commit:
            obj.save()
        return obj


class AdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["qty_delta", "reason", "unit_cost", "note"]


class ReceiptForm(forms.ModelForm):
    new_supplier = forms.CharField(required=False, label="…or new supplier")

    class Meta:
        model = Receipt
        fields = ["supplier", "date", "expected_date", "reference",
                  "supplier_invoice_number", "due_date",
                  "freight_cost", "duties_cost", "notes"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "expected_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def save(self, commit=True):
        obj = super().save(commit=False)
        ns = self.cleaned_data.get("new_supplier", "").strip()
        if ns:
            obj.supplier, _ = Supplier.objects.get_or_create(name=ns)
        if commit:
            obj.save()
        return obj


class ReceiptLineForm(forms.ModelForm):
    class Meta:
        model = ReceiptLine
        fields = ["product", "qty", "unit_cost"]


class PriceListForm(forms.ModelForm):
    class Meta:
        model = PriceList
        fields = ["name", "description", "active"]


class PriceListItemForm(forms.ModelForm):
    class Meta:
        model = PriceListItem
        fields = ["product", "price"]


class CycleCountForm(forms.ModelForm):
    scope = forms.ChoiceField(choices=[("all", "All active products"),
                                       ("category", "One category"),
                                       ("bin", "Bin location prefix")],
                              initial="all")
    category = forms.ModelChoiceField(queryset=Category.objects.all(), required=False)
    bin_prefix = forms.CharField(required=False)

    class Meta:
        model = CycleCount
        fields = ["name"]


class ReceiptBillForm(forms.ModelForm):
    """Supplier-bill details that stay editable after the goods are received:
    invoice number, due date, and how/when it was paid. Matching a bank
    transaction on the Banking screen fills the paid fields automatically."""

    class Meta:
        model = Receipt
        fields = ["supplier_invoice_number", "due_date", "paid_date", "paid_from",
                  "payment_reference"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "paid_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from banking.models import BankAccount
        self.fields["paid_from"].queryset = BankAccount.objects.filter(active=True)
        self.fields["paid_from"].empty_label = "— paid personally / not from a tracked account —"
