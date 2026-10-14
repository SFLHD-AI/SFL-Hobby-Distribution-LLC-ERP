# SFL ERP System — Owner's Guide

A quick tour of daily operations and, since 4.0, of keeping the books as
you go. Log in at your SFL ERP System URL.
Demo logins after `seed_demo`: **admin / office / picker / driver / books**,
password `sflerp123`.

## Users & roles (sidebar → Users & roles, admin only)

Create accounts and assign each one a **role**. A role is a checklist of
modules its users may open (Orders, Inventory, Shipping, Deliveries,
Reports, Expenses, Banking, Accounting…) plus two switches: *can see
costs/profits* and *full admin*. Five roles are built in — Admin
(everything), Office (day-to-day ops, no costs), Warehouse (picking only),
**Fulfillment Manager** (deliveries + shipping) and **Bookkeeper** (banking,
expenses, accounting, invoices, payments, customers, reports; sees costs) —
and you can create as many custom roles as you like. Users only see sidebar
links for modules their role allows; anything else bounces them back to
their own pages. Only roles that can see costs see cash, profit and equity
numbers (the dashboard shows them counts instead). Password resets are on
the user's edit page.

## The dashboard

The top of the dashboard is the **Money** section:

- **Cash position** — every bank account's balance (opening balance plus
  all imported/synced transactions), what you owe on cards, and the net.
- **Net profit this month / year to date** on the basis you chose in
  Settings, with the other basis in small text underneath.
- **Equity (accrual)** — what the business is worth on paper, and how much
  it moved since the last month-end. Up month after month = you are
  building equity.
- **Needs attention** — bank transactions waiting for review, bills due in
  the next 7 days (overdue in red), expenses this month without a receipt,
  auto-created expenses to confirm, and bank connections that need a fresh
  login. Each line links to the page that clears it. The goal is to keep
  this card green.

Below it are the operational widgets (orders by status, low stock,
certificates expiring, recent orders, balances owed, inbound supplier
orders).

## Daily flow

**1. Take an order** — Orders → New order. Pick customer (their ship-to and
per-SKU prices load automatically), add lines by scanning a barcode into the
scan box or picking from the product list. Check ☑ shipping / ☑ tax and set
amounts if applicable. **Confirm** to commit inventory.

**2. Pick it** — Confirm → *Send to pick queue*. Your warehouse person opens
`/pick/` on the iPad, taps the order, scans items (wrong item = red flash +
buzz; over-pick is blocked). Shorts get a reason and flow to backorders.
*Complete Pick* notifies the office; progress is visible live on the PO page.

**3. Finalize** — On the PO, *Finalize*. This is the moment stock is deducted
and the cost of each line is frozen (so later cost changes never rewrite
profit history). If quantities were short, one click creates the backorder PO.

**4. Invoice** — *Create invoice* from the PO, or Invoices → New to merge
several finalized POs from the same customer into one consolidated invoice.
Tax is 0% automatically with a valid resale certificate; otherwise your
default rate applies and the amount can be overridden on the invoice.
*PDF* to print, *Email* to send with the PDF attached.

**5. Ship** — PO → *Ship it*. ShipStation: push the order, tracking and label
cost sync back hourly (and email the customer). Pirate Ship: select orders on
the Shipping screen → export the batch CSV → import at pirateship.com → paste
tracking/cost back. uShip (pallets): push via API if configured, otherwise
enter the booked reference and cost manually. The label cost you record here
is what the accrual books expect to see later as a Pirate Ship / USPS charge
on your card.

**6. Get paid** — Invoices → Record payment. One check can cover several
invoices (*Auto-apply oldest first*). Partial payments are fine. Statements:
customer page → *Statement* (PDF) or *Email statement*. When the deposit
shows up in the bank feed, match it to the payment(s) in the inbox.

**7. Review the money (5 minutes, daily)** — see *Review transactions daily*
below.

## Connect your bank / import statements

Everything that happens in your checking account and on your credit cards
should flow into **Banking**. Two ways, mix as you like:

