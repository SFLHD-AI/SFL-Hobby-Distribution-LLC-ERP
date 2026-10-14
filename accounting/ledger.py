"""
The virtual general ledger.

Every financial statement in SFL ERP System is produced by rebuilding a
balanced, double-entry journal from the operational documents on demand.
Nothing is posted and nothing is stored, so the books can never drift from
the orders, invoices, payments, receipts, bank feeds and expenses that
*are* the business. Edit or void a document and every report follows.

Two bases are produced from the same documents:

**Cash basis** — what your tax return uses. Income counts when a customer
payment is recorded; costs count when money leaves a bank/card account
(or when an expense/receipt is marked paid). There is no AR, AP or
inventory on a cash-basis balance sheet.

**Accrual basis** — the true economic picture. Revenue counts when
invoiced (AR until collected), inventory is an asset at weighted-average
landed cost and becomes COGS when sold, supplier bills are AP until paid.
This is the basis that tells you whether you are *building equity*.

Entry rules (where each dollar lands) are documented per source below and
summarised in docs/ACCOUNTING.md. The public API:

    Journal(basis, checkpoints=[dates...])   # builds everything once
      .balances_through(date)                # {account_id: debits - credits}
      .income_statement(start, end)
      .balance_sheet(as_of)
      .trial_balance(as_of)
      .general_ledger(account, start, end)
    income_statement(basis, start, end), balance_sheet(basis, as_of), ...
    compare_bases(start, end), equity_trend(basis, months)
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.utils import timezone

from accounting.models import (ACCRUAL, CASH, Account, AccountSubtype, AccountType,
                               DEBIT_TYPES, PNL_TYPES)

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def q(x):
    return Decimal(x).quantize(CENTS, ROUND_HALF_UP)


def _local_date(dt):
    if dt is None:
        return None
    if isinstance(dt, date) and not hasattr(dt, "hour"):
        return dt
    return timezone.localtime(dt).date()


def month_end(d):
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def fiscal_year_start(as_of, start_month=None):
    """First day of the fiscal year containing ``as_of``."""
    if start_month is None:
        from core.models import CompanySettings
        start_month = CompanySettings.get().fiscal_year_start_month or 1
    year = as_of.year if as_of.month >= start_month else as_of.year - 1
    return date(year, start_month, 1)


# ---------------------------------------------------------------------------
# Journal data structures
# ---------------------------------------------------------------------------
@dataclass
class Line:
    account_id: int
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass
class Entry:
    date: date
    memo: str
    source_type: str = ""
    source_id: str = ""
    source_url: str = ""
    lines: list = field(default_factory=list)

    def add(self, account, debit=ZERO, credit=ZERO):
        debit, credit = q(debit), q(credit)
        if debit < 0:                 # normalise negatives to the other side
            credit, debit = credit - debit, ZERO
        if credit < 0:
            debit, credit = debit - credit, ZERO
        if debit == 0 and credit == 0:
            return
        acct_id = account.pk if isinstance(account, Account) else account
        self.lines.append(Line(acct_id, debit, credit))

    def pair(self, debit_account, credit_account, amount):
        """Dr one account / Cr another for ``amount`` (negative flips sides)."""
        amount = q(amount)
        if amount == 0:
            return
        if amount < 0:
            debit_account, credit_account, amount = credit_account, debit_account, -amount
        self.add(debit_account, debit=amount)
        self.add(credit_account, credit=amount)

    @property
    def total_debits(self):
        return sum((l.debit for l in self.lines), ZERO)

    @property
    def total_credits(self):
        return sum((l.credit for l in self.lines), ZERO)

    @property
    def is_balanced(self):
        return self.total_debits == self.total_credits


# ---------------------------------------------------------------------------
# The journal builder
# ---------------------------------------------------------------------------
class Journal:
    """Build the full journal for one basis, with valuation true-ups at each
    checkpoint date (period ends you intend to report on)."""

    def __init__(self, basis, checkpoints=None):
        assert basis in (CASH, ACCRUAL)
        self.basis = basis
        self.checkpoints = sorted(set(checkpoints or []))
        self.entries = []
        self._acct_cache = {}
        self._accounts_by_id = None
        self._build()

    # --- account helpers -----------------------------------------------------
    def sys(self, key):
        if key not in self._acct_cache:
            self._acct_cache[key] = Account.system(key)
        return self._acct_cache[key]

    def accounts(self):
        if self._accounts_by_id is None:
            self._accounts_by_id = {a.pk: a for a in Account.objects.all()}
        return self._accounts_by_id

    def account(self, pk):
        accts = self.accounts()
        if pk not in accts:
            accts[pk] = Account.objects.get(pk=pk)
        return accts[pk]

    def _bank_acct(self, bank_account):
        return bank_account.ensure_ledger_account()

    def _source_account(self, paid_from):
        """Where cash came from for a hand-recorded payment: the chosen bank
        account, else the owner paid personally (owner contribution)."""
        if paid_from is not None:
            return self._bank_acct(paid_from)
        return self.sys("owner_contribution")

    def _expense_account(self, expense):
        if expense.category_id:
            return expense.category.ensure_account()
        return self.sys("uncat_expense")

    def _add(self, entry):
        if entry.lines:
            if not entry.is_balanced:          # defensive: never emit an unbalanced entry
                diff = entry.total_debits - entry.total_credits
                entry.add(self.sys("uncat_expense"), credit=diff) if diff > 0 \
                    else entry.add(self.sys("uncat_expense"), debit=-diff)
            self.entries.append(entry)

    # --- build ---------------------------------------------------------------
    def _build(self):
        self._bank_openings()
        self._bank_transactions()
        self._customer_payments()
        if self.basis == ACCRUAL:
            self._invoices()
            self._credit_memos()
            self._stock_movements()
            self._shipments()
        self._receipts()
        self._expenses()
        self.entries.sort(key=lambda e: (e.date, e.source_type, e.source_id))
        self._true_ups()
        self.entries.sort(key=lambda e: (e.date, e.source_type, e.source_id))

    # 1. opening balances -------------------------------------------------------
    def _bank_openings(self):
        from banking.models import BankAccount
        for acct in BankAccount.objects.filter(is_deleted=False):
            if not acct.opening_balance:
                continue
            e = Entry(acct.opening_date, f"Opening balance — {acct}", "BankAccount", str(acct.pk),
                      source_url=f"/banking/accounts/{acct.pk}/")
            ledger = self._bank_acct(acct)
            if acct.is_liability:
                e.pair(self.sys("opening_balance"), ledger, acct.opening_balance)
            else:
                e.pair(ledger, self.sys("opening_balance"), acct.opening_balance)
            self._add(e)

    # 2. bank & card transactions (both bases) ---------------------------------
    def _bank_transactions(self):
        from banking.models import BankTransaction
        qs = (BankTransaction.objects
              .filter(is_deleted=False, pending=False, account__is_deleted=False)
              .exclude(status=BankTransaction.IGNORED)
              .select_related("account", "category_account", "expense", "expense__category",
                              "transfer_peer", "transfer_peer__account")
              .prefetch_related("payments", "receipts"))
        for t in qs:
            bank = self._bank_acct(t.account)
            amt = abs(t.amount)
            e = Entry(t.date, f"{t.description[:80]} — {t.account.name}", "BankTransaction", str(t.pk),
                      source_url=f"/banking/transactions/{t.pk}/")
            inflow = t.amount > 0

            # a) transfers: one entry per pair, emitted from the outflow side
            if t.status == BankTransaction.TRANSFER and t.transfer_peer_id:
                if inflow:
                    continue
                peer_bank = self._bank_acct(t.transfer_peer.account)
                e.memo = f"Transfer {t.account.name} → {t.transfer_peer.account.name}"
                e.pair(peer_bank, bank, amt)
                self._add(e)
                continue

            offsets = self._bank_offsets(t)
            # bank side
            if inflow:
                e.add(bank, debit=amt)
            else:
                e.add(bank, credit=amt)
            # offset side(s)
            for acct, portion in offsets:
                if inflow:
                    e.add(acct, credit=portion)
                else:
                    e.add(acct, debit=portion)
            self._add(e)

    def _bank_offsets(self, t):
        """Return [(account, amount), ...] summing to |t.amount| for the
        non-bank side of a bank transaction, basis-aware."""
        from banking.models import BankTransaction
        amt = abs(t.amount)
        basis = self.basis

        # b) expense linked (created from, or matched to, this transaction)
        exp = t.expense if (t.expense_id and not t.expense.is_deleted) else None
        if exp is not None:
            if basis == CASH:
                return [(self._expense_account(exp), amt)]
            return [(self.sys("ap"), amt)]

        # c) deposit of customer payments
        payments = [p for p in t.payments.all() if not p.is_deleted]
        if payments:
            total_pay = sum((p.amount for p in payments), ZERO)
            rows = [(self.sys("undeposited"), min(total_pay, amt))]
            diff = amt - total_pay
            if diff > 0:        # deposit bigger than the payments it covers
                rows.append((self.sys("uncat_income"), diff))
            elif diff < 0:      # deposit smaller: processor fees etc. shown as expense
                rows = [(self.sys("undeposited"), total_pay), (self.sys("uncat_expense"), diff)]
            return rows

        # d) supplier bill payment
        receipts = [r for r in t.receipts.all() if not r.is_deleted]
        if receipts:
            return [(self.sys("inventory_purchases") if basis == CASH else self.sys("ap"), amt)]

        # e) categorized to an account
        acct = t.category_account if t.category_account_id else None
        if acct is not None:
            sub = acct.subtype
            if sub == AccountSubtype.INVENTORY_PURCHASES:
                return [(acct if basis == CASH else self.sys("ap"), amt)]
            if sub == AccountSubtype.SHIPPING_EXPENSE:
                return [(acct if basis == CASH else self.sys("ap_carriers"), amt)]
            return [(acct, amt)]

        # f) not reviewed yet — keep it visible
        return [(self.sys("uncat_income") if t.amount > 0 else self.sys("uncat_expense"), amt)]

    # 3. customer payments ------------------------------------------------------
    def _customer_payments(self):
        from payments.models import Payment
        qs = (Payment.objects.filter(is_deleted=False)
              .select_related("customer")
              .prefetch_related("applications__invoice"))
        for p in qs:
            e = Entry(p.date, f"Payment from {p.customer.business_name} ({p.get_method_display()}"
                              f"{' ' + p.reference if p.reference else ''})",
                      "Payment", str(p.pk), source_url=f"/payments/{p.pk}/")
            e.add(self.sys("undeposited"), debit=p.amount)
            applied_total = ZERO
            later = []        # applications recorded after the payment date
            for app in p.applications.all():
                inv = app.invoice
                if inv.is_deleted:
                    continue
                applied_total += app.amount
                app_date = _local_date(app.applied_at) or p.date
                if app_date > p.date:
                    later.append((app, inv, app_date))
                    e.add(self.sys("customer_deposits"), credit=app.amount)
                else:
                    self._payment_application_lines(e, inv, app.amount)
            unapplied = p.amount - applied_total
            if unapplied > 0:
                e.add(self.sys("customer_deposits"), credit=unapplied)
            elif unapplied < 0:   # over-applied data — keep the entry balanced
                e.add(self.sys("customer_deposits"), debit=-unapplied)
            self._add(e)
            for app, inv, app_date in later:
                e2 = Entry(app_date, f"Payment applied to {inv.number}", "PaymentApplication",
                           str(app.pk), source_url=f"/payments/{p.pk}/")
                e2.add(self.sys("customer_deposits"), debit=app.amount)
                self._payment_application_lines(e2, inv, app.amount)
                self._add(e2)

    def _payment_application_lines(self, e, inv, amount):
        """Credit side for ``amount`` applied to ``inv``.

        Accrual: the invoice already booked revenue, so this clears AR.
        Cash: this *is* the revenue — split the payment across merchandise,
        shipping and sales tax in the invoice's proportions."""
        if self.basis == ACCRUAL:
            e.add(self.sys("ar"), credit=amount)
            return
        total = inv.total or ZERO
        if total <= 0 or inv.is_ar_adjustment:
            e.add(self.sys("sales"), credit=amount)
            return
        ship = q(amount * inv.shipping_charge / total)
        tax = q(amount * inv.tax_amount / total)
        merch = q(amount) - ship - tax
        e.add(self.sys("sales"), credit=merch)
        e.add(self.sys("shipping_income"), credit=ship)
        e.add(self.sys("sales_tax"), credit=tax)

    # 4. invoices (accrual) -----------------------------------------------------
    def _invoices(self):
        from invoicing.models import Invoice
        qs = (Invoice.objects.filter(is_deleted=False).exclude(status=Invoice.VOID)
              .select_related("customer"))
        for inv in qs:
            e = Entry(inv.date, f"Invoice {inv.number} — {inv.customer.business_name}",
                      "Invoice", str(inv.pk), source_url=f"/invoices/{inv.pk}/")
            e.add(self.sys("ar"), debit=inv.total)
            if inv.is_ar_adjustment:
                e.memo += " (opening balance transfer)"
                e.add(self.sys("opening_balance"), credit=inv.total)
            else:
                e.add(self.sys("shipping_income"), credit=inv.shipping_charge)
                e.add(self.sys("sales_tax"), credit=inv.tax_amount)
                # whatever is left is merchandise (absorbs rounding / discounts)
                e.add(self.sys("sales"), credit=inv.total - inv.shipping_charge - inv.tax_amount)
            self._add(e)

    # 5. credit memos (accrual) -------------------------------------------------
    def _credit_memos(self):
        from invoicing.models import CreditMemo
        qs = (CreditMemo.objects.filter(is_deleted=False, status=CreditMemo.APPLIED)
              .select_related("customer", "invoice"))
        for cm in qs:
            e = Entry(cm.date, f"Credit memo {cm.number} against {cm.invoice.number}",
                      "CreditMemo", str(cm.pk), source_url=f"/invoices/{cm.invoice_id}/")
            e.pair(self.sys("sales_returns"), self.sys("ar"), cm.total)
            self._add(e)

    # 6. stock ledger (accrual): inventory asset & COGS -------------------------
    def _stock_movements(self):
        from inventory.models import StockAdjustment, StockMovement
        adj_reasons = {str(a.pk): a.reason for a in StockAdjustment.objects.all()}
        qs = (StockMovement.objects.select_related("product")
              .order_by("product_id", "timestamp", "id"))
        prev_avg = defaultdict(lambda: ZERO)
        for m in qs:
            pid = m.product_id
            value_delta = (Decimal(m.qty_after) * m.avg_cost_after
                           - Decimal(m.qty_before) * prev_avg[pid])
            prev_avg[pid] = m.avg_cost_after
            if m.movement_type == "receipt":
                continue    # booked at the receipt document level (Dr inventory / Cr AP)
            value_delta = q(value_delta)
            if value_delta == 0:
                continue
            day = _local_date(m.timestamp)
            e = Entry(day, f"{m.get_movement_type_display()} {m.product.sku} "
                           f"{m.qty_delta:+d} {m.reference}".strip(),
                      "StockMovement", str(m.pk), source_url=f"/inventory/{pid}/")
            inv = self.sys("inventory")
            if m.movement_type == "sale":
                offset = self.sys("cogs")
            elif m.movement_type == "credit_restock":
                offset = self.sys("cogs")
            elif m.movement_type == "adjustment":
                if m.source_type == "PurchaseOrder":          # un-finalize reversal
                    offset = self.sys("cogs")
                else:
                    reason = adj_reasons.get(m.source_id, "")
                    if reason == "opening":
                        offset = self.sys("opening_balance")
                    elif reason == "personal":
                        offset = self.sys("owner_draw")
                    elif reason == "received":
                        offset = self.sys("ap")
                    else:
                        offset = self.sys("inventory_adj")
            else:   # revalue, cycle_count, backorder_release
                offset = self.sys("inventory_adj")
            # positive value_delta = inventory went up
            e.pair(inv, offset, value_delta)
            self._add(e)

    # 7. shipping label / freight costs (accrual) -------------------------------
    def _shipments(self):
        from shipping.models import Shipment
        qs = Shipment.objects.filter(is_deleted=False, cost__isnull=False).select_related("po")
        for s in qs:
            if not s.cost:
                continue
            day = s.ship_date or _local_date(s.created_at)
            e = Entry(day, f"Shipping cost {s.get_provider_display()} — {s.po.number}",
                      "Shipment", str(s.pk), source_url=f"/shipping/{s.pk}/")
            e.pair(self.sys("shipping_expense"), self.sys("ap_carriers"), s.cost)
            self._add(e)

    # 8. supplier receipts (bills) ----------------------------------------------
    def _receipts(self):
        from inventory.models import Receipt
        qs = (Receipt.objects.filter(is_deleted=False, posted=True)
              .select_related("supplier", "paid_from")
              .prefetch_related("lines", "supplier_payments"))
        for r in qs:
            landed = q(r.landed_total)
            if landed == 0:
                continue
            posted_day = _local_date(r.posted_at) or r.date
            label = f"RCV-{r.pk} {r.supplier or 'supplier'}"
            if self.basis == ACCRUAL:
                e = Entry(posted_day, f"Received {label}" + (f" inv {r.supplier_invoice_number}"
                                                               if r.supplier_invoice_number else ""),
                          "Receipt", str(r.pk), source_url=f"/inventory/receiving/{r.pk}/")
                e.pair(self.sys("inventory"), self.sys("ap"), landed)
                self._add(e)
            # payment by hand (no bank transaction linked)
            linked = [t for t in r.supplier_payments.all()
                      if not t.is_deleted and t.status != "ignored"]
            if not linked and r.paid_date:
                e = Entry(r.paid_date, f"Paid {label}", "Receipt", f"{r.pk}:paid",
                          source_url=f"/inventory/receiving/{r.pk}/")
                debit = self.sys("ap") if self.basis == ACCRUAL else self.sys("inventory_purchases")
                e.pair(debit, self._source_account(r.paid_from), landed)
                self._add(e)

    # 9. expenses -------------------------------------------------------------
    def _expenses(self):
        from expenses.models import Expense
        qs = (Expense.objects.filter(is_deleted=False)
              .select_related("category", "category__account", "paid_from", "bank_txn"))
        for x in qs:
            acct = self._expense_account(x)
            txn = getattr(x, "bank_txn", None)
            if txn is not None and (txn.is_deleted or txn.status == "ignored"):
                txn = None
            label = f"{x.vendor or x.category or 'Expense'}: {x.note[:60]}".rstrip(": ")
            url = f"/expenses/{x.pk}/"
            if self.basis == ACCRUAL:
                e = Entry(x.date, label, "Expense", str(x.pk), source_url=url)
                e.pair(acct, self.sys("ap"), x.amount)
                self._add(e)
                if txn is None and x.paid_date:
                    e2 = Entry(x.paid_date, f"Paid {label}", "Expense", f"{x.pk}:paid", source_url=url)
                    e2.pair(self.sys("ap"), self._source_account(x.paid_from), x.amount)
                    self._add(e2)
            else:
                if txn is not None:
                    continue              # the bank transaction carries it
                if not x.paid_date:
                    continue              # unpaid bills are invisible on cash basis
                e = Entry(x.paid_date, label, "Expense", str(x.pk), source_url=url)
                e.pair(acct, self._source_account(x.paid_from), x.amount)
                self._add(e)

    # 10. checkpoint true-ups -----------------------------------------------------
    def _true_ups(self):
        if self.basis != ACCRUAL or not self.checkpoints:
            return
        inv_acct = self.sys("inventory")
        adj_acct = self.sys("inventory_adj")
        ap_acct, dep_acct = self.sys("ap"), self.sys("supplier_deposits")
        apc_acct, ship_acct = self.sys("ap_carriers"), self.sys("shipping_expense")
        for cp in self.checkpoints:
            bal = self._raw_balances_through(cp)
            # (a) inventory book value -> exact stock-ledger valuation
            target = inventory_valuation_as_of(cp)
            current = bal.get(inv_acct.pk, ZERO)
            diff = q(target - current)
            if diff != 0:
                e = Entry(cp, "Inventory valuation true-up to stock ledger", "trueup:inventory", str(cp))
                e.pair(inv_acct, adj_acct, diff)
                self._add(e)
            # (b) supplier AP with a debit balance = prepayments (an asset)
            ap_nat = -bal.get(ap_acct.pk, ZERO)            # credit-normal
            dep_cur = self._reclass_balance("trueup:ap", dep_acct.pk, cp)
            want_dep = max(ZERO, -ap_nat)
            delta = q(want_dep - dep_cur)
            if delta != 0:
                e = Entry(cp, "Supplier prepayments reclassified from accounts payable",
                          "trueup:ap", str(cp))
                e.pair(dep_acct, ap_acct, delta)
                self._add(e)
            # (c) carrier AP with a debit balance = label costs never recorded on shipments
            apc_nat = -bal.get(apc_acct.pk, ZERO)
            cur = self._reclass_balance("trueup:apc", ship_acct.pk, cp)
            want = max(ZERO, -apc_nat)
            delta = q(want - cur)
            if delta != 0:
                e = Entry(cp, "Carrier payments without a recorded shipment cost → shipping expense",
                          "trueup:apc", str(cp))
                e.pair(ship_acct, apc_acct, delta)
                self._add(e)

    def _raw_balances_through(self, day):
        """Balances excluding reclass true-ups (so they can be recomputed)."""
        bal = defaultdict(lambda: ZERO)
        for e in self.entries:
            if e.date > day or e.source_type in ("trueup:ap", "trueup:apc"):
                continue
            for l in e.lines:
                bal[l.account_id] += l.debit - l.credit
        return bal

    def _reclass_balance(self, source_type, account_id, day):
        total = ZERO
        for e in self.entries:
            if e.source_type == source_type and e.date <= day:
                for l in e.lines:
                    if l.account_id == account_id:
                        total += l.debit - l.credit
        return total

    # --- queries ---------------------------------------------------------------
    def balances_through(self, day, start=None):
        """{account_id: debits − credits} for entries in [start, day]."""
        bal = defaultdict(lambda: ZERO)
        for e in self.entries:
            if e.date > day or (start and e.date < start):
                continue
            for l in e.lines:
                bal[l.account_id] += l.debit - l.credit
        return bal

    def entries_between(self, start, end, account_id=None):
        out = []
        for e in self.entries:
            if start <= e.date <= end and (account_id is None
                                           or any(l.account_id == account_id for l in e.lines)):
                out.append(e)
        return out

    def income_statement(self, start, end):
        """Sections: income, cogs, expenses; totals; net income."""
        bal = self.balances_through(end, start=start)
        accts = self.accounts()
        sections = {"income": [], "cogs": [], "expense": []}
        totals = {"income": ZERO, "cogs": ZERO, "expense": ZERO}
        for acct_id, raw in bal.items():
            acct = accts.get(acct_id) or self.account(acct_id)
            if acct.type not in PNL_TYPES:
                continue
            amount = acct.signed_balance(raw)
            if amount == 0:
                continue
            sections[acct.type].append({"account": acct, "amount": amount})
            totals[acct.type] += amount
        for rows in sections.values():
            rows.sort(key=lambda r: (r["account"].code, r["account"].name))
        gross = totals["income"] - totals["cogs"]
        net = gross - totals["expense"]
        return {"basis": self.basis, "start": start, "end": end,
                "income": sections["income"], "cogs": sections["cogs"],
                "expenses": sections["expense"],
                "total_income": totals["income"], "total_cogs": totals["cogs"],
                "gross_profit": gross, "total_expenses": totals["expense"],
                "net_income": net,
                "gross_margin_pct": (gross / totals["income"] * 100) if totals["income"] else None}

    def net_income_between(self, start, end):
        return self.income_statement(start, end)["net_income"]

    def balance_sheet(self, as_of, fy_start_month=None):
        bal = self.balances_through(as_of)
        accts = self.accounts()
        assets, liabilities, equity = [], [], []
        t_assets = t_liab = t_equity = ZERO
        for acct_id, raw in bal.items():
            acct = accts.get(acct_id) or self.account(acct_id)
            if acct.type in PNL_TYPES:
                continue
            amount = acct.signed_balance(raw)
            if amount == 0:
                continue
            row = {"account": acct, "amount": amount}
            if acct.type == AccountType.ASSET:
                assets.append(row); t_assets += amount
            elif acct.type == AccountType.LIABILITY:
                liabilities.append(row); t_liab += amount
            else:
                equity.append(row); t_equity += amount
        fy_start = fiscal_year_start(as_of, fy_start_month)
        earliest = min((e.date for e in self.entries), default=fy_start)
        retained = ZERO
        if earliest < fy_start:
            retained = self.net_income_between(earliest, fy_start - timedelta(days=1))
        current = self.net_income_between(fy_start, as_of)
        if retained:
            equity.append({"account": self.sys("retained"), "amount": retained, "computed": True})
            t_equity += retained
        equity.append({"account": None, "label": f"Net income, fiscal year to date",
                       "amount": current, "computed": True})
        t_equity += current
        for rows in (assets, liabilities, equity):
            rows.sort(key=lambda r: ((r["account"].code if r["account"] else "9999"),
                                     r.get("label", "")))
        return {"basis": self.basis, "as_of": as_of,
                "assets": assets, "liabilities": liabilities, "equity": equity,
                "total_assets": t_assets, "total_liabilities": t_liab, "total_equity": t_equity,
                "retained_earnings": retained, "net_income_ytd": current,
                "fiscal_year_start": fy_start,
                "balanced": q(t_assets) == q(t_liab + t_equity),
                "difference": q(t_assets - t_liab - t_equity)}

    def trial_balance(self, as_of):
        bal = self.balances_through(as_of)
        accts = self.accounts()
        rows, td, tc = [], ZERO, ZERO
        for acct_id, raw in bal.items():
            if raw == 0:
                continue
            acct = accts.get(acct_id) or self.account(acct_id)
            d = raw if raw > 0 else ZERO
            c = -raw if raw < 0 else ZERO
            rows.append({"account": acct, "debit": d, "credit": c})
            td += d; tc += c
        rows.sort(key=lambda r: (r["account"].code, r["account"].name))
        return {"basis": self.basis, "as_of": as_of, "rows": rows,
                "total_debit": td, "total_credit": tc, "balanced": q(td) == q(tc)}

    def general_ledger(self, account, start, end):
        acct_id = account.pk if isinstance(account, Account) else account
        acct = self.account(acct_id)
        opening_raw = self.balances_through(start - timedelta(days=1)).get(acct_id, ZERO)
        running = opening_raw
        rows = []
        for e in self.entries_between(start, end, acct_id):
            for l in e.lines:
                if l.account_id != acct_id:
                    continue
                running += l.debit - l.credit
                rows.append({"date": e.date, "memo": e.memo, "source_type": e.source_type,
                             "source_id": e.source_id, "source_url": e.source_url,
                             "debit": l.debit, "credit": l.credit,
                             "balance": acct.signed_balance(running)})
        return {"basis": self.basis, "account": acct, "start": start, "end": end,
                "opening_balance": acct.signed_balance(opening_raw),
                "closing_balance": acct.signed_balance(running), "rows": rows}

    def journal_rows(self, start, end):
        """Flat list of every line in the period (for export / audit)."""
        accts = self.accounts()
        out = []
        for e in self.entries_between(start, end):
            for l in e.lines:
                a = accts.get(l.account_id) or self.account(l.account_id)
                out.append({"date": e.date, "memo": e.memo, "source_type": e.source_type,
                            "source_id": e.source_id, "account": a,
                            "debit": l.debit, "credit": l.credit})
        return out

    def validate(self):
        """Return a list of unbalanced entries (should always be empty)."""
        return [e for e in self.entries if not e.is_balanced]


