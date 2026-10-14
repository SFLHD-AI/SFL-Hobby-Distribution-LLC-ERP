from django.urls import path

from orders import views

urlpatterns = [
    path("", views.po_list, name="po_list"),
    path("new/", views.po_create, name="po_create"),
    path("product-search/", views.product_search_json, name="product_search_json"),
    path("<int:pk>/", views.po_detail, name="po_detail"),
    path("<int:pk>/edit/", views.po_edit, name="po_edit"),
    path("<int:pk>/pdf/", views.po_pdf, name="po_pdf"),
    path("<int:pk>/packing-slip/", views.packing_slip_pdf, name="packing_slip_pdf"),
]
