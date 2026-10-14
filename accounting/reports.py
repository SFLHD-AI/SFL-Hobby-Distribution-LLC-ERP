"""
Helpers shared by the accounting views: request parsing (basis, period,
as-of date), period presets, plain-English explanations for the cash vs.
accrual bridge, CSV row builders for every statement and the year-end /
month-end ZIP package.

Nothing here touches the database model layer; it only reads
``CompanySettings`` and calls ``accounting.ledger``.
"""
import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.ledger import (Journal, compare_bases, fiscal_year_start, month_end)
from accounting.models import ACCRUAL, CASH, AccountType

ZERO = Decimal("0.00")

BASIS_LABELS = {CASH: "Cash basis", ACCRUAL: "Accrual basis"}
BASIS_HELP = {
    CASH: "Cash basis counts money when it moves in or out of your bank and card "
          "accounts. This is what your tax return uses.",
    ACCRUAL: "Accrual basis counts a sale when you invoice it and a cost when you "
             "incur it, and treats inventory as an asset until it sells. This is the "
             "true picture of whether you are building equity.",
}

# Plain-words explanation for every bridge row produced by ledger.compare_bases.
BRIDGE_EXPLANATIONS = {
    "Invoiced but not yet collected":
        "You sent invoices for this amount that customers have not paid yet. Accrual "
        "counts it as income now; cash basis will count it when the money arrives.",
    "Customer payments not yet applied to an invoice":
        "Customers paid you money that is not matched to an invoice yet. Cash basis "
        "treats it as income already; accrual holds it as a deposit until an invoice "
        "uses it up.",
    "Inventory bought but not yet sold":
        "Product you bought is still on the shelf. Cash basis wrote it off as a cost "
        "when you paid for it; accrual keeps it as an asset until it sells, so accrual "
        "profit is higher by this amount.",
    "Paid to suppliers ahead of delivery":
        "Money you paid suppliers for product you have not received yet (allocations, "
        "pre-orders). Cash basis counts it as a cost now; accrual treats it as money "
        "owed back to you until the product arrives.",
    "Supplier bills received but not yet paid":
        "Bills and supplier invoices you have recorded but not paid. Accrual counts "
        "the cost now; cash basis waits until the payment leaves your account.",
    "Shipping charges not yet paid":
        "Shipping labels and freight recorded on shipments that have not hit your card "
        "yet. Accrual counts the cost when you ship; cash basis when the carrier charges "
        "you.",
    "Sales tax collected but not yet sent to the state":
        "Sales tax you collected from customers belongs to the state and is never income "
        "on either basis. This row only moves when the two bases recognise a taxable "
        "invoice in different periods.",
}


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
def default_basis():
    from core.models import CompanySettings
    basis = CompanySettings.get().accounting_basis_default
    return basis if basis in (CASH, ACCRUAL) else CASH


def basis_from_request(request):
    """``?basis=cash|accrual`` with the company default as fallback."""
    basis = (request.GET.get("basis") or "").lower()
    if basis in (CASH, ACCRUAL):
        return basis
    return default_basis()


def other_basis(basis):
    return ACCRUAL if basis == CASH else CASH


def parse_date(value, fallback=None):
    """ISO date string -> date, or ``fallback`` when blank/invalid."""
    if not value:
        return fallback
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return fallback


def fiscal_start_month():
    from core.models import CompanySettings
    return CompanySettings.get().fiscal_year_start_month or 1


def fiscal_year_bounds(start_year, start_month=None):
    """(first day, last day) of the fiscal year that *starts* in ``start_year``."""
    start_month = start_month or fiscal_start_month()
    start = date(start_year, start_month, 1)
    end = date(start_year + 1, start_month, 1) - timedelta(days=1)
    return start, end


def fiscal_year_label(start, end):
    """'FY 2025' when the fiscal year is a calendar year, otherwise
    'FY 2025–26' so the owner can see it spans two calendar years."""
    if start.year == end.year:
        return f"FY {start.year}"
    return f"FY {start.year}–{str(end.year)[-2:]}"


def last_complete_fiscal_year(today=None, start_month=None):
    """Start year of the most recent fiscal year that has fully ended."""
    today = today or timezone.localdate()
    start_month = start_month or fiscal_start_month()
    current_start = fiscal_year_start(today, start_month)
    return current_start.year - 1


