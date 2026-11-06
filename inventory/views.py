import csv
import io
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from urllib.parse import urlencode
from django.shortcuts import get_object_or_404, redirect, render

from core.csvutils import csv_response
from core.models import log_activity
from core.roles import can_see_costs, module_required
from inventory.forms import (ReceiptBillForm, ReceiptPackageForm, AdjustmentForm, CycleCountForm, PriceListForm,
                             PriceListItemForm, ProductForm, ReceiptForm,
                             ReceiptLineForm)
from inventory.models import (Brand, Category, CycleCount, CycleCountLine,
                              PriceList, PriceListItem, Product, Receipt,
                              ReceiptLine, StockAdjustment, StockMovement, Supplier)


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
                       models.Q(upc__icontains=q) | models.Q(bin_location__icontains=q) |
                       models.Q(barcodes__code__icontains=q)).distinct()
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


def _maybe_refresh_market(request, product):
    """Pull the TCGplayer price as soon as an id is saved (sealed product only)."""
    if not product.tcgplayer_id:
        return
    from inventory.pricing import refresh_product
    if not product.market_eligible:
        messages.info(request, f"TCGplayer id saved, but market prices only apply to the "
                               f"\"{__import__('inventory.pricing', fromlist=['x']).eligible_category_name()}\" category.")
        return
    ok, msg = refresh_product(product)
    (messages.success if ok else messages.warning)(request, msg)


