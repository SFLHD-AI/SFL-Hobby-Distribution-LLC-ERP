"""Give every pre-existing expense category its ledger account and seed the
system chart of accounts, so the financial statements work on day one."""
from django.db import migrations

SYSTEM_ACCOUNTS = {
    "cash_on_hand": ("1000", "Cash on hand", "asset", "cash_on_hand"),
    "undeposited": ("1050", "Undeposited funds", "asset", "undeposited"),
    "ar": ("1200", "Accounts receivable", "asset", "ar"),
    "inventory": ("1300", "Inventory", "asset", "inventory"),
    "supplier_deposits": ("1400", "Supplier deposits & prepayments", "asset", "supplier_deposits"),
    "fixed_assets": ("1500", "Equipment & fixed assets", "asset", "fixed_asset"),
    "ap": ("2000", "Accounts payable — suppliers", "liability", "ap"),
    "ap_carriers": ("2010", "Accounts payable — carriers", "liability", "ap_carriers"),
    "sales_tax": ("2200", "Sales tax payable", "liability", "sales_tax"),
    "customer_deposits": ("2300", "Customer deposits / unapplied payments", "liability", "customer_deposits"),
    "loans": ("2500", "Loans & lines of credit", "liability", "loan"),
    "opening_balance": ("3000", "Opening balance equity", "equity", "opening_balance"),
    "owner_contribution": ("3100", "Owner contributions", "equity", "owner_contribution"),
    "owner_draw": ("3200", "Owner draws / personal", "equity", "owner_draw"),
    "retained": ("3900", "Retained earnings", "equity", "retained"),
    "sales": ("4000", "Sales", "income", "sales"),
    "shipping_income": ("4100", "Shipping charged to customers", "income", "shipping_income"),
    "sales_returns": ("4200", "Sales returns & allowances", "income", "sales_returns"),
    "other_income": ("4900", "Other income", "income", "other_income"),
    "uncat_income": ("4950", "Uncategorized income", "income", "uncat_income"),
    "cogs": ("5000", "Cost of goods sold", "cogs", "cogs"),
    "inventory_purchases": ("5100", "Inventory purchases", "cogs", "inventory_purchases"),
    "inventory_adj": ("5200", "Inventory adjustments & shrinkage", "cogs", "inventory_adj"),
    "shipping_expense": ("5300", "Shipping labels & freight", "cogs", "shipping_expense"),
    "uncat_expense": ("6900", "Uncategorized expense", "expense", "uncat_expense"),
}


def forwards(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    ExpenseCategory = apps.get_model("expenses", "ExpenseCategory")
    for key, (code, name, type_, subtype) in SYSTEM_ACCOUNTS.items():
        Account.objects.get_or_create(system_key=key, defaults={
            "code": code, "name": name, "type": type_, "subtype": subtype, "is_system": True})
    used = set(Account.objects.values_list("code", flat=True))
    n = 6000
    for cat in ExpenseCategory.objects.filter(account__isnull=True).order_by("name"):
        while str(n) in used:
            n += 1
        acct = Account.objects.create(code=str(n), name=cat.name, type="expense", subtype="expense")
        used.add(str(n))
        cat.account = acct
        cat.save(update_fields=["account"])


class Migration(migrations.Migration):
    dependencies = [
        ("expenses", "0002_expense_due_date_expense_is_reviewed_and_more"),
        ("accounting", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
