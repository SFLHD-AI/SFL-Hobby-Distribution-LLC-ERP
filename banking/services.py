"""
Small read-only helpers the dashboard and the banking pages share.
"""
from decimal import Decimal

from django.db.models import Count

from banking.models import BankAccount, BankConnection, BankTransaction


def cash_position(accounts=None):
    """Balances of every active account, split into money you have and
    money you owe on cards/loans.

    Returns {"accounts": [(BankAccount, balance)], "cash": Decimal,
             "cards": Decimal, "net": Decimal}. Card/loan balances are
    positive = amount owed; ``net`` = cash − cards."""
    if accounts is None:
        accounts = BankAccount.objects.filter(active=True).select_related("connection")
    rows, cash, cards = [], Decimal("0.00"), Decimal("0.00")
    for acct in accounts:
        bal = acct.current_balance
        rows.append((acct, bal))
        if acct.is_liability:
            cards += bal
        else:
            cash += bal
    return {"accounts": rows, "cash": cash, "cards": cards, "net": cash - cards}


def needs_attention():
    """Counts for the dashboard badge: rows waiting for review, how many of
    those already carry a suggestion, and connections that need a human."""
    unreviewed = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
    return {
        "unreviewed": unreviewed.count(),
        "suggested": unreviewed.filter(suggested_account__isnull=False).count(),
        "connections_error": BankConnection.objects.filter(status__in=["error", "relink"]).count(),
    }


def inbox_counts():
    """Status counts for the inbox header."""
    counts = {s: 0 for s, _ in BankTransaction.STATUS_CHOICES}
    for row in (BankTransaction.objects.filter(is_deleted=False)
                .values("status").order_by().annotate(n=Count("id"))):
        counts[row["status"]] = row["n"]
    counts["suggested"] = BankTransaction.objects.filter(
        is_deleted=False, status=BankTransaction.UNREVIEWED, suggested_account__isnull=False).count()
    return counts
