"""
Banking views: review inbox, transaction detail + matching, accounts,
CSV import, rules, Plaid connect, manual entry.
"""
import json
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounting.models import Account, AccountType
from banking import importers, matching
from banking.forms import (BankAccountForm, CategoryRuleForm, GenericMappingForm, ImportUploadForm,
                           ManualTransactionForm, ReceiptUploadForm, ReconcileForm, category_groups)
from banking.models import BankAccount, BankConnection, BankTransaction, CategoryRule, ImportBatch
from banking.providers import ProviderError, get_provider
from banking.rules import apply_rules, apply_single_rule, rule_from_transaction, test_rule
from banking.services import cash_position, inbox_counts
from banking.tasks import sync_all_connections, sync_connection
from core.models import ActivityLog, CompanySettings, log_activity
from core.roles import module_required
from expenses.models import Expense, ExpenseCategory, Vendor

PAGE_SIZE = 50
SESSION_PREFIX = "bank_import_"
SESSION_TTL = 2 * 60 * 60


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


def _safe_reverse(name, *args):
    """Reverse a URL that another module owns; None when it is not wired yet."""
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_decimal(value):
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _resolve_vendor(name, category_account=None):
    """Find a vendor by name (case-insensitive) or create one. When the vendor
    is new and the category is an expense category, remember it as the
    vendor's default so the feed recognises it next time."""
    name = (name or "").strip()
    if not name:
        return None
    vendor = Vendor.objects.filter(name__iexact=name).first()
    created = False
    if vendor is None:
        vendor = Vendor.objects.create(name=name[:200])
        created = True
    if category_account is not None and category_account.type == AccountType.EXPENSE and \
            (created or vendor.default_category_id is None):
        cat = ExpenseCategory.for_account(category_account)
        if cat is not None:
            vendor.default_category = cat
            vendor.save(update_fields=["default_category"])
    return vendor


def _category_from_post(request, field="category"):
    pk = request.POST.get(field)
    if not pk:
        return None
    return Account.objects.filter(pk=pk, active=True).first()


def _inbox_context_extras():
    return {
        "category_groups": category_groups(),
        "vendor_names": list(Vendor.objects.filter(active=True).values_list("name", flat=True)[:500]),
        "accounts": BankAccount.objects.filter(active=True),
        "counts": inbox_counts(),
        "status_choices": BankTransaction.STATUS_CHOICES,
    }


def _owner_account(txn):
    return Account.system("owner_draw") if txn.amount < 0 else Account.system("owner_contribution")


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------
@module_required("banking")
def inbox(request):
    g = request.GET
    status = g.get("status", BankTransaction.UNREVIEWED)
    qs = (BankTransaction.objects.filter(is_deleted=False)
          .select_related("account", "suggested_account", "category_account", "vendor", "expense",
                          "transfer_peer", "transfer_peer__account", "import_batch"))
    if status and status != "all":
        qs = qs.filter(status=status)
    if status == "suggested":
        qs = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED,
                                            suggested_account__isnull=False).select_related(
            "account", "suggested_account", "vendor")
    if g.get("account"):
        qs = qs.filter(account_id=g["account"])
    d_from, d_to = _parse_date(g.get("from")), _parse_date(g.get("to"))
    if d_from:
        qs = qs.filter(date__gte=d_from)
    if d_to:
        qs = qs.filter(date__lte=d_to)
    q = (g.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(merchant_name__icontains=q)
                       | Q(original_description__icontains=q) | Q(notes__icontains=q)
                       | Q(check_number__icontains=q))
    amin, amax = _parse_decimal(g.get("min")), _parse_decimal(g.get("max"))
    if amin is not None:
        qs = qs.filter(Q(amount__gte=amin) | Q(amount__lte=-amin))
    if amax is not None:
        qs = qs.filter(amount__lte=amax, amount__gte=-amax)
    batch = None
    if g.get("batch"):
        batch = ImportBatch.objects.filter(pk=g["batch"]).first()
        if batch:
            qs = qs.filter(import_batch=batch)
    qs = qs.order_by("-date", "-id")
    paginator = Paginator(qs, PAGE_SIZE)
    page = paginator.get_page(g.get("page"))
    params = g.copy()
    params.pop("page", None)
    ctx = {
        "page": page, "transactions": page.object_list, "status": status, "batch": batch,
        "querystring": params.urlencode(), "filters": g,
        "recent_batches": ImportBatch.objects.select_related("account").order_by("-created_at")[:15],
        "connections": BankConnection.objects.exclude(provider="manual").exclude(status="disconnected"),
        "next_url": request.get_full_path(),
    }
    ctx.update(_inbox_context_extras())
    return render(request, "banking/inbox.html", ctx)


