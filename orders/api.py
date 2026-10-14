"""
DRF endpoints powering the warehouse picking interface.
"""
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.models import CompanySettings, log_activity
from core.roles import is_warehouse
from orders.models import PickEvent, POLine, PurchaseOrder


def _check(request):
    if not is_warehouse(request.user):
        return Response({"error": "Not allowed"}, status=status.HTTP_403_FORBIDDEN)
    return None


def _line_payload(line):
    return {
        "id": line.pk,
        "sku": line.product.sku,
        "name": line.product.name,
        "image": line.product.image.url if line.product.image else None,
        "bin": line.product.bin_location,
        "barcode": line.product.barcode_value(),
        "qty_needed": line.qty_ordered,
        "qty_picked": line.qty_picked,
        "short_reason": line.short_reason,
        "complete": line.is_complete,
    }


def _po_payload(po, with_lines=False):
    data = {
        "id": po.pk, "number": po.number,
        "customer": po.customer.business_name,
        "status": po.status,
        "priority": po.priority, "priority_label": po.get_priority_display(),
        "total_units": po.total_units, "picked_units": po.picked_units,
        "progress": po.pick_progress_pct,
        "line_count": po.live_lines.count(),
        "created": timezone.localtime(po.created_at).strftime("%b %-d %-I:%M %p"),
        "notes": po.notes,
    }
    if with_lines:
        lines = sorted(po.live_lines.select_related("product"),
                       key=lambda l: (l.product.bin_location or "~", l.product.sku))
        data["lines"] = [_line_payload(l) for l in lines]
    return data


@api_view(["GET"])
def pick_queue_api(request):
    err = _check(request)
    if err:
        return err
    pos = (PurchaseOrder.objects
           .filter(is_deleted=False,
                   status__in=[PurchaseOrder.CONFIRMED, PurchaseOrder.PICKING])
           .select_related("customer")
           .order_by("priority", "created_at"))
    return Response({"orders": [_po_payload(p) for p in pos]})


@api_view(["GET"])
def pick_detail_api(request, pk):
    err = _check(request)
    if err:
        return err
    po = get_object_or_404(PurchaseOrder, pk=pk, is_deleted=False)
    if po.status == PurchaseOrder.CONFIRMED:
        po.start_picking(user=request.user)
    return Response(_po_payload(po, with_lines=True))


@api_view(["POST"])
def pick_scan_api(request, pk):
    """Scan a barcode: match to an incomplete line and increment."""
    err = _check(request)
    if err:
        return err
    po = get_object_or_404(PurchaseOrder, pk=pk, is_deleted=False,
                           status=PurchaseOrder.PICKING)
    code = (request.data.get("barcode") or "").strip()
    qty = int(request.data.get("qty", 1))
    if not code:
        return Response({"ok": False, "error": "Empty scan."}, status=400)

    lines = [l for l in po.live_lines.select_related("product")
             if l.product.barcode_value() == code or l.product.sku == code
             or l.product.upc == code]
    if not lines:
        return Response({"ok": False, "error": "WRONG ITEM — not on this order.",
                         "code": "wrong_item"}, status=400)
    # Prefer a line that still needs units
    line = next((l for l in lines if l.qty_picked < l.qty_ordered), None)
    if line is None:
        return Response({"ok": False, "error": "Already fully picked.",
                         "code": "overpick", "line_id": lines[0].pk}, status=400)
    if line.qty_picked + qty > line.qty_ordered:
        return Response({"ok": False,
                         "error": f"OVER-PICK blocked: needs {line.qty_ordered}, "
                                  f"already picked {line.qty_picked}.",
                         "code": "overpick", "line_id": line.pk}, status=400)
    line.qty_picked += qty
    if line.short_reason:
        line.short_reason = ""
        line.short_note = ""
    line.save(update_fields=["qty_picked", "short_reason", "short_note"])
    PickEvent.objects.create(line=line, action="scan", qty=qty, barcode=code,
                             user=request.user)
    return Response({"ok": True, "line": _line_payload(line),
                     "progress": po.pick_progress_pct,
                     "all_complete": all(l.is_complete for l in po.live_lines)})


