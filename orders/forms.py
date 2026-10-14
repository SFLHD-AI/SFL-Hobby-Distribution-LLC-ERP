from django import forms

from core.models import CompanySettings
from orders.models import PurchaseOrder


class POForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["customer", "ship_to", "priority", "customer_po_ref",
                  "order_discount_pct", "charge_shipping", "shipping_charge",
                  "charge_tax", "tax_rate", "local_delivery", "notes", "internal_notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # default tax rate from settings
        if not self.instance.pk:
            self.fields["tax_rate"].initial = CompanySettings.get().default_tax_rate
        # limit ship_to to the selected customer's addresses
        customer = None
        if self.instance.pk:
            customer = self.instance.customer
        elif self.data.get("customer"):
            from customers.models import Customer
            customer = Customer.objects.filter(pk=self.data["customer"]).first()
        elif self.initial.get("customer"):
            from customers.models import Customer
            customer = Customer.objects.filter(pk=self.initial["customer"]).first()
        if customer:
            self.fields["ship_to"].queryset = customer.shipping_addresses.filter(is_deleted=False)
            if not self.instance.pk:
                self.fields["ship_to"].initial = customer.default_shipping_address()
        else:
            from customers.models import ShippingAddress
            self.fields["ship_to"].queryset = ShippingAddress.objects.filter(is_deleted=False)

    def clean(self):
        """Never allow another customer's address on this order."""
        cleaned = super().clean()
        customer = cleaned.get("customer")
        ship_to = cleaned.get("ship_to")
        if customer and ship_to and ship_to.customer_id != customer.pk:
            self.add_error("ship_to",
                           "That address belongs to a different customer.")
        return cleaned