- **Plaid (automatic).** Paste your Plaid keys under Settings → Bank feed,
  then Banking → *Connect a bank* and log in to the bank through Plaid's
  widget. New transactions arrive every hour. Start in Sandbox (fake banks)
  if you want to see the flow first.
- **CSV (manual).** Download a CSV from the bank's website and use Banking →
  *Import CSV*. Chase, Amex, Capital One, Bank of America, Wells Fargo, Citi,
  Discover, PayPal and Square files are recognised automatically; anything
  else works with the generic column picker. Re-importing an overlapping
  range is safe — duplicates are skipped.

First set up each account under Banking → **Accounts**: name, type
(checking / credit card / …), last 4 digits, and the **opening balance as of
your books start date** (for a card: the amount owed, as a positive number).
Transactions before the opening date are ignored. One account can be the
**default** — it is pre-selected as "paid from" on expenses.

Step-by-step Plaid setup, where the CSV download hides on each bank's site,
and what to do when something looks doubled or upside down:
`docs/BANK_CONNECTIONS.md`.

## Review transactions daily

**Banking** (the sidebar badge shows how many are waiting) lists every
transaction that still needs a decision. For each one, pick the action that
describes what the money was:

| The money was… | Action | What happens |
|---|---|---|
| a purchase, a bill, a fee | **Categorize** → pick the expense category (or Shipping labels, Inventory purchases, Owner draw…) | An expense row is created in the register, already marked paid from this account |
| something you already typed in (quick capture, a bill) | **Match to expense** — the suggestion appears automatically when amount and date fit | The expense is marked paid; nothing is counted twice |
| a customer deposit | **Match to payment(s)** — one deposit can cover two or three checks | The payments move from *Undeposited funds* to the bank |
| paying a supplier for product | **Match to supplier bill(s)** (posted receipts) | The receipt is marked paid; the bill drops off the unpaid list |
| paying your credit card, moving money between your accounts | **Transfer** — the twin in the other account is suggested | No expense, no income; money just moved |
| personal | **Categorize → Owner draw / personal** | Shows in equity, not on the P&L |
| a duplicate, or not this business | **Ignore** | Disappears from everything, including the balance |

Tick **"Always do this"** when you categorize something that repeats
(Pirate Ship, ULINE, Google Workspace, the Chase monthly fee…) and a **rule**
is created. From then on those transactions are categorized the moment they
arrive and never reach the inbox. Rules live under Banking → Rules, where
you can also run them against the whole inbox. **Vendors** (Expenses →
Vendors) do the lighter version: when a description contains a vendor's name
or one of its aliases, the vendor's default category is suggested and you
confirm with one click.

Until a transaction is reviewed it sits on your statements as
**Uncategorized expense / income** — visible on purpose, so an unworked inbox
cannot quietly make profit look better than it is.

Every Monday morning the **weekly money review email** (Settings → Bank
feed, sent to the alert email) lists what is waiting: transactions to
review and the oldest one, bills due this week, expenses missing receipts,
auto-created expenses to confirm, and connections that need a fresh login.

## Expenses & receipts

The **Expenses** module is the register of everything you spend outside of
inventory. It fills itself from four directions so nothing has to be
reconstructed at year end:

- **From the bank feed** — a categorized card charge *is* an expense. Rows
  created by a rule arrive as *to confirm* (a quick glance, then Confirm).
- **Quick capture** (Expenses → *Quick add*, works on a phone): amount,
  vendor, category, snap the receipt, done. When the card charge arrives the
  inbox matches it to this expense.
- **Recurring templates** (Expenses → Recurring): rent, software, insurance.
  Posted automatically every morning as either an open bill or an auto-paid
  expense; a matching bank transaction later links to it.
- **Typed in** with due date, vendor invoice number and "paid from" / "paid
  personally". Paid-personally expenses count fully and show up as an owner
  contribution.

Attach a receipt to every expense — photo, PDF or the vendor's email. The
**Missing receipts** report and the dashboard count whatever has none this
month. Clear that list monthly and the year-end receipts bundle is just a
download.

