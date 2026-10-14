"""
Expenses module: money logic (recurring schedules, AP aging, budgets,
mileage, by-month matrix, 1099 thresholds, year-end ZIP) plus every page
rendering for an admin and refusing a user without the module.
"""
import csv
import io
import tempfile
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import ACCRUAL, CASH, Account
from banking.models import BankAccount, BankTransaction
from core.models import CompanySettings, Role, UserProfile
from expenses import reports
from expenses.forms import QuickExpenseForm
from expenses.models import (Budget, Expense, ExpenseCategory, MileageEntry, RecurringExpense,
                             Vendor)
from expenses.tasks import generate_recurring_expenses
from inventory.models import Product, Receipt, ReceiptLine, Supplier

D = Decimal
TODAY = timezone.localdate()
MEDIA = tempfile.mkdtemp(prefix="sfl-expense-tests-")


class ExpenseTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(self.admin)
        self.checking = BankAccount.objects.create(name="Checking", account_type="checking",
                                                  is_default=True, opening_date=date(2020, 1, 1))
        self.card = BankAccount.objects.create(name="Visa", account_type="credit_card",
                                               opening_date=date(2020, 1, 1))
        self.supplies = ExpenseCategory.objects.create(name="Shipping supplies", tax_line="22")
        self.software = ExpenseCategory.objects.create(name="Software", tax_line="18")
        self.meals = ExpenseCategory.objects.create(name="Meals", tax_line="24b", deductible_pct=50)
        self.amazon = Vendor.objects.create(name="Amazon", aliases="AMZN Mktp\nAMAZON.COM",
                                            default_category=self.supplies)

    def expense(self, **kw):
        kw.setdefault("date", TODAY)
        kw.setdefault("amount", D("10.00"))
        kw.setdefault("category", self.supplies)
        return Expense.objects.create(**kw)


# ---------------------------------------------------------------------------
# Quick add
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=MEDIA)
class QuickAddTests(ExpenseTestBase):
    def test_register_quick_add_creates_paid_expense_from_default_account(self):
        r = self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "category": self.supplies.pk, "vendor": "Uline",
            "amount": "48.20", "paid": "on", "paid_from": self.checking.pk, "note": "boxes"},
            follow=True)
        self.assertEqual(r.status_code, 200)
        e = Expense.objects.get()
        self.assertEqual(e.amount, D("48.20"))
        self.assertEqual(e.paid_date, date(2025, 3, 10))
        self.assertEqual(e.paid_from, self.checking)
        self.assertEqual(e.payment_method, "ach")
        self.assertTrue(e.is_paid)
        self.assertEqual(e.source, "manual")
        self.assertEqual(e.created_by, self.admin)

    def test_quick_add_unpaid_is_open_bill(self):
        self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "category": self.supplies.pk,
            "vendor": "Uline", "amount": "48.20"})
        e = Expense.objects.get()
        self.assertFalse(e.is_paid)
        self.assertIsNone(e.paid_from)
        self.assertEqual(e.payment_method, "")

    def test_quick_add_new_category_and_vendor_alias_link(self):
        self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "new_category": "Trade show fees",
            "vendor": "AMZN Mktp US*2K3", "amount": "12.00", "paid": "on"})
        e = Expense.objects.get()
        self.assertEqual(e.category.name, "Trade show fees")
        self.assertIsNotNone(e.category.account)         # ledger account auto-created
        self.assertEqual(e.vendor_link, self.amazon)     # alias matched
        self.assertEqual(e.payment_method, "personal")   # paid, no bank account

    def test_quick_add_requires_category_and_shows_error(self):
        r = self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "vendor": "Uline", "amount": "48.20", "paid": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pick a category")
        self.assertEqual(Expense.objects.count(), 0)

    def test_phone_quick_capture_sets_source_and_receipt(self):
        r = self.client.post(reverse("expense_quick"), {
            "date": TODAY.isoformat(), "category": self.meals.pk, "vendor": "Chipotle",
            "amount": "14.75", "paid": "on", "paid_from": self.card.pk,
            "receipt_file": SimpleUploadedFile("r.jpg", b"\xff\xd8\xff\xe0jpegdata", "image/jpeg")},
            follow=True)
        self.assertEqual(r.status_code, 200)
        e = Expense.objects.get()
        self.assertEqual(e.source, "quick")
        self.assertTrue(e.has_receipt)
        self.assertEqual(e.payment_method, "card")
        self.assertContains(r, "Add another")
        self.assertContains(r, "Saved $14.75")

    def test_quick_form_default_paid_from_is_default_account(self):
        form = QuickExpenseForm()
        self.assertEqual(form.initial["paid_from"], self.checking)


