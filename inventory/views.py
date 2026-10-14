import csv
import io
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render

from core.csvutils import csv_response
from core.models import log_activity
from core.roles import can_see_costs, module_required
from inventory.forms import (ReceiptBillForm, AdjustmentForm, CycleCountForm, PriceListForm,
                             PriceListItemForm, ProductForm, ReceiptForm,
                             ReceiptLineForm)
from inventory.models import (Brand, Category, CycleCount, CycleCountLine,
                              PriceList, PriceListItem, Product, Receipt,
                              ReceiptLine, StockAdjustment, StockMovement)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@module_required("inventory")
def product_list(request):
    qs = (Product.objects.select_related("category", "brand")
          .order_by("sku"))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(models.Q(sku__icontains=q) | models.Q(name__icontains=q) |
                       models.Q(upc__icontains=q) | models.Q(bin_location__icontains=q))
    cat = request.GET.get("category")
    if cat:
        qs = qs.filter(category_id=cat)
    brand = request.GET.get("brand")
    if brand:
        qs = qs.filter(brand_id=brand)
    if request.GET.get("active") == "1":
        qs = qs.filter(active=True)
    products = list(qs)
    if request.GET.get("low") == "1":
        products = [p for p in products if p.is_low_stock and p.active]

    if request.GET.get("export") == "csv":
        show_costs = can_see_costs(request.user)
        header = ["sku", "name", "description", "category", "brand", "upc",
                  "case_pack_qty", "unit_of_measure", "weight_lbs",
                  "default_price", "msrp",
                  "reorder_point", "bin_location", "active",
                  "qty_on_hand", "qty_committed", "qty_available"]
        if show_costs:
            header += ["avg_cost", "inventory_value"]
        rows = []
        for p in products:
            row = [p.sku, p.name, p.description, p.category or "", p.brand or "",
                   p.upc, p.case_pack_qty, p.unit_of_measure,
                   p.weight_lbs if p.weight_lbs is not None else "",
                   p.default_price,
                   p.msrp or "", p.reorder_point, p.bin_location,
                   "1" if p.active else "0",
                   p.qty_on_hand, p.qty_committed, p.qty_available]
            if show_costs:
                row += [p.avg_cost, p.inventory_value]
            rows.append(row)
        return csv_response("products.csv", header, rows)

    return render(request, "inventory/product_list.html", {
        "products": products, "categories": Category.objects.all(),
        "brands": Brand.objects.all(),
    })


@module_required("inventory")
def product_import(request):
    """CSV upsert keyed on SKU."""
    if request.method == "POST" and request.FILES.get("file"):
        decoded = request.FILES["file"].read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        created = updated = errors = 0
        for i, row in enumerate(reader, start=2):
            sku = (row.get("sku") or "").strip()
            if not sku:
                errors += 1
                continue
            try:
                defaults = {}
                for field in ["name", "description", "upc", "bin_location"]:
                    if row.get(field) is not None:
                        defaults[field] = row[field].strip()
                for field in ["case_pack_qty", "reorder_point"]:
                    if row.get(field):
                        defaults[field] = int(row[field])
                for field in ["default_price", "msrp", "weight_lbs"]:
                    if row.get(field):
                        defaults[field] = Decimal(row[field])
                if row.get("unit_of_measure"):
                    defaults["unit_of_measure"] = row["unit_of_measure"].strip().lower()
                if row.get("category"):
                    defaults["category"], _ = Category.objects.get_or_create(
                        name=row["category"].strip())
                if row.get("brand"):
                    defaults["brand"], _ = Brand.objects.get_or_create(
                        name=row["brand"].strip())
                if row.get("active") not in (None, ""):
                    defaults["active"] = row["active"].strip() in ("1", "true", "yes")
                obj, was_created = Product.all_objects.update_or_create(
                    sku=sku, defaults=defaults)
                if obj.is_deleted:
                    obj.restore()
                # Opening stock columns: opening_qty (+ optional opening_unit_cost)
                # sets on-hand to that count via an adjustment at your cost.
                if row.get("opening_qty"):
                    target = int(row["opening_qty"])
                    delta = target - obj.qty_on_hand
                    if delta != 0:
                        cost = (Decimal(row["opening_unit_cost"])
                                if row.get("opening_unit_cost") else None)
                        StockAdjustment.create_and_apply(
                            product=obj, qty_delta=delta, reason="opening",
                            unit_cost=cost, user=request.user,
                            note="Opening stock via CSV import")
                # set_avg_cost: force the weighted average cost to this value
                # regardless of quantity (audited zero-qty revaluation).
                if row.get("set_avg_cost"):
                    obj.refresh_from_db()
                    obj.revalue_cost(Decimal(row["set_avg_cost"]),
                                     user=request.user,
                                     note="Cost set via CSV import")
                created += was_created
                updated += (not was_created)
            except (ValueError, InvalidOperation):
                errors += 1
        log_activity(request.user, "product_import", note=f"{created} created, {updated} updated")
        messages.success(request, f"Import done: {created} created, {updated} updated, {errors} rows skipped.")
    return redirect("product_list")


