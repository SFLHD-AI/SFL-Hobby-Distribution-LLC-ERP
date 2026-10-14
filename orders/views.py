from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.csvutils import csv_response
from core.models import CompanySettings, log_activity
from core.pdf import pdf_response
from core.roles import can_see_costs, module_required
from customers.models import Customer
from inventory.models import Product
from orders.forms import POForm
from orders.models import POLine, PurchaseOrder


@module_required("orders")
def po_list(request):
    from invoicing.models import Invoice, InvoiceLine
    live_inv = InvoiceLine.objects.filter(
        po=models.OuterRef("pk"), is_deleted=False).exclude(
        invoice__status=Invoice.VOID)
    qs = (PurchaseOrder.objects.filter(is_deleted=False)
          .select_related("customer")
          .annotate(billed=models.Exists(live_inv)))
    status = request.GET.get("status")
    if status == "unbilled":
        # shipped/finalized orders that still need an invoice
        qs = qs.filter(status__in=[PurchaseOrder.FINALIZED,
                                   PurchaseOrder.SHIPPED], billed=False)
    elif status:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(models.Q(number__icontains=q) |
                       models.Q(customer__business_name__icontains=q) |
                       models.Q(customer_po_ref__icontains=q))
    customer_id = request.GET.get("customer")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    pos = list(qs[:300])

    if request.GET.get("export") == "csv":
        return csv_response(
            "orders.csv",
            ["number", "customer", "status", "created", "units", "subtotal",
             "shipping", "tax", "total"],
            [[p.number, p.customer.business_name, p.status,
              p.created_at.date(), p.total_units, p.subtotal,
              p.shipping_charge if p.charge_shipping else 0,
              p.tax_amount, p.total] for p in pos])

    return render(request, "orders/po_list.html", {
        "pos": pos, "statuses": PurchaseOrder.STATUS_CHOICES,
        "customers": Customer.objects.filter(active=True),
    })



def _addr_map():
    """{customer_id: [{id, label, def}]} for the live ship-to dropdown."""
    from customers.models import ShippingAddress
    out = {}
    for a in ShippingAddress.objects.filter(is_deleted=False).order_by("-is_default", "label"):
        out.setdefault(str(a.customer_id), []).append(
            {"id": a.pk, "label": f"{a.label}: {a.line1}, {a.city} {a.state}",
             "def": a.is_default})
    return out


@module_required("orders")
def po_create(request):
    initial = {}
    if request.GET.get("customer"):
        initial["customer"] = request.GET["customer"]
    form = POForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        po = form.save(commit=False)
        po.created_by = request.user
        po.save()
        log_activity(request.user, "create", po)
        messages.success(request, f"{po.number} created — add items below.")
        return redirect("po_detail", po.pk)
    return render(request, "orders/po_form.html",
                  {"form": form, "po": None, "addr_map": _addr_map()})


