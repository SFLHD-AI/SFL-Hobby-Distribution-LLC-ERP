"""
Report data for the Expenses module.

Pure builders: they return plain dicts/lists that the views turn into HTML,
CSV or PDF, and that the tests check directly.

Cash vs accrual, in one sentence: an expense counts on the **cash** basis
when the money left (``paid_date``, or the date of its linked bank/card
transaction) and on the **accrual** basis when it was incurred (``date``).
"""
import calendar
import csv
import io
import os
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from accounting.models import ACCRUAL, CASH
from core.models import CompanySettings
from expenses.models import (SCHEDULE_C_LINES, Budget, Expense, ExpenseCategory, MileageEntry,
                             Vendor)
from inventory.models import Receipt

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")
MONTH_ABBR = [calendar.month_abbr[m] for m in range(1, 13)]
SCHEDULE_C = dict(SCHEDULE_C_LINES)

AGING_BUCKETS = [("current", "Not yet due"), ("1_30", "1–30 days late"),
                 ("31_60", "31–60 days late"), ("60_plus", "Over 60 days late")]
AGING_LABELS = dict(AGING_BUCKETS)

# Federal 1099-NEC / 1099-MISC reporting threshold. $600 for payments made
# through Dec 31 2025; $2,000 for payments made after that (tax years 2026+).
THRESHOLD_THROUGH_2025 = Decimal("600")
THRESHOLD_FROM_2026 = Decimal("2000")

# The receipts/ folder in the year-end ZIP is capped so the download stays
# practical to email or upload to a preparer's portal.
RECEIPT_CAP_BYTES = 200 * 1024 * 1024


def default_threshold(year):
    """Default 1099 threshold for a tax year (the owner can override it)."""
    return THRESHOLD_FROM_2026 if int(year) >= 2026 else THRESHOLD_THROUGH_2025


def money(value):
    return (Decimal(value or 0)).quantize(CENTS, ROUND_HALF_UP)


def live_expenses():
    """Every non-deleted expense with the relations the reports read."""
    return (Expense.objects.filter(is_deleted=False)
            .select_related("category", "vendor_link", "paid_from", "bank_txn", "bank_txn__account"))


def basis_date(expense, basis):
    """The date an expense counts on, for the given basis (None = not yet)."""
    if basis == ACCRUAL:
        return expense.date
    return expense.effective_paid_date


def expenses_touching_year(year):
    """Expenses that could count in ``year`` on either basis."""
    return live_expenses().filter(Q(date__year=year) | Q(paid_date__year=year)
                                  | Q(bank_txn__date__year=year)).distinct()


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
def month_budgets(year):
    """{(category_id, month): amount} for the monthly Budget rows of a year."""
    return {(b.category_id, b.month): b.amount
            for b in Budget.objects.filter(year=year, month__isnull=False)}


def annual_budgets(year):
    """{category_id: annual amount} using, in order: the 12 monthly rows,
    a whole-year row, or the category's default monthly budget × 12."""
    monthly = defaultdict(lambda: ZERO)
    has_monthly = set()
    for (cat_id, _m), amt in month_budgets(year).items():
        monthly[cat_id] += amt
        has_monthly.add(cat_id)
    yearly = {b.category_id: b.amount for b in Budget.objects.filter(year=year, month__isnull=True)}
    out = {}
    for cat in ExpenseCategory.objects.all():
        if cat.pk in has_monthly:
            out[cat.pk] = money(monthly[cat.pk])
        elif cat.pk in yearly:
            out[cat.pk] = money(yearly[cat.pk])
        elif cat.monthly_budget is not None:
            out[cat.pk] = money(cat.monthly_budget * 12)
        else:
            out[cat.pk] = None
    return out


def actuals_by_month(year, basis=ACCRUAL):
    """{(category_id or None, month): spend} for one year on one basis."""
    out = defaultdict(lambda: ZERO)
    for e in expenses_touching_year(year):
        d = basis_date(e, basis)
        if d is None or d.year != year:
            continue
        out[(e.category_id, d.month)] += e.amount
    return out


