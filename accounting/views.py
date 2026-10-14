"""Placeholder — replaced by the accounting implementation."""
from django.shortcuts import render

from core.roles import module_required


@module_required("accounting")
def index(request):
    return render(request, "accounting/index.html", {})
