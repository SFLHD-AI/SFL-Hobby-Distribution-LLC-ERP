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
  With a Plaid connection the **Sync now** button pulls new transactions on
  the spot.
- **Net profit this month / year to date** on the basis you chose in
  Settings, with the other basis in small text underneath.
- **Equity (accrual)** — what the business is worth on paper, and how much
  it moved since the last month-end. Up month after month = you are
  building equity.
- **Needs attention** — bank transactions waiting for review, bills due in
  the next 7 days (overdue in red; a bill with no due date is aged from its
  bill date), expenses this month without a receipt (a receipt attached to
  the bank transaction counts), auto-created expenses to confirm, bank
  connections that need a re-login, and last month's **Month close**
  progress ("September: 4 of 6 done"). Each line links to the page that
  clears it. The goal is to keep this card green.

Below it are the operational widgets (today's and month-to-date revenue,
**Customers owe you**, pick queue, orders by status, low stock, certificates
expiring, recent orders, balances owed, inbound supplier orders).

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
  then Banking → **Connect a bank** and log in to the bank through Plaid's
  widget. New transactions arrive every hour, or press **Sync now** on the
  inbox or the dashboard. When a bank asks for a fresh login the connection
  shows **Re-login needed**; press **Re-login** on *Connect a bank*. The
  **Disconnect** button lives on the same page.
- **CSV (manual).** Download a CSV from the bank's website and use Banking →
  **Import CSV**. Chase, Amex, Capital One, Bank of America, Wells Fargo, Citi,
  Discover, PayPal and Square files are recognised automatically; anything
  else works with the generic column picker. A bare three-column Amex export
  (Date, Description, Amount) is not specific enough to recognise: map the
  columns yourself and tick *Amounts are positive for charges (money out)*.
  Re-importing an overlapping range is safe — duplicates are skipped.
- **By hand.** Banking → **+ Add by hand** for a cash purchase, a check or
  anything the feed missed. Hand-entered rows are the only ones you can
  **edit** or **delete** later (from the transaction page); imported or
  synced rows can only be ignored.

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
| a purchase, a bill, a fee | pick the category (an expense category, or Shipping labels & freight, Inventory purchases…) and **Save** | An expense row is created in the register, already marked paid from this account |
| something you already typed in (quick capture, a bill) | **Match** → under *Expenses entered by hand* press **Link** | The expense is marked paid; nothing is counted twice |
| a customer deposit | **Match** → tick the payment(s) and press **Link selected payments** — one deposit can cover two or three checks | The payments move from *Payments received, not yet deposited* to the bank |
| a customer deposit you never recorded (Zelle straight into checking) | **Match** → **Record a customer payment for this deposit** | Opens the payment form pre-filled with the amount and date; come back and Link it |
| paying a supplier for product | **Match** → tick the receipt(s) and press **Link selected receipts** | The receipt's bill is marked paid from this account; it drops off *Unpaid bills* |
| paying your credit card, moving money between your accounts | **Transfer** — or **Match** → **Pair** next to the twin in the other account | No expense, no income; money just moved |
| one receipt, several things (a Costco run: supplies + inventory + personal) | open the transaction → **Split** → one line per category, amounts must add up | Each expense-category part becomes its own expense row; the personal part is an owner draw |
| personal | **Personal** | An owner draw (money in: an owner contribution). Shows in equity, not on the P&L |
| a duplicate, or not this business | **Ignore** | Disappears from everything, including the balance |
| reviewed wrong | **Undo** (inbox) / **Undo review** (transaction page) | Back to *Needs review*. Everything the decision had linked is released: the bill's paid date is cleared, payments are unlinked, the transfer twin goes back to review, split parts and auto-created expense rows are removed |

Choosing a different action on an already-reviewed row does the same
release first, so you can change your mind without anything being counted
twice.

Tick **Always categorize like this** when you categorize something that
repeats (Pirate Ship, ULINE, Google Workspace, the Chase monthly fee…) and a
**rule** is created. From then on those transactions are categorized the
moment they arrive and never reach the inbox. Rules live under Banking →
Rules, where you can also run them against the whole inbox. A rule can match
with *Contains*, *Starts with*, *Is exactly* or a regular expression; a regex
that repeats a group that already repeats — `(a+)+`, `(\w+\s?)*` — is
refused when you save it (patterns over 200 characters too), because such a
pattern can stall every import. **Vendors** (Expenses → Vendors) do the
lighter version: when a description contains a vendor's name or one of its
aliases, the vendor's default category is suggested and you confirm with one
click.

Until a transaction is reviewed it sits on your statements as
**Uncategorized expense / income** — visible on purpose, so an unworked inbox
cannot quietly make profit look better than it is.

Every Monday morning the **weekly money review email** (Settings → Bank
feed, sent to the alert email) lists what is waiting: transactions to
review and the oldest one, bills due this week, expenses missing receipts,
auto-created expenses to confirm, and connections that need a re-login.

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
  expense; a matching bank transaction later links to it. A monthly template
  keeps its day of the month as the anchor: a template due Jan 31 posts
  Feb 28, then Mar 31 again — it never drifts to the 28th for good.
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
  vendor paid at or above the IRS threshold for the year you pick: **$600
  through tax year 2025, $2,000 from 2026** (the threshold box on the report
  defaults to the right one).
- **Categories** — one per ledger account, each mapped to a Schedule C line
  and a deductible percentage (meals 50%).
- **Budgets** — planned spend per category per month or year; the
  *Expenses by month* report shows actual against budget.
