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


# ===========================================================================
# Accounting module UI (views, exports, permissions, helpers)
# ===========================================================================
import io
import zipfile
from datetime import date

from django.test import RequestFactory
from django.urls import reverse

from accounting import reports as rp
from accounting.models import AccountType, FiscalPeriodLock
from core.models import CompanySettings, Role, UserProfile


def _role_user(username, modules, can_see_costs):
    """A non-admin user whose access comes from a Role."""
    user = User.objects.create_user(username, password="x")
    role = Role.objects.create(name=f"role-{username}", modules=modules, can_see_costs=can_see_costs)
    UserProfile.objects.create(user=user, role=role)
    return user


class AccountingUISetup(TestCase):
    """Books with activity on both bases: an unpaid bill (accrual only), a
    categorized card charge (both), an open invoice with inventory (accrual)."""

    def setUp(self):
        Account.ensure_system_accounts()
        self.admin = User.objects.create_superuser("owner", password="x")
        self.client.force_login(self.admin)
        self.checking = BankAccount.objects.create(
            name="Checking", account_type="checking", opening_balance=D("5000.00"),
            opening_date=TODAY - timedelta(days=400))
        self.card = BankAccount.objects.create(
            name="Visa", account_type="credit_card", opening_balance=D("0.00"),
            opening_date=TODAY - timedelta(days=400))
        self.supplies = ExpenseCategory.objects.create(name="Shipping supplies")
        self.software = ExpenseCategory.objects.create(name="Software")
        # card charge categorized -> expense on both bases
        t = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("-40.00"),
                                           description="ULINE SHIP SUPPLIES")
        t.categorize(self.supplies.ensure_account())
        # unpaid bill -> accrual only
        Expense.objects.create(date=TODAY, category=self.software, vendor="Adobe",
                               amount=D("60.00"), due_date=TODAY + timedelta(days=20))
        # stock + open invoice -> accrual AR / inventory / COGS
        self.supplier = Supplier.objects.create(name="GTS Distribution")
        self.p = Product.objects.create(sku="BOX-1", name="Hobby box", default_price=D("100.00"))
        r = Receipt.objects.create(supplier=self.supplier, date=TODAY - timedelta(days=20))
        ReceiptLine.objects.create(receipt=r, product=self.p, qty=10, unit_cost=D("60.00"))
        r.post()
        r.posted_at = timezone.now() - timedelta(days=20)
        r.save(update_fields=["posted_at"])
        self.receipt = r
        self.cust = make_customer("Card Shop", cert=False)
        po = PurchaseOrder.objects.create(customer=self.cust, charge_tax=True, tax_rate=D("7.000"),
                                          charge_shipping=True, shipping_charge=D("10.00"))
        POLine.objects.create(po=po, product=self.p, qty_ordered=4, unit_price=D("100.00"))
        po.confirm(); po.finalize()
        self.inv = Invoice.build_from_pos([po])
        self.inv.date = TODAY - timedelta(days=15)
        self.inv.save(update_fields=["date"])
        # a custom, non-system account for the chart-of-accounts tests
        self.custom = Account.objects.create(code="1600", name="Display cases", type=AccountType.ASSET,
                                             subtype="fixed_asset", description="Shop fixtures")

    # every page + both bases ------------------------------------------------
    def page_urls(self):
        gl = reverse("accounting_general_ledger")
        return [
            reverse("accounting_index"),
            reverse("accounting_index") + "?basis=accrual",
            reverse("accounting_pnl"),
            reverse("accounting_pnl") + "?basis=accrual&preset=ytd",
            reverse("accounting_pnl") + "?basis=cash&start=2020-01-01&end=2020-12-31",
            reverse("accounting_balance_sheet"),
            reverse("accounting_balance_sheet") + "?basis=accrual",
            reverse("accounting_compare"),
            reverse("accounting_compare") + f"?start={YEAR_START}&end={TODAY}",
            reverse("accounting_equity_trend"),
            reverse("accounting_equity_trend") + "?basis=cash&months=3",
            reverse("accounting_trial_balance"),
            reverse("accounting_trial_balance") + "?basis=accrual",
            gl,
            gl + f"?basis=accrual&account={Account.system('ar').pk}&preset=ytd",
            gl + "?account=999999",
            reverse("accounting_journal"),
            reverse("accounting_journal") + "?basis=accrual&preset=ytd",
            reverse("accounting_accounts"),
            reverse("accounting_accounts") + "?inactive=1",
            reverse("accounting_account_create"),
            reverse("accounting_account_create") + "?type=liability",
            reverse("accounting_account_edit", args=[self.custom.pk]),
            reverse("accounting_account_edit", args=[Account.system("ar").pk]),
            reverse("accounting_year_end"),
            reverse("accounting_period_lock"),
        ]


