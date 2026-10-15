# SFL ERP System

Operations **and books** in one app for a wholesale sports card & collectibles
distributor (SFL Hobby Distribution LLC): inventory with a full movement ledger
and weighted-average landed cost, customers + resale certificates, customer POs
with commit/finalize flow, mobile barcode picking, consolidated invoicing with
PDF + email, payments/AR, shipping integrations (ShipStation API, Shippo labels,
uShip adapter, Pirate Ship batch CSV) — plus, new in 4.0, a real ERP money
layer:

- **Banking** — connect checking accounts and credit cards through Plaid, or
  import the CSV any bank lets you download. Every transaction is de-duplicated,
  run through *category rules* you teach the system ("PIRATE SHIP → Shipping
  labels & freight"), recognised by vendor, and parked in a review inbox. One
  click categorizes it, **Match**es it to a customer payment, a supplier bill or
  an expense, pairs a transfer, **splits** it across several categories, or marks
  it **Personal** (an owner draw). Tick **Always categorize like this** and a
  rule is created. Every decision has an **Undo** that releases whatever the
  decision had linked, so nothing is ever counted twice. Hand-entered rows can
  be edited or deleted; imported rows can only be ignored.
- **Expenses** — the register is filled all year, not reconstructed in April:
  card charges become expenses automatically, a phone-friendly quick-capture
  form takes a receipt photo in 10 seconds, recurring templates post rent and
  subscriptions on their anchor day (Jan 31 → Feb 28 → Mar 31), supplier bills
  live on the Receiving document (invoice #, due date, paid date, **Unpaid
  bills** filter), and reports flag missing receipts, unpaid bills (with an
  inline **Mark paid**), 1099 contractors ($600 through tax year 2025, $2,000
  from 2026), budgets and mileage.
- **Accounting** — a virtual double-entry ledger rebuilt on demand from the
  documents that *are* the business. Income statement, balance sheet, trial
  balance, general ledger and journal on both the **cash basis** (what the tax
  return uses) and the **accrual basis** (the true picture), a plain-English
  cash-vs-accrual bridge, a 12-month "am I building equity?" trend, a
  **Month close** checklist, a soft period lock and a year-end package (CSV and
  PDF statements, trial balances, general ledgers and journals on both bases)
  for the tax preparer. The balance sheet always balances.

**Stack:** Django 5.2 LTS · DRF · PostgreSQL · Django-Q2 + Redis · Tailwind CSS · WeasyPrint

## Quick start (Docker)

```bash
cp .env.example .env        # edit secrets (Plaid keys optional)
docker compose up -d --build
docker compose exec web python manage.py seed_demo   # demo data incl. bank feed
# open http://localhost:8000 — admin / sflerp123
```

On every start the web container runs migrations, `setup_roles`,
`setup_accounts` (chart of accounts + default expense categories) and
`setup_schedules` (hourly bank sync, daily recurring expenses, weekly money
review email, ShipStation sync, low-stock digest).

## Quick start (local dev, SQLite)

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py setup_roles && python manage.py setup_accounts
python manage.py seed_demo
python manage.py runserver
```

Demo logins (password `sflerp123`): **admin** (everything), **office**,
**picker**, **driver**, **books** (Bookkeeper: banking, expenses, accounting,
reports, can see costs).

## Day one with real data

1. Settings → company identity, tax rate, numbering; **Accounting** card: books
   start date, fiscal year, basis to show first; **Bank feed** card: Plaid keys
   (optional).
2. Banking → Accounts → add each checking account and credit card with its
   **opening balance as of the books start date** (cards: amount owed).
3. Banking → Connect a bank (Plaid) or Import CSV. Review the inbox; tick
   **Always categorize like this** on anything repetitive to create a rule.
4. Expenses → Vendors: add the people you pay often and their default category;
   mark 1099 contractors.
5. Receiving → on each supplier order fill in the supplier's invoice # and due
   date (the **Supplier bill** card); the bill is open until a bank payment is
   matched to it.
6. Dashboard → Money shows cash position, profit on both bases, equity, the
   "needs attention" list and last month's close progress from here on.

## Reports and exports

Every statement and report can be exported. Financial statements (income
statement, balance sheet, cash vs. accrual, trial balance), the expense
reports and the operational reports export to **CSV and PDF**. **CSV only**:
the general journal, the general ledger, the equity trend (it has an on-screen
chart instead), the expense register and mileage lists, and the QuickBooks
files. Two ZIP packages exist for the tax preparer: **Accounting → Year-end
package** (statements as CSV and PDF, cash and accrual trial balances, general
ledgers and journals on both bases, also per month) and **Expenses → Year-end
package** (Schedule C worksheet, every expense, 1099 vendors, mileage, missing
receipts, bills open at Dec 31 and a `receipts/` folder with every attached
receipt). See `docs/ADMIN_GUIDE.md` → *Year-end*.

## Tests

```bash
python manage.py test                 # everything
python manage.py test accounting      # ledger engine: every statement balances
python manage.py test banking         # import, dedupe, rules, matching, Plaid sync
python manage.py test expenses        # register, recurring, reports, year-end
python manage.py test core            # dashboard, settings, digest, seed_demo smoke test
```

## Docs

- `docs/ACCOUNTING.md` — how the virtual ledger works, entry rules, cash vs accrual, FAQ
- `docs/BANK_CONNECTIONS.md` — Plaid setup, CSV export from each major bank, troubleshooting
- `docs/ADMIN_GUIDE.md` — owner's user guide (daily flow, review inbox, month-end, year-end)
- `docs/SCHEMA.md` — data model
- `docs/RAILWAY.md` — one-click style deploy on Railway (web + worker + Postgres + Redis)
- `docs/DEPLOYMENT.md` — Ubuntu VPS + nginx + gunicorn (Docker and bare-metal)
- `.env.example` — every environment variable documented

## Layout

```
config/      settings, urls
core/        settings singleton, numbering, activity log, roles, dashboard, review digest, seed_demo
inventory/   products, stock ledger, receiving/landed cost + supplier bill fields, cycle counts, price lists
customers/   customers, contacts, addresses, resale certificates
orders/      customer POs, status machine, picking API + mobile UI
invoicing/   invoices, credit memos, PDF layouts (modern/classic)
payments/    payments, applications, AR
expenses/    categories (= ledger accounts), vendors, expenses, recurring, budgets, mileage, reports
banking/     bank accounts, Plaid provider, CSV importers + bank presets, rules (+ regex guard), matching, splits, review inbox
accounting/  chart of accounts, the virtual ledger engine, financial statements, month close, year-end, period lock
shipping/    Shipment model + providers/ (ShippingProvider interface)
reports/     operational reports: valuation, margin P&L, sales, aging, QuickBooks exports
templates/   Tailwind UI + WeasyPrint PDF templates
docs/        guides
```

## Background jobs (Django-Q2)

| Schedule | Job | What it does |
|---|---|---|
| hourly | `banking.tasks.sync_all_connections` | pull new transactions from every Plaid connection, run rules |
| hourly | `shipping.tasks.sync_shipstation_tracking` | tracking numbers + label costs back from ShipStation |
| daily 6am | `expenses.tasks.generate_recurring_expenses` | post due recurring expenses (rent, software…) |
| daily 7am | `shipping.tasks.send_low_stock_digest` | low-stock email |
| Monday 7am | `core.tasks.send_review_digest` | weekly email: transactions to review, bills due, missing receipts, connections that need a re-login |
