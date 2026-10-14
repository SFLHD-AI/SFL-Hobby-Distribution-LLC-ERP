from django.urls import path

from banking import views

urlpatterns = [
    path("", views.inbox, name="bank_inbox"),
]