@module_required("inventory")
def product_create(request):
    form = ProductForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        from django.urls import reverse
        from django.utils.html import format_html
        product = form.save()
        form.save_barcodes(product)
        _maybe_refresh_market(request, product)
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
        form.save_barcodes(product)
        _maybe_refresh_market(request, product)
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
    if request.method == "POST" and request.POST.get("action") == "refresh_market":
        from inventory.pricing import refresh_product
        ok, msg = refresh_product(product)
        (messages.success if ok else messages.warning)(request, msg)
        return redirect("product_detail", pk)
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
    incoming = list(product.incoming_lines())
    return render(request, "inventory/product_detail.html", {
        "product": product, "movements": movements, "adj_form": adj_form,
        "show_costs": can_see_costs(request.user),
        "incoming": incoming, "incoming_summary": product.incoming_summary,
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
    if request.method == "POST" and request.POST.get("action") == "refresh_tracking":
        from inventory.tracking import refresh_receipt
        from shipping.providers import get_provider
        provider = get_provider("shippo")
        if not provider.is_configured():
            messages.warning(request, "Add a Shippo token in Settings to refresh tracking automatically; "
                                      "the tracking links still open the carrier's page.")
            return redirect(request.get_full_path())
        open_tracked = (Receipt.objects.filter(is_deleted=False, posted=False, packages__isnull=False)
                        .exclude(packages__status="DELIVERED").distinct())
        ok = failed = 0
        for r in open_tracked:
            good, _ = refresh_receipt(r, provider)
            ok += good
            failed += (not good)
        if ok or failed:
            messages.success(request, f"Tracking refreshed on {ok} order{'s' if ok != 1 else ''}"
                                      + (f"; {failed} could not be updated (see each order)." if failed else "."))
        else:
            messages.info(request, "No open supplier orders have a tracking number yet.")
        return redirect(request.get_full_path())
    qs = (Receipt.objects.select_related("supplier")
          .prefetch_related("packages", "lines__product", "supplier_payments"))
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db import models as dj
        cond = (dj.Q(supplier__name__icontains=q) | dj.Q(reference__icontains=q)
                | dj.Q(supplier_invoice_number__icontains=q)
                | dj.Q(packages__tracking_number__icontains=q)
                | dj.Q(lines__product__sku__icontains=q) | dj.Q(lines__product__name__icontains=q)
                | dj.Q(lines__product__upc__icontains=q)
                | dj.Q(lines__product__barcodes__code__icontains=q))
        num = q.upper().replace("RCV-", "").replace("RCV", "").strip()
        if num.isdigit():
            cond |= dj.Q(pk=int(num))
        qs = qs.filter(cond).distinct()
    status = request.GET.get("status")
    if status == "open":
        qs = qs.filter(posted=False)
    elif status == "posted":
        qs = qs.filter(posted=True)
    elif status == "unpaid":
        qs = [r for r in qs.filter(posted=True) if not r.is_paid]
    receipts = list(qs[:200]) if not isinstance(qs, list) else qs[:200]
    sort, direction = _receipt_sort(request, receipts)
    keep = {k: v for k, v in request.GET.items() if k not in ("sort", "dir")}
    return render(request, "inventory/receipt_list.html", {
        "receipts": receipts, "q": q, "sort": sort, "dir": direction,
        "sort_base": urlencode(keep), "columns": RECEIPT_SORT_COLUMNS,
    })


# (key, header label, numeric?) — the sortable columns on the Receiving list
RECEIPT_SORT_COLUMNS = [
    ("id", "#", False), ("supplier", "Supplier", False), ("date", "Ordered", False),
    ("expected", "Expected", False), ("reference", "Ref", False), ("merch", "Merch $", True),
    ("freight", "Freight+duty", True), ("status", "Status", False), ("bill", "Bill", False),
]


def _receipt_sort(request, receipts):
    """Sort the (already filtered, ≤200) list in place by ?sort=&dir=."""
    from datetime import date as _date
    sort = request.GET.get("sort") or "id"
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    far = _date.max
    keys = {
        "id": lambda r: r.pk,
        "supplier": lambda r: (r.supplier.name if r.supplier else "").lower(),
        "date": lambda r: (r.date, r.pk),
        "expected": lambda r: (r.expected_date or far, r.pk),
        "reference": lambda r: (r.reference or "").lower(),
        "merch": lambda r: r.merchandise_total,
        "freight": lambda r: r.freight_cost + r.duties_cost,
        # open first, then received (most recent received last)
        "status": lambda r: (1 if r.posted else 0, r.posted_at or timezone.now(), r.pk),
        # overdue, then unpaid, then not yet received, then paid
        "bill": lambda r: ((0 if r.is_overdue else 1 if (r.posted and not r.is_paid) else 2 if not r.posted else 3),
                           r.due_date or far, r.pk),
    }
    if sort not in keys:
        sort = "id"
    receipts.sort(key=keys[sort], reverse=(direction == "desc"))
    return sort, direction


@module_required("receiving")
def receipt_import(request):
    """
    Paste / upload a supplier order (Tradepost export, order-confirmation
    email, any distributor CSV) → review the matched lines → draft RCV.
    """
    from inventory.order_import import ParsedLine, match_products, parse_text
    suppliers = Supplier.objects.order_by("name")
    ctx = {"suppliers": suppliers, "text": "", "lines": None,
           "products": Product.objects.filter(active=True).only("pk", "sku", "name", "upc").order_by("sku"),
           "supplier_id": request.GET.get("supplier") or "", "new_supplier": "",
           "reference": "", "date": timezone.localdate().isoformat(), "expected_date": "",
           "freight_cost": "", "notes": ""}
    if request.method != "POST":
        return render(request, "inventory/receipt_import.html", ctx)

    for k in ("supplier_id", "new_supplier", "reference", "date", "expected_date", "freight_cost", "notes"):
        ctx[k] = (request.POST.get(k) or "").strip()
    action = request.POST.get("action")

    if action == "preview":
        text = (request.POST.get("paste") or request.POST.get("text") or "")
        if request.FILES.get("file"):
            text = request.FILES["file"].read().decode("utf-8-sig", errors="replace")
        ctx["text"] = text
        lines = match_products(parse_text(text), Product)
        if not lines:
            messages.error(request, "Couldn't find any order lines in that text. Paste the item rows "
                                    "(one per line) or upload the order's CSV export.")
        ctx["lines"] = lines
        return render(request, "inventory/receipt_import.html", ctx)

    if action == "create":
        # rebuild the reviewed lines from the form
        lines, errors = [], []
        n = int(request.POST.get("n") or 0)
        for i in range(n):
            if request.POST.get(f"skip_{i}"):
                continue
            ln = ParsedLine(code=request.POST.get(f"code_{i}", "").strip(),
                            name=request.POST.get(f"name_{i}", "").strip(),
                            qty=None, cost=None)
            try:
                ln.qty = int(request.POST.get(f"qty_{i}") or 0)
                ln.cost = Decimal(request.POST.get(f"cost_{i}") or "")
            except (ValueError, InvalidOperation):
                errors.append(f"Line {i + 1}: quantity and unit cost must be numbers.")
                continue
            if ln.qty <= 0 or ln.cost < 0:
                errors.append(f"Line {i + 1}: quantity must be at least 1 and cost can't be negative.")
                continue
            pid = request.POST.get(f"product_{i}") or ""
            if pid.isdigit():
                ln.product_id = int(pid)
            elif request.POST.get(f"create_{i}"):
                ln.product_id = None          # create below
            else:
                errors.append(f"Line {i + 1}: pick a product or tick “create it”.")
                continue
            lines.append(ln)
        supplier = None
        if ctx["new_supplier"]:
            supplier, _ = Supplier.objects.get_or_create(name=ctx["new_supplier"])
        elif ctx["supplier_id"].isdigit():
            supplier = Supplier.objects.filter(pk=int(ctx["supplier_id"])).first()
        if not lines and not errors:
            errors.append("Nothing to import — every line was skipped.")
        if errors:
            for e in errors:
                messages.error(request, e)
            ctx["lines"] = match_products(parse_text(request.POST.get("text") or ""), Product)
            ctx["text"] = request.POST.get("text") or ""
            return render(request, "inventory/receipt_import.html", ctx)

        from django.db import transaction
        with transaction.atomic():
            receipt = Receipt(supplier=supplier, reference=ctx["reference"][:100],
                              notes=ctx["notes"], created_by=request.user)
            for f in ("date", "expected_date"):
                if ctx[f]:
                    try:
                        setattr(receipt, f, timezone.datetime.fromisoformat(ctx[f]).date())
                    except ValueError:
                        pass
            if ctx["freight_cost"]:
                try:
                    receipt.freight_cost = Decimal(ctx["freight_cost"])
                except InvalidOperation:
                    pass
            receipt.save()
            created_products = 0
            for ln in lines:
                if ln.product_id is None:
                    sku = ln.code or _next_import_sku()
                    product = Product.objects.filter(sku=sku).first()
                    if product is None:
                        product = Product.objects.create(
                            sku=sku, name=(ln.name or sku)[:250],
                            upc=ln.code if ln.code.isdigit() else "",
                            default_price=(ln.cost * Decimal("1.25")).quantize(Decimal("0.01")))
                        created_products += 1
                    ln.product_id = product.pk
                ReceiptLine.objects.create(receipt=receipt, product_id=ln.product_id,
                                           qty=ln.qty, unit_cost=ln.cost)
        log_activity(request.user, "create", receipt)
        msg = f"Imported {len(lines)} line{'s' if len(lines) != 1 else ''} into RCV-{receipt.pk}."
        if created_products:
            msg += f" {created_products} new product{'s' if created_products != 1 else ''} created — " \
                   "set their prices and categories when you get a chance."
        messages.success(request, msg)
        return redirect("receipt_detail", receipt.pk)
    return render(request, "inventory/receipt_import.html", ctx)


@module_required("receiving")
def receipt_tradepost(request):
    """Tradepost purchases: synced from the API, matched to logged supplier
    orders, with Shippo tracking per package. Pick fills → one RCV."""
    from core.models import CompanySettings
    from inventory import tradepost as tp
    from inventory.models import TradepostFill
    from shipping.providers import get_provider
    company = CompanySettings.get()
    client = tp.TradepostClient(company.tradepost_api_key)
    view = request.GET.get("view") or request.POST.get("view") or "all"
    try:
        days = max(1, min(int(request.GET.get("days") or request.POST.get("days") or 120), 730))
    except ValueError:
        days = 120
    ctx = {"configured": client.is_configured(), "fills": [], "error": "", "days": days, "view": view,
           "shippo": get_provider("shippo").is_configured(), "last_sync": None}
    if not client.is_configured():
        return render(request, "inventory/receipt_tradepost.html", ctx)
    back = f"?view={view}&days={days}"

    def do_sync(refresh=True):
        try:
            new, upd = tp.sync_fills(client, since=timezone.now() - timezone.timedelta(days=days))
        except tp.TradepostError as e:
            ctx["error"] = str(e)
            return False
        linked = tp.link_logged_fills(Receipt, ReceiptLine)
        n = 0
        if refresh and ctx["shippo"]:
            provider = get_provider("shippo")
            for fill in TradepostFill.objects.filter(dismissed=False).exclude(packages=[]):
                if _fill_needs_refresh(fill):
                    n += tp.refresh_fill_tracking(fill, provider)
        ctx["sync_note"] = (f"Synced with Tradepost: {new} new fill{'s' if new != 1 else ''}, {upd} updated"
                            + (f", {linked} matched to orders you already logged" if linked else "")
                            + (f", tracking refreshed on {n} package{'s' if n != 1 else ''}" if n else "") + ".")
        return True

    if request.method == "POST":
        action = request.POST.get("action")
        ids = request.POST.getlist("fill")
        chosen = TradepostFill.objects.filter(execution_id__in=ids)
        if action == "sync":
            if do_sync():
                messages.success(request, ctx["sync_note"])
            else:
                messages.error(request, ctx["error"])
            return redirect(request.path + back)
        if action == "dismiss":
            n = chosen.filter(receipt__isnull=True).update(dismissed=True)
            messages.success(request, f"Hidden {n} fill{'s' if n != 1 else ''} — they won't be offered for logging again.")
            return redirect(request.path + back)
        if action == "restore":
            n = chosen.update(dismissed=False)
            messages.success(request, f"Restored {n} fill{'s' if n != 1 else ''}.")
            return redirect(request.path + back)
        if action == "unlink":
            n = chosen.filter(receipt__isnull=False).update(receipt=None)
            messages.success(request, f"Unlinked {n} fill{'s' if n != 1 else ''} from their supplier orders.")
            return redirect(request.path + back)
        if action == "link":
            fill = TradepostFill.objects.filter(execution_id=request.POST.get("fill_id")).first()
            num = (request.POST.get("rcv") or "").upper().replace("RCV-", "").replace("RCV", "").strip()
            receipt = Receipt.objects.filter(pk=int(num)).first() if num.isdigit() else None
            if fill and receipt:
                fill.receipt, fill.dismissed = receipt, False
                fill.save(update_fields=["receipt", "dismissed"])
                tp.mirror_packages(fill)
                messages.success(request, f"{fill.order_number} linked to RCV-{receipt.pk}.")
            else:
                messages.error(request, "Enter the number of an existing supplier order, e.g. RCV-12.")
            return redirect(request.path + back)
        if action == "refresh_tracking":
            if not ctx["shippo"]:
                messages.warning(request, "Add a Shippo token in Settings to track packages automatically.")
                return redirect(request.path + back)
            provider = get_provider("shippo")
            qs = chosen if ids else TradepostFill.objects.filter(dismissed=False).exclude(packages=[])
            n = sum(tp.refresh_fill_tracking(f, provider, force=bool(ids)) for f in qs)
            messages.success(request, f"Tracking refreshed on {n} package{'s' if n != 1 else ''}.")
            return redirect(request.path + back)
        if action == "create":
            if request.POST.get("all_new"):
                picked = list(TradepostFill.objects.filter(receipt__isnull=True, dismissed=False)
                              .exclude(status__in=["FAILED", "REVERSED"]))
            else:
                picked = list(chosen.filter(receipt__isnull=True))
            if not picked:
                messages.error(request, "Pick at least one Tradepost fill that isn't on a supplier order yet.")
                return redirect(request.path + back)
            receipt, new_products = tp.build_receipt(picked, request.user, Receipt, ReceiptLine,
                                                     Product, Supplier,
                                                     reference=(request.POST.get("reference") or "").strip())
            log_activity(request.user, "create", receipt)
            msg = f"Created RCV-{receipt.pk} from {len(picked)} Tradepost fill{'s' if len(picked) != 1 else ''}."
            if new_products:
                msg += (f" {new_products} new product{'s' if new_products != 1 else ''} added — "
                        "check their names, categories and prices.")
            messages.success(request, msg)
            return redirect("receipt_detail", receipt.pk)

    # GET: sync automatically when the cache is empty, stale, or ?sync=1
    latest = TradepostFill.objects.order_by("-fetched_at").values_list("fetched_at", flat=True).first()
    ctx["last_sync"] = latest
    if request.GET.get("sync") == "1" or latest is None or (timezone.now() - latest).total_seconds() > 1800:
        do_sync()
        ctx["last_sync"] = timezone.now()

    qs = TradepostFill.objects.select_related("receipt").exclude(status__in=["FAILED", "REVERSED"])
    if view == "new":
        qs = qs.filter(receipt__isnull=True, dismissed=False)
    elif view == "logged":
        qs = qs.filter(receipt__isnull=False)
    elif view == "dismissed":
        qs = qs.filter(dismissed=True)
    else:
        qs = qs.filter(dismissed=False)
    fills = list(qs[:500])
    from inventory.tracking import carrier_url
    for f in fills:
        for p in f.packages or []:
            p["url"] = carrier_url(p.get("carrier"), p.get("tracking"))
    ctx["fills"] = fills
    allq = TradepostFill.objects.exclude(status__in=["FAILED", "REVERSED"])
    ctx["n_new"] = allq.filter(receipt__isnull=True, dismissed=False).count()
    ctx["n_logged"] = allq.filter(receipt__isnull=False).count()
    ctx["n_dismissed"] = allq.filter(dismissed=True).count()
    ctx["total_new"] = sum((f.line_total for f in allq.filter(receipt__isnull=True, dismissed=False)), Decimal("0"))
    return render(request, "inventory/receipt_tradepost.html", ctx)


def _fill_needs_refresh(fill, max_age_minutes=55):
    """Refresh when any undelivered package hasn't been checked in the last hour."""
    from datetime import datetime
    for p in fill.packages or []:
        if p.get("status") == "DELIVERED":
            continue
        chk = p.get("checked_at")
        if not chk:
            return True
        try:
            if (timezone.now() - datetime.fromisoformat(chk)).total_seconds() > max_age_minutes * 60:
                return True
        except ValueError:
            return True
    return False


def _next_import_sku():
    base = "IMP-"
    n = Product.all_objects.filter(sku__startswith=base).count() + 1
    while Product.all_objects.filter(sku=f"{base}{n:04d}").exists():
        n += 1
    return f"{base}{n:04d}"


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
    package_form = ReceiptPackageForm()
    if request.method == "POST" and request.POST.get("action") == "add_packages":
        from inventory.models import ReceiptPackage
        from inventory.tracking import guess_carrier, refresh_receipt
        package_form = ReceiptPackageForm(request.POST)
        if package_form.is_valid():
            carrier = package_form.cleaned_data["carrier"]
            added = []
            for num in package_form.cleaned_data["tracking_numbers"]:
                pkg, created = ReceiptPackage.objects.get_or_create(
                    receipt=receipt, tracking_number=num,
                    defaults={"carrier": carrier or guess_carrier(num)})
                if created:
                    added.append(pkg)
            if added:
                log_activity(request.user, "tracking_added", receipt,
                             note=", ".join(p.tracking_number for p in added))
                ok, msg = refresh_receipt(receipt)
                messages.success(request, f"Added {len(added)} tracking number{'s' if len(added) != 1 else ''}. {msg}")
            else:
                messages.info(request, "Those tracking numbers are already on this order.")
            return redirect("receipt_detail", pk)
        messages.error(request, "; ".join(f"{v[0]}" for v in package_form.errors.values()))
    if request.method == "POST" and request.POST.get("action") == "del_package":
        from inventory.tracking import sync_expected_date
        pkg = get_object_or_404(receipt.packages, pk=request.POST.get("package_id"))
        log_activity(request.user, "tracking_removed", receipt, note=pkg.tracking_number)
        pkg.delete()
        sync_expected_date(receipt)
        messages.success(request, "Tracking number removed.")
        return redirect("receipt_detail", pk)
    if request.method == "POST" and request.POST.get("action") == "refresh_tracking":
        from inventory.tracking import refresh_receipt
        ok, msg = refresh_receipt(receipt)
        (messages.success if ok else messages.warning)(request, msg)
        return redirect("receipt_detail", pk)
    if request.method == "POST" and request.POST.get("action") == "mark_paid":
        # One tap: paid today from the default bank account (or personally
        # if none is set). Details can be edited afterwards; matching a bank
        # transaction later replaces these values.
        from banking.models import BankAccount
        if receipt.is_paid:
            messages.info(request, "This bill is already marked paid.")
        else:
            receipt.paid_date = timezone.localdate()
            receipt.paid_from = BankAccount.objects.filter(active=True, is_default=True).first()
            receipt.save(update_fields=["paid_date", "paid_from"])
            log_activity(request.user, "supplier_bill_paid", receipt,
                         note=f"Marked paid {receipt.paid_date}"
                              + (f" from {receipt.paid_from}" if receipt.paid_from else " (paid personally)"))
            messages.success(request, f"RCV-{receipt.pk} marked paid today"
                                      + (f" from {receipt.paid_from}." if receipt.paid_from
                                         else ". No default bank account is set, so it counts as paid personally."))
        nxt = request.POST.get("next")
        return redirect(nxt if nxt and nxt.startswith("/") else "receipt_detail", *([] if nxt and nxt.startswith("/") else [pk]))
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
        "package_form": package_form, "packages": receipt.packages.all(),
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
