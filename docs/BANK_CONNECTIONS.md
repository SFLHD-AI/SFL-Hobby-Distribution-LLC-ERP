# SFL ERP System — Bank Connections

How to get your checking account and credit card activity into the system:
automatically through **Plaid**, or by **importing the CSV** every bank lets
you download. Both land in the same review inbox and both run your category
rules. You can mix them (Plaid for the Chase accounts, CSV for an Amex).

---

## 1. Plaid (automatic, hourly)

Plaid is the service most small-business accounting tools use to read bank
transactions. You create a free developer account, paste two keys into SFL
ERP System, and then log in to your bank *through Plaid's widget* inside the
app — your bank password is never stored here. From then on new transactions
arrive every hour.

### 1.1 Create the Plaid account

1. Go to `https://dashboard.plaid.com/signup` and sign up (company name =
   your LLC; "I'm building for my own business" / developer is fine).
2. Verify your email and finish the short onboarding.
3. **Team Settings → Keys** (left sidebar). You will see one **client_id**
   and three secrets: *Sandbox*, *Development* (retired for new accounts) and
   *Production*.

### 1.2 Sandbox first (recommended — 10 minutes)

Sandbox is a set of fake banks with fake transactions. It lets you see the
whole flow — connect, sync, review, rules — without touching real money.

1. In SFL ERP System open **Settings → Bank feed (Plaid)**. Paste the
   `client_id` and the **Sandbox** secret, leave *Plaid environment* on
   **Sandbox**, save. The card shows a green **Plaid configured** badge.
2. **Banking → Connect a bank**. Plaid's widget opens; search for any bank
   (e.g. "Chase"), log in with user **`user_good`** and password
   **`pass_good`**. Pick the accounts to link.
3. Each linked account becomes a Bank account under Banking → Accounts. The
   first sync sets its opening date to the earliest transaction and its
   opening balance so today's balance matches the bank.
4. Press **Sync now** (a button on the inbox and on the dashboard's *Bank &
   card balances* card; it is a POST, so it is never triggered by a bookmark
   or a link preview) or wait for the hourly job. Transactions appear in the
   inbox. Try the review actions and a rule.
5. When you are done playing, press **Disconnect** next to the sandbox
   connection on Banking → **Connect a bank** and deactivate the fake
   accounts.

### 1.3 Production (your real banks)

