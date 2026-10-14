from decimal import Decimal

from django.contrib import messages
from django.core.mail import EmailMessage
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.csvutils import csv_response
from core.models import CompanySettings, log_activity
from core.pdf import pdf_response, render_pdf
from core.roles import can_see_costs, module_required
from customers.forms import (CertificateForm, ContactForm, CustomerForm,
                             ShippingAddressForm)
from customers.models import Contact, Customer, ResaleCertificate, ShippingAddress


@module_required("customers")
def customer_list(request):
    qs = Customer.objects.select_related("price_list")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(models.Q(business_name__icontains=q) |
                       models.Q(email__icontains=q) | models.Q(phone__icontains=q))
    if request.GET.get("active") == "1":
        qs = qs.filter(active=True)
    customers = list(qs)

    if request.GET.get("export") == "csv":
        return csv_response(
            "customers.csv",
            ["business_name", "email", "phone", "city", "state", "terms",
             "price_list", "cert_status", "balance"],
            [[c.business_name, c.email, c.phone, c.billing_city, c.billing_state,
              c.get_payment_terms_display(), c.price_list or "",
              c.certificate_status, c.balance] for c in customers])
    return render(request, "customers/customer_list.html", {"customers": customers})


@module_required("customers")
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        customer = form.save()
        log_activity(request.user, "create", customer)
        messages.success(request, f"{customer.business_name} created. Add a shipping address and resale certificate below.")
        return redirect("customer_detail", customer.pk)
    return render(request, "customers/customer_form.html", {"form": form, "customer": None})


@module_required("customers")
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=customer)
    if request.method == "POST" and form.is_valid():
        changes = {f: str(form.cleaned_data.get(f)) for f in form.changed_data}
        form.save()
        log_activity(request.user, "update", customer, changes=changes)
        messages.success(request, "Saved.")
        return redirect("customer_detail", customer.pk)
    return render(request, "customers/customer_form.html", {"form": form, "customer": customer})


@module_required("customers")
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    contact_form, address_form, cert_form = ContactForm(), ShippingAddressForm(), CertificateForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_contact":
            contact_form = ContactForm(request.POST)
            if contact_form.is_valid():
                c = contact_form.save(commit=False)
                c.customer = customer
                c.save()
                messages.success(request, f"Contact “{c.name}” added.")
                return redirect("customer_detail", pk)
            messages.error(request, f"Contact not saved: {contact_form.errors.as_text()}")
        elif action == "del_contact":
            get_object_or_404(Contact, pk=request.POST.get("id"), customer=customer).delete(user=request.user)
            return redirect("customer_detail", pk)
        elif action == "add_address":
            address_form = ShippingAddressForm(request.POST)
            if address_form.is_valid():
                a = address_form.save(commit=False)
                a.customer = customer
                if a.is_default or not customer.shipping_addresses.filter(
                        is_deleted=False).exists():
                    customer.shipping_addresses.update(is_default=False)
                    a.is_default = True
                a.save()
                messages.success(request, f"Address “{a.label}” saved.")
                return redirect("customer_detail", pk)
            messages.error(request, f"Address not saved: {address_form.errors.as_text()}")
        elif action == "del_address":
            get_object_or_404(ShippingAddress, pk=request.POST.get("id"), customer=customer).delete(user=request.user)
            return redirect("customer_detail", pk)
        elif action == "default_address":
            addr = get_object_or_404(ShippingAddress, pk=request.POST.get("id"), customer=customer)
            customer.shipping_addresses.update(is_default=False)
            addr.is_default = True
            addr.save(update_fields=["is_default"])
            return redirect("customer_detail", pk)
        elif action == "add_cert":
            cert_form = CertificateForm(request.POST, request.FILES)
            if cert_form.is_valid():
                cert = cert_form.save(commit=False)
                cert.customer = customer
                cert.save()
                log_activity(request.user, "cert_uploaded", customer,
                             note=cert.certificate_number)
                messages.success(request, f"Certificate {cert.certificate_number} saved.")
                return redirect("customer_detail", pk)
            messages.error(request, f"Certificate not saved: {cert_form.errors.as_text()}")
        elif action == "del_cert":
            get_object_or_404(ResaleCertificate, pk=request.POST.get("id"),
                              customer=customer).delete(user=request.user)
            return redirect("customer_detail", pk)

    pos = customer.purchase_orders.filter(is_deleted=False).select_related()[:50]
    invoices = customer.invoices.filter(is_deleted=False)[:50]
    payments = customer.payments.filter(is_deleted=False)[:50]

    show_costs = can_see_costs(request.user)
    lifetime_revenue = lifetime_profit = Decimal("0")
    for inv in customer.invoices.filter(is_deleted=False).exclude(status="void"):
        lifetime_revenue += inv.total
        if show_costs:
            lifetime_profit += inv.gross_profit

    return render(request, "customers/customer_detail.html", {
        "customer": customer, "pos": pos, "invoices": invoices, "payments": payments,
        "contact_form": contact_form, "address_form": address_form, "cert_form": cert_form,
        "show_costs": show_costs,
        "lifetime_revenue": lifetime_revenue, "lifetime_profit": lifetime_profit,
        "open_invoices": [i for i in invoices if i.balance_due > 0],
    })


def _statement_context(customer):
    invoices = [i for i in customer.invoices.filter(is_deleted=False)
                .exclude(status="void").order_by("date") if i.balance_due > 0]
    return {
        "company": CompanySettings.get(),
        "customer": customer,
        "invoices": invoices,
        "total_due": sum((i.balance_due for i in invoices), Decimal("0")),
        "today": timezone.localdate(),
    }


@module_required("customers")
def customer_statement_pdf(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    return pdf_response("pdf/statement.html", _statement_context(customer),
                        f"statement_{customer.business_name}.pdf")


@module_required("customers")
def customer_statement_email(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if not customer.email:
        messages.error(request, "Customer has no email on file.")
        return redirect("customer_detail", pk)
    company = CompanySettings.get()
    pdf = render_pdf("pdf/statement.html", _statement_context(customer))
    msg = EmailMessage(
        subject=f"Statement from {company.company_name}",
        body=(f"Hi {customer.business_name},\n\nAttached is your current statement "
              f"showing all open invoices.\n\nThank you,\n{company.company_name}\n"
              f"{company.phone}"),
        to=[customer.email])
    msg.attach(f"statement_{timezone.localdate()}.pdf", pdf, "application/pdf")
    msg.send(fail_silently=False)
    log_activity(request.user, "statement_emailed", customer)
    messages.success(request, f"Statement emailed to {customer.email}.")
    return redirect("customer_detail", pk)