Also under Expenses:

- **Vendors** — who you pay, their aliases on statements, default category,
  and the **1099** flag. The *1099 contractors* report totals every flagged
  vendor paid $600+ this year.
- **Categories** — one per ledger account, each mapped to a Schedule C line
  and a deductible percentage (meals 50%).
- **Budgets** — planned spend per category per month or year; the
  *Expenses by month* report shows actual against budget.
- **Mileage** — business miles with purpose, from/to and round trip at the
  IRS rate from Settings; the deduction appears in the year-end package.
- **Unpaid bills** — open expenses and supplier bills by due date. *Mark
  paid* when you pay one by check; let the bank feed do it otherwise.

Supplier bills: on a Receiving document fill in the supplier's invoice
number and due date. Once posted it is an open bill until a bank payment is
matched to it (or you type a paid date).

## Local deliveries (your driver)

Check **🚐 Local delivery** on a PO and, once it's picked/finalized, it appears
on the **Deliveries** screen under "Ready to schedule". Pick a day, and your
driver (Fulfillment Manager role) sees that day's stops on his phone: tap a
stop for the address, phone, item list and a **Navigate** button; **🗺 Route
from warehouse** builds a Google Maps route from your address in Settings
through every open stop. At drop-off he takes a **proof photo** and taps *Mark
delivered* — the photo is saved on the shipment and the order flips to Shipped.

## Shipping weights & packages

Give each product a **unit weight** (product page, or the `weight_lbs` CSV
column). Every PO then shows its estimated ship weight. On a shipment, the
**package builder** splits the order into boxes: add a box with estimated
dimensions, assign items into it, and the box weight auto-sums from unit
weights (override it after weighing the packed box). Box dims + weights are
what you feed to Pirate Ship / ShipStation / uShip quotes.

## Inventory

- **Starting from an existing business?** Enter opening stock at *your* costs:
  on a new product's form fill "Opening quantity + your cost per unit"; on an
  existing product use an adjustment with the optional **unit cost** field
  (adding at a cost rolls the weighted average, removals never change it); or
  bulk-load via CSV import columns `opening_qty` and `opening_unit_cost`.
  Opening stock lands in *Opening balance equity* on the accrual books.
