"""
Background sync of connected bank feeds (scheduled by ``setup_schedules``
as ``banking.tasks.sync_all_connections``; also run on demand from the
"Sync now" button).

``sync_connection(conn)`` pulls added / modified / removed transactions
through the provider's cursor API and reconciles them:

* **added** — becomes a new BankTransaction (source plaid). If the row says
  it replaces a pending row (``pending_external_id``) that we already hold,
  that row is updated in place and takes the new external id, so any review
  work done on the pending row survives. If a CSV-imported row with the same
  fingerprint and no external id exists, it is adopted instead of duplicated.
* **modified** — amount / dates / description / pending flag refreshed; a
  bank-created Expense is kept in step.
* **removed** — the row is unlinked (``reset_review``) and soft-deleted.
* **rules** then run over the new/updated unreviewed rows.

First sync of a brand-new account
---------------------------------
A Plaid account starts with ``opening_date = today`` and ``opening_balance =
0``. On its first sync we derive both from the bank -- but only while the
account is still at those defaults (no transactions, opening balance 0,
opening date not earlier than the day the account was created). An account
the owner configured by hand (an opening balance from a statement, a
back-dated opening date) is never touched. For such accounts we:

1. set ``opening_date`` to the earliest transaction date Plaid delivered, so
   nothing in the history is skipped, and
2. set ``opening_balance`` so that ``balance_as_of(today)`` equals the
   provider's current balance:

   * asset accounts:     opening = current − Σ(posted amounts)
   * cards / loans:      opening = current + Σ(posted amounts)
     (charges are negative amounts that *increase* what is owed, and the
     provider reports a card's current balance as a positive amount owed)

   Pending rows are excluded on both sides (``balance_as_of`` ignores them
   and Plaid's ``current`` balance does not include pending activity).

Errors set ``status``: ``ITEM_LOGIN_REQUIRED`` (and similar) -> ``relink``
so the UI asks the owner to re-authenticate; anything else -> ``error`` with
the message in ``last_error``.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from banking.models import BankAccount, BankConnection, BankTransaction, ImportBatch
from banking.providers import ProviderError, get_provider

log = logging.getLogger(__name__)


def _summary():
    return {"added": 0, "modified": 0, "removed": 0, "auto": 0, "skipped": 0,
            "accounts_initialized": [], "error": "", "status": ""}


def _apply_fields(txn, rec):
    """Copy provider fields onto a BankTransaction. Returns True if changed."""
    changed = False
    fields = {
        "amount": rec["amount"], "date": rec["date"], "posted_date": rec.get("posted_date"),
        "description": rec.get("description") or txn.description,
        "merchant_name": rec.get("merchant_name") or "",
        "pending": bool(rec.get("pending")),
        "check_number": rec.get("check_number") or "",
    }
    for k, v in fields.items():
        if getattr(txn, k) != v:
            setattr(txn, k, v)
            changed = True
    if rec.get("original_description") and not txn.original_description:
        txn.original_description = rec["original_description"]
        changed = True
    if changed:
        txn.fingerprint = BankTransaction.make_fingerprint(txn.account_id, txn.date, txn.amount,
                                                           txn.description)
    return changed


def _sync_expense(txn):
    """Keep a bank-created Expense row in step with its transaction."""
    exp = txn.expense if txn.expense_id else None
    if exp is None or exp.source != "bank":
        return
    exp.amount = -txn.amount
    exp.date = txn.date
    exp.paid_date = txn.date
    exp.save(update_fields=["amount", "date", "paid_date", "updated_at"])


def at_defaults(account):
    """True when the account still looks freshly created: nothing imported,
    opening balance 0 and an opening date no earlier than the day it was
    added. Only such accounts get their opening figures from the bank."""
    if account.opening_balance != 0:
        return False
    created = timezone.localtime(account.created_at).date() if account.created_at else timezone.localdate()
    if account.opening_date < created:
        return False
    return not account.transactions.exists()


def _initialize_account(account, balance_info, created_rows):
    """First sync: derive opening_date / opening_balance (see module doc)."""
    if not created_rows:
        return False
    earliest = min(t.date for t in created_rows)
    account.opening_date = earliest
    current = balance_info.get("current_balance") if balance_info else None
    if current is not None:
        today = timezone.localdate()
        posted = (BankTransaction.objects
                  .filter(account=account, is_deleted=False, pending=False, date__lte=today,
                          status__in=BankTransaction.COUNTED_STATUSES)
                  .aggregate(t=Sum("amount"))["t"] or Decimal("0.00"))
        if account.is_liability:
            account.opening_balance = (Decimal(current) + posted).quantize(Decimal("0.01"))
        else:
            account.opening_balance = (Decimal(current) - posted).quantize(Decimal("0.01"))
    account.save(update_fields=["opening_date", "opening_balance"])
    return True


def sync_connection(conn, provider=None, user=None):
    """Sync one connection. Returns a summary dict (never raises for
    provider problems; they are written to the connection)."""
    from banking.rules import apply_rules

    summary = _summary()
    if provider is None:
        try:
            provider = get_provider(conn.provider)
        except ProviderError as exc:
            conn.status, conn.last_error = "error", str(exc)[:500]
            conn.save(update_fields=["status", "last_error"])
            summary.update(error=str(exc), status=conn.status)
            return summary
    if not provider.is_configured():
        conn.status, conn.last_error = "error", "Bank feed provider is not configured."
        conn.save(update_fields=["status", "last_error"])
        summary.update(error=conn.last_error, status=conn.status)
        return summary

    try:
        result = provider.sync_transactions(conn.access_token, conn.sync_cursor or "")
        balances = {}
        accounts_needing_init = [a for a in conn.accounts.filter(is_deleted=False) if at_defaults(a)]
        if accounts_needing_init:
            try:
                balances = {a["external_account_id"]: a for a in provider.get_accounts(conn.access_token)}
            except ProviderError as exc:          # balances are a nicety; keep syncing
                log.warning("balance lookup failed for %s: %s", conn, exc)
    except ProviderError as exc:
        conn.status = "relink" if exc.needs_relink else "error"
        conn.last_error = f"{exc.code}: {exc}"[:500] if exc.code else str(exc)[:500]
        conn.save(update_fields=["status", "last_error"])
        summary.update(error=conn.last_error, status=conn.status)
        return summary

    accounts = {a.external_account_id: a for a in conn.accounts.filter(is_deleted=False)
                if a.external_account_id}
    first_sync_ids = {a.pk for a in accounts_needing_init}
    batches, created, touched = {}, {}, []

    def batch_for(account):
        if account.pk not in batches:
            batches[account.pk] = ImportBatch.objects.create(
                account=account, source="plaid", filename=f"Plaid sync {timezone.localtime():%Y-%m-%d %H:%M}",
                preset="plaid", created_by=user if getattr(user, "is_authenticated", False) else None)
        return batches[account.pk]

    with transaction.atomic():
        # --- added -----------------------------------------------------------
        for rec in result.get("added", []):
            account = accounts.get(rec.get("external_account_id"))
            if account is None or not rec.get("date") or not rec.get("external_id"):
                summary["skipped"] += 1
                continue
            existing = BankTransaction.all_objects.filter(account=account, external_id=rec["external_id"]).first()
            if existing is None and rec.get("pending_external_id"):
                existing = BankTransaction.all_objects.filter(
                    account=account, external_id=rec["pending_external_id"]).first()
                if existing is not None:
                    existing.external_id = rec["external_id"]
            if existing is None:
                fp = BankTransaction.make_fingerprint(account.pk, rec["date"], rec["amount"], rec["description"])
                existing = BankTransaction.objects.filter(account=account, fingerprint=fp,
                                                          external_id="").first()
                if existing is not None:
                    existing.external_id = rec["external_id"]
            if existing is not None:
                if existing.is_deleted:
                    summary["skipped"] += 1
                    continue
                _apply_fields(existing, rec)
                existing.source = "plaid"
                existing.save()
                _sync_expense(existing)
                summary["modified"] += 1
                if existing.status == BankTransaction.UNREVIEWED:
                    touched.append(existing)
                continue
            if account.pk not in first_sync_ids and rec["date"] < account.opening_date:
                summary["skipped"] += 1
                continue
            txn = BankTransaction(
                account=account, date=rec["date"], posted_date=rec.get("posted_date"),
                amount=rec["amount"], description=rec.get("description") or "(no description)",
                original_description=rec.get("original_description") or "",
                merchant_name=rec.get("merchant_name") or "", check_number=rec.get("check_number") or "",
                external_id=rec["external_id"], pending=bool(rec.get("pending")),
                source="plaid", import_batch=batch_for(account))
            txn.save()
            created.setdefault(account.pk, []).append(txn)
            touched.append(txn)
            summary["added"] += 1

        # --- modified ----------------------------------------------------------
        for rec in result.get("modified", []):
            account = accounts.get(rec.get("external_account_id"))
            if account is None or not rec.get("external_id"):
                summary["skipped"] += 1
                continue
            txn = BankTransaction.objects.filter(account=account, external_id=rec["external_id"]).first()
            if txn is None:
                summary["skipped"] += 1
                continue
            if _apply_fields(txn, rec):
                txn.save()
                _sync_expense(txn)
            summary["modified"] += 1
            if txn.status == BankTransaction.UNREVIEWED:
                touched.append(txn)

        # --- removed -----------------------------------------------------------
        for ext_id in result.get("removed", []):
            txn = BankTransaction.objects.filter(account__connection=conn, external_id=ext_id).first()
            if txn is None:
                continue
            txn.reset_review(user=user)
            txn.delete(user=user)
            summary["removed"] += 1

        # --- first-sync opening balances -------------------------------------------
        for acct_pk in first_sync_ids:
            account = BankAccount.objects.get(pk=acct_pk)
            if _initialize_account(account, balances.get(account.external_account_id), created.get(acct_pk, [])):
                summary["accounts_initialized"].append(account.name)

        for acct_pk, rows in created.items():
            b = batches[acct_pk]
            b.rows_total = b.rows_imported = len(rows)
            b.date_min = min(t.date for t in rows)
            b.date_max = max(t.date for t in rows)
            b.save(update_fields=["rows_total", "rows_imported", "date_min", "date_max"])

        conn.sync_cursor = result.get("next_cursor") or conn.sync_cursor
        conn.last_synced_at = timezone.now()
        conn.status = "active"
        conn.last_error = ""
        conn.save(update_fields=["sync_cursor", "last_synced_at", "status", "last_error"])

    if touched:
        seen, unique = set(), []
        for t in touched:
            if t.pk not in seen:
                seen.add(t.pk)
                unique.append(t)
        counts = apply_rules(unique, user=user)
        summary["auto"] = counts["auto"] + counts["ignored"] + counts["transfers"]
        for acct_pk, b in batches.items():
            b.rows_auto_categorized = sum(
                1 for t in created.get(acct_pk, [])
                if t.status in (BankTransaction.CATEGORIZED, BankTransaction.IGNORED, BankTransaction.TRANSFER))
            b.save(update_fields=["rows_auto_categorized"])
    summary["status"] = conn.status
    return summary


def sync_all_connections(provider=None, user=None):
    """Sync every live, non-manual connection. Returns a one-line summary."""
    from core.models import CompanySettings
    if not CompanySettings.get().bank_sync_enabled:
        return "Bank sync is turned off in Settings."
    conns = list(BankConnection.objects.filter(status__in=["active", "error"]).exclude(provider="manual"))
    if not conns:
        return "No connected banks to sync."
    totals = _summary()
    relink, errors = [], []
    for conn in conns:
        s = sync_connection(conn, provider=provider, user=user)
        for k in ("added", "modified", "removed", "auto"):
            totals[k] += s[k]
        if s["status"] == "relink":
            relink.append(conn.institution_name)
        elif s["status"] == "error":
            errors.append(f"{conn.institution_name}: {s['error']}")
    ok = len(conns) - len(relink) - len(errors)
    parts = [f"Synced {ok} of {len(conns)} connection{'s' if len(conns) != 1 else ''}: "
             f"{totals['added']} new, {totals['modified']} updated, {totals['removed']} removed, "
             f"{totals['auto']} auto-categorized."]
    if relink:
        parts.append("Needs re-authentication: " + ", ".join(relink) + ".")
    if errors:
        parts.append("Errors: " + "; ".join(errors) + ".")
    return " ".join(parts)
