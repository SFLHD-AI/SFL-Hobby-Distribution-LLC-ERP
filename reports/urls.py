from django.urls import path

from reports import views

urlpatterns = [
    path("", views.report_index, name="report_index"),
    path("inventory-valuation/", views.inventory_valuation, name="report_inventory_valuation"),
    path("pnl/", views.pnl, name="report_pnl"),
    path("revenue-by-customer/", views.revenue_by_customer, name="report_revenue_customer"),
    path("revenue-by-dow/", views.revenue_by_dow, name="report_revenue_dow"),
    path("sales-by-product/", views.sales_by_product, name="report_sales_product"),
    path("movements/", views.movement_report, name="report_movements"),
    path("sales-tax/", views.sales_tax_report, name="report_sales_tax"),
    path("shipping/", views.shipping_report, name="report_shipping"),
    path("ar-aging/", views.ar_aging, name="report_ar_aging"),
    path("quickbooks/", views.quickbooks_export, name="report_quickbooks"),
]
