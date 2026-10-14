from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import log_activity
from core.roles import module_required
from orders.models import PurchaseOrder
from shipping.models import PackageItem, Shipment, ShipmentPackage
from shipping.providers import (ProviderError, ProviderNotConfigured,
                                build_batch_csv, get_provider)
from shipping.tasks import email_tracking, sync_shipstation_tracking


@module_required("shipping")
def shipment_list(request):
    shipments = (Shipment.objects.filter(is_deleted=False)
                 .select_related("po", "po__customer")[:200])
    shippable = (PurchaseOrder.objects
                 .filter(is_deleted=False,
                         status__in=[PurchaseOrder.PICKED, PurchaseOrder.FINALIZED,
                                     PurchaseOrder.INVOICED])
                 .exclude(shipments__is_deleted=False,
                          shipments__tracking_number__gt="")
                 .select_related("customer").distinct())
    ss = get_provider("shipstation")
    return render(request, "shipping/shipment_list.html", {
        "shipments": shipments, "shippable": shippable,
        "shipstation_ok": ss.is_configured(),
    })


@module_required("shipping")
def shipment_create(request, po_pk):
    po = get_object_or_404(PurchaseOrder, pk=po_pk, is_deleted=False)
    if request.method == "POST":
        provider_key = request.POST.get("provider", "manual")
        shipment = Shipment.objects.create(
            po=po, provider=provider_key,
            service=request.POST.get("service", ""),
            weight_lbs=request.POST.get("weight_lbs") or None,
            length_in=request.POST.get("length_in") or None,
            width_in=request.POST.get("width_in") or None,
            height_in=request.POST.get("height_in") or None,
            ship_to_text=po.ship_to.as_text() if po.ship_to else "",
            created_by=request.user)
        log_activity(request.user, "shipment_created", shipment)

        if request.POST.get("push") and provider_key in ("shipstation", "uship"):
            provider = get_provider(provider_key)
            try:
                external_id = provider.push_order(shipment)
                shipment.external_id = external_id
                shipment.status = "pushed"
                shipment.save(update_fields=["external_id", "status"])
                messages.success(request,
                                 f"Pushed to {provider.label} (ref {external_id}). "
                                 f"Tracking will sync back automatically.")
            except ProviderNotConfigured as e:
                messages.warning(request, f"{e} Shipment saved for manual entry.")
            except ProviderError as e:
                messages.error(request, str(e))
        else:
            messages.success(request, "Shipment saved — paste tracking/cost when you have the label.")
        return redirect("shipment_detail", shipment.pk)

    return render(request, "shipping/shipment_form.html", {
        "po": po,
        "providers": Shipment.PROVIDERS,
        "shipstation_ok": get_provider("shipstation").is_configured(),
        "uship_ok": get_provider("uship").is_configured(),
    })


