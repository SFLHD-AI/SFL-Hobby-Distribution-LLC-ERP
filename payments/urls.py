from django.urls import path

from payments import views

urlpatterns = [
    path("", views.payment_list, name="payment_list"),
    path("new/", views.payment_create, name="payment_create"),
    path("ar-adjustment/", views.ar_adjustment, name="ar_adjustment"),
    path("<int:pk>/", views.payment_detail, name="payment_detail"),
]
