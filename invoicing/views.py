from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.csvutils import csv_response
from core.models import CompanySettings, log_activity
from core.pdf import invoice_context, invoice_pdf_bytes, pdf_response
from core.roles import can_see_costs, module_required
from customers.models import Customer
from invoicing.models import CreditMemo, CreditMemoLine, Invoice
from orders.models import PurchaseOrder


@module_required("invoices")
def invoice_list(request):
    qs = Invoice.objects.filter(is_deleted=False).select_related("customer")
    status = request.GET.get("status")
    if status == "unpaid":
        qs = qs.filter(status__in=[Invoice.OPEN, Invoice.PARTIAL])
    elif status:
        qs = qs.filter(status=status)
    if request.GET.get("overdue") == "1":
        qs = [i for i in qs if i.is_overdue]
    q = request.GET.get("q", "").strip()
    if q:
        qs = Invoice.objects.filter(is_deleted=False).filter(
            models.Q(number__icontains=q) |
            models.Q(customer__business_name__icontains=q)).select_related("customer")
    invoices = list(qs)[:300]

    if request.GET.get("export") == "csv":
        return csv_response(
            "invoices.csv",
            ["number", "customer", "date", "due_date", "status", "subtotal",
             "shipping", "tax", "total", "paid", "balance"],
            [[i.number, i.customer.business_name, i.date, i.due_date, i.status,
              i.subtotal, i.shipping_charge, i.tax_amount, i.total,
              i.amount_paid, i.balance_due] for i in invoices])
    return render(request, "invoicing/invoice_list.html", {"invoices": invoices})


@module_required("invoices")
def invoice_create(request):
    """Pick finalized POs (one customer) and build one consolidated invoice."""
    preselect_po = request.GET.get("po")
    # Finalized POs, plus shipped/delivered ones that were never invoiced
    # (local deliveries can go out the door before the invoice is cut).
    candidates = (PurchaseOrder.objects
                  .filter(is_deleted=False,
                          status__in=[PurchaseOrder.FINALIZED,
                                      PurchaseOrder.SHIPPED])
                  .select_related("customer")
                  .order_by("customer__business_name", "number"))
    finalized = [po for po in candidates
                 if po.status == PurchaseOrder.FINALIZED or not po.has_live_invoice]

    if request.method == "POST":
        po_ids = request.POST.getlist("po_ids")
        pos = list(PurchaseOrder.objects.filter(pk__in=po_ids, is_deleted=False))
        try:
            shipping_raw = request.POST.get("shipping_charge", "").strip()
            tax_rate_raw = request.POST.get("tax_rate", "").strip()
            tax_override_raw = request.POST.get("tax_amount_override", "").strip()
            invoice = Invoice.build_from_pos(
                pos, user=request.user,
                shipping_charge=Decimal(shipping_raw) if shipping_raw else None,
                tax_rate=Decimal(tax_rate_raw) if tax_rate_raw else None,
                tax_amount_override=Decimal(tax_override_raw) if tax_override_raw else None,
                notes=request.POST.get("notes", ""))
            added = _add_manual_lines_from_post(request, invoice)
            messages.success(request, f"Invoice {invoice.number} created from "
                                      f"{len(pos)} order(s)"
                                      + (f" + {added} manual item(s)." if added else "."))
            return redirect("invoice_detail", invoice.pk)
        except (ValidationError, InvalidOperation) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)

    from inventory.models import Category
    default_rate = CompanySettings.get().default_tax_rate
    return render(request, "invoicing/invoice_create.html", {
        "finalized": finalized, "preselect_po": preselect_po,
        "default_rate": default_rate,
        "categories": Category.objects.order_by("name"),
    })


