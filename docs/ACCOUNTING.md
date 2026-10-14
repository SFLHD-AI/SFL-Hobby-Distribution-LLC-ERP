# SFL ERP System — Accounting Guide

This document explains how the books work: where every dollar lands, why
there are two sets of numbers (cash and accrual), how each statement is
produced, and the answers to the questions owners ask most.

Written for a business owner, not an accountant. Your tax preparer can read
the tables below and will recognise standard double-entry bookkeeping.

---

## 1. The virtual ledger

Traditional accounting software has you *post* journal entries into a
separate ledger that then has to be reconciled against your orders, invoices
and bank statements. SFL ERP System does not store a ledger at all.

Instead, `accounting/ledger.py` **rebuilds a balanced double-entry journal
on demand** from the documents you already keep:

| Document | App | Where it comes from |
|---|---|---|
| Bank / credit card transactions | banking | Plaid sync, CSV import, typed in |
| Bank account opening balances | banking | Banking → Accounts |
| Customer payments and how they were applied | payments | Invoices → Record payment |
| Invoices and credit memos | invoicing | Orders → Create invoice |
| Stock movements (receipts, sales, adjustments, counts) | inventory | Receiving, PO finalization, adjustments |
| Supplier receipts (bills) | inventory | Receiving |
| Shipment label / freight costs | shipping | Shipping screen, ShipStation sync |
| Expenses | expenses | Bank feed, quick capture, recurring templates, hand entry |

Because nothing is posted, nothing can drift. Void an invoice, re-categorize
a card charge or fix a receipt cost and every statement reflects it the next
time it is opened. There is no "re-post", "re-sync" or "rebuild" button
because there is nothing to rebuild.

The engine is `Journal(basis, checkpoints)`. It reads every document once,
emits one balanced entry per document (debits always equal credits), and
answers questions about any date range: `income_statement(start, end)`,
`balance_sheet(as_of)`, `trial_balance(as_of)`, `general_ledger(account,
start, end)`. Twenty-plus automated tests assert that every statement
balances under every combination of documents.

### Chart of accounts

`python manage.py setup_accounts` creates the **system accounts** the engine
relies on (cash on hand, undeposited funds, AR, inventory, AP, sales tax
payable, owner equity, sales, COGS, shipping, uncategorized…) and a starter
set of twenty expense categories mapped to Schedule C lines. You can add,
rename and deactivate accounts under Accounting → Chart of accounts; system
accounts can be renamed but not deleted.

Two kinds of accounts are created for you automatically:

- one **expense account per expense category** (so the P&L, the bank review
  dropdown and the expense register all use the same names), and
- one **bank or credit-card account per bank account** you add under Banking.

---

## 2. Cash basis vs. accrual basis

Both bases are produced from the **same documents**. Only the *timing* of
when a dollar counts differs.

**Cash basis — what your tax return uses.**
Income counts when the customer's money arrives. Costs count when money
leaves a bank or card account (or when you mark an expense or supplier bill
paid). There is no accounts receivable, no accounts payable and no inventory
asset on a cash-basis balance sheet: product you bought is a cost the day you
paid for it, even if it is still on the shelf.

**Accrual basis — the true economic picture.**
Revenue counts when you invoice (and sits in accounts receivable until
collected). Inventory is an asset at weighted-average landed cost and becomes
cost of goods sold only when it is sold. Supplier bills and expenses are
liabilities (accounts payable) from the day you receive them until paid.
This is the basis that tells you whether the business is **building equity**.

Pick which one you see first under Settings → Accounting → "Show first". Every
statement has a Cash / Accrual toggle, and **Accounting → Cash vs. accrual**
shows both side by side with a bridge explaining the difference line by line
(change in receivables, in inventory, in unpaid bills…).

---

## 3. Entry rules — where each dollar lands

`Dr` = debit, `Cr` = credit. Asset and expense accounts increase with a
debit; liability, equity and income accounts increase with a credit.

### Both bases

| Event | Entry |
|---|---|
| Bank account opening balance (asset) | Dr Bank · Cr Opening balance equity |
| Credit card opening balance (owed) | Dr Opening balance equity · Cr Credit card |
| Deposit matched to customer payment(s) | Dr Bank · Cr Undeposited funds (excess → Uncategorized income; shortfall → Uncategorized expense, e.g. processor fees) |
| Transfer between two of your accounts (incl. paying a card) | Dr receiving account · Cr sending account — one entry for the pair |
| Outflow categorized to an owner draw | Dr Owner draws · Cr Bank |
| Inflow categorized to owner contribution | Dr Bank · Cr Owner contributions |
| Inflow categorized to sales / other income | Dr Bank · Cr that income account |
| Unreviewed outflow | Dr **Uncategorized expense** · Cr Bank |
| Unreviewed inflow | Dr Bank · Cr **Uncategorized income** |
| Ignored transaction (duplicate, not business) | nothing — invisible everywhere |
| Pending transaction | nothing until it posts |

