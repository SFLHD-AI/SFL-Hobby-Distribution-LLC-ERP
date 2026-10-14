"""Server-rendered shells for the mobile picking UI (data comes from the API)."""
from django.shortcuts import get_object_or_404, render

from core.roles import warehouse_required
from orders.models import PurchaseOrder


@warehouse_required
def pick_queue(request):
    return render(request, "picking/queue.html")


@warehouse_required
def pick_detail(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, is_deleted=False)
    return render(request, "picking/pick.html", {"po": po})