@module_required("invoices")
def invoice_create_manual(request):
    """Direct AR charge: an invoice with no PO — pick a customer, type the
    items. Lands in AR aging, statements and payments like any invoice."""
    from inventory.models import Category
    customers = Customer.objects.filter(is_deleted=False).order_by("business_name")

    if request.method == "POST":
        try:
            customer = get_object_or_404(Customer, pk=request.POST.get("customer_id"),
                                         is_deleted=False)
            shipping_raw = request.POST.get("shipping_charge", "").strip()
            tax_rate_raw = request.POST.get("tax_rate", "").strip()
            if not any(d.strip() for d in request.POST.getlist("m_desc")):
                raise ValidationError("Add at least one item.")
            invoice = Invoice.build_manual(
                customer, user=request.user,
                shipping_charge=Decimal(shipping_raw) if shipping_raw else None,
                tax_rate=Decimal(tax_rate_raw) if tax_rate_raw else None,
                notes=request.POST.get("notes", ""))
            added = _add_manual_lines_from_post(request, invoice)
            tax_override_raw = request.POST.get("tax_amount_override", "").strip()
            if tax_override_raw:
                invoice.compute_totals(tax_amount_override=Decimal(tax_override_raw))
            messages.success(request, f"Manual invoice {invoice.number} created "
                                      f"for {customer.business_name} — "
                                      f"${invoice.total} added to AR.")
            return redirect("invoice_detail", invoice.pk)
        except (ValidationError, InvalidOperation) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)

    return render(request, "invoicing/invoice_create_manual.html", {
        "customers": customers,
        "categories": Category.objects.order_by("name"),
        "default_rate": CompanySettings.get().default_tax_rate,
    })


def _add_manual_lines_from_post(request, invoice):
    """Read the manual-item rows (m_desc[]/m_qty[]/m_price[]/m_cost[]/m_cat[])
    and add them to the invoice. Rows without a description are skipped."""
    descs = request.POST.getlist("m_desc")
    qtys = request.POST.getlist("m_qty")
    prices = request.POST.getlist("m_price")
    costs = request.POST.getlist("m_cost")
    cats = request.POST.getlist("m_cat")
    added = 0
    for i, desc in enumerate(descs):
        desc = desc.strip()
        if not desc:
            continue
        try:
            qty = int(qtys[i]) if i < len(qtys) and qtys[i].strip() else 1
            price = Decimal(prices[i].strip()) if i < len(prices) and prices[i].strip() else None
        except (ValueError, InvalidOperation):
            raise ValidationError(f"Manual item “{desc}”: enter a valid quantity and price.")
        if price is None:
            raise ValidationError(f"Manual item “{desc}”: price is required.")
        if qty <= 0:
            raise ValidationError(f"Manual item “{desc}”: quantity must be at least 1.")
        cost_raw = costs[i].strip() if i < len(costs) else ""
        try:
            cost = Decimal(cost_raw) if cost_raw else None
        except InvalidOperation:
            raise ValidationError(f"Manual item “{desc}”: enter a valid cost.")
        if not can_see_costs(request.user):
            cost = None  # users without cost access can't set costs
        invoice.add_manual_line(
            description=desc, qty=qty, unit_price=price, unit_cost=cost,
            category_name=cats[i] if i < len(cats) else "", user=request.user)
        added += 1
    return added


@module_required("invoices")
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("customer"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "void":
                invoice.void(user=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, "Invoice voided.")
            elif action == "update_tax":
                raw = request.POST.get("tax_amount", "").strip()
                invoice.compute_totals(tax_amount_override=Decimal(raw) if raw else None)
                log_activity(request.user, "invoice_tax_override", invoice, note=raw)
                messages.success(request, "Tax updated.")
            elif action == "add_manual":
                added = _add_manual_lines_from_post(request, invoice)
                if added:
                    messages.success(request, f"{added} item(s) added to the invoice.")
                else:
                    messages.error(request, "Enter a description for the item.")
            elif action == "remove_manual":
                invoice.remove_manual_line(request.POST.get("line_id"),
                                           user=request.user)
                messages.success(request, "Item removed.")
        except (ValidationError, InvalidOperation) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)
        return redirect("invoice_detail", pk)

    from inventory.models import Category
    return render(request, "invoicing/invoice_detail.html", {
        "invoice": invoice,
        "lines": invoice.lines.filter(is_deleted=False)
                 .select_related("product", "po", "category"),
        "applications": invoice.payment_applications.select_related("payment"),
        "credits": invoice.credit_memos.filter(is_deleted=False),
        "show_costs": can_see_costs(request.user),
        "categories": Category.objects.order_by("name"),
    })


