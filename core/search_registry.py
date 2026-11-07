"""
Every screen a person can jump to from the top search box, with the words
they are likely to type for it. Filtered by the user's modules so the
dropdown never offers a page they can't open.

(label, url name, module key or "admin", keywords)
"""
from django.urls import NoReverseMatch, reverse

PAGES = [
    # --- front office ---
    ("Dashboard", "dashboard", "dashboard", "home overview money cash equity"),
    ("Orders (POs)", "po_list", "orders", "purchase orders customer orders sales"),
    ("New order", "po_create", "orders", "create po new sale"),
    ("Invoices", "invoice_list", "invoices", "billing"),
    ("Credit memos", "creditmemo_list", "invoices", "returns credits refunds customers"),
    ("Payments / AR", "payment_list", "payments", "receivable collections customer payments"),
    ("Record a payment", "payment_create", "payments", "receive money check ach"),
    ("Customers", "customer_list", "customers", "stores accounts buyers"),
    ("New customer", "customer_create", "customers", "add store"),
    # --- warehouse ---
    ("Inventory", "product_list", "inventory", "products stock skus catalog"),
    ("New product", "product_create", "inventory", "add sku item"),
    ("Import products (CSV)", "product_import", "inventory", "upload catalog spreadsheet"),
    ("Print barcode labels", "product_labels", "inventory", "sku label printer 2x1"),
    ("Price lists", "pricelist_list", "inventory", "pricing tiers wholesale"),
    ("Receiving — supplier orders", "receipt_list", "receiving", "rcv purchases incoming inbound receive"),
    ("New supplier order", "receipt_create", "receiving", "rcv new purchase"),
    ("Import a supplier order", "receipt_import", "receiving", "paste csv order confirmation"),
    ("Tradepost purchases", "receipt_tradepost", "receiving", "tradepost fills sync api"),
    ("Suppliers", "supplier_list", "receiving", "vendors distributors rename merge"),
    ("Cycle counts", "cyclecount_list", "receiving", "count stock audit"),
    ("Pick queue", "pick_queue", "picking", "picking pack orders warehouse"),
    ("Shipping", "shipment_list", "shipping", "labels shippo pirate ship tracking"),
    ("Deliveries", "delivery_list", "deliveries", "local delivery route"),
    # --- money ---
    ("Banking — review inbox", "bank_inbox", "banking", "bank transactions categorize review card"),
    ("Bank & card accounts", "bank_accounts", "banking", "chase amex balances reconcile statement"),
    ("Import bank CSV", "bank_import", "banking", "upload statement transactions"),
    ("Category rules", "bank_rules", "banking", "auto categorize rules starter"),
    ("Add a bank transaction by hand", "bank_transaction_add", "banking", "manual entry"),
    ("Expenses", "expense_list", "expenses", "spending bills receipts"),
    ("Quick expense capture", "expense_quick", "expenses", "snap receipt phone"),
    ("Vendors (1099)", "expense_vendors", "expenses", "contractors 1099 w9"),
    ("Expense categories", "expense_categories", "expenses", "schedule c deductible meals"),
    ("Recurring expenses", "expense_recurring", "expenses", "subscriptions monthly"),
    ("Budgets", "expense_budgets", "expenses", "budget vs actual"),
    ("Mileage", "expense_mileage", "expenses", "miles car trips standard rate"),
    ("Expenses by month", "expense_report_by_month", "expenses", "report spending trend"),
    ("Missing receipts", "expense_report_missing_receipts", "expenses", "report audit"),
    ("Unpaid bills", "expense_report_unpaid_bills", "expenses", "ap aging owe suppliers"),
    ("Year-end expense package", "expense_year_end", "expenses", "accountant zip receipts 1099 tax"),
    ("Accounting", "accounting_index", "accounting", "books ledger cash accrual"),
    ("Profit & loss (P&L)", "accounting_pnl", "accounting", "income statement profit revenue net"),
    ("Balance sheet", "accounting_balance_sheet", "accounting", "equity assets liabilities net worth"),
    ("Equity trend", "accounting_equity_trend", "accounting", "net worth growth monthly"),
    ("Cash vs accrual", "accounting_compare", "accounting", "bridge difference bases"),
    ("Trial balance", "accounting_trial_balance", "accounting", "debits credits"),
    ("General ledger", "accounting_general_ledger", "accounting", "entries account detail"),
    ("Journal", "accounting_journal", "accounting", "entries postings"),
    ("Chart of accounts", "accounting_accounts", "accounting", "accounts codes"),
    ("Tax summary (cash basis)", "accounting_tax_summary", "accounting", "accountant pdf schedule c taxes year"),
    ("Year-end package", "accounting_year_end", "accounting", "close year accountant pdfs"),
    ("Month close", "accounting_month_close", "accounting", "close month checklist"),
    ("Reports", "report_index", "reports", "all reports"),
    ("Inventory valuation report", "report_inventory_valuation", "reports", "stock value cost on hand"),
    ("P&L report", "report_pnl", "reports", "profit loss"),
    ("Revenue by customer", "report_revenue_customer", "reports", "top customers sales"),
    ("Revenue by day of week", "report_revenue_dow", "reports", "sales pattern weekday"),
    ("Sales by product", "report_sales_product", "reports", "best sellers units"),
    ("Stock movements", "report_movements", "reports", "ledger in out history"),
    ("Sales tax report", "report_sales_tax", "reports", "tax collected remit state"),
    ("Shipping report", "report_shipping", "reports", "postage costs carriers"),
    ("AR aging", "report_ar_aging", "reports", "overdue customers owe"),
    ("QuickBooks export", "report_quickbooks", "reports", "iif csv accountant"),
    ("Market price check (TCGplayer)", "report_market_check", "reports", "market value cost percent"),
    # --- admin ---
    ("Settings", "settings_edit", "admin", "company logo theme api keys tradepost shippo plaid"),
    ("Users & roles", "user_list", "admin", "permissions staff login"),
    ("Activity log", "activity_log", "admin", "audit who changed what"),
]


def pages_for(user):
    """Pages this user may open: [{label, url, keywords}]."""
    from core.roles import is_admin, user_modules
    admin = is_admin(user)
    mods = user_modules(user)
    out = []
    for label, url_name, module, keywords in PAGES:
        if module == "admin":
            if not admin:
                continue
        elif module not in mods and not admin:
            continue
        try:
            url = reverse(url_name)
        except NoReverseMatch:
            continue
        out.append({"label": label, "url": url, "keywords": keywords})
    return out


def match_pages(user, q, limit=6):
    """Pages whose label or keywords contain every word typed."""
    words = [w for w in q.lower().split() if w]
    if not words:
        return []
    hits = []
    for page in pages_for(user):
        hay = f"{page['label']} {page['keywords']}".lower()
        if all(w in hay for w in words):
            # label matches rank above keyword-only matches
            score = 0 if all(w in page["label"].lower() for w in words) else 1
            hits.append((score, page))
    hits.sort(key=lambda h: (h[0], h[1]["label"]))
    return [h[1] for h in hits[:limit]]