# ---------------------------------------------------------------------------
# Inventory valuation (shared with the month-end valuation report)
# ---------------------------------------------------------------------------
def inventory_valuation_as_of(day):
    """Exact stock-ledger valuation: Σ (qty_after × avg_cost_after) of each
    product's last movement on/before ``day``."""
    from inventory.models import StockMovement
    cutoff = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.max.time()))
    total = ZERO
    seen = set()
    for m in (StockMovement.objects.filter(timestamp__lte=cutoff)
              .order_by("product_id", "-timestamp", "-id")
              .values_list("product_id", "qty_after", "avg_cost_after")):
        pid, qty, cost = m
        if pid in seen:
            continue
        seen.add(pid)
        total += Decimal(qty) * cost
    return q(total)


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------
def income_statement(basis, start, end):
    return Journal(basis, [start - timedelta(days=1), end]).income_statement(start, end)


def balance_sheet(basis, as_of):
    return Journal(basis, [as_of]).balance_sheet(as_of)


def trial_balance(basis, as_of):
    return Journal(basis, [as_of]).trial_balance(as_of)


def general_ledger(basis, account, start, end):
    return Journal(basis, [start - timedelta(days=1), end]).general_ledger(account, start, end)


def compare_bases(start, end):
    """Side-by-side cash vs accrual income statement with the bridge that
    explains the difference (changes in AR, inventory, AP, …)."""
    cash_j = Journal(CASH, [start - timedelta(days=1), end])
    acc_j = Journal(ACCRUAL, [start - timedelta(days=1), end])
    cash = cash_j.income_statement(start, end)
    acc = acc_j.income_statement(start, end)

    def by_account(stmt):
        out = {}
        for key in ("income", "cogs", "expenses"):
            for r in stmt[key]:
                out[r["account"].pk] = (r["account"], r["amount"])
        return out

    c_rows, a_rows = by_account(cash), by_account(acc)
    rows = []
    for pk in sorted(set(c_rows) | set(a_rows),
                     key=lambda k: (c_rows.get(k) or a_rows.get(k))[0].code):
        acct = (c_rows.get(pk) or a_rows.get(pk))[0]
        c = c_rows.get(pk, (None, ZERO))[1]
        a = a_rows.get(pk, (None, ZERO))[1]
        rows.append({"account": acct, "cash": c, "accrual": a, "difference": a - c})

    # Bridge: balance-sheet movements over the period on the accrual books
    before = acc_j.balances_through(start - timedelta(days=1))
    after = acc_j.balances_through(end)

    def nat_delta(key):
        a = Account.system(key)
        return a.signed_balance(after.get(a.pk, ZERO)) - a.signed_balance(before.get(a.pk, ZERO))

    bridge = [
        ("Invoiced but not yet collected (Δ accounts receivable)", nat_delta("ar")),
        ("Unapplied customer payments (Δ customer deposits)", -nat_delta("customer_deposits")),
        ("Inventory bought but not yet sold (Δ inventory)", nat_delta("inventory")),
        ("Supplier prepayments (Δ supplier deposits)", nat_delta("supplier_deposits")),
        ("Bills received but not yet paid (Δ accounts payable)", -nat_delta("ap")),
        ("Carrier charges not yet paid (Δ AP carriers)", -nat_delta("ap_carriers")),
        ("Sales tax collected vs. remitted (Δ sales tax payable)", -nat_delta("sales_tax")),
    ]
    return {"start": start, "end": end, "cash": cash, "accrual": acc, "rows": rows,
            "net_difference": acc["net_income"] - cash["net_income"], "bridge": bridge}


