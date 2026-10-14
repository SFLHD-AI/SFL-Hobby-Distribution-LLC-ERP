from django.urls import path

from invoicing import views

urlpatterns = [
    path("", views.invoice_list, name="invoice_list"),
    path("new/", views.invoice_create, name="invoice_create"),
    path("new/manual/", views.invoice_create_manual, name="invoice_create_manual"),
    path("credits/", views.creditmemo_list, name="creditmemo_list"),
    path("<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("<int:pk>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("<int:pk>/email/", views.invoice_email, name="invoice_email"),
    path("<int:invoice_pk>/credit/", views.creditmemo_create, name="creditmemo_create"),
]