# ---------------------------------------------------------------------------
# Full form / edit / linked bank transaction
# ---------------------------------------------------------------------------
class ExpenseFormTests(ExpenseTestBase):
    def test_create_with_bank_account_defaults_paid_date(self):
        r = self.client.post(reverse("expense_create"), {
            "date": "2025-02-01", "category": self.software.pk, "vendor": "Adobe",
            "amount": "54.99", "paid_from": self.card.pk, "payment_method": "card",
            "reference": "", "note": "", "due_date": "", "paid_date": ""})
        self.assertEqual(r.status_code, 302)
        e = Expense.objects.get()
        self.assertEqual(e.paid_date, date(2025, 2, 1))
        self.assertEqual(e.paid_from, self.card)

    def test_zero_amount_rejected(self):
        r = self.client.post(reverse("expense_create"), {
            "date": "2025-02-01", "category": self.software.pk, "amount": "0"})
        self.assertContains(r, "Amount can")
        self.assertEqual(Expense.objects.count(), 0)

    def test_linked_expense_amount_locked_and_delete_blocked(self):
        t = BankTransaction.objects.create(account=self.card, date=date(2025, 4, 2), amount=D("-40.00"),
                                           description="ULINE SHIP SUPPLIES")
        t.categorize(self.supplies.ensure_account(), user=self.admin)
        e = t.expense
        url = reverse("expense_edit", args=[e.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Linked to a bank transaction")
        self.assertContains(r, reverse("bank_transaction_detail", args=[t.pk]))
        # try to change the amount and the category
        r = self.client.post(url, {"date": "2025-01-01", "amount": "999", "category": self.software.pk,
                                   "vendor": "Uline", "note": "fixed", "reference": "", "due_date": "",
                                   "paid_date": "", "payment_method": "card"})
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual(e.amount, D("40.00"))                 # locked
        self.assertEqual(e.date, date(2025, 4, 2))             # locked
        self.assertEqual(e.category, self.software)            # editable
        self.assertEqual(t.category_account, self.software.account)   # kept in sync
        self.assertEqual(e.note, "fixed")
        r = self.client.post(url, {"action": "delete"}, follow=True)
        self.assertContains(r, "came from a bank transaction")
        self.assertTrue(Expense.objects.filter(pk=e.pk).exists())

    def test_soft_delete_and_mark_paid_from_register(self):
        e = self.expense(vendor="Uline", amount=D("30.00"))
        r = self.client.post(reverse("expense_list"), {"action": "mark_paid", "id": e.pk,
                                                       "paid_date": "2025-05-05",
                                                       "paid_from": self.checking.pk}, follow=True)
        self.assertContains(r, "Marked paid")
        e.refresh_from_db()
        self.assertEqual(e.paid_date, date(2025, 5, 5))
        self.assertEqual(e.paid_from, self.checking)
        self.client.post(reverse("expense_list"), {"action": "del", "id": e.pk})
        self.assertFalse(Expense.objects.filter(pk=e.pk).exists())
        self.assertTrue(Expense.all_objects.get(pk=e.pk).is_deleted)

    def test_bulk_actions(self):
        a = self.expense(vendor="A", is_reviewed=False)
        b = self.expense(vendor="B", is_reviewed=False)
        self.client.post(reverse("expense_list"), {"action": "bulk", "bulk_action": "reviewed",
                                                   "ids": [a.pk, b.pk]})
        self.assertTrue(all(Expense.objects.get(pk=p).is_reviewed for p in (a.pk, b.pk)))
        self.client.post(reverse("expense_list"), {"action": "bulk", "bulk_action": "paid",
                                                   "ids": [a.pk], "bulk-paid_date": "2025-06-01",
                                                   "bulk-paid_from": self.card.pk})
        a.refresh_from_db()
        self.assertEqual(a.paid_date, date(2025, 6, 1))
        self.assertEqual(a.paid_from, self.card)
        self.client.post(reverse("expense_list"), {"action": "bulk", "bulk_action": "category",
                                                   "ids": [a.pk, b.pk], "category": self.meals.pk})
        self.assertEqual(Expense.objects.get(pk=b.pk).category, self.meals)

    def test_list_filters_and_csv(self):
        paid = self.expense(vendor="Paid Co", paid_date=TODAY, date=TODAY)
        unpaid = self.expense(vendor="Open Co", due_date=TODAY - timedelta(days=5), date=TODAY)
        r = self.client.get(reverse("expense_list"), {"preset": "all", "status": "overdue"})
        self.assertContains(r, "Open Co")
        self.assertNotContains(r, "Paid Co")
        r = self.client.get(reverse("expense_list"), {"preset": "all", "receipt": "no", "vendor": "paid"})
        self.assertContains(r, "Paid Co")
        self.assertNotContains(r, "Open Co")
        r = self.client.get(reverse("expense_list"), {"preset": "all", "export": "csv"})
        body = r.content.decode()
        self.assertIn("Paid Co", body)
        self.assertIn("overdue", body)
        self.assertEqual(r["Content-Type"], "text/csv")


# ---------------------------------------------------------------------------
# Unpaid bills / AP aging
# ---------------------------------------------------------------------------
class UnpaidBillsTests(ExpenseTestBase):
    def _receipt(self, bill_date, due, total, paid_date=None):
        supplier = Supplier.objects.create(name="Topps")
        product = Product.objects.create(sku=f"SKU-{Receipt.objects.count()}", name="Box")
        rcv = Receipt.objects.create(supplier=supplier, date=bill_date, due_date=due, posted=True,
                                     paid_date=paid_date)
        ReceiptLine.objects.create(receipt=rcv, product=product, qty=2, unit_cost=total / 2)
        return rcv

    def test_buckets_totals_and_report_page(self):
        as_of = date(2025, 6, 30)
        current = self.expense(vendor="Not due yet", amount=D("100.00"), date=date(2025, 6, 20),
                               due_date=date(2025, 7, 15))
        late_45 = self.expense(vendor="Forty-five", amount=D("200.00"), date=date(2025, 5, 1),
                               due_date=date(2025, 5, 16))
        late_90 = self.expense(vendor="Ninety", amount=D("50.00"), date=date(2025, 3, 1),
                               due_date=date(2025, 4, 1))
        no_due = self.expense(vendor="NoDue", amount=D("25.00"), date=date(2025, 6, 10))   # aged from bill date
        self.expense(vendor="Paid already", amount=D("999.00"), date=date(2025, 6, 1), paid_date=date(2025, 6, 2))
        self.expense(vendor="Paid later", amount=D("77.00"), date=date(2025, 6, 1),
                     paid_date=date(2025, 7, 9))   # still open on Jun 30
        self._receipt(date(2025, 6, 1), date(2025, 6, 20), D("400.00"))          # 10 days late
        self._receipt(date(2025, 5, 1), date(2025, 5, 15), D("300.00"), paid_date=date(2025, 5, 20))  # paid

        data = reports.unpaid_bills(as_of)
        by_label = {r["label"]: r for r in data["rows"]}
        self.assertEqual(by_label[f"EXP-{current.pk}"]["bucket"], "current")
        self.assertEqual(by_label[f"EXP-{late_45.pk}"]["bucket"], "31_60")
        self.assertEqual(by_label[f"EXP-{late_45.pk}"]["days"], 45)
        self.assertEqual(by_label[f"EXP-{late_90.pk}"]["bucket"], "60_plus")
        self.assertEqual(by_label[f"EXP-{no_due.pk}"]["bucket"], "1_30")
        bills = [r for r in data["rows"] if r["kind"] == "bill"]
        self.assertEqual(len(bills), 1)
        self.assertEqual(bills[0]["amount"], D("400.00"))
        self.assertEqual(bills[0]["bucket"], "1_30")
        buckets = {b["key"]: b["total"] for b in data["buckets"]}
        self.assertEqual(buckets["current"], D("100.00"))
        self.assertEqual(buckets["1_30"], D("25.00") + D("400.00") + D("77.00"))
        self.assertEqual(buckets["31_60"], D("200.00"))
        self.assertEqual(buckets["60_plus"], D("50.00"))
        self.assertEqual(data["total"], D("852.00"))
        self.assertEqual(data["expense_total"] + data["bill_total"], data["total"])

        r = self.client.get(reverse("expense_report_unpaid_bills"), {"as_of": "2025-06-30"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Forty-five")
        self.assertContains(r, "31–60 days late")
        self.assertContains(r, reverse("expense_edit", args=[late_45.pk]))
        self.assertNotContains(r, "Paid already")
        r = self.client.get(reverse("expense_report_unpaid_bills"), {"as_of": "2025-06-30", "export": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[-1][-1], "852.00")

    def test_quick_added_unpaid_bill_shows_in_report(self):
        self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "category": self.supplies.pk,
            "vendor": "Uline", "amount": "48.20"})
        data = reports.unpaid_bills(date(2025, 3, 20))
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["bucket"], "1_30")     # aged from bill date, 10 days
        self.assertEqual(data["total"], D("48.20"))


# ---------------------------------------------------------------------------
# Recurring
# ---------------------------------------------------------------------------
class RecurringTests(ExpenseTestBase):
    def test_month_end_advance_jan31_to_feb28(self):
        tpl = RecurringExpense.objects.create(name="Storage unit", category=self.supplies,
                                              amount=D("120.00"), frequency="monthly",
                                              next_date=date(2025, 1, 31))
        created = tpl.generate_due(today=date(2025, 3, 1))
        self.assertEqual([e.date for e in created], [date(2025, 1, 31), date(2025, 2, 28)])
        self.assertEqual(tpl.next_date, date(2025, 3, 31))   # anchored to month end, no drift
        self.assertEqual(tpl.last_generated, date(2025, 2, 28))
        for e in created:
            self.assertEqual(e.source, "recurring")
            self.assertFalse(e.is_reviewed)
            self.assertFalse(e.is_paid)                 # open bill (auto_mark_paid off)
            self.assertEqual(e.recurring, tpl)
        self.assertEqual(tpl.advance(date(2024, 1, 31)), date(2024, 2, 29))   # leap year
        self.assertEqual(tpl.advance(date(2025, 12, 15)), date(2026, 1, 31))   # anchored to the template's day (31)

    def test_autopay_creates_paid_rows_and_respects_end_date(self):
        tpl = RecurringExpense.objects.create(name="Adobe", category=self.software, amount=D("54.99"),
                                              frequency="monthly", next_date=date(2025, 1, 5),
                                              end_date=date(2025, 2, 28), auto_mark_paid=True,
                                              paid_from=self.card)
        created = tpl.generate_due(today=date(2025, 6, 1))
        self.assertEqual(len(created), 2)                  # Jan 5, Feb 5 — Mar 5 is past end_date
        self.assertTrue(all(e.is_paid and e.paid_from == self.card for e in created))
        self.assertEqual(created[0].payment_method, "ach")

    def test_task_summary_and_generate_button(self):
        RecurringExpense.objects.create(name="Rent", category=self.supplies, amount=D("500.00"),
                                        frequency="monthly", next_date=TODAY - timedelta(days=40))
        RecurringExpense.objects.create(name="Future", category=self.supplies, amount=D("1.00"),
                                        frequency="monthly", next_date=TODAY + timedelta(days=10))
        RecurringExpense.objects.create(name="Paused", category=self.supplies, amount=D("1.00"),
                                        frequency="weekly", next_date=TODAY - timedelta(days=60), active=False)
        summary = generate_recurring_expenses()
        self.assertEqual(Expense.objects.count(), 2)       # 40 days ago + ~10 days ago
        self.assertIn("Created 2 expenses from 1 recurring template", summary)
        self.assertEqual(generate_recurring_expenses(), "Nothing due. No recurring expenses were created.")
        RecurringExpense.objects.filter(name="Future").update(next_date=TODAY)
        r = self.client.post(reverse("expense_recurring"), {"action": "generate"}, follow=True)
        self.assertContains(r, "Created 1 expense from 1 recurring template")

    def test_create_form_validation(self):
        r = self.client.post(reverse("expense_recurring_create"), {
            "name": "Bad", "category": self.supplies.pk, "amount": "10", "frequency": "monthly",
            "next_date": "2025-05-01", "end_date": "2025-04-01", "auto_mark_paid": "on", "active": "on"})
        self.assertContains(r, "Stop date is before the next date")
        self.assertContains(r, "Pick the account auto-pay comes out of")
        self.assertEqual(RecurringExpense.objects.count(), 0)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
class BudgetTests(ExpenseTestBase):
    def test_grid_upsert_update_and_clear(self):
        url = reverse("expense_budgets")
        post = {"year": 2025, f"b_{self.supplies.pk}_1": "100", f"b_{self.supplies.pk}_2": "1,250.50",
                f"b_{self.software.pk}_1": "55"}
        r = self.client.post(url, post, follow=True)
        self.assertContains(r, "Budgets for 2025 saved")
        self.assertEqual(Budget.objects.count(), 3)
        self.assertEqual(Budget.objects.get(category=self.supplies, year=2025, month=2).amount, D("1250.50"))
        # second save: change one, clear one, bad value rejected
        post[f"b_{self.supplies.pk}_1"] = "120"
        post[f"b_{self.software.pk}_1"] = ""
        post[f"b_{self.meals.pk}_3"] = "abc"
        r = self.client.post(url, post, follow=True)
        self.assertContains(r, "is not a number")
        self.assertEqual(Budget.objects.count(), 2)
        self.assertEqual(Budget.objects.get(category=self.supplies, year=2025, month=1).amount, D("120.00"))
        self.assertFalse(Budget.objects.filter(category=self.software).exists())
        # no duplicate rows ever
        self.assertEqual(Budget.objects.filter(category=self.supplies, year=2025, month=1).count(), 1)

    def test_grid_actuals_and_variance(self):
        Budget.objects.create(category=self.supplies, year=2025, month=1, amount=D("100.00"))
        self.expense(date=date(2025, 1, 15), amount=D("130.00"))
        self.expense(date=date(2025, 1, 20), amount=D("-10.00"))      # refund
        grid = reports.budget_grid(2025)
        row = next(r for r in grid["rows"] if r["category"] == self.supplies)
        jan = row["cells"][0]
        self.assertEqual(jan["actual"], D("120.00"))
        self.assertEqual(jan["state"], "over")
        self.assertEqual(row["variance"], D("-20.00"))
        self.assertEqual(grid["actual_total"], D("120.00"))
        self.assertEqual(grid["variance_total"], D("-20.00"))
        r = self.client.get(reverse("expense_budgets"), {"year": 2025})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="b_%d_1"' % self.supplies.pk)

    def test_fill_defaults(self):
        self.software.monthly_budget = D("60.00")
        self.software.save()
        self.client.post(reverse("expense_budgets"), {"year": 2025, "action": "fill_defaults"})
        self.assertEqual(Budget.objects.filter(category=self.software, year=2025).count(), 12)
        self.assertEqual(reports.annual_budgets(2025)[self.software.pk], D("720.00"))
        self.assertEqual(reports.annual_budgets(2025)[self.meals.pk], None)


# ---------------------------------------------------------------------------
# Mileage
# ---------------------------------------------------------------------------
class MileageTests(ExpenseTestBase):
    def test_deduction_math_and_rate_snapshot(self):
        settings = CompanySettings.get()
        settings.mileage_rate = D("0.700")
        settings.save()
        r = self.client.post(reverse("expense_mileage"), {
            "action": "add", "date": "2025-04-12", "miles": "17.3", "round_trip": "on",
            "purpose": "Card show", "from_location": "Shop", "to_location": "Tampa", "vehicle": "Van"},
            follow=True)
        self.assertEqual(r.status_code, 200)
        m = MileageEntry.objects.get()
        self.assertEqual(m.rate, D("0.700"))
        self.assertEqual(m.total_miles, D("34.6"))
        self.assertEqual(m.deduction, D("24.22"))
        # rate change later must not alter the logged trip
        settings.mileage_rate = D("0.675")
        settings.save()
        MileageEntry.objects.create(date=date(2025, 5, 1), miles=D("10"), purpose="Post office",
                                    rate=CompanySettings.get().mileage_rate)
        summary = reports.mileage_summary(2025)
        self.assertEqual(summary["total_miles"], D("44.6"))
        self.assertEqual(summary["total_deduction"], D("24.22") + D("6.75"))
        r = self.client.get(reverse("expense_mileage"), {"year": 2025, "export": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[-1][0], "TOTAL")
        self.assertEqual(rows[-1][-1], "30.97")

    def test_invalid_miles_shows_error(self):
        r = self.client.post(reverse("expense_mileage"), {"action": "add", "date": "2025-04-12",
                                                          "miles": "0", "purpose": "x"})
        self.assertContains(r, "Miles must be more than zero")
        self.assertEqual(MileageEntry.objects.count(), 0)


# ---------------------------------------------------------------------------
# By-month matrix
# ---------------------------------------------------------------------------
class ByMonthTests(ExpenseTestBase):
    def setUp(self):
        super().setUp()
        self.expense(date=date(2025, 1, 10), amount=D("100.00"), paid_date=date(2025, 1, 10))
        self.expense(date=date(2025, 3, 5), amount=D("200.00"), paid_date=date(2025, 4, 2))   # Mar bill, Apr cash
        self.expense(date=date(2025, 3, 20), amount=D("50.00"))                                # unpaid
        self.expense(date=date(2024, 12, 28), amount=D("70.00"), paid_date=date(2025, 1, 3))  # prior-year bill
        self.expense(date=date(2025, 6, 1), amount=D("30.00"), category=self.meals, paid_date=date(2025, 6, 1))
        t = BankTransaction.objects.create(account=self.card, date=date(2025, 2, 14), amount=D("-40.00"),
                                           description="ULINE")
        t.categorize(self.supplies.ensure_account())

    def test_cash_vs_accrual_totals(self):
        cash = reports.month_matrix(2025, CASH)
        sup = next(r for r in cash["rows"] if r["category"] == self.supplies)
        self.assertEqual(sup["cells"][0], D("170.00"))    # Jan: 100 + 70 (prior-year bill paid in Jan)
        self.assertEqual(sup["cells"][1], D("40.00"))     # Feb: bank card charge
        self.assertEqual(sup["cells"][2], D("0.00"))      # Mar: nothing paid
        self.assertEqual(sup["cells"][3], D("200.00"))    # Apr: Mar bill paid
        self.assertEqual(sup["total"], D("410.00"))
        self.assertEqual(cash["grand_total"], D("440.00"))
        self.assertEqual(sum(cash["month_totals"], D("0")), cash["grand_total"])

        acc = reports.month_matrix(2025, ACCRUAL)
        sup = next(r for r in acc["rows"] if r["category"] == self.supplies)
        self.assertEqual(sup["cells"][0], D("100.00"))    # Dec-2024 bill not in 2025
        self.assertEqual(sup["cells"][2], D("250.00"))    # Mar: 200 + 50 unpaid
        self.assertEqual(sup["cells"][3], D("0.00"))
        self.assertEqual(acc["grand_total"], D("420.00"))
        self.assertFalse(acc["has_budgets"])

    def test_budget_columns_when_budgets_exist(self):
        Budget.objects.create(category=self.supplies, year=2025, month=None, amount=D("500.00"))
        data = reports.month_matrix(2025, CASH)
        self.assertTrue(data["has_budgets"])
        sup = next(r for r in data["rows"] if r["category"] == self.supplies)
        self.assertEqual(sup["budget"], D("500.00"))
        self.assertEqual(sup["variance"], D("90.00"))
        r = self.client.get(reverse("expense_report_by_month"), {"year": 2025, "basis": "cash"})
        self.assertContains(r, "Variance")
        self.assertContains(r, "410.00")

    def test_report_exports(self):
        r = self.client.get(reverse("expense_report_by_month"), {"year": 2025, "basis": "accrual", "export": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][:2], ["Category", "Jan"])
        self.assertEqual(rows[-1][0], "TOTAL")
        self.assertEqual(rows[-1][-1], "420.00")
        r = self.client.get(reverse("expense_report_by_month"), {"year": 2025, "export": "pdf"})
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))


# ---------------------------------------------------------------------------
# Missing receipts
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=MEDIA)
class MissingReceiptTests(ExpenseTestBase):
    def test_threshold_and_bank_receipt_counts(self):
        big = self.expense(vendor="Big", date=date(2025, 2, 1), amount=D("300.00"))
        self.expense(vendor="Small", date=date(2025, 2, 2), amount=D("4.00"))
        self.expense(vendor="HasFile", date=date(2025, 2, 3), amount=D("90.00"),
                     receipt_file=SimpleUploadedFile("a.jpg", b"img", "image/jpeg"))
        t = BankTransaction.objects.create(account=self.card, date=date(2025, 2, 4), amount=D("-60.00"),
                                           description="ULINE",
                                           receipt_file=SimpleUploadedFile("b.jpg", b"img", "image/jpeg"))
        t.categorize(self.supplies.ensure_account())
        t2 = BankTransaction.objects.create(account=self.card, date=date(2025, 2, 5), amount=D("-20.00"),
                                            description="STAPLES")
        t2.categorize(self.supplies.ensure_account())
        data = reports.missing_receipts(date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual({e.vendor for e in data["rows"]}, {"Big", "Small", "STAPLES"})
        self.assertEqual(data["rows"][0], big)                      # biggest first
        data = reports.missing_receipts(date(2025, 1, 1), date(2025, 12, 31), D("10"))
        self.assertEqual({e.vendor for e in data["rows"]}, {"Big", "STAPLES"})
        r = self.client.get(reverse("expense_report_missing_receipts"),
                            {"start": "2025-01-01", "end": "2025-12-31", "min": "10"})
        self.assertContains(r, reverse("expense_edit", args=[big.pk]))
        self.assertNotContains(r, "Small")


# ---------------------------------------------------------------------------
# 1099
# ---------------------------------------------------------------------------
class Report1099Tests(ExpenseTestBase):
    def setUp(self):
        super().setUp()
        self.photog = Vendor.objects.create(name="Photo Pro", is_1099=True, tax_id_last4="1234")
        self.helper = Vendor.objects.create(name="Show Helper", is_1099=True)
        self.expense(vendor_link=self.photog, date=date(2025, 3, 1), amount=D("400.00"), paid_date=date(2025, 3, 1))
        self.expense(vendor_link=self.photog, date=date(2025, 9, 1), amount=D("300.00"), paid_date=date(2025, 9, 3))
        self.expense(vendor_link=self.photog, date=date(2025, 12, 20), amount=D("500.00"))   # unpaid: not counted
        self.expense(vendor_link=self.helper, date=date(2025, 5, 1), amount=D("500.00"), paid_date=date(2025, 5, 1))
        self.expense(vendor_link=self.amazon, date=date(2025, 5, 1), amount=D("1000.00"), paid_date=date(2025, 5, 1))
        self.expense(vendor_link=self.photog, date=date(2024, 12, 1), amount=D("5000.00"), paid_date=date(2024, 12, 1))

    def test_threshold(self):
        data = reports.vendors_1099(2025)
        self.assertEqual([r["vendor"] for r in data["qualifying"]], [self.photog])
        self.assertEqual(data["qualifying"][0]["total"], D("700.00"))
        self.assertEqual([r["vendor"] for r in data["flagged_under"]], [self.helper])
        self.assertEqual([r["vendor"] for r in data["all"]], [self.amazon, self.photog, self.helper])
        self.assertFalse(next(r for r in data["all"] if r["vendor"] == self.amazon)["needs_1099"])
        data = reports.vendors_1099(2025, D("450"))
        self.assertEqual({r["vendor"] for r in data["qualifying"]}, {self.photog, self.helper})
        r = self.client.get(reverse("expense_report_1099"), {"year": 2025, "threshold": "450"})
        self.assertContains(r, "Show Helper")
        self.assertContains(r, "YES — file 1099-NEC", count=2)
        r = self.client.get(reverse("expense_report_1099"), {"year": 2025, "export": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[1][0], "Amazon")
        self.assertEqual(rows[2][4], "700.00")


# ---------------------------------------------------------------------------
# Year-end ZIP
# ---------------------------------------------------------------------------
class YearEndTests(ExpenseTestBase):
    def test_zip_has_every_file_and_cash_accrual_columns(self):
        self.expense(vendor="Uline", date=date(2025, 12, 30), amount=D("100.00"), paid_date=date(2026, 1, 4))
        self.expense(vendor="Chipotle", category=self.meals, date=date(2025, 6, 1), amount=D("40.00"),
                     paid_date=date(2025, 6, 1))
        MileageEntry.objects.create(date=date(2025, 4, 1), miles=D("10"), round_trip=True, purpose="Show",
                                    rate=D("0.700"))
        r = self.client.get(reverse("expense_year_end"), {"year": 2025})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn('expenses_year_end_2025.zip', r["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(sorted(zf.namelist()), sorted(reports.YEAR_END_FILES))
        by_cat = list(csv.reader(io.StringIO(zf.read("expenses_by_category.csv").decode())))
        rows = {r[0]: r for r in by_cat[1:]}
        self.assertEqual(rows["Shipping supplies"][3], "0.00")      # cash: paid in 2026
        self.assertEqual(rows["Shipping supplies"][4], "100.00")    # accrual: incurred 2025
        self.assertEqual(rows["Meals"][2], "50")
        self.assertEqual(rows["Meals"][5], "20.00")                 # 50% deductible
        self.assertEqual(rows["TOTAL"][4], "140.00")
        mileage = list(csv.reader(io.StringIO(zf.read("mileage.csv").decode())))
        self.assertEqual(mileage[-1][-1], "14.00")
        unpaid = list(csv.reader(io.StringIO(zf.read("unpaid_bills_at_year_end.csv").decode())))
        self.assertEqual(unpaid[1][2], "Uline")                      # open on Dec 31
        self.assertEqual(unpaid[-1][6], "100.00")
        readme = zf.read("README.txt").decode()
        self.assertIn("cash basis", readme.lower())
        self.assertIn("accrual", readme.lower())
        all_rows = list(csv.reader(io.StringIO(zf.read("all_expenses.csv").decode())))
        self.assertEqual(len(all_rows), 3)
        # landing page without ?year
        r = self.client.get(reverse("expense_year_end"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "package (.zip)")


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
class VendorTests(ExpenseTestBase):
    def test_alias_matching(self):
        self.assertTrue(self.amazon.matches("AMZN MKTP US*2K3 Seattle"))
        self.assertTrue(self.amazon.matches("Amazon.com services"))
        self.assertFalse(self.amazon.matches("ULINE"))
        self.assertEqual(Vendor.find_for_description("POS DEBIT AMAZON.COM"), self.amazon)
        self.assertIsNone(Vendor.find_for_description("PIRATE SHIP"))

    def test_create_vendor_links_existing_expenses_and_detail_page(self):
        self.expense(vendor="Pirate Ship", amount=D("20.00"))
        self.expense(vendor="PIRATESHIP.COM", amount=D("30.00"))
        r = self.client.post(reverse("expense_vendor_create"), {
            "name": "Pirate Ship", "aliases": "PIRATESHIP.COM", "default_category": self.supplies.pk,
            "active": "on", "tax_id_last4": "", "email": "", "phone": "", "website": "",
            "account_number": "", "notes": ""}, follow=True)
        self.assertContains(r, "2 existing expenses linked")
        v = Vendor.objects.get(name="Pirate Ship")
        self.assertEqual(Expense.objects.filter(vendor_link=v).count(), 2)
        self.assertContains(r, "Pirate Ship")
        self.assertEqual(v.total_paid(), D("50.00"))
        # duplicate name rejected
        r = self.client.post(reverse("expense_vendor_create"), {"name": "pirate ship", "active": "on"})
        self.assertContains(r, "already exists")
        # alias editing on the detail page
        self.client.post(reverse("expense_vendor_detail", args=[v.pk]), {"action": "aliases",
                                                                         "aliases": "PIRATESHIP.COM\nPIRATE SHIP LLC"})
        v.refresh_from_db()
        self.assertEqual(v.alias_list, ["PIRATESHIP.COM", "PIRATE SHIP LLC"])

    def test_vendor_list_shows_ytd_and_suggestions(self):
        self.expense(vendor_link=self.amazon, date=TODAY, amount=D("12.50"))
        self.expense(vendor="Unknown Shop", amount=D("5.00"))
        r = self.client.get(reverse("expense_vendors"))
        self.assertContains(r, "Amazon")
        self.assertContains(r, "12.50")
        self.assertContains(r, "Unknown Shop")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
class CategoryTests(ExpenseTestBase):
    def test_add_rename_syncs_account_and_delete_rules(self):
        r = self.client.post(reverse("expense_categories"), {
            "action": "add", "name": "Booth fees", "description": "", "tax_line": "27a",
            "deductible_pct": "100", "monthly_budget": ""}, follow=True)
        cat = ExpenseCategory.objects.get(name="Booth fees")
        self.assertContains(r, cat.account.code)
        self.assertEqual(cat.account.type, "expense")
        # inline rename keeps the ledger account name in sync
        self.client.post(reverse("expense_categories"), {
            "action": "update", "id": cat.pk, f"c{cat.pk}-name": "Show booth fees",
            f"c{cat.pk}-description": "Card show tables", f"c{cat.pk}-tax_line": "27a",
            f"c{cat.pk}-deductible_pct": "100", f"c{cat.pk}-monthly_budget": "75"})
        cat.refresh_from_db()
        self.assertEqual(cat.name, "Show booth fees")
        self.assertEqual(Account.objects.get(pk=cat.account_id).name, "Show booth fees")
        self.assertEqual(cat.monthly_budget, D("75.00"))
        # duplicate name on add is rejected; bad percent rejected inline
        r = self.client.post(reverse("expense_categories"), {"action": "add", "name": "software",
                                                             "deductible_pct": "100"})
        self.assertContains(r, "already exists")
        r = self.client.post(reverse("expense_categories"), {
            "action": "update", "id": cat.pk, f"c{cat.pk}-name": "Show booth fees",
            f"c{cat.pk}-deductible_pct": "150"}, follow=True)
        self.assertContains(r, "0 to 100")
        # delete blocked while in use, allowed when empty
        self.expense(category=cat)
        r = self.client.post(reverse("expense_categories"), {"action": "delete", "id": cat.pk}, follow=True)
        self.assertContains(r, "can&#x27;t be deleted")
        self.assertTrue(ExpenseCategory.objects.filter(pk=cat.pk).exists())
        Expense.objects.filter(category=cat).delete()
        self.client.post(reverse("expense_categories"), {"action": "delete", "id": cat.pk})
        self.assertFalse(ExpenseCategory.objects.filter(pk=cat.pk).exists())
        # re-adding the same name restores the soft-deleted row instead of crashing
        self.client.post(reverse("expense_categories"), {"action": "add", "name": "Show booth fees",
                                                         "deductible_pct": "100"})
        self.assertEqual(ExpenseCategory.all_objects.filter(name="Show booth fees").count(), 1)
        self.assertTrue(ExpenseCategory.objects.filter(pk=cat.pk).exists())


# ---------------------------------------------------------------------------
# Permissions + every page renders
# ---------------------------------------------------------------------------
class PageAccessTests(ExpenseTestBase):
    def setUp(self):
        super().setUp()
        self.e = self.expense(vendor="Uline", amount=D("20.00"), is_reviewed=False)
        t = BankTransaction.objects.create(account=self.card, date=TODAY, amount=D("-15.00"),
                                           description="AMZN Mktp")
        t.categorize(self.supplies.ensure_account(), vendor=self.amazon)
        self.tpl = RecurringExpense.objects.create(name="Rent", category=self.supplies, amount=D("500"),
                                                   frequency="monthly", next_date=TODAY)
        MileageEntry.objects.create(date=TODAY, miles=D("5"), purpose="PO", rate=D("0.7"))
        Budget.objects.create(category=self.supplies, year=TODAY.year, month=1, amount=D("50"))

    def urls(self):
        return [
            reverse("expense_list"), reverse("expense_list") + "?preset=all&status=unpaid&receipt=no&review=yes",
            reverse("expense_list") + "?preset=last_month&sort=-amount",
            reverse("expense_create"), reverse("expense_edit", args=[self.e.pk]), reverse("expense_quick"),
            reverse("expense_vendors"), reverse("expense_vendors") + "?q=ama&inactive=1",
            reverse("expense_vendor_create"), reverse("expense_vendor_detail", args=[self.amazon.pk]),
            reverse("expense_vendor_edit", args=[self.amazon.pk]), reverse("expense_categories"),
            reverse("expense_recurring"), reverse("expense_recurring_create"),
            reverse("expense_recurring_edit", args=[self.tpl.pk]),
            reverse("expense_budgets"), reverse("expense_budgets") + "?year=2024",
            reverse("expense_mileage"), reverse("expense_mileage") + "?export=csv",
            reverse("expense_report_by_month"), reverse("expense_report_by_month") + "?basis=accrual",
            reverse("expense_report_missing_receipts"), reverse("expense_report_missing_receipts") + "?export=csv",
            reverse("expense_report_unpaid_bills"), reverse("expense_report_unpaid_bills") + "?export=csv",
            reverse("expense_report_1099"), reverse("expense_report_1099") + "?export=csv",
            reverse("expense_year_end"), reverse("expense_year_end") + f"?year={TODAY.year}",
        ]

    def test_every_page_200_as_admin(self):
        for url in self.urls():
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, url)

    def test_bad_params_do_not_crash(self):
        for url in [reverse("expense_list") + "?preset=custom&start=garbage&end=2025-13-45&category=zzz",
                    reverse("expense_budgets") + "?year=abc",
                    reverse("expense_report_missing_receipts") + "?min=-5&start=nope",
                    reverse("expense_report_1099") + "?threshold=x&year=99999",
                    reverse("expense_report_unpaid_bills") + "?as_of=bad"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_403_without_module_and_login_redirect(self):
        # has the dashboard (so the role middleware lets the request through) but not expenses
        role = Role.objects.create(name="Sales only", modules=["dashboard", "orders"])
        user = User.objects.create_user("picker", password="x")
        UserProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        for url in self.urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(reverse("expense_list"), {"action": "del", "id": self.e.pk}).status_code, 403)
        self.client.logout()
        r = self.client.get(reverse("expense_list"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_bookkeeper_role_has_access(self):
        role = Role.objects.create(name="Books", modules=["expenses"])
        user = User.objects.create_user("books", password="x")
        UserProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("expense_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("expense_report_by_month")).status_code, 200)



class RecurringAnchorTests(TestCase):
    def test_month_end_anchor_does_not_drift(self):
        from datetime import date
        from expenses.models import ExpenseCategory, RecurringExpense
        cat = ExpenseCategory.objects.create(name="Rent & storage")
        t = RecurringExpense.objects.create(name="Storage unit", category=cat, amount=Decimal("150"),
                                            frequency="monthly", next_date=date(2026, 1, 31))
        d = t.advance(t.next_date)
        self.assertEqual(d, date(2026, 2, 28))
        d = t.advance(d)
        self.assertEqual(d, date(2026, 3, 31))         # back to the 31st, not stuck on the 28th
        d = t.advance(d)
        self.assertEqual(d, date(2026, 4, 30))

    def test_template_past_end_date_is_not_due_and_retires(self):
        from datetime import date, timedelta
        from django.utils import timezone
        from expenses.models import ExpenseCategory, RecurringExpense
        cat = ExpenseCategory.objects.create(name="Software")
        today = timezone.localdate()
        t = RecurringExpense.objects.create(name="Old tool", category=cat, amount=Decimal("9"),
                                            frequency="monthly", next_date=today - timedelta(days=3),
                                            end_date=today - timedelta(days=10))
        self.assertFalse(t.is_due)
        self.assertEqual(t.generate_due(), [])
        t.refresh_from_db()
        self.assertFalse(t.active)


# ---------------------------------------------------------------------------
# Review fixes (independent code + product review)
# ---------------------------------------------------------------------------
class FullFormPaidDefaultTests(ExpenseTestBase):
    """Item 1: a new expense is an open bill unless the owner says it's paid."""

    def test_new_form_does_not_preselect_paid_from(self):
        from expenses.forms import ExpenseForm
        self.assertIsNone(ExpenseForm().initial.get("paid_from"))
        r = self.client.get(reverse("expense_create"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, f'<option value="{self.checking.pk}" selected>')

    def test_minimal_submit_is_unpaid_bill(self):
        r = self.client.post(reverse("expense_create"), {
            "date": "2025-02-01", "amount": "54.99", "category": self.software.pk,
            "due_date": "2025-02-20", "vendor": "", "reference": "", "note": "",
            "paid_date": "", "paid_from": "", "payment_method": ""})
        self.assertEqual(r.status_code, 302)
        e = Expense.objects.get()
        self.assertFalse(e.is_paid)
        self.assertIsNone(e.paid_date)
        self.assertIsNone(e.paid_from)
        self.assertEqual(e.due_date, date(2025, 2, 20))
        self.assertEqual(len(reports.unpaid_bills(date(2025, 2, 25))["rows"]), 1)

    def test_choosing_account_or_method_defaults_paid_date(self):
        self.client.post(reverse("expense_create"), {
            "date": "2025-02-01", "amount": "10", "category": self.software.pk,
            "paid_from": self.card.pk, "payment_method": ""})
        self.assertEqual(Expense.objects.get().paid_date, date(2025, 2, 1))
        Expense.objects.all().delete()
        self.client.post(reverse("expense_create"), {
            "date": "2025-02-03", "amount": "10", "category": self.software.pk,
            "paid_from": "", "payment_method": "personal"})
        e = Expense.objects.get()
        self.assertEqual(e.paid_date, date(2025, 2, 3))
        self.assertTrue(e.paid_personally)

    def test_quick_capture_keeps_paid_default(self):
        form = QuickExpenseForm()
        self.assertTrue(form.fields["paid"].initial)
        self.assertEqual(form.initial["paid_from"], self.checking)


class ThresholdTests(ExpenseTestBase):
    """Item 2: the federal 1099 threshold changes for payments after Dec 31 2025."""

    def test_default_threshold_by_year(self):
        self.assertEqual(reports.default_threshold(2024), D("600"))
        self.assertEqual(reports.default_threshold(2025), D("600"))
        self.assertEqual(reports.default_threshold(2026), D("2000"))
        self.assertEqual(reports.default_threshold(2030), D("2000"))

    def test_report_and_package_use_year_default(self):
        helper = Vendor.objects.create(name="Show Helper", is_1099=True)
        self.expense(vendor_link=helper, date=date(2025, 5, 1), amount=D("1500.00"), paid_date=date(2025, 5, 1))
        self.expense(vendor_link=helper, date=date(2026, 5, 1), amount=D("1500.00"), paid_date=date(2026, 5, 1))
        self.assertEqual([r["vendor"] for r in reports.vendors_1099(2025)["qualifying"]], [helper])
        self.assertEqual(reports.vendors_1099(2026)["qualifying"], [])
        self.assertEqual(reports.vendors_1099(2026)["threshold"], D("2000.00"))
        r = self.client.get(reverse("expense_report_1099"), {"year": 2025})
        self.assertContains(r, "YES — file 1099-NEC")
        self.assertContains(r, "threshold for 2025 is $600")
        self.assertContains(r, "confirm with your preparer")
        r = self.client.get(reverse("expense_report_1099"), {"year": 2026})
        self.assertNotContains(r, "YES — file 1099-NEC")
        self.assertContains(r, "threshold for 2026 is $2,000")
        self.assertContains(r, 'value="2000"')
        # an explicit threshold still wins
        r = self.client.get(reverse("expense_report_1099"), {"year": 2026, "threshold": "1000"})
        self.assertContains(r, "YES — file 1099-NEC")
        for year, expected in ((2025, "600.00"), (2026, "2000.00")):
            zf = zipfile.ZipFile(io.BytesIO(reports.year_end_package(year)))
            header = list(csv.reader(io.StringIO(zf.read("vendors_1099.csv").decode())))[0]
            self.assertIn(expected, header[-1], year)
            self.assertIn("confirm with your preparer", zf.read("README.txt").decode())


class RecurringDueListTests(ExpenseTestBase):
    """Item 3: the list and the Generate button use the model's is_due."""

    def test_finished_template_not_due_and_badged(self):
        RecurringExpense.objects.create(name="Old tool", category=self.software, amount=D("9"),
                                        frequency="monthly", next_date=TODAY - timedelta(days=3),
                                        end_date=TODAY - timedelta(days=10))
        RecurringExpense.objects.create(name="Rent", category=self.supplies, amount=D("500"),
                                        frequency="monthly", next_date=TODAY - timedelta(days=1))
        RecurringExpense.objects.create(name="Paused", category=self.supplies, amount=D("5"),
                                        frequency="monthly", next_date=TODAY - timedelta(days=1), active=False)
        r = self.client.get(reverse("expense_recurring"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["due_now"], 1)
        self.assertEqual(r.context["active_count"], 1)
        self.assertContains(r, "Generate due now (1)")
        self.assertContains(r, ">Finished</span>", count=1)
        self.assertContains(r, ">Paused</span>", count=1)
        self.assertContains(r, ">due</span>", count=1)
        self.assertEqual(r.context["monthly_estimate"], D("500.00"))


class BudgetYearClampTests(ExpenseTestBase):
    """Item 4: a silly year in the POST body can't reach the database."""

    def test_post_year_is_clamped(self):
        r = self.client.post(reverse("expense_budgets"), {"year": "99999", f"b_{self.supplies.pk}_1": "10"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("year=2100", r["Location"])
        self.assertEqual(Budget.objects.get().year, 2100)
        r = self.client.post(reverse("expense_budgets"), {"year": "abc", "action": "fill_defaults"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"year={TODAY.year}", r["Location"])
        r = self.client.post(reverse("expense_budgets"), {"year": "-5", f"b_{self.meals.pk}_2": "1"})
        self.assertIn("year=2000", r["Location"])


class QuickAddVendorNameTests(ExpenseTestBase):
    """Item 5: a typed alias is stored as the vendor's real name."""

    def test_alias_normalised_to_vendor_name(self):
        self.client.post(reverse("expense_list"), {
            "action": "add", "date": "2025-03-10", "category": self.supplies.pk,
            "vendor": "AMZN Mktp US*2K3", "amount": "12.00", "paid": "on"})
        e = Expense.objects.get()
        self.assertEqual(e.vendor_link, self.amazon)
        self.assertEqual(e.vendor, "Amazon")
        # unknown vendor text is kept verbatim, unlinked
        self.client.post(reverse("expense_quick"), {
            "date": "2025-03-11", "category": self.supplies.pk, "vendor": "Corner Store", "amount": "3.00"})
        e2 = Expense.objects.get(vendor="Corner Store")
        self.assertIsNone(e2.vendor_link)


class ReviewFilterTests(ExpenseTestBase):
    """Item 6: ?review=1 (dashboard) and ?review=yes (register) both work."""

    def test_review_param_variants(self):
        self.expense(vendor="Auto Row", is_reviewed=False)
        self.expense(vendor="Checked Row", is_reviewed=True)
        for value in ("1", "yes"):
            r = self.client.get(reverse("expense_list"), {"preset": "all", "review": value})
            self.assertContains(r, "Auto Row")
            self.assertNotContains(r, "Checked Row")
            self.assertEqual(r.context["filters"]["review"], "yes")
            self.assertContains(r, 'name="review" value="yes" checked')
        r = self.client.get(reverse("expense_list"), {"preset": "all"})
        self.assertContains(r, "Checked Row")


@override_settings(MEDIA_ROOT=MEDIA)
class YearEndReceiptsTests(ExpenseTestBase):
    """Item 7: the package carries a receipts/ folder."""

    def test_receipts_folder_naming_dedupe_missing_and_cap(self):
        self.expense(vendor="Uline / Boxes & Tape", date=date(2025, 2, 3), amount=D("90.00"),
                     paid_date=date(2025, 2, 3),
                     receipt_file=SimpleUploadedFile("a.jpg", b"img-a-bytes", "image/jpeg"))
        # same date / vendor / amount / type twice -> _2 suffix
        self.expense(vendor="Uline / Boxes & Tape", date=date(2025, 2, 3), amount=D("90.00"),
                     paid_date=date(2025, 2, 3),
                     receipt_file=SimpleUploadedFile("b.JPG", b"img-b-bytes-14", "image/jpeg"))
        # receipt lives on the bank transaction, not the expense (a PDF this time)
        t = BankTransaction.objects.create(account=self.card, date=date(2025, 2, 4), amount=D("-60.00"),
                                           description="STAPLES",
                                           receipt_file=SimpleUploadedFile("c.pdf", b"%PDF-c", "application/pdf"))
        t.categorize(self.supplies.ensure_account())
        # file name recorded but gone from disk
        gone = self.expense(vendor="Gone", date=date(2025, 3, 1), amount=D("5.00"), paid_date=date(2025, 3, 1),
                            receipt_file="expense_receipts/2025/03/vanished.jpg")
        # no receipt at all
        self.expense(vendor="Bare", date=date(2025, 3, 2), amount=D("1.00"), paid_date=date(2025, 3, 2))
        # other year: not included
        self.expense(vendor="Other year", date=date(2024, 3, 2), amount=D("1.00"), paid_date=date(2024, 3, 2),
                     receipt_file=SimpleUploadedFile("d.jpg", b"img-d", "image/jpeg"))

        r = self.client.get(reverse("expense_year_end"), {"year": 2025})
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        receipts = sorted(n for n in names if n.startswith("receipts/"))
        self.assertEqual(receipts, ["receipts/2025-02-03_Uline_Boxes_Tape_90.00.jpg",
                                    "receipts/2025-02-03_Uline_Boxes_Tape_90.00_2.jpg",
                                    "receipts/2025-02-04_STAPLES_60.00.pdf"])
        self.assertEqual(zf.read("receipts/2025-02-03_Uline_Boxes_Tape_90.00.jpg"), b"img-a-bytes")
        self.assertEqual(zf.read("receipts/2025-02-03_Uline_Boxes_Tape_90.00_2.jpg"), b"img-b-bytes-14")
        self.assertEqual(zf.read("receipts/2025-02-04_STAPLES_60.00.pdf"), b"%PDF-c")
        self.assertEqual(sorted(n for n in names if not n.startswith("receipts/")),
                         sorted(reports.YEAR_END_FILES))
        readme = zf.read("README.txt").decode()
        self.assertIn("receipts/", readme)
        self.assertIn("3 receipt photos/PDFs", readme)
        self.assertIn("RECEIPTS NOT INCLUDED", readme)
        self.assertIn(f"2025-03-01_Gone_5.00.jpg  (expense #{gone.pk}): the file is missing", readme)
        # all_expenses.csv points at the archive name
        all_rows = list(csv.reader(io.StringIO(zf.read("all_expenses.csv").decode())))
        self.assertEqual(all_rows[0][-1], "Receipt file in this package")
        by_vendor = {row[6]: row[-1] for row in all_rows[1:]}
        self.assertEqual(by_vendor["STAPLES"], "receipts/2025-02-04_STAPLES_60.00.pdf")
        self.assertEqual(by_vendor["Gone"], "")
        self.assertEqual(by_vendor["Bare"], "")

        # cap: only what fits goes in, the rest is listed
        zf = zipfile.ZipFile(io.BytesIO(reports.year_end_package(2025, receipt_cap=16)))
        receipts = [n for n in zf.namelist() if n.startswith("receipts/")]
        self.assertEqual(receipts, ["receipts/2025-02-03_Uline_Boxes_Tape_90.00.jpg"])   # 11 bytes fit, next 14 don't
        readme = zf.read("README.txt").decode()
        self.assertIn("2025-02-03_Uline_Boxes_Tape_90.00_2.jpg", readme)
        self.assertIn("2025-02-04_STAPLES_60.00.pdf", readme)
        self.assertIn("left out to keep the folder under 0 MB", readme)
        # landing page mentions the folder
        r = self.client.get(reverse("expense_year_end"))
        self.assertContains(r, "receipts/")
        self.assertContains(r, "200 MB")

    def test_safe_filename(self):
        self.assertEqual(reports.safe_filename("Uline / Boxes & Tape"), "Uline_Boxes_Tape")
        self.assertEqual(reports.safe_filename("  ../../etc/passwd "), "etcpasswd")   # no leading dots or slashes
        self.assertEqual(reports.safe_filename("AMZN Mktp US*2K3"), "AMZN_Mktp_US2K3")
        self.assertEqual(reports.safe_filename(""), "receipt")
        self.assertEqual(len(reports.safe_filename("x" * 200)), 60)


class CopyTests(ExpenseTestBase):
    """Item 8: plain-English copy."""

    def test_unpaid_bills_copy(self):
        self.expense(vendor="Late Co", date=date(2025, 1, 1), due_date=date(2025, 1, 10))
        r = self.client.get(reverse("expense_report_unpaid_bills"), {"as_of": "2025-02-01"})
        self.assertContains(r, "Unpaid bills</a>")                 # subnav
        self.assertContains(r, '<h1 class="page-title">Unpaid bills</h1>')
        self.assertNotContains(r, "AP aging")
        self.assertNotContains(r, "accounts payable")
        self.assertContains(r, "Bills you still owe")
        self.assertContains(r, "<th class=\"num \">How late</th>")
        self.assertNotContains(r, ">Bucket<")
        r = self.client.get(reverse("expense_report_unpaid_bills"), {"as_of": "2025-02-01", "export": "csv"})
        self.assertIn("How late", r.content.decode().splitlines()[0])

    def test_quick_capture_paid_personally_copy(self):
        r = self.client.get(reverse("expense_quick"))
        self.assertContains(r, "Paid personally (my own card or cash)")
        self.assertContains(r, "counts as money you put into the business")
        r = self.client.get(reverse("expense_list"))
        self.assertContains(r, "Paid personally (my own card or cash)")


class UnpaidBillsMarkPaidTests(UnpaidBillsTests):
    """Item 9: inline Mark paid on the report."""

    def test_inline_mark_paid_and_receipt_link(self):
        e = self.expense(vendor="Late Co", amount=D("80.00"), date=date(2025, 5, 1), due_date=date(2025, 5, 16))
        rcv = self._receipt(date(2025, 6, 1), date(2025, 6, 20), D("400.00"))
        url = reverse("expense_report_unpaid_bills") + "?as_of=2025-06-30"
        r = self.client.get(url)
        self.assertContains(r, 'name="action" value="mark_paid"')
        self.assertContains(r, f'name="id" value="{e.pk}"')
        self.assertContains(r, 'name="paid_date"')
        self.assertContains(r, f'<option value="{self.checking.pk}" selected>Checking</option>')
        self.assertContains(r, reverse("receipt_detail", args=[rcv.pk]))
        self.assertContains(r, "Record payment there")
        self.assertContains(r, "<th class=\"text-right\">Action</th>")
        r = self.client.post(url, {"action": "mark_paid", "id": e.pk, "paid_date": "2025-06-25",
                                   "paid_from": self.card.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], url)
        e.refresh_from_db()
        self.assertEqual(e.paid_date, date(2025, 6, 25))
        self.assertEqual(e.paid_from, self.card)
        r = self.client.get(url)
        self.assertContains(r, "Marked paid: Late Co $80.00")
        self.assertNotContains(r, "Late Co</a>")                      # gone from the open list
        self.assertEqual(len(reports.unpaid_bills(date(2025, 6, 30))["rows"]), 1)   # the receipt remains
        # bad date -> error, nothing changes
        f = self.expense(vendor="Other", amount=D("1.00"), date=date(2025, 5, 1))
        r = self.client.post(url, {"action": "mark_paid", "id": f.pk, "paid_date": "nope"}, follow=True)
        self.assertContains(r, "Enter a valid paid date")
        f.refresh_from_db()
        self.assertFalse(f.is_paid)
        # CSV/PDF still work with the action column present on the HTML only
        r = self.client.get(url + "&export=csv")
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(len(rows[0]), 9)


class BudgetCsvTests(ExpenseTestBase):
    """Item 10: budgets grid export."""

    def test_budget_csv(self):
        Budget.objects.create(category=self.supplies, year=2025, month=1, amount=D("100.00"))
        Budget.objects.create(category=self.supplies, year=2025, month=3, amount=D("50.00"))
        self.expense(date=date(2025, 1, 15), amount=D("130.00"))
        r = self.client.get(reverse("expense_budgets"), {"year": 2025, "export": "csv"})
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn("budgets_2025.csv", r["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][0], "Category")
        self.assertEqual(rows[0][1], "Jan budget $")
        self.assertEqual(rows[0][12], "Dec budget $")
        self.assertEqual(rows[0][13:], ["Annual budget $", "Annual actual $", "Variance $"])
        sup = next(row for row in rows if row[0] == "Shipping supplies")
        self.assertEqual(sup[1], "100.00")
        self.assertEqual(sup[2], "")
        self.assertEqual(sup[3], "50.00")
        self.assertEqual(sup[13:], ["150.00", "130.00", "20.00"])
        self.assertEqual(rows[-1][0], "TOTAL")
        self.assertEqual(rows[-1][13], "150.00")
        r = self.client.get(reverse("expense_budgets"), {"year": 2025})
        self.assertContains(r, "export=csv")


class YearEndPageYearsTests(ExpenseTestBase):
    """Item 11: no next-year package; in-progress wording."""

    def test_years_stop_at_this_year(self):
        from expenses import views
        self.assertEqual(max(views._years()), TODAY.year)
        self.assertEqual(max(views._years(include_next=True)), TODAY.year + 1)   # budgets only
        r = self.client.get(reverse("expense_year_end"))
        self.assertNotContains(r, f"?year={TODAY.year + 1}")
        self.assertContains(r, f"?year={TODAY.year}")
        r = self.client.get(reverse("expense_year_end"), {"year": TODAY.year + 1})
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn(f"expenses_year_end_{TODAY.year}.zip", r["Content-Disposition"])
        r = self.client.get(reverse("expense_budgets"))
        self.assertContains(r, f"?year={TODAY.year + 1}")

    def test_in_progress_year_says_open_today(self):
        self.expense(vendor="Still open", date=TODAY - timedelta(days=2))
        r = self.client.get(reverse("expense_year_end"), {"for": TODAY.year})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["check_year"], TODAY.year)
        if date(TODAY.year, 12, 31) > TODAY:
            self.assertTrue(r.context["in_progress"])
            self.assertContains(r, "open today")
            self.assertNotContains(r, f"open on Dec 31, {TODAY.year}")
            self.assertContains(r, f"as_of={TODAY.isoformat()}")
            self.assertContains(r, "is still in progress")
        self.assertContains(r, "1 bill ")
        r = self.client.get(reverse("expense_year_end"), {"for": TODAY.year - 1})
        self.assertFalse(r.context["in_progress"])
        self.assertContains(r, f"still open on Dec 31, {TODAY.year - 1}")
        self.assertContains(r, f"as_of={TODAY.year - 1}-12-31")
        # bad / future "for" falls back to the default year
        r = self.client.get(reverse("expense_year_end"), {"for": TODAY.year + 5})
        self.assertEqual(r.context["check_year"], r.context["default_year"])
        # the ZIP for an in-progress year ages bills as of today
        if date(TODAY.year, 12, 31) > TODAY:
            zf = zipfile.ZipFile(io.BytesIO(reports.year_end_package(TODAY.year)))
            readme = zf.read("README.txt").decode()
            self.assertIn("still in progress", readme)
            unpaid = list(csv.reader(io.StringIO(zf.read("unpaid_bills_at_year_end.csv").decode())))
            self.assertEqual(unpaid[1][2], "Still open")


class CategoryMergeTests(ExpenseTestBase):
    """Item 12: merge one category into another, keeping every total."""

    def test_merge_moves_everything_and_retires_source(self):
        from banking.models import BankTransactionSplit, CategoryRule
        src, tgt = self.software, self.supplies
        src_acct, tgt_acct = src.ensure_account(), tgt.ensure_account()
        self.expense(category=src, date=date(2025, 1, 5), amount=D("54.99"), paid_date=date(2025, 1, 5))
        self.expense(category=src, date=date(2025, 2, 5), amount=D("20.00"))
        self.expense(category=tgt, date=date(2025, 1, 9), amount=D("100.00"), paid_date=date(2025, 1, 9))
        self.expense(category=self.meals, date=date(2025, 1, 9), amount=D("7.00"), paid_date=date(2025, 1, 9))
        deleted = self.expense(category=src, amount=D("3.00"))
        deleted.delete()
        Budget.objects.create(category=src, year=2025, month=1, amount=D("60.00"))     # target has Jan too
        Budget.objects.create(category=src, year=2025, month=2, amount=D("60.00"))     # target has no Feb
        Budget.objects.create(category=tgt, year=2025, month=1, amount=D("100.00"))
        tpl = RecurringExpense.objects.create(name="Adobe", category=src, amount=D("54.99"),
                                              frequency="monthly", next_date=TODAY + timedelta(days=5))
        vend = Vendor.objects.create(name="Adobe", default_category=src)
        rule = CategoryRule.objects.create(name="Adobe rule", pattern="ADOBE", category_account=src_acct)
        t = BankTransaction.objects.create(account=self.card, date=date(2025, 3, 1), amount=D("-54.99"),
                                           description="ADOBE")
        t.categorize(src_acct)
        t_sugg = BankTransaction.objects.create(account=self.card, date=date(2025, 3, 2), amount=D("-9.00"),
                                                description="ADOBE?", suggested_account=src_acct)
        t_split = BankTransaction.objects.create(account=self.card, date=date(2025, 3, 3), amount=D("-30.00"),
                                                 description="MIXED")
        split = BankTransactionSplit.objects.create(transaction=t_split, account=src_acct, amount=D("12.00"))
        BankTransactionSplit.objects.create(transaction=t_split, account=self.meals.ensure_account(), amount=D("18.00"))

        before_total = sum((e.amount for e in Expense.objects.all()), D("0"))
        before_src_tgt = sum((e.amount for e in Expense.objects.filter(category__in=[src, tgt])), D("0"))
        before_cash = reports.month_matrix(2025, CASH)["grand_total"]
        before_budget = sum((b.amount for b in Budget.objects.all()), D("0"))

        r = self.client.post(reverse("expense_categories"), {"action": "merge", "id": src.pk, "target": tgt.pk},
                             follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Software merged into Shipping supplies")
        self.assertContains(r, "4 expenses")
        self.assertContains(r, "2 budget rows")
        self.assertContains(r, "1 recurring template")
        self.assertContains(r, "1 vendor default")
        self.assertContains(r, "1 bank rule")
        self.assertContains(r, "1 bank transaction,")        # the categorized one (suggestions move too, uncounted)
        self.assertContains(r, "1 split line")

        # source gone (soft), its account inactive, target untouched
        self.assertFalse(ExpenseCategory.objects.filter(pk=src.pk).exists())
        self.assertTrue(ExpenseCategory.all_objects.get(pk=src.pk).is_deleted)
        self.assertFalse(Account.objects.get(pk=src_acct.pk).active)
        self.assertTrue(Account.objects.get(pk=tgt_acct.pk).active)
        self.assertNotContains(r, f'name="id" value="{src.pk}"')

        # totals preserved
        self.assertEqual(sum((e.amount for e in Expense.objects.all()), D("0")), before_total)
        self.assertEqual(sum((e.amount for e in Expense.objects.filter(category=tgt)), D("0")), before_src_tgt)
        self.assertFalse(Expense.all_objects.filter(category=src).exists())
        self.assertEqual(Expense.all_objects.get(pk=deleted.pk).category, tgt)
        self.assertEqual(reports.month_matrix(2025, CASH)["grand_total"], before_cash)
        self.assertEqual(Expense.objects.filter(category=self.meals).count(), 1)
        # budgets: Jan summed, Feb moved, nothing left on the source
        self.assertEqual(sum((b.amount for b in Budget.objects.all()), D("0")), before_budget)
        self.assertEqual(Budget.objects.get(category=tgt, year=2025, month=1).amount, D("160.00"))
        self.assertEqual(Budget.objects.get(category=tgt, year=2025, month=2).amount, D("60.00"))
        self.assertFalse(Budget.objects.filter(category=src).exists())
        self.assertEqual(reports.annual_budgets(2025)[tgt.pk], D("220.00"))
        # everything else re-pointed
        tpl.refresh_from_db(); vend.refresh_from_db(); rule.refresh_from_db()
        t.refresh_from_db(); t_sugg.refresh_from_db(); split.refresh_from_db()
        self.assertEqual(tpl.category, tgt)
        self.assertEqual(vend.default_category, tgt)
        self.assertEqual(rule.category_account, tgt_acct)
        self.assertEqual(t.category_account, tgt_acct)
        self.assertEqual(t.expense.category, tgt)
        self.assertEqual(t_sugg.suggested_account, tgt_acct)
        self.assertEqual(split.account, tgt_acct)
        self.assertEqual(ExpenseCategory.for_account(tgt_acct), tgt)

    def test_merge_validation_and_page_control(self):
        r = self.client.get(reverse("expense_categories"))
        self.assertContains(r, "Merge into…")
        self.assertContains(r, f'id="merge-{self.software.pk}"')
        self.assertContains(r, "confirmMerge(")
        r = self.client.post(reverse("expense_categories"), {"action": "merge", "id": self.software.pk,
                                                             "target": self.software.pk}, follow=True)
        self.assertContains(r, "Pick a different category")
        r = self.client.post(reverse("expense_categories"), {"action": "merge", "id": self.software.pk,
                                                             "target": ""}, follow=True)
        self.assertContains(r, "Pick the category to merge into")
        self.assertTrue(ExpenseCategory.objects.filter(pk=self.software.pk).exists())
        self.assertEqual(self.client.post(reverse("expense_categories"), {"action": "merge", "id": 999999,
                                                                          "target": self.supplies.pk}).status_code, 404)


class QuickCaptureMobileTests(ExpenseTestBase):
    """Item 13: thumb-sized chips, PDF-friendly receipt input, big Save."""

    def test_markup(self):
        for i in range(3):
            self.expense(category=self.meals, date=TODAY)
        r = self.client.get(reverse("expense_quick"))
        self.assertContains(r, 'class="chip px-4 py-3 min-h-[44px]')
        self.assertContains(r, 'accept="image/*,application/pdf"')
        self.assertNotContains(r, "capture=")
        self.assertContains(r, "Take a photo or attach a PDF")
        self.assertContains(r, 'class="btn-primary w-full py-4 text-lg">Save expense</button>')
        self.assertEqual(r.context["chips"][0], self.meals)        # most-used first
        # the form widget agrees with the template (register quick-add uses the widget)
        attrs = QuickExpenseForm().fields["receipt_file"].widget.attrs
        self.assertEqual(attrs.get("accept"), "image/*,application/pdf")
        self.assertNotIn("capture", attrs)
        # a PDF forwarded from email can be attached from the phone page
        r = self.client.post(reverse("expense_quick"), {
            "date": TODAY.isoformat(), "category": self.software.pk, "amount": "9.99", "paid": "on",
            "receipt_file": SimpleUploadedFile("inv.pdf", b"%PDF-1.4 x", "application/pdf")}, follow=True)
        self.assertContains(r, "Saved $9.99")
        self.assertTrue(Expense.objects.get(amount=D("9.99")).has_receipt)