def equity_trend(basis, months=12, as_of=None):
    """Month-end total equity, assets, liabilities and that month's net
    income for the last ``months`` months — the 'am I building equity?' view."""
    as_of = as_of or timezone.localdate()
    ends = []
    cur = month_end(as_of)
    for _ in range(months):
        ends.append(cur)
        cur = month_end((cur.replace(day=1) - timedelta(days=1)))
    ends.reverse()
    j = Journal(basis, ends)
    out = []
    for me in ends:
        bs = j.balance_sheet(me)
        ms = me.replace(day=1)
        out.append({"month_end": me, "label": me.strftime("%b %Y"),
                    "assets": bs["total_assets"], "liabilities": bs["total_liabilities"],
                    "equity": bs["total_equity"],
                    "net_income": j.net_income_between(ms, me)})
    for i, row in enumerate(out):
        row["equity_change"] = row["equity"] - out[i - 1]["equity"] if i else None
    return out


def dashboard_snapshot(basis=None, as_of=None):
    """Small set of numbers for the dashboard: MTD and YTD net income, cash
    position, equity — in one journal build."""
    from core.models import CompanySettings
    settings_row = CompanySettings.get()
    basis = basis or settings_row.accounting_basis_default or CASH
    as_of = as_of or timezone.localdate()
    mtd_start = as_of.replace(day=1)
    fy_start = fiscal_year_start(as_of, settings_row.fiscal_year_start_month)
    prev_end = mtd_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    j = Journal(basis, [prev_start - timedelta(days=1), prev_end, as_of])
    bs = j.balance_sheet(as_of, settings_row.fiscal_year_start_month)
    return {"basis": basis, "as_of": as_of,
            "net_income_mtd": j.net_income_between(mtd_start, as_of),
            "net_income_prev_month": j.net_income_between(prev_start, prev_end),
            "net_income_ytd": j.net_income_between(fy_start, as_of),
            "total_equity": bs["total_equity"], "total_assets": bs["total_assets"],
            "total_liabilities": bs["total_liabilities"]}