@module_required("banking")
def inbox_action(request, pk):
    """Inline review actions from the inbox rows and the detail page."""
    txn = get_object_or_404(BankTransaction, pk=pk, is_deleted=False)
    nxt = _safe_next(request, reverse("bank_inbox"))
    if request.method != "POST":
        return redirect("bank_transaction_detail", txn.pk)
    action = request.POST.get("action", "save")
    note = (request.POST.get("note") or "").strip()[:500]

    if action == "save":
        account = _category_from_post(request)
        if account is None:
            messages.error(request, "Pick a category first.")
            return redirect(nxt)
        vendor = _resolve_vendor(request.POST.get("vendor"), account)
        txn.categorize(account, user=request.user, vendor=vendor, note=note or None)
        msg = f"Saved as {account.name}."
        if request.POST.get("always"):
            rule = rule_from_transaction(txn, account, vendor=vendor,
                                         scope=request.POST.get("rule_scope", "all"), user=request.user)
            others = apply_single_rule(rule, user=request.user)
            msg += f" Rule '{rule.name}' saved" + (f" and applied to {others} more." if others else ".")
        messages.success(request, msg)
    elif action == "personal":
        account = _owner_account(txn)
        txn.categorize(account, user=request.user, note=note or None)
        messages.success(request, f"Marked as {account.name.lower()}.")
    elif action == "ignore":
        txn.mark_ignored(user=request.user, note=note)
        messages.success(request, "Ignored. It will not count anywhere.")
    elif action == "transfer":
        twins = matching.transfer_twins(txn)
        if len(twins) == 1:
            txn.mark_transfer(twins[0], user=request.user)
            messages.success(request, f"Paired with {twins[0].account.name} on {twins[0].date:%b %-d}.")
        elif not twins:
            messages.warning(request, "No matching transaction found in another account within 3 days. "
                                      "Import or add the other side first, then try again.")
            return redirect("bank_transaction_detail", txn.pk)
        else:
            messages.info(request, "More than one possible match. Pick the right one below.")
            return redirect("bank_transaction_detail", txn.pk)
    elif action == "match":
        return redirect("bank_transaction_detail", txn.pk)
    elif action == "reset":
        txn.reset_review(user=request.user)
        log_activity(request.user, "bank_txn_reset", txn)
        messages.success(request, "Back in the review inbox.")
    else:
        messages.error(request, "Unknown action.")
    return redirect(nxt)


@module_required("banking")
def inbox_bulk(request):
    nxt = _safe_next(request, reverse("bank_inbox"))
    if request.method != "POST":
        return redirect(nxt)
    ids = [i for i in request.POST.getlist("ids") if i.isdigit()]
    action = request.POST.get("bulk_action", "")
    txns = list(BankTransaction.objects.filter(pk__in=ids, is_deleted=False).select_related("account"))
    if action == "run_rules":
        target = txns or list(BankTransaction.objects.filter(
            is_deleted=False, status=BankTransaction.UNREVIEWED).select_related("account")[:5000])
        c = apply_rules(target, user=request.user)
        messages.success(request, _rules_message(c))
        return redirect(nxt)
    if not txns:
        messages.warning(request, "Select at least one transaction first.")
        return redirect(nxt)
    n = 0
    if action == "categorize":
        account = _category_from_post(request, "bulk_category")
        if account is None:
            messages.error(request, "Pick a category for the selected rows.")
            return redirect(nxt)
        for t in txns:
            t.categorize(account, user=request.user)
            n += 1
        messages.success(request, f"{n} transaction{'s' if n != 1 else ''} saved as {account.name}.")
    elif action == "ignore":
        for t in txns:
            t.mark_ignored(user=request.user)
            n += 1
        messages.success(request, f"{n} transaction{'s' if n != 1 else ''} ignored.")
    elif action == "personal":
        for t in txns:
            t.categorize(_owner_account(t), user=request.user)
            n += 1
        messages.success(request, f"{n} transaction{'s' if n != 1 else ''} marked personal.")
    elif action == "reset":
        for t in txns:
            t.reset_review(user=request.user)
            n += 1
        messages.success(request, f"{n} transaction{'s' if n != 1 else ''} sent back to review.")
    else:
        messages.error(request, "Pick a bulk action.")
    return redirect(nxt)


def _rules_message(c):
    return (f"Rules checked {c['scanned']} transaction{'s' if c['scanned'] != 1 else ''}: "
            f"{c['auto']} categorized, {c['ignored']} ignored, {c['transfers']} paired as transfers, "
            f"{c['suggested'] + c['vendor'] + c['learned']} suggestions, {c['unmatched']} still open.")


