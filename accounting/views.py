"""
Accounting module: financial statements on top of ``accounting.ledger``.

Every page here reveals costs and profit, so each view carries both
``@module_required("accounting")`` and ``@costs_required``. Reports accept
``?basis=cash|accrual`` plus ``?export=csv|pdf`` where an export exists.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting import closing
from accounting import reports as rp
from accounting.forms import (AccountForm, CODE_RANGES, MonthCloseForm, PeriodLockForm,
                              SystemAccountForm, SUBTYPES_BY_TYPE)
from accounting.ledger import (Journal, compare_bases, dashboard_snapshot, equity_trend,
                               fiscal_year_start)
from accounting.models import ACCRUAL, CASH, Account, AccountType, FiscalPeriodLock, MonthClose
from core.csvutils import csv_response
from core.models import CompanySettings, log_activity
from core.pdf import pdf_response
from core.roles import admin_required, costs_required, is_admin, module_required

ZERO = Decimal("0.00")
JOURNAL_HTML_CAP = 2500       # rows shown on screen; CSV always has everything


# ---------------------------------------------------------------------------
# shared bits
# ---------------------------------------------------------------------------
def _common(request, basis, **extra):
    """Context every statement page needs for the filter bar and header."""
    today = timezone.localdate()
    ctx = {
        "basis": basis, "basis_label": rp.BASIS_LABELS[basis],
        "basis_help": rp.BASIS_HELP[basis], "other_basis": rp.other_basis(basis),
        "today": today,
        "presets": [{"key": k, "label": l, "start": s, "end": e}
                    for k, l, s, e in rp.period_presets(today)],
        "lock": FiscalPeriodLock.get(),
        "export_qs": request.GET.urlencode(),
    }
    ctx.update(extra)
    return ctx


def _pdf(request, title, subtitle, sections, columns, filename, extra=None):
    ctx = rp.statement_pdf_context(title, subtitle, sections, columns, **(extra or {}))
    return pdf_response("pdf/financial_statement.html", ctx, filename)


def _slug(basis, start, end=None):
    return f"{basis}_{start.isoformat()}" + (f"_{end.isoformat()}" if end else "")


# ---------------------------------------------------------------------------
# 1. hub
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def index(request):
    basis = rp.basis_from_request(request)
    today = timezone.localdate()
    snapshots = {CASH: dashboard_snapshot(CASH, today), ACCRUAL: dashboard_snapshot(ACCRUAL, today)}
    # "Am I building equity?" is an accrual question: the trend widget ignores the toggle.
    trend = equity_trend(ACCRUAL, months=6, as_of=today)
    settings_row = CompanySettings.get()
    fy_start = fiscal_year_start(today, settings_row.fiscal_year_start_month)
    pick = snapshots[basis]
    other = snapshots[rp.other_basis(basis)]
    ctx = _common(request, basis,
                  snap=pick, snap_other=other, snap_cash=snapshots[CASH],
                  snap_accrual=snapshots[ACCRUAL], trend=trend,
                  equity_gap=snapshots[ACCRUAL]["total_equity"] - snapshots[CASH]["total_equity"],
                  equity_gap_abs=abs(snapshots[ACCRUAL]["total_equity"] - snapshots[CASH]["total_equity"]),
                  ytd_gap=snapshots[ACCRUAL]["net_income_ytd"] - snapshots[CASH]["net_income_ytd"],
                  mtd_gap=snapshots[ACCRUAL]["net_income_mtd"] - snapshots[CASH]["net_income_mtd"],
                  fy_start=fy_start, settings_row=settings_row,
                  month_start=today.replace(day=1),
                  trend_basis=ACCRUAL, trend_basis_label=rp.BASIS_LABELS[ACCRUAL],
                  close_tile=closing.dashboard_tile(today),
                  account_count=Account.objects.count())
    return render(request, "accounting/index.html", ctx)


# ---------------------------------------------------------------------------
# 2. income statement
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def pnl(request):
    basis = rp.basis_from_request(request)
    start, end = rp.period_from_request(request, default="this_month")
    stmt = Journal(basis, [start - timedelta(days=1), end]).income_statement(start, end)
    title = "Income statement"
    subtitle = f"{rp.BASIS_LABELS[basis]} · {rp.period_label(start, end)}"
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"income_statement_{_slug(basis, start, end)}.csv",
                            rp.PNL_HEADER, rp.pnl_rows(stmt))
    if export == "pdf":
        return _pdf(request, title, subtitle, rp.pnl_pdf_sections(stmt), ["Amount"],
                    f"income_statement_{_slug(basis, start, end)}.pdf")
    ctx = _common(request, basis, stmt=stmt, start=start, end=end, title=title,
                  period_text=rp.period_label(start, end),
                  active_preset=rp.active_preset(start, end), show_period=True)
    return render(request, "accounting/pnl.html", ctx)


# ---------------------------------------------------------------------------
# 3. balance sheet
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def balance_sheet(request):
    basis = rp.basis_from_request(request)
    as_of = rp.as_of_from_request(request)
    bs = Journal(basis, [as_of]).balance_sheet(as_of)
    title = "Balance sheet"
    subtitle = f"{rp.BASIS_LABELS[basis]} · as of {as_of:%b %-d, %Y}"
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"balance_sheet_{_slug(basis, as_of)}.csv",
                            rp.BS_HEADER, rp.balance_sheet_rows(bs))
    if export == "pdf":
        return _pdf(request, title, subtitle, rp.balance_sheet_pdf_sections(bs), ["Amount"],
                    f"balance_sheet_{_slug(basis, as_of)}.pdf",
                    extra={"badge": "Balanced" if bs["balanced"] else
                           f"OUT OF BALANCE by ${bs['difference']}", "badge_ok": bs["balanced"]})
    ctx = _common(request, basis, bs=bs, as_of=as_of, title=title, show_as_of=True,
                  liab_plus_equity=bs["total_liabilities"] + bs["total_equity"])
    return render(request, "accounting/balance_sheet.html", ctx)


# ---------------------------------------------------------------------------
# 4. cash vs accrual
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def compare(request):
    start, end = rp.period_from_request(request, default="ytd")
    cmp = compare_bases(start, end)
    title = "Cash vs. accrual"
    subtitle = rp.period_label(start, end)
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"cash_vs_accrual_{start.isoformat()}_{end.isoformat()}.csv",
                            rp.COMPARE_HEADER, rp.compare_rows(cmp))
    if export == "pdf":
        return _pdf(request, title, subtitle, rp.compare_pdf_sections(cmp),
                    ["Cash", "Accrual", "Difference"],
                    f"cash_vs_accrual_{start.isoformat()}_{end.isoformat()}.pdf")
    sections = rp.compare_sections(cmp)
    bridge = rp.bridge_rows(cmp)
    bridge_total = sum((b["amount"] for b in bridge), ZERO)
    ctx = _common(request, rp.basis_from_request(request), cmp=cmp, sections=sections,
                  bridge=bridge, bridge_total=bridge_total,
                  bridge_unexplained=cmp["net_difference"] - bridge_total,
                  start=start, end=end, title=title, period_text=subtitle,
                  active_preset=rp.active_preset(start, end), show_period=True, hide_basis=True,
                  gross_diff=cmp["accrual"]["gross_profit"] - cmp["cash"]["gross_profit"])
    return render(request, "accounting/compare.html", ctx)


# ---------------------------------------------------------------------------
# 5. equity trend
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def equity_trend_view(request):
    basis = rp.basis_from_request(request)
    default_months = rp.default_trend_months()
    try:
        months = int(request.GET.get("months", default_months))
    except (TypeError, ValueError):
        months = default_months
    months = max(1, min(months, 60))
    rows = equity_trend(basis, months=months)
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"equity_trend_{basis}_{months}m.csv",
                            rp.EQUITY_HEADER, rp.equity_csv_rows(rows))
    if export == "pdf":
        return _pdf(request, "Equity trend",
                    f"{rp.BASIS_LABELS[basis]} · {rows[0]['label']} – {rows[-1]['label']}",
                    rp.equity_pdf_sections(rows), rp.EQUITY_PDF_COLUMNS,
                    f"equity_trend_{basis}_{months}m.pdf",
                    extra={"label_heading": "Month end", "code_heading": "",
                           "footnote": "Equity = what you own minus what you owe. 'Change' compares "
                                       "each month end to the one before. The current month shows "
                                       "the position as of today."})
    first, last = rows[0], rows[-1]
    ctx = _common(request, basis, rows=rows, months=months, chart=rp.equity_chart(rows),
                  title="Equity trend", first=first, last=last,
                  total_change=last["equity"] - first["equity"],
                  month_options=sorted({3, 6, 12, 24, 36, default_months}),
                  default_months=default_months, hide_period=True,
                  months_up=sum(1 for r in rows if r["equity_change"] is not None and r["equity_change"] > 0),
                  months_down=sum(1 for r in rows if r["equity_change"] is not None and r["equity_change"] < 0))
    return render(request, "accounting/equity_trend.html", ctx)


# ---------------------------------------------------------------------------
# 6. trial balance
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def trial_balance(request):
    basis = rp.basis_from_request(request)
    as_of = rp.as_of_from_request(request)
    tb = Journal(basis, [as_of]).trial_balance(as_of)
    title = "Trial balance"
    subtitle = f"{rp.BASIS_LABELS[basis]} · as of {as_of:%b %-d, %Y}"
    export = request.GET.get("export")
    if export == "csv":
        return csv_response(f"trial_balance_{_slug(basis, as_of)}.csv",
                            rp.TB_HEADER, rp.trial_balance_rows(tb))
    if export == "pdf":
        return _pdf(request, title, subtitle, rp.trial_balance_pdf_sections(tb), ["Debit", "Credit"],
                    f"trial_balance_{_slug(basis, as_of)}.pdf",
                    extra={"badge": "Balanced" if tb["balanced"] else "OUT OF BALANCE",
                           "badge_ok": tb["balanced"]})
    ctx = _common(request, basis, tb=tb, as_of=as_of, title=title, show_as_of=True,
                  difference=tb["total_debit"] - tb["total_credit"])
    return render(request, "accounting/trial_balance.html", ctx)


# ---------------------------------------------------------------------------
# 7. general ledger
# ---------------------------------------------------------------------------
def _account_groups(accounts):
    """[(type label, [accounts])] in statement order."""
    order = [AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
             AccountType.INCOME, AccountType.COGS, AccountType.EXPENSE]
    groups = []
    for t in order:
        items = [a for a in accounts if a.type == t]
        if items:
            groups.append({"type": t, "label": AccountType(t).label, "accounts": items})
    return groups


@module_required("accounting")
@costs_required
def general_ledger(request):
    basis = rp.basis_from_request(request)
    start, end = rp.period_from_request(request, default="this_month")
    accounts = list(Account.objects.order_by("code", "name"))
    account = None
    raw = request.GET.get("account")
    if raw:
        try:
            account = Account.objects.get(pk=int(raw))
        except (ValueError, Account.DoesNotExist):
            messages.error(request, "That account does not exist. Pick one from the list.")
    gl = None
    if account is not None:
        gl = Journal(basis, [start - timedelta(days=1), end]).general_ledger(account, start, end)
        export = request.GET.get("export")
        if export == "csv":
            return csv_response(f"general_ledger_{account.code}_{_slug(basis, start, end)}.csv",
                                rp.GL_HEADER, rp.gl_csv_rows(gl))
        if export == "pdf":
            return _pdf(request, "General ledger",
                        f"{rp.BASIS_LABELS[basis]} · {rp.period_label(start, end)}",
                        rp.gl_pdf_sections(gl), rp.GL_PDF_COLUMNS,
                        f"general_ledger_{account.code}_{_slug(basis, start, end)}.pdf",
                        extra={"wide_code": True, "code_heading": "Date", "label_heading": "Memo",
                               "footnote": "Balance is shown the natural way for this account: "
                                           "positive for money in an asset, positive for an amount "
                                           "owed, positive for income earned."})
    ctx = _common(request, basis, groups=_account_groups(accounts), account=account, gl=gl,
                  start=start, end=end, title="General ledger",
                  period_text=rp.period_label(start, end),
                  active_preset=rp.active_preset(start, end), show_period=True,
                  total_debit=sum((r["debit"] for r in gl["rows"]), ZERO) if gl else ZERO,
                  total_credit=sum((r["credit"] for r in gl["rows"]), ZERO) if gl else ZERO)
    return render(request, "accounting/general_ledger.html", ctx)


# ---------------------------------------------------------------------------
# 8. journal
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def journal(request):
    basis = rp.basis_from_request(request)
    start, end = rp.period_from_request(request, default="this_month")
    j = Journal(basis, [start - timedelta(days=1), end])
    rows = j.journal_rows(start, end)
    if request.GET.get("export") == "csv":
        return csv_response(f"general_journal_{_slug(basis, start, end)}.csv",
                            rp.JOURNAL_HEADER, rp.journal_csv_rows(rows))
    total_debit = sum((r["debit"] for r in rows), ZERO)
    total_credit = sum((r["credit"] for r in rows), ZERO)
    shown = rows[:JOURNAL_HTML_CAP]
    ctx = _common(request, basis, rows=shown, row_count=len(rows), truncated=len(rows) > len(shown),
                  entry_count=len(j.entries_between(start, end)),
                  total_debit=total_debit, total_credit=total_credit,
                  balanced=total_debit == total_credit, unbalanced=len(j.validate()),
                  start=start, end=end, title="General journal",
                  period_text=rp.period_label(start, end),
                  active_preset=rp.active_preset(start, end), show_period=True)
    return render(request, "accounting/journal.html", ctx)


# ---------------------------------------------------------------------------
# 9. chart of accounts
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def accounts(request):
    today = timezone.localdate()
    if request.method == "POST" and request.POST.get("action") == "toggle":
        acct = get_object_or_404(Account, pk=request.POST.get("pk"))
        if acct.is_system:
            messages.error(request, "System accounts cannot be turned off.")
        else:
            acct.active = not acct.active
            acct.save(update_fields=["active"])
            log_activity(request.user, "update", acct,
                         changes={"active": acct.active},
                         note="Account turned on" if acct.active else "Account turned off")
            messages.success(request, f"{acct} is now {'active' if acct.active else 'inactive'}.")
        return redirect("accounting_accounts")

    show_inactive = request.GET.get("inactive") == "1"
    j = Journal(ACCRUAL, [today])
    balances = j.balances_through(today)
    qs = Account.objects.order_by("code", "name")
    items = []
    for a in qs:
        if not a.active and not show_inactive:
            continue
        raw = balances.get(a.pk, ZERO)
        a.current_balance = a.signed_balance(raw)
        a.linked = _linked_label(a)
        items.append(a)
    groups = _account_groups(items)
    for g in groups:
        g["total"] = sum((a.current_balance for a in g["accounts"]), ZERO)
    ctx = _common(request, ACCRUAL, groups=groups, as_of=today, show_inactive=show_inactive,
                  inactive_count=Account.objects.filter(active=False).count(),
                  title="Chart of accounts", hide_period=True)
    return render(request, "accounting/accounts.html", ctx)


def _linked_label(acct):
    """Where this account comes from, for the 'source' column."""
    if acct.is_system:
        return "System"
    if getattr(acct, "expense_category", None) is not None:
        return "Expense category"
    if getattr(acct, "bank_account", None) is not None:
        return "Bank / card account"
    return "Custom"


@module_required("accounting")
@costs_required
def account_create(request):
    initial = {}
    type_ = request.GET.get("type")
    if type_ in CODE_RANGES:
        lo, hi = CODE_RANGES[type_]
        initial = {"type": type_, "code": Account.next_code(lo, hi + 1),
                   "subtype": SUBTYPES_BY_TYPE[type_][-1]}
    if request.method == "POST":
        form = AccountForm(request.POST)
        if form.is_valid():
            acct = form.save()
            log_activity(request.user, "create", acct, note="Chart of accounts")
            messages.success(request, f"Added account {acct}.")
            return redirect("accounting_accounts")
        messages.error(request, "Please fix the highlighted fields.")
    else:
        form = AccountForm(initial=initial)
    return render(request, "accounting/account_form.html",
                  {"form": form, "account": None, "title": "New account",
                   "code_ranges": CODE_RANGES, "subtypes_by_type": _subtype_map_json()})


@module_required("accounting")
@costs_required
def account_edit(request, pk):
    acct = get_object_or_404(Account, pk=pk)
    form_cls = SystemAccountForm if acct.is_system else AccountForm
    if request.method == "POST":
        form = form_cls(request.POST, instance=acct)
        if form.is_valid():
            before = {f: form.initial.get(f) for f in form.changed_data}
            acct = form.save()
            after = {f: getattr(acct, f) for f in form.changed_data}
            log_activity(request.user, "update", acct,
                         changes={f: [str(before[f]), str(after[f])] for f in form.changed_data},
                         note="Chart of accounts")
            messages.success(request, f"Saved {acct}.")
            return redirect("accounting_accounts")
        messages.error(request, "Please fix the highlighted fields.")
    else:
        form = form_cls(instance=acct)
    return render(request, "accounting/account_form.html",
                  {"form": form, "account": acct, "title": f"Edit {acct.code} {acct.name}",
                   "code_ranges": CODE_RANGES, "subtypes_by_type": _subtype_map_json()})


def _subtype_map_json():
    import json
    return json.dumps({t.value: [s.value for s in subs] for t, subs in SUBTYPES_BY_TYPE.items()})


# ---------------------------------------------------------------------------
# 10. year-end / month-end package
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def year_end(request):
    today = timezone.localdate()
    settings_row = CompanySettings.get()
    start_month = settings_row.fiscal_year_start_month or 1
    default_year = rp.last_complete_fiscal_year(today, start_month)

    wants_zip = any(k in request.GET for k in ("year", "month", "download"))
    if wants_zip:
        month = rp.parse_month(request.GET.get("month"))
        if request.GET.get("month") and month is None:
            messages.error(request, "Enter the month as YYYY-MM, e.g. 2026-03.")
            return redirect("accounting_year_end")
        if month:
            start, end = month
            if start > today:
                messages.error(request, "That month has not started yet.")
                return redirect("accounting_year_end")
            label = f"Month-end package {start:%B %Y}"
            filename = f"month_end_{start:%Y_%m}.zip"
        else:
            try:
                year = int(request.GET.get("year", default_year))
            except (TypeError, ValueError):
                messages.error(request, "Enter the year as four digits, e.g. 2025.")
                return redirect("accounting_year_end")
            if year < 1990 or year > today.year:
                messages.error(request, "Pick a fiscal year that has already started.")
                return redirect("accounting_year_end")
            start, end = rp.fiscal_year_bounds(year, start_month)
            label = f"Year-end package {rp.fiscal_year_label(start, end)}"
            filename = f"year_end_{year}.zip"
        data = rp.build_package_zip(start, end, label)
        log_activity(request.user, "export", model="FinancialPackage",
                     note=f"{label} ({start} – {end})")
        resp = HttpResponse(data, content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    # chooser page
    current_fy_start = fiscal_year_start(today, start_month)
    years = []
    books_start = settings_row.books_start_date
    first_year = (books_start.year - 1) if books_start else today.year - 4
    for y in range(current_fy_start.year, max(first_year, 1990) - 1, -1):
        s, e = rp.fiscal_year_bounds(y, start_month)
        years.append({"year": y, "label": rp.fiscal_year_label(s, e), "start": s, "end": e,
                      "complete": e < today, "default": y == default_year})
    last_month_end = today.replace(day=1) - timedelta(days=1)
    months = []
    cur = last_month_end
    for _ in range(12):
        months.append({"value": cur.strftime("%Y-%m"), "label": cur.strftime("%B %Y")})
        cur = (cur.replace(day=1) - timedelta(days=1))
    check_start, check_end = rp.fiscal_year_bounds(default_year, start_month)
    ctx = _common(request, rp.basis_from_request(request), title="Year-end package",
                  years=years, months=months, default_year=default_year,
                  files=rp.PACKAGE_FILES, csv_files=rp.PACKAGE_CSV_FILES,
                  pdf_files=rp.PACKAGE_PDF_FILES, start_month=start_month,
                  start_month_name=calendar.month_name[start_month],
                  hide_period=True, hide_basis=True,
                  this_month=today.strftime("%Y-%m"), last_month=last_month_end.strftime("%Y-%m"),
                  checklist=_accountant_checklist(check_start, check_end, today),
                  check_start=check_start, check_end=check_end,
                  check_label=rp.fiscal_year_label(check_start, check_end))
    return render(request, "accounting/year_end.html", ctx)


def _accountant_checklist(start, end, today):
    """'Before you send to your accountant' rows for the fiscal year
    [start, end]: the live automatic checks plus how many of the year's
    months were reconciled and closed."""
    items = []
    for c in closing.automatic_checks(start, end):
        items.append({"key": c["key"], "label": c["label"], "ok": c["ok"], "count": c["count"],
                      "amount": c["amount"], "url": c["url"], "link_label": c["link_label"],
                      "noun": c["noun"], "help": c["help"]})
    months = [ym for ym in closing.months_in_range(start, end) if date(ym[0], ym[1], 1) <= today]
    if not months:
        return items
    rows = closing.close_rows_for(months)
    checked = [ym for ym in months if ym in rows and (rows[ym].steps or {}).get("statements_checked")]
    closed = [ym for ym in months if ym in rows and rows[ym].is_closed]
    first_open = next((ym for ym in months if ym not in checked), None)
    items.insert(1, {
        "key": "statements_checked", "label": "Every bank and card account checked against its statement",
        "ok": len(checked) == len(months), "count": len(months) - len(checked), "amount": None,
        "url": closing.month_url(*first_open) if first_open else closing.month_url(*months[-1]),
        "link_label": "Open month close", "noun": "month",
        "progress": f"{len(checked)} of {len(months)} months",
        "help": "Ticked on the month-close page once each statement balance matches the books."})
    items.append({
        "key": "months_closed", "label": "Every month is closed",
        "ok": len(closed) == len(months), "count": len(months) - len(closed), "amount": None,
        "url": closing.month_url(*next((ym for ym in months if ym not in closed), months[-1])),
        "link_label": "Close it", "noun": "month",
        "progress": f"{len(closed)} of {len(months)} months",
        "help": "Closing a month records that you finished its checklist."})
    return items


# ---------------------------------------------------------------------------
# 10b. month close
# ---------------------------------------------------------------------------
@module_required("accounting")
@costs_required
def month_close(request):
    today = timezone.localdate()
    raw = request.POST.get("month") if request.method == "POST" else request.GET.get("month")
    ym = rp.parse_month(raw)
    if raw and ym is None:
        messages.error(request, "Enter the month as YYYY-MM, e.g. 2026-03.")
        return redirect("accounting_month_close")
    if ym is None:
        year, month = closing.previous_month(today)
    else:
        if ym[0] > today:
            messages.error(request, "That month has not started yet.")
            return redirect("accounting_month_close")
        year, month = ym[0].year, ym[0].month
    label = closing.month_label(year, month)
    here = closing.month_url(year, month)

    if request.method == "POST":
        row, _ = MonthClose.objects.get_or_create(year=year, month=month)
        action = request.POST.get("action", "save")
        if action == "reopen":
            row.closed_at = None
            row.closed_by = None
            row.save(update_fields=["closed_at", "closed_by", "updated_at"])
            log_activity(request.user, "update", row, note=f"{label} reopened")
            messages.success(request, f"{label} is open again. Finish the checklist and close it when ready.")
            return redirect(here)
        form = MonthCloseForm(request.POST)
        if form.is_valid():
            row.steps = form.steps()
            row.notes = form.cleaned_data["notes"]
            if action == "close":
                row.closed_at = timezone.now()
                row.closed_by = request.user
                row.save()
                note = f"{label} closed"
                if form.cleaned_data["lock_books"] and is_admin(request.user):
                    end = closing.month_bounds(year, month)[1]
                    lock = FiscalPeriodLock.get()
                    if lock.locked_through is None or lock.locked_through < end:
                        lock.locked_through = end
                        lock.note = f"Month close: {label}"
                        lock.save()
                        log_activity(request.user, "update", lock,
                                     changes={"locked_through": str(end)}, note=lock.note)
                        note += f"; books locked through {end:%b %-d, %Y}"
                        messages.success(request, f"{label} closed and the books are locked through "
                                                  f"{end:%b %-d, %Y}.")
                    else:
                        messages.success(request, f"{label} closed. The books were already locked "
                                                  f"through {lock.locked_through:%b %-d, %Y}.")
                else:
                    messages.success(request, f"{label} closed. Nice work — the habit is what matters.")
                log_activity(request.user, "update", row, changes={"steps": row.steps}, note=note)
            else:
                row.save()
                log_activity(request.user, "update", row, changes={"steps": row.steps},
                             note=f"{label} checklist saved")
                messages.success(request, "Checklist saved.")
            return redirect(here)
        messages.error(request, "Please fix the highlighted fields.")
        status = closing.month_status(year, month)
    else:
        status = closing.month_status(year, month)
        initial = {m["key"]: m["checked"] for m in status["manual"]}
        initial["notes"] = status["month_close"].notes
        form = MonthCloseForm(initial=initial)

    prev_y, prev_m = status["prev"]
    ctx = {
        "status": status, "form": form, "title": f"Month close · {label}", "label": label,
        "year": year, "month": month, "month_param": closing.month_param(year, month),
        "today": today, "lock": FiscalPeriodLock.get(),
        "prev_url": closing.month_url(prev_y, prev_m), "prev_label": closing.month_label(prev_y, prev_m),
        "next_url": closing.month_url(*status["next"]) if status["next"] else "",
        "next_label": closing.month_label(*status["next"]) if status["next"] else "",
        "package_url": f"{reverse('accounting_year_end')}?month={closing.month_param(year, month)}",
        "pnl_url": f"{reverse('accounting_pnl')}?basis=accrual&start={status['start']}&end={status['end']}",
        "can_lock": is_admin(request.user),
        "is_current_month": (year, month) == (today.year, today.month),
    }
    return render(request, "accounting/month_close.html", ctx)


# ---------------------------------------------------------------------------
# 11. period lock
# ---------------------------------------------------------------------------
@module_required("accounting")
@admin_required
def period_lock(request):
    lock = FiscalPeriodLock.get()
    if request.method == "POST":
        if request.POST.get("action") == "clear":
            lock.locked_through = None
            lock.note = ""
            lock.save()
            log_activity(request.user, "update", lock, note="Period lock removed")
            messages.success(request, "Books are open again. Any date can be edited.")
            return redirect("accounting_index")
        form = PeriodLockForm(request.POST, instance=lock)
        if form.is_valid():
            lock = form.save()
            log_activity(request.user, "update", lock,
                         changes={"locked_through": str(lock.locked_through)},
                         note=lock.note)
            if lock.locked_through:
                messages.success(request, f"Books closed through {lock.locked_through:%b %-d, %Y}. "
                                          "Edits to documents on or before that date will warn.")
            else:
                messages.success(request, "Books are open. No period is closed.")
            return redirect("accounting_index")
        messages.error(request, "Please fix the highlighted fields.")
    else:
        form = PeriodLockForm(instance=lock)
    today = timezone.localdate()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    prev_fy_start = fiscal_year_start(today) - timedelta(days=1)
    ctx = {"form": form, "lock": lock, "title": "Close the books",
           "suggest_month": last_month_end, "suggest_year": prev_fy_start,
           "today": today}
    return render(request, "accounting/period_lock.html", ctx)
