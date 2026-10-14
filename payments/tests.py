"""Payment application: partial, multi-invoice, over-apply guards, status flow."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from customers.models import Customer, ResaleCertificate
from inventory.models import Product
from invoicing.models import Invoice
from orders.models import POLine, PurchaseOrder
from payments.models import Payment


def build_invoice(customer, amount_units=10):
    p = Product.objects.create(sku=f"PAY-{Invoice.objects.count()}-{amount_units}",
                               name="X", default_price=Decimal("10.00"))
    p.apply_movement(movement_type="receipt", qty_delta=amount_units + 5,
                     unit_cost=Decimal("4.00"), new_avg_cost=Decimal("4.00"))
    po = PurchaseOrder.objects.create(customer=customer)
    POLine.objects.create(po=po, product=p, qty_ordered=amount_units,
                          unit_price=Decimal("10.00"))
    po.confirm(); po.finalize()
    return Invoice.build_from_pos([po])


class PaymentApplicationTests(TestCase):
    def setUp(self):
        self.c = Customer.objects.create(business_name="Payer")
        ResaleCertificate.objects.create(
            customer=self.c, certificate_number="C", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=90))
        self.inv1 = build_invoice(self.c, 10)   # $100
        self.inv2 = build_invoice(self.c, 5)    # $50

    def test_partial_payment_sets_partial_status(self):
        pay = Payment.objects.create(customer=self.c, amount=Decimal("40.00"),
                                     method="check")
        pay.apply_to_invoice(self.inv1, Decimal("40.00"))
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.status, Invoice.PARTIAL)
        self.assertEqual(self.inv1.balance_due, Decimal("60.00"))

    def test_one_payment_across_multiple_invoices(self):
        pay = Payment.objects.create(customer=self.c, amount=Decimal("150.00"),
                                     method="wire")
        pay.apply_to_invoice(self.inv1, Decimal("100.00"))
        pay.apply_to_invoice(self.inv2, Decimal("50.00"))
        self.inv1.refresh_from_db(); self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.status, Invoice.PAID)
        self.assertEqual(self.inv2.status, Invoice.PAID)
        self.assertEqual(pay.amount_unapplied, Decimal("0.00"))
        self.assertEqual(self.c.balance, Decimal("0.00"))

    def test_cannot_apply_more_than_payment(self):
        pay = Payment.objects.create(customer=self.c, amount=Decimal("30.00"),
                                     method="cash")
        with self.assertRaises(ValidationError):
            pay.apply_to_invoice(self.inv1, Decimal("50.00"))

    def test_cannot_overpay_invoice(self):
        pay = Payment.objects.create(customer=self.c, amount=Decimal("500.00"),
                                     method="ach")
        with self.assertRaises(ValidationError):
            pay.apply_to_invoice(self.inv2, Decimal("60.00"))  # balance is 50

    def test_cannot_apply_to_other_customers_invoice(self):
        stranger = Customer.objects.create(business_name="Stranger")
        pay = Payment.objects.create(customer=stranger, amount=Decimal("10.00"),
                                     method="cash")
        with self.assertRaises(ValidationError):
            pay.apply_to_invoice(self.inv1, Decimal("10.00"))

    def test_unapply_restores_balance_and_status(self):
        pay = Payment.objects.create(customer=self.c, amount=Decimal("100.00"),
                                     method="check")
        app = pay.apply_to_invoice(self.inv1, Decimal("100.00"))
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.status, Invoice.PAID)
        pay.unapply(app)
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.status, Invoice.OPEN)
        self.assertEqual(self.inv1.balance_due, Decimal("100.00"))

    def test_customer_balance_math(self):
        self.assertEqual(self.c.balance, Decimal("150.00"))
        pay = Payment.objects.create(customer=self.c, amount=Decimal("70.00"),
                                     method="zelle")
        pay.apply_to_invoice(self.inv1, Decimal("70.00"))
        self.assertEqual(self.c.balance, Decimal("80.00"))


class ArAdjustmentTests(TestCase):
    """Manual AR button: transfer opening balances from the old system."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("arb2", password="x")
        self.client.force_login(self.admin)
        from customers.models import Customer
        self.c = Customer.objects.create(business_name="Legacy Wholesale",
                                         payment_terms="net30")

    def test_adjustment_creates_backdated_tax_free_invoice(self):
        from invoicing.models import Invoice
        r = self.client.post("/payments/ar-adjustment/", {
            "customer_id": str(self.c.pk), "amount": "1250.00",
            "date": "2026-06-15", "due_date": "2026-07-15",
            "memo": "Balance from old system",
        })
        self.assertEqual(r.status_code, 302)
        inv = Invoice.objects.latest("id")
        self.assertEqual(inv.total, Decimal("1250.00"))
        self.assertEqual(inv.tax_amount, Decimal("0.00"))
        self.assertEqual(str(inv.date), "2026-06-15")
        self.assertEqual(str(inv.due_date), "2026-07-15")
        self.assertEqual(inv.balance_due, Decimal("1250.00"))
        # shows in AR aging in an aged bucket (backdated past 30 days)
        r = self.client.get("/reports/ar-aging/")
        self.assertContains(r, "Legacy Wholesale")

    def test_zero_or_negative_rejected(self):
        from invoicing.models import Invoice
        r = self.client.post("/payments/ar-adjustment/", {
            "customer_id": str(self.c.pk), "amount": "-50",
        }, follow=True)
        self.assertContains(r, "greater than zero")
        self.assertEqual(Invoice.objects.count(), 0)

    def test_defaults_to_today_and_terms(self):
        from django.utils import timezone
        from datetime import timedelta
        from invoicing.models import Invoice
        self.client.post("/payments/ar-adjustment/", {
            "customer_id": str(self.c.pk), "amount": "100",
        })
        inv = Invoice.objects.latest("id")
        self.assertEqual(inv.date, timezone.localdate())
        self.assertEqual(inv.due_date, timezone.localdate() + timedelta(days=30))