def budget_grid(year, basis=ACCRUAL):
    """Rows for the budget editor: every live category × 12 months, with the
    saved budget, the actual spend, and a variance flag per cell."""
    budgets = month_budgets(year)
    actuals = actuals_by_month(year, basis)
    rows = []
    month_budget_totals = [ZERO] * 12
    month_actual_totals = [ZERO] * 12
    for cat in ExpenseCategory.objects.all():
        cells = []
        b_total = ZERO
        a_total = ZERO
        for m in range(1, 13):
            b = budgets.get((cat.pk, m))
            a = money(actuals.get((cat.pk, m), ZERO))
            if b is not None:
                b_total += b
                month_budget_totals[m - 1] += b
            a_total += a
            month_actual_totals[m - 1] += a
            cells.append({
                "month": m, "label": MONTH_ABBR[m - 1], "name": f"b_{cat.pk}_{m}",
                "budget": b, "actual": a,
                "state": ("none" if b is None else "over" if a > b else "under"),
            })
        rows.append({"category": cat, "cells": cells, "budget_total": money(b_total),
                     "actual_total": money(a_total),
                     "variance": money(b_total - a_total),
                     "default_monthly": cat.monthly_budget})
    budget_total = money(sum(month_budget_totals, ZERO))
    actual_total = money(sum(month_actual_totals, ZERO))
    return {
        "year": year, "basis": basis, "months": MONTH_ABBR, "rows": rows,
        "month_totals": [{"label": MONTH_ABBR[i], "budget": money(month_budget_totals[i]),
                          "actual": money(month_actual_totals[i])} for i in range(12)],
        "month_budget_totals": [money(x) for x in month_budget_totals],
        "month_actual_totals": [money(x) for x in month_actual_totals],
        "budget_total": budget_total, "actual_total": actual_total,
        "variance_total": money(budget_total - actual_total),
    }


def upsert_budgets(year, values):
    """Save a whole grid at once.

    ``values`` is {(category_id, month): Decimal | None}. None/blank clears
    the cell. Returns (saved, cleared)."""
    saved = cleared = 0
    existing = {(b.category_id, b.month): b
                for b in Budget.objects.filter(year=year, month__isnull=False)}
    for (cat_id, month), amount in values.items():
        row = existing.get((cat_id, month))
        if amount is None:
            if row is not None:
                row.delete()
                cleared += 1
            continue
        amount = money(amount)
        if row is None:
            Budget.objects.create(category_id=cat_id, year=year, month=month, amount=amount)
            saved += 1
        elif row.amount != amount:
            row.amount = amount
            row.save(update_fields=["amount"])
            saved += 1
    return saved, cleared


def parse_budget_post(year, post):
    """Turn the grid's POST data (b_<cat>_<month>=amount) into the dict
    ``upsert_budgets`` wants. Bad numbers are reported, not saved."""
    values, errors = {}, []
    cat_ids = set(ExpenseCategory.objects.values_list("pk", flat=True))
    for key, raw in post.items():
        if not key.startswith("b_"):
            continue
        try:
            _, cat_id, month = key.split("_")
            cat_id, month = int(cat_id), int(month)
        except ValueError:
            continue
        if cat_id not in cat_ids or not 1 <= month <= 12:
            continue
        raw = (raw or "").strip().replace(",", "").replace("$", "")
        if raw == "":
            values[(cat_id, month)] = None
            continue
        try:
            amount = Decimal(raw)
        except Exception:
            errors.append(f"'{raw}' is not a number ({MONTH_ABBR[month - 1]}).")
            continue
        if amount < 0:
            errors.append(f"Budgets can't be negative ({MONTH_ABBR[month - 1]}).")
            continue
        values[(cat_id, month)] = amount
    return values, errors


