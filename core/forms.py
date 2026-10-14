import calendar
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from core.models import CompanySettings, NumberSequence

MONTH_CHOICES = [(i, calendar.month_name[i]) for i in range(1, 13)]


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = ["company_name", "logo", "address_line1", "address_line2", "city",
                  "state", "zip_code", "phone", "email", "website", "points_of_contact",
                  "default_tax_rate", "invoice_template", "invoice_footer",
                  "default_payment_terms", "low_stock_email_digest", "alert_email",
                  "shipstation_api_key", "shipstation_api_secret", "shippo_api_token", "uship_api_key",
                  # accounting
                  "fiscal_year_start_month", "accounting_basis_default", "books_start_date",
                  "mileage_rate", "sales_tax_filing_frequency",
                  # bank feed
                  "plaid_client_id", "plaid_secret", "plaid_env", "bank_sync_enabled",
                  "expense_review_digest"]
        widgets = {
            "points_of_contact": forms.Textarea(attrs={"rows": 3}),
            "invoice_footer": forms.Textarea(attrs={"rows": 3}),
            "shipstation_api_key": forms.PasswordInput(render_value=True),
            "shipstation_api_secret": forms.PasswordInput(render_value=True),
            "uship_api_key": forms.PasswordInput(render_value=True),
            "shippo_api_token": forms.PasswordInput(render_value=True),
            "fiscal_year_start_month": forms.Select(choices=MONTH_CHOICES),
            "books_start_date": forms.DateInput(attrs={"type": "date"}),
            "mileage_rate": forms.NumberInput(attrs={"step": "0.001", "min": "0"}),
            "plaid_secret": forms.PasswordInput(render_value=True),
            "plaid_client_id": forms.TextInput(attrs={"autocomplete": "off"}),
        }
        labels = {
            "fiscal_year_start_month": "Fiscal year starts in",
            "accounting_basis_default": "Show first",
            "books_start_date": "Books start date",
            "mileage_rate": "Mileage rate ($ per mile)",
            "sales_tax_filing_frequency": "Sales tax filing",
            "plaid_client_id": "Plaid client ID",
            "plaid_secret": "Plaid secret",
            "plaid_env": "Plaid environment",
            "bank_sync_enabled": "Pull new transactions automatically (hourly)",
            "expense_review_digest": "Send the weekly money review email",
        }

    def clean_fiscal_year_start_month(self):
        month = self.cleaned_data.get("fiscal_year_start_month")
        if month is None or not 1 <= int(month) <= 12:
            raise ValidationError("Pick a month between January and December.")
        return int(month)

    def clean_mileage_rate(self):
        rate = self.cleaned_data.get("mileage_rate")
        if rate is None:
            return Decimal("0.000")
        if rate < 0:
            raise ValidationError("The mileage rate cannot be negative.")
        if rate > 5:
            raise ValidationError("That rate looks wrong — enter dollars per mile, e.g. 0.70.")
        return rate

    def clean(self):
        cleaned = super().clean()
        client_id = (cleaned.get("plaid_client_id") or "").strip()
        secret = (cleaned.get("plaid_secret") or "").strip()
        if bool(client_id) != bool(secret):
            self.add_error("plaid_secret" if client_id else "plaid_client_id",
                           "Enter both the Plaid client ID and the secret (or leave both blank).")
        cleaned["plaid_client_id"] = client_id
        cleaned["plaid_secret"] = secret
        return cleaned


class NumberSequenceForm(forms.ModelForm):
    class Meta:
        model = NumberSequence
        fields = ["prefix", "next_number", "padding"]