@module_required("banking")
def rules_run(request):
    nxt = _safe_next(request, reverse("bank_inbox"))
    if request.method != "POST":
        return redirect(nxt)
    qs = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
    if request.POST.get("account"):
        qs = qs.filter(account_id=request.POST["account"])
    c = apply_rules(list(qs.select_related("account")[:5000]), user=request.user)
    messages.success(request, _rules_message(c))
    return redirect(nxt)


@module_required("banking")
def sync_now(request):
    nxt = _safe_next(request, reverse("bank_inbox"))
    result = sync_all_connections(user=request.user)
    level = messages.warning if ("Needs re-authentication" in result or "Errors" in result
                                 or "turned off" in result) else messages.success
    level(request, result)
    log_activity(request.user, "bank_sync", note=result[:500], model="BankConnection")
    return redirect(nxt)


# ---------------------------------------------------------------------------
# Transaction detail / matching
# ---------------------------------------------------------------------------
@module_required("banking")
def transaction_detail(request, pk):
    txn = get_object_or_404(
        BankTransaction.objects.select_related("account", "category_account", "suggested_account",
                                               "vendor", "expense", "expense__category", "transfer_peer",
                                               "transfer_peer__account", "rule_applied", "import_batch",
                                               "reviewed_by"),
        pk=pk, is_deleted=False)
    if request.method == "POST":
        return _transaction_post(request, txn)
    sugg = matching.suggestions(txn) if txn.status in (BankTransaction.UNREVIEWED, BankTransaction.CATEGORIZED) \
        else {"payments": [], "payment_groups": [], "receipts": [], "expenses": [], "transfer_twins": []}
    history = ActivityLog.objects.filter(model="BankTransaction", object_id=str(txn.pk)).order_by("-timestamp")[:30]
    ctx = {
        "txn": txn, "sugg": sugg, "has_suggestions": matching.has_any(sugg), "history": history,
        "receipt_form": ReceiptUploadForm(),
        "linked_payments": txn.payments.select_related("customer") if txn.status == BankTransaction.MATCHED else [],
        "linked_receipts": txn.receipts.select_related("supplier") if txn.status == BankTransaction.MATCHED else [],
        "expense_url": _safe_reverse("expense_edit", txn.expense_id) if txn.expense_id else None,
        "next_url": request.get_full_path(),
    }
    ctx.update(_inbox_context_extras())
    return render(request, "banking/transaction_detail.html", ctx)


