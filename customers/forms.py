from django import forms

from customers.models import Contact, Customer, ResaleCertificate, ShippingAddress


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["business_name", "email", "phone",
                  "billing_line1", "billing_line2", "billing_city",
                  "billing_state", "billing_zip",
                  "payment_terms", "credit_limit", "price_list", "discount_pct",
                  "no_cert_policy", "notes", "active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "email", "phone", "role", "is_primary"]


class ShippingAddressForm(forms.ModelForm):
    class Meta:
        model = ShippingAddress
        fields = ["label", "attention", "line1", "line2", "city", "state",
                  "zip_code", "phone", "is_default"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["label"].required = False  # defaults to "Main"

    def clean_label(self):
        return self.cleaned_data.get("label", "").strip() or "Main"


class CertificateForm(forms.ModelForm):
    class Meta:
        model = ResaleCertificate
        fields = ["file", "certificate_number", "issuing_state",
                  "issue_date", "expiration_date"]
        widgets = {
            "issue_date": forms.DateInput(attrs={"type": "date"}),
            "expiration_date": forms.DateInput(attrs={"type": "date"}),
        }