@module_required("inventory")
def product_create(request):
    form = ProductForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        from django.urls import reverse
        from django.utils.html import format_html
        product = form.save()
        log_activity(request.user, "create", product)
        label_url = f"{reverse('product_labels')}?id={product.pk}&copies=1&mode=sku"
        messages.info(request, format_html(
            '🏷 <a href="{}" target="_blank" class="underline font-semibold">'
            'Print the 2×1 barcode label for {}</a>', label_url, product.sku))
        qty = form.cleaned_data.get("initial_qty")
        cost = form.cleaned_data.get("initial_unit_cost")
        if qty:
            StockAdjustment.create_and_apply(
                product=product, qty_delta=qty, reason="opening",
                unit_cost=cost, user=request.user,
                note="Opening stock entered at product creation")
            messages.success(request,
                             f"{product.sku} created with {qty} on hand"
                             + (f" at ${cost}/unit." if cost is not None else "."))
        else:
            messages.success(request, f"{product.sku} created.")
        return redirect("product_detail", product.pk)
    return render(request, "inventory/product_form.html", {"form": form, "product": None})


@module_required("inventory")
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    if request.method == "POST" and form.is_valid():
        changes = {f: str(form.cleaned_data.get(f)) for f in form.changed_data}
        form.save()
        log_activity(request.user, "update", product, changes=changes)
        messages.success(request, f"{product.sku} saved.")
        return redirect("product_detail", product.pk)
    return render(request, "inventory/product_form.html", {"form": form, "product": product})


@module_required("inventory")
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        # Safety: never delete a product that's still on an open order —
        # that would corrupt picking/finalization.
        from orders.models import POLine, PurchaseOrder
        open_orders = (POLine.objects
                       .filter(product=product, is_deleted=False,
                               po__is_deleted=False,
                               po__status__in=[PurchaseOrder.DRAFT,
                                               PurchaseOrder.CONFIRMED,
                                               PurchaseOrder.PICKING,
                                               PurchaseOrder.PICKED,
                                               PurchaseOrder.FINALIZED,
                                               PurchaseOrder.INVOICED])
                       .select_related("po")
                       .values_list("po__number", flat=True).distinct())
        open_orders = list(open_orders)
        if open_orders:
            messages.error(request,
                           f"Can't delete {product.sku} — it's on open order(s): "
                           f"{', '.join(open_orders[:5])}. Ship or cancel them first.")
            return redirect("product_detail", pk)
        product.delete(user=request.user)
        log_activity(request.user, "delete", product)
        messages.success(request, f"{product.sku} deleted. Its history stays in "
                                  "the ledger and it can be restored by an admin.")
        return redirect("product_list")
    return redirect("product_detail", pk)


@module_required("inventory")
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    adj_form = AdjustmentForm()
    if request.method == "POST" and request.POST.get("action") == "revalue":
        if not can_see_costs(request.user):
            messages.error(request, "You don't have permission to change costs.")
            return redirect("product_detail", pk)
        try:
            new_cost = Decimal(request.POST.get("new_avg_cost", "").strip())
            if new_cost < 0:
                raise InvalidOperation
        except InvalidOperation:
            messages.error(request, "Enter a valid cost.")
            return redirect("product_detail", pk)
        product.revalue_cost(new_cost, user=request.user,
                             note=request.POST.get("note", ""))
        messages.success(request, f"Average cost set to ${new_cost}.")
        return redirect("product_detail", pk)
    if request.method == "POST":
        adj_form = AdjustmentForm(request.POST)
        if adj_form.is_valid():
            try:
                unit_cost = adj_form.cleaned_data.get("unit_cost")
                if unit_cost is not None and not can_see_costs(request.user):
                    unit_cost = None  # cost entry reserved for cost-permitted users
                StockAdjustment.create_and_apply(
                    product=product, user=request.user,
                    qty_delta=adj_form.cleaned_data["qty_delta"],
                    reason=adj_form.cleaned_data["reason"],
                    unit_cost=unit_cost,
                    note=adj_form.cleaned_data["note"])
                messages.success(request, "Adjustment applied.")
                return redirect("product_detail", pk)
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
    movements = product.movements.select_related("user")[:200]
    return render(request, "inventory/product_detail.html", {
        "product": product, "movements": movements, "adj_form": adj_form,
        "show_costs": can_see_costs(request.user),
    })


