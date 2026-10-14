"""Minimal django-admin registrations (main UI is the SFL ERP System app itself;
/django-admin/ is used chiefly for user & group management and data repair)."""
from django.contrib import admin

from accounting.models import Account, FiscalPeriodLock
from banking.models import (BankAccount, BankConnection, BankTransaction, CategoryRule,
                            ImportBatch)
from core.models import ActivityLog, CompanySettings, NumberSequence
from customers.models import Customer
from expenses.models import (Budget, Expense, ExpenseCategory, MileageEntry,
                             RecurringExpense, Vendor)
from inventory.models import Brand, Category, PriceList, Product, Supplier
from invoicing.models import Invoice
from orders.models import PurchaseOrder
from payments.models import Payment
from shipping.models import Shipment

admin.site.site_header = "SFL ERP System administration"


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "type", "subtype", "system_key", "active")
    list_filter = ("type", "subtype", "active")
    search_fields = ("code", "name", "system_key")


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "account", "description", "amount", "status", "category_account")
    list_filter = ("status", "account", "source", "pending")
    search_fields = ("description", "original_description", "merchant_name", "external_id")
    date_hierarchy = "date"
    raw_id_fields = ("expense", "transfer_peer", "payments", "receipts")


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_type", "institution", "last4", "opening_balance",
                    "opening_date", "active", "is_default")


@admin.register(BankConnection)
class BankConnectionAdmin(admin.ModelAdmin):
    list_display = ("institution_name", "provider", "status", "last_synced_at")
    exclude = ("access_token",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("date", "vendor", "category", "amount", "paid_date", "paid_from", "source")
    list_filter = ("category", "source", "payment_method")
    search_fields = ("vendor", "note", "reference")
    date_hierarchy = "date"


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "default_category", "is_1099", "active")
    search_fields = ("name", "aliases")


for model in (CompanySettings, NumberSequence, ActivityLog, Product, Category,
              Brand, Supplier, PriceList, Customer, PurchaseOrder, Invoice,
              Payment, ExpenseCategory, RecurringExpense, Budget, MileageEntry,
              CategoryRule, ImportBatch, FiscalPeriodLock, Shipment):
    try:
        admin.site.register(model)
    except admin.sites.AlreadyRegistered:
        pass