def _transaction_post(request, txn):
    from inventory.models import Receipt
    from payments.models import Payment
    action = request.POST.get("action", "")
    try:
        if action == "link_payments":
            ids = [i for i in request.POST.getlist("payment_ids") if i.isdigit()]
            pays = list(Payment.objects.filter(pk__in=ids, is_deleted=False))
            if not pays:
                raise ValueError("Pick at least one payment.")
            total = sum((p.amount for p in pays), Decimal("0"))
            if total != abs(txn.amount):
                raise ValueError(f"The payments add up to ${total:,.2f} but the deposit is "
                                 f"${abs(txn.amount):,.2f}. Pick payments that add up exactly.")
            txn.link_payments(pays, user=request.user)
            messages.success(request, f"Deposit matched to {len(pays)} payment{'s' if len(pays) != 1 else ''}.")
        elif action == "link_receipts":
            ids = [i for i in request.POST.getlist("receipt_ids") if i.isdigit()]
            recs = list(Receipt.objects.filter(pk__in=ids, is_deleted=False))
            if not recs:
                raise ValueError("Pick at least one supplier receipt.")
            txn.link_receipts(recs, user=request.user)
            messages.success(request, f"Matched to {len(recs)} supplier receipt{'s' if len(recs) != 1 else ''}.")
        elif action == "link_expense":
            exp = Expense.objects.filter(pk=request.POST.get("expense_id"), is_deleted=False).first()
            if exp is None:
                raise ValueError("That expense no longer exists.")
            if getattr(exp, "bank_txn", None) is not None and exp.bank_txn.pk != txn.pk:
                raise ValueError("That expense is already linked to another bank transaction.")
            txn.link_expense(exp, user=request.user)
            messages.success(request, f"Matched to expense: {exp.vendor} ${exp.amount}.")
        elif action == "transfer_with":
            peer = BankTransaction.objects.filter(pk=request.POST.get("peer_id"), is_deleted=False).first()
            if peer is None:
                raise ValueError("That transaction no longer exists.")
            txn.mark_transfer(peer, user=request.user)
            messages.success(request, f"Paired as a transfer with {peer.account.name}.")
        elif action == "upload_receipt":
            form = ReceiptUploadForm(request.POST, request.FILES)
            if not form.is_valid():
                raise ValueError("; ".join(e for errs in form.errors.values() for e in errs))
            txn.receipt_file = form.cleaned_data["receipt_file"]
            txn.save(update_fields=["receipt_file"])
            if txn.expense_id and not txn.expense.receipt_file:
                txn.expense.receipt_file = txn.receipt_file
                txn.expense.save(update_fields=["receipt_file", "updated_at"])
            log_activity(request.user, "bank_txn_receipt", txn)
            messages.success(request, "Receipt attached.")
        elif action == "note":
            txn.notes = (request.POST.get("note") or "").strip()[:500]
            txn.save(update_fields=["notes"])
            messages.success(request, "Note saved.")
        elif action in ("save", "personal", "ignore", "transfer", "reset"):
            request.POST = request.POST.copy()
            request.POST.setdefault("next", reverse("bank_transaction_detail", args=[txn.pk]))
            return inbox_action(request, txn.pk)
        else:
            raise ValueError("Unknown action.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("bank_transaction_detail", txn.pk)


@module_required("banking")
def transaction_add(request):
    if request.method == "POST":
        form = ManualTransactionForm(request.POST)
        if form.is_valid():
            txn = form.save(user=request.user)
            log_activity(request.user, "bank_txn_added", txn)
            c = apply_rules([txn], user=request.user)
            txn.refresh_from_db()
            if txn.status != BankTransaction.UNREVIEWED:
                messages.success(request, f"Added and {txn.get_status_display().lower()} by a rule.")
            else:
                messages.success(request, "Added. Categorize it below." if not c["suggested"] + c["vendor"] + c["learned"]
                                 else "Added with a suggested category — confirm it below.")
            return redirect("bank_transaction_detail", txn.pk)
        messages.error(request, "Please fix the highlighted fields.")
    else:
        initial = {}
        if request.GET.get("account"):
            initial["account"] = request.GET["account"]
        form = ManualTransactionForm(initial=initial)
    return render(request, "banking/transaction_form.html", {"form": form})


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
@module_required("banking")
def accounts(request):
    show_inactive = request.GET.get("all") == "1"
    qs = BankAccount.objects.select_related("connection")
    if not show_inactive:
        qs = qs.filter(active=True)
    position = cash_position(qs)
    rows = []
    for acct, bal in position["accounts"]:
        rows.append({"account": acct, "balance": bal, "unreviewed": acct.unreviewed_count,
                     "last_txn": acct.last_transaction_date})
    return render(request, "banking/accounts.html", {
        "rows": rows, "position": position, "show_inactive": show_inactive,
        "connections": BankConnection.objects.exclude(status="disconnected"),
    })


@module_required("banking")
def account_create(request):
    form = BankAccountForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            acct = form.save()
            log_activity(request.user, "create", acct)
            messages.success(request, f"{acct.name} added. Import a statement or connect your bank to pull transactions.")
            return redirect("bank_account_detail", acct.pk)
        messages.error(request, "Please fix the highlighted fields.")
    return render(request, "banking/account_form.html", {"form": form, "account": None})


@module_required("banking")
def account_edit(request, pk):
    acct = get_object_or_404(BankAccount, pk=pk)
    form = BankAccountForm(request.POST or None, instance=acct)
    if request.method == "POST":
        if form.is_valid():
            acct = form.save()
            log_activity(request.user, "update", acct, changes={k: str(v) for k, v in form.cleaned_data.items()
                                                                 if k in form.changed_data})
            messages.success(request, "Account saved.")
            return redirect("bank_account_detail", acct.pk)
        messages.error(request, "Please fix the highlighted fields.")
    return render(request, "banking/account_form.html", {"form": form, "account": acct})


@module_required("banking")
def account_detail(request, pk):
    acct = get_object_or_404(BankAccount.objects.select_related("connection"), pk=pk)
    g = request.GET
    d_from, d_to = _parse_date(g.get("from")), _parse_date(g.get("to"))
    q = (g.get("q") or "").strip()
    status = g.get("status", "")
    base = acct.transactions.filter(is_deleted=False).select_related(
        "category_account", "vendor", "transfer_peer", "transfer_peer__account")
    qs = base
    if d_from:
        qs = qs.filter(date__gte=d_from)
    if d_to:
        qs = qs.filter(date__lte=d_to)
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(merchant_name__icontains=q)
                       | Q(check_number__icontains=q) | Q(notes__icontains=q))
    if status:
        qs = qs.filter(status=status)
    running_ok = not q and not status
    rows = list(qs.order_by("date", "id")[:1000])
    # running balance (natural sign) — only meaningful when nothing but dates filters the register
    running = None
    if running_ok:
        start = acct.balance_as_of(d_from - timedelta(days=1)) if d_from else acct.opening_balance
        running = start
    register = []
    for t in rows:
        if running is not None and t.pending is False and t.status in BankTransaction.COUNTED_STATUSES:
            running = running - t.amount if acct.is_liability else running + t.amount
        register.append({"t": t, "running": running if running_ok else None})
    register.reverse()
    if g.get("export") == "csv":
        from core.csvutils import csv_response
        return csv_response(
            f"{acct.name.replace(' ', '_')}_register.csv",
            ["date", "posted", "description", "merchant", "check", "amount", "status", "category", "vendor",
             "running_balance", "notes"],
            [[r["t"].date, r["t"].posted_date or "", r["t"].description, r["t"].merchant_name, r["t"].check_number,
              r["t"].amount, r["t"].get_status_display(),
              r["t"].category_account.name if r["t"].category_account_id else "",
              r["t"].vendor.name if r["t"].vendor_id else "",
              r["running"] if r["running"] is not None else "", r["t"].notes] for r in register])

    # reconcile box
    recon = None
    recon_form = ReconcileForm(g if g.get("statement_date") else None)
    if g.get("statement_date") and recon_form.is_valid():
        sd = recon_form.cleaned_data["statement_date"]
        eb = recon_form.cleaned_data["ending_balance"]
        system = acct.balance_as_of(sd)
        recon = {"statement_date": sd, "ending_balance": eb, "system": system, "difference": eb - system,
                 "after": list(base.filter(date__gt=sd).order_by("date", "id")[:50]),
                 "pending": list(base.filter(date__lte=sd, pending=True)[:50]),
                 "unreviewed_through": base.filter(date__lte=sd, status=BankTransaction.UNREVIEWED).count()}
    elif g.get("statement_date"):
        messages.error(request, "Enter a statement date and ending balance to reconcile.")
    return render(request, "banking/account_detail.html", {
        "account": acct, "register": register, "filters": g, "running_ok": running_ok,
        "truncated": len(rows) >= 1000, "balance": acct.current_balance,
        "unreviewed": acct.unreviewed_count, "recon": recon, "recon_form": recon_form,
        "status_choices": BankTransaction.STATUS_CHOICES,
        "batches": acct.imports.order_by("-created_at")[:10],
        "querystring": g.urlencode(),
    })