@module_required("inventory")
def toggle_low_stock_mute(request, pk):
    """Mute/unmute low-stock alerts for one product (dashboard 🔕 button)."""
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        product.low_stock_alert_muted = not product.low_stock_alert_muted
        product.save(update_fields=["low_stock_alert_muted"])
        log_activity(request.user,
                     "low_stock_muted" if product.low_stock_alert_muted else "low_stock_unmuted",
                     product)
        messages.success(request,
                         f"Low-stock alerts {'muted' if product.low_stock_alert_muted else 'back on'} "
                         f"for {product.sku}.")
    ref = request.META.get("HTTP_REFERER")
    return redirect(ref) if ref else redirect("product_detail", pk=pk)


@module_required("inventory")
def product_labels(request):
    """Printable 2" x 1" Code 128 labels for SKUs and/or bin locations."""
    import base64

    import barcode
    from barcode.writer import ImageWriter

    ids = request.GET.getlist("id") or request.POST.getlist("id")
    copies = int(request.GET.get("copies", request.POST.get("copies", 1)) or 1)
    mode = request.GET.get("mode", request.POST.get("mode", "sku"))
    products = Product.objects.filter(pk__in=ids) if ids else Product.objects.none()

    labels = []
    seen_bins = set()
    for p in products:
        if mode == "bin":
            if not p.bin_location or p.bin_location in seen_bins:
                continue
            seen_bins.add(p.bin_location)
            value, title, sub = p.bin_location, p.bin_location, "BIN LOCATION"
        else:
            value, title, sub = p.barcode_value(), p.sku, p.name[:40]
        # High-DPI PNG, bars only (the SKU is printed by the template),
        # quiet zone preserved for reliable scanning on 2" labels.
        code = barcode.get("code128", value, writer=ImageWriter())
        buf = io.BytesIO()
        code.write(buf, options={"module_height": 12.0, "write_text": False,
                                 "quiet_zone": 2.0, "dpi": 300})
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        for _ in range(copies):
            labels.append({"img": f"data:image/png;base64,{img_b64}",
                           "title": title, "sub": sub})
    return render(request, "inventory/labels.html", {"labels": labels})


# ---------------------------------------------------------------------------
# Receiving (supplier orders)
# ---------------------------------------------------------------------------
@module_required("receiving")
def receipt_list(request):
    qs = Receipt.objects.select_related("supplier")
    status = request.GET.get("status")
    if status == "open":
        qs = qs.filter(posted=False)
    elif status == "posted":
        qs = qs.filter(posted=True)
    elif status == "unpaid":
        qs = [r for r in qs.filter(posted=True).prefetch_related("supplier_payments")
              if not r.is_paid]
    return render(request, "inventory/receipt_list.html", {"receipts": qs[:200]})


@module_required("receiving")
def receipt_create(request):
    form = ReceiptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        receipt = form.save(commit=False)
        receipt.created_by = request.user
        receipt.save()
        log_activity(request.user, "create", receipt)
        return redirect("receipt_detail", receipt.pk)
    return render(request, "inventory/receipt_form.html", {"form": form})


