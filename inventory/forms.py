import re
from django import forms

from django.db import models
from inventory.models import (ProductBarcode, CycleCount, PriceList, PriceListItem, Product,
                              Receipt, ReceiptLine, StockAdjustment, Supplier,
                              Category, Brand)


class ProductForm(forms.ModelForm):
    """SKU and UPC fields get a camera-scan button (see static/js/scanner.js)."""
    new_category = forms.CharField(required=False, label="…or new category")
    extra_barcodes = forms.CharField(
        required=False, label="Other barcodes / UPCs",
        widget=forms.Textarea(attrs={"rows": 2, "data-scan": "", "data-scan-append": "",
                                     "placeholder": "one per line — same product, different packaging"}),
        help_text="Each code here finds this product when scanned or typed, and shares its stock and average cost.")

    tcgplayer = forms.CharField(
        required=False, label="TCGplayer product (URL or ID)",
        widget=forms.TextInput(attrs={"placeholder": "paste the tcgplayer.com product link, e.g. https://www.tcgplayer.com/product/593391/…"}),
        help_text="Sealed TCG Product only: pulls the TCGplayer market price every hour and shows your cost as a % of it.")

    tcgplayer_unit = forms.CharField(
        required=False, label="TCGplayer link of the SINGLE unit inside (case SKUs)",
        widget=forms.TextInput(attrs={"placeholder": "for a case: paste the tcgplayer.com link of one box / ETB / pack"}),
        help_text="Case products: the single box's market price × case pack qty is shown next to the case "
                  "price, so you can see your cost as a % of the singles too.")

    def clean_tcgplayer_unit(self):
        from inventory.pricing import parse_tcgplayer_id
        raw = (self.cleaned_data.get("tcgplayer_unit") or "").strip()
        if not raw:
            return None
        pid = parse_tcgplayer_id(raw)
        if pid is None:
            raise forms.ValidationError("Paste the single unit's tcgplayer.com link or its numeric product ID.")
        return pid

    def clean_tcgplayer(self):
        from inventory.pricing import parse_tcgplayer_id
        raw = (self.cleaned_data.get("tcgplayer") or "").strip()
        if not raw:
            return None
        pid = parse_tcgplayer_id(raw)
        if pid is None:
            raise forms.ValidationError("Paste the product's tcgplayer.com link or its numeric product ID.")
        return pid

    def clean_extra_barcodes(self):
        raw = self.cleaned_data.get("extra_barcodes", "")
        codes = []
        for token in re.split(r"[\s,;]+", raw):
            token = token.strip()
            if token and token not in codes:
                codes.append(token)
        primary = (self.cleaned_data.get("upc") or self.data.get("upc") or "").strip()
        sku = (self.cleaned_data.get("sku") or self.data.get("sku") or "").strip()
        codes = [c for c in codes if c != primary and c.lower() != sku.lower()]
        for c in codes:
            clash = ProductBarcode.objects.filter(code=c).exclude(product=self.instance if self.instance.pk else None).first()
            if clash:
                raise forms.ValidationError(f"Barcode {c} already belongs to {clash.product.sku}.")
            other = Product.all_objects.filter(models.Q(upc=c) | models.Q(sku__iexact=c))
            if self.instance.pk:
                other = other.exclude(pk=self.instance.pk)
            if other.exists():
                raise forms.ValidationError(f"{c} is already the SKU or UPC of {other.first().sku}.")
        return codes

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("sku", "upc"):
            if name in self.fields:
                self.fields[name].widget.attrs["data-scan"] = ""
        if self.instance.pk and not self.is_bound:
            self.fields["extra_barcodes"].initial = self.instance.extra_codes_text
            self.fields["tcgplayer"].initial = self.instance.tcgplayer_id or ""
            self.fields["tcgplayer_unit"].initial = self.instance.tcgplayer_unit_id or ""

    def save_barcodes(self, product):
        """Sync ProductBarcode rows to the textarea (call after save())."""
        codes = self.cleaned_data.get("extra_barcodes") or []
        product.barcodes.exclude(code__in=codes).delete()
        for c in codes:
            ProductBarcode.objects.get_or_create(code=c, defaults={"product": product})
        return codes
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
        if "tcgplayer" in self.cleaned_data:
            obj.tcgplayer_id = self.cleaned_data["tcgplayer"]
            if not obj.tcgplayer_id:
                obj.market_price = obj.market_low = None
                obj.market_error = ""
        if "tcgplayer_unit" in self.cleaned_data:
            obj.tcgplayer_unit_id = self.cleaned_data["tcgplayer_unit"]
            if not obj.tcgplayer_unit_id:
                obj.unit_market_price = obj.unit_market_low = None
                obj.unit_market_error = ""
        if commit:
            obj.save()
        return obj


class AdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["qty_delta", "reason", "unit_cost", "note"]


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_name", "email", "phone", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the supplier a name.")
        dup = Supplier.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).first()
        if dup:
            raise forms.ValidationError(f"There is already a supplier called “{dup.name}” — "
                                        "open it instead, or merge this one into it below.")
        return name


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


class ReceiptPackageForm(forms.Form):
    """Add one or more tracking numbers to a supplier order."""
    carrier = forms.ChoiceField(choices=Receipt.CARRIERS, required=False,
                                help_text="Leave blank to guess from the number")
    tracking_numbers = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "one per line — paste or 📷 scan each label",
                                     "autocomplete": "off", "data-scan": "", "data-scan-append": ""}),
        label="Tracking number(s)")

    def clean_tracking_numbers(self):
        raw = self.cleaned_data.get("tracking_numbers", "")
        nums = []
        for token in re.split(r"[\s,;]+", raw):
            token = token.strip()
            if token and token.upper() not in [n.upper() for n in nums]:
                nums.append(token)
        if not nums:
            raise forms.ValidationError("Enter at least one tracking number.")
        return nums
