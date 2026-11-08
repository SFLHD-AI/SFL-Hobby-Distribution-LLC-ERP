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


class MarketPriceTests(TestCase):
    class FakeProvider:
        name = "fake"
        def __init__(self, prices): self.prices, self.calls = prices, []
        def is_configured(self): return True
        def fetch(self, pid):
            from inventory.pricing import PricingError
            self.calls.append(pid)
            if pid not in self.prices:
                raise PricingError("TCGplayer product id not found.")
            m = self.prices[pid]
            return {"market": m, "low": m}

    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Category, Product
        self.client.force_login(User.objects.create_superuser("mkt", password="x"))
        sealed = Category.objects.create(name="Sealed TCG Product")
        other = Category.objects.create(name="Supplies")
        self.p = Product.objects.create(sku="PRISM-ETB", name="Prismatic ETB", category=sealed,
                                        default_price=Decimal("120"), tcgplayer_id=593391)
        self.p.apply_movement(movement_type="receipt", qty_delta=10, unit_cost=Decimal("62.50"), new_avg_cost=Decimal("62.50"))
        self.other = Product.objects.create(sku="SLEEVES", name="Sleeves", category=other,
                                            default_price=Decimal("5"), tcgplayer_id=1)

    def test_parse_id_from_url_or_number(self):
        from inventory.pricing import parse_tcgplayer_id
        self.assertEqual(parse_tcgplayer_id("https://www.tcgplayer.com/product/593391/pokemon-sv-prismatic-evolutions-etb?Language=English"), 593391)
        self.assertEqual(parse_tcgplayer_id("593391"), 593391)
        self.assertIsNone(parse_tcgplayer_id("not a link"))

    def test_refresh_only_sealed_category_and_math(self):
        from inventory.pricing import refresh_all
        fake = self.FakeProvider({593391: Decimal("100.00"), 1: Decimal("9.99")})
        self.assertIn("1 updated", refresh_all(fake))
        self.assertEqual(fake.calls, [593391])                 # Supplies never fetched
        self.p.refresh_from_db()
        self.assertEqual(self.p.market_price, Decimal("100.00"))
        self.assertEqual(self.p.cost_pct_of_market, Decimal("62.5"))
        self.assertEqual(self.p.market_margin_per_unit, Decimal("37.50"))
        self.assertEqual(self.p.market_badge[1], "bg-emerald-100 text-emerald-700")
        self.other.refresh_from_db(); self.assertIsNone(self.other.market_price)

    def test_error_is_stored_not_raised(self):
        from inventory.pricing import refresh_product
        ok, msg = refresh_product(self.p, self.FakeProvider({}))
        self.assertFalse(ok)
        self.p.refresh_from_db()
        self.assertIn("not found", self.p.market_error)

    def test_pages_show_market(self):
        from unittest.mock import patch
        fake = self.FakeProvider({593391: Decimal("100.00")})
        with patch("inventory.pricing.get_provider", return_value=fake):
            self.client.post(f"/inventory/{self.p.pk}/", {"action": "refresh_market"})
        detail = self.client.get(f"/inventory/{self.p.pk}/").content.decode()
        self.assertIn("TCGplayer market", detail)
        self.assertIn("cost = 62.5% of mkt", detail)
        lst = self.client.get("/inventory/").content.decode()
        self.assertIn("62.5%", lst)
        rep = self.client.get("/reports/market-check/").content.decode()
        self.assertIn("PRISM-ETB", rep)
        self.assertIn("$1,000.00", rep)                       # 10 on hand at market
        csv = self.client.get("/reports/market-check/?export=csv").content.decode()
        self.assertIn("62.5", csv)

    def test_product_form_accepts_url_and_refreshes(self):
        from unittest.mock import patch
        fake = self.FakeProvider({777: Decimal("50.00")})
        data = {"sku": "PRISM-ETB", "name": "Prismatic ETB", "category": self.p.category_id, "upc": "",
                "tcgplayer": "https://www.tcgplayer.com/product/777/x", "extra_barcodes": "",
                "case_pack_qty": 1, "unit_of_measure": "each", "avg_cost": "62.5", "default_price": "120",
                "reorder_point": 0, "active": "on"}
        with patch("inventory.pricing.get_provider", return_value=fake):
            r = self.client.post(f"/inventory/{self.p.pk}/edit/", data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        self.p.refresh_from_db()
        self.assertEqual(self.p.tcgplayer_id, 777)
        self.assertEqual(self.p.market_price, Decimal("50.00"))


class ReceivingListExpandSortTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Receipt, ReceiptLine, Supplier
        self.client.force_login(User.objects.create_superuser("rcvlist", password="x"))
        a = Supplier.objects.create(name="Alpha Cards")
        z = Supplier.objects.create(name="Zeta Hobby")
        p1 = make_product("RL-1")
        p2 = make_product("RL-2")
        self.r1 = Receipt.objects.create(supplier=z, reference="B")
        ReceiptLine.objects.create(receipt=self.r1, product=p1, qty=3, unit_cost=Decimal("10.00"))
        self.r2 = Receipt.objects.create(supplier=a, reference="A")
        ReceiptLine.objects.create(receipt=self.r2, product=p2, qty=5, unit_cost=Decimal("100.00"))

    def test_rows_expand_to_show_lines(self):
        html = self.client.get("/inventory/receiving/").content.decode()
        self.assertIn('id="expandAll"', html)
        self.assertIn(f'id="rcv-lines-{self.r1.pk}"', html)
        self.assertIn("RL-1", html)
        self.assertIn("$10.00", html)
        self.assertIn("$30.00", html)        # line total 3 x 10
        self.assertIn("$500.00", html)       # line total 5 x 100

    def _order(self, qs):
        html = self.client.get("/inventory/receiving/" + qs).content.decode()
        return html.index(f"RCV-{self.r1.pk}") < html.index(f"RCV-{self.r2.pk}")

    def test_sort_columns(self):
        self.assertFalse(self._order(""))                      # newest (r2) first by default
        self.assertTrue(self._order("?sort=id&dir=asc"))
        self.assertFalse(self._order("?sort=supplier&dir=asc"))   # Alpha before Zeta
        self.assertTrue(self._order("?sort=supplier&dir=desc"))
        self.assertTrue(self._order("?sort=merch&dir=asc"))       # $30 before $500
        self.assertFalse(self._order("?sort=merch&dir=desc"))
        self.assertFalse(self._order("?sort=reference&dir=asc"))  # A before B
        self.assertFalse(self._order("?sort=bogus"))              # falls back to newest first

    def test_sort_keeps_search_and_filter(self):
        html = self.client.get("/inventory/receiving/?q=RL&status=open").content.decode()
        self.assertIn("q=RL", html)
        self.assertIn("status=open", html)
        self.assertIn("sort=merch", html)


class OrderImportTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser("imp", password="x"))
        self.p = make_product("TOPPS-1", name="Topps Chrome Hobby Box", upc="887521161300")

    def test_parser_csv_with_headers(self):
        from inventory.order_import import parse_text
        lines = parse_text("UPC,Description,Qty,Unit Price,Total\n"
                           "887521161300,Topps Chrome Hobby Box,4,$1288.76,$5155.04\n"
                           "810199502014,OP-17 Double Pack,13,,481.52\n")
        self.assertEqual([(l.code, l.qty, l.cost) for l in lines],
                         [("887521161300", 4, Decimal("1288.76")), ("810199502014", 13, Decimal("37.04"))])

    def test_parser_freeform_lines(self):
        from inventory.order_import import parse_text
        lines = parse_text("Order #555\n"
                           "887521161300  2025/26 Topps Chrome Hobby Box   4   $1,288.76   $5,155.04\n"
                           "SV Prismatic Bundle - Sealed Case x1 $1,737.37\n"
                           "Subtotal $6,892.41")
        self.assertEqual(len(lines), 2)
        self.assertEqual((lines[0].code, lines[0].qty, lines[0].cost), ("887521161300", 4, Decimal("1288.76")))
        self.assertEqual((lines[1].code, lines[1].name, lines[1].qty, lines[1].cost),
                         ("", "SV Prismatic Bundle - Sealed Case", 1, Decimal("1737.37")))

    def test_preview_matches_products_by_upc(self):
        r = self.client.post("/inventory/receiving/import/", {
            "action": "preview", "paste": "887521161300  Topps Chrome Hobby Box  4  $1,288.76\n"
                                          "000000000001  Mystery Item  2  $5.00"})
        html = r.content.decode()
        self.assertIn("matched", html)
        self.assertIn("TOPPS-1", html)
        self.assertIn("create it as a new product", html)

    def test_create_builds_receipt_and_new_product(self):
        from inventory.models import Receipt, Supplier
        r = self.client.post("/inventory/receiving/import/", {
            "action": "create", "new_supplier": "Tradepost", "reference": "TP-1001", "n": "3",
            "code_0": "887521161300", "name_0": "Topps", "product_0": str(self.p.pk), "qty_0": "4", "cost_0": "1288.76",
            "code_1": "000000000001", "name_1": "Mystery Item", "product_1": "", "create_1": "1", "qty_1": "2", "cost_1": "5.00",
            "code_2": "", "name_2": "Skipped", "product_2": "", "qty_2": "1", "cost_2": "1", "skip_2": "1",
        })
        self.assertEqual(r.status_code, 302)
        rcv = Receipt.objects.get(reference="TP-1001")
        self.assertEqual(rcv.supplier, Supplier.objects.get(name="Tradepost"))
        self.assertEqual(rcv.lines.count(), 2)
        self.assertFalse(rcv.posted)
        self.assertEqual(rcv.merchandise_total, Decimal("5165.04"))
        new = Product.objects.get(sku="000000000001")
        self.assertEqual(new.upc, "000000000001")
        self.assertEqual(new.name, "Mystery Item")

    def test_create_refuses_unmatched_line_without_create_flag(self):
        from inventory.models import Receipt
        r = self.client.post("/inventory/receiving/import/", {
            "action": "create", "n": "1", "code_0": "999", "name_0": "X", "product_0": "", "qty_0": "1", "cost_0": "1",
            "text": "999 X 1 $1",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Receipt.objects.count(), 0)


class TradepostImportTests(TestCase):
    """Drives the Tradepost page with a fake API client and fake Shippo (no network)."""

    LABEL = {"id": "lb-1", "trackingCode": "1Z999AA10123456784", "carrier": "UPSDAP",
             "currentStatus": "IN_TRANSIT", "estimatedDeliveryDate": "2026-11-09T00:00:00Z", "updatedAt": "x"}
    LABEL2 = {"id": "lb-2", "trackingCode": "9400111899223033005555", "carrier": "USPS",
              "currentStatus": "DELIVERED", "estimatedDeliveryDate": None, "updatedAt": "x"}
    SAMPLE = [
        {"id": "aaaa-1", "orderNumber": "TPST-AAAA0001", "takerSide": "BUY", "quantity": 2,
         "executionPriceCents": 173737, "grossAmountCents": 347474, "status": "SETTLED",
         "executedAt": "2026-11-01T15:00:00Z", "settledAt": "2026-11-01T15:01:00Z",
         "assetConfigurationId": "cfg-1",
         "assetConfiguration": {"id": "cfg-1", "name": "Prismatic Evolutions Booster Bundle",
                                "externalId": {"TCGPLAYER": "593285"}, "slug": "pe-bundle", "data": {"brand": "Pokemon"}},
         "asset": {"name": "Scarlet & Violet: Prismatic Evolutions"}, "key": {"name": "MINT"},
         "pnl": {"boughtQuantity": 2, "buyUnitPriceCents": "176000", "buyFeesCents": "4526"},
         "transfers": [{"id": "tr-1", "status": "SHIPPED", "shipmentGroup": {"id": "sg-1", "shippingLabels": [LABEL, LABEL2]}}]},
        {"id": "aaaa-2", "orderNumber": "TPST-AAAA0002", "takerSide": "BUY", "quantity": 13,
         "executionPriceCents": 3704, "grossAmountCents": 48152, "status": "SETTLING",
         "executedAt": "2026-11-03T15:00:00Z", "settledAt": None, "assetConfigurationId": "cfg-2",
         "assetConfiguration": {"id": "cfg-2", "name": "OP-17 Double Pack", "externalId": None},
         "asset": {"name": "One Piece: OP-17"}, "key": {"name": "MINT"}, "pnl": None, "transfers": []},
        {"id": "aaaa-3", "orderNumber": "TPST-AAAA0003", "takerSide": "BUY", "quantity": 1,
         "executionPriceCents": 100, "grossAmountCents": 100, "status": "REVERSED",
         "executedAt": "2026-11-04T15:00:00Z", "assetConfigurationId": "cfg-3",
         "assetConfiguration": {"id": "cfg-3", "name": "Refunded thing"}, "asset": None, "key": None},
    ]

    def setUp(self):
        from unittest import mock
        from django.contrib.auth.models import User
        from core.models import CompanySettings
        from inventory import tradepost as tp
        self.client.force_login(User.objects.create_superuser("tp", password="x"))
        c = CompanySettings.get(); c.tradepost_api_key = "tp_live_test"; c.save()
        self.existing = make_product("PE-BUNDLE", name="Prismatic Evolutions Booster Bundle - Sealed Case",
                                     tcgplayer_id=593285)
        parse = tp.TradepostClient.parse_fill
        self.real_list_buys = tp.TradepostClient.list_buys
        patcher = mock.patch.object(tp.TradepostClient, "list_buys",
                                    lambda self, since=None, **kw: [parse(r) for r in TradepostImportTests.SAMPLE])
        patcher.start(); self.addCleanup(patcher.stop)
        # fake Shippo: configured, answers every tracking lookup
        self.shippo = mock.Mock(); self.shippo.is_configured.return_value = True
        p2 = mock.patch("shipping.providers.get_provider", return_value=self.shippo)
        p2.start(); self.addCleanup(p2.stop)
        self.lookups = []
        def fetch(carrier, number, provider=None):
            self.lookups.append((carrier, number))
            from datetime import date, datetime, timezone as tz
            if number.startswith("1Z"):
                return {"status": "TRANSIT", "detail": "Departed facility", "location": "Dallas, TX",
                        "event_at": datetime(2026, 11, 7, tzinfo=tz.utc), "eta": date(2026, 11, 10)}
            return {"status": "DELIVERED", "detail": "Delivered, front door", "location": "Miami, FL",
                    "event_at": datetime(2026, 11, 5, tzinfo=tz.utc), "eta": None}
        p3 = mock.patch("inventory.tracking.fetch_status", side_effect=fetch)
        p3.start(); self.addCleanup(p3.stop)

    def test_tcgplayer_id_shapes(self):
        from inventory.tradepost import _tcgplayer_id
        self.assertEqual(_tcgplayer_id({"TCGPLAYER": "285229"}), 285229)
        self.assertEqual(_tcgplayer_id('{"TCG_PLAYER_ID": "477236"}'), 477236)
        self.assertEqual(_tcgplayer_id("666891"), 666891)
        self.assertEqual(_tcgplayer_id(None, {}, {"brand": "Pokemon"}), None)
        self.assertEqual(_tcgplayer_id(None, {"TCGPLAYER": ["12", "13"]}), 12)

    def test_parse_fill_prices_ids_and_labels(self):
        from inventory.tradepost import TradepostClient
        f = TradepostClient.parse_fill(self.SAMPLE[0])
        self.assertEqual(f.unit_cost, Decimal("1760.0000"))
        self.assertEqual(f.tcgplayer_id, 593285)
        self.assertEqual(f.line_total, Decimal("3520.00"))
        self.assertEqual([(l["tracking"], l["carrier"], l["tp_status"], l["tp_eta"]) for l in f.labels],
                         [("1Z999AA10123456784", "ups", "IN_TRANSIT", "2026-11-09"),
                          ("9400111899223033005555", "usps", "DELIVERED", "")])
        g = TradepostClient.parse_fill(self.SAMPLE[1])
        self.assertEqual(g.unit_cost, Decimal("37.0400"))      # no pnl → cross price
        self.assertIsNone(g.tcgplayer_id)
        self.assertEqual(g.labels, [])

    def test_disputed_fill_uses_original_price(self):
        import copy
        from inventory.tradepost import TradepostClient
        row = copy.deepcopy(self.SAMPLE[0])
        row["status"] = "IN_DISPUTE"
        row["pnl"] = {"boughtQuantity": 1, "buyUnitPriceCents": "88000", "buyFeesCents": "0"}   # dispute moved it
        row["disputes"] = [{"id": "d1", "status": "OPEN", "reason": "Box arrived crushed", "createdAt": "x", "resolvedAt": None}]
        f = TradepostClient.parse_fill(row)
        self.assertTrue(f.disputed)
        self.assertEqual(f.quantity, 2)                              # what you bought, not the outcome qty
        self.assertEqual(f.unit_cost, Decimal("1737.3700"))          # the cross price you originally paid
        self.assertIn("Box arrived crushed", f.dispute_note)
        # a settled fill with a resolved dispute goes back to the fees-in price
        row["status"] = "SETTLED"
        row["disputes"][0].update(status="RESOLVED", resolvedAt="2026-11-05T00:00:00Z")
        g = TradepostClient.parse_fill(row)
        self.assertFalse(g.disputed)
        self.assertEqual(g.unit_cost, Decimal("880.0000"))
        # and an open dispute on a SETTLING fill still counts as disputed
        row["status"] = "SETTLING"
        row["disputes"][0].update(status="OPEN", resolvedAt=None)
        self.assertTrue(TradepostClient.parse_fill(row).disputed)

    def test_disputed_fill_logs_rcv_at_original_price(self):
        import copy
        from unittest import mock
        from inventory import tradepost as tp
        from inventory.models import Receipt
        rows = copy.deepcopy(self.SAMPLE)
        rows[0]["status"] = "IN_DISPUTE"
        rows[0]["pnl"] = {"boughtQuantity": 1, "buyUnitPriceCents": "88000", "buyFeesCents": "0"}
        rows[0]["disputes"] = [{"id": "d1", "status": "OPEN", "reason": "crushed", "createdAt": "x", "resolvedAt": None}]
        parse = tp.TradepostClient.parse_fill
        with mock.patch.object(tp.TradepostClient, "list_buys", lambda self, since=None, **kw: [parse(r) for r in rows]):
            html = self.client.get("/inventory/receiving/tradepost/").content.decode()
            self.assertIn("original price", html)
            self.client.post("/inventory/receiving/tradepost/", {"action": "create", "fill": ["aaaa-1"]})
        line = Receipt.objects.get().lines.get()
        self.assertEqual((line.qty, line.unit_cost), (2, Decimal("1737.3700")))

    def test_first_visit_syncs_and_tracks(self):
        from inventory.models import TradepostFill
        html = self.client.get("/inventory/receiving/tradepost/").content.decode()
        self.assertIn("TPST-AAAA0001", html)
        self.assertIn("PE-BUNDLE", html)                      # matched by TCGplayer id
        self.assertIn("Not in your inventory yet", html)      # OP-17 has no match yet
        self.assertNotIn("TPST-AAAA0003", html)               # reversed fills are not purchases
        self.assertIn("1 of 2 delivered", html)               # Shippo said one box landed
        self.assertIn("ETA Tue Nov 10", html)
        self.assertIn("Departed facility", html)
        fill = TradepostFill.objects.get(execution_id="aaaa-1")
        self.assertEqual(fill.packages[0]["status"], "TRANSIT")
        self.assertEqual(fill.packages[1]["status"], "DELIVERED")
        # second visit within 30 minutes: served from the cache, delivered box not re-queried
        n = len(self.lookups)
        self.client.get("/inventory/receiving/tradepost/")
        self.assertEqual(len(self.lookups), n)

    def test_matches_orders_already_logged_by_invoice_number(self):
        from inventory.models import Receipt, Supplier, TradepostFill
        sup = Supplier.objects.create(name="Tradepost")
        rcv = Receipt.objects.create(supplier=sup, supplier_invoice_number="TPST-AAAA0001", reference="hand-logged")
        self.client.get("/inventory/receiving/tradepost/")
        fill = TradepostFill.objects.get(execution_id="aaaa-1")
        self.assertEqual(fill.receipt, rcv)
        # its Tradepost tracking numbers now live on the order too
        self.assertEqual(sorted(rcv.tracking_numbers), ["1Z999AA10123456784", "9400111899223033005555"])
        pkg = rcv.packages.get(tracking_number="1Z999AA10123456784")
        self.assertEqual((pkg.carrier, pkg.status, str(pkg.eta)), ("ups", "TRANSIT", "2026-11-10"))
        rcv.refresh_from_db()
        self.assertEqual(str(rcv.expected_date), "2026-11-10")
        html = self.client.get("/inventory/receiving/tradepost/?view=new").content.decode()
        self.assertNotIn("TPST-AAAA0001", html)
        self.assertIn("TPST-AAAA0002", html)

    def test_unmatched_fill_is_never_auto_created(self):
        from inventory.models import Receipt, TradepostFill
        self.client.get("/inventory/receiving/tradepost/")
        html = self.client.get("/inventory/receiving/tradepost/").content.decode()
        self.assertIn("Not in your inventory yet", html)
        self.assertIn(f"?tradepost=aaaa-2", html)
        r = self.client.post("/inventory/receiving/tradepost/", {"action": "create", "fill": ["aaaa-2"]}, follow=True)
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 1)              # nothing created behind your back
        self.assertIn("Match these to a product first", r.content.decode())
        # match it to a product by hand → remembered on the product and logs fine
        op17 = make_product("OP17-DP", name="One Piece OP-17 Double Pack")
        self.client.post("/inventory/receiving/tradepost/", {"action": "match", "fill_id": "aaaa-2", "product": op17.pk})
        fill = TradepostFill.objects.get(execution_id="aaaa-2")
        self.assertEqual(fill.matched_product, op17)
        op17.refresh_from_db()
        self.assertEqual(op17.tradepost_config_id, "cfg-2")
        self.client.post("/inventory/receiving/tradepost/", {"action": "create", "fill": ["aaaa-2"]})
        self.assertEqual(Receipt.objects.get().lines.get().product, op17)

    def test_create_product_from_fill_prefills_and_matches(self):
        from inventory.models import TradepostFill
        self.client.get("/inventory/receiving/tradepost/")
        html = self.client.get("/inventory/new/?tradepost=aaaa-2").content.decode()
        self.assertIn("One Piece: OP-17 — OP-17 Double Pack", html)
        self.assertIn('name="tradepost_fill" value="aaaa-2"', html)
        r = self.client.post("/inventory/new/?tradepost=aaaa-2", {
            "sku": "OP17-DP", "name": "OP-17 Double Pack", "default_price": "50.00", "unit_of_measure": "each",
            "case_pack_qty": "1", "reorder_point": "0", "tradepost_fill": "aaaa-2"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].endswith("/inventory/receiving/tradepost/"))
        p = Product.objects.get(sku="OP17-DP")
        self.assertEqual(TradepostFill.objects.get(execution_id="aaaa-2").matched_product, p)
        self.assertEqual(p.tradepost_config_id, "cfg-2")

    def test_create_receipt_from_selected_fills(self):
        from inventory.models import Receipt, TradepostFill
        self.op17 = make_product("OP17-DP", name="OP-17 Double Pack")      # exact-name match
        self.client.get("/inventory/receiving/tradepost/")
        r = self.client.post("/inventory/receiving/tradepost/", {"action": "create", "fill": ["aaaa-1", "aaaa-2"]})
        self.assertEqual(r.status_code, 302)
        rcv = Receipt.objects.get()
        self.assertEqual(rcv.supplier.name, "Tradepost")
        self.assertEqual(rcv.supplier_invoice_number, "TPST-AAAA0001, TPST-AAAA0002")
        self.assertEqual(rcv.reference, "TPST-AAAA0001, TPST-AAAA0002")
        lines = {l.tradepost_execution_id: l for l in rcv.lines.all()}
        self.assertEqual(lines["aaaa-1"].product, self.existing)
        self.assertEqual(lines["aaaa-1"].unit_cost, Decimal("1760.0000"))
        self.assertEqual(lines["aaaa-2"].qty, 13)
        self.assertEqual(lines["aaaa-2"].product, self.op17)
        self.op17.refresh_from_db()
        self.assertEqual(self.op17.tradepost_config_id, "cfg-2")
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.tradepost_config_id, "cfg-1")
        self.assertEqual(rcv.packages.count(), 2)
        self.assertTrue(all(f.receipt == rcv for f in TradepostFill.objects.filter(execution_id__in=["aaaa-1", "aaaa-2"])))
        # can't be logged twice
        self.client.post("/inventory/receiving/tradepost/", {"action": "create", "fill": ["aaaa-1"]})
        self.assertEqual(Receipt.objects.count(), 1)

    def test_log_all_new(self):
        from inventory.models import Receipt
        make_product("OP17-DP", name="OP-17 Double Pack")
        self.client.get("/inventory/receiving/tradepost/")
        self.client.post("/inventory/receiving/tradepost/", {"action": "create", "all_new": "1"})
        self.assertEqual(Receipt.objects.get().lines.count(), 2)

    def test_close_reopen_and_manual_link(self):
        from inventory.models import Receipt, Supplier, TradepostFill
        make_product("OP17-DP", name="OP-17 Double Pack")
        self.client.get("/inventory/receiving/tradepost/")
        self.client.post("/inventory/receiving/tradepost/", {"action": "close", "fill": ["aaaa-2"]})
        self.assertTrue(TradepostFill.objects.get(execution_id="aaaa-2").closed)
        html = self.client.get("/inventory/receiving/tradepost/").content.decode()     # Open tab by default
        self.assertNotIn("TPST-AAAA0002", html)
        html = self.client.get("/inventory/receiving/tradepost/?view=closed").content.decode()
        self.assertIn("TPST-AAAA0002", html)
        # "log all new" skips closed fills
        self.client.post("/inventory/receiving/tradepost/", {"action": "create", "all_new": "1"})
        rcv1 = Receipt.objects.get()
        self.assertEqual(rcv1.lines.count(), 1)
        self.client.post("/inventory/receiving/tradepost/", {"action": "reopen", "fill": ["aaaa-2"]})
        self.assertFalse(TradepostFill.objects.get(execution_id="aaaa-2").closed)
        # receiving the supplier order closes its Tradepost order automatically
        rcv1.post()
        self.assertTrue(TradepostFill.objects.get(execution_id="aaaa-1").closed)
        rcv = Receipt.objects.create(supplier=Supplier.objects.get(name="Tradepost"), reference="manual")
        self.client.post("/inventory/receiving/tradepost/", {"action": "link", "fill_id": "aaaa-2", "rcv": f"RCV-{rcv.pk}"})
        self.assertEqual(TradepostFill.objects.get(execution_id="aaaa-2").receipt, rcv)
        self.client.post("/inventory/receiving/tradepost/", {"action": "unlink", "fill": ["aaaa-2"]})
        self.assertIsNone(TradepostFill.objects.get(execution_id="aaaa-2").receipt)

    def test_hourly_task(self):
        from inventory.models import TradepostFill
        from inventory.tasks import sync_tradepost
        msg = sync_tradepost()
        self.assertIn("3 new", msg)
        self.assertIn("2 packages tracked", msg)
        self.assertEqual(TradepostFill.objects.count(), 3)

    def test_unconfigured_shows_setup_steps(self):
        from core.models import CompanySettings
        c = CompanySettings.get(); c.tradepost_api_key = ""; c.save()
        html = self.client.get("/inventory/receiving/tradepost/").content.decode()
        self.assertIn("API Keys", html)

    def test_client_surfaces_auth_error(self):
        from unittest import mock
        from inventory.tradepost import TradepostClient, TradepostError
        c = TradepostClient("tp_live_bad")
        fake = mock.Mock(status_code=401); fake.json.return_value = {"error": "Invalid API key", "code": "INVALID_API_KEY"}
        with mock.patch("inventory.tradepost.requests.post", return_value=fake):
            with self.assertRaises(TradepostError) as cm:
                c._query("query { x }", {})
        self.assertIn("API key", str(cm.exception))

    def test_client_retries_without_optional_fields(self):
        from unittest import mock
        from inventory.tradepost import TradepostClient
        c = TradepostClient("tp_live_ok")
        calls = []
        def post(url, json, headers, timeout):
            calls.append(json["query"])
            m = mock.Mock(status_code=200)
            if "externalId" in json["query"]:
                m.status_code = 400
                m.json.return_value = {"errors": [{"message": "Invalid request. Request ID: x", "code": "INVALID_REQUEST"}]}
            else:
                m.json.return_value = {"data": {"listOrderExecutions": {
                    "orderExecutions": [self.SAMPLE[1]], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            return m
        with mock.patch("inventory.tradepost.requests.post", side_effect=post):
            fills = self.real_list_buys(c)
        self.assertEqual(len(calls), 2)
        self.assertEqual(fills[0].order_number, "TPST-AAAA0002")


class ReceiptAdjustmentTests(TestCase):
    """Damaged write-offs and supplier returns on a received order."""

    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Receipt, ReceiptLine, Supplier
        self.user = User.objects.create_superuser("adj", password="x")
        self.client.force_login(self.user)
        self.p = make_product("BOX-1", name="Hobby Box")
        # opening stock: 10 @ $100 avg
        StockAdjustment.create_and_apply(product=self.p, qty_delta=10, reason="opening", unit_cost=Decimal("100"))
        sup = Supplier.objects.create(name="Tradepost")
        self.r = Receipt.objects.create(supplier=sup, reference="R1", freight_cost=Decimal("20"))
        # 10 more @ $120 + $2 freight each → landed 122; avg becomes 111
        self.line = ReceiptLine.objects.create(receipt=self.r, product=self.p, qty=10, unit_cost=Decimal("120"))
        self.r.post(user=self.user)
        self.p.refresh_from_db()
        self.assertEqual((self.p.qty_on_hand, self.p.avg_cost), (20, Decimal("111.0000")))

    def test_requires_received_order(self):
        from inventory.models import Receipt, ReceiptAdjustment, ReceiptLine
        r2 = Receipt.objects.create(reference="open")
        l2 = ReceiptLine.objects.create(receipt=r2, product=self.p, qty=1, unit_cost=Decimal("1"))
        with self.assertRaises(ValidationError):
            ReceiptAdjustment.create_and_apply(receipt=r2, line=l2, kind="damaged", qty=1)

    def test_damaged_write_off_is_a_loss_at_average_cost(self):
        from inventory.models import ReceiptAdjustment, StockMovement
        adj = ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="damaged", qty=2,
                                                 credit_amount=Decimal("50"), note="crushed", user=self.user)
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 18)
        self.assertEqual(self.p.avg_cost, Decimal("111.0000"))         # removals never move the average
        self.assertEqual(adj.unit_cost_removed, Decimal("111.0000"))
        self.assertEqual(adj.value_removed, Decimal("222.00"))
        self.assertEqual(adj.loss, Decimal("172.00"))                  # 222 out, 50 credit back
        self.assertEqual(adj.movement.movement_type, "write_off")
        self.assertTrue(self.r.posted)                                 # still received
        self.assertEqual(self.r.units_written_off, 2)
        self.assertEqual(self.r.credits_total, Decimal("50.00"))
        self.assertEqual(self.r.bill_total, Decimal("1170.00"))        # 1220 landed − 50
        self.assertEqual(self.r.balance_due, Decimal("1170.00"))

    def test_return_rolls_back_weighted_average(self):
        from inventory.models import ReceiptAdjustment
        adj = ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="return", qty=5,
                                                 credit_amount=Decimal("600"), user=self.user)
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 15)
        # (20×111 − 5×122) / 15 = (2220 − 610) / 15 = 107.3333
        self.assertEqual(self.p.avg_cost, Decimal("107.3333"))
        self.assertEqual(adj.unit_cost_removed, Decimal("122.0000"))   # landed cost it came in at
        self.assertEqual(adj.movement.movement_type, "supplier_return")
        self.assertEqual(adj.loss, Decimal("10.00"))                   # freight not refunded
        self.assertEqual(self.r.bill_total, Decimal("620.00"))

    def test_cannot_adjust_more_than_received_or_on_hand(self):
        from inventory.models import ReceiptAdjustment
        with self.assertRaises(ValidationError):
            ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="damaged", qty=11)
        ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="damaged", qty=6)
        with self.assertRaises(ValidationError):
            ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="return", qty=5)   # 4 left

    def test_undo_puts_units_back(self):
        from inventory.models import ReceiptAdjustment
        adj = ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="return", qty=5,
                                                 credit_amount=Decimal("600"), user=self.user)
        adj.undo(user=self.user)
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 20)
        self.assertEqual(self.p.avg_cost, Decimal("111.0000"))
        self.assertTrue(adj.reversed)
        self.assertEqual(self.r.credits_total, Decimal("0.00"))
        self.assertEqual(self.r.units_returned, 0)

    def test_credit_after_hand_payment_is_a_refund_due(self):
        from datetime import timedelta
        from inventory.models import ReceiptAdjustment
        self.r.paid_date = timezone.localdate() - timedelta(days=3)
        self.r.save()
        self.assertEqual(self.r.amount_paid, Decimal("1220.00"))
        ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="damaged", qty=1,
                                           credit_amount=Decimal("120"), user=self.user)
        self.assertEqual(self.r.credits_after_payment, Decimal("120.00"))
        self.assertEqual(self.r.amount_paid, Decimal("1220.00"))        # you really paid the full bill
        self.assertEqual(self.r.balance_due, Decimal("-120.00"))
        self.assertEqual(self.r.refund_due, Decimal("120.00"))
        self.assertTrue(self.r.is_paid)
        r = self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "refund_received"})
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db()
        self.assertEqual(self.r.refund_due, Decimal("0.00"))
        self.assertEqual(self.r.amount_paid, Decimal("1100.00"))

    def test_credit_before_hand_payment_reduces_what_you_pay(self):
        from inventory.models import ReceiptAdjustment
        ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="return", qty=2,
                                           credit_amount=Decimal("240"), user=self.user)
        self.client.post(f"/inventory/receiving/{self.r.pk}/", {"action": "mark_paid"})
        self.r.refresh_from_db()
        self.assertEqual(self.r.amount_paid, Decimal("980.00"))
        self.assertEqual(self.r.balance_due, Decimal("0.00"))
        self.assertEqual(self.r.refund_due, Decimal("0.00"))

    def test_detail_page_form_and_list(self):
        html = self.client.get(f"/inventory/receiving/{self.r.pk}/").content.decode()
        self.assertIn("Damaged or returned boxes", html)
        r = self.client.post(f"/inventory/receiving/{self.r.pk}/", {
            "action": "adjust", "line_id": self.line.pk, "kind": "damaged", "qty": "1", "credit": "30", "note": "dented"})
        self.assertEqual(r.status_code, 302)
        html = self.client.get(f"/inventory/receiving/{self.r.pk}/").content.decode()
        self.assertIn("1 written off", html)
        self.assertIn("dented", html)
        self.assertIn("Supplier credits", html)
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 19)

    def test_ledger_books_loss_and_credit(self):
        from datetime import timedelta
        from accounting.ledger import ACCRUAL, Journal
        from inventory.models import ReceiptAdjustment
        ReceiptAdjustment.create_and_apply(receipt=self.r, line=self.line, kind="damaged", qty=2,
                                           credit_amount=Decimal("50"), user=self.user)
        today = timezone.localdate()
        j = Journal(ACCRUAL, [today])
        tb = j.trial_balance(today)
        self.assertTrue(tb["balanced"])
        by_code = {r["account"].code: r for r in tb["rows"]}
        # inventory asset: 1000 opening + 1220 received − 222 written off
        self.assertEqual(by_code["1300"]["debit"] - by_code["1300"]["credit"], Decimal("1998.00"))
        # AP: owes 1220 less the 50 credit
        self.assertEqual(by_code["2000"]["credit"] - by_code["2000"]["debit"], Decimal("1170.00"))
        inc = j.income_statement(today - timedelta(days=30), today)
        adj_rows = [r for r in inc["cogs"] if r["account"].code == "5200"]
        self.assertEqual(sum(r["amount"] for r in adj_rows), Decimal("172.00"))


class SupplierPagesTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Supplier
        self.client.force_login(User.objects.create_superuser("sup", password="x"))
        self.a = Supplier.objects.create(name="Tradepost")
        self.b = Supplier.objects.create(name="Trade Post (dupe)")
        self.r = Receipt.objects.create(supplier=self.b, reference="x")

    def test_rename_supplier(self):
        html = self.client.get("/inventory/suppliers/").content.decode()
        self.assertIn("Trade Post (dupe)", html)
        r = self.client.post(f"/inventory/suppliers/{self.b.pk}/", {
            "name": "TX-FYE (DN)", "contact_name": "Dan", "email": "", "phone": "", "notes": ""})
        self.assertEqual(r.status_code, 302)
        self.b.refresh_from_db()
        self.assertEqual(self.b.name, "TX-FYE (DN)")
        self.r.refresh_from_db()
        self.assertEqual(str(self.r.supplier), "TX-FYE (DN)")

    def test_rename_to_existing_name_is_refused(self):
        r = self.client.post(f"/inventory/suppliers/{self.b.pk}/", {"name": "tradepost", "contact_name": "", "email": "", "phone": "", "notes": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("already a supplier", r.content.decode())

    def test_merge_moves_orders(self):
        from inventory.models import Supplier
        r = self.client.post(f"/inventory/suppliers/{self.b.pk}/", {"action": "merge", "into": self.a.pk})
        self.assertEqual(r.status_code, 302)
        self.r.refresh_from_db()
        self.assertEqual(self.r.supplier, self.a)
        self.assertFalse(Supplier.objects.filter(pk=self.b.pk).exists())        # retired (soft-deleted)
        self.assertTrue(Supplier.all_objects.filter(pk=self.b.pk).exists())

    def test_receipt_detail_links_to_supplier_and_back(self):
        self.r.supplier = self.a; self.r.save()
        from inventory.models import ReceiptLine
        p = make_product("S-1"); ReceiptLine.objects.create(receipt=self.r, product=p, qty=1, unit_cost=Decimal("1"))
        self.r.post()
        html = self.client.get(f"/inventory/receiving/{self.r.pk}/").content.decode()
        self.assertIn(f"/inventory/suppliers/{self.a.pk}/?next=", html)
        r = self.client.post(f"/inventory/suppliers/{self.a.pk}/", {"name": "Tradepost LLC", "contact_name": "", "email": "", "phone": "", "notes": "", "next": f"/inventory/receiving/{self.r.pk}/"})
        self.assertEqual(r["Location"], f"/inventory/receiving/{self.r.pk}/")


class CaseVsSinglesMarketTests(TestCase):
    """A case SKU with a second TCGplayer link for the single box inside."""
    FakeProvider = MarketPriceTests.FakeProvider

    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import Category, Product
        self.client.force_login(User.objects.create_superuser("mkt2", password="x"))
        sealed = Category.objects.create(name="Sealed TCG Product")
        self.p = Product.objects.create(sku="PRISM-ETB", name="Prismatic ETB", category=sealed,
                                        default_price=Decimal("120"), tcgplayer_id=593391)
        # case of 6 bundles, paid $1,500 landed; case page 10001, single bundle page 10002
        self.case = Product.objects.create(sku="PE-BUNDLE-CASE", name="Prismatic Bundle Case", category=sealed,
                                           default_price=Decimal("1825"), case_pack_qty=6,
                                           tcgplayer_id=10001, tcgplayer_unit_id=10002)
        self.case.apply_movement(movement_type="receipt", qty_delta=2, unit_cost=Decimal("1500.00"),
                                 new_avg_cost=Decimal("1500.00"))

    def test_unit_price_math(self):
        from inventory.pricing import refresh_all
        fake = self.FakeProvider({593391: Decimal("100.00"), 10001: Decimal("1700.00"), 10002: Decimal("320.00")})
        refresh_all(fake)
        self.assertEqual(sorted(fake.calls), [10001, 10002, 593391])
        self.case.refresh_from_db()
        self.assertEqual(self.case.market_price, Decimal("1700.00"))
        self.assertEqual(self.case.unit_market_price, Decimal("320.00"))
        self.assertEqual(self.case.case_market_from_units, Decimal("1920.00"))      # 320 × 6
        self.assertEqual(self.case.cost_pct_of_market, Decimal("88.2"))             # 1500 / 1700
        self.assertEqual(self.case.cost_pct_of_unit_market, Decimal("78.1"))        # 1500 / 1920
        self.assertEqual(self.case.unit_market_margin, Decimal("420.00"))
        self.assertEqual(self.case.unit_market_badge[0], "78.1% of singles")

    def test_unit_only_product_still_refreshes(self):
        from inventory.pricing import refresh_all
        self.case.tcgplayer_id = None; self.case.save()
        fake = self.FakeProvider({593391: Decimal("100.00"), 10002: Decimal("300.00")})
        self.assertIn("2 updated", refresh_all(fake))
        self.case.refresh_from_db()
        self.assertEqual(self.case.case_market_from_units, Decimal("1800.00"))
        self.assertIsNone(self.case.market_price)

    def test_pages_and_form(self):
        from unittest.mock import patch
        fake = self.FakeProvider({10001: Decimal("1700.00"), 10002: Decimal("320.00")})
        with patch("inventory.pricing.get_provider", return_value=fake):
            self.client.post(f"/inventory/{self.case.pk}/", {"action": "refresh_market"})
        detail = self.client.get(f"/inventory/{self.case.pk}/").content.decode()
        self.assertIn("Case at singles", detail)
        self.assertIn("$1,920.00", detail)
        self.assertIn("78.1% of singles", detail)
        rep = self.client.get("/reports/market-check/").content.decode()
        self.assertIn("Cost % of singles", rep)
        self.assertIn("78.1%", rep)
        csv = self.client.get("/reports/market-check/?export=csv").content.decode()
        self.assertIn("1920.00", csv)
        # the edit form accepts the single-unit link and clears prices when removed
        data = {"sku": "PE-BUNDLE-CASE", "name": "Prismatic Bundle Case", "category": self.case.category_id, "upc": "",
                "tcgplayer": "10001", "tcgplayer_unit": "", "extra_barcodes": "", "case_pack_qty": 6,
                "unit_of_measure": "each", "avg_cost": "1500", "default_price": "1825", "reorder_point": 0, "active": "on"}
        with patch("inventory.pricing.get_provider", return_value=fake):
            r = self.client.post(f"/inventory/{self.case.pk}/edit/", data)
        self.assertEqual(r.status_code, 302)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.tcgplayer_unit_id)
        self.assertIsNone(self.case.unit_market_price)
        data["tcgplayer_unit"] = "https://www.tcgplayer.com/product/10002/pokemon-bundle"
        with patch("inventory.pricing.get_provider", return_value=fake):
            self.client.post(f"/inventory/{self.case.pk}/edit/", data)
        self.case.refresh_from_db()
        self.assertEqual(self.case.tcgplayer_unit_id, 10002)
        self.assertEqual(self.case.unit_market_price, Decimal("320.00"))


class IncomingTileTests(TestCase):
    def test_tile_shows_incoming_average_and_blended_cost(self):
        from django.contrib.auth.models import User
        from inventory.models import Receipt, ReceiptLine
        self.client.force_login(User.objects.create_superuser("inc", password="x"))
        p = make_product("INC-1")
        StockAdjustment.create_and_apply(product=p, qty_delta=10, reason="opening", unit_cost=Decimal("100"))
        r = Receipt.objects.create(reference="open")
        ReceiptLine.objects.create(receipt=r, product=p, qty=10, unit_cost=Decimal("120"))
        summ = p.incoming_summary
        self.assertEqual(summ["avg_cost"], Decimal("120.0000"))
        self.assertEqual(summ["blended_avg"], Decimal("110.0000"))
        html = self.client.get(f"/inventory/{p.pk}/").content.decode()
        self.assertIn("avg $120.00", html)
        self.assertIn("$110.00 avg after arrival", html)