# ---------------------------------------------------------------------------
# Expenses by month (category × month matrix)
# ---------------------------------------------------------------------------
def month_matrix(year, basis=CASH):
    """Category rows × 12 month columns + total, with budget/variance when
    budgets exist. Uncategorized spend is its own row at the bottom."""
    per_cat = defaultdict(lambda: [ZERO] * 12)
    cats = {}
    for e in expenses_touching_year(year):
        d = basis_date(e, basis)
        if d is None or d.year != year:
            continue
        key = e.category_id or 0
        cats[key] = e.category
        per_cat[key][d.month - 1] += e.amount
    budgets = annual_budgets(year)
    # categories with a budget but no spend still get a row
    for cat in ExpenseCategory.objects.all():
        if cat.pk not in per_cat and budgets.get(cat.pk) is not None:
            per_cat[cat.pk] = [ZERO] * 12
            cats[cat.pk] = cat

    rows = []
    for key in sorted(per_cat, key=lambda k: (k == 0, cats[k].name.lower() if cats[k] else "")):
        cells = [money(c) for c in per_cat[key]]
        total = money(sum(cells, ZERO))
        budget = budgets.get(key) if key else None
        rows.append({
            "category": cats[key], "name": cats[key].name if cats[key] else "Uncategorized",
            "cells": cells, "total": total, "budget": budget,
            "variance": money(budget - total) if budget is not None else None,
            "tax_line": SCHEDULE_C.get(cats[key].tax_line, "") if cats[key] else "",
        })
    month_totals = [money(sum((r["cells"][i] for r in rows), ZERO)) for i in range(12)]
    grand = money(sum(month_totals, ZERO))
    has_budgets = any(r["budget"] is not None for r in rows)
    budget_total = money(sum((r["budget"] for r in rows if r["budget"] is not None), ZERO))
    return {
        "year": year, "basis": basis, "months": MONTH_ABBR, "rows": rows,
        "month_totals": month_totals, "grand_total": grand,
        "has_budgets": has_budgets, "budget_total": budget_total if has_budgets else None,
        "variance_total": money(budget_total - sum(
            (r["total"] for r in rows if r["budget"] is not None), ZERO)) if has_budgets else None,
    }


# ---------------------------------------------------------------------------
# Missing receipts
# ---------------------------------------------------------------------------
def receipt_missing(expense):
    """True when neither the expense nor its bank transaction holds a receipt."""
    if expense.has_receipt:
        return False
    txn = expense.bank_transaction
    if txn is not None and txn.receipt_file:
        return False
    return True


def missing_receipts(start, end, min_amount=ZERO):
    """Expenses dated in [start, end] of at least ``min_amount`` with no
    receipt on file. Sorted biggest first so the owner chases what matters."""
    qs = live_expenses().filter(date__gte=start, date__lte=end, amount__gte=min_amount)
    rows = [e for e in qs if receipt_missing(e)]
    rows.sort(key=lambda e: (-e.amount, e.date))
    return {"start": start, "end": end, "min_amount": money(min_amount), "rows": rows,
            "total": money(sum((e.amount for e in rows), ZERO)), "count": len(rows)}


# ---------------------------------------------------------------------------
# Unpaid bills / AP aging
# ---------------------------------------------------------------------------
def aging_bucket(due, as_of):
    """(bucket key, days late). Negative days = not yet due."""
    days = (as_of - due).days
    if days <= 0:
        return "current", days
    if days <= 30:
        return "1_30", days
    if days <= 60:
        return "31_60", days
    return "60_plus", days


def receipt_balance_as_of(receipt, as_of):
    """What was still owed on a supplier receipt at the end of ``as_of``."""
    from banking.models import BankTransaction
    landed = receipt.landed_total
    linked = receipt.supplier_payments.filter(is_deleted=False, date__lte=as_of).exclude(
        status=BankTransaction.IGNORED)
    if linked.exists():
        return landed - sum((abs(t.amount) for t in linked), ZERO)
    if receipt.paid_date is not None and receipt.paid_date <= as_of:
        return ZERO
    return landed


