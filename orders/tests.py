"""Order lifecycle: commitments, finalization decrement + cost snapshot,
backorders, and the picking API (over-pick block, wrong item, shorts)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from customers.models import Customer, ResaleCertificate, ShippingAddress
from inventory.models import Product
from orders.models import POLine, PurchaseOrder


def stocked_product(sku, qty=100, cost="5.00", **kw):
    p = Product.objects.create(sku=sku, name=f"Product {sku}",
                               default_price=Decimal("10.00"),
                               upc=f"UPC{sku}", **kw)
    p.apply_movement(movement_type="receipt", qty_delta=qty,
                     unit_cost=Decimal(cost), new_avg_cost=Decimal(cost))
    return p


def make_customer(name="Store", cert=True, policy="charge_tax"):
    c = Customer.objects.create(business_name=name, no_cert_policy=policy)
    ShippingAddress.objects.create(customer=c, label="Main", line1="1 Main St",
                                   city="Tampa", state="FL", zip_code="33610",
                                   is_default=True)
    if cert:
        ResaleCertificate.objects.create(
            customer=c, certificate_number="FL-1", issuing_state="FL",
            expiration_date=timezone.localdate() + timedelta(days=365))
    return c


class OrderLifecycleTests(TestCase):
    def setUp(self):
        self.p = stocked_product("SKU-1", qty=50, cost="4.00")
        self.c = make_customer()

    def _po(self, qty=10):
        po = PurchaseOrder.objects.create(customer=self.c,
                                          ship_to=self.c.default_shipping_address())
        POLine.objects.create(po=po, product=self.p, qty_ordered=qty,
                              unit_price=Decimal("10.00"))
        return po

    def test_confirm_commits_but_does_not_decrement(self):
        po = self._po(10)
        po.confirm()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 50)
        self.assertEqual(self.p.qty_committed, 10)
        self.assertEqual(self.p.qty_available, 40)

    def test_confirm_blocked_when_over_available(self):
        po = self._po(60)
        with self.assertRaises(ValidationError):
            po.confirm()
        self.assertEqual(po.status, PurchaseOrder.DRAFT)

    def test_finalize_decrements_on_hand_and_snapshots_cost(self):
        po = self._po(10)
        po.confirm()
        po.finalize()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 40)
        self.assertEqual(self.p.qty_committed, 0)
        line = po.live_lines.first()
        self.assertEqual(line.cost_snapshot, Decimal("4.0000"))
        # later cost changes must NOT rewrite history
        self.p.apply_movement(movement_type="receipt", qty_delta=10,
                              unit_cost=Decimal("9.00"), new_avg_cost=Decimal("9.00"))
        line.refresh_from_db()
        self.assertEqual(line.cost_snapshot, Decimal("4.0000"))
        self.assertEqual(line.line_profit, Decimal("60.00"))  # (10-4)*10

    def test_cancel_releases_commitment(self):
        po = self._po(10)
        po.confirm()
        po.cancel()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_committed, 0)
        self.assertEqual(self.p.qty_available, 50)

    def test_cert_block_policy_blocks_finalization(self):
        c2 = make_customer("NoCert Corp", cert=False, policy="block")
        po = PurchaseOrder.objects.create(customer=c2)
        POLine.objects.create(po=po, product=self.p, qty_ordered=1,
                              unit_price=Decimal("10.00"))
        po.confirm()
        with self.assertRaises(ValidationError):
            po.finalize()

    def test_backorder_split(self):
        po = self._po(10)
        po.confirm()
        po.start_picking()
        line = po.live_lines.first()
        line.qty_picked = 7
        line.short_reason = "oos"
        line.save()
        po.complete_pick()
        po.finalize()
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_on_hand, 43)  # only 7 shipped
        child = po.create_backorder()
        self.assertEqual(child.status, PurchaseOrder.DRAFT)
        child_line = child.live_lines.first()
        self.assertEqual(child_line.qty_ordered, 3)
        self.assertEqual(child_line.unit_price, Decimal("10.00"))
        self.assertEqual(child.parent, po)


class PickingApiTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name="Warehouse")
        self.user = User.objects.create_user("pk", password="x")
        self.user.groups.add(Group.objects.get(name="Warehouse"))
        self.client.force_login(self.user)
        self.p1 = stocked_product("PICK-1", qty=20)
        self.p2 = stocked_product("PICK-2", qty=20)
        self.c = make_customer("PickCo")
        self.po = PurchaseOrder.objects.create(customer=self.c)
        self.l1 = POLine.objects.create(po=self.po, product=self.p1, qty_ordered=2,
                                        unit_price=Decimal("10.00"))
        self.l2 = POLine.objects.create(po=self.po, product=self.p2, qty_ordered=1,
                                        unit_price=Decimal("10.00"))
        self.po.confirm()
        self.po.start_picking()

    def scan(self, code):
        return self.client.post(f"/api/pick/po/{self.po.pk}/scan/",
                                {"barcode": code}, content_type="application/json")

    def test_scan_increments(self):
        r = self.scan("UPCPICK-1")
        self.assertEqual(r.status_code, 200)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 1)

    def test_wrong_item_rejected(self):
        r = self.scan("NOT-ON-ORDER")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "wrong_item")

    def test_overpick_blocked(self):
        self.scan("UPCPICK-1")
        self.scan("UPCPICK-1")
        r = self.scan("UPCPICK-1")  # third scan on a 2-qty line
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "overpick")
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 2)

    def test_complete_requires_all_lines_picked_or_short(self):
        self.scan("UPCPICK-1")
        r = self.client.post(f"/api/pick/po/{self.po.pk}/complete/",
                             {}, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        # finish + short the rest
        self.scan("UPCPICK-1")
        self.client.post(f"/api/pick/line/{self.l2.pk}/short/",
                         {"reason": "oos"}, content_type="application/json")
        r = self.client.post(f"/api/pick/po/{self.po.pk}/complete/",
                             {}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.PICKED)

    def test_manual_adjust_and_negative_floor(self):
        r = self.client.post(f"/api/pick/line/{self.l1.pk}/adjust/",
                             {"delta": 1}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/pick/line/{self.l1.pk}/adjust/",
                             {"delta": -5}, content_type="application/json")
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 0)

    def test_office_user_cannot_pick(self):
        other = User.objects.create_user("outsider", password="x")
        self.client.force_login(other)
        r = self.scan("UPCPICK-1")
        # blocked: either a hard 403 or a redirect away from the pick API
        self.assertIn(r.status_code, (302, 403))
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 0)


class UnfinalizeTests(TestCase):
    def setUp(self):
        self.p = stocked_product("FIX-1", qty=50, cost="0.00")  # costs missing!
        self.c = make_customer("Mistake Co")
        self.po = PurchaseOrder.objects.create(customer=self.c)
        POLine.objects.create(po=self.po, product=self.p, qty_ordered=10,
                              unit_price=Decimal("20.00"))
        self.po.confirm()
        self.po.finalize()  # snapshot taken at wrong $0 cost

    def test_full_recovery_flow(self):
        from invoicing.models import Invoice
        inv = Invoice.build_from_pos([self.po])
        line = self.po.live_lines.first()
        self.assertEqual(line.cost_snapshot, Decimal("0.0000"))  # the mistake
        # 1. void the invoice -> PO released back to finalized
        inv.void()
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.FINALIZED)
        # 2. un-finalize -> stock returns, snapshot cleared
        self.po.unfinalize(reason="costs were missing")
        self.po.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.CONFIRMED)
        self.assertEqual(self.p.qty_on_hand, 50)
        line.refresh_from_db()
        self.assertIsNone(line.cost_snapshot)
        # 3. fix the cost, finalize again -> correct snapshot this time
        self.p.revalue_cost(Decimal("12.00"))
        self.po.finalize()
        line.refresh_from_db()
        self.assertEqual(line.cost_snapshot, Decimal("12.0000"))
        self.assertEqual(line.line_profit, Decimal("80.00"))  # (20-12)*10
        inv2 = Invoice.build_from_pos([self.po])
        self.assertEqual(inv2.gross_profit, Decimal("80.00"))

    def test_unfinalize_blocked_while_invoiced(self):
        from invoicing.models import Invoice
        Invoice.build_from_pos([self.po])
        self.po.refresh_from_db()
        with self.assertRaises(ValidationError):
            # status is INVOICED so _require_status blocks it
            self.po.unfinalize()

    def test_unfinalize_blocked_with_live_invoice_on_finalized_po(self):
        from invoicing.models import Invoice
        inv = Invoice.build_from_pos([self.po])
        # force PO back to finalized WITHOUT voiding (simulates tampering)
        PurchaseOrder.objects.filter(pk=self.po.pk).update(status="finalized")
        self.po.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.po.unfinalize()


class PickSetQtyTests(PickingApiTests):
    """Direct quantity entry endpoint (type + checkmark)."""

    def set_qty(self, line_id, qty):
        return self.client.post(f"/api/pick/line/{line_id}/set/",
                                {"qty": qty}, content_type="application/json")

    def test_set_qty_directly(self):
        r = self.set_qty(self.l1.pk, 2)
        self.assertEqual(r.status_code, 200)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 2)
        self.assertTrue(r.json()["line"]["complete"])

    def test_set_qty_overpick_blocked(self):
        r = self.set_qty(self.l1.pk, 99)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "overpick")
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 0)

    def test_set_qty_can_correct_downward(self):
        self.set_qty(self.l1.pk, 2)
        r = self.set_qty(self.l1.pk, 1)
        self.assertEqual(r.status_code, 200)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.qty_picked, 1)

    def test_set_qty_garbage_rejected(self):
        r = self.client.post(f"/api/pick/line/{self.l1.pk}/set/",
                             {"qty": "abc"}, content_type="application/json")
        self.assertEqual(r.status_code, 400)


class PoListBillingBadgeTests(TestCase):
    """PO list shows billed / not-billed for shipped+finalized orders."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("listboss", password="x")
        self.client.force_login(self.admin)
        self.p = stocked_product("LB-1", qty=100)
        self.c = make_customer("Badge Co")

    def _finalized_po(self):
        po = PurchaseOrder.objects.create(customer=self.c)
        POLine.objects.create(po=po, product=self.p, qty_ordered=5,
                              unit_price=Decimal("10.00"))
        po.confirm(); po.finalize()
        return po

    def test_shipped_unbilled_shows_warning(self):
        po = self._finalized_po()
        po.mark_shipped()
        r = self.client.get("/orders/")
        self.assertContains(r, "not billed")

    def test_shipped_billed_shows_check(self):
        from invoicing.models import Invoice
        po = self._finalized_po()
        po.mark_shipped()
        Invoice.build_from_pos([po])
        r = self.client.get("/orders/")
        self.assertContains(r, "✓ billed")
        self.assertNotContains(r, "⚠ not billed")   # the row badge (dropdown label differs)

    def test_unbilled_filter(self):
        from invoicing.models import Invoice
        billed = self._finalized_po(); billed.mark_shipped()
        Invoice.build_from_pos([billed])
        unbilled = self._finalized_po(); unbilled.mark_shipped()
        r = self.client.get("/orders/?status=unbilled")
        self.assertContains(r, unbilled.number)
        self.assertNotContains(r, billed.number)