# ---------------------------------------------------------------------------
# CSV import (upload -> preview -> confirm -> result)
# ---------------------------------------------------------------------------
def _session_cleanup(session):
    now = time.time()
    for key in [k for k in session.keys() if k.startswith(SESSION_PREFIX)]:
        if now - session[key].get("created", 0) > SESSION_TTL:
            pending_path = session[key].get("stored_path")
            if pending_path and default_storage.exists(pending_path):
                default_storage.delete(pending_path)
            del session[key]


def _preview_context(request, token, state, mapped, flip=False):
    account = get_object_or_404(BankAccount, pk=state["account_id"])
    if flip:
        importers.flip_rows(mapped)
    rows, counts = importers.classify_rows(account, mapped)
    preset = state.get("preset") or "generic"
    column_labels = [(i, l) for i, l in state["column_labels"]]
    mapping_form = None
    if preset == "generic":
        init = {f"col_{k}": str(v) for k, v in state["mapping"]["columns"].items()} if state.get("mapping") else {}
        if state.get("mapping"):
            init["positive_is_charge"] = state["mapping"].get("negate", False)
        mapping_form = GenericMappingForm(column_labels, initial=init)
    new_total = sum((r["amount"] for r in rows if r["status"] == importers.NEW), Decimal("0"))
    return {
        "token": token, "account": account, "rows": rows[:25], "all_rows": len(rows), "counts": counts,
        "preset": preset, "preset_label": importers.PRESETS.get(preset, {}).get("label", preset),
        "filename": state["filename"], "warnings": state.get("warnings", []),
        "sign_warning": importers.sign_warning(account, rows) if not flip else "",
        "flip": flip, "mapping_form": mapping_form, "headers": state.get("headers"),
        "new_total": new_total, "mapping_ok": True,
        "presets": importers.PRESET_CHOICES,
    }