1. In the Plaid dashboard open **Production access** (or "Apply for
   production") and fill in the application. Choose **Transactions** as the
   only product. Describe the use case honestly: "internal bookkeeping for
   my own wholesale business — reading my own accounts". Small businesses
   are usually approved within one to two business days. Production has a
   small per-connected-account monthly fee; a couple of accounts costs a few
   dollars a month.
2. Once approved, copy the **Production** secret into Settings → Bank feed,
   switch *Plaid environment* to **Production**, save.
3. Make sure `PUBLIC_BASE_URL` in `.env` is your real `https://` address
   (Plaid Link requires HTTPS in production — see `docs/DEPLOYMENT.md`).
4. **Banking → Connect a bank** and log in to your real bank. Link the
   business checking account and every business credit card. Do **not**
   link personal accounts; personal purchases on business cards are handled
   in the inbox (press **Personal**).
5. Set each new account's **opening date** and **opening balance** if you
   want history to start at your books start date rather than at the
   earliest transaction Plaid delivered (Banking → Accounts → edit).

### 1.4 How sync works

- `banking.tasks.sync_all_connections` runs **hourly** (registered by
  `setup_schedules`, executed by the `worker` container / `qcluster`). You
  can also press **Sync now** on the inbox or the dashboard.
- Plaid sends *added*, *modified* and *removed* transactions through a
  cursor, so nothing is fetched twice. Pending card authorizations arrive
  marked **pending** and are updated in place (same row, review work kept)
  when they post; pending rows are shown but not booked.
- Every new row is run through your **category rules**, then **vendor
  recognition** (the vendor's name and aliases), then a **learned
  suggestion** (what you chose for the same description before). Rules with
  "apply automatically" off only suggest; everything else lands in the
  inbox as *Needs review*.
- If a transaction that was also imported from a CSV earlier is delivered by
  Plaid, the existing row is adopted (matched on account + date + amount +
  normalized description) rather than duplicated.
- **Needs re-authentication**: banks force a fresh login every so often
  (Chase about every 90 days). The connection shows **Re-login needed** on
  Banking → Accounts and on *Connect a bank*, and the dashboard "needs
  attention" card and the weekly email say so. Press **Re-login** on
  Banking → **Connect a bank** (next to the connection). Nothing is lost;
  syncing resumes where it stopped.
- Turn the hourly pull off with Settings → Bank feed → "Pull new
  transactions automatically" (useful while you are catching up by CSV).

---

## 2. CSV import (any bank, no setup)

**Banking → Import CSV**: pick the account, upload the file, check the
preview (new / duplicate / before opening date), tick *Flip signs* only if
the warning tells you to, import. Duplicates are detected by a fingerprint
of account + date + amount + description, so re-importing an overlapping
date range is safe — overlap generously rather than risk a gap.

Presets recognise the big banks' files automatically from the column
headers; a *Generic* mapping lets you pick the date / description / amount
(or debit + credit) columns for anything else. Rows dated before the
account's opening date are skipped.

**Amex and other "charges are positive" cards.** Amex lists purchases as
positive numbers. Its full export (Date, Description, Card Member, Account #,
Amount, and more with *additional details* ticked) is recognised and the
signs are flipped for you. A bare **three-column** export (Date,
Description, Amount) is not specific enough to tell apart from other banks:
map the columns yourself in the Generic picker and tick **Amounts are
positive for charges (money out)**, or tick *Flip signs* on the preview.

### Where the download lives, bank by bank

Websites move buttons around; the words to look for are *Download*,
*Export* or *Statements & Activity*, next to the transaction list, with a
file-type choice of **CSV** (sometimes called "Excel/CSV" or "spreadsheet").

| Bank | Steps | Notes |
|---|---|---|
| **Chase** (checking, Ink cards) | Log in → pick the account → above the activity list click the **download** icon (↓) → *File type: CSV*, *Activity: choose a date range* → Download | Checking files have Details / Posting Date / Description / Amount / Type / Balance; card files have Transaction Date / Post Date / Description / Category / Type / Amount. Both are signed correctly (charges negative). Max range is about 2 years. |
| **American Express** | Statements & Activity → *Download* (top right) → *CSV*, tick *Include all additional transaction details*, pick the statement or date range | Amex lists **charges as positive** and payments as negative. The full export is recognised and flipped for you; a bare 3-column Date/Description/Amount file must be mapped by hand with *Amounts are positive for charges (money out)* ticked (see above). |
| **Capital One** | Account → *Download Transactions* (on the account page) → *CSV*, choose dates | Separate Debit and Credit columns; handled by the preset. |
| **Bank of America** | Account → above the transaction list choose *Download* → *File type: Microsoft Excel / CSV*, date range | The file starts with a summary block (beginning balance etc.) above the real header — the importer skips it. |
| **Wells Fargo** | Account → *Download Account Activity* (right side) → *Comma Delimited*, date range | The file has **no header row**: date, amount, *, check #, description. Pick the Wells Fargo preset. |
| **Citi** | Account → *Export* (above activity) → *CSV*, date range | Has a Status column; rows marked *Pending* are skipped on import. Debit/Credit columns. |
| **Discover** | Activity & Statements → *Download* → *CSV*, choose a statement or date range | **Charges are positive** in Discover files; the preset flips them. |
| **PayPal** (business) | Activity → *Statements* (right) → *Custom* → date range, *Transaction type: Balance affecting*, *File type: CSV* → Create report, then download it from the list | Gross / Fee / Net columns: the importer uses **Net** (what actually hit your balance) and puts the fee in the description. Link PayPal as its own "Payment processor" account; transfers to checking are transfers. |
| **Square** | Dashboard → Transactions → *Export* → *Transactions CSV*, date range (or Balance → Transfers for payouts) | Use the Square preset for sales; treat payouts to checking as transfers. |
| Anything else | Download whatever CSV the bank offers | Use the *Generic* mapping and tell the importer which columns are date, description and amount (or debit + credit). |

**How often?** Weekly is plenty for CSV; the inbox is faster to work when
it is short. Overlap the previous import by a few days every time.

### Typed in by hand

Banking → **+ Add by hand** records a cash purchase, a check you wrote or
anything the feed missed. It lands in the inbox like any other row. Rows
you typed in are the only ones that can be **edited** (date, amount,
description) or **deleted** afterwards, from the transaction page; imported
and synced rows are the bank's record and can only be ignored.

---

## 2b. Working the inbox

- **Save** categorizes (pick the category, optionally a vendor and a note).
  Tick **Always categorize like this** to create a rule on the spot.
- **Match** opens the transaction page's *Match to something already
  recorded* list: customer payments (**Link selected payments**; groups of
  checks that add up to the deposit are offered as one button), supplier
  receipts (**Link selected receipts**, unposted ones marked *not posted
  yet* for prepayments), hand-entered expenses (**Link**) and the transfer
  twin in another account (**Pair**).
- **Record a customer payment for this deposit** (on a deposit's page) opens
  the payment form pre-filled with the amount and date for money a customer
  sent that you never recorded; Link it when you come back.
- **Split** divides one transaction across several categories (a Costco run:
  supplies + inventory + personal). The parts must add up to the amount;
  each expense-category part becomes its own expense row.
- **Transfer** pairs the row with its twin in another account (card
  payments, moving money between accounts).
- **Personal** books an owner draw (money out) or an owner contribution
  (money in).
- **Ignore** hides a duplicate or something that is not this business.
- **Undo** / **Undo review** sends a reviewed row back and releases
  everything that decision had linked (bill paid dates, payments, twin,
  split parts). Picking a different action on a reviewed row does the same
  release first.

**Rules** (Banking → Rules) match on *Contains*, *Starts with*, *Is exactly*
or a regular expression. A regex that repeats a group that already repeats,
such as `(a+)+` or `(\w+\s?)*`, is refused when you save it, as is any
pattern longer than 200 characters; such patterns can take seconds per row
and stall every import and sync. Use *Contains* for almost everything.

---

## 3. Troubleshooting

**Everything imported twice.**
The same transaction described two ways (e.g. "PIRATE SHIP" in one file,
"PIRATE SHIP 844-5553355 PA" in another, or a card export with and without
the "additional details" option) does not fingerprint the same. Open the
inbox filtered to the second import batch and **Ignore** the duplicates
(ignored rows count toward nothing, not even the balance). Next time export
with the same options. Plaid never duplicates; if you move from CSV to Plaid
the overlap is adopted automatically.

**Balances are moving the wrong way / charges show as income.**
The file lists money out as positive. On the import screen the preview
shows a warning and a *Flip signs* checkbox — tick it. If you already
imported, open the batch from the inbox filter, select all → *Ignore*, then
re-import with the flip. Sign convention everywhere in the app: **positive
= money into the account, negative = money out**, for cards too (a charge
is negative; a payment to the card is positive).

**The credit card balance is negative, or far too high.**
The opening balance for a card is the amount **owed**, entered as a
positive number, as of the opening date. If you entered it negative (or
entered the available credit) edit the account. Also check for a missing
month of history between the opening date and the first import.

**The checking balance here does not match the bank.**
1. Opening balance / opening date wrong — compare with the statement on
   that date and fix it on the account.
2. A gap or an overlap in imports — Banking → Accounts shows the last
   transaction date; import the missing range.
3. Pending transactions — the bank's "current" balance usually excludes
   them, and so does this app; its "available" balance includes them.
4. Rows you ignored that were real, or real rows you deleted.

**The balance sheet's bank account differs from Banking → Accounts.**
Expenses or supplier bills marked *paid from* this account by hand are on
the books but not in the feed yet. When the real charge arrives, **Match →
Link** it to the expense (the inbox suggests it) instead of categorizing it
fresh. See `docs/ACCOUNTING.md` FAQ.

**A transfer keeps showing as an expense.**
Paying the card from checking is two rows (checking −, card +). Open either
one → **Transfer** — the twin within a few days with the opposite amount is
suggested (**Pair**). Mark it once and create a rule ("CHASE CREDIT CRD
AUTOPAY" → transfer) so it never asks again.

**A deposit is bigger or smaller than the payments it covers.**
Match it to the payments anyway. The difference is booked as
*Uncategorized income* (over) or *Uncategorized expense* (under — typical for
Square/PayPal fees); categorize the fee part later or create a *Bank &
merchant fees* expense.

**Plaid says ITEM_LOGIN_REQUIRED / connection needs attention.**
Normal; the bank wants a fresh login. Banking → **Connect a bank** →
**Re-login** next to the connection. If it keeps happening, your bank may
require you to enable third-party access in its own security settings
(Chase: Profile & settings → Security → Linked apps & websites).

**Plaid Link does not open in production.**
The page must be served over HTTPS and `PUBLIC_BASE_URL` must match the
address in the browser. In the Plaid dashboard, *Team Settings → Allowed
redirect URIs* must include `https://your-domain/banking/connect/`.

**I connected the wrong account / want to start over.**
Banking → **Connect a bank** → **Disconnect** next to the connection.
Accounts and their transactions are kept (soft-deleted when you deactivate
them) so nothing you reviewed is lost; the ledger ignores inactive
accounts' transactions only if you ignore them too.

**I categorized / matched something wrong.**
Press **Undo** on the row (or **Undo review** on its page). The previous
decision is fully released — the bill's paid date is cleared, payments are
unlinked, a transfer twin goes back to review, split parts and the expense
rows they created are removed — and the row is back in *Needs review*.

**The file will not parse.**
The importer tries UTF-8, Windows-1252 and Latin-1 and sniffs commas,
semicolons and tabs. If it still fails the file is probably an Excel
workbook (`.xlsx`) or a PDF statement; open it in Excel/Numbers and *Save
as CSV (comma delimited)*. Quicken `.qfx/.ofx` files are not supported —
pick CSV on the bank's download screen instead.