def unpaid_bills(as_of=None):
    """Accounts-payable aging: open expenses + unpaid posted supplier
    receipts as of ``as_of``. Bills without a due date are aged from the
    bill date."""
    as_of = as_of or timezone.localdate()
    rows = []
    for e in live_expenses().filter(date__lte=as_of):
        paid_on = e.effective_paid_date
        if paid_on is not None and paid_on <= as_of:
            continue
        if e.amount <= 0:
            continue
        due = e.due_date or e.date
        bucket, days = aging_bucket(due, as_of)
        rows.append({"kind": "expense", "obj": e, "pk": e.pk, "label": f"EXP-{e.pk}",
                     "who": e.vendor or (e.vendor_link.name if e.vendor_link_id else "—"),
                     "what": e.category.name if e.category_id else "Uncategorized",
                     "date": e.date, "due": e.due_date, "aged_from": due,
                     "amount": money(e.amount), "days": days, "bucket": bucket,
                     "bucket_label": AGING_LABELS[bucket]})
    receipts = (Receipt.objects.filter(is_deleted=False, posted=True, date__lte=as_of)
                .select_related("supplier").prefetch_related("lines"))
    for r in receipts:
        balance = receipt_balance_as_of(r, as_of)
        if balance <= 0:
            continue
        due = r.due_date or r.date
        bucket, days = aging_bucket(due, as_of)
        rows.append({"kind": "bill", "obj": r, "pk": r.pk, "label": f"RCV-{r.pk}",
                     "who": str(r.supplier) if r.supplier_id else "Supplier",
                     "what": "Inventory purchase" + (f" · inv {r.supplier_invoice_number}"
                                                     if r.supplier_invoice_number else ""),
                     "date": r.date, "due": r.due_date, "aged_from": due,
                     "amount": money(balance), "days": days, "bucket": bucket,
                     "bucket_label": AGING_LABELS[bucket]})
    rows.sort(key=lambda r: (r["aged_from"], r["label"]))
    buckets = []
    for key, label in AGING_BUCKETS:
        items = [r for r in rows if r["bucket"] == key]
        buckets.append({"key": key, "label": label, "count": len(items),
                        "total": money(sum((r["amount"] for r in items), ZERO))})
    return {"as_of": as_of, "rows": rows, "buckets": buckets,
            "total": money(sum((r["amount"] for r in rows), ZERO)),
            "expense_total": money(sum((r["amount"] for r in rows if r["kind"] == "expense"), ZERO)),
            "bill_total": money(sum((r["amount"] for r in rows if r["kind"] == "bill"), ZERO))}


# ---------------------------------------------------------------------------
# 1099 vendors
# ---------------------------------------------------------------------------
def vendor_payments(year):
    """{vendor_id: (total paid in year, count)} on the cash basis — a 1099
    reports what you actually paid during the calendar year."""
    totals = defaultdict(lambda: [ZERO, 0])
    qs = live_expenses().filter(vendor_link__isnull=False).filter(
        Q(paid_date__year=year) | Q(bank_txn__date__year=year)).distinct()
    for e in qs:
        d = e.effective_paid_date
        if d is None or d.year != year:
            continue
        totals[e.vendor_link_id][0] += e.amount
        totals[e.vendor_link_id][1] += 1
    return {k: (money(v[0]), v[1]) for k, v in totals.items()}


def vendors_1099(year, threshold=None):
    """Vendors flagged as 1099 contractors paid at least ``threshold`` in
    ``year`` (default: the federal threshold for that year), plus every
    vendor's total for reference."""
    threshold = money(default_threshold(year) if threshold is None else threshold)
    paid = vendor_payments(year)
    vendors = {v.pk: v for v in Vendor.all_objects.filter(pk__in=list(paid))}
    all_rows = []
    for vid, (total, count) in paid.items():
        v = vendors.get(vid)
        if v is None:
            continue
        all_rows.append({"vendor": v, "total": total, "count": count,
                         "needs_1099": v.is_1099 and total >= threshold})
    all_rows.sort(key=lambda r: (-r["total"], r["vendor"].name.lower()))
    qualifying = [r for r in all_rows if r["needs_1099"]]
    flagged_under = [r for r in all_rows if r["vendor"].is_1099 and not r["needs_1099"]]
    return {"year": year, "threshold": threshold, "qualifying": qualifying,
            "flagged_under": flagged_under, "all": all_rows,
            "qualifying_total": money(sum((r["total"] for r in qualifying), ZERO)),
            "all_total": money(sum((r["total"] for r in all_rows), ZERO))}


# ---------------------------------------------------------------------------
# Mileage
# ---------------------------------------------------------------------------
def mileage_summary(year):
    entries = list(MileageEntry.objects.filter(is_deleted=False, date__year=year))
    total_miles = sum((e.total_miles for e in entries), Decimal("0.0"))
    total_deduction = money(sum((e.deduction for e in entries), ZERO))
    return {"year": year, "entries": entries, "total_miles": total_miles,
            "total_deduction": total_deduction, "count": len(entries)}


# ---------------------------------------------------------------------------
# Year-end package (ZIP of CSVs)
# ---------------------------------------------------------------------------
def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    return buf.getvalue().encode("utf-8")


def _yes(flag):
    return "yes" if flag else "no"


