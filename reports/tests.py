"""Report math: shipping kept out of profit; sales-tax report totals."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from invoicing.models import Invoice
from orders.models import POLine, PurchaseOrder
from orders.tests import make_customer, stocked_product


class ReportSetup(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="x")
        self.client.force_login(self.admin)
        self.p = stocked_product("RPT-1", qty=100, cost="4.00")

        # Exempt sale: valid resale certificate -> 0% tax
        self.exempt_cust = make_customer("Exempt LLC", cert=True)
        po1 = PurchaseOrder.objects.create(customer=self.exempt_cust)
        POLine.objects.create(po=po1, product=self.p, qty_ordered=10,
                              unit_price=Decimal("10.00"))
        po1.confirm(); po1.finalize()
        self.inv_exempt = Invoice.build_from_pos(
            [po1], shipping_charge=Decimal("15.00"))  # $100 merch + $15 ship

        # Taxable sale: no certificate, PO charges 7.5% tax
        self.tax_cust = make_customer("NoCert Retail", cert=False)
        po2 = PurchaseOrder.objects.create(customer=self.tax_cust,
                                           charge_tax=True,
                                           tax_rate=Decimal("7.5"))
        POLine.objects.create(po=po2, product=self.p, qty_ordered=20,
                              unit_price=Decimal("10.00"))
        po2.confirm(); po2.finalize()
        self.inv_taxable = Invoice.build_from_pos(
            [po2], shipping_charge=Decimal("5.00"))   # $200 merch + $5 ship


class ShippingOutOfProfitTests(ReportSetup):
    def test_invoice_profit_excludes_shipping(self):
        # $100 merch - $40 cogs = $60; the $15 shipping must not inflate it
        self.assertEqual(self.inv_exempt.gross_profit, Decimal("60.00"))

    def test_pnl_separates_shipping_from_gross_profit(self):
        r = self.client.get("/reports/pnl/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        # merch 300, cogs 120 -> gross 180 (NOT 200 with the 20 shipping)
        self.assertIn("GROSS PROFIT (merchandise — shipping excluded)", html)
        self.assertIn("180.00", html)
        self.assertIn("NET SHIPPING (pass-through)", html)
        self.assertIn("20.00", html)

    def test_by_customer_report_shows_shipping_separately(self):
        r = self.client.get("/reports/revenue-by-customer/")
        html = r.content.decode()
        self.assertIn("Merch revenue $", html)
        self.assertIn("Shipping $", html)


class SalesTaxReportTests(ReportSetup):
    def test_taxable_vs_exempt_split_and_tax_collected(self):
        r = self.client.get("/reports/sales-tax/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        # exempt: 100 + 15 = 115; taxable: 200 + 5 = 205; tax = 205 * 7.5% = 15.38
        self.assertIn("Exempt", html)
        self.assertIn("Taxable", html)
        self.assertIn("Taxable: $205.00", html)
        self.assertIn("Exempt: $115.00", html)
        self.assertIn("15.38", html)
        # certificate invoice collected zero tax
        self.assertEqual(self.inv_exempt.tax_amount, Decimal("0.00"))
        self.assertEqual(self.inv_taxable.tax_amount, Decimal("15.38"))

    def test_void_invoices_excluded(self):
        self.inv_taxable.void()
        r = self.client.get("/reports/sales-tax/")
        html = r.content.decode()
        self.assertIn("Taxable: $0.00", html)
        self.assertIn("Exempt: $115.00", html)

    def test_shipping_report_charged_vs_paid(self):
        from django.utils import timezone
        from payments.models import Payment
        from shipping.models import Shipment
        # record what WE paid a carrier
        po = PurchaseOrder.objects.filter(customer=self.tax_cust).first()
        Shipment.objects.create(po=po, provider="shippo", cost=Decimal("8.25"),
                                ship_date=timezone.localdate())
        # pay off the exempt invoice in full so its shipping counts as collected
        pay = Payment.objects.create(customer=self.exempt_cust,
                                     amount=self.inv_exempt.total,
                                     date=timezone.localdate())
        pay.applications.create(invoice=self.inv_exempt, amount=self.inv_exempt.total)
        r = self.client.get("/reports/shipping/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("20.00", html)           # charged: 15 + 5
        self.assertIn("Collected (on paid invoices): $15.00", html)
        self.assertIn("8.25", html)            # paid to carrier

    def test_csv_export_works(self):
        r = self.client.get("/reports/sales-tax/?export=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        body = r.content.decode()
        self.assertIn("Tax collected $", body)
        self.assertIn(self.inv_taxable.number, body)