@module_required("banking")
def bank_import(request):
    _session_cleanup(request.session)
    if request.method != "POST":
        form = ImportUploadForm(initial={"account": request.GET.get("account")})
        return render(request, "banking/import.html", {"form": form, "step": "upload",
                                                       "accounts": BankAccount.objects.filter(active=True)})
    step = request.POST.get("step", "upload")
    if step == "upload":
        form = ImportUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Please fix the highlighted fields.")
            return render(request, "banking/import.html", {"form": form, "step": "upload",
                                                           "accounts": BankAccount.objects.filter(active=True)})
        account = form.cleaned_data["account"]
        upload = form.cleaned_data["file"]
        data = upload.read()
        parsed = importers.parse_csv(data)
        if not parsed.rows:
            messages.error(request, "No transactions found in that file. " + " ".join(parsed.warnings))
            return render(request, "banking/import.html", {"form": form, "step": "upload",
                                                           "accounts": BankAccount.objects.filter(active=True)})
        preset = form.cleaned_data["preset"]
        if preset == "auto":
            preset = importers.detect_preset(parsed.headers, parsed.rows[0]) or "generic"
        mapping = None
        if preset != "generic":
            mapping = importers.resolve_mapping(preset, parsed.headers, parsed.rows[0])
            if mapping is None:
                messages.warning(request, f"The {importers.PRESETS[preset]['label']} layout did not fit this file. "
                                          "Pick the columns below.")
                preset = "generic"
        token = secrets.token_urlsafe(8)
        stored_path = default_storage.save(
            f"bank_imports/pending/{token}_{upload.name[-60:]}", ContentFile(data))
        state = {
            "account_id": account.pk, "filename": upload.name[:200], "stored_path": stored_path,
            "preset": preset, "headers": parsed.headers, "rows": parsed.rows,
            "column_labels": parsed.column_labels(), "warnings": parsed.warnings,
            "mapping": mapping, "created": time.time(),
        }
        request.session[SESSION_PREFIX + token] = state
        request.session.modified = True
        if mapping is None:
            ctx = _generic_picker_context(token, state, GenericMappingForm(state["column_labels"]))
            return render(request, "banking/import.html", ctx)
        mapped = importers.map_rows(parsed.rows, mapping)
        return render(request, "banking/import.html", _preview_context(request, token, state, mapped))

    token = request.POST.get("token", "")
    state = request.session.get(SESSION_PREFIX + token)
    if not state:
        messages.error(request, "That upload has expired. Please upload the file again.")
        return redirect("bank_import")

    if step == "remap":
        mform = GenericMappingForm(state["column_labels"], request.POST)
        if not mform.is_valid():
            messages.error(request, "Pick the columns that hold the date, description and amount.")
            return render(request, "banking/import.html", _generic_picker_context(token, state, mform))
        state["mapping"] = importers.mapping_from_form(mform.cleaned_data)
        state["preset"] = "generic"
        request.session[SESSION_PREFIX + token] = state
        request.session.modified = True
        try:
            mapped = importers.map_rows(state["rows"], state["mapping"])
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, "banking/import.html", _generic_picker_context(token, state, mform))
        return render(request, "banking/import.html", _preview_context(request, token, state, mapped))

    if step == "reparse":
        preset = request.POST.get("preset", state["preset"])
        if preset == "generic" or preset not in importers.PRESETS:
            return render(request, "banking/import.html",
                          _generic_picker_context(token, state, GenericMappingForm(state["column_labels"])))
        mapping = importers.resolve_mapping(preset, state["headers"], state["rows"][0] if state["rows"] else None)
        if mapping is None:
            messages.warning(request, f"The {importers.PRESETS[preset]['label']} layout does not fit this file.")
            return render(request, "banking/import.html",
                          _generic_picker_context(token, state, GenericMappingForm(state["column_labels"])))
        state["mapping"], state["preset"] = mapping, preset
        request.session[SESSION_PREFIX + token] = state
        request.session.modified = True
        mapped = importers.map_rows(state["rows"], mapping)
        return render(request, "banking/import.html", _preview_context(request, token, state, mapped))

    if step == "confirm":
        if not state.get("mapping"):
            messages.error(request, "Pick the columns first.")
            return redirect("bank_import")
        account = get_object_or_404(BankAccount, pk=state["account_id"])
        mapped = importers.map_rows(state["rows"], state["mapping"])
        if request.POST.get("flip"):
            importers.flip_rows(mapped)
        batch = importers.import_rows(account, mapped, user=request.user, preset=state["preset"],
                                      filename=state["filename"], stored_path=state.get("stored_path"))
        log_activity(request.user, "bank_import", batch,
                     note=f"{batch.rows_imported} imported, {batch.rows_duplicate} duplicates, "
                          f"{batch.rows_skipped} skipped")
        del request.session[SESSION_PREFIX + token]
        request.session.modified = True
        messages.success(request, f"Imported {batch.rows_imported} transaction{'s' if batch.rows_imported != 1 else ''} "
                                  f"into {account.name}.")
        return redirect("bank_import_result", batch.pk)
    return HttpResponseBadRequest("Unknown step")


