from django.urls import path

from expenses import views

urlpatterns = [
    # register
    path("", views.expense_list, name="expense_list"),
    path("new/", views.expense_create, name="expense_create"),
    path("quick/", views.expense_quick, name="expense_quick"),
    path("<int:pk>/edit/", views.expense_edit, name="expense_edit"),
    # vendors
    path("vendors/", views.expense_vendors, name="expense_vendors"),
    path("vendors/new/", views.expense_vendor_create, name="expense_vendor_create"),
    path("vendors/<int:pk>/", views.expense_vendor_detail, name="expense_vendor_detail"),
    path("vendors/<int:pk>/edit/", views.expense_vendor_edit, name="expense_vendor_edit"),
    # categories
    path("categories/", views.expense_categories, name="expense_categories"),
    # recurring templates
    path("recurring/", views.expense_recurring, name="expense_recurring"),
    path("recurring/new/", views.expense_recurring_create, name="expense_recurring_create"),
    path("recurring/<int:pk>/edit/", views.expense_recurring_edit, name="expense_recurring_edit"),
    # budgets & mileage
    path("budgets/", views.expense_budgets, name="expense_budgets"),
    path("mileage/", views.expense_mileage, name="expense_mileage"),
    # reports
    path("reports/by-month/", views.expense_report_by_month, name="expense_report_by_month"),
    path("reports/missing-receipts/", views.expense_report_missing_receipts,
         name="expense_report_missing_receipts"),
    path("reports/unpaid-bills/", views.expense_report_unpaid_bills, name="expense_report_unpaid_bills"),
    path("reports/1099/", views.expense_report_1099, name="expense_report_1099"),
    path("year-end/", views.expense_year_end, name="expense_year_end"),
]
