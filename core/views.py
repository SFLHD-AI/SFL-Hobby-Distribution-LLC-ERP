import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import redirect, render
from django.utils import timezone

from core.forms import CompanySettingsForm, NumberSequenceForm
from core.models import ActivityLog, CompanySettings, NumberSequence, log_activity
from core.roles import admin_required, can_see_costs, module_required
from core.tasks import money_attention
from customers.models import Customer, ResaleCertificate
from inventory.models import Product, Receipt
from invoicing.models import Invoice
from orders.models import PurchaseOrder

log = logging.getLogger(__name__)


def _month_close_tile():
    """The previous month's close checklist for the dashboard, or None.

    ``accounting.closing.dashboard_tile()`` returns {"label", "year",
    "month", "done", "total", "is_closed", "url"}. The module is optional
    (it ships with the Month close page), so a missing import or a failure
    inside it must never take the dashboard down.
    """
    try:
        from accounting.closing import dashboard_tile
    except ImportError:
        return None
    try:
        tile = dashboard_tile()
    except Exception:          # a bug in the checklist must not hide the dashboard
        log.exception("month close tile failed")
        return None
    return tile if isinstance(tile, dict) and tile.get("label") else None


def _money_widgets(user, today):
    """Everything the dashboard's Money section needs.

    Counts come from ``core.tasks.money_attention`` and are safe for every
    role. Dollar figures (cash position, profit, equity) are only computed
    for users who may see costs — one journal build per basis via
    ``accounting.ledger.dashboard_snapshot`` plus a two-month equity trend.
    """
    from accounting.models import ACCRUAL, CASH
    from banking.models import BankAccount, BankConnection

    ctx = {"attention": money_attention(today), "money": None, "money_error": False}
    ctx["has_bank_accounts"] = BankAccount.objects.filter(active=True).exists()
    # "Sync now" only makes sense when a live bank feed (Plaid) is connected
    ctx["has_connections"] = (BankConnection.objects.filter(is_deleted=False, provider="plaid")
                              .exclude(status="disconnected").exists())
    ctx["month_close"] = _month_close_tile()
    if not can_see_costs(user):
        return ctx

    settings_row = CompanySettings.get()
    default_basis = settings_row.accounting_basis_default or CASH
    other_basis = ACCRUAL if default_basis == CASH else CASH
    try:
        from accounting.ledger import dashboard_snapshot, equity_trend

        rows, cash, cards = [], Decimal("0.00"), Decimal("0.00")
        for acct in BankAccount.objects.filter(active=True):
            bal = acct.current_balance
            rows.append({"account": acct, "balance": bal, "unreviewed": acct.unreviewed_count})
            if acct.is_liability:
                cards += bal
            else:
                cash += bal

        snaps = {CASH: dashboard_snapshot(CASH, today),
                 ACCRUAL: dashboard_snapshot(ACCRUAL, today)}
        trend = equity_trend(ACCRUAL, 2, as_of=today)
        prev = trend[-2] if len(trend) >= 2 else None
        ctx["money"] = {
            "accounts": rows, "cash": cash, "cards": cards, "net": cash - cards,
            "basis": default_basis, "other_basis": other_basis,
            "basis_label": "cash" if default_basis == CASH else "accrual",
            "other_label": "cash" if other_basis == CASH else "accrual",
            "snap": snaps[default_basis], "snap_other": snaps[other_basis],
            "equity": snaps[ACCRUAL]["total_equity"],
            "equity_prev": prev["equity"] if prev else None,
            "equity_prev_label": prev["label"] if prev else "",
            "equity_change": trend[-1]["equity_change"] if trend else None,
            "fy_start": None,
        }
        from accounting.ledger import fiscal_year_start
        ctx["money"]["fy_start"] = fiscal_year_start(today, settings_row.fiscal_year_start_month)
    except Exception:          # the dashboard must never go down over a ledger edge case
        log.exception("dashboard money widgets failed")
        ctx["money_error"] = True
    return ctx


@login_required
def dashboard(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)

    invoices = Invoice.objects.filter(is_deleted=False).exclude(status=Invoice.VOID)
    today_revenue = invoices.filter(date=today).aggregate(t=models.Sum("total"))["t"] or 0
    mtd_revenue = invoices.filter(date__gte=month_start).aggregate(t=models.Sum("total"))["t"] or 0

    open_pos = (PurchaseOrder.objects.filter(is_deleted=False,
                                             status__in=PurchaseOrder.OPEN_STATUSES)
                .values("status").annotate(n=models.Count("id")))
    status_counts = {r["status"]: r["n"] for r in open_pos}
    pick_queue = PurchaseOrder.objects.filter(
        is_deleted=False, status__in=[PurchaseOrder.CONFIRMED, PurchaseOrder.PICKING]).count()

    low_stock = [p for p in Product.objects.filter(active=True)
                              .select_related("brand")
                 if p.is_low_stock and not p.low_stock_alert_muted][:12]

    open_invoices = [i for i in invoices if i.balance_due > 0]
    ar_total = sum((i.balance_due for i in open_invoices), Decimal("0"))
    overdue = sum((i.balance_due for i in open_invoices if i.is_overdue), Decimal("0"))
    debtor_totals = {}
    for i in open_invoices:
        debtor_totals[i.customer] = debtor_totals.get(i.customer, Decimal("0")) + i.balance_due
    top_debtors = sorted(debtor_totals.items(), key=lambda kv: -kv[1])[:5]

    soon = today + timedelta(days=30)
    expiring_certs = (ResaleCertificate.objects
                      .filter(is_deleted=False, expiration_date__lte=soon,
                              customer__is_deleted=False, customer__active=True)
                      .select_related("customer").order_by("expiration_date")[:10])

    inbound = Receipt.objects.filter(is_deleted=False, posted=False)[:8]
    recent_pos = PurchaseOrder.objects.filter(is_deleted=False).select_related("customer")[:8]

    ctx = {
        "today_revenue": today_revenue, "mtd_revenue": mtd_revenue,
        "status_counts": status_counts, "pick_queue": pick_queue,
        "low_stock": low_stock, "ar_total": ar_total, "overdue": overdue,
        "top_debtors": top_debtors, "expiring_certs": expiring_certs,
        "inbound": inbound, "recent_pos": recent_pos,
        "statuses": PurchaseOrder.STATUS_CHOICES,
        "today": today, "month_start": month_start,
    }
    ctx.update(_money_widgets(request.user, today))
    if ctx["money_error"]:
        messages.warning(request, "The money numbers could not be calculated right now. "
                                  "Everything else on this page is current.")
    return render(request, "core/dashboard.html", ctx)


