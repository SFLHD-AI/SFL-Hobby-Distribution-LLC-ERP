"""
Core background jobs (Django-Q2) and the "what needs a human" summary that
the dashboard and the weekly review email share.

Schedule with `python manage.py setup_schedules` (done automatically on
container start and by seed_demo):

    core.tasks.send_review_digest   -- Monday 7am
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import models
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core.models import CompanySettings

ZERO = Decimal("0.00")


def money_attention(today=None):
    """Counts (and a few totals) of everything in the money modules that is
    waiting for a person. Pure reads; safe for every role because callers
    decide whether to show the dollar figures.

    Returns a dict:
        unreviewed            bank/card transactions still in the inbox
        unreviewed_oldest     date of the oldest one (or None)
        unreviewed_suggested  of those, how many already carry a suggestion
        bills_due             unpaid expenses + supplier bills due in 7 days (incl. overdue)
        bills_due_total       their combined balance
        bills_overdue         how many of them are already past due
        bills_unpaid_total    every open bill, regardless of due date
        missing_receipts      expenses dated this month with no receipt attached (neither on
                              the expense nor on the bank transaction it came from)
        missing_receipts_total
        expenses_to_review    auto-created expenses (bank feed / recurring) not yet confirmed
        connections_attention bank connections in error or needing re-authentication
        total                 sum of the counts above (for the badge / subject line)
    """
    from banking.models import BankConnection, BankTransaction
    from expenses.models import Expense
    from inventory.models import Receipt

    today = today or timezone.localdate()
    soon = today + timedelta(days=7)
    month_start = today.replace(day=1)

    unreviewed = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
    unreviewed_count = unreviewed.count()
    oldest = unreviewed.order_by("date").values_list("date", flat=True).first()
    suggested = unreviewed.filter(suggested_account__isnull=False).count()

    # Open bills: expenses with no paid date and no bank transaction behind them.
    # Same rule as the Unpaid bills report: a bill with no due date is aged
    # from its bill date, so an old undated bill still shows up as overdue.
    open_expenses = (Expense.objects.filter(is_deleted=False, paid_date__isnull=True,
                                            bank_txn__isnull=True, amount__gt=0,
                                            date__lte=today)
                     .annotate(aged_from=Coalesce("due_date", "date")))
    due_expenses = open_expenses.filter(aged_from__lte=soon)
    bills_due = due_expenses.count()
    bills_due_total = due_expenses.aggregate(t=models.Sum("amount"))["t"] or ZERO
    bills_overdue = open_expenses.filter(aged_from__lt=today).count()
    bills_unpaid_total = open_expenses.aggregate(t=models.Sum("amount"))["t"] or ZERO

    # Posted supplier receipts that still carry a balance (partly paid ones too)
    for r in (Receipt.objects.filter(is_deleted=False, posted=True, date__lte=today)
              .prefetch_related("lines", "supplier_payments")):
        balance = r.balance_due
        if balance <= 0:
            continue
        aged_from = r.due_date or r.date
        bills_unpaid_total += balance
        if aged_from <= soon:
            bills_due += 1
            bills_due_total += balance
            if aged_from < today:
                bills_overdue += 1

    # Missing receipts: a receipt attached to the bank transaction the expense
    # came from counts, exactly as on the Missing receipts report.
    missing_rows = [e for e in Expense.objects.filter(is_deleted=False, amount__gt=0,
                                                      date__gte=month_start, date__lte=today)
                    .select_related("bank_txn")
                    if _receipt_missing(e)]
    missing = len(missing_rows)
    missing_total = sum((e.amount for e in missing_rows), ZERO)

    to_review = Expense.objects.filter(is_deleted=False, is_reviewed=False).count()
    connections = BankConnection.objects.filter(is_deleted=False,
                                                status__in=["error", "relink"]).count()

    return {
        "today": today, "soon": soon, "month_start": month_start,
        "unreviewed": unreviewed_count, "unreviewed_oldest": oldest,
        "unreviewed_suggested": suggested,
        "bills_due": bills_due, "bills_due_total": bills_due_total,
        "bills_overdue": bills_overdue, "bills_unpaid_total": bills_unpaid_total,
        "missing_receipts": missing, "missing_receipts_total": missing_total,
        "expenses_to_review": to_review,
        "connections_attention": connections,
        "total": unreviewed_count + bills_due + missing + to_review + connections,
    }


def _receipt_missing(expense):
    """True when neither the expense nor its bank transaction holds a receipt.
    Delegates to the Missing receipts report so both screens agree."""
    try:
        from expenses.reports import receipt_missing
    except ImportError:                      # report module not available: same rule inline
        if expense.has_receipt:
            return False
        txn = expense.bank_transaction
        return not (txn is not None and txn.receipt_file)
    return receipt_missing(expense)


def _link(url_name):
    """Absolute link for emails when PUBLIC_BASE_URL is set, else just the path."""
    try:
        path = reverse(url_name)
    except NoReverseMatch:
        return ""
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}{path}"


def send_review_digest():
    """Weekly email listing what needs a look in the money modules (if
    enabled in Settings). Returns a one-line summary for the task log."""
    from banking.models import BankConnection, BankTransaction

    company = CompanySettings.get()
    if not (company.expense_review_digest and company.alert_email):
        return "Review digest disabled."

    info = money_attention()
    if info["total"] == 0:
        return "Nothing to review this week."

    today = info["today"]
    lines = [f"Money review for {company.company_name} — week of {today:%b %-d, %Y}", ""]

    if info["unreviewed"]:
        oldest = info["unreviewed_oldest"]
        age = f", oldest from {oldest:%b %-d}" if oldest else ""
        lines.append(f"* {info['unreviewed']} bank/card transaction(s) waiting for review{age}.")
        if info["unreviewed_suggested"]:
            lines.append(f"    {info['unreviewed_suggested']} already have a suggested category — "
                         f"one click to confirm.")
        link = _link("bank_inbox")
        if link:
            lines.append(f"    Review: {link}")
        lines.append("")

    if info["bills_due"]:
        overdue = f" ({info['bills_overdue']} overdue)" if info["bills_overdue"] else ""
        lines.append(f"* {info['bills_due']} bill(s) due by {info['soon']:%b %-d}{overdue}: "
                     f"${info['bills_due_total']:,.2f}.")
        link = _link("expense_report_unpaid_bills")
        if link:
            lines.append(f"    Unpaid bills: {link}")
        lines.append("")

    if info["missing_receipts"]:
        lines.append(f"* {info['missing_receipts']} expense(s) this month have no receipt attached "
                     f"(${info['missing_receipts_total']:,.2f}). Snap them now, not at year end.")
        link = _link("expense_report_missing_receipts")
        if link:
            lines.append(f"    Missing receipts: {link}")
        lines.append("")

    if info["expenses_to_review"]:
        lines.append(f"* {info['expenses_to_review']} expense(s) were created automatically "
                     f"(bank feed or recurring) and need a quick confirmation.")
        link = _link("expense_list")
        if link:
            lines.append(f"    Expenses: {link}?review=yes")
        lines.append("")

    if info["connections_attention"]:
        names = list(BankConnection.objects.filter(is_deleted=False, status__in=["error", "relink"])
                     .values_list("institution_name", flat=True))
        lines.append(f"* {info['connections_attention']} bank connection(s) need attention: "
                     + ", ".join(names) + ". Press Re-login on Banking → Connect a bank so "
                     f"transactions keep flowing.")
        link = _link("bank_accounts")
        if link:
            lines.append(f"    Bank accounts: {link}")
        lines.append("")

    unreviewed_total = (BankTransaction.objects
                        .filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
                        .aggregate(t=models.Sum("amount"))["t"])
    if unreviewed_total is not None:
        lines.append(f"Until reviewed, ${abs(unreviewed_total):,.2f} shows on your statements as "
                     f"'Uncategorized'.")
    lines.append("")
    lines.append("— SFL ERP System")

    EmailMessage(
        subject=f"[SFL ERP] Weekly money review: {info['total']} item(s) need a look",
        body="\n".join(lines),
        to=[company.alert_email]).send(fail_silently=True)
    return (f"Sent review digest: {info['unreviewed']} transaction(s) to review, "
            f"{info['bills_due']} bill(s) due, {info['missing_receipts']} missing receipt(s), "
            f"{info['expenses_to_review']} expense(s) to confirm, "
            f"{info['connections_attention']} connection(s) needing attention.")
