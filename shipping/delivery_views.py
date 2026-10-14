"""
Local deliveries — the Fulfillment Manager's screen.

* Day view of scheduled local deliveries in the MOST EFFICIENT drop-off
  order (computed from the warehouse address in Settings — see routing.py).
* Each stop gets a one-tap Apple Maps navigate button; a full-route
  Google Maps link (optimized order) is offered as a fallback.
* Photo proof upload marks a delivery as delivered.
* "Needs scheduling": local-delivery POs that are ready but not yet on a day.
"""
from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import CompanySettings, log_activity
from core.roles import module_required
from orders.models import PurchaseOrder
from shipping.models import Shipment
from shipping.routing import apple_maps_url, google_maps_route_url, plan_route


def _parse_date(raw):
    try:
        return date.fromisoformat(raw) if raw else timezone.localdate()
    except ValueError:
        return timezone.localdate()


def _warehouse_origin():
    c = CompanySettings.get()
    parts = [c.address_line1, c.city, c.state, c.zip_code]
    return " ".join(p for p in parts if p)


@module_required("deliveries")
def delivery_list(request):
    day = _parse_date(request.GET.get("date"))

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "schedule":
            po = get_object_or_404(PurchaseOrder, pk=request.POST["po_id"], is_deleted=False)
            when = _parse_date(request.POST.get("date"))
            Shipment.objects.create(
                po=po, provider="local", status="pending", ship_date=when,
                ship_to_text=po.ship_to.as_text() if po.ship_to else "",
                created_by=request.user)
            log_activity(request.user, "delivery_scheduled", po, note=str(when))
            messages.success(request, f"{po.number} scheduled for delivery {when:%b %-d}.")
            return redirect(f"{request.path}?date={when}")
        if action == "out":
            ids = request.POST.getlist("shipment_id")
            Shipment.objects.filter(pk__in=ids, provider="local").update(status="out_for_delivery")
            messages.success(request, "Marked out for delivery. Safe driving!")
            return redirect(f"{request.path}?date={day}")

    deliveries = (Shipment.objects
                  .filter(is_deleted=False, provider="local", ship_date=day)
                  .select_related("po", "po__customer")
                  .order_by("status", "id"))

    needs_scheduling = (PurchaseOrder.objects
                        .filter(is_deleted=False, local_delivery=True,
                                status__in=[PurchaseOrder.PICKED, PurchaseOrder.FINALIZED,
                                            PurchaseOrder.INVOICED])
                        .exclude(shipments__provider="local",
                                 shipments__is_deleted=False)
                        .select_related("customer", "ship_to"))

    # ---- optimize the drop-off order for the open stops -------------------
    origin = _warehouse_origin()
    open_deliveries = [d for d in deliveries if d.status != "delivered"]
    done_deliveries = [d for d in deliveries if d.status == "delivered"]
    order, optimized = plan_route(origin, [d.maps_destination for d in open_deliveries])
    open_deliveries = [open_deliveries[i] for i in order]
    for n, d in enumerate(open_deliveries, start=1):
        d.stop_number = n
        d.apple_url = apple_maps_url(d.maps_destination) if d.maps_destination else None

    ordered_stops = [d.maps_destination for d in open_deliveries if d.maps_destination]
    return render(request, "shipping/delivery_list.html", {
        "day": day,
        "deliveries": open_deliveries + done_deliveries,
        "open_count": len(open_deliveries),
        "route_optimized": optimized,
        "needs_scheduling": needs_scheduling,
        "route_url": google_maps_route_url(origin, ordered_stops),
        "first_stop_apple": apple_maps_url(ordered_stops[0], origin=origin) if ordered_stops else None,
        "origin": origin,
        "today": timezone.localdate(),
    })


@module_required("deliveries")
def delivery_detail(request, pk):
    shipment = get_object_or_404(
        Shipment.objects.select_related("po", "po__customer"),
        pk=pk, provider="local", is_deleted=False)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "deliver":
            photo = request.FILES.get("proof_photo")
            if photo:
                shipment.proof_photo = photo
            shipment.delivery_notes = request.POST.get("notes", "")[:500]
            shipment.status = "delivered"
            shipment.delivered_at = timezone.now()
            shipment.delivered_by = request.user
            shipment.ship_date = shipment.ship_date or timezone.localdate()
            shipment.save()
            shipment.po.mark_shipped()
            log_activity(request.user, "delivered", shipment,
                         note="photo attached" if photo else "no photo")
            messages.success(request, f"{shipment.po.number} marked delivered"
                             + (" with photo proof." if photo else "."))
            return redirect(f"{'/deliveries/'}?date={shipment.ship_date}")
        if action == "reschedule":
            when = _parse_date(request.POST.get("date"))
            shipment.ship_date = when
            shipment.status = "pending"
            shipment.save(update_fields=["ship_date", "status"])
            messages.success(request, f"Rescheduled to {when:%b %-d}.")
            return redirect("delivery_detail", pk)

    dest = shipment.maps_destination
    return render(request, "shipping/delivery_detail.html", {
        "shipment": shipment,
        "apple_url": apple_maps_url(dest) if dest else None,
        "maps_url": google_maps_route_url(_warehouse_origin(), [dest]) if dest else None,
    })
