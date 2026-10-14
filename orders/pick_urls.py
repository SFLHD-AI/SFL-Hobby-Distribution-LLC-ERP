from django.urls import path

from orders import pick_views

urlpatterns = [
    path("", pick_views.pick_queue, name="pick_queue"),
    path("<int:pk>/", pick_views.pick_detail, name="pick_detail"),
]
