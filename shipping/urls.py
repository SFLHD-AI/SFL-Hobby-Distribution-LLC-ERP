from django.urls import path

from shipping import views

urlpatterns = [
    path("", views.shipment_list, name="shipment_list"),
    path("sync/", views.sync_now, name="shipping_sync_now"),
    path("pirateship-export/", views.pirateship_export, name="pirateship_export"),
    path("po/<int:po_pk>/new/", views.shipment_create, name="shipment_create"),
    path("<int:pk>/", views.shipment_detail, name="shipment_detail"),
]