### Cash basis only

| Event | Entry |
|---|---|
| Customer payment recorded | Dr Undeposited funds · Cr Sales / Shipping income / Sales tax payable, split in the invoice's proportions (unapplied part → Customer deposits) |
| Card charge categorized to an expense category (creates an Expense row) | Dr Expense category · Cr Card |
| Expense marked paid by hand (no bank transaction) | Dr Expense category · Cr Bank — or Cr **Owner contributions** when "paid personally" |
| Unpaid expense / bill | nothing (invisible until paid) |
| Bank payment linked to supplier receipt(s), or receipt marked paid | Dr **Inventory purchases** (COGS) · Cr Bank |
| Card charge categorized to Shipping labels & freight | Dr Shipping labels & freight · Cr Card |
| Invoice issued | nothing |
| Inventory received or sold | nothing |

### Accrual basis only

| Event | Entry |
|---|---|
| Invoice issued | Dr Accounts receivable · Cr Sales + Shipping income + Sales tax payable |
| Opening-balance (AR adjustment) invoice | Dr Accounts receivable · Cr Opening balance equity |
| Customer payment applied | Dr Undeposited funds · Cr Accounts receivable |
| Credit memo applied | Dr Sales returns · Cr Accounts receivable |
| Supplier receipt posted | Dr Inventory · Cr Accounts payable — suppliers (landed cost incl. freight) |
| Supplier receipt paid (bank link or by hand) | Dr Accounts payable · Cr Bank |
| PO finalized (stock leaves) | Dr Cost of goods sold · Cr Inventory, at the weighted-average cost snapshot |
| Credit memo restock | Dr Inventory · Cr Cost of goods sold |
| Adjustment: damaged / shrinkage / correction / cycle count / revaluation | Dr Inventory adjustments · Cr Inventory (or reverse) |
| Adjustment: opening balance | Dr Inventory · Cr Opening balance equity |
| Adjustment: personal use | Dr Owner draws · Cr Inventory |
| Expense incurred (its `date`) | Dr Expense category · Cr Accounts payable |
| Expense paid (its `paid_date`, or the linked bank transaction) | Dr Accounts payable · Cr Bank / Card (or Owner contributions when paid personally) |
| Shipment with a label/freight cost | Dr Shipping labels & freight · Cr Accounts payable — carriers |
| Card charge categorized to Shipping labels & freight | Dr Accounts payable — carriers · Cr Card |
| Card charge categorized to Inventory purchases | Dr Accounts payable — suppliers · Cr Card |

### Checkpoint true-ups (accrual)

At each period end you report on, the engine applies three corrections so
the balance sheet tells the truth even when documents are imperfect:

1. **Inventory** is set to the exact stock-ledger valuation on that date
   (Σ on-hand × average cost). Any difference goes to *Inventory
   adjustments & shrinkage*.
2. If **Accounts payable — suppliers** has a *debit* balance (you paid
   suppliers more than you have received — pre-orders, allocations,
   deposits), that amount is shown as the asset **Supplier deposits &
   prepayments** instead of a negative liability.
3. If **Accounts payable — carriers** has a debit balance (you paid Pirate
   Ship/USPS but never recorded the label cost on a shipment), the excess is
   simply shipping expense.

### Equity

> Equity = Opening balance equity + Owner contributions − Owner draws
> + Retained earnings (prior fiscal years) + Net income, fiscal year to date

Retained earnings are computed, never posted. The fiscal year starts in the
month set under Settings → Accounting.

---

## 4. How each report is produced

All reports live under **Accounting** (and are linked from the Reports hub).
Every one accepts a basis toggle, a period or as-of date, and exports to CSV
and PDF.

| Report | What it shows | How |
|---|---|---|
| **Income statement (P&L)** | Income, cost of goods sold, gross profit, each expense category, net income for a period | Sums every journal line dated in the period by account; gross margin % included |
| **Balance sheet** | Assets, liabilities, equity as of a date; always balanced | Cumulative balances through the date; retained earnings and YTD net income computed from the P&L |
| **Cash vs. accrual** | Both P&Ls side by side, the difference per account, and a bridge (Δ AR, Δ inventory, Δ AP, Δ sales tax…) | Two journals for the same period |
| **Equity trend** | Month-end equity, assets, liabilities and monthly net income for 12 months, with a chart | One journal with a checkpoint at each month end |
| **Trial balance** | Every account's debit or credit balance as of a date | Raw cumulative balances |
| **General ledger** | Every entry touching one account in a period, running balance, link to the source document | Entries filtered by account |
| **Journal** | Every line in a period (audit / export for your accountant) | All entries, flat |
| **Year-end package** | Statements, trial balance, GL and journal for the fiscal year in one place, plus the checklist | Runs the above for the fiscal year |
| **Dashboard → Money** | Cash position, net profit MTD/YTD on both bases, equity and its change vs. last month-end, "needs attention" | One journal build per basis (`dashboard_snapshot`) |