def quarter_bounds(day):
    q_start_month = 3 * ((day.month - 1) // 3) + 1
    start = day.replace(month=q_start_month, day=1)
    end = month_end(start.replace(month=q_start_month + 2))
    return start, end


def period_presets(today=None, start_month=None):
    """Ordered list of (key, label, start, end) for the quick-period links."""
    today = today or timezone.localdate()
    start_month = start_month or fiscal_start_month()
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    q_start, _ = quarter_bounds(today)
    fy_start = fiscal_year_start(today, start_month)
    prev_fy_start, prev_fy_end = fiscal_year_bounds(fy_start.year - 1, start_month)
    return [
        ("this_month", "This month", this_month_start, today),
        ("last_month", "Last month", last_month_start, last_month_end),
        ("this_quarter", "This quarter", q_start, today),
        ("ytd", "Year to date", fy_start, today),
        ("last_year", "Last year", prev_fy_start, prev_fy_end),
    ]


def period_from_request(request, default="this_month"):
    """Return (start, end).

    Accepts ``?preset=<key>`` or explicit ``?start=&end=`` (ISO dates).
    Missing/invalid values fall back to the named default preset. ``end``
    is clamped so it is never before ``start``."""
    today = timezone.localdate()
    presets = {k: (s, e) for k, _, s, e in period_presets(today)}
    d_start, d_end = presets.get(default, presets["this_month"])
    preset = request.GET.get("preset")
    if preset in presets and not (request.GET.get("start") or request.GET.get("end")):
        d_start, d_end = presets[preset]
    start = parse_date(request.GET.get("start"), d_start)
    end = parse_date(request.GET.get("end"), d_end)
    if end < start:
        start, end = end, start
    return start, end


def as_of_from_request(request):
    today = timezone.localdate()
    return parse_date(request.GET.get("as_of"), today)


def active_preset(start, end, today=None):
    """Which preset (if any) the current range matches — for highlighting."""
    for key, _, s, e in period_presets(today):
        if s == start and e == end:
            return key
    return ""


def period_label(start, end):
    if start == end:
        return start.strftime("%b %-d, %Y")
    if start.year == end.year:
        return f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    return f"{start.strftime('%b %-d, %Y')} – {end.strftime('%b %-d, %Y')}"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def money(value):
    """Decimal -> '1234.56' for CSV (no thousands separator, 2 dp)."""
    if value is None:
        return ""
    return f"{Decimal(value):.2f}"


def pct(value):
    return "" if value is None else f"{Decimal(value):.1f}%"


# ---------------------------------------------------------------------------
# CSV row builders (used by the views' exports AND the ZIP packages)
# ---------------------------------------------------------------------------
PNL_HEADER = ["Section", "Code", "Account", "Amount"]
BS_HEADER = ["Section", "Code", "Account", "Amount"]
COMPARE_HEADER = ["Section", "Code", "Account", "Cash", "Accrual", "Difference"]
TB_HEADER = ["Code", "Account", "Type", "Debit", "Credit"]
JOURNAL_HEADER = ["Date", "Memo", "Source type", "Source id", "Code", "Account", "Debit", "Credit"]
GL_HEADER = ["Date", "Memo", "Source type", "Source id", "Debit", "Credit", "Balance"]
EQUITY_HEADER = ["Month", "Assets", "Liabilities", "Equity", "Net income", "Change in equity"]


def pnl_rows(stmt):
    rows = []
    for r in stmt["income"]:
        rows.append(["Income", r["account"].code, r["account"].name, money(r["amount"])])
    rows.append(["Income", "", "Total income", money(stmt["total_income"])])
    for r in stmt["cogs"]:
        rows.append(["Cost of goods sold", r["account"].code, r["account"].name, money(r["amount"])])
    rows.append(["Cost of goods sold", "", "Total cost of goods sold", money(stmt["total_cogs"])])
    rows.append(["Gross profit", "", "Gross profit", money(stmt["gross_profit"])])
    rows.append(["Gross profit", "", "Gross margin %", pct(stmt["gross_margin_pct"])])
    for r in stmt["expenses"]:
        rows.append(["Expenses", r["account"].code, r["account"].name, money(r["amount"])])
    rows.append(["Expenses", "", "Total expenses", money(stmt["total_expenses"])])
    rows.append(["Net income", "", "Net income", money(stmt["net_income"])])
    return rows


def _bs_row_label(r):
    return r["account"].name if r.get("account") else r.get("label", "")


def _bs_row_code(r):
    return r["account"].code if r.get("account") else ""


def balance_sheet_rows(bs):
    rows = []
    for r in bs["assets"]:
        rows.append(["Assets", _bs_row_code(r), _bs_row_label(r), money(r["amount"])])
    rows.append(["Assets", "", "Total assets", money(bs["total_assets"])])
    for r in bs["liabilities"]:
        rows.append(["Liabilities", _bs_row_code(r), _bs_row_label(r), money(r["amount"])])
    rows.append(["Liabilities", "", "Total liabilities", money(bs["total_liabilities"])])
    for r in bs["equity"]:
        rows.append(["Equity", _bs_row_code(r), _bs_row_label(r), money(r["amount"])])
    rows.append(["Equity", "", "Total equity", money(bs["total_equity"])])
    rows.append(["Check", "", "Total liabilities + equity",
                 money(bs["total_liabilities"] + bs["total_equity"])])
    rows.append(["Check", "", "Balanced", "yes" if bs["balanced"] else f"NO (off by {money(bs['difference'])})"])
    return rows


def compare_sections(cmp):
    """Group compare_bases()['rows'] by statement section, with section totals.

    Returns list of dicts: {key, heading, rows, cash, accrual, difference}."""
    order = [(AccountType.INCOME, "income", "Income"),
             (AccountType.COGS, "cogs", "Cost of goods sold"),
             (AccountType.EXPENSE, "expense", "Expenses")]
    out = []
    for type_, key, heading in order:
        rows = [r for r in cmp["rows"] if r["account"].type == type_]
        c = sum((r["cash"] for r in rows), ZERO)
        a = sum((r["accrual"] for r in rows), ZERO)
        out.append({"key": key, "heading": heading, "rows": rows,
                    "cash": c, "accrual": a, "difference": a - c})
    return out


def compare_rows(cmp):
    rows = []
    for sec in compare_sections(cmp):
        for r in sec["rows"]:
            rows.append([sec["heading"], r["account"].code, r["account"].name,
                         money(r["cash"]), money(r["accrual"]), money(r["difference"])])
        rows.append([sec["heading"], "", f"Total {sec['heading'].lower()}",
                     money(sec["cash"]), money(sec["accrual"]), money(sec["difference"])])
        if sec["key"] == "cogs":
            c, a = cmp["cash"], cmp["accrual"]
            rows.append(["Gross profit", "", "Gross profit", money(c["gross_profit"]),
                         money(a["gross_profit"]), money(a["gross_profit"] - c["gross_profit"])])
    c, a = cmp["cash"], cmp["accrual"]
    rows.append(["Net income", "", "Net income", money(c["net_income"]),
                 money(a["net_income"]), money(cmp["net_difference"])])
    for label, amount in cmp["bridge"]:
        rows.append(["Bridge (why they differ)", "", label, "", "", money(amount)])
    rows.append(["Bridge (why they differ)", "", "Sum of bridge rows", "", "",
                 money(sum((amt for _, amt in cmp["bridge"]), ZERO))])
    return rows


def bridge_rows(cmp):
    """[{label, amount, explanation}] for the compare page."""
    out = []
    for label, amount in cmp["bridge"]:
        out.append({"label": label, "amount": amount,
                    "explanation": BRIDGE_EXPLANATIONS.get(
                        label, "A timing difference between when cash moved and when the "
                               "sale or cost belongs.")})
    return out


def trial_balance_rows(tb):
    rows = [[r["account"].code, r["account"].name, r["account"].get_type_display(),
             money(r["debit"]), money(r["credit"])] for r in tb["rows"]]
    rows.append(["", "TOTAL", "", money(tb["total_debit"]), money(tb["total_credit"])])
    return rows


def journal_csv_rows(rows):
    return [[r["date"].isoformat(), r["memo"], r["source_type"], r["source_id"],
             r["account"].code, r["account"].name, money(r["debit"]), money(r["credit"])]
            for r in rows]


def gl_csv_rows(gl):
    rows = [[gl["start"].isoformat(), "Opening balance", "", "", "", "", money(gl["opening_balance"])]]
    for r in gl["rows"]:
        rows.append([r["date"].isoformat(), r["memo"], r["source_type"], r["source_id"],
                     money(r["debit"]), money(r["credit"]), money(r["balance"])])
    rows.append([gl["end"].isoformat(), "Closing balance", "", "", "", "", money(gl["closing_balance"])])
    return rows


def equity_csv_rows(rows):
    return [[r["label"], money(r["assets"]), money(r["liabilities"]), money(r["equity"]),
             money(r["net_income"]), money(r["equity_change"])] for r in rows]


# ---------------------------------------------------------------------------
# PDF section builders (templates/pdf/financial_statement.html)
# ---------------------------------------------------------------------------
def pnl_pdf_sections(stmt):
    def rows(items):
        return [{"label": r["account"].name, "code": r["account"].code, "amounts": [r["amount"]]}
                for r in items]
    return [
        {"heading": "Income", "rows": rows(stmt["income"]),
         "total_label": "Total income", "totals": [stmt["total_income"]]},
        {"heading": "Cost of goods sold", "rows": rows(stmt["cogs"]),
         "total_label": "Total cost of goods sold", "totals": [stmt["total_cogs"]]},
        {"heading": None, "rows": [], "total_label": "Gross profit",
         "totals": [stmt["gross_profit"]], "emphasis": True,
         "note": f"Gross margin {pct(stmt['gross_margin_pct'])}" if stmt["gross_margin_pct"] is not None else ""},
        {"heading": "Expenses", "rows": rows(stmt["expenses"]),
         "total_label": "Total expenses", "totals": [stmt["total_expenses"]]},
        {"heading": None, "rows": [], "total_label": "Net income",
         "totals": [stmt["net_income"]], "emphasis": True},
    ]


def balance_sheet_pdf_sections(bs):
    def rows(items):
        return [{"label": _bs_row_label(r), "code": _bs_row_code(r), "amounts": [r["amount"]],
                 "computed": r.get("computed", False)} for r in items]
    return [
        {"heading": "Assets", "rows": rows(bs["assets"]),
         "total_label": "Total assets", "totals": [bs["total_assets"]], "emphasis": True},
        {"heading": "Liabilities", "rows": rows(bs["liabilities"]),
         "total_label": "Total liabilities", "totals": [bs["total_liabilities"]]},
        {"heading": "Equity", "rows": rows(bs["equity"]),
         "total_label": "Total equity", "totals": [bs["total_equity"]]},
        {"heading": None, "rows": [], "total_label": "Total liabilities and equity",
         "totals": [bs["total_liabilities"] + bs["total_equity"]], "emphasis": True},
    ]


def compare_pdf_sections(cmp):
    sections = []
    for sec in compare_sections(cmp):
        sections.append({
            "heading": sec["heading"],
            "rows": [{"label": r["account"].name, "code": r["account"].code,
                      "amounts": [r["cash"], r["accrual"], r["difference"]]} for r in sec["rows"]],
            "total_label": f"Total {sec['heading'].lower()}",
            "totals": [sec["cash"], sec["accrual"], sec["difference"]]})
        if sec["key"] == "cogs":
            c, a = cmp["cash"], cmp["accrual"]
            sections.append({"heading": None, "rows": [], "total_label": "Gross profit",
                             "totals": [c["gross_profit"], a["gross_profit"],
                                        a["gross_profit"] - c["gross_profit"]], "emphasis": True})
    c, a = cmp["cash"], cmp["accrual"]
    sections.append({"heading": None, "rows": [], "total_label": "Net income",
                     "totals": [c["net_income"], a["net_income"], cmp["net_difference"]],
                     "emphasis": True})
    sections.append({
        "heading": "Why they differ (bridge from cash to accrual net income)",
        "rows": [{"label": label, "code": "", "amounts": [None, None, amount]}
                 for label, amount in cmp["bridge"]],
        "total_label": "Sum of bridge rows",
        "totals": [None, None, sum((amt for _, amt in cmp["bridge"]), ZERO)]})
    return sections


def trial_balance_pdf_sections(tb):
    return [{
        "heading": None,
        "rows": [{"label": r["account"].name, "code": r["account"].code,
                  "amounts": [r["debit"], r["credit"]]} for r in tb["rows"]],
        "total_label": "Totals", "totals": [tb["total_debit"], tb["total_credit"]],
        "emphasis": True}]


# ---------------------------------------------------------------------------
# Year-end / month-end package
# ---------------------------------------------------------------------------
PACKAGE_FILES = [
    ("pnl_cash.csv", "Income statement (profit & loss), cash basis — matches what goes on the tax return."),
    ("pnl_accrual.csv", "Income statement, accrual basis — the economic picture for the period."),
    ("balance_sheet_cash.csv", "Balance sheet at the last day of the period, cash basis (no AR, AP or inventory)."),
    ("balance_sheet_accrual.csv", "Balance sheet at the last day of the period, accrual basis (AR, inventory, AP included)."),
    ("cash_vs_accrual.csv", "Side-by-side income statement on both bases with the bridge that explains the difference."),
    ("trial_balance_accrual.csv", "Every account's debit or credit balance at period end, accrual basis."),
    ("general_journal_accrual.csv", "Every journal line in the period, accrual basis, with the source document."),
    ("general_journal_cash.csv", "Every journal line in the period, cash basis."),
]


def _csv_bytes(header, rows):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def package_readme(label, start, end, company_name, generated):
    lines = [
        f"{company_name} — financial package: {label}",
        f"Period: {start.isoformat()} to {end.isoformat()}",
        f"Generated: {generated:%Y-%m-%d %H:%M} from SFL ERP System",
        "",
        "This company files taxes on the CASH basis. Files marked 'cash' are what the",
        "tax return should be built from. Files marked 'accrual' show the true economic",
        "picture (invoices as income when sent, inventory as an asset until sold, bills",
        "as costs when received) and are for understanding whether equity is growing.",
        "",
        "Every number is rebuilt from the underlying documents (invoices, payments,",
        "supplier receipts, bank/card transactions, expenses, stock movements). Nothing",
        "is posted by hand, so the statements always agree with the source documents.",
        "",
        "Amounts are in US dollars with two decimals and no thousands separators.",
        "Income, liabilities and equity are positive when they are what you would expect",
        "(money earned / money owed / your stake). Negative numbers are shown with a",
        "leading minus sign.",
        "",
        "Files in this package:",
    ]
    for name, desc in PACKAGE_FILES:
        lines.append(f"  {name:<30} {desc}")
    lines += ["", "Open any CSV in Excel, Google Sheets or QuickBooks.", ""]
    return "\n".join(lines)


def build_package_zip(start, end, label):
    """Return ZIP bytes for the period [start, end] with every statement on
    both bases plus a README. Built in memory."""
    from core.models import CompanySettings
    company = CompanySettings.get()
    generated = timezone.localtime()
    checkpoints = [start - timedelta(days=1), end]
    cash_j = Journal(CASH, checkpoints)
    acc_j = Journal(ACCRUAL, checkpoints)

    files = {
        "pnl_cash.csv": _csv_bytes(PNL_HEADER, pnl_rows(cash_j.income_statement(start, end))),
        "pnl_accrual.csv": _csv_bytes(PNL_HEADER, pnl_rows(acc_j.income_statement(start, end))),
        "balance_sheet_cash.csv": _csv_bytes(BS_HEADER, balance_sheet_rows(cash_j.balance_sheet(end))),
        "balance_sheet_accrual.csv": _csv_bytes(BS_HEADER, balance_sheet_rows(acc_j.balance_sheet(end))),
        "cash_vs_accrual.csv": _csv_bytes(COMPARE_HEADER, compare_rows(compare_bases(start, end))),
        "trial_balance_accrual.csv": _csv_bytes(TB_HEADER, trial_balance_rows(acc_j.trial_balance(end))),
        "general_journal_accrual.csv": _csv_bytes(JOURNAL_HEADER, journal_csv_rows(acc_j.journal_rows(start, end))),
        "general_journal_cash.csv": _csv_bytes(JOURNAL_HEADER, journal_csv_rows(cash_j.journal_rows(start, end))),
        "README.txt": package_readme(label, start, end, company.company_name, generated).encode("utf-8"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, _ in PACKAGE_FILES:
            zf.writestr(name, files[name])
        zf.writestr("README.txt", files["README.txt"])
    return buf.getvalue()


def parse_month(value):
    """'YYYY-MM' -> (first day, last day) or None."""
    if not value:
        return None
    try:
        year, month = value.strip().split("-")[:2]
        start = date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return None
    return start, month_end(start)


# ---------------------------------------------------------------------------
# Inline SVG chart geometry for the equity trend page (no JS libraries)
# ---------------------------------------------------------------------------
def _nice_step(span, target_ticks=4):
    """Round the raw tick spacing to 1/2/2.5/5 × 10^n so labels read cleanly."""
    if span <= 0:
        return Decimal("1")
    raw = Decimal(span) / target_ticks
    exp = raw.adjusted()           # power of ten of the leading digit
    base = Decimal(10) ** exp
    for mult in (Decimal("1"), Decimal("2"), Decimal("2.5"), Decimal("5"), Decimal("10")):
        if base * mult >= raw:
            return base * mult
    return base * 10


def equity_chart(rows, width=640, height=260):
    """Geometry for a single-series bar chart of month-end equity.

    Returns None when there is nothing to draw. All coordinates are plain
    floats in a fixed viewBox so the SVG scales to any width."""
    if not rows:
        return None
    pad_left, pad_right, pad_top, pad_bottom = 64, 12, 16, 36
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    values = [r["equity"] for r in rows]
    lo = min(min(values), ZERO)
    hi = max(max(values), ZERO)
    if lo == hi:
        hi = lo + Decimal("100")
    step = _nice_step(hi - lo)
    # extend to tick boundaries
    lo_t = (lo / step).to_integral_value(rounding="ROUND_FLOOR") * step
    hi_t = (hi / step).to_integral_value(rounding="ROUND_CEILING") * step
    span = hi_t - lo_t

    def y_for(v):
        return pad_top + float((hi_t - Decimal(v)) / span) * plot_h

    ticks = []
    t = lo_t
    while t <= hi_t:
        ticks.append({"y": round(y_for(t), 1), "label": _tick_label(t)})
        t += step
    zero_y = round(y_for(ZERO), 1)

    n = len(rows)
    slot = plot_w / n
    bar_w = max(6.0, min(36.0, slot * 0.6))
    bars, points = [], []
    label_every = 1 if n <= 12 else (2 if n <= 24 else 3)
    for i, r in enumerate(rows):
        cx = pad_left + slot * (i + 0.5)
        y = y_for(r["equity"])
        top, bottom = (y, zero_y) if r["equity"] >= 0 else (zero_y, y)
        bars.append({
            "x": round(cx - bar_w / 2, 1), "y": round(top, 1), "w": round(bar_w, 1),
            "h": round(max(bottom - top, 0.0), 1), "neg": r["equity"] < 0,
            "d": _bar_path(cx - bar_w / 2, top, bar_w, max(bottom - top, 0.0), r["equity"] < 0),
            "label": r["label"], "value": r["equity"], "cx": round(cx, 1),
            "show_label": (n - 1 - i) % label_every == 0,
            "short": r["month_end"].strftime("%b") if n <= 12 else r["month_end"].strftime("%b %y"),
        })
        points.append(f"{cx:.1f},{y:.1f}")
    return {"width": width, "height": height, "bars": bars, "ticks": ticks,
            "zero_y": zero_y, "points": " ".join(points),
            "x_axis_y": pad_top + plot_h, "pad_left": pad_left,
            "plot_right": width - pad_right}


def _bar_path(x, y, w, h, neg, radius=4.0):
    """SVG path for a bar rounded only at its data end (top for positive,
    bottom for negative) and square at the zero baseline."""
    if h <= 0:
        return ""
    r = min(radius, w / 2, h)
    x2, y2 = x + w, y + h
    if neg:      # baseline is the top edge; round the bottom corners
        return (f"M{x:.1f},{y:.1f} H{x2:.1f} V{y2 - r:.1f} Q{x2:.1f},{y2:.1f} {x2 - r:.1f},{y2:.1f} "
                f"H{x + r:.1f} Q{x:.1f},{y2:.1f} {x:.1f},{y2 - r:.1f} Z")
    return (f"M{x:.1f},{y2:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x2 - r:.1f} Q{x2:.1f},{y:.1f} {x2:.1f},{y + r:.1f} V{y2:.1f} Z")


def _tick_label(v):
    v = Decimal(v)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}k".replace(".0k", "k")
    return f"{sign}${a:.0f}"