def _generic_picker_context(token, state, mform):
    account = BankAccount.objects.filter(pk=state["account_id"]).first()
    return {"step": "map", "token": token, "account": account, "mapping_form": mform,
            "filename": state["filename"], "headers": state.get("headers"), "sample": state["rows"][:5],
            "column_labels": state["column_labels"], "warnings": state.get("warnings", []),
            "presets": importers.PRESET_CHOICES, "preset": state.get("preset") or "generic"}


@module_required("banking")
def import_result(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related("account"), pk=pk)
    sample = batch.transactions.filter(is_deleted=False).select_related("category_account")[:10]
    return render(request, "banking/import_result.html", {"batch": batch, "sample": sample})


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
@module_required("banking")
def rules(request):
    qs = CategoryRule.objects.select_related("category_account", "vendor", "account_filter")
    return render(request, "banking/rules.html", {
        "rules": qs, "unreviewed": BankTransaction.objects.filter(is_deleted=False,
                                                                  status=BankTransaction.UNREVIEWED).count()})


def _rule_form(request, rule=None):
    form = CategoryRuleForm(request.POST or None, instance=rule)
    test = None
    if request.method == "POST":
        if form.is_valid():
            if request.POST.get("test"):
                probe = form.save(commit=False)
                hits, sample = test_rule(probe)
                test = {"hits": hits, "sample": sample}
                messages.info(request, f"This rule would match {hits} transaction{'s' if hits != 1 else ''} "
                                       "waiting for review. Nothing was saved yet.")
            else:
                is_new = rule is None
                rule = form.save(commit=False)
                if is_new:
                    rule.created_by = request.user
                rule.save()
                log_activity(request.user, "create" if is_new else "update", rule)
                applied = 0
                if request.POST.get("apply_now") and rule.active:
                    applied = apply_single_rule(rule, user=request.user)
                msg = f"Rule '{rule.name}' saved."
                if applied:
                    msg += f" Applied to {applied} transaction{'s' if applied != 1 else ''} in the inbox."
                messages.success(request, msg)
                return redirect("bank_rules")
        else:
            messages.error(request, "Please fix the highlighted fields.")
    return render(request, "banking/rule_form.html", {"form": form, "rule": rule, "test": test})


@module_required("banking")
def rule_create(request):
    return _rule_form(request)


@module_required("banking")
def rule_edit(request, pk):
    rule = get_object_or_404(CategoryRule, pk=pk)
    return _rule_form(request, rule)


@module_required("banking")
@require_POST
def rule_delete(request, pk):
    rule = get_object_or_404(CategoryRule, pk=pk)
    rule.delete(user=request.user)
    log_activity(request.user, "delete", rule)
    messages.success(request, f"Rule '{rule.name}' removed. Transactions it already categorized are unchanged.")
    return redirect("bank_rules")


# ---------------------------------------------------------------------------
# Plaid connect
# ---------------------------------------------------------------------------
def _provider_or_none():
    try:
        p = get_provider("plaid")
        return p if p.is_configured() else None
    except ProviderError:
        return None


@module_required("banking")
def connect(request):
    provider = _provider_or_none()
    settings_obj = CompanySettings.get()
    return render(request, "banking/connect.html", {
        "configured": provider is not None,
        "env": getattr(provider, "env", settings_obj.plaid_env),
        "connections": BankConnection.objects.exclude(provider="manual").prefetch_related("accounts"),
        "unlinked_accounts": BankAccount.objects.filter(active=True, connection__isnull=True),
        "sync_enabled": settings_obj.bank_sync_enabled,
    })


@module_required("banking")
@require_POST
def connect_link_token(request):
    provider = _provider_or_none()
    if provider is None:
        return JsonResponse({"error": "Plaid is not configured."}, status=400)
    try:
        token = provider.create_link_token(request.user, redirect_uri=None)
    except ProviderError as exc:
        return JsonResponse({"error": str(exc), "code": exc.code}, status=502)
    return JsonResponse({"link_token": token})


@module_required("banking")
@require_POST
def connect_exchange(request):
    provider = _provider_or_none()
    if provider is None:
        messages.error(request, "Plaid is not configured.")
        return redirect("bank_connect")
    public_token = request.POST.get("public_token", "").strip()
    if not public_token:
        messages.error(request, "The bank login did not finish. Please try again.")
        return redirect("bank_connect")
    try:
        result = provider.exchange_public_token(public_token)
    except ProviderError as exc:
        messages.error(request, f"Plaid could not finish linking: {exc}")
        return redirect("bank_connect")
    metadata = {}
    try:
        metadata = json.loads(request.POST.get("metadata") or "{}")
    except ValueError:
        metadata = {}
    inst = metadata.get("institution") or {}
    conn = BankConnection.objects.create(
        provider="plaid", institution_name=(inst.get("name") or request.POST.get("institution_name")
                                            or "Connected bank")[:150],
        institution_id=(inst.get("institution_id") or "")[:100], item_id=result.get("item_id", "")[:120],
        access_token=result["access_token"], created_by=request.user)
    log_activity(request.user, "bank_connected", conn)
    messages.success(request, f"{conn.institution_name} linked. Choose which accounts to track.")
    return redirect("bank_connect_accounts", conn.pk)


