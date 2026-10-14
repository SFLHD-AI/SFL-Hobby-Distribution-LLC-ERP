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