class ArAdjustmentNotRevenueTests(TestCase):
    """Balance transfers hit AR but must NOT count as revenue/profit/tax."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("arb3", password="x")
        self.client.force_login(self.admin)
        from customers.models import Customer
        self.c = Customer.objects.create(business_name="Old System Co")
        self.client.post("/payments/ar-adjustment/", {
            "customer_id": str(self.c.pk), "amount": "5000.00",
            "memo": "Opening balance transfer",
        })
        self.client.get("/")   # consume the success flash message

    def test_flagged_and_in_ar(self):
        from invoicing.models import Invoice
        inv = Invoice.objects.latest("id")
        self.assertTrue(inv.is_ar_adjustment)
        r = self.client.get("/reports/ar-aging/")
        self.assertContains(r, "Old System Co")      # still owed

    def test_excluded_from_pnl_and_customer_and_tax_reports(self):
        r = self.client.get("/reports/pnl/")
        self.assertNotContains(r, "5,000")
        self.assertNotContains(r, "5000.00")
        r = self.client.get("/reports/revenue-by-customer/")
        self.assertNotContains(r, "Old System Co")
        r = self.client.get("/reports/sales-tax/")
        self.assertNotContains(r, "5000.00")
        r = self.client.get("/reports/sales-by-product/?group=category")
        self.assertNotContains(r, "5000.00")


class PaymentFromBankDepositTests(TestCase):
    """Record a payment straight from a bank deposit: the form is pre-filled
    from the query string and the deposit is matched once the payment saves."""

    def setUp(self):
        from django.contrib.auth.models import User
        from django.urls import reverse
        from accounting.models import Account
        from banking.models import BankAccount, BankTransaction
        Account.ensure_system_accounts()
        self.admin = User.objects.create_superuser("bankpay", password="x")
        self.client.force_login(self.admin)
        self.c = Customer.objects.create(business_name="Card Shop LLC")
        self.acct = BankAccount.objects.create(name="Checking", account_type="checking",
                                               opening_date=timezone.localdate() - timedelta(days=400))
        self.dep = BankTransaction.objects.create(account=self.acct, date=timezone.localdate() - timedelta(days=2),
                                                  amount=Decimal("1250.00"), description="ZELLE FROM CARD SHOP")
        self.url = reverse("payment_create")

    def test_get_prefills_from_query_string(self):
        r = self.client.get(self.url + f"?customer={self.c.pk}&amount=1250&date={self.dep.date.isoformat()}"
                                       f"&reference=ZELLE+FROM+CARD+SHOP&bank_txn={self.dep.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'value="1250.00"')
        self.assertContains(r, f'value="{self.dep.date.isoformat()}"')
        self.assertContains(r, 'value="ZELLE FROM CARD SHOP"')
        self.assertContains(r, f'<input type="hidden" name="bank_txn" value="{self.dep.pk}">')
        self.assertContains(r, "Back to the bank transaction")
        self.assertContains(r, "Card Shop LLC")
        # switching customer keeps the prefill (hidden inputs in the GET form)
        self.assertContains(r, '<input type="hidden" name="amount" value="1250.00">')
        # junk prefill never 500s and is simply dropped
        r = self.client.get(self.url + f"?customer={self.c.pk}&amount=abc&date=2025-99-99&bank_txn=xyz")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Back to the bank transaction")
        # plain form unchanged: today's date, empty amount
        r = self.client.get(self.url + f"?customer={self.c.pk}")
        self.assertContains(r, f'value="{timezone.localdate():%Y-%m-%d}"')
        self.assertContains(r, 'id="payAmount" required value=""')

    def test_post_with_bank_txn_matches_the_deposit(self):
        from banking.models import BankTransaction
        from accounting.models import Account
        r = self.client.post(self.url, {"customer": self.c.pk, "amount": "1250.00", "date": self.dep.date.isoformat(),
                                        "method": "zelle", "reference": "ZELLE FROM CARD SHOP",
                                        "bank_txn": self.dep.pk}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Deposit matched")
        pay = Payment.objects.get()
        self.assertEqual((pay.customer, pay.amount, pay.method), (self.c, Decimal("1250.00"), "zelle"))
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.status, BankTransaction.MATCHED)
        self.assertEqual(list(self.dep.payments.all()), [pay])
        self.assertEqual(self.dep.category_account, Account.system("undeposited"))
        self.assertEqual(self.dep.reviewed_by, self.admin)
        # unapplied credit warning from the existing flow is still there
        self.assertContains(r, "unapplied")

    def test_amount_mismatch_saves_payment_but_leaves_deposit_open(self):
        from banking.models import BankTransaction
        r = self.client.post(self.url, {"customer": self.c.pk, "amount": "300.00", "method": "check",
                                        "bank_txn": self.dep.pk}, follow=True)
        self.assertContains(r, "not matched to the bank deposit")
        self.assertEqual(Payment.objects.count(), 1)
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.status, BankTransaction.UNREVIEWED)
        self.assertEqual(self.dep.payments.count(), 0)

    def test_missing_or_bogus_bank_txn_is_ignored(self):
        from banking.models import BankTransaction
        for bogus in ("abc", "999999", ""):
            Payment.all_objects.all().delete()
            r = self.client.post(self.url, {"customer": self.c.pk, "amount": "10.00", "method": "cash",
                                            "bank_txn": bogus}, follow=True)
            self.assertEqual(r.status_code, 200, bogus)
            self.assertNotContains(r, "Deposit matched")
            self.assertEqual(Payment.objects.count(), 1, bogus)
        # a soft-deleted deposit is not live
        self.dep.delete(user=self.admin)
        r = self.client.post(self.url, {"customer": self.c.pk, "amount": "1250.00", "method": "cash",
                                        "bank_txn": self.dep.pk}, follow=True)
        self.assertNotContains(r, "Deposit matched")
        self.assertEqual(BankTransaction.all_objects.get(pk=self.dep.pk).payments.count(), 0)

    def test_bad_amount_redirect_keeps_bank_txn(self):
        r = self.client.post(self.url, {"customer": self.c.pk, "amount": "abc", "bank_txn": self.dep.pk})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"customer={self.c.pk}", r["Location"])
        self.assertIn(f"bank_txn={self.dep.pk}", r["Location"])
        self.assertEqual(Payment.objects.count(), 0)
