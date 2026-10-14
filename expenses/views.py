"""
Expenses module views.

Register + quick capture + vendors + categories + recurring templates +
budgets + mileage + four reports + the year-end ZIP. Everything is gated by
the "expenses" module; nothing hard-deletes.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting.models import ACCRUAL, CASH
from banking.models import BankAccount
from core.csvutils import csv_response
from core.models import CompanySettings, log_activity
from core.pdf import pdf_response
from core.roles import module_required
from expenses import reports
from expenses.forms import (ExpenseCategoryForm, ExpenseForm, MarkPaidForm, MileageForm,
                            QuickExpenseForm, RecurringExpenseForm, VendorForm,
                            categories_recent_first, get_or_create_category)
from expenses.models import Budget, Expense, ExpenseCategory, MileageEntry, RecurringExpense, Vendor
from expenses.tasks import generate_recurring_expenses

ZERO = Decimal("0.00")
PAID_Q = Q(paid_date__isnull=False) | Q(bank_txn__isnull=False)

PRESETS = [("this_month", "This month"), ("last_month", "Last month"),
           ("this_quarter", "This quarter"), ("this_year", "This year"),
           ("last_year", "Last year"), ("all", "All time")]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _year(request, param="year"):
    today = timezone.localdate()
    year = _int(request.GET.get(param), today.year)
    return min(max(year, 2000), 2100)


def _date(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _decimal(value, default=None):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return default


def _preset_range(key, today):
    """(start, end) for a quick date preset; (None, None) = all time."""
    if key == "this_month":
        return today.replace(day=1), today
    if key == "last_month":
        first = today.replace(day=1)
        last_end = first - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if key == "this_quarter":
        qm = (today.month - 1) // 3 * 3 + 1
        return today.replace(month=qm, day=1), today
    if key == "this_year":
        return today.replace(month=1, day=1), today
    if key == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None


def _vendor_names():
    return list(Vendor.objects.filter(active=True).values_list("name", flat=True))


def _years(today=None):
    today = today or timezone.localdate()
    first = Expense.objects.order_by("date").values_list("date", flat=True).first()
    start = min(first.year if first else today.year, today.year - 1)
    return list(range(today.year + 1, start - 1, -1))


def _bank_accounts():
    return BankAccount.objects.filter(active=True)


def _money_str(value, blank_zero=False):
    value = Decimal(value)
    if blank_zero and value == 0:
        return ""
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# 1. Register
# ---------------------------------------------------------------------------
def _list_filters(request, today):
    g = request.GET
    preset = g.get("preset") or ""
    start, end = _date(g.get("start")), _date(g.get("end"))
    if not preset and not start and not end:
        preset = "this_year"
    if preset and preset != "custom":
        p_start, p_end = _preset_range(preset, today)
        if preset != "all":
            start, end = p_start, p_end
        else:
            start = end = None
    return {
        "preset": preset, "start": start, "end": end,
        "category": g.get("category", ""), "vendor": (g.get("vendor") or "").strip(),
        "status": g.get("status", ""), "source": g.get("source", ""),
        "receipt": g.get("receipt", ""), "review": g.get("review", ""),
        "sort": g.get("sort", ""),
    }


def _apply_filters(qs, f, today):
    if f["start"]:
        qs = qs.filter(date__gte=f["start"])
    if f["end"]:
        qs = qs.filter(date__lte=f["end"])
    if f["category"] == "none":
        qs = qs.filter(category__isnull=True)
    elif _int(f["category"]):
        qs = qs.filter(category_id=_int(f["category"]))
    if f["vendor"]:
        qs = qs.filter(Q(vendor__icontains=f["vendor"]) | Q(vendor_link__name__icontains=f["vendor"]))
    if f["status"] == "paid":
        qs = qs.filter(PAID_Q)
    elif f["status"] == "unpaid":
        qs = qs.exclude(PAID_Q)
    elif f["status"] == "overdue":
        qs = qs.exclude(PAID_Q).filter(due_date__lt=today)
    if f["source"]:
        qs = qs.filter(source=f["source"])
    if f["review"] == "yes":
        qs = qs.filter(is_reviewed=False)
    sort = f["sort"]
    order = {"amount": ["amount", "-date"], "-amount": ["-amount", "-date"],
             "date": ["date", "id"], "vendor": ["vendor", "-date"]}.get(sort, ["-date", "-id"])
    return qs.order_by(*order)


def _filtered_expenses(request, today):
    f = _list_filters(request, today)
    rows = list(_apply_filters(reports.live_expenses(), f, today)[:5000])
    if f["receipt"] == "yes":
        rows = [e for e in rows if not reports.receipt_missing(e)]
    elif f["receipt"] == "no":
        rows = [e for e in rows if reports.receipt_missing(e)]
    return f, rows


def _list_csv(rows):
    header = ["ID", "Date", "Paid date", "Status", "Category", "Vendor", "Amount", "Reference",
              "Note", "Due date", "Paid from", "Payment method", "Source", "Receipt", "Reviewed"]
    out = []
    for e in rows:
        paid_on = e.effective_paid_date
        status = "overdue" if e.is_overdue else ("paid" if e.is_paid else "unpaid")
        out.append([e.pk, e.date, paid_on or "", status, e.category.name if e.category_id else "",
                    e.vendor, f"{e.amount:.2f}", e.reference, e.note, e.due_date or "",
                    str(e.paid_from) if e.paid_from_id else "",
                    e.get_payment_method_display() if e.payment_method else "",
                    e.get_source_display(), "yes" if not reports.receipt_missing(e) else "no",
                    "yes" if e.is_reviewed else "no"])
    return csv_response("expenses.csv", header, out)


def _bulk(request, ids, action, user):
    qs = Expense.objects.filter(pk__in=ids)
    n = qs.count()
    if n == 0:
        messages.warning(request, "Nothing selected.")
        return
    if action == "reviewed":
        qs.update(is_reviewed=True)
        messages.success(request, f"{n} expense{'s' if n != 1 else ''} marked reviewed.")
    elif action == "paid":
        form = MarkPaidForm(request.POST, prefix="bulk")
        if not form.is_valid():
            messages.error(request, "Enter a valid paid date.")
            return
        done = 0
        for e in qs:
            if e.bank_transaction is not None:
                continue       # paid status comes from the bank for these
            e.mark_paid(form.cleaned_data["paid_date"], form.cleaned_data.get("paid_from"),
                        form.cleaned_data.get("payment_method") or "")
            done += 1
        messages.success(request, f"{done} expense{'s' if done != 1 else ''} marked paid.")
    elif action == "category":
        cat = ExpenseCategory.objects.filter(pk=_int(request.POST.get("category"))).first()
        if cat is None and request.POST.get("new_category", "").strip():
            cat = get_or_create_category(request.POST["new_category"])
        if cat is None:
            messages.error(request, "Pick a category to apply.")
            return
        qs.update(category=cat)
        for e in qs.select_related("bank_txn"):
            txn = e.bank_transaction
            if txn is not None:
                txn.category_account = cat.ensure_account()
                txn.save(update_fields=["category_account"])
        messages.success(request, f"{n} expense{'s' if n != 1 else ''} set to {cat.name}.")
    else:
        messages.error(request, "Unknown bulk action.")
    log_activity(user, f"expense_bulk_{action}", None, model="Expense", note=f"{n} rows")


def _expense_list_post(request):
    action = request.POST.get("action")
    user = request.user
    if action == "del":
        exp = get_object_or_404(Expense, pk=_int(request.POST.get("id"), 0))
        if exp.bank_transaction is not None:
            messages.error(request, "This expense came from a bank transaction. Change the "
                                    "transaction's category in Banking instead.")
        else:
            exp.delete(user=user)
            log_activity(user, "delete", exp)
            messages.success(request, "Expense deleted (recoverable from the activity log).")
    elif action == "mark_paid":
        exp = get_object_or_404(Expense, pk=_int(request.POST.get("id"), 0))
        form = MarkPaidForm(request.POST)
        if exp.bank_transaction is not None:
            messages.info(request, "Already paid — this expense is linked to a bank transaction.")
        elif form.is_valid():
            exp.mark_paid(form.cleaned_data["paid_date"], form.cleaned_data.get("paid_from"),
                          form.cleaned_data.get("payment_method") or "")
            log_activity(user, "expense_paid", exp)
            messages.success(request, f"Marked paid: {exp.vendor or 'expense'} ${exp.amount:,.2f}.")
        else:
            messages.error(request, "Enter a valid paid date.")
    elif action == "review":
        exp = get_object_or_404(Expense, pk=_int(request.POST.get("id"), 0))
        exp.is_reviewed = True
        exp.save(update_fields=["is_reviewed", "updated_at"])
        messages.success(request, "Marked reviewed.")
    elif action == "bulk":
        ids = [i for i in (_int(x) for x in request.POST.getlist("ids")) if i]
        _bulk(request, ids, request.POST.get("bulk_action"), user)
    else:
        messages.error(request, "Unknown action.")
    return redirect(request.get_full_path())


@module_required("expenses")
def expense_list(request):
    today = timezone.localdate()
    quick_form = None
    if request.method == "POST":
        if request.POST.get("action") == "add":
            quick_form = QuickExpenseForm(request.POST, request.FILES, camera=False)
            if quick_form.is_valid():
                exp = quick_form.save(user=request.user, source="manual")
                log_activity(request.user, "create", exp)
                messages.success(request, f"Added {exp.vendor or exp.category} ${exp.amount:,.2f}"
                                          f"{'' if exp.is_paid else ' as an open bill'}.")
                return redirect(request.get_full_path())
            messages.error(request, "Expense not added — fix the highlighted fields.")
        else:
            return _expense_list_post(request)

    f, rows = _filtered_expenses(request, today)
    if request.GET.get("export") == "csv":
        return _list_csv(rows)

    total = sum((e.amount for e in rows), ZERO)
    unpaid_rows = [e for e in rows if not e.is_paid]
    tiles = {
        "total": total, "count": len(rows),
        "unpaid": sum((e.amount for e in unpaid_rows), ZERO), "unpaid_count": len(unpaid_rows),
        "overdue_count": sum(1 for e in unpaid_rows if e.is_overdue),
        "missing_receipts": sum(1 for e in rows if reports.receipt_missing(e)),
        "needs_review": sum(1 for e in rows if not e.is_reviewed),
    }
    paginator = Paginator(rows, 100)
    page = paginator.get_page(request.GET.get("page"))
    base_qs = request.GET.copy()
    base_qs.pop("page", None)
    base_qs.pop("export", None)

    return render(request, "expenses/expense_list.html", {
        "page": page, "expenses": page.object_list, "tiles": tiles, "filters": f,
        "presets": PRESETS, "base_qs": base_qs.urlencode(),
        "quick_form": quick_form or QuickExpenseForm(camera=False),
        "mark_paid_form": MarkPaidForm(), "bulk_paid_form": MarkPaidForm(prefix="bulk"),
        "categories": ExpenseCategory.objects.all(), "bank_accounts": _bank_accounts(),
        "vendor_names": _vendor_names(), "sources": Expense.SOURCES, "today": today,
    })


# ---------------------------------------------------------------------------
# 2. Create / edit
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_create(request):
    initial = {}
    if request.GET.get("vendor"):
        initial["vendor"] = request.GET["vendor"]
    if _int(request.GET.get("category")):
        initial["category"] = _int(request.GET["category"])
    form = ExpenseForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            exp = form.save(user=request.user, source="manual")
            log_activity(request.user, "create", exp)
            messages.success(request, f"Saved {exp.vendor or exp.category} ${exp.amount:,.2f}.")
            if request.POST.get("then") == "another":
                return redirect("expense_create")
            return redirect("expense_list")
        messages.error(request, "Not saved — fix the highlighted fields.")
    return render(request, "expenses/expense_form.html", {
        "form": form, "expense": None, "txn": None,
        "vendor_names": _vendor_names(), "today": timezone.localdate(),
    })


@module_required("expenses")
def expense_edit(request, pk):
    exp = get_object_or_404(Expense.objects.select_related("category", "vendor_link", "paid_from"),
                            pk=pk)
    txn = exp.bank_transaction
    if request.method == "POST" and request.POST.get("action") == "delete":
        if txn is not None:
            messages.error(request, "This expense came from a bank transaction. To remove it, open "
                                    "the transaction in Banking and change how it's categorized.")
            return redirect("expense_edit", pk=exp.pk)
        exp.delete(user=request.user)
        log_activity(request.user, "delete", exp)
        messages.success(request, "Expense deleted (recoverable from the activity log).")
        return redirect("expense_list")

    before_cat = exp.category_id          # read before the form mutates the instance
    form = ExpenseForm(request.POST or None, request.FILES or None, instance=exp, linked=txn is not None)
    if request.method == "POST":
        if form.is_valid():
            exp = form.save(user=request.user)
            if txn is not None and exp.category_id and exp.category_id != before_cat:
                txn.category_account = exp.category.ensure_account()
                txn.save(update_fields=["category_account"])
            log_activity(request.user, "update", exp)
            messages.success(request, "Expense saved.")
            return redirect("expense_list")
        messages.error(request, "Not saved — fix the highlighted fields.")
    return render(request, "expenses/expense_form.html", {
        "form": form, "expense": exp, "txn": txn,
        "vendor_names": _vendor_names(), "today": timezone.localdate(),
    })


# ---------------------------------------------------------------------------
# 3. Quick capture (phone)
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_quick(request):
    form = QuickExpenseForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        if form.is_valid():
            exp = form.save(user=request.user, source="quick")
            log_activity(request.user, "create", exp, note="quick capture")
            messages.success(request, f"Saved ${exp.amount:,.2f} to {exp.category}"
                                      f"{' · ' + exp.vendor if exp.vendor else ''}"
                                      f"{'' if exp.has_receipt else ' (no receipt attached)'}.")
            return redirect(f"{reverse('expense_quick')}?saved={exp.pk}")
        messages.error(request, "Not saved — check the fields below.")
    saved = Expense.objects.filter(pk=_int(request.GET.get("saved"), 0)).first()
    cats = categories_recent_first()
    return render(request, "expenses/expense_quick.html", {
        "form": form, "saved": saved, "categories": cats, "chips": cats[:6],
        "vendor_names": _vendor_names(), "bank_accounts": _bank_accounts(),
        "today": timezone.localdate(),
    })


# ---------------------------------------------------------------------------
# 4. Vendors
# ---------------------------------------------------------------------------
def _link_matching_expenses(vendor):
    """Attach unlinked expenses whose vendor text looks like this vendor."""
    n = 0
    for e in Expense.objects.filter(vendor_link__isnull=True).exclude(vendor=""):
        if vendor.matches(e.vendor):
            e.vendor_link = vendor
            if e.category_id is None and vendor.default_category_id:
                e.category = vendor.default_category
            e.save(update_fields=["vendor_link", "category", "updated_at"])
            n += 1
    return n


@module_required("expenses")
def expense_vendors(request):
    today = timezone.localdate()
    q = (request.GET.get("q") or "").strip()
    show_inactive = request.GET.get("inactive") == "1"
    vendors = Vendor.objects.select_related("default_category")
    if not show_inactive:
        vendors = vendors.filter(active=True)
    if q:
        vendors = vendors.filter(Q(name__icontains=q) | Q(aliases__icontains=q))
    vendors = list(vendors)
    ytd = {r["vendor_link"]: r for r in
           Expense.objects.filter(vendor_link__in=[v.pk for v in vendors],
                                  date__gte=today.replace(month=1, day=1))
           .values("vendor_link").annotate(t=Sum("amount"), n=Count("id"))}
    alltime = {r["vendor_link"]: r for r in
               Expense.objects.filter(vendor_link__in=[v.pk for v in vendors])
               .values("vendor_link").annotate(t=Sum("amount"), n=Count("id"))}
    rows = [{"vendor": v, "ytd": ytd.get(v.pk, {}).get("t") or ZERO,
             "ytd_count": ytd.get(v.pk, {}).get("n") or 0,
             "total": alltime.get(v.pk, {}).get("t") or ZERO,
             "count": alltime.get(v.pk, {}).get("n") or 0} for v in vendors]
    suggestions = (Expense.objects.filter(vendor_link__isnull=True).exclude(vendor="")
                   .values("vendor").annotate(n=Count("id"), t=Sum("amount"))
                   .order_by("-n", "vendor")[:8])
    return render(request, "expenses/vendor_list.html", {
        "rows": rows, "q": q, "show_inactive": show_inactive, "suggestions": suggestions,
        "year": today.year, "ytd_total": sum((r["ytd"] for r in rows), ZERO),
    })


@module_required("expenses")
def expense_vendor_create(request):
    initial = {"name": request.GET.get("name", "")}
    form = VendorForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            vendor = form.save()
            linked = _link_matching_expenses(vendor)
            log_activity(request.user, "create", vendor)
            msg = f"Vendor {vendor.name} saved."
            if linked:
                msg += f" {linked} existing expense{'s' if linked != 1 else ''} linked to it."
            messages.success(request, msg)
            return redirect("expense_vendor_detail", pk=vendor.pk)
        messages.error(request, "Not saved — fix the highlighted fields.")
    return render(request, "expenses/vendor_form.html", {"form": form, "vendor": None})


@module_required("expenses")
def expense_vendor_edit(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)
    if request.method == "POST" and request.POST.get("action") == "delete":
        vendor.delete(user=request.user)
        log_activity(request.user, "delete", vendor)
        messages.success(request, f"Vendor {vendor.name} deleted. Its expenses keep the vendor name.")
        return redirect("expense_vendors")
    form = VendorForm(request.POST or None, instance=vendor)
    if request.method == "POST":
        if form.is_valid():
            vendor = form.save()
            Expense.objects.filter(vendor_link=vendor).exclude(vendor=vendor.name).update(vendor=vendor.name)
            log_activity(request.user, "update", vendor)
            messages.success(request, f"Vendor {vendor.name} saved.")
            return redirect("expense_vendor_detail", pk=vendor.pk)
        messages.error(request, "Not saved — fix the highlighted fields.")
    return render(request, "expenses/vendor_form.html", {"form": form, "vendor": vendor})


@module_required("expenses")
def expense_vendor_detail(request, pk):
    vendor = get_object_or_404(Vendor.objects.select_related("default_category"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "aliases":
            vendor.aliases = request.POST.get("aliases", "").strip()
            vendor.save(update_fields=["aliases"])
            messages.success(request, "Aliases saved.")
        elif action == "link_matching":
            n = _link_matching_expenses(vendor)
            messages.success(request, f"{n} expense{'s' if n != 1 else ''} linked to {vendor.name}."
                             if n else "No unlinked expenses match this vendor.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("expense_vendor_detail", pk=vendor.pk)

    expenses = list(reports.live_expenses().filter(vendor_link=vendor).order_by("-date", "-id"))
    by_year = {}
    for e in expenses:
        y = by_year.setdefault(e.date.year, {"year": e.date.year, "total": ZERO, "count": 0,
                                             "paid": ZERO, "unpaid": ZERO, "no_receipt": 0})
        y["total"] += e.amount
        y["count"] += 1
        if e.is_paid:
            y["paid"] += e.amount
        else:
            y["unpaid"] += e.amount
        if reports.receipt_missing(e):
            y["no_receipt"] += 1
    unlinked = [e for e in Expense.objects.filter(vendor_link__isnull=True).exclude(vendor="")
                if vendor.matches(e.vendor)]
    bank_txns = (vendor.bank_transactions.filter(is_deleted=False)
                 .select_related("account").order_by("-date", "-id")[:50])
    return render(request, "expenses/vendor_detail.html", {
        "vendor": vendor, "expenses": expenses[:100], "expense_count": len(expenses),
        "by_year": sorted(by_year.values(), key=lambda r: -r["year"]),
        "unlinked_count": len(unlinked), "bank_txns": bank_txns,
        "total": sum((e.amount for e in expenses), ZERO),
    })


# ---------------------------------------------------------------------------
# 5. Categories
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_categories(request):
    today = timezone.localdate()
    add_form = ExpenseCategoryForm(initial={"deductible_pct": 100})
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            add_form = ExpenseCategoryForm(request.POST)
            if add_form.is_valid():
                cat = add_form.save()
                log_activity(request.user, "create", cat)
                messages.success(request, f"Category {cat.name} added (ledger account {cat.account.code}).")
                return redirect("expense_categories")
            messages.error(request, "Category not added — fix the highlighted fields.")
        elif action == "update":
            cat = get_object_or_404(ExpenseCategory, pk=_int(request.POST.get("id"), 0))
            form = ExpenseCategoryForm(request.POST, instance=cat, prefix=f"c{cat.pk}")
            if form.is_valid():
                form.save()
                log_activity(request.user, "update", cat)
                messages.success(request, f"{cat.name} saved.")
            else:
                for field, errs in form.errors.items():
                    for err in errs:
                        messages.error(request, f"{cat.name}: {field} — {err}")
            return redirect("expense_categories")
        elif action == "delete":
            cat = get_object_or_404(ExpenseCategory, pk=_int(request.POST.get("id"), 0))
            in_use = cat.expenses.filter(is_deleted=False).count()
            if in_use:
                messages.error(request, f"{cat.name} is used by {in_use} expense"
                                        f"{'s' if in_use != 1 else ''} and can't be deleted. "
                                        "Move those expenses to another category first.")
            elif RecurringExpense.objects.filter(category=cat).exists():
                messages.error(request, f"{cat.name} is used by a recurring template. Change the "
                                        "template's category first.")
            else:
                cat.delete(user=request.user)
                log_activity(request.user, "delete", cat)
                messages.success(request, f"{cat.name} deleted.")
            return redirect("expense_categories")
        else:
            messages.error(request, "Unknown action.")
            return redirect("expense_categories")

    cats = list(ExpenseCategory.objects.select_related("account"))
    ytd = {r["category"]: r for r in
           Expense.objects.filter(category__in=[c.pk for c in cats], date__gte=today.replace(month=1, day=1))
           .values("category").annotate(t=Sum("amount"))}
    counts = {r["category"]: r["n"] for r in
              Expense.objects.filter(category__in=[c.pk for c in cats]).values("category").annotate(n=Count("id"))}
    recurring_use = set(RecurringExpense.objects.exclude(category__isnull=True)
                        .values_list("category_id", flat=True))
    rows = []
    for c in cats:
        rows.append({"cat": c, "form": ExpenseCategoryForm(instance=c, prefix=f"c{c.pk}"),
                     "ytd": ytd.get(c.pk, {}).get("t") or ZERO, "count": counts.get(c.pk, 0),
                     "in_use": counts.get(c.pk, 0) > 0 or c.pk in recurring_use})
    return render(request, "expenses/categories.html", {
        "rows": rows, "add_form": add_form, "year": today.year,
        "ytd_total": sum((r["ytd"] for r in rows), ZERO),
    })


# ---------------------------------------------------------------------------
# 6. Recurring templates
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_recurring(request):
    today = timezone.localdate()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "generate":
            summary = generate_recurring_expenses(user=request.user)
            messages.success(request, summary)
        elif action == "delete":
            tpl = get_object_or_404(RecurringExpense, pk=_int(request.POST.get("id"), 0))
            tpl.delete(user=request.user)
            log_activity(request.user, "delete", tpl)
            messages.success(request, f"Recurring template {tpl.name} deleted. Expenses it "
                                      "already created are kept.")
        elif action == "toggle":
            tpl = get_object_or_404(RecurringExpense, pk=_int(request.POST.get("id"), 0))
            tpl.active = not tpl.active
            tpl.save(update_fields=["active"])
            messages.success(request, f"{tpl.name} {'resumed' if tpl.active else 'paused'}.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("expense_recurring")
    templates = list(RecurringExpense.objects.select_related("vendor_link", "category", "paid_from")
                     .annotate(generated_count=Count("generated", filter=Q(generated__is_deleted=False))))
    due_now = sum(1 for t in templates if t.active and t.next_date <= today)
    monthly = ZERO
    per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
    for t in templates:
        if t.active:
            monthly += (t.amount * per_year[t.frequency] / 12)
    return render(request, "expenses/recurring_list.html", {
        "templates": templates, "due_now": due_now, "today": today,
        "monthly_estimate": monthly.quantize(Decimal("0.01")),
    })


@module_required("expenses")
def expense_recurring_create(request):
    form = RecurringExpenseForm(request.POST or None, initial={"next_date": timezone.localdate()})
    if request.method == "POST":
        if form.is_valid():
            tpl = form.save()
            log_activity(request.user, "create", tpl)
            messages.success(request, f"Recurring template {tpl.name} saved. First expense on "
                                      f"{tpl.next_date:%b %-d, %Y}.")
            return redirect("expense_recurring")
        messages.error(request, "Not saved — fix the highlighted fields.")
    return render(request, "expenses/recurring_form.html", {"form": form, "template": None})


@module_required("expenses")
def expense_recurring_edit(request, pk):
    tpl = get_object_or_404(RecurringExpense, pk=pk)
    form = RecurringExpenseForm(request.POST or None, instance=tpl)
    if request.method == "POST":
        if form.is_valid():
            tpl = form.save()
            log_activity(request.user, "update", tpl)
            messages.success(request, f"Recurring template {tpl.name} saved.")
            return redirect("expense_recurring")
        messages.error(request, "Not saved — fix the highlighted fields.")
    generated = tpl.generated.filter(is_deleted=False).order_by("-date")[:24]
    return render(request, "expenses/recurring_form.html", {"form": form, "template": tpl,
                                                           "generated": generated})


# ---------------------------------------------------------------------------
# 7. Budgets
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_budgets(request):
    year = _year(request)
    if request.method == "POST":
        year = _int(request.POST.get("year"), year)
        action = request.POST.get("action", "save")
        if action == "fill_defaults":
            created = 0
            have = {b.category_id for b in Budget.objects.filter(year=year, month__isnull=False)}
            for cat in ExpenseCategory.objects.exclude(monthly_budget__isnull=True):
                if cat.pk in have:
                    continue
                for m in range(1, 13):
                    Budget.objects.create(category=cat, year=year, month=m, amount=cat.monthly_budget)
                created += 1
            messages.success(request, f"Filled {year} from default monthly budgets for {created} "
                                      f"categor{'y' if created == 1 else 'ies'}."
                             if created else "Every category with a default already has budgets "
                                             "for this year. Nothing changed.")
        else:
            values, errors = reports.parse_budget_post(year, request.POST)
            for err in errors[:5]:
                messages.error(request, err)
            saved, cleared = reports.upsert_budgets(year, values)
            log_activity(request.user, "budget_save", None, model="Budget",
                         note=f"{year}: {saved} saved, {cleared} cleared")
            messages.success(request, f"Budgets for {year} saved ({saved} changed, {cleared} cleared).")
        return redirect(f"{reverse('expense_budgets')}?year={year}")
    grid = reports.budget_grid(year)
    return render(request, "expenses/budgets.html", {
        "grid": grid, "year": year, "years": _years(),
        "has_defaults": ExpenseCategory.objects.exclude(monthly_budget__isnull=True).exists(),
    })


# ---------------------------------------------------------------------------
# 8. Mileage
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_mileage(request):
    year = _year(request)
    settings = CompanySettings.get()
    form = MileageForm(initial={"date": timezone.localdate()})
    if request.method == "POST":
        action = request.POST.get("action", "add")
        if action == "add":
            form = MileageForm(request.POST)
            if form.is_valid():
                entry = form.save(commit=False)
                entry.rate = settings.mileage_rate
                entry.created_by = request.user
                entry.save()
                log_activity(request.user, "create", entry)
                messages.success(request, f"Logged {entry.total_miles} mi — ${entry.deduction:,.2f} "
                                          f"deduction at ${entry.rate}/mi.")
                return redirect(f"{reverse('expense_mileage')}?year={entry.date.year}")
            messages.error(request, "Not saved — fix the highlighted fields.")
        elif action == "delete":
            entry = get_object_or_404(MileageEntry, pk=_int(request.POST.get("id"), 0))
            entry.delete(user=request.user)
            log_activity(request.user, "delete", entry)
            messages.success(request, "Mileage entry deleted.")
            return redirect(f"{reverse('expense_mileage')}?year={year}")
    summary = reports.mileage_summary(year)
    if request.GET.get("export") == "csv":
        rows = [[m.date, f"{m.miles:.1f}", "yes" if m.round_trip else "no", f"{m.total_miles:.1f}",
                 m.purpose, m.from_location, m.to_location, m.vehicle, f"{m.rate:.3f}",
                 f"{m.deduction:.2f}"] for m in summary["entries"]]
        rows.append(["TOTAL", "", "", f"{summary['total_miles']:.1f}", "", "", "", "", "",
                     f"{summary['total_deduction']:.2f}"])
        return csv_response(f"mileage_{year}.csv",
                            ["Date", "Miles", "Round trip", "Total miles", "Purpose", "From", "To",
                             "Vehicle", "Rate", "Deduction $"], rows)
    return render(request, "expenses/mileage.html", {
        "form": form, "summary": summary, "year": year, "years": _years(),
        "rate": settings.mileage_rate,
    })


# ---------------------------------------------------------------------------
# 9. Reports (shared output)
# ---------------------------------------------------------------------------
def _fmt(cell, export):
    if isinstance(cell, Decimal):
        if export == "csv":
            return f"{cell:.2f}"
        return "—" if cell == 0 else f"{cell:,.2f}"
    if isinstance(cell, date):
        return cell.isoformat() if export == "csv" else cell.strftime("%b %-d, %Y")
    return "" if cell is None else str(cell)


def _report_out(request, *, title, columns, rows, totals=None, links=None, period="",
                filename=None, extra=None):
    export = request.GET.get("export")
    if export == "csv":
        slug = (filename or title).lower().replace(" ", "_").replace("/", "-")
        return csv_response(f"{slug}.csv", columns,
                            [[_fmt(c, "csv") for c in r] for r in rows]
                            + ([[_fmt(c, "csv") for c in totals]] if totals else []))
    if export == "pdf":
        return pdf_response("pdf/report.html", {
            "title": title, "columns": columns,
            "rows": [[_fmt(c, "pdf") for c in r] for r in rows],
            "totals": [_fmt(c, "pdf") for c in totals] if totals else None,
            "company": CompanySettings.get(), "generated": timezone.localtime(), "period": period,
        }, f"{title}.pdf")
    links = links or [None] * len(rows)
    table = [{"cells": [_fmt(c, "html") for c in r], "link": link} for r, link in zip(rows, links)]
    ctx = {"title": title, "columns": columns, "table": table, "row_count": len(rows),
           "totals": [_fmt(c, "html") for c in totals] if totals else None, "period": period}
    base_qs = request.GET.copy()
    base_qs.pop("export", None)
    ctx["export_qs"] = base_qs.urlencode()
    if extra:
        ctx.update(extra)
    return render(request, "expenses/report_table.html", ctx)


@module_required("expenses")
def expense_report_by_month(request):
    year = _year(request)
    basis = ACCRUAL if request.GET.get("basis") == ACCRUAL else CASH
    data = reports.month_matrix(year, basis)
    columns = ["Category"] + data["months"] + ["Total"]
    if data["has_budgets"]:
        columns += ["Budget", "Variance"]
    rows, links = [], []
    for r in data["rows"]:
        row = [r["name"]] + r["cells"] + [r["total"]]
        if data["has_budgets"]:
            row += [r["budget"] if r["budget"] is not None else "",
                    r["variance"] if r["variance"] is not None else ""]
        rows.append(row)
        links.append(f"{reverse('expense_list')}?preset=custom&start={year}-01-01&end={year}-12-31"
                     f"&category={r['category'].pk if r['category'] else 'none'}")
    totals = ["TOTAL"] + data["month_totals"] + [data["grand_total"]]
    if data["has_budgets"]:
        totals += [data["budget_total"], data["variance_total"]]
    basis_help = ("Cash basis: an expense lands in the month the money left (paid date or bank "
                  "transaction date). Unpaid bills are not shown." if basis == CASH else
                  "Accrual basis: an expense lands in the month of its bill date, paid or not.")
    return _report_out(request, title=f"Expenses by month {year} ({basis} basis)",
                       filename=f"expenses_by_month_{year}_{basis}",
                       columns=columns, rows=rows, totals=totals, links=links,
                       period=f"Jan 1 – Dec 31, {year}",
                       extra={"show_year": True, "year": year, "years": _years(),
                              "show_basis": True, "basis": basis, "intro": basis_help,
                              "wide": True, "sticky_first": True,
                              "note": ("Variance = budget − actual; negative (red) means over budget."
                                       if data["has_budgets"] else
                                       "Set budgets to see Budget and Variance columns here.")})


@module_required("expenses")
def expense_report_missing_receipts(request):
    today = timezone.localdate()
    start = _date(request.GET.get("start"), today.replace(month=1, day=1))
    end = _date(request.GET.get("end"), today)
    min_amount = _decimal(request.GET.get("min"), ZERO)
    if min_amount is None or min_amount < 0:
        min_amount = ZERO
    data = reports.missing_receipts(start, end, min_amount)
    rows, links = [], []
    for e in data["rows"]:
        rows.append([e.date, e.vendor or (e.vendor_link.name if e.vendor_link_id else "—"),
                     e.category.name if e.category_id else "Uncategorized", e.amount,
                     e.get_source_display(), str(e.paid_from) if e.paid_from_id else
                     ("Paid personally" if e.paid_personally else "Unpaid" if not e.is_paid else ""),
                     e.note[:60]])
        links.append(reverse("expense_edit", args=[e.pk]))
    totals = ["TOTAL", f"{data['count']} expenses", "", data["total"], "", "", ""]
    return _report_out(request, title="Missing receipts", filename="missing_receipts",
                       columns=["Date", "Vendor", "Category", "Amount $", "Source", "Paid from", "Note"],
                       rows=rows, totals=totals, links=links,
                       period=f"{start:%b %-d, %Y} – {end:%b %-d, %Y}",
                       extra={"show_period": True, "start": start, "end": end,
                              "show_min": True, "min_amount": min_amount,
                              "intro": "Expenses with no receipt photo or PDF attached (on the expense "
                                       "or on its bank transaction). Click a row to add one. Biggest first.",
                              "link_hint": "Open and attach a receipt"})


@module_required("expenses")
def expense_report_unpaid_bills(request):
    as_of = _date(request.GET.get("as_of"), timezone.localdate())
    data = reports.unpaid_bills(as_of)
    rows, links = [], []
    for r in data["rows"]:
        rows.append(["Expense" if r["kind"] == "expense" else "Supplier bill", r["label"], r["who"],
                     r["what"], r["date"], r["due"] if r["due"] else "(none)",
                     max(r["days"], 0) if r["days"] > 0 else "", r["bucket_label"], r["amount"]])
        links.append(reverse("expense_edit", args=[r["pk"]]) if r["kind"] == "expense"
                     else reverse("receipt_detail", args=[r["pk"]]))
    totals = ["TOTAL", f"{len(rows)} open", "", "", "", "", "", "", data["total"]]
    tiles = [{"label": b["label"], "value": b["total"], "count": b["count"],
              "tone": {"current": "slate", "1_30": "amber", "31_60": "orange", "60_plus": "red"}[b["key"]]}
             for b in data["buckets"]]
    return _report_out(request, title="Unpaid bills (AP aging)", filename=f"unpaid_bills_{as_of}",
                       columns=["Type", "Ref", "Vendor / supplier", "What", "Bill date", "Due",
                                "Days late", "Bucket", "Amount due $"],
                       rows=rows, totals=totals, links=links, period=f"as of {as_of:%b %-d, %Y}",
                       extra={"show_as_of": True, "as_of": as_of, "tiles": tiles,
                              "intro": "Everything the business owes: open expenses plus posted supplier "
                                       "receipts not yet paid. Bills without a due date are aged from "
                                       "their bill date. This is your accounts payable.",
                              "summary_pairs": [("Open expenses", data["expense_total"]),
                                                ("Supplier bills", data["bill_total"]),
                                                ("Total owed", data["total"])]})


@module_required("expenses")
def expense_report_1099(request):
    year = _year(request)
    threshold = _decimal(request.GET.get("threshold"), Decimal("600"))
    if threshold is None or threshold < 0:
        threshold = Decimal("600")
    data = reports.vendors_1099(year, threshold)
    rows, links = [], []
    for r in data["all"]:
        v = r["vendor"]
        rows.append([v.name, "yes" if v.is_1099 else "", v.tax_id_last4 or "", r["count"], r["total"],
                     "YES — file 1099-NEC" if r["needs_1099"] else
                     ("under threshold" if v.is_1099 else "")])
        links.append(reverse("expense_vendor_detail", args=[v.pk]))
    totals = ["TOTAL", f"{len(data['qualifying'])} need a 1099", "", "", data["all_total"], ""]
    qualifying_table = [{"cells": [r["vendor"].name, r["vendor"].tax_id_last4 or "missing",
                                   r["vendor"].email or "", f"{r['total']:,.2f}"],
                         "link": reverse("expense_vendor_edit", args=[r["vendor"].pk])}
                        for r in data["qualifying"]]
    return _report_out(request, title=f"1099 vendors {year}", filename=f"vendors_1099_{year}",
                       columns=["Vendor", "1099 contractor", "Tax ID last 4", "Payments",
                                f"Paid in {year} $", "1099 needed?"],
                       rows=rows, totals=totals, links=links, period=f"Jan 1 – Dec 31, {year}",
                       extra={"show_year": True, "year": year, "years": _years(),
                              "show_threshold": True, "threshold": threshold,
                              "intro": f"Contractors flagged as 1099 vendors who were paid at least "
                                       f"${threshold:,.2f} in {year} need a 1099-NEC by Jan 31. Totals "
                                       "use the date money actually left (cash basis). All vendors are "
                                       "listed below for reference — flag any contractor you missed.",
                              "pre_table": {"title": f"Need a 1099-NEC for {year}",
                                            "columns": ["Vendor", "Tax ID last 4", "Email", "Paid $"],
                                            "rows": qualifying_table,
                                            "total": data["qualifying_total"],
                                            "empty": "No 1099 vendor crossed the threshold."}})


# ---------------------------------------------------------------------------
# 10. Year-end package
# ---------------------------------------------------------------------------
@module_required("expenses")
def expense_year_end(request):
    today = timezone.localdate()
    if request.GET.get("year"):
        year = _year(request)
        data = reports.year_end_package(year)
        log_activity(request.user, "year_end_package", None, model="Expense", note=str(year))
        resp = HttpResponse(data, content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="expenses_year_end_{year}.zip"'
        return resp
    default_year = today.year - 1 if today.month <= 4 else today.year
    year_start = date(default_year, 1, 1)
    year_end = date(default_year, 12, 31)
    mr = reports.missing_receipts(year_start, year_end)
    ub = reports.unpaid_bills(year_end)
    unreviewed = Expense.objects.filter(is_reviewed=False, date__year=default_year).count()
    uncategorized = Expense.objects.filter(category__isnull=True, date__year=default_year).count()
    return render(request, "expenses/year_end.html", {
        "default_year": default_year, "years": _years(), "files": reports.YEAR_END_FILES,
        "checks": {"missing_receipts": mr["count"], "missing_total": mr["total"],
                   "unpaid": len(ub["rows"]), "unpaid_total": ub["total"],
                   "unreviewed": unreviewed, "uncategorized": uncategorized},
        "year_start": year_start, "year_end": year_end,
    })
