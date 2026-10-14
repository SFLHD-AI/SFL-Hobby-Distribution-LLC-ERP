from django.urls import path

from accounting import views

urlpatterns = [
    path("", views.index, name="accounting_index"),
]