@api_view(["POST"])
def pick_adjust_api(request, line_id):
    """Manual +/- fallback for damaged barcodes."""
    err = _check(request)
    if err:
        return err
    line = get_object_or_404(POLine.objects.select_related("po", "product"),
                             pk=line_id, is_deleted=False,
                             po__status=PurchaseOrder.PICKING)
    delta = int(request.data.get("delta", 0))
    new_qty = line.qty_picked + delta
    if new_qty < 0:
        new_qty = 0
    if new_qty > line.qty_ordered:
        return Response({"ok": False, "error": "Over-pick blocked.",
                         "code": "overpick"}, status=400)
    line.qty_picked = new_qty
    if line.short_reason and new_qty >= line.qty_ordered:
        line.short_reason = ""
        line.short_note = ""
    line.save(update_fields=["qty_picked", "short_reason", "short_note"])
    PickEvent.objects.create(line=line, action="manual_add" if delta >= 0 else "manual_remove",
                             qty=delta, user=request.user)
    po = line.po
    return Response({"ok": True, "line": _line_payload(line),
                     "progress": po.pick_progress_pct,
                     "all_complete": all(l.is_complete for l in po.live_lines)})


@api_view(["POST"])
def pick_set_qty_api(request, line_id):
    """Type the picked quantity directly (for big lines — no 40 scans needed)."""
    err = _check(request)
    if err:
        return err
    line = get_object_or_404(POLine.objects.select_related("po", "product"),
                             pk=line_id, is_deleted=False,
                             po__status=PurchaseOrder.PICKING)
    try:
        qty = int(request.data.get("qty"))
    except (TypeError, ValueError):
        return Response({"ok": False, "error": "Enter a number."}, status=400)
    if qty < 0:
        qty = 0
    if qty > line.qty_ordered:
        return Response({"ok": False,
                         "error": f"OVER-PICK blocked: order needs {line.qty_ordered}.",
                         "code": "overpick"}, status=400)
    old = line.qty_picked
    line.qty_picked = qty
    if line.short_reason and qty >= line.qty_ordered:
        line.short_reason = ""
        line.short_note = ""
    line.save(update_fields=["qty_picked", "short_reason", "short_note"])
    PickEvent.objects.create(line=line,
                             action="manual_add" if qty >= old else "manual_remove",
                             qty=qty - old, user=request.user)
    po = line.po
    return Response({"ok": True, "line": _line_payload(line),
                     "progress": po.pick_progress_pct,
                     "all_complete": all(l.is_complete for l in po.live_lines)})


@api_view(["POST"])
def pick_short_api(request, line_id):
    err = _check(request)
    if err:
        return err
    line = get_object_or_404(POLine.objects.select_related("po"),
                             pk=line_id, is_deleted=False,
                             po__status=PurchaseOrder.PICKING)
    reason = request.data.get("reason", "oos")
    if reason not in dict(POLine.SHORT_REASONS):
        return Response({"ok": False, "error": "Bad reason."}, status=400)
    line.short_reason = reason
    line.short_note = (request.data.get("note") or "")[:250]
    line.save(update_fields=["short_reason", "short_note"])
    PickEvent.objects.create(line=line, action="short", qty=0, user=request.user)
    po = line.po
    return Response({"ok": True, "line": _line_payload(line),
                     "progress": po.pick_progress_pct,
                     "all_complete": all(l.is_complete for l in po.live_lines)})


@api_view(["POST"])
def pick_complete_api(request, pk):
    err = _check(request)
    if err:
        return err
    po = get_object_or_404(PurchaseOrder, pk=pk, is_deleted=False,
                           status=PurchaseOrder.PICKING)
    try:
        po.complete_pick(user=request.user)
    except ValidationError as e:
        return Response({"ok": False, "error": "; ".join(e.messages)}, status=400)
    # Notify the office
    company = CompanySettings.get()
    if company.alert_email:
        try:
            from django_q.tasks import async_task
            async_task("django.core.mail.send_mail",
                       f"[SFL ERP] {po.number} picked",
                       f"{po.number} for {po.customer.business_name} was picked by "
                       f"{request.user.username}. "
                       f"{po.picked_units}/{po.total_units} units.",
                       None, [company.alert_email])
        except Exception:
            try:
                send_mail(f"[SFL ERP] {po.number} picked",
                          f"{po.number} for {po.customer.business_name} was picked.",
                          None, [company.alert_email], fail_silently=True)
            except Exception:
                pass
    return Response({"ok": True, "status": po.status})
