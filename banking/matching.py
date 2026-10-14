"""
Match suggestions for one bank transaction.

``suggestions(txn)`` looks for documents already in the system that explain
the money movement, so the owner links instead of re-typing:

* **payments** — customer payments not yet tied to a deposit, same amount,
  within ±7 days; plus **payment_groups**: small sets (2–6) of same-day
  unlinked payments that add up to the deposit (one bank deposit often
  covers several checks).
* **receipts** — unpaid supplier receipts (bills), posted or not, whose
  landed total is within 1% of the amount.
* **expenses** — expense rows entered by hand (not yet linked to a bank
  row), same amount, within ±5 days.
* **transfer_twins** — the opposite-sign row in another account, same
  amount, within ±3 days, still open.

Every query is bounded so the detail page stays fast on big books.
"""
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from django.db.models import Q

from banking.models import BankTransaction
from banking.rules import find_transfer_twin

PAYMENT_WINDOW_DAYS = 7
EXPENSE_WINDOW_DAYS = 5
RECEIPT_TOLERANCE = Decimal("0.01")      # 1 %
MAX_CANDIDATES = 60
MAX_GROUP_SIZE = 6
MAX_SAMEDAY_POOL = 14                    # C(14, 6) = 3003 combinations max
MAX_GROUPS = 5


def _unlinked_payments(txn):
    from payments.models import Payment
    amt = abs(txn.amount)
    lo, hi = txn.date - timedelta(days=PAYMENT_WINDOW_DAYS), txn.date + timedelta(days=PAYMENT_WINDOW_DAYS)
    linked_ids = (BankTransaction.objects
                  .filter(is_deleted=False, payments__isnull=False)
                  .exclude(status=BankTransaction.IGNORED)
                  .exclude(pk=txn.pk)
                  .values_list("payments__id", flat=True))
    qs = (Payment.objects.filter(is_deleted=False, date__gte=lo, date__lte=hi, amount__gt=0,
                                 amount__lte=amt)
          .exclude(pk__in=linked_ids)
          .select_related("customer").order_by("date", "id"))
    return list(qs[:MAX_CANDIDATES])


def payment_matches(txn):
    """(exact_payments, payment_groups) for a deposit."""
    if txn.amount <= 0:
        return [], []
    amt = abs(txn.amount)
    pool = _unlinked_payments(txn)
    exact = [p for p in pool if p.amount == amt]
    groups = []
    by_day = {}
    for p in pool:
        if p.amount < amt:
            by_day.setdefault(p.date, []).append(p)
    for day in sorted(by_day, key=lambda d: abs((d - txn.date).days)):
        day_pool = by_day[day][:MAX_SAMEDAY_POOL]
        if sum(p.amount for p in day_pool) < amt:
            continue
        for size in range(2, min(MAX_GROUP_SIZE, len(day_pool)) + 1):
            for combo in combinations(day_pool, size):
                if sum(p.amount for p in combo) == amt:
                    groups.append(list(combo))
                    if len(groups) >= MAX_GROUPS:
                        return exact, groups
    return exact, groups


def receipt_matches(txn):
    """Unpaid supplier receipts whose landed total is within 1% of |amount|."""
    from inventory.models import Receipt
    if txn.amount >= 0:
        return []
    amt = abs(txn.amount)
    tol = (amt * RECEIPT_TOLERANCE).quantize(Decimal("0.01"))
    linked_ids = (BankTransaction.objects
                  .filter(is_deleted=False, receipts__isnull=False)
                  .exclude(status=BankTransaction.IGNORED)
                  .exclude(pk=txn.pk)
                  .values_list("receipts__id", flat=True))
    qs = (Receipt.objects.filter(is_deleted=False, paid_date__isnull=True)
          .exclude(pk__in=linked_ids)
          .select_related("supplier").prefetch_related("lines")
          .order_by("-date", "-id"))
    out = []
    for r in qs[:300]:
        total = r.landed_total
        if total > 0 and abs(total - amt) <= tol:
            out.append(r)
        if len(out) >= 10:
            break
    return out


def expense_matches(txn):
    """Hand-entered expenses with the same amount, ±5 days, not yet linked."""
    from expenses.models import Expense
    if txn.amount == 0:
        return []
    target = -txn.amount                 # outflow -> positive expense, refund -> negative
    lo, hi = txn.date - timedelta(days=EXPENSE_WINDOW_DAYS), txn.date + timedelta(days=EXPENSE_WINDOW_DAYS)
    qs = (Expense.objects.filter(is_deleted=False, amount=target, bank_txn__isnull=True)
          .filter(Q(date__gte=lo, date__lte=hi) | Q(paid_date__gte=lo, paid_date__lte=hi))
          .select_related("category", "vendor_link", "paid_from").order_by("date"))
    rows = list(qs[:20])
    # prefer expenses paid from this account (or not yet marked paid)
    rows.sort(key=lambda e: (0 if e.paid_from_id in (None, txn.account_id) else 1, e.date))
    return rows


def transfer_twins(txn):
    return find_transfer_twin(txn, days=3, include_flagged=True)


def suggestions(txn):
    payments, groups = payment_matches(txn)
    return {
        "payments": payments,
        "payment_groups": groups,
        "receipts": receipt_matches(txn),
        "expenses": expense_matches(txn),
        "transfer_twins": transfer_twins(txn),
    }


def has_any(sugg):
    return any(sugg[k] for k in ("payments", "payment_groups", "receipts", "expenses", "transfer_twins"))