def expenses_by_category_rows(year):
    """[(category, schedule C line, deductible %, cash total, accrual total,
    deductible amount)] — the Schedule C worksheet."""
    cash = defaultdict(lambda: ZERO)
    accrual = defaultdict(lambda: ZERO)
    cats = {}
    for e in expenses_touching_year(year):
        cats[e.category_id or 0] = e.category
        d = e.effective_paid_date
        if d is not None and d.year == year:
            cash[e.category_id or 0] += e.amount
        if e.date.year == year:
            accrual[e.category_id or 0] += e.amount
    rows = []
    for key in sorted(set(cash) | set(accrual),
                      key=lambda k: (k == 0, cats[k].name.lower() if cats[k] else "")):
        cat = cats[key]
        pct = cat.deductible_pct if cat else 100
        c_total, a_total = money(cash[key]), money(accrual[key])
        rows.append({
            "category": cat, "name": cat.name if cat else "Uncategorized",
            "tax_line": SCHEDULE_C.get(cat.tax_line, "") if cat else "",
            "deductible_pct": pct, "cash_total": c_total, "accrual_total": a_total,
            "deductible": money(c_total * Decimal(pct) / Decimal(100)),
        })
    return rows


def safe_filename(text, limit=60):
    """Keep letters, digits, dot, dash, underscore; spaces become underscores."""
    text = re.sub(r"[^A-Za-z0-9._ -]+", "", str(text or "")).strip()
    text = re.sub(r"[\s_]+", "_", text)
    return (text[:limit].strip("._ ") or "receipt")


def receipt_files(year, cap_bytes=RECEIPT_CAP_BYTES):
    """Receipts to bundle with the year-end package.

    One entry per expense touching ``year`` that has a receipt on the expense
    itself or on its linked bank/card transaction, named
    ``YYYY-MM-DD_<vendor>_<amount>.<ext>`` (deduped with a counter). Files no
    longer on disk and files that would push the folder past ``cap_bytes``
    are listed in ``skipped`` instead.

    Returns ``{"included": [...], "skipped": [...], "total_bytes": int}``
    where each included row is ``{"arcname", "file", "expense", "size"}`` and
    each skipped row is ``{"arcname", "expense", "reason"}``."""
    included, skipped = [], []
    used = set()
    total = 0
    for e in expenses_touching_year(year).order_by("date", "id"):
        f = e.receipt_file if e.receipt_file else None
        if not f:
            txn = e.bank_transaction
            if txn is not None and txn.receipt_file:
                f = txn.receipt_file
        if not f:
            continue
        ext = os.path.splitext(f.name)[1].lower()[:10]
        who = e.vendor or (e.vendor_link.name if e.vendor_link_id else "") or "no_vendor"
        base = f"{e.date:%Y-%m-%d}_{safe_filename(who)}_{abs(e.amount):.2f}"
        name, n = base + ext, 2
        while name in used:
            name = f"{base}_{n}{ext}"
            n += 1
        try:
            exists = f.storage.exists(f.name)
            size = f.storage.size(f.name) if exists else 0
        except Exception:                      # unreadable storage entry
            exists, size = False, 0
        if not exists:
            skipped.append({"arcname": name, "expense": e,
                            "reason": "the file is missing from the server"})
            continue
        if total + size > cap_bytes:
            skipped.append({"arcname": name, "expense": e,
                            "reason": f"left out to keep the folder under "
                                      f"{cap_bytes // (1024 * 1024)} MB"})
            continue
        used.add(name)
        total += size
        included.append({"arcname": f"receipts/{name}", "file": f, "expense": e, "size": size})
    return {"included": included, "skipped": skipped, "total_bytes": total}


