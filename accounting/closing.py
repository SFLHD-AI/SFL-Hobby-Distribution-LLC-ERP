"""
Month-end close.

The owner should close every month shortly after it ends. A close has two
kinds of steps:

* **automatic checks** computed live from the documents (bank inbox empty,
  receipts attached, bills paid, recurring templates caught up), and
* **manual steps** the owner ticks off (statements reconciled, PDFs saved,
  P&L eyeballed), stored on ``accounting.models.MonthClose``.

``month_status(year, month)`` returns everything the month-close page needs;
``dashboard_tile()`` is the small summary the home dashboard shows for the
month the owner should be closing right now (the previous month).
"""
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from accounting.ledger import month_end
from accounting.models import FiscalPeriodLock, MonthClose

ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# calendar helpers
# ---------------------------------------------------------------------------
def previous_month(today=None):
    """(year, month) of the month before ``today`` — the one to close."""
    today = today or timezone.localdate()
    last = today.replace(day=1) - timedelta(days=1)
    return last.year, last.month


def month_bounds(year, month):
    start = date(year, month, 1)
    return start, month_end(start)


def shift_month(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def month_label(year, month):
    return date(year, month, 1).strftime("%B %Y")


def month_param(year, month):
    return f"{year}-{month:02d}"


def month_url(year, month):
    return f"{reverse('accounting_month_close')}?month={month_param(year, month)}"


# ---------------------------------------------------------------------------
# automatic checks (live)
# ---------------------------------------------------------------------------
def _receipt_missing(expense):
    """No receipt on the expense and none on its live bank transaction."""
    if expense.receipt_file:
        return False
    txn = expense.bank_transaction
    return not (txn is not None and txn.receipt_file)


def unreviewed_count(start, end):
    """Bank/card transactions dated in the period still waiting for review."""
    from banking.models import BankTransaction
    return BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED,
                                          date__gte=start, date__lte=end).count()


def missing_receipt_expenses(start, end):
    """Live expenses dated in the period with no receipt anywhere."""
    from expenses.models import Expense
    qs = (Expense.objects.filter(is_deleted=False, date__gte=start, date__lte=end)
          .select_related("bank_txn"))
    return [e for e in qs if _receipt_missing(e)]


def unpaid_overdue(end):
    """Unpaid expenses and posted, unpaid supplier receipts whose due date
    (or document date when there is none) is on or before ``end``.

    Returns (count, total still owed)."""
    from expenses.models import Expense
    from inventory.models import Receipt
    count, total = 0, ZERO
    expenses = (Expense.objects.filter(is_deleted=False, date__lte=end, amount__gt=0)
                .select_related("bank_txn"))
    for e in expenses:
        if e.is_paid:
            continue
        if (e.due_date or e.date) <= end:
            count += 1
            total += e.amount
    receipts = (Receipt.objects.filter(is_deleted=False, posted=True, date__lte=end)
                .prefetch_related("lines"))
    for r in receipts:
        if (r.due_date or r.date) > end:
            continue
        balance = r.balance_due
        if balance <= 0:
            continue
        count += 1
        total += balance
    return count, total


def recurring_due_count():
    """Active recurring templates whose next date has passed (nothing
    generated yet)."""
    from expenses.models import RecurringExpense
    return sum(1 for t in RecurringExpense.objects.filter(active=True) if t.is_due)