- **Mileage** — business miles with purpose, from/to and round trip at the
  IRS rate from Settings; the deduction appears in the year-end package.
- **Unpaid bills (AP aging)** — open expenses and supplier bills aged by
  due date (a bill with no due date is aged from its bill date), with
  buckets for not yet due / 1–30 / 31–60 / over 60 days late. **Mark paid**
  inline when you pay one by check; let the bank feed do it otherwise.
- **Needs review** — the register's *Needs review* filter
  (`Expenses → ?review=yes`, also the dashboard link) lists rows created by
  a rule or a recurring template that still want a human glance.

### Supplier bills on Receiving

A supplier order (Receiving) is also the supplier's **bill**. On the
Receiving document the **Supplier bill** card holds the supplier's invoice
number, the due date, and — when you paid by check or wire outside the feed —
the paid date, check/ref # and the account it was paid from. Once the order
is posted the bill is open (badge **Unpaid**, **Overdue** after the due date)
until a bank payment is matched to it: in the inbox the payment's **Match**
list offers the receipt, and linking it fills in the paid date and account.
Receiving → **Unpaid bills** filters the list to open bills; the *Unpaid
bills* report shows them together with open expenses.

**Prepayments (allocations, pre-orders).** You can pay a supplier before the
product arrives: link the bank payment to the not-yet-posted receipt (the
Match list marks it *not posted yet*), or type the paid date on the Supplier
bill card. The card shows **Prepaid**, and on the accrual books the money
sits in the asset **Supplier deposits & prepayments** — the cash-vs-accrual
bridge calls it *Paid to suppliers ahead of delivery* — until the receipt is
posted. The rule is applied **per supplier**: a deposit paid to Panini is
shown as a prepayment even while you still owe Topps a bill, so one supplier
never hides behind another.

**Supplier refunds.** When a supplier refunds you, the deposit lands in the
inbox like any other. **Match → Link** it to the receipt it belongs to (the
bill's balance goes back up by the refund), or categorize it to *Inventory
purchases* if it is a general credit; either way the refund reduces your
cost of product, on both bases.

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

Open **Accounting → Month close** (`/accounting/month-close/?month=YYYY-MM`;
the dashboard tile and the Reports page take you to last month). It is a
checklist for one month: the automatic steps show their live count (bank
transactions still to review, expenses to confirm, bills without a due
date, receipts missing) and the manual steps are tick boxes. When every step
is done, **Close the month**; the dashboard tile turns green and the period
lock moves to the month-end. The steps:

1. **Inbox to zero** — every bank transaction reviewed; bank balances on
   Banking → Accounts match the real statements.
2. **Bills** — supplier receipts posted with invoice numbers and due dates;
   recurring expenses confirmed; *Unpaid bills* report looks right.
3. **Receipts** — *Missing receipts* report cleared.
4. **Stock** — spot cycle count; *Month-end inventory valuation* saved.
5. **Statements** — Accounting → Income statement and Balance sheet for the
   month on both bases; *Cash vs. accrual* to understand the gap; *Equity
   trend* to see the direction. Accounting → Year-end package → **Month-end**
   downloads the month's statements as one ZIP if you want to file them.
6. **Sales tax** — Reports → Sales tax report for your filing period
   (monthly/quarterly per Settings) when it is due.
7. **Close the month** on the Month close page (or Accounting → **Close the
   books** through the month-end). Editing a document in a closed period
   then shows a warning (admins can override).

### Year-end (what your tax preparer gets)

Two ZIP packages, both linked from the Reports page and from each other's
page; hand over both.

- **Accounting → Year-end package** (`/accounting/year-end/`): one ZIP for
  the fiscal year with the income statement, balance sheet and cash-vs-accrual
  comparison as **CSV and PDF**, the trial balance on both bases (cash and
  accrual), the general ledger on both bases, the general journal on both
  bases, and a README that tells the preparer which files are cash basis (the
  return) and which are accrual (the true picture). The same ZIP is available
  for any single month.
- **Expenses → Year-end package** (`/expenses/year-end/`): one ZIP with the
  Schedule C worksheet (totals per category with the line and the deductible
  percentage applied), every expense that touches the year with its cash and
  accrual columns, the 1099 vendor list at that year's threshold, the mileage
  deduction, the missing-receipts list, the bills still open on Dec 31, a
  README, and a **`receipts/`** folder holding every receipt file attached to
  an expense (or to the bank transaction it came from) dated in the year.
- **Reports → QuickBooks export** if your preparer wants invoices, payments
  and expenses as plain CSVs: pick From/To dates and the file.
- Close the books through December 31 (Accounting → **Close the books**)
  once the return is filed.

### Other reports (Reports page)

- **Operational P&L (margin view)** — revenue, COGS, gross margin, shipping
  pass-through and expenses from invoice cost snapshots; good for margin
  analysis, not a financial statement.
- **Inventory valuation** as of any date, **revenue/profit by customer**,
  **by day of week**, **sales by SKU/category/brand**, **AR aging**,
  **shipping charges vs. costs**, **sales tax**.
- Every report exports to **CSV**, and the financial statements, expense
  reports and operational reports also to **PDF**. CSV only: the general
  journal and general ledger (your accountant imports them), the equity trend
  (it has a chart on screen instead), the expense register and mileage lists,
  and the QuickBooks files.

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
  on/off, the weekly money review email on/off. The green **Plaid
  configured** badge shows when both keys are present (here or in `.env`);
  otherwise it reads *Not configured — CSV import still works*.
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
