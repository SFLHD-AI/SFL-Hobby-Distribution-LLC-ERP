"""
Shared WeasyPrint rendering helpers.
"""
import io

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string


def render_pdf(template, context, base_url=None):
    """Render a Django template to PDF bytes with WeasyPrint."""
    from weasyprint import HTML
    html = render_to_string(template, context)
    buf = io.BytesIO()
    HTML(string=html, base_url=base_url or str(settings.BASE_DIR)).write_pdf(buf)
    return buf.getvalue()


def pdf_response(template, context, filename, inline=True, base_url=None):
    data = render_pdf(template, context, base_url=base_url)
    resp = HttpResponse(data, content_type="application/pdf")
    disp = "inline" if inline else "attachment"
    resp["Content-Disposition"] = f'{disp}; filename="{filename}"'
    return resp


def invoice_context(invoice, company=None, include_balance=False):
    """Build the template context used by both invoice layouts.

    ``include_balance`` adds an account-summary box: this invoice's
    balance, other open invoices, and the customer's total balance —
    so customers can track what they owe overall."""
    from core.models import CompanySettings
    from customers.models import PAYMENT_TERMS

    company = company or CompanySettings.get()
    terms_map = dict(PAYMENT_TERMS)

    groups = {}
    for line in invoice.lines.filter(is_deleted=False).select_related("po", "product"):
        key = line.po.number if line.po_id else ""
        groups.setdefault(key, []).append({
            "description": line.description,
            "sku": line.product.sku if line.product_id else "",
            "qty": line.qty,
            "unit_price": line.unit_price,
            "discount_pct": line.discount_pct,
            "line_total": line.line_total,
        })
    line_groups = [{"label": f"Order {k}" if k else "Items", "lines": v}
                   for k, v in sorted(groups.items())]

    tracking = []
    for line_po in {l.po for l in invoice.lines.filter(is_deleted=False) if l.po_id}:
        for sh in line_po.shipments.filter(is_deleted=False).exclude(tracking_number=""):
            tracking.append(sh.tracking_number)

    tax_note = ""
    if invoice.tax_amount == 0 and invoice.customer.has_valid_certificate:
        cert = invoice.customer.current_certificate
        if cert:
            tax_note = (f"Tax exempt — resale certificate {cert.certificate_number} "
                        f"({cert.issuing_state}).")

    logo_url = None
    if company.logo:
        logo_url = company.logo.path if hasattr(company.logo, "path") else None

    account_balance = previous_balance = None
    if include_balance:
        account_balance = invoice.customer.balance
        previous_balance = account_balance - invoice.balance_due

    return {
        "company": company,
        "invoice": invoice,
        "include_balance": include_balance,
        "account_balance": account_balance,
        "previous_balance": previous_balance,
        "logo_url": logo_url,
        "terms_display": terms_map.get(invoice.terms, invoice.terms),
        "po_refs": ", ".join(invoice.po_numbers),
        "line_groups": line_groups,
        "show_po_groups": len(line_groups) > 1,
        "amount_paid": invoice.amount_paid + invoice.amount_credited,
        "balance_due": invoice.balance_due,
        "tracking_numbers": ", ".join(tracking),
        "tax_exempt_note": tax_note,
    }


def invoice_pdf_bytes(invoice):
    from core.models import CompanySettings
    company = CompanySettings.get()
    template = f"pdf/invoice_{company.invoice_template}.html"
    return render_pdf(template, invoice_context(invoice, company))