- **Receiving**: enter a supplier order (SKUs, qty, unit cost, freight/duties,
  supplier invoice # and due date). *Mark received* puts stock in, rolls the
  **weighted average landed cost**, and opens the supplier bill.
- **Adjustments**: on any product page — reason code required, everything logged.
  *Personal use* goes to owner draws; damaged/shrinkage/corrections go to
  inventory adjustments on the P&L.
- **Cycle counts**: create a count sheet (all / category / bin), type counted
  quantities, apply — the system writes the deltas.
- **"Why is this SKU at this number?"** — every product page shows the full
  movement ledger: each receipt, sale, adjustment, count, and restock with
  before/after, user, time and source document.
- **Labels**: select products → print Code 128 labels for SKUs or bins.
- **CSV**: import/export products from the Inventory screen.

## Month-end & year-end

### Cash basis vs. accrual basis (the one thing worth understanding)

You file taxes on the **cash basis**: income counts when the money arrives,
costs count when the money leaves. It is simple and it is what the IRS gets.
But it is a poor way to tell how the business is *doing*: the month you buy
$30,000 of product looks like a disaster and the month you sell it looks like
a windfall, even if you made a steady 20% all along.

The **accrual basis** fixes that: revenue counts when you invoice, product
is an asset on the shelf until it is sold (then it is cost of goods sold),
and bills count when you receive them. Accrual profit is the real profit of
the period, and accrual **equity** — assets minus what you owe — is what the
business is worth on paper.

SFL ERP System produces **both from the same documents**, every time you
open a statement; there is nothing to post or close. Settings → Accounting
decides which one you see first (cash, since that is your tax basis). Every
statement has a Cash / Accrual toggle, and **Accounting → Cash vs. accrual**
shows the two side by side with the exact reasons they differ.

### What "building equity" means

> Equity = what you put in + profits kept in the business − what you took out

On the accrual basis that equals **assets** (cash, receivables, inventory at
cost, deposits with suppliers) **minus liabilities** (cards, unpaid supplier
bills, sales tax you have collected but not remitted). If accrual equity is
higher at the end of this month than last — after your draws — the business
grew. If cash is up but equity is flat, you sold inventory you have not
replaced. If equity is up but cash is down, you turned cash into stock or
receivables; fine as long as it keeps turning.

The dashboard shows equity and its change since last month-end;
**Accounting → Equity trend** shows twelve months with a chart; Reports →
*Am I building equity?* is the same page.

### Month-end routine (30 minutes)

1. **Inbox to zero** — every bank transaction reviewed; bank balances on
   Banking → Accounts match the real statements.
2. **Bills** — supplier receipts posted with invoice numbers and due dates;
   recurring expenses confirmed; *Unpaid bills* report looks right.
3. **Receipts** — *Missing receipts* report cleared.
4. **Stock** — spot cycle count; *Month-end inventory valuation* saved.
5. **Statements** — Accounting → Income statement and Balance sheet for the
   month on both bases; *Cash vs. accrual* to understand the gap; *Equity
   trend* to see the direction.
6. **Sales tax** — Reports → Sales tax report for your filing period
   (monthly/quarterly per Settings) when it is due.
7. Optional: Accounting → **Close books** through the month-end. Editing a
   document in a closed period then shows a warning (admins can override).

### Year-end (what your tax preparer gets)

- **Accounting → Year-end package**: cash-basis income statement and balance
  sheet for the fiscal year, trial balance, general ledger and journal
  exports, plus the accrual versions for your own records.
- **Expenses → Year-end package**: expense totals by Schedule C line with
  the deductible percentage applied, the mileage deduction, the 1099
  contractor list and the receipts bundle.
- **Reports → QuickBooks export** if your preparer wants invoices, payments
  and expenses as plain CSVs.
- Close the books through December 31 (Accounting → Close books) once the
  return is filed.

### Other reports (Reports page)

- **Operational P&L (margin view)** — revenue, COGS, gross margin, shipping
  pass-through and expenses from invoice cost snapshots; good for margin
  analysis, not a financial statement.
- **Inventory valuation** as of any date, **revenue/profit by customer**,
  **by day of week**, **sales by SKU/category/brand**, **AR aging**,
  **shipping charges vs. costs**, **sales tax**.
- Every report exports to CSV and PDF.

## Certificates & tax

Upload resale certificates on the customer page. Expired/missing certs show
warnings on the dashboard and customer page. Per customer, choose what
happens without a valid cert: **charge tax** or **block finalization**.

## Settings (admin only)

- **Company identity** — name, PNG logo, address, phone, contacts, website;
  these appear on invoices, POs, packing slips and statements.
- **Invoicing & tax** — default tax rate, invoice layout (Modern/Classic),
  footer, starting numbers and prefixes for PO/Invoice/Credit memo.
- **Accounting** — which basis to show first, fiscal year start month,
  books start date, mileage rate, sales-tax filing frequency.
- **Bank feed (Plaid)** — client ID, secret, sandbox/production, hourly sync
  on/off, the weekly money review email on/off. "Plaid configured" shows
  when both keys are present (here or in `.env`).
- **Alerts** — alert email, daily low-stock digest.
- **Integrations** — ShipStation, Shippo, uShip keys.

## Safety nets

- Nothing is hard-deleted — deletions are recoverable soft-deletes.
- The books are rebuilt from documents on demand, so there is no ledger to
  corrupt or get out of sync; fix the document and the statements follow.
- Global **Activity log** (admin) records who did what and when, including
  every transaction review.
- Nightly `backup_db` keeps 14 compressed database backups.
- Global search (top bar) finds SKUs, customers, PO/invoice/tracking numbers.
