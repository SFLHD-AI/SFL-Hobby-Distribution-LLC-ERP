from django.urls import path

from shipping import delivery_views

urlpatterns = [
    path("", delivery_views.delivery_list, name="delivery_list"),
    path("<int:pk>/", delivery_views.delivery_detail, name="delivery_detail"),
]