class AccountingPagesRender(AccountingUISetup):
    def test_every_page_returns_200_for_admin(self):
        for url in self.page_urls():
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, url)

    def test_index_shows_both_bases_and_plain_english_box(self):
        html = self.client.get(reverse("accounting_index")).content.decode()
        self.assertIn("Cash basis</b> = what your tax return uses", html)
        self.assertIn("Accrual</b> = the true picture", html)
        for name in ("accounting_pnl", "accounting_balance_sheet", "accounting_compare",
                     "accounting_equity_trend", "accounting_trial_balance", "accounting_general_ledger",
                     "accounting_journal", "accounting_accounts", "accounting_year_end",
                     "accounting_period_lock"):
            self.assertIn(reverse(name), html, name)
        # six-month trend table
        self.assertIn("Equity, last 6 months", html)

    def test_index_respects_company_default_basis(self):
        s = CompanySettings.get(); s.accounting_basis_default = "accrual"; s.save()
        r = self.client.get(reverse("accounting_index"))
        self.assertEqual(r.context["basis"], ACCRUAL)
        r = self.client.get(reverse("accounting_index") + "?basis=cash")
        self.assertEqual(r.context["basis"], CASH)
        r = self.client.get(reverse("accounting_index") + "?basis=bogus")
        self.assertEqual(r.context["basis"], ACCRUAL)

    def test_pnl_numbers_differ_by_basis(self):
        cash = self.client.get(reverse("accounting_pnl") + "?basis=cash&preset=ytd").context["stmt"]
        acc = self.client.get(reverse("accounting_pnl") + "?basis=accrual&preset=ytd").context["stmt"]
        self.assertEqual(cash["total_expenses"], D("40.00"))          # card charge only
        self.assertEqual(acc["total_expenses"], D("100.00"))          # + unpaid bill
        self.assertEqual(acc["total_income"], D("410.00"))            # invoice sent
        self.assertEqual(cash["total_income"], D("0.00"))             # nothing collected
        self.assertEqual(acc["total_cogs"], D("240.00"))

    def test_pnl_default_period_is_current_month_and_swapped_dates_are_fixed(self):
        r = self.client.get(reverse("accounting_pnl"))
        self.assertEqual(r.context["start"], TODAY.replace(day=1))
        self.assertEqual(r.context["end"], TODAY)
        r = self.client.get(reverse("accounting_pnl") + "?start=2026-03-31&end=2026-03-01")
        self.assertEqual((r.context["start"], r.context["end"]), (date(2026, 3, 1), date(2026, 3, 31)))
        r = self.client.get(reverse("accounting_pnl") + "?start=garbage&end=2026-03-31")
        self.assertEqual(r.status_code, 200)

    def test_balance_sheet_shows_balanced_badge_and_computed_rows(self):
        r = self.client.get(reverse("accounting_balance_sheet") + "?basis=accrual")
        html = r.content.decode()
        self.assertTrue(r.context["bs"]["balanced"])
        self.assertIn("Balanced", html)
        self.assertIn("Net income, fiscal year to date", html)
        self.assertIn("computed", html)
        self.assertNotIn("Out of balance", html)

    def test_compare_page_shows_bridge_rows_with_explanations(self):
        r = self.client.get(reverse("accounting_compare") + f"?start={YEAR_START}&end={TODAY}")
        html = r.content.decode()
        for label in ("Invoiced but not yet collected", "Inventory bought but not yet sold",
                      "Bills received but not yet paid", "Sales tax collected vs. remitted"):
            self.assertIn(label, html)
        self.assertIn("customers have not paid yet", html)        # explanation text
        self.assertIn("Net income on a cash basis", html)
        self.assertIn("Net income on an accrual basis", html)
        cmp = r.context["cmp"]
        self.assertEqual(cmp["accrual"]["net_income"] - cmp["cash"]["net_income"], cmp["net_difference"])
        bridge = dict(cmp["bridge"])
        self.assertEqual(bridge["Invoiced but not yet collected (Δ accounts receivable)"], D("438.70"))
        self.assertEqual(bridge["Bills received but not yet paid (Δ accounts payable)"], D("-660.00"))

    def test_equity_trend_clamps_months_and_draws_svg(self):
        r = self.client.get(reverse("accounting_equity_trend") + "?months=500")
        self.assertEqual(r.context["months"], 60)
        r = self.client.get(reverse("accounting_equity_trend") + "?months=abc")
        self.assertEqual(r.context["months"], 12)
        self.assertEqual(len(r.context["rows"]), 12)
        html = r.content.decode()
        self.assertIn("<svg", html)
        self.assertIn("<rect", html)
        self.assertIn("$", html)

    def test_trial_balance_balanced(self):
        r = self.client.get(reverse("accounting_trial_balance") + "?basis=accrual")
        self.assertTrue(r.context["tb"]["balanced"])
        self.assertIn("Balanced", r.content.decode())

    def test_general_ledger_for_ar_shows_invoice_with_source_link(self):
        ar = Account.system("ar")
        r = self.client.get(reverse("accounting_general_ledger") +
                            f"?basis=accrual&account={ar.pk}&start={YEAR_START}&end={TODAY}")
        gl = r.context["gl"]
        self.assertEqual(gl["closing_balance"], D("438.70"))
        self.assertEqual(len(gl["rows"]), 1)
        self.assertIn(f"/invoices/{self.inv.pk}/", r.content.decode())

    def test_general_ledger_without_account_prompts(self):
        r = self.client.get(reverse("accounting_general_ledger"))
        self.assertIsNone(r.context["account"])
        self.assertIn("Pick an account", r.content.decode())
        r = self.client.get(reverse("accounting_general_ledger") + "?account=999999")
        self.assertIn("does not exist", r.content.decode())

    def test_journal_totals_balance(self):
        r = self.client.get(reverse("accounting_journal") + f"?basis=accrual&start={YEAR_START}&end={TODAY}")
        self.assertTrue(r.context["balanced"])
        self.assertEqual(r.context["unbalanced"], 0)
        self.assertGreater(r.context["row_count"], 0)