@module_required("invoices")
def invoice_pdf(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    company = CompanySettings.get()
    template = f"pdf/invoice_{company.invoice_template}.html"
    include_balance = request.GET.get("balance") == "1"
    return pdf_response(template,
                        invoice_context(invoice, company,
                                        include_balance=include_balance),
                        f"{invoice.number}.pdf")


@module_required("invoices")
def invoice_email(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    to_addr = invoice.customer.email
    if not to_addr:
        messages.error(request, "Customer has no email on file.")
        return redirect("invoice_detail", pk)
    company = CompanySettings.get()
    pdf = invoice_pdf_bytes(invoice)
    tracking = invoice_context(invoice, company)["tracking_numbers"]
    body = (f"Hi {invoice.customer.business_name},\n\n"
            f"Please find attached invoice {invoice.number} for ${invoice.total}.\n"
            f"Due date: {invoice.due_date:%B %d, %Y}.\n")
    if tracking:
        body += f"Tracking: {tracking}\n"
    body += f"\nThank you for your business!\n{company.company_name}\n{company.phone}"
    msg = EmailMessage(subject=f"Invoice {invoice.number} from {company.company_name}",
                       body=body, to=[to_addr])
    msg.attach(f"{invoice.number}.pdf", pdf, "application/pdf")
    msg.send(fail_silently=False)
    invoice.emailed_at = timezone.now()
    invoice.save(update_fields=["emailed_at"])
    log_activity(request.user, "invoice_emailed", invoice, note=to_addr)
    messages.success(request, f"Invoice emailed to {to_addr}.")
    return redirect("invoice_detail", pk)


@module_required("invoices")
def creditmemo_create(request, invoice_pk):
    invoice = get_object_or_404(Invoice, pk=invoice_pk)
    inv_lines = invoice.lines.filter(is_deleted=False).select_related("product")
    if request.method == "POST":
        memo = CreditMemo.objects.create(
            invoice=invoice, customer=invoice.customer,
            reason=request.POST.get("reason", ""), created_by=request.user)
        any_line = False
        for line in inv_lines:
            raw = request.POST.get(f"qty_{line.pk}", "").strip()
            if not raw:
                continue
            qty = int(raw)
            if qty <= 0:
                continue
            qty = min(qty, line.qty)
            CreditMemoLine.objects.create(
                credit_memo=memo, product=line.product,
                description=line.description, qty=qty,
                unit_price=line.unit_price,
                restock=bool(request.POST.get(f"restock_{line.pk}")),
                cost_snapshot=line.cost_snapshot)
            any_line = True
        if not any_line:
            memo.hard_delete()
            messages.error(request, "Enter a return quantity on at least one line.")
            return redirect("creditmemo_create", invoice_pk)
        try:
            memo.apply(user=request.user)
            messages.success(request, f"Credit memo {memo.number} applied "
                                      f"(${memo.total} credited).")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("invoice_detail", invoice_pk)
    return render(request, "invoicing/creditmemo_form.html",
                  {"invoice": invoice, "lines": inv_lines})


@module_required("invoices")
def creditmemo_list(request):
    memos = CreditMemo.objects.filter(is_deleted=False).select_related(
        "customer", "invoice")[:200]
    return render(request, "invoicing/creditmemo_list.html", {"memos": memos})
