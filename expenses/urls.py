from django.urls import path

from expenses import views

urlpatterns = [
    path("", views.expense_list, name="expense_list"),
]
