from django.urls import path

from customers import views

urlpatterns = [
    path("", views.customer_list, name="customer_list"),
    path("new/", views.customer_create, name="customer_create"),
    path("<int:pk>/", views.customer_detail, name="customer_detail"),
    path("<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path("<int:pk>/statement.pdf", views.customer_statement_pdf, name="customer_statement_pdf"),
    path("<int:pk>/statement/email/", views.customer_statement_email, name="customer_statement_email"),
]
