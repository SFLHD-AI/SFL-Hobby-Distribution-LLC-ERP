"""Minimal django-admin registrations (main UI is the SFL ERP System app itself;
/django-admin/ is used chiefly for user & group management)."""
from django.contrib import admin

from core.models import ActivityLog, CompanySettings, NumberSequence
from customers.models import Customer
from expenses.models import Expense, ExpenseCategory
from inventory.models import Brand, Category, PriceList, Product, Supplier
from invoicing.models import Invoice
from orders.models import PurchaseOrder
from payments.models import Payment
from shipping.models import Shipment

admin.site.site_header = "SFL ERP System administration"

for model in (CompanySettings, NumberSequence, ActivityLog, Product, Category,
              Brand, Supplier, PriceList, Customer, PurchaseOrder, Invoice,
              Payment, Expense, ExpenseCategory, Shipment):
    try:
        admin.site.register(model)
    except admin.sites.AlreadyRegistered:
        pass
