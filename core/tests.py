"""Integration layer: dashboard Money section, Settings (accounting + bank
feed cards), the weekly review digest, background schedules, the reports
hub, and the seed_demo smoke test (the demo books must balance)."""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounting.ledger import ACCRUAL, CASH, Journal
from accounting.models import Account
from banking.models import BankAccount, BankTransaction, CategoryRule
from core.models import CompanySettings, Role, UserProfile
from core.tasks import money_attention, send_review_digest
from expenses.models import (Budget, Expense, ExpenseCategory, MileageEntry,
                             RecurringExpense, Vendor)
from inventory.models import Receipt

D = Decimal
TODAY = timezone.localdate()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class DashboardMoneyTests(TestCase):
    def setUp(self):
        Account.ensure_system_accounts()
        self.admin = User.objects.create_superuser("boss", password="x")
        self.checking = BankAccount.objects.create(
            name="Checking", account_type="checking", opening_balance=D("1000.00"),
            opening_date=TODAY - timedelta(days=30))
        self.card = BankAccount.objects.create(
            name="Visa", account_type="credit_card", opening_balance=D("250.00"),
            opening_date=TODAY - timedelta(days=30))
        self.supplies = ExpenseCategory.objects.create(name="Shipping supplies")
        # one unreviewed outflow, one auto-categorized card charge (-> unreviewed expense)
        BankTransaction.objects.create(account=self.checking, date=TODAY - timedelta(days=3),
                                       amount=D("-12.34"), description="MYSTERY CHARGE")
        t = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("-40.00"),
                                           description="ULINE SHIP SUPPLIES")
        t.categorize(self.supplies.ensure_account(), auto=True)
        # two open bills: one due in 3 days, one already overdue
        Expense.objects.create(date=TODAY, category=self.supplies, vendor="Boxes Inc",
                               amount=D("75.00"), due_date=TODAY + timedelta(days=3))
        Expense.objects.create(date=TODAY, category=self.supplies, vendor="Late Co",
                               amount=D("20.00"), due_date=TODAY - timedelta(days=2))

    def test_money_attention_counts(self):
        info = money_attention(TODAY)
        self.assertEqual(info["unreviewed"], 1)
        self.assertEqual(info["unreviewed_oldest"], TODAY - timedelta(days=3))
        self.assertEqual(info["bills_due"], 2)
        self.assertEqual(info["bills_overdue"], 1)
        self.assertEqual(info["bills_due_total"], D("95.00"))
        self.assertEqual(info["bills_unpaid_total"], D("95.00"))
        self.assertEqual(info["expenses_to_review"], 1)
        # the auto expense + both bills are dated this month and have no receipt
        self.assertEqual(info["missing_receipts"], 3)
        self.assertEqual(info["missing_receipts_total"], D("135.00"))
        self.assertEqual(info["connections_attention"], 0)
        self.assertEqual(info["total"], 1 + 2 + 3 + 1)

    def test_money_attention_includes_open_supplier_bills(self):
        from inventory.models import Product, ReceiptLine, Supplier
        p = Product.objects.create(sku="X-1", name="X", default_price=D("10.00"))
        r = Receipt.objects.create(supplier=Supplier.objects.create(name="GTS"),
                                   date=TODAY - timedelta(days=20), due_date=TODAY - timedelta(days=1))
        ReceiptLine.objects.create(receipt=r, product=p, qty=10, unit_cost=D("5.00"))
        r.post()
        info = money_attention(TODAY)
        self.assertEqual(info["bills_due"], 3)
        self.assertEqual(info["bills_overdue"], 2)
        self.assertEqual(info["bills_due_total"], D("145.00"))
        # paying it by hand removes it
        r.paid_date = TODAY
        r.save(update_fields=["paid_date"])
        self.assertEqual(money_attention(TODAY)["bills_due"], 2)

    def test_dashboard_renders_money_section_for_admin(self):
        self.client.force_login(self.admin)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for text in ["Money", "Cash position", "Net profit this month", "Net profit, year to date",
                     "Equity", "Needs attention", "Bank transactions to review",
                     "Bills due in 7 days", "missing a receipt", "Auto-created expenses to confirm",
                     "Bank &amp; card balances", "Operations"]:
            self.assertIn(text, html, text)
        # cash position: 1000 - 12.34 in the bank, 250 + 40 owed on the card
        self.assertIn("987.66", html)
        self.assertIn("290.00", html)
        self.assertIn("697.66", html)
        # the unreviewed badge on the account row and the attention counts
        self.assertIn("1 to review", html)
        self.assertIn("1 overdue", html)

    def test_dashboard_money_profit_matches_ledger(self):
        self.client.force_login(self.admin)
        r = self.client.get("/")
        # cash basis (default): 12.34 uncategorized + 40 supplies = -52.34 this month
        # (both transactions are dated this month only if today >= the 4th; use the ledger)
        pl = Journal(CASH, [TODAY]).income_statement(TODAY.replace(day=1), TODAY)
        expected = f"{abs(pl['net_income']):,.2f}"
        self.assertIn(expected, r.content.decode())

    def test_counts_only_for_user_without_costs(self):
        role = Role.objects.create(name="Clerk", modules=["dashboard", "banking", "expenses"],
                                   can_see_costs=False)
        u = User.objects.create_user("clerk", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.client.force_login(u)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Needs attention", html)
        self.assertIn("Bank transactions to review", html)
        self.assertNotIn("Cash position", html)
        self.assertNotIn("Net profit", html)
        self.assertNotIn("697.66", html)
        self.assertNotIn("95.00", html)          # bill totals hidden too

    def test_dashboard_with_no_bank_accounts_offers_setup(self):
        BankTransaction.objects.all().delete()
        BankAccount.all_objects.all().delete()
        self.client.force_login(self.admin)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No bank accounts yet", r.content.decode())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class SettingsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="x")
        self.client.force_login(self.admin)

    def _data(self, **over):
        data = {
            "company_name": "SFL Hobby Distribution LLC", "default_tax_rate": "6.5",
            "invoice_template": "modern", "default_payment_terms": "net30",
            "alert_email": "owner@example.com",
            "fiscal_year_start_month": "1", "accounting_basis_default": "accrual",
            "books_start_date": "2026-01-01", "mileage_rate": "0.700",
            "sales_tax_filing_frequency": "monthly",
            "plaid_client_id": "client-abc", "plaid_secret": "s3cret", "plaid_env": "sandbox",
            "bank_sync_enabled": "on", "expense_review_digest": "on",
        }
        for key, prefix in (("po", "PO-"), ("invoice", "INV-"), ("credit", "CM-")):
            data[f"seq_{key}-prefix"] = prefix
            data[f"seq_{key}-next_number"] = "1"
            data[f"seq_{key}-padding"] = "4"
        data.update(over)
        return data

    def test_settings_page_renders_new_cards(self):
        r = self.client.get("/settings/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for text in ["Accounting", "Fiscal year starts in", "Books start date", "Mileage rate",
                     "Bank feed (Plaid)", "Plaid client ID", "How to get Plaid keys",
                     "Not configured", "January", "December"]:
            self.assertIn(text, html, text)

    def test_post_saves_accounting_and_plaid_fields(self):
        r = self.client.post("/settings/", self._data())
        self.assertEqual(r.status_code, 302)
        cs = CompanySettings.get()
        self.assertEqual(cs.accounting_basis_default, "accrual")
        self.assertEqual(cs.fiscal_year_start_month, 1)
        self.assertEqual(cs.books_start_date, date(2026, 1, 1))
        self.assertEqual(cs.mileage_rate, D("0.700"))
        self.assertEqual(cs.sales_tax_filing_frequency, "monthly")
        self.assertEqual(cs.plaid_client_id, "client-abc")
        self.assertEqual(cs.plaid_secret, "s3cret")
        self.assertEqual(cs.plaid_env, "sandbox")
        self.assertTrue(cs.bank_sync_enabled)
        self.assertTrue(cs.expense_review_digest)
        html = self.client.get("/settings/").content.decode()
        self.assertIn("Plaid configured", html)
        self.assertIn("Settings saved.", html)
        # the secret is rendered back (masked input) so re-saving keeps it
        self.assertIn('value="s3cret"', html)

    def test_fiscal_month_select_offers_twelve_months(self):
        r = self.client.post("/settings/", self._data(fiscal_year_start_month="7"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CompanySettings.get().fiscal_year_start_month, 7)

    def test_invalid_month_shows_error_and_does_not_save(self):
        r = self.client.post("/settings/", self._data(fiscal_year_start_month="13"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Pick a month between January and December", html)
        self.assertIn("Settings were not saved", html)
        self.assertEqual(CompanySettings.get().fiscal_year_start_month, 1)

    def test_plaid_id_without_secret_is_rejected(self):
        r = self.client.post("/settings/", self._data(plaid_secret=""))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Enter both the Plaid client ID and the secret", r.content.decode())
        self.assertEqual(CompanySettings.get().plaid_client_id, "")

    def test_negative_mileage_rate_rejected(self):
        r = self.client.post("/settings/", self._data(mileage_rate="-1"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("cannot be negative", r.content.decode())

    def test_non_admin_cannot_open_settings(self):
        u = User.objects.create_user("clerk", password="x")
        self.client.force_login(u)
        r = self.client.get("/settings/")
        self.assertNotEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# Weekly review digest
# ---------------------------------------------------------------------------
class ReviewDigestTests(TestCase):
    def setUp(self):
        Account.ensure_system_accounts()
        self.acct = BankAccount.objects.create(name="Checking", account_type="checking",
                                               opening_balance=D("500.00"),
                                               opening_date=TODAY - timedelta(days=30))

    def _enable(self):
        cs = CompanySettings.get()
        cs.expense_review_digest = True
        cs.alert_email = "owner@example.com"
        cs.save()

    def test_disabled_returns_string_without_sending(self):
        out = send_review_digest()
        self.assertIsInstance(out, str)
        self.assertEqual(out, "Review digest disabled.")
        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_to_review(self):
        self._enable()
        self.assertEqual(send_review_digest(), "Nothing to review this week.")
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_digest_with_every_section(self):
        self._enable()
        from banking.models import BankConnection
        BankTransaction.objects.create(account=self.acct, date=TODAY - timedelta(days=9),
                                       amount=D("-33.00"), description="UNKNOWN VENDOR")
        cat = ExpenseCategory.objects.create(name="Rent & storage")
        Expense.objects.create(date=TODAY, category=cat, vendor="Landlord", amount=D("1200.00"),
                               due_date=TODAY + timedelta(days=2))
        Expense.objects.create(date=TODAY, category=cat, vendor="Auto", amount=D("10.00"),
                               paid_date=TODAY, is_reviewed=False, source="recurring")
        BankConnection.objects.create(provider="plaid", institution_name="Chase", status="relink")
        out = send_review_digest()
        self.assertTrue(out.startswith("Sent review digest"), out)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["owner@example.com"])
        self.assertIn("need a look", msg.subject)
        body = msg.body
        self.assertIn("1 bank/card transaction(s) waiting for review", body)
        self.assertIn("1 bill(s) due by", body)
        self.assertIn("1,200.00", body)
        self.assertIn("no receipt attached", body)
        self.assertIn("created automatically", body)
        self.assertIn("Chase", body)
        self.assertIn("Uncategorized", body)
        self.assertIn("/banking/", body)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
class SetupSchedulesTests(TestCase):
    def test_registers_all_jobs_idempotently(self):
        from django_q.models import Schedule
        call_command("setup_schedules", stdout=StringIO())
        names = set(Schedule.objects.values_list("name", flat=True))
        self.assertTrue({"shipstation-sync", "low-stock-digest", "bank-sync",
                         "recurring-expenses", "review-digest"} <= names, names)
        bank = Schedule.objects.get(name="bank-sync")
        self.assertEqual(bank.func, "banking.tasks.sync_all_connections")
        self.assertEqual(bank.schedule_type, Schedule.HOURLY)
        rec = Schedule.objects.get(name="recurring-expenses")
        self.assertEqual(rec.func, "expenses.tasks.generate_recurring_expenses")
        digest = Schedule.objects.get(name="review-digest")
        self.assertEqual(digest.func, "core.tasks.send_review_digest")
        self.assertIn(digest.schedule_type, (Schedule.CRON, Schedule.WEEKLY))
        if digest.schedule_type == Schedule.CRON:
            self.assertEqual(digest.cron, "0 7 * * 1")
        call_command("setup_schedules", stdout=StringIO())
        self.assertEqual(Schedule.objects.filter(name="review-digest").count(), 1)


# ---------------------------------------------------------------------------
# seed_demo smoke test
# ---------------------------------------------------------------------------
class SeedDemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=StringIO())

    def test_money_data_counts(self):
        self.assertEqual(BankAccount.objects.count(), 2)
        checking = BankAccount.objects.get(account_type="checking")
        card = BankAccount.objects.get(account_type="credit_card")
        self.assertEqual(checking.opening_balance, D("12500.00"))
        self.assertEqual(card.opening_balance, D("1840.00"))
        self.assertEqual(checking.opening_date, TODAY - timedelta(days=90))
        self.assertTrue(checking.is_default)

        n = BankTransaction.objects.count()
        self.assertGreaterEqual(n, 60, n)
        self.assertLessEqual(n, 90, n)
        self.assertTrue(BankTransaction.objects.filter(date__lt=checking.opening_date).count() == 0)
        statuses = {s: BankTransaction.objects.filter(status=s).count()
                    for s, _ in BankTransaction.STATUS_CHOICES}
        self.assertGreaterEqual(statuses["unreviewed"], 10, statuses)
        self.assertLessEqual(statuses["unreviewed"], 16, statuses)
        self.assertEqual(statuses["transfer"], 6, statuses)
        self.assertTrue(BankTransaction.objects.filter(status="matched", payments__isnull=False).exists())
        self.assertTrue(BankTransaction.objects.filter(status="matched", receipts__isnull=False).exists())
        self.assertTrue(BankTransaction.objects.filter(payments__isnull=False)
                        .annotate(n=__import__("django").db.models.Count("payments")).filter(n__gte=2).exists(),
                        "expected at least one deposit batch covering several payments")

        self.assertEqual(CategoryRule.objects.count(), 3)
        self.assertEqual(CategoryRule.objects.filter(times_applied__gt=0).count(), 3)
        shipping = Account.system("shipping_expense")
        self.assertGreaterEqual(BankTransaction.objects.filter(
            category_account=shipping, auto_categorized=True, rule_applied__isnull=False).count(), 12)

        self.assertEqual(RecurringExpense.objects.count(), 1)
        self.assertEqual(MileageEntry.objects.count(), 6)
        self.assertEqual(Budget.objects.count(), 4)
        self.assertGreaterEqual(Vendor.objects.count(), 10)
        self.assertTrue(Vendor.objects.filter(is_1099=True).exists())

        self.assertGreaterEqual(Expense.objects.filter(source="bank").count(), 9)
        self.assertTrue(Expense.objects.filter(source="bank", is_reviewed=False).exists())
        manual = Expense.objects.filter(source="manual")
        self.assertEqual(manual.count(), 18)
        self.assertFalse(manual.filter(paid_date__isnull=True).exists())
        self.assertTrue(manual.filter(payment_method="personal", paid_from__isnull=True).exists())
        self.assertTrue(manual.filter(paid_from=checking).exists())

        cs = CompanySettings.get()
        self.assertEqual(cs.books_start_date, checking.opening_date)
        self.assertTrue(cs.expense_review_digest)

        # two supplier bills paid through the feed, one left open and due within a week
        open_bills = [r for r in Receipt.objects.filter(posted=True) if not r.is_paid]
        self.assertEqual(len(open_bills), 1)
        self.assertLessEqual(open_bills[0].due_date, TODAY + timedelta(days=7))
        self.assertEqual(Receipt.objects.filter(posted=True, paid_date__isnull=False).count(), 2)

    def test_demo_books_balance_on_both_bases(self):
        for basis in (CASH, ACCRUAL):
            j = Journal(basis, [TODAY])
            self.assertEqual(j.validate(), [], basis)
            bs = j.balance_sheet(TODAY)
            self.assertTrue(bs["balanced"], f"{basis} off by {bs['difference']}")
            tb = j.trial_balance(TODAY)
            self.assertTrue(tb["balanced"], basis)
        # the required assertion from the brief, verbatim
        self.assertTrue(Journal("accrual", [TODAY]).balance_sheet(TODAY)["balanced"])
        # month-end checkpoints (what the dashboard's equity trend uses) balance too
        from accounting.ledger import equity_trend
        for row in equity_trend(ACCRUAL, 3, as_of=TODAY):
            self.assertEqual(row["assets"], row["liabilities"] + row["equity"], row)

    def test_demo_ledger_contents_make_sense(self):
        start = TODAY - timedelta(days=90)
        cash = Journal(CASH, [TODAY]).income_statement(start, TODAY)
        cogs = {r["account"].system_key: r["amount"] for r in cash["cogs"]}
        self.assertGreater(cogs.get("shipping_expense", D("0")), D("400.00"))   # Pirate Ship + USPS
        self.assertGreater(cogs.get("inventory_purchases", D("0")), D("1000.00"))  # supplier ACH
        income = {r["account"].system_key: r["amount"] for r in cash["income"]}
        self.assertGreater(income.get("sales", D("0")), D("0"))
        self.assertGreater(income.get("other_income", D("0")), D("0"))
        self.assertGreater(income.get("uncat_income", D("0")), D("0"))          # the Zelle deposit
        expenses = {r["account"].name: r["amount"] for r in cash["expenses"]}
        self.assertIn("Uncategorized expense", expenses)
        self.assertIn("Shipping supplies", expenses)
        # accrual balance sheet carries inventory, AR and the open supplier bill
        bs = Journal(ACCRUAL, [TODAY]).balance_sheet(TODAY)
        keys = {r["account"].system_key: r["amount"] for r in bs["assets"] + bs["liabilities"]
                if r["account"] is not None}
        self.assertGreater(keys.get("inventory", D("0")), D("0"))
        self.assertGreater(keys.get("ap", D("0")), D("0"))
        # bank balances on the balance sheet equal the feed balances
        checking = BankAccount.objects.get(account_type="checking")
        rows = {r["account"].pk: r["amount"] for r in bs["assets"]}
        ledger_checking = rows.get(checking.ledger_account_id, D("0"))
        hand_paid = sum((e.amount for e in Expense.objects.filter(
            source="manual", paid_from=checking)), D("0"))
        self.assertEqual(ledger_checking, (checking.current_balance - hand_paid).quantize(D("0.01")))

    def test_seed_demo_is_idempotent(self):
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(BankAccount.objects.count(), 2)
        self.assertEqual(CategoryRule.objects.count(), 3)
        self.assertEqual(RecurringExpense.objects.count(), 1)
        self.assertEqual(MileageEntry.objects.count(), 6)
        self.assertEqual(Expense.objects.filter(source="manual").count(), 18)

    def test_pages_render_with_demo_data(self):
        admin = User.objects.get(username="admin")
        self.client.force_login(admin)
        for url in ["/", "/settings/", "/reports/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = self.client.get("/").content.decode()
        self.assertIn("Chase Business Checking", html)
        self.assertIn("Chase Ink Business Cash", html)
        self.assertIn("to review", html)
        reports = self.client.get("/reports/").content.decode()
        for text in ["Financial statements", "Expenses &amp; bills", "Operational P&amp;L (margin view)",
                     "live under Accounting", "Am I building equity?", "Unpaid bills", "1099 contractors"]:
            self.assertIn(text, reports, text)

    def test_bookkeeper_sees_money_pages(self):
        books = User.objects.get(username="books")
        self.client.force_login(books)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cash position", r.content.decode())   # Bookkeeper role can see costs
