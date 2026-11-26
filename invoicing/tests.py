"""Invoice totals, consolidation, tax rules + override, credit memos."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from customers.models import Customer, ResaleCertificate, ShippingAddress
from inventory.models import Product
from invoicing.models import CreditMemoLine, CreditMemo, Invoice
from orders.models import POLine, PurchaseOrder


def stocked_product(sku, qty=100, cost="4.00"):
    p = Product.objects.create(sku=sku, name=f"P {sku}",
                               default_price=Decimal("10.00"))
    p.apply_movement(movement_type="receipt", qty_delta=qty,
                     unit_cost=Decimal(cost), new_avg_cost=Decimal(cost))
    return p


def finalized_po(customer, product, qty=10, price="10.00", **po_kw):
    po = PurchaseOrder.objects.create(customer=customer, **po_kw)
    POLine.objects.create(po=po, product=product, qty_ordered=qty,
                          unit_price=Decimal(price))
    po.confirm()
    po.finalize()
    return po


class InvoiceTotalsTests(TestCase):
    def setUp(self):
        self.c = Customer.objects.create(business_name="Store", payment_terms="net30")
        ResaleCertificate.objects.create(
            customer=self.c, certificate_number="FL-9", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=100))
        self.p = stocked_product("INV-1")

    def test_single_po_invoice_totals_and_terms(self):
        po = finalized_po(self.c, self.p, qty=10, price="12.50",
                          charge_shipping=True, shipping_charge=Decimal("20.00"))
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.subtotal, Decimal("125.00"))
        self.assertEqual(inv.shipping_charge, Decimal("20.00"))
        self.assertEqual(inv.tax_amount, Decimal("0.00"))  # valid resale cert
        self.assertEqual(inv.total, Decimal("145.00"))
        self.assertEqual(inv.due_date, inv.date + timedelta(days=30))
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.INVOICED)

    def test_line_discount_math(self):
        po = PurchaseOrder.objects.create(customer=self.c)
        POLine.objects.create(po=po, product=self.p, qty_ordered=10,
                              unit_price=Decimal("10.00"),
                              discount_pct=Decimal("10"))
        po.confirm(); po.finalize()
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.subtotal, Decimal("90.00"))

    def test_consolidated_invoice_from_multiple_pos(self):
        p2 = stocked_product("INV-2")
        po1 = finalized_po(self.c, self.p, qty=5)
        po2 = finalized_po(self.c, p2, qty=3)
        inv = Invoice.build_from_pos([po1, po2])
        self.assertEqual(inv.subtotal, Decimal("80.00"))
        self.assertEqual(set(inv.po_numbers), {po1.number, po2.number})

    def test_mixed_customers_rejected(self):
        other = Customer.objects.create(business_name="Other")
        po1 = finalized_po(self.c, self.p, qty=1)
        po2 = finalized_po(other, stocked_product("INV-3"), qty=1)
        with self.assertRaises(ValidationError):
            Invoice.build_from_pos([po1, po2])

    def test_tax_applied_without_cert_and_manual_override(self):
        nocert = Customer.objects.create(business_name="Taxable Inc")
        po = finalized_po(nocert, self.p, qty=10, charge_tax=True,
                          tax_rate=Decimal("6.5"))
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.tax_amount, Decimal("6.50"))  # 100 * 6.5%
        self.assertEqual(inv.total, Decimal("106.50"))
        # manual override
        inv.compute_totals(tax_amount_override=Decimal("5.00"))
        self.assertTrue(inv.tax_overridden)
        self.assertEqual(inv.total, Decimal("105.00"))

    def test_invoice_uses_picked_qty_not_ordered(self):
        po = PurchaseOrder.objects.create(customer=self.c)
        POLine.objects.create(po=po, product=self.p, qty_ordered=10,
                              unit_price=Decimal("10.00"))
        po.confirm(); po.start_picking()
        line = po.live_lines.first()
        line.qty_picked = 8
        line.short_reason = "oos"
        line.save()
        po.complete_pick(); po.finalize()
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.subtotal, Decimal("80.00"))

    def test_cogs_and_gross_profit(self):
        po = finalized_po(self.c, self.p, qty=10)  # cost 4, price 10
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.cogs, Decimal("40.00"))
        self.assertEqual(inv.gross_profit, Decimal("60.00"))


class CreditMemoTests(TestCase):
    def setUp(self):
        self.c = Customer.objects.create(business_name="Returner")
        ResaleCertificate.objects.create(
            customer=self.c, certificate_number="X", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=30))
        self.p = stocked_product("RET-1", qty=50)
        po = finalized_po(self.c, self.p, qty=10)
        self.inv = Invoice.build_from_pos([po])

    def test_credit_restocks_and_reduces_balance(self):
        self.p.refresh_from_db()
        on_hand_before = self.p.qty_on_hand  # 40
        memo = CreditMemo.objects.create(invoice=self.inv, customer=self.c)
        CreditMemoLine.objects.create(credit_memo=memo, product=self.p,
                                      description="Return", qty=3,
                                      unit_price=Decimal("10.00"), restock=True,
                                      cost_snapshot=Decimal("4.0000"))
        memo.apply()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, on_hand_before)          # credit first…
        self.assertEqual(memo.total, Decimal("30.00"))
        self.assertEqual(self.inv.balance_due, Decimal("70.00"))
        self.assertTrue(memo.awaiting_return)
        memo.receive_returns()                                          # …stock when the boxes are back
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, on_hand_before + 3)
        self.assertFalse(memo.awaiting_return)

    def test_credit_without_restock_leaves_stock_alone(self):
        self.p.refresh_from_db()
        before = self.p.qty_on_hand
        memo = CreditMemo.objects.create(invoice=self.inv, customer=self.c)
        CreditMemoLine.objects.create(credit_memo=memo, product=self.p,
                                      description="Price adj", qty=1,
                                      unit_price=Decimal("10.00"), restock=False)
        memo.apply()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, before)


class CertOverridesPOTaxTests(TestCase):
    def test_valid_cert_zeroes_tax_even_if_po_charged_it(self):
        c = Customer.objects.create(business_name="Exempt Co")
        ResaleCertificate.objects.create(
            customer=c, certificate_number="OK-1", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=60))
        p = stocked_product("EX-1")
        po = PurchaseOrder.objects.create(customer=c, charge_tax=True,
                                          tax_rate=Decimal("6.5"))
        POLine.objects.create(po=po, product=p, qty_ordered=10,
                              unit_price=Decimal("10.00"))
        po.confirm(); po.finalize()
        inv = Invoice.build_from_pos([po])
        self.assertEqual(inv.tax_amount, Decimal("0.00"))
        # explicit rate still wins when passed on purpose
        po2 = PurchaseOrder.objects.create(customer=c)
        POLine.objects.create(po=po2, product=p, qty_ordered=1,
                              unit_price=Decimal("10.00"))
        po2.confirm(); po2.finalize()
        inv2 = Invoice.build_from_pos([po2], tax_rate=Decimal("5"))
        self.assertEqual(inv2.tax_amount, Decimal("0.50"))


class ManualLineTests(TestCase):
    """One-off items typed onto an invoice: price, cost, category."""

    def setUp(self):
        self.c = Customer.objects.create(business_name="Manual Co")
        self.p = stocked_product("MAN-1")
        self.po = finalized_po(self.c, self.p, qty=10, price="10.00")
        self.inv = Invoice.build_from_pos([self.po])  # $100 merch, $40 cogs

    def test_add_manual_line_updates_totals_and_profit(self):
        line = self.inv.add_manual_line(description="Prizm single",
                                        qty=2, unit_price="25.00",
                                        unit_cost="10.00",
                                        category_name="Singles")
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.subtotal, Decimal("150.00"))   # 100 + 50
        self.assertEqual(self.inv.cogs, Decimal("60.00"))        # 40 + 20
        self.assertEqual(self.inv.gross_profit, Decimal("90.00"))
        self.assertTrue(line.is_manual)
        self.assertEqual(line.category.name, "Singles")
        self.assertEqual(line.line_profit, Decimal("30.00"))

    def test_category_created_once_case_insensitive(self):
        from inventory.models import Category
        self.inv.add_manual_line(description="A", qty=1, unit_price="5.00",
                                 category_name="Supplies")
        self.inv.add_manual_line(description="B", qty=1, unit_price="5.00",
                                 category_name="supplies")
        self.assertEqual(Category.objects.filter(name__iexact="supplies").count(), 1)

    def test_remove_manual_line_restores_totals(self):
        line = self.inv.add_manual_line(description="Fee", qty=1,
                                        unit_price="9.99")
        self.inv.remove_manual_line(line.pk)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.subtotal, Decimal("100.00"))

    def test_add_blocked_after_payment(self):
        from payments.models import Payment
        pay = Payment.objects.create(customer=self.c, amount=Decimal("50.00"),
                                     date=timezone.localdate())
        pay.applications.create(invoice=self.inv, amount=Decimal("50.00"))
        with self.assertRaises(ValidationError):
            self.inv.add_manual_line(description="Late add", qty=1,
                                     unit_price="5.00")

    def test_manual_line_respects_tax_override(self):
        self.inv.compute_totals(tax_amount_override=Decimal("7.77"))
        self.inv.add_manual_line(description="X", qty=1, unit_price="10.00")
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.tax_amount, Decimal("7.77"))   # override kept
        self.assertEqual(self.inv.total, Decimal("117.77"))

    def test_create_view_accepts_manual_rows(self):
        from django.contrib.auth.models import User
        admin = User.objects.create_superuser("mboss", password="x")
        self.client.force_login(admin)
        po2 = finalized_po(self.c, self.p, qty=5, price="10.00")
        r = self.client.post("/invoices/new/", {
            "po_ids": [str(po2.pk)],
            "m_desc": ["Box break spot", ""],
            "m_cat": ["Breaks", ""],
            "m_qty": ["3", ""],
            "m_price": ["20.00", ""],
            "m_cost": ["8.00", ""],
        })
        self.assertEqual(r.status_code, 302)
        inv = Invoice.objects.latest("id")
        manual = inv.lines.get(is_manual=True)
        self.assertEqual(manual.description, "Box break spot")
        self.assertEqual(manual.category.name, "Breaks")
        self.assertEqual(inv.subtotal, Decimal("110.00"))        # 50 + 60

    def test_profit_by_category_report_includes_manual_lines(self):
        from django.contrib.auth.models import User
        admin = User.objects.create_superuser("rboss", password="x")
        self.client.force_login(admin)
        self.inv.add_manual_line(description="Single card", qty=1,
                                 unit_price="40.00", unit_cost="15.00",
                                 category_name="Singles")
        r = self.client.get("/reports/sales-by-product/?group=category")
        html = r.content.decode()
        self.assertIn("Singles", html)
        self.assertIn("25.00", html)   # 40 - 15 manual profit


class ManualInvoiceTests(TestCase):
    """Direct AR charges: invoices with no PO behind them."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("arboss", password="x")
        self.client.force_login(self.admin)
        self.c = Customer.objects.create(business_name="Fee Customer",
                                         payment_terms="net30")

    def test_build_manual_lands_in_ar(self):
        inv = Invoice.build_manual(self.c)
        inv.add_manual_line(description="Storage fee", qty=1,
                            unit_price="250.00")
        inv.refresh_from_db()
        self.assertEqual(inv.subtotal, Decimal("250.00"))
        self.assertGreater(inv.balance_due, Decimal("0"))
        self.assertEqual(inv.due_date, inv.date + timedelta(days=30))
        # shows up in AR aging
        r = self.client.get("/reports/ar-aging/")
        self.assertContains(r, "Fee Customer")

    def test_cert_customer_taxed_at_zero(self):
        ResaleCertificate.objects.create(
            customer=self.c, certificate_number="FL-77", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=30))
        inv = Invoice.build_manual(self.c)
        inv.add_manual_line(description="X", qty=1, unit_price="100.00")
        inv.refresh_from_db()
        self.assertEqual(inv.tax_amount, Decimal("0.00"))

    def test_no_cert_customer_gets_default_rate(self):
        from core.models import CompanySettings
        s = CompanySettings.get()
        s.default_tax_rate = Decimal("7.5")
        s.save()
        inv = Invoice.build_manual(self.c)
        inv.add_manual_line(description="X", qty=1, unit_price="100.00")
        inv.refresh_from_db()
        self.assertEqual(inv.tax_amount, Decimal("7.50"))

    def test_view_creates_manual_invoice(self):
        r = self.client.post("/invoices/new/manual/", {
            "customer_id": str(self.c.pk),
            "m_desc": ["Consulting"], "m_cat": ["Services"],
            "m_qty": ["2"], "m_price": ["75.00"], "m_cost": [""],
            "shipping_charge": "", "tax_rate": "0", "notes": "manual AR",
        })
        self.assertEqual(r.status_code, 302)
        inv = Invoice.objects.latest("id")
        self.assertEqual(inv.subtotal, Decimal("150.00"))
        self.assertEqual(inv.customer, self.c)
        self.assertIsNone(inv.lines.first().po)
        self.assertEqual(inv.lines.first().category.name, "Services")

    def test_view_rejects_empty_items(self):
        r = self.client.post("/invoices/new/manual/", {
            "customer_id": str(self.c.pk),
            "m_desc": [""], "m_qty": [""], "m_price": [""],
        }, follow=True)
        self.assertContains(r, "Add at least one item")
        self.assertEqual(Invoice.objects.count(), 0)

    def test_payment_applies_to_manual_invoice(self):
        from payments.models import Payment
        inv = Invoice.build_manual(self.c)
        inv.add_manual_line(description="Fee", qty=1, unit_price="100.00")
        inv.refresh_from_db()
        pay = Payment.objects.create(customer=self.c, amount=inv.total,
                                     date=timezone.localdate())
        pay.applications.create(invoice=inv, amount=inv.total)
        self.assertEqual(inv.balance_due, Decimal("0.00"))


