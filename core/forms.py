from django import forms

from core.models import CompanySettings, NumberSequence


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = ["company_name", "logo", "address_line1", "address_line2", "city",
                  "state", "zip_code", "phone", "email", "website", "points_of_contact",
                  "default_tax_rate", "invoice_template", "invoice_footer",
                  "default_payment_terms", "low_stock_email_digest", "alert_email",
                  "shipstation_api_key", "shipstation_api_secret", "shippo_api_token", "uship_api_key"]
        widgets = {
            "points_of_contact": forms.Textarea(attrs={"rows": 3}),
            "invoice_footer": forms.Textarea(attrs={"rows": 3}),
            "shipstation_api_key": forms.PasswordInput(render_value=True),
            "shipstation_api_secret": forms.PasswordInput(render_value=True),
            "uship_api_key": forms.PasswordInput(render_value=True),
            "shippo_api_token": forms.PasswordInput(render_value=True),
        }


class NumberSequenceForm(forms.ModelForm):
    class Meta:
        model = NumberSequence
        fields = ["prefix", "next_number", "padding"]