class AccountingExports(AccountingUISetup):
    def _csv(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn("attachment", r["Content-Disposition"])
        return r.content.decode().splitlines()

    def test_pnl_csv(self):
        lines = self._csv(reverse("accounting_pnl") + "?basis=accrual&preset=ytd&export=csv")
        self.assertEqual(lines[0], "Section,Code,Account,Amount")
        self.assertIn("Net income,,Net income,", "\n".join(lines))
        self.assertTrue(any(l.startswith("Gross profit,,Gross margin %") for l in lines))

    def test_balance_sheet_csv_has_balanced_check(self):
        lines = self._csv(reverse("accounting_balance_sheet") + "?basis=accrual&export=csv")
        self.assertEqual(lines[0], "Section,Code,Account,Amount")
        self.assertIn("Check,,Balanced,yes", lines)

    def test_compare_csv(self):
        lines = self._csv(reverse("accounting_compare") + f"?start={YEAR_START}&end={TODAY}&export=csv")
        self.assertEqual(lines[0], "Section,Code,Account,Cash,Accrual,Difference")
        self.assertTrue(any("Bridge (why they differ)" in l for l in lines))

    def test_equity_trend_csv(self):
        lines = self._csv(reverse("accounting_equity_trend") + "?basis=accrual&months=4&export=csv")
        self.assertEqual(lines[0], "Month,Assets,Liabilities,Equity,Net income,Change in equity")
        self.assertEqual(len(lines), 5)

    def test_trial_balance_csv(self):
        lines = self._csv(reverse("accounting_trial_balance") + "?basis=accrual&export=csv")
        self.assertEqual(lines[0], "Code,Account,Type,Debit,Credit")
        self.assertTrue(lines[-1].startswith(",TOTAL,"))

    def test_general_ledger_csv(self):
        ar = Account.system("ar")
        lines = self._csv(reverse("accounting_general_ledger") +
                          f"?basis=accrual&account={ar.pk}&start={YEAR_START}&end={TODAY}&export=csv")
        self.assertEqual(lines[0], "Date,Memo,Source type,Source id,Debit,Credit,Balance")
        self.assertIn("Opening balance", lines[1])
        self.assertIn("Closing balance", lines[-1])
        self.assertTrue(lines[-1].endswith("438.70"))

    def test_journal_csv(self):
        lines = self._csv(reverse("accounting_journal") + f"?basis=cash&start={YEAR_START}&end={TODAY}&export=csv")
        self.assertEqual(lines[0], "Date,Memo,Source type,Source id,Code,Account,Debit,Credit")
        self.assertGreater(len(lines), 1)

    def test_pdf_exports_render(self):
        for url in (reverse("accounting_pnl") + "?basis=accrual&preset=ytd&export=pdf",
                    reverse("accounting_balance_sheet") + "?basis=accrual&export=pdf",
                    reverse("accounting_compare") + f"?start={YEAR_START}&end={TODAY}&export=pdf",
                    reverse("accounting_trial_balance") + "?basis=cash&export=pdf"):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r["Content-Type"], "application/pdf")
                self.assertTrue(r.content.startswith(b"%PDF"))

    def test_year_end_zip_contains_every_file(self):
        r = self.client.get(reverse("accounting_year_end") + f"?year={TODAY.year}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn(f'attachment; filename="year_end_{TODAY.year}.zip"', r["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        expected = {"pnl_cash.csv", "pnl_accrual.csv", "balance_sheet_cash.csv", "balance_sheet_accrual.csv",
                    "cash_vs_accrual.csv", "trial_balance_accrual.csv", "general_journal_accrual.csv",
                    "general_journal_cash.csv", "README.txt"}
        self.assertEqual(names, expected)
        readme = zf.read("README.txt").decode()
        self.assertIn("CASH basis", readme)
        for name in expected - {"README.txt"}:
            self.assertIn(name, readme)
        pnl = zf.read("pnl_accrual.csv").decode().splitlines()
        self.assertEqual(pnl[0], "Section,Code,Account,Amount")
        bs = zf.read("balance_sheet_accrual.csv").decode()
        self.assertIn("Check,,Balanced,yes", bs)

    def test_year_end_default_year_and_month_variant(self):
        r = self.client.get(reverse("accounting_year_end") + "?download=1")
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn(f"year_end_{TODAY.year - 1}.zip", r["Content-Disposition"])
        r = self.client.get(reverse("accounting_year_end") + f"?month={TODAY:%Y-%m}")
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn(f"month_end_{TODAY:%Y_%m}.zip", r["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertIn("Month-end package", zf.read("README.txt").decode())

    def test_year_end_bad_input_redirects_with_message(self):
        r = self.client.get(reverse("accounting_year_end") + "?month=2026-13", follow=True)
        self.assertIn("YYYY-MM", r.content.decode())
        r = self.client.get(reverse("accounting_year_end") + "?year=abcd", follow=True)
        self.assertIn("four digits", r.content.decode())
        r = self.client.get(reverse("accounting_year_end") + f"?year={TODAY.year + 5}", follow=True)
        self.assertIn("already started", r.content.decode())


class ChartOfAccountsViews(AccountingUISetup):
    def test_accounts_page_groups_and_balances(self):
        r = self.client.get(reverse("accounting_accounts"))
        html = r.content.decode()
        for label in ("Asset", "Liability", "Equity", "Income", "Cost of goods sold", "Expense"):
            self.assertIn(label, html)
        self.assertIn("Display cases", html)
        self.assertIn("System", html)
        self.assertIn("Expense category", html)
        groups = {g["type"]: g for g in r.context["groups"]}
        ar_row = [a for a in groups[AccountType.ASSET]["accounts"] if a.system_key == "ar"][0]
        self.assertEqual(ar_row.current_balance, D("438.70"))

    def test_toggle_active_only_for_custom_accounts(self):
        r = self.client.post(reverse("accounting_accounts"), {"action": "toggle", "pk": self.custom.pk})
        self.assertRedirects(r, reverse("accounting_accounts"))
        self.custom.refresh_from_db()
        self.assertFalse(self.custom.active)
        # hidden by default, shown with ?inactive=1
        self.assertNotIn("Display cases", self.client.get(reverse("accounting_accounts")).content.decode())
        self.assertIn("Display cases", self.client.get(reverse("accounting_accounts") + "?inactive=1").content.decode())
        ar = Account.system("ar")
        r = self.client.post(reverse("accounting_accounts"), {"action": "toggle", "pk": ar.pk}, follow=True)
        ar.refresh_from_db()
        self.assertTrue(ar.active)
        self.assertIn("System accounts cannot be turned off", r.content.decode())

    def test_create_account_validates_and_saves(self):
        url = reverse("accounting_account_create")
        # duplicate code
        r = self.client.post(url, {"code": "1600", "name": "Van", "type": "asset",
                                   "subtype": "fixed_asset", "description": "", "active": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("already used", r.content.decode())
        # subtype that does not belong to the type
        r = self.client.post(url, {"code": "1700", "name": "Van", "type": "asset",
                                   "subtype": "loan", "description": "", "active": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("does not belong", r.content.decode())
        # ok
        r = self.client.post(url, {"code": "1700", "name": "Van", "type": "asset",
                                   "subtype": "fixed_asset", "description": "Delivery van", "active": "on"})
        self.assertRedirects(r, reverse("accounting_accounts"))
        van = Account.objects.get(code="1700")
        self.assertFalse(van.is_system)
        self.assertEqual(van.name, "Van")

    def test_create_prefills_code_for_type(self):
        r = self.client.get(reverse("accounting_account_create") + "?type=expense")
        self.assertEqual(r.context["form"].initial["type"], "expense")
        self.assertTrue(r.context["form"].initial["code"].startswith("6"))

    def test_edit_custom_and_system_accounts(self):
        r = self.client.post(reverse("accounting_account_edit", args=[self.custom.pk]),
                             {"code": "1650", "name": "Display cases & fixtures", "type": "asset",
                              "subtype": "fixed_asset", "description": "", "active": "on"})
        self.assertRedirects(r, reverse("accounting_accounts"))
        self.custom.refresh_from_db()
        self.assertEqual((self.custom.code, self.custom.name), ("1650", "Display cases & fixtures"))
        ar = Account.system("ar")
        r = self.client.post(reverse("accounting_account_edit", args=[ar.pk]),
                             {"code": "9999", "type": "expense", "subtype": "expense",
                              "name": "Money customers owe", "description": "Open invoices"})
        self.assertRedirects(r, reverse("accounting_accounts"))
        ar.refresh_from_db()
        self.assertEqual(ar.code, "1200")                # untouched
        self.assertEqual(ar.type, AccountType.ASSET)     # untouched
        self.assertEqual(ar.name, "Money customers owe")
        self.assertEqual(ar.description, "Open invoices")
        # blank name rejected
        r = self.client.post(reverse("accounting_account_edit", args=[ar.pk]), {"name": "  ", "description": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("required", r.content.decode())
        ar.refresh_from_db()
        self.assertEqual(ar.name, "Money customers owe")   # unchanged


class PeriodLockView(AccountingUISetup):
    def test_set_and_clear_lock(self):
        url = reverse("accounting_period_lock")
        last_month_end = TODAY.replace(day=1) - timedelta(days=1)
        r = self.client.post(url, {"locked_through": last_month_end.isoformat(), "note": "Filed"})
        self.assertRedirects(r, reverse("accounting_index"))
        lock = FiscalPeriodLock.get()
        self.assertEqual(lock.locked_through, last_month_end)
        self.assertTrue(lock.is_locked(last_month_end))
        self.assertFalse(lock.is_locked(TODAY))
        html = self.client.get(reverse("accounting_index")).content.decode()
        self.assertIn("Closed through", html)
        r = self.client.post(url, {"action": "clear"})
        self.assertRedirects(r, reverse("accounting_index"))
        self.assertIsNone(FiscalPeriodLock.get().locked_through)

    def test_future_date_rejected(self):
        r = self.client.post(reverse("accounting_period_lock"),
                             {"locked_through": (TODAY + timedelta(days=3)).isoformat(), "note": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("already ended", r.content.decode())
        self.assertIsNone(FiscalPeriodLock.get().locked_through)


class AccountingPermissions(AccountingUISetup):
    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        for url in self.page_urls():
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/accounts/login/", r["Location"])

    def test_user_without_module_is_blocked(self):
        user = _role_user("clerk", ["orders"], can_see_costs=True)
        self.client.force_login(user)
        for url in self.page_urls():
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertIn(r.status_code, (302, 403), url)
                if r.status_code == 302:
                    self.assertNotIn("/accounting/", r["Location"])

    def test_module_without_costs_is_forbidden(self):
        user = _role_user("helper", ["dashboard", "accounting"], can_see_costs=False)
        self.client.force_login(user)
        for url in self.page_urls():
            if url.startswith(reverse("accounting_period_lock")):
                continue       # admin-only; covered below
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.get(reverse("accounting_year_end") + f"?year={TODAY.year}").status_code, 403)
        self.assertEqual(self.client.get(reverse("accounting_pnl") + "?export=csv").status_code, 403)

    def test_bookkeeper_sees_statements_but_not_period_lock(self):
        user = _role_user("books", ["dashboard", "accounting"], can_see_costs=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("accounting_pnl")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounting_year_end") + f"?year={TODAY.year}").status_code, 200)
        r = self.client.get(reverse("accounting_period_lock"))
        self.assertEqual(r.status_code, 302)        # admin_required -> login redirect
        r = self.client.post(reverse("accounting_period_lock"), {"locked_through": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(FiscalPeriodLock.get().locked_through)


class ReportHelpers(TestCase):
    """Pure helpers in accounting/reports.py."""

    def setUp(self):
        self.rf = RequestFactory()
        CompanySettings.get()

    def _req(self, qs=""):
        return self.rf.get("/accounting/x/" + (f"?{qs}" if qs else ""))

    def test_basis_from_request(self):
        self.assertEqual(rp.basis_from_request(self._req("basis=accrual")), ACCRUAL)
        self.assertEqual(rp.basis_from_request(self._req("basis=CASH")), CASH)
        self.assertEqual(rp.basis_from_request(self._req("basis=nope")), CASH)   # company default
        s = CompanySettings.get(); s.accounting_basis_default = "accrual"; s.save()
        self.assertEqual(rp.basis_from_request(self._req()), ACCRUAL)

    def test_period_presets_and_defaults(self):
        start, end = rp.period_from_request(self._req())
        self.assertEqual((start, end), (TODAY.replace(day=1), TODAY))
        start, end = rp.period_from_request(self._req("preset=last_month"))
        self.assertEqual(end, TODAY.replace(day=1) - timedelta(days=1))
        self.assertEqual(start, end.replace(day=1))
        start, end = rp.period_from_request(self._req("preset=ytd"))
        self.assertEqual(start, YEAR_START)
        start, end = rp.period_from_request(self._req("preset=last_year"))
        self.assertEqual((start, end), (date(TODAY.year - 1, 1, 1), date(TODAY.year - 1, 12, 31)))
        # explicit dates win over a preset
        start, end = rp.period_from_request(self._req("preset=ytd&start=2024-02-01&end=2024-02-29"))
        self.assertEqual((start, end), (date(2024, 2, 1), date(2024, 2, 29)))
        self.assertEqual(rp.active_preset(TODAY.replace(day=1), TODAY), "this_month")
        self.assertEqual(rp.active_preset(date(2001, 1, 1), date(2001, 1, 2)), "")

    def test_fiscal_year_helpers_respect_start_month(self):
        s = CompanySettings.get(); s.fiscal_year_start_month = 7; s.save()
        self.assertEqual(rp.fiscal_year_bounds(2025), (date(2025, 7, 1), date(2026, 6, 30)))
        self.assertEqual(rp.fiscal_year_label(date(2025, 7, 1), date(2026, 6, 30)), "FY 2025–26")
        self.assertEqual(rp.fiscal_year_label(date(2025, 1, 1), date(2025, 12, 31)), "FY 2025")
        self.assertEqual(rp.last_complete_fiscal_year(date(2026, 3, 1), 7), 2024)
        self.assertEqual(rp.last_complete_fiscal_year(date(2026, 8, 1), 7), 2025)
        self.assertEqual(rp.last_complete_fiscal_year(date(2026, 10, 14), 1), 2025)
        start, end = rp.period_from_request(self._req("preset=ytd"))
        self.assertEqual(start.month, 7 if TODAY.month >= 7 else 7)
        self.assertEqual(start, date(TODAY.year if TODAY.month >= 7 else TODAY.year - 1, 7, 1))

    def test_parse_month_and_quarter(self):
        self.assertEqual(rp.parse_month("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertIsNone(rp.parse_month("2026-13"))
        self.assertIsNone(rp.parse_month("junk"))
        self.assertIsNone(rp.parse_month(""))
        self.assertEqual(rp.quarter_bounds(date(2026, 5, 9)), (date(2026, 4, 1), date(2026, 6, 30)))
        self.assertEqual(rp.quarter_bounds(date(2026, 12, 1)), (date(2026, 10, 1), date(2026, 12, 31)))

    def test_money_and_pct_formatting(self):
        self.assertEqual(rp.money(D("1234.5")), "1234.50")
        self.assertEqual(rp.money(D("-0.10")), "-0.10")
        self.assertEqual(rp.money(None), "")
        self.assertEqual(rp.pct(D("42.456")), "42.5%")
        self.assertEqual(rp.pct(None), "")

    def test_bridge_rows_have_an_explanation_for_every_engine_label(self):
        Account.ensure_system_accounts()
        cmp = compare_bases(YEAR_START, TODAY)
        rows = rp.bridge_rows(cmp)
        self.assertEqual(len(rows), len(cmp["bridge"]))
        for r in rows:
            self.assertIn(r["label"], rp.BRIDGE_EXPLANATIONS)
            self.assertGreater(len(r["explanation"]), 40)

    def test_compare_rows_bridge_sums_to_net_difference_in_simple_case(self):
        Account.ensure_system_accounts()
        cat = ExpenseCategory.objects.create(name="Software")
        Expense.objects.create(date=TODAY, category=cat, vendor="Adobe", amount=D("60.00"))
        cmp = compare_bases(YEAR_START, TODAY)
        self.assertEqual(cmp["net_difference"], D("-60.00"))
        self.assertEqual(sum((a for _, a in cmp["bridge"]), D("0")), D("-60.00"))
        csv_rows = rp.compare_rows(cmp)
        self.assertEqual(csv_rows[-1][2], "Sum of bridge rows")
        self.assertEqual(csv_rows[-1][-1], "-60.00")

    def test_equity_chart_geometry(self):
        rows = [{"month_end": date(2026, m, 28), "label": f"M{m}", "equity": D(v),
                 "assets": D(0), "liabilities": D(0), "net_income": D(0), "equity_change": None}
                for m, v in ((1, "-200"), (2, "0"), (3, "1500"), (4, "900"))]
        chart = rp.equity_chart(rows)
        self.assertEqual(len(chart["bars"]), 4)
        self.assertTrue(chart["bars"][0]["neg"])
        self.assertFalse(chart["bars"][2]["neg"])
        self.assertEqual(chart["bars"][1]["h"], 0.0)                      # zero equity = no bar
        self.assertGreater(chart["bars"][2]["h"], chart["bars"][3]["h"])
        # negative bar hangs below the zero line, positive bars sit on it
        self.assertEqual(chart["bars"][0]["y"], chart["zero_y"])
        self.assertAlmostEqual(chart["bars"][2]["y"] + chart["bars"][2]["h"], chart["zero_y"], places=0)
        self.assertGreaterEqual(len(chart["ticks"]), 3)
        labels = [t["label"] for t in chart["ticks"]]
        self.assertIn("$0", labels)
        self.assertTrue(all(0 <= b["x"] <= chart["width"] for b in chart["bars"]))
        self.assertIsNone(rp.equity_chart([]))
        self.assertEqual(rp._tick_label(D("2500")), "$2.5k")
        self.assertEqual(rp._tick_label(D("-1000")), "-$1k")
        self.assertEqual(rp._tick_label(D("1500000")), "$1.5M")

    def test_package_zip_is_self_describing(self):
        Account.ensure_system_accounts()
        data = rp.build_package_zip(date(TODAY.year, 1, 1), TODAY, "Test package")
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(len(zf.namelist()), len(rp.PACKAGE_FILES) + 1)
        for name, _ in rp.PACKAGE_FILES:
            self.assertIn(name, zf.namelist())
            self.assertTrue(zf.read(name).decode().splitlines()[0])      # has a header row