@module_required("receiving")
def receipt_detail(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    line_form = ReceiptLineForm()
    header_form = ReceiptForm(instance=receipt)
    bill_form = ReceiptBillForm(instance=receipt)
    if request.method == "POST" and request.POST.get("action") == "save_bill":
        # Bill / payment details stay editable after receiving
        bill_form = ReceiptBillForm(request.POST, instance=receipt)
        if bill_form.is_valid():
            bill_form.save()
            log_activity(request.user, "supplier_bill_updated", receipt,
                         changes={f: str(bill_form.cleaned_data.get(f))
                                  for f in bill_form.changed_data})
            messages.success(request, "Bill details saved.")
            return redirect("receipt_detail", pk)
        messages.error(request, "Bill details not saved — check the fields.")
    elif request.method == "POST" and not receipt.posted:
        action = request.POST.get("action")
        if action == "add_line":
            line_form = ReceiptLineForm(request.POST)
            if line_form.is_valid():
                line = line_form.save(commit=False)
                line.receipt = receipt
                line.save()
                return redirect("receipt_detail", pk)
        elif action == "del_line":
            line = get_object_or_404(ReceiptLine, pk=request.POST.get("line_id"),
                                     receipt=receipt)
            line.delete(user=request.user)
            return redirect("receipt_detail", pk)
        elif action == "save_header":
            header_form = ReceiptForm(request.POST, instance=receipt)
            if header_form.is_valid():
                header_form.save()
                messages.success(request, "Saved.")
                return redirect("receipt_detail", pk)
        elif action == "post":
            try:
                receipt.post(user=request.user)
                messages.success(request, "Received! Stock and average costs updated.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            return redirect("receipt_detail", pk)
    linked_payments = (receipt.supplier_payments.filter(is_deleted=False)
                       .exclude(status="ignored").select_related("account").order_by("-date"))
    return render(request, "inventory/receipt_detail.html", {
        "receipt": receipt, "line_form": line_form, "header_form": header_form,
        "bill_form": bill_form, "linked_payments": linked_payments,
        "lines": receipt.lines.filter(is_deleted=False).select_related("product"),
        "products": Product.objects.filter(active=True),
    })


# ---------------------------------------------------------------------------
# Cycle counts
# ---------------------------------------------------------------------------
@module_required("receiving")
def cyclecount_list(request):
    return render(request, "inventory/cyclecount_list.html",
                  {"counts": CycleCount.objects.all()[:100]})


@module_required("receiving")
def cyclecount_create(request):
    form = CycleCountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cc = form.save(commit=False)
        cc.created_by = request.user
        cc.save()
        qs = Product.objects.filter(active=True)
        scope = form.cleaned_data["scope"]
        if scope == "category" and form.cleaned_data["category"]:
            qs = qs.filter(category=form.cleaned_data["category"])
        elif scope == "bin" and form.cleaned_data["bin_prefix"]:
            qs = qs.filter(bin_location__istartswith=form.cleaned_data["bin_prefix"])
        CycleCountLine.objects.bulk_create([
            CycleCountLine(cycle_count=cc, product=p, snapshot_qty=p.qty_on_hand)
            for p in qs])
        log_activity(request.user, "create", cc)
        return redirect("cyclecount_detail", cc.pk)
    return render(request, "inventory/cyclecount_form.html", {"form": form})


@module_required("receiving")
def cyclecount_detail(request, pk):
    cc = get_object_or_404(CycleCount, pk=pk)
    if request.method == "POST" and cc.status == "open":
        action = request.POST.get("action")
        if action == "save":
            for line in cc.lines.all():
                raw = request.POST.get(f"count_{line.pk}", "").strip()
                line.counted_qty = int(raw) if raw != "" else None
                line.save(update_fields=["counted_qty"])
            messages.success(request, "Counts saved.")
            return redirect("cyclecount_detail", pk)
        elif action == "apply":
            for line in cc.lines.all():
                raw = request.POST.get(f"count_{line.pk}", "").strip()
                line.counted_qty = int(raw) if raw != "" else None
                line.save(update_fields=["counted_qty"])
            try:
                cc.apply(user=request.user)
                messages.success(request, "Cycle count applied — adjustments written to the ledger.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            return redirect("cyclecount_detail", pk)
        elif action == "cancel":
            cc.status = "cancelled"
            cc.save(update_fields=["status"])
            return redirect("cyclecount_list")
    return render(request, "inventory/cyclecount_detail.html", {
        "cc": cc, "lines": cc.lines.select_related("product").order_by("product__bin_location", "product__sku")})


# ---------------------------------------------------------------------------
# Price lists
# ---------------------------------------------------------------------------
@module_required("inventory")
def pricelist_list(request):
    return render(request, "inventory/pricelist_list.html",
                  {"lists": PriceList.objects.all()})


@module_required("inventory")
def pricelist_create(request):
    form = PriceListForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        pl = form.save()
        log_activity(request.user, "create", pl)
        return redirect("pricelist_detail", pl.pk)
    return render(request, "inventory/pricelist_form.html", {"form": form})


@module_required("inventory")
def pricelist_detail(request, pk):
    pl = get_object_or_404(PriceList, pk=pk)
    item_form = PriceListItemForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            item_form = PriceListItemForm(request.POST)
            if item_form.is_valid():
                PriceListItem.objects.update_or_create(
                    price_list=pl, product=item_form.cleaned_data["product"],
                    is_deleted=False,
                    defaults={"price": item_form.cleaned_data["price"]})
                return redirect("pricelist_detail", pk)
        elif action == "del":
            item = get_object_or_404(PriceListItem, pk=request.POST.get("item_id"), price_list=pl)
            item.delete(user=request.user)
            return redirect("pricelist_detail", pk)
        elif action == "rename":
            form = PriceListForm(request.POST, instance=pl)
            if form.is_valid():
                form.save()
                return redirect("pricelist_detail", pk)
    return render(request, "inventory/pricelist_detail.html", {
        "pl": pl, "items": pl.items.filter(is_deleted=False).select_related("product"),
        "item_form": item_form, "form": PriceListForm(instance=pl),
        "products": Product.objects.filter(active=True),
    })