@module_required("shipping")
def shipment_detail(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("po", "po__customer"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "manual_update":
            shipment.tracking_number = request.POST.get("tracking_number", "").strip()
            shipment.cost = request.POST.get("cost") or None
            shipment.service = request.POST.get("service", shipment.service)
            shipment.ship_date = request.POST.get("ship_date") or None
            if shipment.tracking_number:
                shipment.status = "shipped"
                shipment.po.mark_shipped()
            shipment.save()
            log_activity(request.user, "shipment_updated", shipment,
                         note=shipment.tracking_number)
            if request.POST.get("email_customer") and shipment.tracking_number:
                shipment.tracking_emailed_at = None
                email_tracking(shipment)
                messages.success(request, "Saved and tracking emailed to the customer.")
            else:
                messages.success(request, "Saved.")
        elif action == "delete":
            shipment.delete(user=request.user)
            messages.success(request, "Shipment deleted (recoverable).")
            return redirect("shipment_list")
        # ---- package builder ----
        elif action == "add_package":
            n = shipment.packages.filter(is_deleted=False).count() + 1
            ShipmentPackage.objects.create(
                shipment=shipment, number=n,
                length_in=request.POST.get("length_in") or None,
                width_in=request.POST.get("width_in") or None,
                height_in=request.POST.get("height_in") or None)
            messages.success(request, f"Box {n} added — assign items to it below.")
        elif action == "del_package":
            pkg = get_object_or_404(ShipmentPackage, pk=request.POST["package_id"],
                                    shipment=shipment)
            pkg.hard_delete()
        elif action == "pkg_dims":
            pkg = get_object_or_404(ShipmentPackage, pk=request.POST["package_id"],
                                    shipment=shipment)
            pkg.length_in = request.POST.get("length_in") or None
            pkg.width_in = request.POST.get("width_in") or None
            pkg.height_in = request.POST.get("height_in") or None
            if request.POST.get("weight_lbs"):
                pkg.weight_lbs = request.POST["weight_lbs"]
            pkg.save()
        elif action == "pkg_add_item":
            pkg = get_object_or_404(ShipmentPackage, pk=request.POST["package_id"],
                                    shipment=shipment)
            from orders.models import POLine
            line = get_object_or_404(POLine, pk=request.POST["line_id"],
                                     po=shipment.po)
            qty = int(request.POST.get("qty", 1))
            already = sum(i.qty for i in line.package_items
                          .filter(package__shipment=shipment,
                                  package__is_deleted=False))
            remaining = line.fulfilled_qty - already
            if qty > remaining:
                messages.error(request, f"Only {remaining} of {line.product.sku} left to box.")
            else:
                item, created = PackageItem.objects.get_or_create(
                    package=pkg, po_line=line, defaults={"qty": qty})
                if not created:
                    item.qty += qty
                    item.save(update_fields=["qty"])
                pkg.refresh_weight()
        elif action == "pkg_del_item":
            item = get_object_or_404(PackageItem, pk=request.POST["item_id"],
                                     package__shipment=shipment)
            pkg = item.package
            item.delete()
            pkg.refresh_weight()
        # ---- Shippo: live rates + label purchase ----
        elif action == "shippo_rates":
            provider = get_provider("shippo")
            try:
                rates = provider.get_rates(shipment)
                request.session[f"shippo_rates_{pk}"] = rates
                messages.success(request, f"{len(rates)} rates found — pick one below.")
            except (ProviderNotConfigured, ProviderError) as e:
                messages.error(request, str(e))
        elif action == "shippo_buy":
            provider = get_provider("shippo")
            try:
                result = provider.buy_label(request.POST["rate_id"])
                shipment.provider = "shippo"
                shipment.tracking_number = result["tracking_number"]
                shipment.label_url = result["label_url"]
                shipment.service = request.POST.get("service", shipment.service)
                if request.POST.get("amount"):
                    shipment.cost = request.POST["amount"]
                shipment.ship_date = timezone.localdate()
                shipment.status = "label_created"
                shipment.save()
                shipment.po.mark_shipped()
                request.session.pop(f"shippo_rates_{pk}", None)
                log_activity(request.user, "shippo_label_purchased", shipment,
                             note=f"{shipment.service} ${shipment.cost} "
                                  f"{shipment.tracking_number}")
                email_tracking(shipment)
                messages.success(request,
                                 f"Label purchased! Tracking {shipment.tracking_number} "
                                 f"— print the label below.")
            except (ProviderNotConfigured, ProviderError) as e:
                messages.error(request, str(e))
        return redirect("shipment_detail", pk)

    packages = shipment.packages.filter(is_deleted=False).prefetch_related(
        "items__po_line__product")
    lines = shipment.po.live_lines.select_related("product")
    boxed = {}
    for p in packages:
        for i in p.items.all():
            boxed[i.po_line_id] = boxed.get(i.po_line_id, 0) + i.qty
    line_rows = [{"line": l, "boxed": boxed.get(l.pk, 0),
                  "remaining": l.fulfilled_qty - boxed.get(l.pk, 0)}
                 for l in lines if l.fulfilled_qty > 0]
    po_weight = shipment.po.total_weight_lbs
    return render(request, "shipping/shipment_detail.html", {
        "shipment": shipment, "packages": packages, "line_rows": line_rows,
        "po_weight": po_weight,
        "all_boxed": all(r["remaining"] <= 0 for r in line_rows) if line_rows else False,
        "shippo_ok": get_provider("shippo").is_configured(),
        "shippo_rates": request.session.get(f"shippo_rates_{pk}"),
    })


@module_required("shipping")
def sync_now(request):
    result = sync_shipstation_tracking()
    messages.info(request, f"ShipStation sync: {result}")
    return redirect("shipment_list")


@module_required("shipping")
def pirateship_export(request):
    """Batch CSV of selected POs in Pirate Ship import format."""
    if request.method == "POST":
        po_ids = request.POST.getlist("po_ids")
        pos = PurchaseOrder.objects.filter(pk__in=po_ids, is_deleted=False)
        shipments = []
        for po in pos:
            shipment = (po.shipments.filter(is_deleted=False, provider="pirateship",
                                            tracking_number="").first()
                        or Shipment.objects.create(
                            po=po, provider="pirateship",
                            ship_to_text=po.ship_to.as_text() if po.ship_to else "",
                            created_by=request.user))
            shipments.append(shipment)
        csv_text = build_batch_csv(shipments)
        log_activity(request.user, "pirateship_export",
                     note=f"{len(shipments)} orders")
        resp = HttpResponse(csv_text, content_type="text/csv")
        resp["Content-Disposition"] = ('attachment; filename="pirateship_batch_'
                                       f'{timezone.localdate()}.csv"')
        return resp
    return redirect("shipment_list")