class ShippedBeforeInvoicedTests(TestCase):
    """Local deliveries can ship before billing — those POs must stay invoiceable."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("shipboss", password="x")
        self.client.force_login(self.admin)
        self.c = Customer.objects.create(business_name="Deliver First LLC")
        self.p = stocked_product("SHIP-1")
        self.po = finalized_po(self.c, self.p, qty=10, price="10.00")
        self.po.mark_shipped()          # delivered before any invoice existed
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.SHIPPED)

    def test_shipped_uninvoiced_po_can_be_invoiced(self):
        inv = Invoice.build_from_pos([self.po])
        self.assertEqual(inv.subtotal, Decimal("100.00"))
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.SHIPPED)  # no regression
        # ...but not twice
        with self.assertRaises(ValidationError):
            Invoice.build_from_pos([self.po])

    def test_shipped_uninvoiced_po_listed_on_create_screen(self):
        r = self.client.get("/invoices/new/")
        self.assertContains(r, self.po.number)
        self.assertContains(r, "delivered, not billed")
        Invoice.build_from_pos([self.po])
        r = self.client.get("/invoices/new/")
        self.assertNotContains(r, "delivered, not billed")

    def test_voided_invoice_makes_shipped_po_invoiceable_again(self):
        inv = Invoice.build_from_pos([self.po])
        inv.void()
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.SHIPPED)
        inv2 = Invoice.build_from_pos([self.po])   # re-billable after void
        self.assertEqual(inv2.subtotal, Decimal("100.00"))


class BalanceOnPdfTests(TestCase):
    """Optional account-balance box on the invoice PDF (?balance=1)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("pdfboss", password="x")
        self.client.force_login(self.admin)
        self.c = Customer.objects.create(business_name="Balance Co")
        self.p = stocked_product("BAL-1")
        self.inv1 = Invoice.build_from_pos([finalized_po(self.c, self.p, qty=10)])  # $100
        self.inv2 = Invoice.build_from_pos([finalized_po(self.c, self.p, qty=5)])   # $50

    def test_context_math(self):
        from core.pdf import invoice_context
        ctx = invoice_context(self.inv2, include_balance=True)
        self.assertEqual(ctx["account_balance"], Decimal("150.00"))
        self.assertEqual(ctx["previous_balance"], Decimal("100.00"))
        self.assertEqual(ctx["balance_due"], Decimal("50.00"))
        # off by default
        ctx = invoice_context(self.inv2)
        self.assertFalse(ctx["include_balance"])
        self.assertIsNone(ctx["account_balance"])

    def test_pdf_renders_with_and_without_balance(self):
        r = self.client.get(f"/invoices/{self.inv2.pk}/pdf/?balance=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        r = self.client.get(f"/invoices/{self.inv2.pk}/pdf/")
        self.assertEqual(r.status_code, 200)



class InvoicePdfViewerTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from decimal import Decimal
        from orders.tests import make_customer, stocked_product
        from orders.models import POLine, PurchaseOrder
        from invoicing.models import Invoice
        self.client.force_login(User.objects.create_superuser("pdfv", password="x"))
        p = stocked_product("PDF-1", qty=10, cost="4.00")
        po = PurchaseOrder.objects.create(customer=make_customer("Viewer Co"))
        POLine.objects.create(po=po, product=p, qty_ordered=1, unit_price=Decimal("10.00"))
        po.confirm(); po.finalize()
        self.inv = Invoice.build_from_pos([po])

    def test_viewer_page_has_save_and_share(self):
        r = self.client.get(f"/invoices/{self.inv.pk}/pdf/view/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Save to this phone", html)
        self.assertIn('download="%s.pdf"' % self.inv.number, html)
        self.assertIn("download=1", html)
        self.assertIn("navigator.share", html)

    def test_download_flag_forces_attachment(self):
        r = self.client.get(f"/invoices/{self.inv.pk}/pdf/?download=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Disposition"].startswith("attachment;"))
        self.assertTrue(r.content.startswith(b"%PDF"))
        r = self.client.get(f"/invoices/{self.inv.pk}/pdf/")
        self.assertTrue(r["Content-Disposition"].startswith("inline;"))

    def test_detail_links_to_viewer(self):
        html = self.client.get(f"/invoices/{self.inv.pk}/").content.decode()
        self.assertIn(f"/invoices/{self.inv.pk}/pdf/view/", html)


