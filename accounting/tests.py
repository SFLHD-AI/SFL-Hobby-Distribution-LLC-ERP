"""Ledger engine: every statement balances, cash vs accrual recognise the
same documents at different times, and nothing is double counted."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounting.ledger import (ACCRUAL, CASH, Journal, balance_sheet, compare_bases,
                               equity_trend, income_statement)
from accounting.models import Account
from banking.models import BankAccount, BankTransaction
from expenses.models import Expense, ExpenseCategory
from inventory.models import Product, Receipt, ReceiptLine, Supplier
from invoicing.models import Invoice
from orders.models import POLine, PurchaseOrder
from orders.tests import make_customer
from payments.models import Payment

D = Decimal
TODAY = timezone.localdate()
YEAR_START = TODAY.replace(month=1, day=1)


def _assert_balanced(test, basis, as_of=None):
    as_of = as_of or TODAY
    j = Journal(basis, [as_of])
    test.assertEqual(j.validate(), [], f"unbalanced entries on {basis}")
    bs = j.balance_sheet(as_of)
    test.assertTrue(bs["balanced"], f"{basis} balance sheet off by {bs['difference']}")
    tb = j.trial_balance(as_of)
    test.assertTrue(tb["balanced"])
    return j, bs


class LedgerBasics(TestCase):
    def setUp(self):
        Account.ensure_system_accounts()
        self.checking = BankAccount.objects.create(
            name="Checking", account_type="checking", opening_balance=D("1000.00"),
            opening_date=TODAY - timedelta(days=60))
        self.card = BankAccount.objects.create(
            name="Visa", account_type="credit_card", opening_balance=D("250.00"),
            opening_date=TODAY - timedelta(days=60))
        self.supplies = ExpenseCategory.objects.create(name="Shipping supplies")

    def test_empty_books_balance(self):
        for basis in (CASH, ACCRUAL):
            j, bs = _assert_balanced(self, basis)
            self.assertEqual(bs["total_assets"], D("1000.00"))
            self.assertEqual(bs["total_liabilities"], D("250.00"))
            self.assertEqual(bs["total_equity"], D("750.00"))

    def test_card_charge_categorized_creates_expense_and_hits_both_bases(self):
        t = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("-40.00"),
                                           description="ULINE SHIP SUPPLIES")
        t.categorize(self.supplies.ensure_account())
        self.assertEqual(Expense.objects.count(), 1)
        exp = Expense.objects.get()
        self.assertEqual(exp.amount, D("40.00"))
        self.assertTrue(exp.is_paid)
        self.assertEqual(exp.paid_from, self.card)
        for basis in (CASH, ACCRUAL):
            j, bs = _assert_balanced(self, basis)
            pl = j.income_statement(YEAR_START, TODAY)
            self.assertEqual(pl["total_expenses"], D("40.00"), basis)   # once, not twice
            # card balance owed went up by 40
            card_row = [r for r in bs["liabilities"] if r["account"].pk == self.card.ledger_account_id][0]
            self.assertEqual(card_row["amount"], D("290.00"))

    def test_unreviewed_outflow_shows_as_uncategorized(self):
        BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-12.34"),
                                       description="MYSTERY")
        pl = income_statement(CASH, YEAR_START, TODAY)
        names = {r["account"].system_key: r["amount"] for r in pl["expenses"]}
        self.assertEqual(names.get("uncat_expense"), D("12.34"))
        _assert_balanced(self, CASH)

    def test_ignored_transactions_are_invisible(self):
        t = BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-99.00"),
                                           description="DUPLICATE")
        t.mark_ignored()
        pl = income_statement(CASH, YEAR_START, TODAY)
        self.assertEqual(pl["total_expenses"], D("0.00"))
        self.assertEqual(self.checking.current_balance, D("1000.00"))

    def test_transfer_moves_money_without_income_or_expense(self):
        out = BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-250.00"),
                                             description="Payment to Visa")
        inn = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("250.00"),
                                             description="PAYMENT THANK YOU")
        out.mark_transfer(inn)
        for basis in (CASH, ACCRUAL):
            j, bs = _assert_balanced(self, basis)
            pl = j.income_statement(YEAR_START, TODAY)
            self.assertEqual(pl["net_income"], D("0.00"))
            self.assertEqual(bs["total_liabilities"], D("0.00"))     # card paid off
            self.assertEqual(bs["total_assets"], D("750.00"))

    def test_owner_draw_reduces_equity_not_profit(self):
        t = BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-100.00"),
                                           description="ATM WITHDRAWAL")
        t.categorize(Account.system("owner_draw"))
        j, bs = _assert_balanced(self, CASH)
        self.assertEqual(j.income_statement(YEAR_START, TODAY)["net_income"], D("0.00"))
        self.assertEqual(bs["total_equity"], D("650.00"))

    def test_expense_paid_personally_is_owner_contribution(self):
        Expense.objects.create(date=TODAY, category=self.supplies, vendor="Walmart",
                               amount=D("30.00"), paid_date=TODAY, payment_method="personal")
        j, bs = _assert_balanced(self, CASH)
        self.assertEqual(j.income_statement(YEAR_START, TODAY)["total_expenses"], D("30.00"))
        contrib = [r for r in bs["equity"] if r["account"] and r["account"].system_key == "owner_contribution"]
        self.assertEqual(contrib[0]["amount"], D("30.00"))
        self.assertEqual(bs["total_assets"], D("1000.00"))      # no bank account touched

    def test_unpaid_bill_only_on_accrual(self):
        Expense.objects.create(date=TODAY, category=self.supplies, vendor="ULINE",
                               amount=D("500.00"), due_date=TODAY + timedelta(days=30))
        self.assertEqual(income_statement(CASH, YEAR_START, TODAY)["total_expenses"], D("0.00"))
        acc = income_statement(ACCRUAL, YEAR_START, TODAY)
        self.assertEqual(acc["total_expenses"], D("500.00"))
        j, bs = _assert_balanced(self, ACCRUAL)
        ap = [r for r in bs["liabilities"] if r["account"].system_key == "ap"][0]
        self.assertEqual(ap["amount"], D("500.00"))

    def test_bill_then_card_payment_counts_once_on_accrual(self):
        exp = Expense.objects.create(date=TODAY - timedelta(days=10), category=self.supplies,
                                     vendor="ULINE", amount=D("500.00"))
        t = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("-500.00"),
                                           description="ULINE")
        t.link_expense(exp)
        exp.refresh_from_db()
        self.assertTrue(exp.is_paid)
        for basis in (CASH, ACCRUAL):
            j, bs = _assert_balanced(self, basis)
            self.assertEqual(j.income_statement(YEAR_START, TODAY)["total_expenses"], D("500.00"), basis)
            ap = [r for r in bs["liabilities"] if r["account"].system_key == "ap"]
            self.assertEqual(ap, [], f"AP should be cleared on {basis}")
        # cash basis: expense dated when the card was charged, not the bill date
        cash_before = income_statement(CASH, YEAR_START, TODAY - timedelta(days=1))
        self.assertEqual(cash_before["total_expenses"], D("0.00"))


class SalesCycle(TestCase):
    """Receive stock → sell → invoice → get paid → deposit, on both bases."""

    def setUp(self):
        Account.ensure_system_accounts()
        self.admin = User.objects.create_superuser("acct", password="x")
        self.checking = BankAccount.objects.create(name="Checking", opening_balance=D("0.00"),
                                                   opening_date=TODAY - timedelta(days=90))
        self.supplier = Supplier.objects.create(name="GTS Distribution")
        self.p = Product.objects.create(sku="BOX-1", name="Hobby box", default_price=D("100.00"))
        r = Receipt.objects.create(supplier=self.supplier, date=TODAY - timedelta(days=20),
                                   freight_cost=D("20.00"))
        ReceiptLine.objects.create(receipt=r, product=self.p, qty=10, unit_cost=D("58.00"))
        r.post()                        # 10 @ 60 landed = $600 inventory, $600 AP
        r.posted_at = timezone.now() - timedelta(days=20)
        r.save(update_fields=["posted_at"])
        self.receipt = r
        self.cust = make_customer("Card Shop", cert=False)
        po = PurchaseOrder.objects.create(customer=self.cust, charge_tax=True,
                                          tax_rate=D("7.000"), charge_shipping=True,
                                          shipping_charge=D("10.00"))
        POLine.objects.create(po=po, product=self.p, qty_ordered=4, unit_price=D("100.00"))
        po.confirm(); po.finalize()
        self.inv = Invoice.build_from_pos([po])   # 400 merch + 10 ship + 28.70 tax = 438.70
        self.inv.date = TODAY - timedelta(days=15)
        self.inv.save(update_fields=["date"])

    def test_invoice_numbers(self):
        self.assertEqual(self.inv.total, D("438.70"))
        self.assertEqual(self.inv.tax_amount, D("28.70"))

    def test_accrual_recognises_sale_and_cogs_before_payment(self):
        j, bs = _assert_balanced(self, ACCRUAL)
        pl = j.income_statement(YEAR_START, TODAY)
        inc = {r["account"].system_key: r["amount"] for r in pl["income"]}
        self.assertEqual(inc["sales"], D("400.00"))
        self.assertEqual(inc["shipping_income"], D("10.00"))
        self.assertEqual(pl["total_cogs"], D("240.00"))          # 4 × 60 landed
        self.assertEqual(pl["gross_profit"], D("170.00"))
        assets = {r["account"].system_key: r["amount"] for r in bs["assets"]}
        liab = {r["account"].system_key: r["amount"] for r in bs["liabilities"]}
        self.assertEqual(assets["ar"], D("438.70"))
        self.assertEqual(assets["inventory"], D("360.00"))      # 6 left × 60
        self.assertEqual(liab["ap"], D("600.00"))
        self.assertEqual(liab["sales_tax"], D("28.70"))
        self.assertEqual(bs["total_equity"], D("170.00"))        # exactly the profit so far

    def test_cash_basis_sees_nothing_until_paid(self):
        j, bs = _assert_balanced(self, CASH)
        pl = j.income_statement(YEAR_START, TODAY)
        self.assertEqual(pl["total_income"], D("0.00"))
        self.assertEqual(pl["total_cogs"], D("0.00"))
        self.assertEqual(bs["total_assets"], D("0.00"))

    def test_payment_recognised_on_cash_basis_with_tax_split(self):
        pay = Payment.objects.create(customer=self.cust, amount=D("438.70"), method="check",
                                     date=TODAY - timedelta(days=5))
        pay.apply_to_invoice(self.inv, D("438.70"))
        j, bs = _assert_balanced(self, CASH)
        pl = j.income_statement(YEAR_START, TODAY)
        inc = {r["account"].system_key: r["amount"] for r in pl["income"]}
        self.assertEqual(inc["sales"], D("400.00"))
        self.assertEqual(inc["shipping_income"], D("10.00"))
        liab = {r["account"].system_key: r["amount"] for r in bs["liabilities"]}
        self.assertEqual(liab["sales_tax"], D("28.70"))         # collected, owed to the state
        assets = {r["account"].system_key: r["amount"] for r in bs["assets"]}
        self.assertEqual(assets["undeposited"], D("438.70"))
        # accrual: AR cleared, revenue unchanged
        ja, bsa = _assert_balanced(self, ACCRUAL)
        self.assertNotIn("ar", {r["account"].system_key for r in bsa["assets"]})
        self.assertEqual(ja.income_statement(YEAR_START, TODAY)["total_income"], D("410.00"))

    def test_partial_payment_splits_proportionally(self):
        pay = Payment.objects.create(customer=self.cust, amount=D("219.35"), method="zelle")
        pay.apply_to_invoice(self.inv, D("219.35"))      # exactly half
        pl = income_statement(CASH, YEAR_START, TODAY)
        inc = {r["account"].system_key: r["amount"] for r in pl["income"]}
        self.assertEqual(inc["sales"], D("200.00"))
        self.assertEqual(inc["shipping_income"], D("5.00"))
        _assert_balanced(self, CASH)

    def test_deposit_clears_undeposited_funds(self):
        pay = Payment.objects.create(customer=self.cust, amount=D("438.70"), method="check")
        pay.apply_to_invoice(self.inv, D("438.70"))
        dep = BankTransaction.objects.create(account=self.checking, date=TODAY,
                                             amount=D("438.70"), description="DEPOSIT")
        dep.link_payments([pay])
        for basis in (CASH, ACCRUAL):
            j, bs = _assert_balanced(self, basis)
            assets = {r["account"].system_key or r["account"].pk: r["amount"] for r in bs["assets"]}
            self.assertNotIn("undeposited", assets)
            self.assertEqual(assets[self.checking.ledger_account_id], D("438.70"))
            # revenue not doubled by the deposit
            self.assertEqual(j.income_statement(YEAR_START, TODAY)["total_income"], D("410.00"))

    def test_supplier_payment_is_cogs_on_cash_and_clears_ap_on_accrual(self):
        t = BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-600.00"),
                                           description="GTS DISTRIBUTION ACH")
        t.link_receipts([self.receipt])
        self.receipt.refresh_from_db()
        self.assertTrue(self.receipt.is_paid)
        jc, bsc = _assert_balanced(self, CASH)
        plc = jc.income_statement(YEAR_START, TODAY)
        self.assertEqual(plc["total_cogs"], D("600.00"))         # expensed when paid
        ja, bsa = _assert_balanced(self, ACCRUAL)
        self.assertNotIn("ap", {r["account"].system_key for r in bsa["liabilities"]})
        self.assertEqual(ja.income_statement(YEAR_START, TODAY)["total_cogs"], D("240.00"))

    def test_prepaying_an_unposted_receipt_is_a_supplier_deposit(self):
        pre = Receipt.objects.create(supplier=self.supplier, date=TODAY)   # allocation, not received
        ReceiptLine.objects.create(receipt=pre, product=self.p, qty=10, unit_cost=D("50.00"))
        t = BankTransaction.objects.create(account=self.checking, date=TODAY, amount=D("-1100.00"),
                                           description="GTS PREORDER")
        t.link_receipts([self.receipt, pre])   # pays the $600 bill and $500 ahead
        j, bs = _assert_balanced(self, ACCRUAL)
        assets = {r["account"].system_key: r["amount"] for r in bs["assets"]}
        liab = {r["account"].system_key: r["amount"] for r in bs["liabilities"]}
        self.assertEqual(assets.get("supplier_deposits"), D("500.00"))
        self.assertNotIn("ap", liab)

    def test_compare_bases_bridge_explains_difference(self):
        cmp = compare_bases(YEAR_START, TODAY)
        self.assertEqual(cmp["accrual"]["net_income"], D("170.00"))
        self.assertEqual(cmp["cash"]["net_income"], D("0.00"))
        self.assertEqual(cmp["net_difference"], D("170.00"))
        bridge = dict(cmp["bridge"])
        self.assertEqual(bridge["Invoiced but not yet collected (Δ accounts receivable)"], D("438.70"))
        self.assertEqual(bridge["Inventory bought but not yet sold (Δ inventory)"], D("360.00"))

    def test_equity_trend_runs_and_balances(self):
        rows = equity_trend(ACCRUAL, months=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["equity"], D("170.00"))

    def test_void_invoice_disappears_and_books_still_balance(self):
        self.inv.void()
        j, bs = _assert_balanced(self, ACCRUAL)
        self.assertEqual(j.income_statement(YEAR_START, TODAY)["total_income"], D("0.00"))
        self.assertNotIn("ar", {r["account"].system_key for r in bs["assets"]})
