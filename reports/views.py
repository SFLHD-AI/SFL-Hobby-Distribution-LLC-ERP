"""
Reporting. Every report accepts ?start=YYYY-MM-DD&end=YYYY-MM-DD,
?export=csv and ?export=pdf.

Revenue basis: invoices (excluding VOID), merchandise subtotal + shipping
charged to the customer. Tax collected is not revenue.
COGS basis: invoice line cost snapshots (frozen at PO finalization).
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import models
from django.shortcuts import render
from django.utils import timezone

from core.csvutils import csv_response
from core.models import CompanySettings
from core.pdf import pdf_response
from core.roles import costs_required, module_required
from customers.models import Customer
from expenses.models import Expense
from inventory.models import Product, StockMovement
from invoicing.models import CreditMemo, Invoice, InvoiceLine
from orders.models import PurchaseOrder
from shipping.models import Shipment

ZERO = Decimal("0.00")


def _period(request):
    today = timezone.localdate()
    default_start = today.replace(day=1)
    try:
        start = date.fromisoformat(request.GET.get("start", "")) if request.GET.get("start") else default_start
    except ValueError:
        start = default_start
    try:
        end = date.fromisoformat(request.GET.get("end", "")) if request.GET.get("end") else today
    except ValueError:
        end = today
    return start, end


def _out(request, *, title, columns, rows, template="reports/table_report.html",
         extra=None, totals=None):
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"{title.lower().replace(' ', '_')}.csv", columns,
                            [[str(c) for c in r] for r in rows])
    if export == "pdf":
        return pdf_response("pdf/report.html", {
            "title": title, "columns": columns, "rows": rows, "totals": totals,
            "company": CompanySettings.get(),
            "generated": timezone.localtime(),
            "period": f"{request.GET.get('start', '')} – {request.GET.get('end', '')}",
        }, f"{title}.pdf")
    ctx = {"title": title, "columns": columns, "rows": rows, "totals": totals}
    if extra:
        ctx.update(extra)
    return render(request, template, ctx)


@module_required("reports")
def report_index(request):
    return render(request, "reports/index.html")


# ---------------------------------------------------------------------------
# Month-end inventory valuation (point-in-time, from the ledger)
# ---------------------------------------------------------------------------
@costs_required
def inventory_valuation(request):
    as_of_raw = request.GET.get("as_of")
    try:
        as_of = date.fromisoformat(as_of_raw) if as_of_raw else timezone.localdate()
    except ValueError:
        as_of = timezone.localdate()
    # end of day in local time
    cutoff = timezone.make_aware(
        timezone.datetime.combine(as_of, timezone.datetime.max.time()))

    rows = []
    total_value = ZERO
    total_units = 0
    for p in Product.all_objects.order_by("sku"):
        last = (StockMovement.objects.filter(product=p, timestamp__lte=cutoff)
                .order_by("-timestamp", "-id").first())
        if last is None:
            continue
        qty, cost = last.qty_after, last.avg_cost_after
        if qty == 0:
            continue
        value = (Decimal(qty) * cost).quantize(Decimal("0.01"))
        rows.append([p.sku, p.name[:50], qty, f"{cost:.4f}", f"{value:.2f}"])
        total_value += value
        total_units += qty
    return _out(request, title=f"Inventory valuation as of {as_of}",
                columns=["SKU", "Product", "On hand", "Unit cost", "Value $"],
                rows=rows,
                totals=["", "TOTAL", total_units, "", f"{total_value:.2f}"],
                extra={"as_of": as_of, "show_as_of": True})


# ---------------------------------------------------------------------------
# Monthly P&L
# ---------------------------------------------------------------------------
@costs_required
def pnl(request):
    start, end = _period(request)
    invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start,
                                       date__lte=end)
                .exclude(status=Invoice.VOID)
                .exclude(is_ar_adjustment=True))   # balance transfers ≠ revenue
    revenue_merch = sum((i.subtotal for i in invoices), ZERO)
    revenue_ship = sum((i.shipping_charge for i in invoices), ZERO)
    cogs = sum((i.cogs for i in invoices), ZERO)
    credits = CreditMemo.objects.filter(is_deleted=False, status="applied",
                                        date__gte=start, date__lte=end)
    credit_total = sum((c.total for c in credits), ZERO)

    # Shipping is a PASS-THROUGH, kept out of merchandise profit entirely:
    # gross profit = merchandise revenue (less credits) - COGS.
    merch_net = revenue_merch - credit_total
    gross = merch_net - cogs

    ship_costs = (Shipment.objects.filter(is_deleted=False, cost__isnull=False,
                                          ship_date__gte=start, ship_date__lte=end)
                  .aggregate(t=models.Sum("cost"))["t"] or ZERO)
    ship_net = revenue_ship - ship_costs
    expense_rows = (Expense.objects.filter(is_deleted=False, date__gte=start,
                                           date__lte=end)
                    .values("category__name").annotate(t=models.Sum("amount"))
                    .order_by("-t"))
    expenses_total = sum((r["t"] for r in expense_rows), ZERO)
    net = gross + ship_net - expenses_total

    rows = [
        ["Merchandise revenue", f"{revenue_merch:.2f}"],
        ["Less: credits / returns", f"-{credit_total:.2f}"],
        ["NET MERCHANDISE REVENUE", f"{merch_net:.2f}"],
        ["Cost of goods sold", f"-{cogs:.2f}"],
        ["GROSS PROFIT (merchandise — shipping excluded)", f"{gross:.2f}"],
        ["Shipping charged to customers", f"{revenue_ship:.2f}"],
        ["Shipping label/freight costs", f"-{ship_costs:.2f}"],
        ["NET SHIPPING (pass-through)", f"{ship_net:.2f}"],
    ]
    for r in expense_rows:
        rows.append([f"Expense: {r['category__name'] or 'Uncategorized'}", f"-{r['t']:.2f}"])
    rows.append(["NET PROFIT", f"{net:.2f}"])

    return _out(request, title="Profit & Loss",
                columns=["Line", "Amount $"], rows=rows,
                extra={"show_period": True, "start": start, "end": end,
                       "highlight_rows": ["NET MERCHANDISE REVENUE",
                                          "GROSS PROFIT (merchandise — shipping excluded)",
                                          "NET SHIPPING (pass-through)", "NET PROFIT"]})


# ---------------------------------------------------------------------------
# Revenue / profit by customer
# ---------------------------------------------------------------------------
@costs_required
def revenue_by_customer(request):
    start, end = _period(request)
    invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start, date__lte=end)
                .exclude(status=Invoice.VOID)
                .exclude(is_ar_adjustment=True)
                .select_related("customer"))
    agg = defaultdict(lambda: {"rev": ZERO, "ship": ZERO, "cogs": ZERO, "n": 0})
    for i in invoices:
        a = agg[i.customer.business_name]
        a["rev"] += i.subtotal          # merchandise only — shipping kept separate
        a["ship"] += i.shipping_charge
        a["cogs"] += i.cogs
        a["n"] += 1
    rows = []
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["rev"]):
        profit = a["rev"] - a["cogs"]
        margin = (profit / a["rev"] * 100) if a["rev"] else 0
        rows.append([name, a["n"], f"{a['rev']:.2f}", f"{a['ship']:.2f}",
                     f"{a['cogs']:.2f}", f"{profit:.2f}", f"{margin:.1f}%"])
    return _out(request, title="Revenue and profit by customer",
                columns=["Customer", "Invoices", "Merch revenue $", "Shipping $",
                         "COGS $", "Profit $", "Margin"],
                rows=rows, extra={"show_period": True, "start": start, "end": end})


# ---------------------------------------------------------------------------
# Revenue / profit by day of week
# ---------------------------------------------------------------------------
@costs_required
def revenue_by_dow(request):
    start, end = _period(request)
    invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start,
                                       date__lte=end)
                .exclude(status=Invoice.VOID)
                .exclude(is_ar_adjustment=True))
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    agg = {n: {"rev": ZERO, "cogs": ZERO, "n": 0} for n in names}
    for i in invoices:
        n = names[i.date.weekday()]
        agg[n]["rev"] += i.subtotal     # merchandise only — shipping kept separate
        agg[n]["cogs"] += i.cogs
        agg[n]["n"] += 1
    rows = [[n, a["n"], f"{a['rev']:.2f}", f"{(a['rev'] - a['cogs']):.2f}"]
            for n, a in agg.items()]
    return _out(request, title="Revenue by day of week",
                columns=["Day", "Invoices", "Merch revenue $", "Profit $"], rows=rows,
                extra={"show_period": True, "start": start, "end": end})


# ---------------------------------------------------------------------------
# Sales by SKU / category / brand
# ---------------------------------------------------------------------------
@module_required("reports")
def sales_by_product(request):
    start, end = _period(request)
    group = request.GET.get("group", "sku")
    lines = (InvoiceLine.objects
             .filter(is_deleted=False, invoice__is_deleted=False,
                     invoice__date__gte=start, invoice__date__lte=end)
             .exclude(invoice__status=Invoice.VOID)
             .exclude(invoice__is_ar_adjustment=True)
             .select_related("product", "product__category", "product__brand",
                             "category"))
    from core.roles import can_see_costs
    show_costs = can_see_costs(request.user)

    agg = defaultdict(lambda: {"units": 0, "dollars": ZERO, "profit": ZERO})
    for l in lines:
        if group == "category":
            # product lines use the product's category; manual one-off lines
            # carry their own category picked at invoicing time
            if l.product and l.product.category:
                key = str(l.product.category)
            elif l.category_id:
                key = str(l.category)
            else:
                key = "Uncategorized"
        elif group == "brand":
            key = str(l.product.brand) if l.product and l.product.brand else "No brand"
        else:
            key = f"{l.product.sku} — {l.product.name[:40]}" if l.product else l.description
        a = agg[key]
        a["units"] += l.qty
        a["dollars"] += l.line_total
        if l.line_profit is not None:
            a["profit"] += l.line_profit

    columns = [group.title(), "Units", "Dollars $"]
    if show_costs:
        columns.append("Profit $")
    rows = []
    for key, a in sorted(agg.items(), key=lambda kv: -kv[1]["dollars"]):
        row = [key, a["units"], f"{a['dollars']:.2f}"]
        if show_costs:
            row.append(f"{a['profit']:.2f}")
        rows.append(row)
    return _out(request, title=f"Sales by {group}",
                columns=columns, rows=rows,
                extra={"show_period": True, "start": start, "end": end,
                       "show_group_tabs": True, "group": group})


# ---------------------------------------------------------------------------
# Inventory movement report
# ---------------------------------------------------------------------------
@module_required("reports")
def movement_report(request):
    start, end = _period(request)
    qs = (StockMovement.objects
          .filter(timestamp__date__gte=start, timestamp__date__lte=end)
          .select_related("product", "user").order_by("timestamp"))
    sku = request.GET.get("sku", "").strip()
    if sku:
        qs = qs.filter(product__sku__icontains=sku)
    mtype = request.GET.get("type")
    if mtype:
        qs = qs.filter(movement_type=mtype)
    rows = [[timezone.localtime(m.timestamp).strftime("%Y-%m-%d %H:%M"),
             m.product.sku, m.get_movement_type_display(),
             m.qty_delta, m.qty_before, m.qty_after,
             m.reference, m.user.username if m.user else ""]
            for m in qs[:2000]]
    return _out(request, title="Inventory movements",
                columns=["When", "SKU", "Type", "Δ", "Before", "After", "Ref", "User"],
                rows=rows,
                extra={"show_period": True, "start": start, "end": end,
                       "show_sku_filter": True, "sku": sku,
                       "movement_types": StockMovement.TYPES, "mtype": mtype or ""})


# ---------------------------------------------------------------------------
# Sales tax report (for quarterly filing)
# ---------------------------------------------------------------------------
@module_required("reports")
def sales_tax_report(request):
    """Everything needed to file sales tax for a period:

    * Gross sales (merchandise + shipping charged), per invoice and total
    * Taxable vs tax-EXEMPT sales (resale-certificate / 0%-tax invoices)
    * Sales tax collected

    Default period = current quarter; pass ?start=&end= for any range.
    """
    today = timezone.localdate()
    q_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    if not request.GET.get("start"):
        request.GET = request.GET.copy()
        request.GET["start"] = q_start.isoformat()
    start, end = _period(request)

    invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start,
                                       date__lte=end)
                .exclude(status=Invoice.VOID)
                .exclude(is_ar_adjustment=True)   # taxed (or not) in the old system
                .select_related("customer").order_by("date", "number"))

    rows = []
    gross = taxable = exempt = tax_collected = ZERO
    for i in invoices:
        sale = i.subtotal + i.shipping_charge     # tax collected is NOT a sale
        gross += sale
        tax_collected += i.tax_amount
        is_taxable = i.tax_amount > 0
        if is_taxable:
            taxable += sale
        else:
            exempt += sale
        rows.append([i.number, i.date, i.customer.business_name,
                     f"{i.subtotal:.2f}", f"{i.shipping_charge:.2f}",
                     f"{sale:.2f}",
                     "Taxable" if is_taxable else "Exempt",
                     f"{i.tax_rate:.3f}%" if is_taxable else "—",
                     f"{i.tax_amount:.2f}"])

    credits = CreditMemo.objects.filter(is_deleted=False, status="applied",
                                        date__gte=start, date__lte=end)
    credit_total = sum((c.total for c in credits), ZERO)
    if credit_total:
        rows.append(["(credits)", "", "Credits issued this period — review "
                     "whether they adjust taxable sales", "", "",
                     f"-{credit_total:.2f}", "", "", ""])

    return _out(request, title="Sales tax report",
                columns=["Invoice", "Date", "Customer", "Merch $", "Shipping $",
                         "Gross sale $", "Tax status", "Rate", "Tax collected $"],
                rows=rows,
                totals=["TOTAL", "", f"Taxable: ${taxable:.2f} · Exempt: ${exempt:.2f}",
                        "", "", f"{gross:.2f}", "", "", f"{tax_collected:.2f}"],
                extra={"show_period": True, "start": start, "end": end})


# ---------------------------------------------------------------------------
# Shipping & delivery charges report
# ---------------------------------------------------------------------------
@module_required("reports")
def shipping_report(request):
    """Shipping/delivery money for a period, both directions:

    * charged to customers on invoices (and whether that invoice is paid)
    * paid by you to carriers (label/freight cost recorded on shipments)
    """
    start, end = _period(request)
    rows = []
    charged = collected = carrier_paid = ZERO

    invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start,
                                       date__lte=end, shipping_charge__gt=0)
                .exclude(status=Invoice.VOID)
                .select_related("customer").order_by("date", "number"))
    for i in invoices:
        paid = i.balance_due <= 0
        charged += i.shipping_charge
        if paid:
            collected += i.shipping_charge
        rows.append(["Invoice", i.number, i.date, i.customer.business_name,
                     f"{i.shipping_charge:.2f}", "✓ paid" if paid else "unpaid", ""])

    shipments = (Shipment.objects.filter(is_deleted=False, cost__isnull=False,
                                         ship_date__gte=start, ship_date__lte=end)
                 .select_related("po", "po__customer").order_by("ship_date"))
    for s in shipments:
        carrier_paid += s.cost
        rows.append([f"Carrier ({s.get_provider_display()})",
                     s.po.number if s.po else "", s.ship_date,
                     s.po.customer.business_name if s.po else "",
                     "", "", f"{s.cost:.2f}"])

    return _out(request, title="Shipping and delivery charges",
                columns=["Kind", "Ref", "Date", "Customer",
                         "Charged to customer $", "Invoice status",
                         "Paid to carrier $"],
                rows=rows,
                totals=["TOTAL", "", "",
                        f"Collected (on paid invoices): ${collected:.2f} · "
                        f"Net vs carriers: ${charged - carrier_paid:.2f}",
                        f"{charged:.2f}", "", f"{carrier_paid:.2f}"],
                extra={"show_period": True, "start": start, "end": end})


# ---------------------------------------------------------------------------
# AR aging
# ---------------------------------------------------------------------------
@module_required("reports")
def ar_aging(request):
    today = timezone.localdate()
    buckets = ["Current", "1-30", "31-60", "61-90", "90+"]
    agg = defaultdict(lambda: [ZERO, ZERO, ZERO, ZERO, ZERO])
    invoices = (Invoice.objects.filter(is_deleted=False)
                .exclude(status=Invoice.VOID).select_related("customer"))
    for i in invoices:
        bal = i.balance_due
        if bal <= 0:
            continue
        days = (today - i.due_date).days
        idx = 0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3 if days <= 90 else 4
        agg[i.customer.business_name][idx] += bal
    rows = []
    totals = [ZERO] * 5
    for name, vals in sorted(agg.items(), key=lambda kv: -sum(kv[1])):
        rows.append([name] + [f"{v:.2f}" for v in vals] + [f"{sum(vals):.2f}"])
        for k in range(5):
            totals[k] += vals[k]
    return _out(request, title="AR aging",
                columns=["Customer"] + buckets + ["Total $"],
                rows=rows,
                totals=["TOTAL"] + [f"{t:.2f}" for t in totals] + [f"{sum(totals):.2f}"])


# ---------------------------------------------------------------------------
# QuickBooks-friendly export (CSV bundle for the accountant)
# ---------------------------------------------------------------------------
@costs_required
def quickbooks_export(request):
    start, end = _period(request)
    kind = request.GET.get("kind", "invoices")
    if kind == "invoices":
        invoices = (Invoice.objects.filter(is_deleted=False, date__gte=start,
                                           date__lte=end)
                    .exclude(status=Invoice.VOID).select_related("customer"))
        return csv_response(
            f"qb_invoices_{start}_{end}.csv",
            ["InvoiceNo", "Customer", "InvoiceDate", "DueDate", "Terms",
             "LineItem", "LineQty", "LineUnitPrice", "LineAmount",
             "Shipping", "Tax", "Total"],
            [[i.number, i.customer.business_name, i.date, i.due_date, i.terms,
              l.description, l.qty, l.unit_price, l.line_total,
              i.shipping_charge, i.tax_amount, i.total]
             for i in invoices for l in i.lines.filter(is_deleted=False)])
    if kind == "payments":
        payments = (__import__("payments.models", fromlist=["Payment"]).Payment
                    .objects.filter(is_deleted=False, date__gte=start, date__lte=end)
                    .select_related("customer"))
        return csv_response(
            f"qb_payments_{start}_{end}.csv",
            ["Date", "Customer", "Method", "RefNumber", "Amount", "AppliedToInvoices"],
            [[p.date, p.customer.business_name, p.get_method_display(), p.reference,
              p.amount, "; ".join(f"{a.invoice.number}:{a.amount}"
                                  for a in p.applications.all())]
             for p in payments])
    # expenses
    exps = Expense.objects.filter(is_deleted=False, date__gte=start,
                                  date__lte=end).select_related("category")
    return csv_response(
        f"qb_expenses_{start}_{end}.csv",
        ["Date", "Category", "Vendor", "Amount", "Memo"],
        [[e.date, e.category or "", e.vendor, e.amount, e.note] for e in exps])
