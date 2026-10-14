from django.urls import path

from inventory import views

urlpatterns = [
    path("", views.product_list, name="product_list"),
    path("import/", views.product_import, name="product_import"),
    path("new/", views.product_create, name="product_create"),
    path("labels/", views.product_labels, name="product_labels"),
    path("<int:pk>/", views.product_detail, name="product_detail"),
    path("<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("<int:pk>/mute-low-stock/", views.toggle_low_stock_mute, name="toggle_low_stock_mute"),

    path("receiving/", views.receipt_list, name="receipt_list"),
    path("receiving/new/", views.receipt_create, name="receipt_create"),
    path("receiving/<int:pk>/", views.receipt_detail, name="receipt_detail"),

    path("cycle-counts/", views.cyclecount_list, name="cyclecount_list"),
    path("cycle-counts/new/", views.cyclecount_create, name="cyclecount_create"),
    path("cycle-counts/<int:pk>/", views.cyclecount_detail, name="cyclecount_detail"),

    path("price-lists/", views.pricelist_list, name="pricelist_list"),
    path("price-lists/new/", views.pricelist_create, name="pricelist_create"),
    path("price-lists/<int:pk>/", views.pricelist_detail, name="pricelist_detail"),
]
