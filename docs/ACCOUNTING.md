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
relies on and a starter set of twenty expense categories mapped to Schedule
C lines. System accounts carry plain-English names: *Payments received, not
yet deposited* (what accountants call undeposited funds), *Accounts
receivable*, *Inventory*, *Supplier deposits & prepayments*, *Accounts
payable — suppliers* / *— carriers*, *Sales tax payable*, *Customer payments
not yet applied*, *Starting balances (opening balance equity)*, *Owner
contributions*, *Owner draws / personal*, *Retained earnings*, *Sales*,
*Shipping charged to customers*, *Cost of goods sold*, *Inventory purchases*,
*Shipping labels & freight*, *Uncategorized income* / *expense*… You can add,
rename and deactivate accounts under Accounting → Chart of accounts; system
accounts can be renamed but not deleted (`setup_accounts` refreshes their
names when the code changes them).

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
shows both side by side with a bridge explaining the difference line by line,
in plain words:

| Bridge row | What it means |
|---|---|
| Invoiced but not yet collected | invoices customers have not paid yet (accrual counts them now) |
| Customer payments not yet applied to an invoice | money received that no invoice has used up yet |
| Inventory bought but not yet sold | product still on the shelf (cash wrote it off when paid) |
| Paid to suppliers ahead of delivery | deposits and prepayments, per supplier |
| Supplier bills received but not yet paid | posted receipts and bills still open |
| Shipping charges not yet paid | label costs on shipments the carrier has not charged yet |
| Sales tax collected but not yet sent to the state | never income on either basis; only timing |

Each row has a one-sentence explanation on the page itself.

---

## 3. Entry rules — where each dollar lands

`Dr` = debit, `Cr` = credit. Asset and expense accounts increase with a
debit; liability, equity and income accounts increase with a credit.

### Both bases

| Event | Entry |
|---|---|
| Bank account opening balance (asset) | Dr Bank · Cr Starting balances (opening balance equity) |
| Credit card opening balance (owed) | Dr Starting balances · Cr Credit card |
| Deposit matched to customer payment(s) | Dr Bank · Cr Payments received, not yet deposited (excess → Uncategorized income; shortfall → Uncategorized expense, e.g. processor fees) |
| Transfer between two of your accounts (incl. paying a card) | Dr receiving account · Cr sending account — one entry for the pair |
| Outflow marked **Personal** (owner draw) | Dr Owner draws / personal · Cr Bank |
| Inflow marked **Personal** (owner contribution) | Dr Bank · Cr Owner contributions |
| Inflow categorized to sales / other income | Dr Bank · Cr that income account |
| Inflow categorized to an expense category (vendor refund) | Dr Bank · Cr that expense category — the register gets a negative expense row |
| Inflow categorized to Inventory purchases (supplier credit) | Dr Bank · Cr Inventory purchases, on both bases |
| **Split** transaction | one line per part: each expense-category part is its own expense row (cash: Dr category; accrual: Dr AP, cleared the same day), other parts follow their account's rule; parts always add up to the transaction |
| Hand-entered transaction (Add by hand) | exactly like an imported one; editing it moves the entry, deleting it (soft) removes it |
| Unreviewed outflow | Dr **Uncategorized expense** · Cr Bank |
| Unreviewed inflow | Dr Bank · Cr **Uncategorized income** |
| Review undone / changed (**Undo**) | the previous decision's links are released first (bill paid date cleared, payments unlinked, twin back to review, split rows removed), so nothing is booked twice |
| Ignored transaction (duplicate, not business) | nothing — invisible everywhere |
| Pending transaction | nothing until it posts |

### Cash basis only

