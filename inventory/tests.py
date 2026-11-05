"""Money-critical tests: weighted average cost, landed cost allocation,
ledger integrity, point-in-time valuation."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from inventory.models import Product, Receipt, ReceiptLine, StockAdjustment, StockMovement


def make_product(sku="TEST-1", **kw):
    defaults = dict(name="Test product", default_price=Decimal("10.00"))
    defaults.update(kw)
    return Product.objects.create(sku=sku, **defaults)


class WeightedAverageCostTests(TestCase):
    def test_first_receipt_sets_avg_cost_to_landed_unit(self):
        p = make_product()
        r = Receipt.objects.create(freight_cost=Decimal("100.00"))
        ReceiptLine.objects.create(receipt=r, product=p, qty=100,
                                   unit_cost=Decimal("5.00"))
        r.post()
        p.refresh_from_db()
        # landed = (100*5 + 100 freight) / 100 = 6.00
        self.assertEqual(p.avg_cost, Decimal("6.0000"))
        self.assertEqual(p.qty_on_hand, 100)

    def test_weighted_average_rolls_correctly(self):
        p = make_product()
        r1 = Receipt.objects.create()
        ReceiptLine.objects.create(receipt=r1, product=p, qty=100, unit_cost=Decimal("4.00"))
        r1.post()
        r2 = Receipt.objects.create()
        ReceiptLine.objects.create(receipt=r2, product=p, qty=100, unit_cost=Decimal("6.00"))
        r2.post()
        p.refresh_from_db()
        self.assertEqual(p.avg_cost, Decimal("5.0000"))  # (100*4 + 100*6)/200
        self.assertEqual(p.qty_on_hand, 200)

    def test_freight_allocated_proportionally_to_merch_value(self):
        pa, pb = make_product("A"), make_product("B")
        r = Receipt.objects.create(freight_cost=Decimal("90.00"))
        # A: 10 * 20 = 200 ; B: 10 * 10 = 100  -> A gets 60, B gets 30
        la = ReceiptLine.objects.create(receipt=r, product=pa, qty=10, unit_cost=Decimal("20.00"))
        lb = ReceiptLine.objects.create(receipt=r, product=pb, qty=10, unit_cost=Decimal("10.00"))
        r.post()
        la.refresh_from_db(); lb.refresh_from_db()
        self.assertEqual(la.landed_unit_cost, Decimal("26.0000"))  # (200+60)/10
        self.assertEqual(lb.landed_unit_cost, Decimal("13.0000"))  # (100+30)/10

    def test_receipt_cannot_post_twice(self):
        p = make_product()
        r = Receipt.objects.create()
        ReceiptLine.objects.create(receipt=r, product=p, qty=5, unit_cost=Decimal("1.00"))
        r.post()
        with self.assertRaises(ValidationError):
            r.post()
        p.refresh_from_db()
        self.assertEqual(p.qty_on_hand, 5)


class LedgerTests(TestCase):
    def test_every_movement_records_before_after(self):
        p = make_product()
        p.apply_movement(movement_type="adjustment", qty_delta=10)
        p.apply_movement(movement_type="adjustment", qty_delta=-3)
        moves = list(StockMovement.objects.filter(product=p).order_by("id"))
        self.assertEqual([(m.qty_before, m.qty_after) for m in moves],
                         [(0, 10), (10, 7)])

    def test_cannot_go_below_zero(self):
        p = make_product()
        p.apply_movement(movement_type="adjustment", qty_delta=5)
        with self.assertRaises(ValidationError):
            p.apply_movement(movement_type="adjustment", qty_delta=-6)
        p.refresh_from_db()
        self.assertEqual(p.qty_on_hand, 5)

    def test_adjustment_helper_links_movement(self):
        p = make_product()
        adj = StockAdjustment.create_and_apply(product=p, qty_delta=4,
                                               reason="received")
        self.assertIsNotNone(adj.movement)
        self.assertEqual(adj.movement.qty_after, 4)


class OpeningStockAtCostTests(TestCase):
    def test_opening_adjustment_sets_avg_cost(self):
        p = make_product()
        StockAdjustment.create_and_apply(product=p, qty_delta=50,
                                         reason="opening", unit_cost=Decimal("7.25"))
        p.refresh_from_db()
        self.assertEqual(p.qty_on_hand, 50)
        self.assertEqual(p.avg_cost, Decimal("7.2500"))

    def test_adding_at_cost_rolls_weighted_average(self):
        p = make_product()
        StockAdjustment.create_and_apply(product=p, qty_delta=100,
                                         reason="opening", unit_cost=Decimal("4.00"))
        StockAdjustment.create_and_apply(product=p, qty_delta=100,
                                         reason="received", unit_cost=Decimal("6.00"))
        p.refresh_from_db()
        self.assertEqual(p.avg_cost, Decimal("5.0000"))

    def test_removal_never_changes_avg_cost(self):
        p = make_product()
        StockAdjustment.create_and_apply(product=p, qty_delta=100,
                                         reason="opening", unit_cost=Decimal("4.00"))
        StockAdjustment.create_and_apply(product=p, qty_delta=-30,
                                         reason="correction", unit_cost=Decimal("99.00"))
        p.refresh_from_db()
        self.assertEqual(p.qty_on_hand, 70)
        self.assertEqual(p.avg_cost, Decimal("4.0000"))

    def test_adjustment_without_cost_keeps_avg(self):
        p = make_product()
        StockAdjustment.create_and_apply(product=p, qty_delta=10,
                                         reason="opening", unit_cost=Decimal("3.00"))
        StockAdjustment.create_and_apply(product=p, qty_delta=5, reason="correction")
        p.refresh_from_db()
        self.assertEqual(p.avg_cost, Decimal("3.0000"))


class PointInTimeValuationTests(TestCase):
    def test_valuation_reads_history_not_current_state(self):
        p = make_product()
        r1 = Receipt.objects.create()
        ReceiptLine.objects.create(receipt=r1, product=p, qty=100, unit_cost=Decimal("4.00"))
        r1.post()
        # pretend that happened 10 days ago
        StockMovement.objects.filter(product=p).update(
            timestamp=timezone.now() - timedelta(days=10))
        # newer receipt changes qty and cost
        r2 = Receipt.objects.create()
        ReceiptLine.objects.create(receipt=r2, product=p, qty=100, unit_cost=Decimal("8.00"))
        r2.post()

        cutoff = timezone.now() - timedelta(days=5)
        last = (StockMovement.objects.filter(product=p, timestamp__lte=cutoff)
                .order_by("-timestamp", "-id").first())
        self.assertEqual(last.qty_after, 100)
        self.assertEqual(last.avg_cost_after, Decimal("4.0000"))
        # value as of 5 days ago = 400, not today's 200 @ 6.00 = 1200
        self.assertEqual(last.qty_after * last.avg_cost_after, Decimal("400.0000"))


class RevalueCostTests(TestCase):
    def test_revalue_sets_avg_without_touching_qty(self):
        p = make_product()
        p.apply_movement(movement_type="receipt", qty_delta=50,
                         unit_cost=Decimal("4.00"), new_avg_cost=Decimal("4.00"))
        p.revalue_cost(Decimal("6.50"), note="onboarding fix")
        p.refresh_from_db()
        self.assertEqual(p.qty_on_hand, 50)
        self.assertEqual(p.avg_cost, Decimal("6.5000"))
        move = p.movements.first()
        self.assertEqual(move.movement_type, "revalue")
        self.assertEqual(move.qty_delta, 0)
        self.assertEqual(move.avg_cost_after, Decimal("6.5000"))

    def test_revalue_feeds_future_valuation_not_past(self):
        p = make_product()
        p.apply_movement(movement_type="receipt", qty_delta=10,
                         unit_cost=Decimal("4.00"), new_avg_cost=Decimal("4.00"))
        first_move = p.movements.first()
        p.revalue_cost(Decimal("9.00"))
        # older ledger rows keep their historical avg
        first_move.refresh_from_db()
        self.assertEqual(first_move.avg_cost_after, Decimal("4.0000"))


class LowStockMuteTests(TestCase):
    def test_muted_products_excluded_from_digest_and_dashboard_logic(self):
        p = make_product(reorder_point=10)
        p.apply_movement(movement_type="adjustment", qty_delta=5)  # low (5 <= 10)
        self.assertTrue(p.is_low_stock)
        p.low_stock_alert_muted = True
        p.save()
        visible = [x for x in Product.objects.filter(active=True)
                   if x.is_low_stock and not x.low_stock_alert_muted]
        self.assertNotIn(p, visible)
        # still counts as low stock for filters/reality
        self.assertTrue(p.is_low_stock)


class ProductDeleteTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("delboss", password="x")
        self.client.force_login(self.admin)
        self.p = Product.objects.create(sku="DEL-1", name="Deletable",
                                        default_price=Decimal("10.00"))
        self.p.apply_movement(movement_type="receipt", qty_delta=5,
                              unit_cost=Decimal("2.00"),
                              new_avg_cost=Decimal("2.00"))

    def test_delete_blocked_while_on_open_order(self):
        from customers.models import Customer
        from orders.models import POLine, PurchaseOrder
        c = Customer.objects.create(business_name="Blocker")
        po = PurchaseOrder.objects.create(customer=c)
        POLine.objects.create(po=po, product=self.p, qty_ordered=1,
                              unit_price=Decimal("10.00"))
        po.confirm()
        r = self.client.post(f"/inventory/{self.p.pk}/delete/", follow=True)
        self.p.refresh_from_db()
        self.assertFalse(self.p.is_deleted)
        self.assertContains(r, "open order")

    def test_delete_soft_deletes_and_keeps_history(self):
        from inventory.models import StockMovement
        r = self.client.post(f"/inventory/{self.p.pk}/delete/")
        self.assertEqual(r.status_code, 302)
        self.p.refresh_from_db()
        self.assertTrue(self.p.is_deleted)
        self.assertFalse(Product.objects.filter(pk=self.p.pk).exists())      # hidden
        self.assertTrue(Product.all_objects.filter(pk=self.p.pk).exists())   # recoverable
        self.assertTrue(StockMovement.objects.filter(product=self.p).exists())  # ledger kept



class SupplierBillTests(TestCase):
    """Receipts double as supplier bills: due date, paid state, balance."""

    def setUp(self):
        from django.contrib.auth.models import User
        from decimal import Decimal
        from inventory.models import Product, Receipt, ReceiptLine, Supplier
        self.admin = User.objects.create_superuser("billadmin", password="x")
        self.client.force_login(self.admin)
        self.p = Product.objects.create(sku="BILL-1", name="Box", default_price=Decimal("10"))
        self.r = Receipt.objects.create(supplier=Supplier.objects.create(name="GTS"))
        ReceiptLine.objects.create(receipt=self.r, product=self.p, qty=10, unit_cost=Decimal("50"))
        self.r.post()

    def test_posted_receipt_is_an_unpaid_bill_until_paid(self):
        from decimal import Decimal
        self.assertFalse(self.r.is_paid)
        self.assertEqual(self.r.balance_due, Decimal("500.00"))
        r = self.client.get("/inventory/receiving/?status=unpaid")
        self.assertContains(r, "RCV-%d" % self.r.pk)

    def test_bill_details_editable_after_posting(self):
        from decimal import Decimal
        r = self.client.post(f"/inventory/receiving/{self.r.pk}/", {
            "action": "save_bill", "supplier_invoice_number": "INV-77",
            "due_date": "2026-12-01", "paid_date": "2026-10-01", "payment_reference": "chk 1001",
        })
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db()
        self.assertEqual(self.r.supplier_invoice_number, "INV-77")
        self.assertTrue(self.r.is_paid)
        self.assertEqual(self.r.amount_paid, Decimal("500.00"))
        page = self.client.get(f"/inventory/receiving/{self.r.pk}/")
        self.assertContains(page, "Paid")
        self.assertContains(page, "INV-77")

    def test_detail_page_renders_bill_card(self):
        page = self.client.get(f"/inventory/receiving/{self.r.pk}/")
        self.assertContains(page, "Supplier bill")
        self.assertContains(page, "Unpaid")

    def test_one_click_mark_paid_uses_default_account(self):
        from banking.models import BankAccount
        from django.utils import timezone
        acct = BankAccount.objects.create(name="Checking", is_default=True)
        r = self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "mark_paid"})
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db()
        self.assertEqual(self.r.paid_date, timezone.localdate())
        self.assertEqual(self.r.paid_from, acct)
        self.assertTrue(self.r.is_paid)
        # second click is harmless
        self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "mark_paid", "next": "/inventory/receiving/"})
        self.r.refresh_from_db(); self.assertEqual(self.r.paid_from, acct)

    def test_mark_paid_without_default_account_counts_as_personal(self):
        self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "mark_paid"})
        self.r.refresh_from_db()
        self.assertTrue(self.r.is_paid); self.assertIsNone(self.r.paid_from)


class CameraScannerWiringTests(TestCase):
    """Every place a SKU can be typed offers the phone-camera scan button."""

    def setUp(self):
        from django.contrib.auth.models import User
        from decimal import Decimal
        from inventory.models import PriceList, Product, Receipt
        from orders.tests import make_customer
        from orders.models import PurchaseOrder
        self.client.force_login(User.objects.create_superuser("scan", password="x"))
        self.p = Product.objects.create(sku="SCAN-1", name="Box", upc="012345678905",
                                        default_price=Decimal("10"))
        self.receipt = Receipt.objects.create()
        self.pl = PriceList.objects.create(name="Wholesale")
        self.po = PurchaseOrder.objects.create(customer=make_customer("Scan Shop"))

    def test_scanner_script_loads_on_app_pages_and_pick_screen(self):
        self.assertContains(self.client.get("/inventory/"), "js/scanner.js")
        self.assertContains(self.client.get(f"/pick/{self.po.pk}/"), "js/scanner.js")

    def test_inputs_opt_in(self):
        html = self.client.get("/inventory/").content.decode()
        self.assertIn('name="q"', html)
        self.assertRegex(html, r'name="q"[^>]*data-scan')          # product search
        form = self.client.get("/inventory/new/").content.decode()
        self.assertRegex(form, r'name="sku"[^>]*data-scan')
        self.assertRegex(form, r'name="upc"[^>]*data-scan')
        pick = self.client.get(f"/pick/{self.po.pk}/").content.decode()
        self.assertIn("data-scan-continuous", pick)
        po = self.client.get(f"/orders/{self.po.pk}/").content.decode()
        self.assertRegex(po, r'name="barcode"[^>]*data-scan-submit')

    def test_product_selects_carry_sku_and_upc(self):
        for url in (f"/inventory/receiving/{self.receipt.pk}/", f"/inventory/price-lists/{self.pl.pk}/",
                    f"/orders/{self.po.pk}/"):
            html = self.client.get(url).content.decode()
            self.assertIn("data-scan-select", html, url)
            self.assertIn('data-upc="SCAN-1 012345678905"', html, url)




class InboundTrackingTests(TestCase):
    """Several tracking numbers per supplier order; Shippo fills status + ETA."""

    class FakeShippo:
        def __init__(self, payload=None, error=None, by_number=None):
            self.payload, self.error, self.by_number = payload, error, by_number or {}
            self.calls = []

        def is_configured(self):
            return True

        def _request(self, method, path, payload=None):
            from shipping.providers import ProviderError
            self.calls.append(path)
            if self.error:
                raise ProviderError(self.error)
            num = path.rsplit("/", 1)[-1]
            return self.by_number.get(num, self.payload)

    @staticmethod
    def _payload(status, eta, detail="Moving", city="Tampa"):
        return {"tracking_status": {"status": status, "status_details": detail,
                                    "status_date": "2026-11-03T14:05:00Z",
                                    "location": {"city": city, "state": "FL"}},
                "eta": eta}

    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Receipt, Supplier
        self.client.force_login(User.objects.create_superuser("trk", password="x"))
        self.r = Receipt.objects.create(supplier=Supplier.objects.create(name="GTS"))

    def test_guess_carrier(self):
        from inventory.tracking import guess_carrier
        self.assertEqual(guess_carrier("1Z999AA10123456784"), "ups")
        self.assertEqual(guess_carrier("9400111899223456789012"), "usps")
        self.assertEqual(guess_carrier("TBA123456789000"), "amazon")

    def test_add_several_numbers_at_once_and_expected_follows_latest_eta(self):
        from unittest.mock import patch
        fake = self.FakeShippo(by_number={
            "9400111899223456789012": self._payload("TRANSIT", "2026-11-06T18:00:00Z"),
            "1Z999AA10123456784": self._payload("TRANSIT", "2026-11-09T18:00:00Z"),
        })
        with patch("shipping.providers.get_provider", return_value=fake):
            r = self.client.post(f"/inventory/receiving/{self.r.pk}/", {
                "action": "add_packages", "carrier": "",
                "tracking_numbers": "9400111899223456789012\n1Z999AA10123456784, 9400111899223456789012"})
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db()
        pkgs = list(self.r.packages.order_by("id"))
        self.assertEqual([p.carrier for p in pkgs], ["usps", "ups"])      # guessed, deduped
        self.assertEqual({p.status for p in pkgs}, {"TRANSIT"})
        self.assertEqual(str(self.r.expected_date), "2026-11-09")          # last box wins
        self.assertEqual(self.r.tracking_badge[0], "In transit (2 boxes)")
        page = self.client.get(f"/inventory/receiving/{self.r.pk}/")
        self.assertContains(page, "ups.com/track")
        self.assertContains(page, "tools.usps.com")

    def test_partial_delivery_badge_and_remove(self):
        from inventory.models import ReceiptPackage
        a = ReceiptPackage.objects.create(receipt=self.r, tracking_number="A1", carrier="ups", status="DELIVERED")
        b = ReceiptPackage.objects.create(receipt=self.r, tracking_number="B2", carrier="ups", status="TRANSIT")
        self.assertEqual(self.r.tracking_badge[0], "1 of 2 delivered")
        self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "del_package", "package_id": b.pk})
        self.assertEqual(self.r.packages.count(), 1)
        self.assertEqual(self.r.tracking_badge[0], "Delivered")

    def test_provider_error_is_recorded_not_raised(self):
        from inventory.models import ReceiptPackage
        from inventory.tracking import refresh_receipt
        ReceiptPackage.objects.create(receipt=self.r, tracking_number="1Z999AA10123456784")
        ok, msg = refresh_receipt(self.r, self.FakeShippo(error="Shippo 404: not found"))
        self.assertFalse(ok)
        self.assertIn("404", self.r.packages.get().error)

    def test_receiving_list_refresh_all_button(self):
        from unittest.mock import patch
        from inventory.models import Receipt, ReceiptPackage
        ReceiptPackage.objects.create(receipt=self.r, tracking_number="9400111899223456789012", carrier="usps")
        other = Receipt.objects.create()
        ReceiptPackage.objects.create(receipt=other, tracking_number="1Z999AA10123456784", carrier="ups")
        fake = self.FakeShippo(self._payload("TRANSIT", "2026-11-08T12:00:00Z"))
        page = self.client.get("/inventory/receiving/")
        self.assertContains(page, "Refresh tracking")
        with patch("shipping.providers.get_provider", return_value=fake):
            r = self.client.post("/inventory/receiving/", {"action": "refresh_tracking"})
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(str(self.r.expected_date), "2026-11-08")
        self.assertEqual(str(other.expected_date), "2026-11-08")
        self.assertEqual(len(fake.calls), 2)

    def test_refresh_all_without_token_warns(self):
        r = self.client.post("/inventory/receiving/", {"action": "refresh_tracking"}, follow=True)
        self.assertContains(r, "Add a Shippo token")

    def test_hourly_task_skips_without_token(self):
        from inventory.tasks import refresh_inbound_tracking
        self.assertIn("not configured", refresh_inbound_tracking())


class ReceivingSearchTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Product, ProductBarcode, Receipt, ReceiptLine, ReceiptPackage, Supplier
        self.client.force_login(User.objects.create_superuser("rs", password="x"))
        gts = Supplier.objects.create(name="GTS Distribution")
        panini = Supplier.objects.create(name="Panini America")
        p = Product.objects.create(sku="PRISM-BB", name="SV Prismatic Evolutions Booster Bundle",
                                   upc="196214112541", default_price=Decimal("50"))
        ProductBarcode.objects.create(product=p, code="196214112544", note="2nd print")
        self.r1 = Receipt.objects.create(supplier=gts, reference="PO-77")
        ReceiptLine.objects.create(receipt=self.r1, product=p, qty=6, unit_cost=Decimal("1824.31"))
        ReceiptPackage.objects.create(receipt=self.r1, tracking_number="1Z999AA10123456784", carrier="ups")
        self.r2 = Receipt.objects.create(supplier=panini, reference="PO-78")

    def _ids(self, q):
        html = self.client.get(f"/inventory/receiving/?q={q}").content.decode()
        return [n for n in (self.r1.pk, self.r2.pk) if f"RCV-{n}<" in html or f"RCV-{n}</a>" in html]

    def test_search_by_product_supplier_barcode_tracking(self):
        self.assertEqual(self._ids("PRISM"), [self.r1.pk])
        self.assertEqual(self._ids("Prismatic"), [self.r1.pk])
        self.assertEqual(self._ids("196214112544"), [self.r1.pk])     # extra barcode
        self.assertEqual(self._ids("Panini"), [self.r2.pk])
        self.assertEqual(self._ids("1Z999AA10123456784"), [self.r1.pk])  # scanned label
        self.assertEqual(self._ids("PO-78"), [self.r2.pk])
        self.assertEqual(self._ids(f"RCV-{self.r2.pk}"), [self.r2.pk])
        self.assertEqual(self._ids("zzz"), [])


class ProductBarcodeTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Product
        self.client.force_login(User.objects.create_superuser("bc", password="x"))
        self.p = Product.objects.create(sku="BUNDLE-1", name="Bundle", upc="111111111111",
                                        default_price=Decimal("50"))

    def test_form_adds_and_removes_codes(self):
        from inventory.models import Product
        data = {"sku": "BUNDLE-1", "name": "Bundle", "upc": "111111111111", "extra_barcodes": "222222222222\n333333333333",
                "case_pack_qty": 1, "unit_of_measure": "each", "avg_cost": "0", "default_price": "50",
                "reorder_point": 0, "active": "on"}
        r = self.client.post(f"/inventory/{self.p.pk}/edit/", data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        self.assertEqual(sorted(b.code for b in self.p.barcodes.all()), ["222222222222", "333333333333"])
        self.assertEqual(Product.find_by_code("333333333333"), self.p)
        self.assertEqual(Product.find_by_code("111111111111"), self.p)
        self.assertEqual(Product.find_by_code("bundle-1"), self.p)
        data["extra_barcodes"] = "222222222222"
        self.client.post(f"/inventory/{self.p.pk}/edit/", data)
        self.assertEqual([b.code for b in self.p.barcodes.all()], ["222222222222"])

    def test_code_cannot_belong_to_two_products(self):
        from inventory.models import Product, ProductBarcode
        other = Product.objects.create(sku="OTHER", name="Other", default_price=Decimal("1"))
        ProductBarcode.objects.create(product=other, code="999999999999")
        data = {"sku": "BUNDLE-1", "name": "Bundle", "upc": "111111111111", "extra_barcodes": "999999999999",
                "case_pack_qty": 1, "unit_of_measure": "each", "avg_cost": "0", "default_price": "50",
                "reorder_point": 0, "active": "on"}
        r = self.client.post(f"/inventory/{self.p.pk}/edit/", data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already belongs to OTHER")

    def test_scan_add_and_search_use_extra_codes(self):
        from inventory.models import ProductBarcode
        from orders.tests import make_customer
        from orders.models import PurchaseOrder
        ProductBarcode.objects.create(product=self.p, code="444444444444")
        self.p.apply_movement(movement_type="receipt", qty_delta=5, unit_cost=Decimal("4"), new_avg_cost=Decimal("4"))
        po = PurchaseOrder.objects.create(customer=make_customer("Scan Co"))
        self.client.post(f"/orders/{po.pk}/", {"action": "scan_add", "barcode": "444444444444"})
        self.assertEqual(po.live_lines.count(), 1)
        self.assertContains(self.client.get("/inventory/?q=444444444444"), "BUNDLE-1")
        self.assertContains(self.client.get("/search/?q=444444444444"), "BUNDLE-1")
        html = self.client.get(f"/orders/{po.pk}/").content.decode()
        self.assertIn('data-upc="BUNDLE-1 111111111111 444444444444"', html)


class IncomingStockTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from datetime import date
        from inventory.models import Product, Receipt, ReceiptLine, ReceiptPackage, Supplier
        self.client.force_login(User.objects.create_superuser("inc", password="x"))
        self.p = Product.objects.create(sku="INC-1", name="Prismatic Bundle", default_price=Decimal("50"))
        gts = Supplier.objects.create(name="GTS")
        r1 = Receipt.objects.create(supplier=gts, date=date(2026, 11, 1), expected_date=date(2026, 11, 20))
        ReceiptLine.objects.create(receipt=r1, product=self.p, qty=6, unit_cost=Decimal("1824.3125"))
        r2 = Receipt.objects.create(supplier=gts, date=date(2026, 11, 2), expected_date=date(2026, 11, 10))
        ReceiptPackage.objects.create(receipt=r2, tracking_number="1Z999AA10123456784", carrier="ups",
                                      status="TRANSIT", eta=date(2026, 11, 9))
        ReceiptLine.objects.create(receipt=r2, product=self.p, qty=4, unit_cost=Decimal("1800.00"))
        posted = Receipt.objects.create(supplier=gts)                 # already received: not incoming
        ReceiptLine.objects.create(receipt=posted, product=self.p, qty=2, unit_cost=Decimal("1700"))
        posted.post()

    def test_summary_counts_only_open_orders(self):
        inc = self.p.incoming_summary
        self.assertEqual(inc["qty"], 10)
        self.assertEqual(inc["cost"], Decimal("18145.88"))
        self.assertEqual(inc["avg_cost"], Decimal("1814.5880"))
        self.assertEqual(inc["orders"], 2)
        self.assertEqual(str(inc["next_eta"]), "2026-11-09")

    def test_detail_and_list_show_incoming(self):
        html = self.client.get(f"/inventory/{self.p.pk}/").content.decode()
        self.assertIn('id="incoming"', html)
        self.assertIn("18,145.88", html)
        self.assertIn("1824.3125", html)
        self.assertIn("In transit", html)
        lst = self.client.get("/inventory/?q=INC-1").content.decode()
        self.assertIn(">10</a>", lst)


class ReceiveOverridesTrackingTests(TestCase):
    def test_marking_received_sets_expected_today_and_delivered(self):
        from datetime import date
        from django.utils import timezone
        from inventory.models import Product, Receipt, ReceiptLine, ReceiptPackage
        p = Product.objects.create(sku="RCV-OVR", name="Box", default_price=Decimal("10"))
        r = Receipt.objects.create(expected_date=date(2030, 1, 1))
        ReceiptPackage.objects.create(receipt=r, tracking_number="A", status="TRANSIT", eta=date(2030, 1, 1))
        ReceiptPackage.objects.create(receipt=r, tracking_number="B", status="UNKNOWN")
        ReceiptLine.objects.create(receipt=r, product=p, qty=1, unit_cost=Decimal("5"))
        r.post()
        r.refresh_from_db()
        self.assertEqual(r.expected_date, timezone.localdate())
        self.assertEqual({pk.status for pk in r.packages.all()}, {"DELIVERED"})
        self.assertEqual(r.tracking_badge[0], "Delivered (2 boxes)")