def plaid_configured(settings_row=None):
    """True when a Plaid client id AND secret are present, either on the
    Settings row or in the environment."""
    settings_row = settings_row or CompanySettings.get()
    client_id = settings_row.plaid_client_id or getattr(django_settings, "PLAID_CLIENT_ID", "")
    secret = settings_row.plaid_secret or getattr(django_settings, "PLAID_SECRET", "")
    return bool(client_id and secret)


@admin_required
def settings_edit(request):
    obj = CompanySettings.get()
    seqs = []
    for key, label in NumberSequence.KEYS:
        seq, _ = NumberSequence.objects.get_or_create(
            key=key, defaults={"prefix": {"po": "PO-", "invoice": "INV-", "credit": "CM-"}[key]})
        seqs.append((label, seq))
    if request.method == "POST":
        form = CompanySettingsForm(request.POST, request.FILES, instance=obj)
        seq_forms = [(label, NumberSequenceForm(request.POST, instance=seq, prefix=f"seq_{seq.key}"))
                     for label, seq in seqs]
        if form.is_valid() and all(f.is_valid() for _, f in seq_forms):
            form.save()
            for _, f in seq_forms:
                f.save()
            log_activity(request.user, "settings_updated", obj)
            messages.success(request, "Settings saved.")
            return redirect("settings_edit")
        messages.error(request, "Settings were not saved — please fix the highlighted fields.")
    else:
        form = CompanySettingsForm(instance=obj)
        seq_forms = [(label, NumberSequenceForm(instance=seq, prefix=f"seq_{seq.key}"))
                     for label, seq in seqs]
    from shipping.providers import get_provider
    env_plaid = bool(getattr(django_settings, "PLAID_CLIENT_ID", "")
                     and getattr(django_settings, "PLAID_SECRET", ""))
    return render(request, "core/settings.html", {
        "form": form, "seq_forms": seq_forms,
        "shipstation_configured": get_provider("shipstation").is_configured(),
        "uship_configured": get_provider("uship").is_configured(),
        "shippo_configured": get_provider("shippo").is_configured(),
        "plaid_configured": plaid_configured(obj),
        "plaid_from_env": env_plaid and not (obj.plaid_client_id and obj.plaid_secret),
        "public_base_url": getattr(django_settings, "PUBLIC_BASE_URL", ""),
    })


@admin_required
def activity_log(request):
    qs = ActivityLog.objects.select_related("user")
    model = request.GET.get("model")
    if model:
        qs = qs.filter(model=model)
    user_id = request.GET.get("user")
    if user_id:
        qs = qs.filter(user_id=user_id)
    q = request.GET.get("q")
    if q:
        qs = qs.filter(models.Q(object_repr__icontains=q) | models.Q(action__icontains=q))
    models_list = (ActivityLog.objects.values_list("model", flat=True)
                   .distinct().order_by("model"))
    from django.contrib.auth.models import User
    return render(request, "core/activity_log.html", {
        "entries": qs[:300], "models_list": models_list,
        "users": User.objects.filter(is_active=True).order_by("username"),
    })


@module_required("dashboard")
def global_search(request):
    q = (request.GET.get("q") or "").strip()
    results = {"q": q}
    if q:
        from invoicing.models import Invoice as Inv
        from shipping.models import Shipment
        results["products"] = Product.objects.filter(
            models.Q(sku__icontains=q) | models.Q(name__icontains=q) |
            models.Q(upc__icontains=q))[:20]
        results["customers"] = Customer.objects.filter(
            models.Q(business_name__icontains=q) | models.Q(email__icontains=q) |
            models.Q(phone__icontains=q))[:20]
        results["pos"] = PurchaseOrder.objects.filter(
            models.Q(number__icontains=q) | models.Q(customer_po_ref__icontains=q)
        ).select_related("customer")[:20]
        results["invoices"] = Inv.objects.filter(number__icontains=q).select_related("customer")[:20]
        results["shipments"] = (Shipment.objects.filter(tracking_number__icontains=q)
                                .select_related("po", "po__customer")[:20])
    return render(request, "core/search.html", results)