class InvoicePdfViewerPhoneTests(TestCase):
    def test_viewer_uses_share_sheet_on_iphone(self):
        from django.contrib.auth.models import User
        from decimal import Decimal
        from orders.tests import make_customer, stocked_product
        from orders.models import POLine, PurchaseOrder
        from invoicing.models import Invoice
        self.client.force_login(User.objects.create_superuser("pdfp", password="x"))
        p = stocked_product("PDF-2", qty=10, cost="4.00")
        po = PurchaseOrder.objects.create(customer=make_customer("Phone Co"))
        POLine.objects.create(po=po, product=p, qty_ordered=1, unit_price=Decimal("10.00"))
        po.confirm(); po.finalize()
        inv = Invoice.build_from_pos([po])
        html = self.client.get(f"/invoices/{inv.pk}/pdf/view/").content.decode()
        self.assertIn('id="saveBtn"', html)
        self.assertIn("navigator.share", html)
        self.assertIn("isIOS", html)
        self.assertIn("Open the PDF", html)
        self.assertIn('class="card-pad sm:hidden', html)      # phone layout, no iframe
        # the PDF itself must be embeddable in the preview page (same origin),
        # otherwise browsers show "refused to connect" instead of the invoice
        r = self.client.get(f"/invoices/{inv.pk}/pdf/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")


