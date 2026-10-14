"""Placeholder — replaced by the banking implementation."""
from django.shortcuts import render

from core.roles import module_required


@module_required("banking")
def inbox(request):
    return render(request, "banking/inbox.html", {})