The **Operational P&L** under Reports is a different, older view built from
invoice line cost snapshots and shipment costs. It is useful for margin
analysis per period but is not a financial statement; the Accounting P&L is.

### Period lock

Accounting → Close books lets you set a date through which the books are
considered closed (typically after your preparer has the year-end package).
Editing or adding a document dated on or before that date shows a warning;
admins can override. It is a soft lock — nothing is frozen in the database —
so a genuine correction is still possible and still flows into the
statements.

---

## 5. FAQ

**Why is my cash profit different from my accrual profit?**
Because timing. A typical month for a distributor: you paid a supplier
$30,000 for product that is still mostly on the shelf, and you invoiced
$12,000 that customers have not paid yet. Cash basis says you lost $30,000
and earned nothing; accrual says you earned $12,000 of revenue, your
inventory asset went up $30,000, and you owe or paid for it. Open
**Accounting → Cash vs. accrual** — the bridge lists each reason with the
dollar amount. Over a long enough period the two converge; in any given
month they rarely match.

**Which number do I give my tax preparer?**
The cash-basis statements (you are a cash-basis taxpayer). Use the Year-end
package with the basis set to Cash. Keep the accrual statements for yourself
and your lender.

**Where do unreviewed transactions show up?**
On both bases, in two catch-all accounts: outflows as **Uncategorized
expense**, inflows as **Uncategorized income**. They are deliberately visible
so an unreviewed inbox cannot quietly inflate profit — a $500 unreviewed card
charge *lowers* profit until you categorize it. The dashboard, the Banking
badge in the sidebar and the weekly review email all count them. Review the
inbox and the lines disappear from "Uncategorized" and land where they
belong.

**Why is accounts payable negative, or why do I see "Supplier deposits &
prepayments"?**
You paid a supplier before posting the receipt (allocations and pre-orders
work this way), or you linked a bank payment to a receipt whose landed cost
is smaller than what you paid. A debit balance in AP is really money the
supplier is holding for you, so at each period end the engine reclassifies
it to the asset *Supplier deposits & prepayments*. When the product arrives
and the receipt is posted, AP goes back toward zero and inventory goes up.
If it never clears, check whether the receipt's freight/duties were entered.

**Why doesn't my bank balance on the balance sheet match the bank?**
The balance sheet's bank account includes expenses or supplier bills you
marked "paid from" that account **by hand** that have not yet appeared in
the feed (or were never imported). The Banking → Accounts balance is the
feed only. Once the real charge arrives, match it to the expense (the review
inbox suggests it) instead of categorizing it fresh, and both agree. Also
check for transactions still marked pending, and for ignored rows.

**I paid for something with my personal card. What do I do?**
Enter the expense (quick capture works) and choose payment method *Paid
personally (owner)*. It counts as a business expense on both bases and the
money shows as an **Owner contribution** in equity — exactly what your
preparer wants to see.

**A customer paid by Zelle straight into checking. The deposit shows up but
I never recorded a payment.**
Record the payment on the invoice first (Invoices → Record payment), then
match the deposit to it in the inbox. Until then the deposit is
*Uncategorized income*, which is still correct on the cash basis — it just
is not applied to the invoice.

**I paid my credit card from checking. Is that an expense?**
No, and the system will not let it become one. Mark the pair as a
**transfer**; the inbox finds the twin automatically. The charges on the
card are the expenses; the payment just moves money.

**Why does the equity trend dip in a month where I bought a lot of stock?**
On the accrual basis it should not — inventory is an asset — unless the
stock was received but the receipt was not posted, or the purchase was
categorized straight to *Inventory purchases* instead of being matched to a
posted receipt. Match supplier payments to receipts and the dip disappears.
On the cash basis a big stock purchase is a cost in the month paid, so that
trend will dip; that is the point of looking at both.

**Can I edit a closed period?**
Yes, with a warning (period lock is soft). Tell your preparer if the change
affects a year they have already filed.

**How exact is inventory valuation?**
Exact to the stock movement ledger: each movement stores the average cost
after it, so the value on any date is Σ (last on-hand × last average cost)
per SKU — the same number the Month-end inventory valuation report shows.