| Event | Entry |
|---|---|
| Customer payment recorded | Dr Payments received, not yet deposited · Cr Sales / Shipping income / Sales tax payable, split in the invoice's proportions (unapplied part → Customer payments not yet applied) |
| Card charge categorized to an expense category (creates an Expense row) | Dr Expense category · Cr Card |
| Expense marked paid by hand (no bank transaction) | Dr Expense category · Cr Bank — or Cr **Owner contributions** when "paid personally". Prefer *Paid personally* or matching the real bank row later: an expense marked "paid from checking" by hand with no bank row behind it makes the ledger's checking balance differ from the register |
| Unpaid expense / bill | nothing (invisible until paid) |
| Bank payment linked to supplier receipt(s), or receipt marked paid | Dr **Inventory purchases** (COGS) · Cr Bank — whether or not the receipt is posted yet (a prepayment is a cost the day it is paid) |
| Supplier refund linked to a receipt | Dr Bank · Cr Inventory purchases |
| Card charge categorized to Shipping labels & freight | Dr Shipping labels & freight · Cr Card |
| Invoice issued | nothing |
| Inventory received or sold | nothing |

### Accrual basis only

| Event | Entry |
|---|---|
| Invoice issued | Dr Accounts receivable · Cr Sales + Shipping income + Sales tax payable |
| Opening-balance (AR adjustment) invoice | Dr Accounts receivable · Cr Starting balances (opening balance equity) |
| Customer payment applied | Dr Payments received, not yet deposited · Cr Accounts receivable (unapplied part → Customer payments not yet applied) |
| Credit memo applied | Dr Sales returns · Cr Accounts receivable |
| Supplier receipt posted | Dr Inventory · Cr Accounts payable — suppliers (landed cost incl. freight), tagged with the supplier |
| Supplier receipt paid (bank link or by hand) | Dr Accounts payable — suppliers · Cr Bank, tagged with the supplier. Paid before posting = that supplier's AP goes into debit = a prepayment (see true-ups) |
| Supplier refund linked to a receipt | Dr Bank · Cr Accounts payable — suppliers (the bill is owed again) |
| PO finalized (stock leaves) | Dr Cost of goods sold · Cr Inventory, at the weighted-average cost snapshot |
| Credit memo restock | Dr Inventory · Cr Cost of goods sold |
| Adjustment: damaged / shrinkage / correction / cycle count / revaluation | Dr Inventory adjustments · Cr Inventory (or reverse) |
| Adjustment: opening balance | Dr Inventory · Cr Starting balances (opening balance equity) |
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
2. If **Accounts payable — suppliers** has a *debit* balance for a supplier
   (you paid that supplier more than you have received from them —
   pre-orders, allocations, deposits), that amount is shown as the asset
   **Supplier deposits & prepayments** instead of a negative liability. This
   is judged **per supplier**: a $5,000 deposit paid to Panini is a
   prepayment even while a $3,000 Topps bill is still open; the two never
   net against each other. The cash-vs-accrual bridge shows it as *Paid to
   suppliers ahead of delivery*.
3. If **Accounts payable — carriers** has a debit balance (you paid Pirate
   Ship/USPS but never recorded the label cost on a shipment), the excess is
   simply shipping expense.

### Equity

> Equity = Starting balances (opening balance equity) + Owner contributions
> − Owner draws + Retained earnings (prior fiscal years) + Net income, fiscal
> year to date

Retained earnings are computed, never posted. The fiscal year starts in the
month set under Settings → Accounting.

---

## 4. How each report is produced

All reports live under **Accounting** (and are linked from the Reports hub).
Every one accepts a basis toggle and a period or as-of date. Exports: the
statements export to **CSV and PDF**; the general ledger, the journal and the
equity trend export to **CSV only** (the trend has a chart on screen).