@module_required("orders")
def po_edit(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if not po.is_editable:
        messages.error(request, "Order can no longer be edited.")
        return redirect("po_detail", pk)
    form = POForm(request.POST or None, instance=po)
    if request.method == "POST" and form.is_valid():
        changes = {f: str(form.cleaned_data.get(f)) for f in form.changed_data}
        form.save()
        log_activity(request.user, "update", po, changes=changes)
        return redirect("po_detail", pk)
    return render(request, "orders/po_form.html",
                  {"form": form, "po": po, "addr_map": _addr_map()})


@module_required("orders")
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related("customer", "ship_to"), pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "add_line" and po.is_editable:
                _add_line(request, po)
            elif action == "scan_add" and po.is_editable:
                _scan_add(request, po)
            elif action == "update_line" and po.is_editable:
                line = get_object_or_404(POLine, pk=request.POST["line_id"], po=po)
                line.qty_ordered = int(request.POST.get("qty", line.qty_ordered))
                line.unit_price = Decimal(request.POST.get("price", line.unit_price))
                line.discount_pct = Decimal(request.POST.get("disc", line.discount_pct) or 0)
                line.save()
            elif action == "del_line" and po.is_editable:
                line = get_object_or_404(POLine, pk=request.POST["line_id"], po=po)
                line.delete(user=request.user)
            elif action == "confirm":
                po.confirm(user=request.user)
                messages.success(request, f"{po.number} confirmed — inventory committed.")
            elif action == "start_picking":
                po.start_picking(user=request.user)
                messages.success(request, "Sent to pick queue.")
            elif action == "finalize":
                po.finalize(user=request.user)
                messages.success(request, "Finalized — stock decremented and costs snapshotted.")
            elif action == "cancel":
                po.cancel(user=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, "Order cancelled; commitments released.")
            elif action == "unfinalize":
                from core.roles import is_admin
                if not is_admin(request.user):
                    raise ValidationError("Only an admin can un-finalize an order.")
                po.unfinalize(user=request.user, reason=request.POST.get("reason", ""))
                messages.success(request,
                                 "Finalization reversed — stock returned and cost "
                                 "snapshots cleared. Fix your costs, then finalize again.")
            elif action == "backorder":
                child = po.create_backorder(user=request.user)
                messages.success(request, f"Backorder {child.number} created.")
                return redirect("po_detail", child.pk)
        except (ValidationError, ValueError) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)
        return redirect("po_detail", pk)

    lines = po.live_lines.select_related("product")
    has_shorts = any(l.qty_ordered - l.fulfilled_qty > 0 for l in lines) \
        if po.status in (PurchaseOrder.PICKED, PurchaseOrder.FINALIZED,
                         PurchaseOrder.INVOICED) else False
    cert_warning = None
    if not po.customer.has_valid_certificate and po.status in (
            PurchaseOrder.DRAFT, PurchaseOrder.CONFIRMED, PurchaseOrder.PICKING,
            PurchaseOrder.PICKED):
        cert_warning = po.customer.no_cert_policy
    return render(request, "orders/po_detail.html", {
        "po": po, "lines": lines,
        "products": Product.objects.filter(active=True),
        "show_costs": can_see_costs(request.user),
        "has_shorts": has_shorts, "cert_warning": cert_warning,
        "existing_backorders": po.backorders.filter(is_deleted=False),
    })


def _add_line(request, po):
    product = get_object_or_404(Product, pk=request.POST["product"])
    qty = int(request.POST.get("qty", 1))
    price_raw = request.POST.get("price", "").strip()
    price = Decimal(price_raw) if price_raw else po.customer.price_for(product)
    existing = po.live_lines.filter(product=product, unit_price=price).first()
    if existing:
        existing.qty_ordered += qty
        existing.save(update_fields=["qty_ordered"])
    else:
        POLine.objects.create(po=po, product=product, qty_ordered=qty,
                              unit_price=price, description=product.name)


def _scan_add(request, po):
    code = request.POST.get("barcode", "").strip()
    if not code:
        return
    product = (Product.objects.filter(upc=code).first()
               or Product.objects.filter(sku__iexact=code).first())
    if not product:
        raise ValidationError(f"No product matches barcode/SKU “{code}”.")
    price = po.customer.price_for(product)
    existing = po.live_lines.filter(product=product).first()
    if existing:
        existing.qty_ordered += 1
        existing.save(update_fields=["qty_ordered"])
    else:
        POLine.objects.create(po=po, product=product, qty_ordered=1,
                              unit_price=price, description=product.name)


@module_required("orders")
def po_pdf(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    company = CompanySettings.get()
    logo = company.logo.path if company.logo else None
    return pdf_response("pdf/po_confirmation.html", {
        "po": po, "company": company, "logo_url": logo,
        "lines": po.live_lines.select_related("product"),
    }, f"{po.number}.pdf")


@module_required("orders")
def packing_slip_pdf(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    company = CompanySettings.get()
    logo = company.logo.path if company.logo else None
    return pdf_response("pdf/packing_slip.html", {
        "po": po, "company": company, "logo_url": logo,
        "lines": po.live_lines.select_related("product").order_by("product__bin_location"),
    }, f"packing_slip_{po.number}.pdf")


@module_required("orders")
def product_search_json(request):
    """Autocomplete for the PO line editor; also resolves scans."""
    q = (request.GET.get("q") or "").strip()
    customer_id = request.GET.get("customer")
    customer = Customer.objects.filter(pk=customer_id).first() if customer_id else None
    if not q:
        return JsonResponse({"results": []})
    qs = Product.objects.filter(active=True).filter(
        models.Q(sku__icontains=q) | models.Q(name__icontains=q) |
        models.Q(upc__icontains=q))[:15]
    results = []
    for p in qs:
        results.append({
            "id": p.pk, "sku": p.sku, "name": p.name,
            "available": p.qty_available, "on_hand": p.qty_on_hand,
            "price": str(customer.price_for(p) if customer else p.default_price),
        })
    return JsonResponse({"results": results})