def year_end_package(year, today=None, receipt_cap=RECEIPT_CAP_BYTES):
    """Build the ZIP (bytes) the owner hands to the tax preparer.

    For a year still in progress the "unpaid bills" file is as of ``today``
    (there is no Dec 31 yet)."""
    settings = CompanySettings.get()
    today = today or timezone.localdate()
    year_end = date(year, 12, 31)
    year_start = date(year, 1, 1)
    bills_as_of = min(year_end, today)
    in_progress = bills_as_of < year_end
    files = {}
    receipts = receipt_files(year, receipt_cap)
    receipt_name = {r["expense"].pk: r["arcname"] for r in receipts["included"]}

    # 1. Schedule C worksheet by category
    by_cat = expenses_by_category_rows(year)
    rows = [[r["name"], r["tax_line"], r["deductible_pct"], f"{r['cash_total']:.2f}",
             f"{r['accrual_total']:.2f}", f"{r['deductible']:.2f}"] for r in by_cat]
    rows.append(["TOTAL", "", "",
                 f"{money(sum((r['cash_total'] for r in by_cat), ZERO)):.2f}",
                 f"{money(sum((r['accrual_total'] for r in by_cat), ZERO)):.2f}",
                 f"{money(sum((r['deductible'] for r in by_cat), ZERO)):.2f}"])
    files["expenses_by_category.csv"] = _csv_bytes(
        ["Category", "Schedule C line", "Deductible %", "Paid in year (cash basis) $",
         "Incurred in year (accrual basis) $", "Deductible amount (cash basis) $"], rows)

    # 2. Every expense that touches the year
    rows = []
    for e in expenses_touching_year(year).order_by("date", "id"):
        paid_on = e.effective_paid_date
        txn = e.bank_transaction
        rows.append([
            e.pk, e.date.isoformat(), paid_on.isoformat() if paid_on else "",
            _yes(e.is_paid), e.category.name if e.category_id else "Uncategorized",
            SCHEDULE_C.get(e.category.tax_line, "") if e.category_id else "",
            e.vendor or (e.vendor_link.name if e.vendor_link_id else ""),
            f"{e.amount:.2f}", e.reference, e.note, e.due_date.isoformat() if e.due_date else "",
            str(e.paid_from) if e.paid_from_id else ("Paid personally" if e.paid_personally else ""),
            e.get_payment_method_display() if e.payment_method else "",
            e.get_source_display(), _yes(not receipt_missing(e)), _yes(e.is_reviewed),
            _yes(paid_on is not None and paid_on.year == year), _yes(e.date.year == year),
            txn.pk if txn is not None else "", receipt_name.get(e.pk, ""),
        ])
    files["all_expenses.csv"] = _csv_bytes(
        ["ID", "Date incurred", "Paid date", "Paid", "Category", "Schedule C line", "Vendor",
         "Amount $", "Reference", "Note", "Due date", "Paid from", "Payment method", "Source",
         "Receipt on file", "Reviewed", "Counts in cash-basis year", "Counts in accrual-basis year",
         "Bank transaction ID", "Receipt file in this package"], rows)

    # 3. 1099 vendors
    data = vendors_1099(year)
    rows = [[r["vendor"].name, _yes(r["vendor"].is_1099), r["vendor"].tax_id_last4,
             r["vendor"].email, r["count"], f"{r['total']:.2f}", _yes(r["needs_1099"])]
            for r in data["all"]]
    files["vendors_1099.csv"] = _csv_bytes(
        ["Vendor", "Flagged as 1099 contractor", "Tax ID last 4", "Email", "Payments",
         f"Paid in {year} $", f"Needs 1099-NEC (>= ${data['threshold']:.2f})"], rows)

    # 4. Mileage
    ms = mileage_summary(year)
    rows = [[m.date.isoformat(), f"{m.miles:.1f}", _yes(m.round_trip), f"{m.total_miles:.1f}",
             m.purpose, m.from_location, m.to_location, m.vehicle, f"{m.rate:.3f}",
             f"{m.deduction:.2f}"] for m in sorted(ms["entries"], key=lambda m: (m.date, m.pk))]
    rows.append(["TOTAL", "", "", f"{ms['total_miles']:.1f}", "", "", "", "", "",
                 f"{ms['total_deduction']:.2f}"])
    files["mileage.csv"] = _csv_bytes(
        ["Date", "Miles (one way)", "Round trip", "Total miles", "Purpose", "From", "To",
         "Vehicle", "Rate $/mile", "Deduction $"], rows)

    # 5. Missing receipts
    mr = missing_receipts(year_start, year_end)
    rows = [[e.pk, e.date.isoformat(), e.vendor, e.category.name if e.category_id else "Uncategorized",
             f"{e.amount:.2f}", e.get_source_display(), str(e.paid_from) if e.paid_from_id else ""]
            for e in mr["rows"]]
    files["missing_receipts.csv"] = _csv_bytes(
        ["ID", "Date", "Vendor", "Category", "Amount $", "Source", "Paid from"], rows)

    # 6. Unpaid bills at year end (or today, for a year still in progress)
    ub = unpaid_bills(bills_as_of)
    rows = [[r["kind"], r["label"], r["who"], r["what"], r["date"].isoformat(),
             r["due"].isoformat() if r["due"] else "", f"{r['amount']:.2f}",
             max(r["days"], 0), r["bucket_label"]] for r in ub["rows"]]
    rows.append(["TOTAL", "", "", "", "", "", f"{ub['total']:.2f}", "", ""])
    files["unpaid_bills_at_year_end.csv"] = _csv_bytes(
        ["Type", "ID", "Vendor / supplier", "What", "Bill date", "Due date", "Amount due $",
         "Days past due", "How late"], rows)

    # 7. README
    bills_line = (f"Bills still open on {bills_as_of:%b %-d, %Y} (the year is still in progress)."
                  if in_progress else f"Bills still open on Dec 31, {year}.")
    n_receipts = len(receipts["included"])
    mb = receipts["total_bytes"] / (1024 * 1024)
    receipts_block = (f"  receipts/                      {n_receipts} receipt photo{'s' if n_receipts != 1 else ''}"
                      f"/PDF{'s' if n_receipts != 1 else ''} ({mb:.1f} MB), named "
                      "date_vendor_amount.\n")
    skipped_block = ""
    if receipts["skipped"]:
        lines = [f"  {r['arcname']}  (expense #{r['expense'].pk}): {r['reason']}"
                 for r in receipts["skipped"]]
        skipped_block = ("\nRECEIPTS NOT INCLUDED\n" + "\n".join(lines) + "\n")
    readme = f"""{settings.company_name} — expense package for tax year {year}
Generated {timezone.localtime():%Y-%m-%d %H:%M}

WHAT'S INSIDE
  expenses_by_category.csv       Totals per category with the Schedule C line and deductible %.
  all_expenses.csv               Every expense that touches {year}, one row each, with the name of
                                 its receipt file in this package (if any).
  vendors_1099.csv               Who you paid, with the 1099 contractors that crossed the threshold
                                 (default for {year}: ${data['threshold']:,.0f} — confirm with your preparer).
  mileage.csv                    Business miles and the standard-rate deduction.
  missing_receipts.csv           Expenses dated in {year} with no receipt on file.
  unpaid_bills_at_year_end.csv   {bills_line}
{receipts_block}{skipped_block}
CASH VS ACCRUAL COLUMNS
  "Paid in year (cash basis)" counts an expense in the year the money left:
  its paid date, or the date of the bank/card transaction it came from. This
  is the number a cash-basis tax return uses.

  "Incurred in year (accrual basis)" counts an expense in the year of its
  bill / receipt date, whether or not it was paid. Use it to see what the
  business really cost to run; bills still open at Dec 31 are listed in
  unpaid_bills_at_year_end.csv.

  An expense incurred in December and paid in January appears in {year}'s
  accrual column and in {year + 1}'s cash column. The "Counts in ..." columns
  in all_expenses.csv show which applies to each row.

  "Deductible amount" = cash-basis total x deductible % (meals are usually 50%).
  Mileage is deducted at the IRS standard rate (${settings.mileage_rate}/mile is the
  current setting; each entry stores the rate that applied when it was logged).
  Mileage is not a cash movement and is not included in the expense totals.

  Refunds and vendor credits are negative amounts and reduce the totals.
"""
    files["README.txt"] = readme.encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        for r in receipts["included"]:
            try:
                with r["file"].open("rb") as src, zf.open(r["arcname"], "w") as dst:
                    shutil.copyfileobj(src, dst)
            except OSError:
                # vanished between the existence check and the copy
                zf.writestr(r["arcname"] + ".missing.txt",
                            f"Receipt for expense #{r['expense'].pk} could not be read.")
    return buf.getvalue()


YEAR_END_FILES = ["expenses_by_category.csv", "all_expenses.csv", "vendors_1099.csv",
                  "mileage.csv", "missing_receipts.csv", "unpaid_bills_at_year_end.csv",
                  "README.txt"]
YEAR_END_FOLDERS = ["receipts/"]