def automatic_checks(start, end):
    """The four live checks for [start, end], each as a dict:
    key, label, count, amount (or None), ok, url, link_label, help."""
    s, e = start.isoformat(), end.isoformat()
    unreviewed = unreviewed_count(start, end)
    missing = missing_receipt_expenses(start, end)
    unpaid_n, unpaid_total = unpaid_overdue(end)
    recurring = recurring_due_count()
    inbox_qs = urlencode({"status": "unreviewed", "start": s, "end": e, "from": s, "to": e})
    return [
        {"key": "unreviewed", "label": "Bank inbox is empty",
         "count": unreviewed, "amount": None, "ok": unreviewed == 0,
         "url": f"{reverse('bank_inbox')}?{inbox_qs}",
         "link_label": "Review them", "noun": "transaction",
         "help": "Every bank and card transaction dated in the month has been categorized, "
                 "matched or marked as a transfer."},
        {"key": "missing_receipts", "label": "Every expense has a receipt",
         "count": len(missing), "amount": sum((x.amount for x in missing), ZERO),
         "ok": not missing,
         "url": f"{reverse('expense_report_missing_receipts')}?{urlencode({'start': s, 'end': e})}",
         "link_label": "Attach receipts", "noun": "expense",
         "help": "Expenses dated in the month with no photo or PDF on the expense or on its "
                 "bank transaction."},
        {"key": "unpaid_overdue", "label": "No bills past due",
         "count": unpaid_n, "amount": unpaid_total, "ok": unpaid_n == 0,
         "url": reverse("expense_report_unpaid_bills"),
         "link_label": "See open bills", "noun": "bill",
         "help": "Unpaid expenses and supplier bills that were due on or before the last day "
                 "of the month. Pay them or fix the due date."},
        {"key": "recurring_due", "label": "Recurring expenses are caught up",
         "count": recurring, "amount": None, "ok": recurring == 0,
         "url": reverse("expense_recurring"),
         "link_label": "Generate them", "noun": "template",
         "help": "Active recurring templates whose next date has passed without an expense "
                 "being created."},
    ]


# ---------------------------------------------------------------------------
# month status
# ---------------------------------------------------------------------------
def month_status(year, month, create=True):
    """Everything about one month's close, computed live.

    Keys: year, month, label, start, end, checks (list, see
    ``automatic_checks``), the four flat counts (``unreviewed``,
    ``missing_receipts``, ``unpaid_overdue``, ``recurring_due``),
    ``unpaid_total``, manual (list of {key, label, checked}), month_close
    (the MonthClose row — created when ``create`` is true), is_closed,
    done, total, is_complete, locked, prev (year, month), next (year,
    month) or None when the next month has not started, url."""
    today = timezone.localdate()
    start, end = month_bounds(year, month)
    checks = automatic_checks(start, end)
    if create:
        row, _ = MonthClose.objects.get_or_create(year=year, month=month)
    else:
        row = MonthClose.objects.filter(year=year, month=month).first() or MonthClose(year=year, month=month)
    steps = row.steps or {}
    manual = [{"key": key, "label": label, "checked": bool(steps.get(key))}
              for key, label in MonthClose.MANUAL_STEPS]
    done = sum(1 for c in checks if c["ok"]) + sum(1 for m in manual if m["checked"])
    total = len(checks) + len(manual)
    counts = {c["key"]: c["count"] for c in checks}
    nxt = shift_month(year, month, 1)
    return {
        "year": year, "month": month, "label": month_label(year, month),
        "start": start, "end": end, "checks": checks,
        "unreviewed": counts["unreviewed"], "missing_receipts": counts["missing_receipts"],
        "unpaid_overdue": counts["unpaid_overdue"], "recurring_due": counts["recurring_due"],
        "unpaid_total": next(c["amount"] for c in checks if c["key"] == "unpaid_overdue"),
        "manual": manual, "month_close": row, "is_closed": row.is_closed,
        "done": done, "total": total, "is_complete": done == total,
        "locked": FiscalPeriodLock.get().is_locked(end),
        "prev": shift_month(year, month, -1),
        "next": nxt if date(nxt[0], nxt[1], 1) <= today else None,
        "url": month_url(year, month),
    }


def dashboard_tile(today=None):
    """Summary of the month the owner should be closing (the previous month)
    for the home dashboard: {label, year, month, done, total, is_closed, url}."""
    year, month = previous_month(today)
    st = month_status(year, month, create=False)
    return {"label": st["label"], "year": year, "month": month, "done": st["done"],
            "total": st["total"], "is_closed": st["is_closed"], "url": st["url"]}


def months_in_range(start, end):
    """[(year, month), ...] for every calendar month touching [start, end]."""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        out.append((y, m))
        y, m = shift_month(y, m, 1)
    return out


def close_rows_for(months):
    """{(year, month): MonthClose} for the given months (no rows created)."""
    if not months:
        return {}
    years = {y for y, _ in months}
    rows = MonthClose.objects.filter(year__in=years)
    wanted = set(months)
    return {(r.year, r.month): r for r in rows if (r.year, r.month) in wanted}
