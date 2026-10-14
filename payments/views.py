from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from core.csvutils import csv_response
from core.models import log_activity
from core.roles import module_required
from customers.models import Customer
from invoicing.models import Invoice
from payments.models import Payment, PaymentApplication


@module_required("payments")
def ar_adjustment(request):
    """Manually put an amount ONTO a customer's AR balance — used to
    transfer opening balances from a previous system, or for corrections.

    Creates a manual invoice (no PO, no tax, no inventory impact) with the
    date you choose, so AR aging buckets stay accurate for old balances.
    To REDUCE a balance, record a payment or issue a credit against the
    invoice instead — that keeps a clean audit trail.
    """
    from datetime import date as date_cls, timedelta

    from django.utils import timezone
    customers = Customer.objects.filter(is_deleted=False).order_by("business_name")
    if request.method == "POST":
        try:
            customer = get_object_or_404(Customer, pk=request.POST.get("customer_id"),
                                         is_deleted=False)
            amount = Decimal(request.POST.get("amount", "").strip())
            if amount <= 0:
                raise ValidationError("Enter an amount greater than zero. "
                                      "(To reduce a balance, record a payment "
                                      "or credit instead.)")
            memo = request.POST.get("memo", "").strip() or "Opening balance transfer"
            inv_date = (date_cls.fromisoformat(request.POST["date"])
                        if request.POST.get("date") else timezone.localdate())
            due_raw = request.POST.get("due_date", "").strip()
            due = (date_cls.fromisoformat(due_raw) if due_raw
                   else inv_date + timedelta(days=customer.term_days))
            with transaction.atomic():
                invoice = Invoice.build_manual(customer, user=request.user,
                                               tax_rate=Decimal("0"),
                                               notes="AR adjustment")
                invoice.date = inv_date
                invoice.due_date = due
                invoice.is_ar_adjustment = True   # keep out of revenue/profit reports
                invoice.save(update_fields=["date", "due_date", "is_ar_adjustment"])
                invoice.add_manual_line(description=memo, qty=1,
                                        unit_price=amount, user=request.user)
            log_activity(request.user, "ar_adjustment", invoice,
                         note=f"{customer.business_name} +${amount} ({memo})")
            messages.success(request, f"${amount} added to {customer.business_name}'s "
                                      f"balance as {invoice.number} "
                                      f"(dated {inv_date}, due {due}).")
            return redirect("payment_list")
        except (ValidationError, InvalidOperation, ValueError) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else \
                "Enter a valid amount and dates."
            messages.error(request, msg)
    return render(request, "payments/ar_adjustment.html", {"customers": customers})


@module_required("payments")
def payment_list(request):
    qs = Payment.objects.filter(is_deleted=False).select_related("customer")
    customer_id = request.GET.get("customer")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    payments = list(qs[:300])
    if request.GET.get("export") == "csv":
        return csv_response(
            "payments.csv",
            ["date", "customer", "amount", "method", "reference", "applied", "unapplied"],
            [[p.date, p.customer.business_name, p.amount, p.method, p.reference,
              p.amount_applied, p.amount_unapplied] for p in payments])
    return render(request, "payments/payment_list.html", {
        "payments": payments,
        "customers": Customer.objects.filter(active=True),
    })


@module_required("payments")
def payment_create(request):
    customer = None
    customer_id = request.GET.get("customer") or request.POST.get("customer")
    if customer_id:
        customer = Customer.objects.filter(pk=customer_id).first()

    open_invoices = []
    if customer:
        open_invoices = [i for i in customer.invoices.filter(is_deleted=False)
                         .exclude(status=Invoice.VOID).order_by("due_date")
                         if i.balance_due > 0]

    if request.method == "POST" and customer:
        try:
            amount = Decimal(request.POST["amount"])
            if amount <= 0:
                raise InvalidOperation
        except (KeyError, InvalidOperation):
            messages.error(request, "Enter a valid payment amount.")
            return redirect(f"{request.path}?customer={customer.pk}")
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    customer=customer, amount=amount,
                    date=request.POST.get("date") or None,
                    method=request.POST.get("method", "check"),
                    reference=request.POST.get("reference", ""),
                    notes=request.POST.get("notes", ""),
                    created_by=request.user)
                for inv in open_invoices:
                    raw = request.POST.get(f"apply_{inv.pk}", "").strip()
                    if not raw:
                        continue
                    apply_amt = Decimal(raw)
                    if apply_amt > 0:
                        payment.apply_to_invoice(inv, apply_amt, user=request.user)
            log_activity(request.user, "payment_recorded", payment)
            if payment.amount_unapplied > 0:
                messages.warning(request,
                                 f"Payment saved; ${payment.amount_unapplied} is unapplied "
                                 f"(apply it later from the payment page).")
            else:
                messages.success(request, "Payment recorded and applied.")
            return redirect("payment_detail", payment.pk)
        except (ValidationError, InvalidOperation) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)

    preselect_invoice = request.GET.get("invoice")
    return render(request, "payments/payment_form.html", {
        "customers": Customer.objects.filter(active=True),
        "customer": customer, "open_invoices": open_invoices,
        "methods": Payment.METHODS, "preselect_invoice": preselect_invoice,
    })


@module_required("payments")
def payment_detail(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("customer"), pk=pk)
    open_invoices = [i for i in payment.customer.invoices.filter(is_deleted=False)
                     .exclude(status=Invoice.VOID).order_by("due_date")
                     if i.balance_due > 0]
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "apply":
                inv = get_object_or_404(Invoice, pk=request.POST["invoice_id"])
                payment.apply_to_invoice(inv, Decimal(request.POST["amount"]),
                                         user=request.user)
                messages.success(request, "Applied.")
            elif action == "unapply":
                app = get_object_or_404(PaymentApplication,
                                        pk=request.POST["app_id"], payment=payment)
                payment.unapply(app, user=request.user)
                messages.success(request, "Unapplied.")
            elif action == "delete":
                if payment.applications.exists():
                    messages.error(request, "Unapply from all invoices first.")
                else:
                    payment.delete(user=request.user)
                    messages.success(request, "Payment deleted (recoverable).")
                    return redirect("payment_list")
        except (ValidationError, InvalidOperation) as e:
            msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
            messages.error(request, msg)
        return redirect("payment_detail", pk)
    return render(request, "payments/payment_detail.html", {
        "payment": payment,
        "applications": payment.applications.select_related("invoice"),
        "open_invoices": open_invoices,
    })