class ReturnsAndRefundsTests(TestCase):
    """Return at the sold price or less a restocking fee; stock comes back when
    received; a paid invoice's credit is refunded or moved to another invoice."""

    def setUp(self):
        from django.contrib.auth.models import User
        from payments.models import Payment
        self.user = User.objects.create_superuser("ret", password="x")
        self.client.force_login(self.user)
        self.c = Customer.objects.create(business_name="Ross Cards")
        ResaleCertificate.objects.create(customer=self.c, certificate_number="X", issuing_state="FL",
                                         expiration_date=timezone.localdate() + timedelta(days=30))
        self.p = stocked_product("RR-1", qty=50)
        po = finalized_po(self.c, self.p, qty=10)          # 10 × $10
        self.inv = Invoice.build_from_pos([po])
        self.line = self.inv.lines.get()
        self.pay = Payment.objects.create(customer=self.c, amount=Decimal("100.00"), method="zelle")
        self.pay.apply_to_invoice(self.inv, Decimal("100.00"))
        self.assertEqual(self.inv.balance_due, Decimal("0.00"))

    def _issue(self, qty, pct="0", restock=True):
        data = {f"qty_{self.line.pk}": str(qty), "restocking_pct": pct, "reason": "test"}
        if restock:
            data[f"restock_{self.line.pk}"] = "on"
        r = self.client.post(f"/invoices/{self.inv.pk}/credit/", data)
        self.assertEqual(r.status_code, 302, r.content[:200])
        return CreditMemo.objects.filter(invoice=self.inv).order_by("-id").first()

    def test_restocking_fee_and_caps(self):
        memo = self._issue(4, pct="15")
        line = memo.lines.get()
        self.assertEqual((line.sold_unit_price, line.unit_price), (Decimal("10.00"), Decimal("8.50")))
        self.assertEqual(memo.total, Decimal("34.00"))
        self.assertEqual(memo.restocking_pct, Decimal("15"))
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_returnable, 6)
        # can't return more than what is left
        r = self.client.post(f"/invoices/{self.inv.pk}/credit/", {f"qty_{self.line.pk}": "7", "restocking_pct": "0"}, follow=True)
        self.assertIn("only 6 of 10", r.content.decode())
        self.assertEqual(CreditMemo.objects.filter(invoice=self.inv).count(), 1)

    def test_paid_invoice_credit_is_owed_then_refunded(self):
        from payments.models import Refund
        memo = self._issue(3)
        self.assertEqual(memo.total, Decimal("30.00"))
        self.assertEqual(self.inv.balance_due, Decimal("-30.00"))
        self.assertEqual(memo.credit_available, Decimal("30.00"))
        self.assertTrue(memo.awaiting_return)
        self.p.refresh_from_db(); before = self.p.qty_on_hand
        # goods come back: 2 of 3 actually arrive
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "receive", f"got_{memo.lines.get().pk}": "2"})
        self.assertEqual(r.status_code, 302)
        self.p.refresh_from_db(); memo.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, before + 2)
        self.assertFalse(memo.awaiting_return)
        # refund the customer
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "refund", "amount": "30.00", "method": "zelle", "reference": "Z123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Refund.objects.get().amount, Decimal("30.00"))
        self.assertEqual(self.inv.balance_due, Decimal("0.00"))
        self.assertEqual(memo.credit_available, Decimal("0.00"))
        self.assertEqual(self.c.balance, Decimal("0.00"))
        # can't refund twice
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "refund", "amount": "1.00"}, follow=True)
        self.assertIn("Only $0.00", r.content.decode())
        html = self.client.get("/payments/").content.decode()
        self.assertIn("Refunds to customers", html)

    def test_credit_moved_to_another_invoice(self):
        from payments.models import Payment
        po2 = finalized_po(self.c, self.p, qty=5)
        inv2 = Invoice.build_from_pos([po2])                 # $50 open
        memo = self._issue(3)                                 # $30 owed back
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "move_credit", "invoice_id": inv2.pk, "amount": "30.00"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(inv2.balance_due, Decimal("20.00"))
        self.assertEqual(self.inv.balance_due, Decimal("0.00"))
        self.assertEqual(memo.credit_available, Decimal("0.00"))
        credit_pay = Payment.objects.get(method="credit")
        self.assertEqual(credit_pay.credit_memo, memo)
        self.assertEqual(self.c.balance, Decimal("20.00"))

    def test_unpaid_invoice_credit_just_reduces_balance(self):
        self.pay.unapply(self.pay.applications.get())
        memo = self._issue(2, restock=False)
        self.assertEqual(self.inv.balance_due, Decimal("80.00"))
        self.assertEqual(memo.credit_available, Decimal("0.00"))
        self.assertIsNotNone(memo.received_at)                # nothing coming back → nothing to wait for
        self.assertFalse(memo.awaiting_return)

    def test_ledger_books_refund_both_bases(self):
        from accounting.ledger import ACCRUAL, CASH, Journal
        from accounting.models import Account
        from banking.models import BankAccount
        from payments.models import Refund
        Account.ensure_system_accounts()
        acct = BankAccount.objects.create(name="Chase", account_type="checking", opening_balance=Decimal("1000"),
                                          opening_date=timezone.localdate() - timedelta(days=30))
        memo = self._issue(3)
        Refund.objects.create(customer=self.c, credit_memo=memo, amount=Decimal("30.00"), paid_from=acct, method="zelle")
        today = timezone.localdate()
        for basis in (ACCRUAL, CASH):
            j = Journal(basis, [today])
            tb = j.trial_balance(today)
            self.assertTrue(tb["balanced"], basis)
            pl = j.income_statement(today - timedelta(days=30), today)
            inc = {r["account"].system_key: r["amount"] for r in pl["income"]}
            self.assertEqual(inc.get("sales_returns"), Decimal("-30.00"), basis)


class CreditMemoFixTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_superuser("fix", password="x")
        self.client.force_login(self.user)
        self.c = Customer.objects.create(business_name="Oops Cards")
        ResaleCertificate.objects.create(customer=self.c, certificate_number="X", issuing_state="FL",
                                         expiration_date=timezone.localdate() + timedelta(days=30))
        self.p = stocked_product("FX-1", qty=50)
        po = finalized_po(self.c, self.p, qty=10)
        self.inv = Invoice.build_from_pos([po])
        self.line = self.inv.lines.get()
        self.p.refresh_from_db(); self.on_hand = self.p.qty_on_hand           # 40

    def _memo(self, qty=3, restock=False):
        data = {f"qty_{self.line.pk}": str(qty), "restocking_pct": "0"}
        if restock:
            data[f"restock_{self.line.pk}"] = "on"
        self.client.post(f"/invoices/{self.inv.pk}/credit/", data)
        return CreditMemo.objects.get(invoice=self.inv)

    def test_switch_items_to_coming_back_then_receive(self):
        memo = self._memo(restock=False)
        self.assertIsNotNone(memo.received_at); self.assertFalse(memo.awaiting_return)
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "set_restock", f"restock_{memo.lines.get().pk}": "on"})
        self.assertEqual(r.status_code, 302)
        memo.refresh_from_db()
        self.assertTrue(memo.awaiting_return)
        self.assertEqual(memo.total, Decimal("30.00"))                      # money untouched
        self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "receive", f"got_{memo.lines.get().pk}": "3"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, self.on_hand + 3)
        # can't switch off once restocked
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "set_restock"}, follow=True)
        self.assertIn("already went back into stock", r.content.decode())

    def test_void_reverses_credit_and_stock(self):
        memo = self._memo(restock=True)
        memo.receive_returns(user=self.user)
        self.p.refresh_from_db(); self.assertEqual(self.p.qty_on_hand, self.on_hand + 3)
        self.assertEqual(self.inv.balance_due, Decimal("70.00"))
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "void", "reason": "wrong qty"})
        self.assertEqual(r.status_code, 302)
        memo.refresh_from_db(); self.p.refresh_from_db()
        self.assertEqual(memo.status, CreditMemo.VOID)
        self.assertEqual(self.p.qty_on_hand, self.on_hand)
        self.assertEqual(self.inv.balance_due, Decimal("100.00"))
        self.line.refresh_from_db(); self.assertEqual(self.line.qty_returnable, 10)   # can be redone
        memo2 = self._memo_again(4)
        self.assertEqual(self.inv.balance_due, Decimal("60.00"))

    def _memo_again(self, qty):
        self.client.post(f"/invoices/{self.inv.pk}/credit/", {f"qty_{self.line.pk}": str(qty), "restocking_pct": "0", f"restock_{self.line.pk}": "on"})
        return CreditMemo.objects.filter(invoice=self.inv, status="applied").get()

    def test_void_blocked_by_refund(self):
        from payments.models import Payment
        pay = Payment.objects.create(customer=self.c, amount=Decimal("100.00"), method="zelle")
        pay.apply_to_invoice(self.inv, Decimal("100.00"))
        memo = self._memo(restock=False)
        self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "refund", "amount": "30.00", "method": "zelle"})
        r = self.client.post(f"/invoices/credits/{memo.pk}/", {"action": "void"}, follow=True)
        self.assertIn("Remove the refund", r.content.decode())
        memo.refresh_from_db(); self.assertEqual(memo.status, CreditMemo.APPLIED)