@module_required("banking")
def connect_accounts(request, pk):
    conn = get_object_or_404(BankConnection, pk=pk)
    provider = _provider_or_none()
    if provider is None:
        messages.error(request, "Plaid is not configured.")
        return redirect("bank_connect")
    try:
        remote = provider.get_accounts(conn.access_token)
    except ProviderError as exc:
        conn.status = "relink" if exc.needs_relink else "error"
        conn.last_error = str(exc)[:500]
        conn.save(update_fields=["status", "last_error"])
        messages.error(request, f"Could not read the accounts at {conn.institution_name}: {exc}")
        return redirect("bank_connect")
    existing = {a.external_account_id: a for a in conn.accounts.all()}
    for r in remote:
        r["existing"] = existing.get(r["external_account_id"])
    unlinked = BankAccount.objects.filter(active=True, connection__isnull=True)
    if request.method == "POST":
        picked = 0
        for r in remote:
            ext = r["external_account_id"]
            if not request.POST.get(f"track_{ext}"):
                continue
            target = request.POST.get(f"map_{ext}", "new")
            if ext in existing:
                continue
            if target == "new":
                acct = BankAccount.objects.create(
                    name=(r["official_name"] or r["name"])[:120], institution=conn.institution_name,
                    account_type=r["account_type"], last4=(r["mask"] or "")[:4], connection=conn,
                    external_account_id=ext, opening_date=timezone.localdate(), opening_balance=Decimal("0"))
            else:
                acct = unlinked.filter(pk=target).first()
                if acct is None:
                    messages.warning(request, f"Could not map {r['name']}: that account was not found.")
                    continue
                acct.connection = conn
                acct.external_account_id = ext
                if not acct.institution:
                    acct.institution = conn.institution_name
                acct.save(update_fields=["connection", "external_account_id", "institution"])
            log_activity(request.user, "bank_account_linked", acct, note=conn.institution_name)
            picked += 1
        if not picked:
            messages.warning(request, "Tick at least one account to track.")
        else:
            summary = sync_connection(conn, provider=provider, user=request.user)
            if summary["error"]:
                messages.warning(request, f"Accounts linked, but the first sync failed: {summary['error']}")
            else:
                msg = f"{picked} account{'s' if picked != 1 else ''} linked. First sync pulled {summary['added']} transactions"
                if summary["accounts_initialized"]:
                    msg += (f"; opening balances were set from the bank for "
                            + ", ".join(summary["accounts_initialized"]))
                messages.success(request, msg + ".")
            return redirect("bank_accounts")
    return render(request, "banking/connect_accounts.html", {
        "conn": conn, "remote": remote, "existing": existing, "unlinked": unlinked,
    })


@module_required("banking")
@require_POST
def connection_disconnect(request, pk):
    conn = get_object_or_404(BankConnection, pk=pk)
    provider = _provider_or_none()
    if provider is not None and conn.access_token:
        try:
            provider.remove_item(conn.access_token)
        except ProviderError as exc:
            messages.warning(request, f"Plaid reported a problem removing the link ({exc}); it was disconnected here anyway.")
    conn.status = "disconnected"
    conn.access_token = ""
    conn.save(update_fields=["status", "access_token"])
    for acct in conn.accounts.all():
        acct.connection = None
        acct.save(update_fields=["connection"])
    log_activity(request.user, "bank_disconnected", conn)
    messages.success(request, f"{conn.institution_name} disconnected. Its transactions are kept; you can still import CSV files.")
    return redirect("bank_connect")


@module_required("banking")
@require_POST
def connection_sync(request, pk):
    conn = get_object_or_404(BankConnection, pk=pk)
    summary = sync_connection(conn, user=request.user)
    if summary["error"]:
        messages.error(request, f"{conn.institution_name}: {summary['error']}")
    else:
        messages.success(request, f"{conn.institution_name}: {summary['added']} new, {summary['modified']} updated, "
                                  f"{summary['removed']} removed, {summary['auto']} auto-categorized.")
    return redirect(_safe_next(request, reverse("bank_connect")))
