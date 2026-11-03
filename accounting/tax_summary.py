"""
The accountant's one-pager: a cash-basis summary of the year.

Everything your preparer needs for a Schedule C, built from the same virtual
ledger as every other statement: revenue, cost of goods, expenses by category
with their Schedule C line and deductible share, sales tax collected and
remitted, the standard-mileage deduction, and the 1099 vendor list.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from accounting import reports as rp
from accounting.ledger import CASH, Journal
from accounting.models import Account, AccountType

ZERO = Decimal("0.00")


def build(start, end):
    from banking.models import BankTransaction
    from core.models import CompanySettings
    from expenses.models import SCHEDULE_C_LINES, ExpenseCategory
    from expenses.reports import default_threshold, mileage_summary, vendors_1099

    sched = dict(SCHEDULE_C_LINES)
    j = Journal(CASH, [start - timezone.timedelta(days=1), end])
    stmt = j.income_statement(start, end)

    # Expenses with Schedule C mapping and deductible share
    cats = {c.account_id: c for c in ExpenseCategory.all_objects.filter(account__isnull=False)}
    expenses = []
    total_deductible = ZERO
    for r in stmt["expenses"]:
        cat = cats.get(r["account"].pk)
        pct = cat.deductible_pct if cat else 100
        ded = (r["amount"] * Decimal(pct) / Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        total_deductible += ded
        expenses.append({"account": r["account"], "category": cat, "amount": r["amount"],
                         "pct": pct, "deductible": ded,
                         "tax_line": sched.get(cat.tax_line, "") if cat and cat.tax_line else "",
                         "uncategorized": r["account"].system_key == "uncat_expense"})
    uncat_income = sum((r["amount"] for r in stmt["income"] if r["account"].system_key == "uncat_income"), ZERO)
    uncat_expense = sum((r["amount"] for r in expenses if r["uncategorized"]), ZERO)

    # Sales tax: credits = collected, debits = sent to the state (cash basis)
    tax_acct = Account.system("sales_tax")
    collected = remitted = ZERO
    for row in j.journal_rows(start, end):
        if row["account"].pk == tax_acct.pk:
            collected += row["credit"]
            remitted += row["debit"]

    year = end.year
    mileage = mileage_summary(year)
    nec = vendors_1099(year)
    unreviewed = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED,
                                                date__gte=start, date__lte=end).count()
    net_after = stmt["gross_profit"] - total_deductible - mileage["total_deduction"]
    return {
        "start": start, "end": end, "label": rp.fiscal_year_label(start, end), "basis": CASH,
        "stmt": stmt, "expenses": expenses, "total_deductible": total_deductible,
        "uncat_income": uncat_income, "uncat_expense": uncat_expense, "unreviewed": unreviewed,
        "sales_tax_collected": collected, "sales_tax_remitted": remitted,
        "sales_tax_owed": collected - remitted,
        "mileage": mileage, "nec": nec, "threshold": default_threshold(year),
        "net_after_deductions": net_after,
        "settings_row": CompanySettings.get(), "generated": timezone.localtime(),
    }


def pdf_sections(data):
    stmt = data["stmt"]
    inc = [{"label": r["account"].name, "code": r["account"].code, "amounts": [r["amount"], None]}
           for r in stmt["income"]]
    cogs = [{"label": r["account"].name, "code": r["account"].code, "amounts": [r["amount"], None]}
            for r in stmt["cogs"]]
    exp = [{"label": e["account"].name + (f"  ·  {e['tax_line']}" if e["tax_line"] else "")
                     + (f"  ({e['pct']}% deductible)" if e["pct"] != 100 else ""),
            "code": e["account"].code, "amounts": [e["amount"], e["deductible"]]} for e in data["expenses"]]
    m = data["mileage"]
    nec_rows = [{"label": f"{r['vendor'].name}" + (f"  ·  tax ID …{r['vendor'].tax_id_last4}" if r["vendor"].tax_id_last4 else ""),
                 "code": "", "amounts": [r["total"], None]} for r in data["nec"]["qualifying"]]
    sections = [
        {"heading": "Revenue (money received)", "rows": inc,
         "total_label": "Total revenue", "totals": [stmt["total_income"], None]},
        {"heading": "Cost of goods (inventory paid for)", "rows": cogs,
         "total_label": "Total cost of goods", "totals": [stmt["total_cogs"], None]},
        {"heading": None, "rows": [], "total_label": "Gross profit",
         "totals": [stmt["gross_profit"], None], "emphasis": True},
        {"heading": "Expenses by category (Schedule C line · deductible share)", "rows": exp,
         "total_label": "Total expenses", "totals": [stmt["total_expenses"], data["total_deductible"]]},
        {"heading": None, "rows": [], "total_label": "Net income per the books (cash basis)",
         "totals": [stmt["net_income"], None], "emphasis": True},
        {"heading": "Standard mileage deduction (not in the books)",
         "rows": [{"label": f"{m['count']} trips · {m['total_miles']} business miles at the IRS rate",
                   "code": "", "amounts": [None, m["total_deduction"]]}],
         "total_label": "Mileage deduction", "totals": [None, m["total_deduction"]]},
        {"heading": None, "rows": [], "total_label": "Estimated net after deductibility and mileage",
         "totals": [None, data["net_after_deductions"]], "emphasis": True,
         "note": "Estimate only: your preparer applies the actual rules (home office, depreciation, self-employment tax, etc.)."},
        {"heading": "Sales tax (never income)", "rows": [
            {"label": "Collected from customers", "code": "", "amounts": [data["sales_tax_collected"], None]},
            {"label": "Sent to the state", "code": "", "amounts": [data["sales_tax_remitted"], None]}],
         "total_label": "Still owed to the state at year end", "totals": [data["sales_tax_owed"], None]},
        {"heading": f"1099-NEC contractors (paid ≥ ${data['threshold']:,.0f} this year)",
         "rows": nec_rows or [{"label": "None", "code": "", "amounts": [None, None]}],
         "total_label": "Total paid to 1099 contractors", "totals": [data["nec"]["qualifying_total"], None]},
    ]
    return sections