| Report | What it shows | How | Export |
|---|---|---|---|
| **Income statement (P&L)** | Income, cost of goods sold, gross profit, each expense category, net income for a period | Sums every journal line dated in the period by account; gross margin % included | CSV, PDF |
| **Balance sheet** | Assets, liabilities, equity as of a date; always balanced | Cumulative balances through the date; retained earnings and YTD net income computed from the P&L | CSV, PDF |
| **Cash vs. accrual** | Both P&Ls side by side, the difference per account, and the plain-English bridge above | Two journals for the same period | CSV, PDF |
| **Equity trend** | Month-end equity, assets, liabilities and monthly net income for 12 months, with a chart | One journal with a checkpoint at each month end | CSV |
| **Trial balance** | Every account's debit or credit balance as of a date | Raw cumulative balances | CSV, PDF |
| **General ledger** | Every entry touching one account in a period, running balance, link to the source document | Entries filtered by account | CSV |
| **Journal** | Every line in a period (audit / export for your accountant) | All entries, flat | CSV |
| **Month close** | One month's closing checklist: live counts for the automatic steps (transactions to review, expenses to confirm, bills without a due date, missing receipts), tick boxes for the manual ones, *Close the month* | `/accounting/month-close/?month=YYYY-MM`; `accounting.closing.dashboard_tile()` feeds the dashboard ("September 2026: 4 of 6 done") | — |
| **Year-end package** | One ZIP for the fiscal year (or any month): P&L, balance sheet and cash-vs-accrual as CSV **and PDF**, trial balance on both bases, general ledger on both bases, general journal on both bases, README | Runs the above for the period | ZIP |
| **Dashboard → Money** | Cash position, net profit MTD/YTD on both bases, equity and its change vs. last month-end, "needs attention", last month's close progress | One journal build per basis (`dashboard_snapshot`) | — |

The **Expenses → Year-end package** is the companion ZIP (Schedule C
worksheet, every expense, 1099 vendors at that year's threshold — $600
through tax year 2025, $2,000 from 2026 — mileage, missing receipts, bills
open at Dec 31, and a `receipts/` folder with every attached receipt). Both
packages link to each other and to the Reports page.

The **Operational P&L** under Reports is a different, older view built from
invoice line cost snapshots and shipment costs. It is useful for margin
analysis per period but is not a financial statement; the Accounting P&L is.

### Period lock

Accounting → **Close the books** lets you set a date through which the books
are considered closed (typically after your preparer has the year-end
package); closing a month on the Month close page moves it too.
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
work this way — the Supplier bill card shows **Prepaid**), or you linked a
bank payment to a receipt whose landed cost is smaller than what you paid. A
debit balance in that supplier's AP is really money the supplier is holding
for you, so at each period end the engine reclassifies it, **supplier by
supplier**, to the asset *Supplier deposits & prepayments* (*Paid to
suppliers ahead of delivery* on the bridge). When the product arrives and
the receipt is posted, that supplier's AP goes back toward zero and
inventory goes up. If it never clears, check whether the receipt's
freight/duties were entered, or whether the payment was linked to the wrong
supplier's receipt.

**Why doesn't my bank balance on the balance sheet match the bank?**
The balance sheet's bank account includes expenses or supplier bills you
marked "paid from" that account **by hand** that have not yet appeared in
the feed (or were never imported). The Banking → Accounts balance is the
feed only. Once the real charge arrives, **Match → Link** it to the expense
(the review inbox suggests it) instead of categorizing it fresh, and both
agree. If the money really never left a business account, edit the expense
and choose *Paid personally (owner)*. Also check for transactions still
marked pending, and for ignored rows.

**I reviewed something wrong. Will undoing it leave a mess?**
No. **Undo** (or simply choosing a different action) first releases
everything the old decision had linked: the supplier bill's paid date is
cleared, customer payments go back to *Payments received, not yet
deposited*, the transfer twin returns to the inbox, split parts and the
expense rows they created are removed. The statements reflect only the new
decision.

**One card charge covers several things.**
Open the transaction and **Split** it: one line per category with amounts
that add up to the charge. Each expense-category part becomes its own
expense row (with its own receipt if you like); a *Personal* part is an
owner draw; an *Inventory purchases* part is cost of product.

**A supplier refunded me.**
The refund arrives as a deposit. **Match → Link** it to the receipt it
belongs to and the bill's balance goes back up (accrual: AP is owed again;
cash: inventory purchases go down). A general credit with no receipt behind
it can be categorized straight to *Inventory purchases*.

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
