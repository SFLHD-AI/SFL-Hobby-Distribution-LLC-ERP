"""Package weights, local delivery flow, and custom-role module access."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Role, UserProfile
from customers.models import Customer, ResaleCertificate, ShippingAddress
from inventory.models import Product
from orders.models import POLine, PurchaseOrder
from shipping.models import PackageItem, Shipment, ShipmentPackage

TINY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def make_env():
    c = Customer.objects.create(business_name="Local Shop")
    ShippingAddress.objects.create(customer=c, label="Store", line1="9 Elm St",
                                   city="Tampa", state="FL", zip_code="33610",
                                   is_default=True)
    ResaleCertificate.objects.create(customer=c, certificate_number="FL-77",
                                     issuing_state="FL",
                                     expiration_date=timezone.localdate() + timedelta(days=90))
    p1 = Product.objects.create(sku="W-1", name="Heavy box",
                                default_price=Decimal("10"), weight_lbs=Decimal("2.5"))
    p2 = Product.objects.create(sku="W-2", name="Light pack",
                                default_price=Decimal("5"), weight_lbs=Decimal("0.5"))
    for p in (p1, p2):
        p.apply_movement(movement_type="receipt", qty_delta=100,
                         unit_cost=Decimal("1"), new_avg_cost=Decimal("1"))
    po = PurchaseOrder.objects.create(customer=c, ship_to=c.default_shipping_address(),
                                      local_delivery=True)
    POLine.objects.create(po=po, product=p1, qty_ordered=4, unit_price=Decimal("10"))
    POLine.objects.create(po=po, product=p2, qty_ordered=10, unit_price=Decimal("5"))
    po.confirm()
    po.finalize()
    return c, p1, p2, po


class WeightTests(TestCase):
    def test_po_total_weight(self):
        _, _, _, po = make_env()
        w = po.total_weight_lbs
        self.assertEqual(w["weight"], Decimal("15.00"))  # 4*2.5 + 10*0.5
        self.assertFalse(w["missing_weights"])

    def test_missing_weight_flagged(self):
        c, p1, _, po = make_env()
        p1.weight_lbs = None
        p1.save()
        self.assertTrue(po.total_weight_lbs["missing_weights"])

    def test_package_weight_estimation(self):
        _, p1, p2, po = make_env()
        shipment = Shipment.objects.create(po=po, provider="pirateship")
        box = ShipmentPackage.objects.create(shipment=shipment, number=1,
                                             length_in=14, width_in=12, height_in=10)
        PackageItem.objects.create(package=box, po_line=po.live_lines.get(product=p1), qty=2)
        PackageItem.objects.create(package=box, po_line=po.live_lines.get(product=p2), qty=4)
        self.assertEqual(box.estimated_weight_lbs, Decimal("7.00"))  # 2*2.5 + 4*0.5
        box.refresh_weight()
        self.assertEqual(box.weight_lbs, Decimal("7.00"))
        self.assertEqual(shipment.packages_weight_lbs, Decimal("7.00"))


@override_settings(MEDIA_ROOT="/tmp/sfl_erp_test_media")
class DeliveryFlowTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(name="Fulfillment Manager TEST",
                                        modules=["deliveries", "shipping"])
        self.driver = User.objects.create_user("dan", password="x")
        UserProfile.objects.create(user=self.driver, role=self.role)
        self.client.force_login(self.driver)
        _, _, _, self.po = make_env()
        self.shipment = Shipment.objects.create(
            po=self.po, provider="local", status="pending",
            ship_date=timezone.localdate(),
            ship_to_text=self.po.ship_to.as_text())

    def test_driver_sees_delivery_list(self):
        r = self.client.get("/deliveries/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Local Shop")

    def test_driver_blocked_from_orders_and_inventory(self):
        # middleware bounces limited users back to their own modules
        r = self.client.get("/orders/", follow=True)
        self.assertNotContains(r, "Customer orders (POs)")
        r = self.client.get("/inventory/", follow=True)
        self.assertNotContains(r, "New product")

    def test_photo_upload_marks_delivered(self):
        photo = SimpleUploadedFile("proof.png", TINY_PNG, content_type="image/png")
        r = self.client.post(f"/deliveries/{self.shipment.pk}/",
                             {"action": "deliver", "proof_photo": photo,
                              "notes": "left with manager"})
        self.assertEqual(r.status_code, 302)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "delivered")
        self.assertTrue(self.shipment.proof_photo)
        self.assertEqual(self.shipment.delivered_by, self.driver)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.SHIPPED)


class RouteOptimizationTests(TestCase):
    """Efficient multi-stop ordering + Apple Maps links (no network: cached)."""

    def _cache(self, address, lat, lng):
        from shipping.routing import GeocodeCache
        GeocodeCache.objects.create(address=" ".join(address.split()),
                                    lat=lat, lng=lng, ok=True)

    def test_optimize_order_picks_shortest_path(self):
        from shipping.routing import optimize_order
        # Warehouse at origin; stops laid out north along a line but given
        # shuffled: farthest first. Efficient order = nearest to farthest.
        origin = (28.00, -82.45)
        stops = [(28.30, -82.45), (28.10, -82.45), (28.20, -82.45)]
        self.assertEqual(optimize_order(origin, stops), [1, 2, 0])

    def test_plan_route_uses_cache_and_orders_stops(self):
        from shipping.routing import plan_route
        self._cache("WH Tampa FL", 28.00, -82.45)
        self._cache("Far St", 28.30, -82.45)
        self._cache("Near St", 28.10, -82.45)
        self._cache("Mid St", 28.20, -82.45)
        order, optimized = plan_route("WH Tampa FL", ["Far St", "Near St", "Mid St"])
        self.assertTrue(optimized)
        self.assertEqual(order, [1, 2, 0])

    def test_plan_route_falls_back_when_address_unknown(self):
        from unittest.mock import patch
        from shipping.routing import plan_route
        self._cache("WH Tampa FL", 28.00, -82.45)
        with patch("shipping.routing.requests.get", side_effect=Exception("offline")):
            order, optimized = plan_route("WH Tampa FL", ["Nowhere 1", "Nowhere 2"])
        self.assertFalse(optimized)
        self.assertEqual(order, [0, 1])  # original order kept — page still works

    def test_geocode_caches_failures(self):
        from unittest.mock import patch
        from shipping.routing import GeocodeCache, geocode
        with patch("shipping.routing.requests.get", side_effect=Exception("offline")) as m:
            self.assertIsNone(geocode("123 Bad Address"))
            self.assertIsNone(geocode("123 Bad Address"))  # second call: cache hit
        self.assertEqual(m.call_count, 1)
        self.assertEqual(GeocodeCache.objects.filter(ok=False).count(), 1)

    def test_apple_maps_url_single_destination(self):
        from shipping.routing import apple_maps_url
        url = apple_maps_url("400 N Tampa St Tampa FL 33602")
        self.assertTrue(url.startswith("https://maps.apple.com/?"))
        self.assertIn("daddr=400+N+Tampa+St+Tampa+FL+33602", url)
        self.assertIn("dirflg=d", url)

    def test_delivery_list_shows_numbered_stops_and_apple_links(self):
        from core.models import CompanySettings
        c = CompanySettings.get()
        c.address_line1, c.city, c.state, c.zip_code = "1 Depot Way", "Tampa", "FL", "33610"
        c.save()
        self._cache("1 Depot Way Tampa FL 33610", 28.00, -82.45)
        self._cache("900 Far Rd, Tampa, FL 33637", 28.30, -82.45)
        self._cache("100 Near Ave, Tampa, FL 33602", 28.10, -82.45)

        role = Role.objects.create(name="FM Route", modules=["deliveries", "shipping"])
        u = User.objects.create_user("route_dan", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.client.force_login(u)

        _, _, _, po = make_env()
        for addr in ["900 Far Rd\nTampa, FL 33637", "100 Near Ave\nTampa, FL 33602"]:
            Shipment.objects.create(po=po, provider="local", status="pending",
                                    ship_date=timezone.localdate(), ship_to_text=addr)
        r = self.client.get("/deliveries/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "maps.apple.com")
        self.assertContains(r, "most efficient drop-off order")
        # nearest stop is #1: "Near Ave" must appear before "Far Rd"
        html = r.content.decode()
        self.assertLess(html.index("100 Near Ave"), html.index("900 Far Rd"))


class CustomRoleAccessTests(TestCase):
    def test_custom_role_limits_modules(self):
        role = Role.objects.create(name="Sales only", modules=["orders", "customers"])
        u = User.objects.create_user("sally", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.client.force_login(u)
        self.assertEqual(self.client.get("/orders/").status_code, 200)
        self.assertEqual(self.client.get("/customers/").status_code, 200)
        # disallowed modules: bounced away (redirect), never rendered
        r = self.client.get("/invoices/", follow=True)
        self.assertNotContains(r, "New invoice")
        r = self.client.get("/expenses/", follow=True)
        self.assertNotContains(r, "Add expense")
        # settings & users are admin-only
        self.assertEqual(self.client.get("/settings/", follow=False).status_code, 302)

    def test_admin_role_grants_everything(self):
        role = Role.objects.create(name="Second admin", is_admin_role=True)
        u = User.objects.create_user("boss2", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.client.force_login(u)
        self.assertEqual(self.client.get("/users/").status_code, 200)
        self.assertEqual(self.client.get("/settings/").status_code, 200)

    def test_home_never_pick_queue_unless_picking_only(self):
        """A user with picking PLUS other modules must not get the dead-end
        pick queue as home — that trapped drivers on the mobile pick screen."""
        from core.roles import home_url_name
        role = Role.objects.create(name="Driver+Picker",
                                   modules=["picking", "deliveries"])
        u = User.objects.create_user("dp", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.assertEqual(home_url_name(u), "delivery_list")
        # picking-only users still land on the pick queue
        role2 = Role.objects.create(name="Picker only", modules=["picking"])
        u2 = User.objects.create_user("ponly", password="x")
        UserProfile.objects.create(user=u2, role=role2)
        self.assertEqual(home_url_name(u2), "pick_queue")
        # and the fulfillment role home page loads with a working home link
        self.client.force_login(u)
        r = self.client.get("/pick/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "/deliveries/")

    def test_cost_visibility_flag(self):
        from core.roles import can_see_costs
        role = Role.objects.create(name="Buyer", modules=["inventory"],
                                   can_see_costs=True)
        u = User.objects.create_user("buyer", password="x")
        UserProfile.objects.create(user=u, role=role)
        self.assertTrue(can_see_costs(u))
        role2 = Role.objects.create(name="Clerk", modules=["inventory"])
        u2 = User.objects.create_user("clerk", password="x")
        UserProfile.objects.create(user=u2, role=role2)
        self.assertFalse(can_see_costs(u2))


class ShippoProviderTests(TestCase):
    """Shippo adapter: payload building + response parsing (API mocked)."""

    def setUp(self):
        from core.models import CompanySettings
        cs = CompanySettings.get()
        cs.company_name = "PYC Supply Vault"
        cs.address_line1 = "1 Warehouse Way"
        cs.city, cs.state, cs.zip_code = "Tampa", "FL", "33610"
        cs.shippo_api_token = "shippo_test_token"
        cs.save()
        _, self.p1, _, self.po = make_env()
        from shipping.models import Shipment, ShipmentPackage
        self.shipment = Shipment.objects.create(po=self.po, provider="shippo")
        box = ShipmentPackage.objects.create(shipment=self.shipment, number=1,
                                             length_in=14, width_in=12, height_in=10)
        PackageItem.objects.create(package=box, po_line=self.po.live_lines.first(), qty=2)
        box.refresh_weight()

    def test_get_rates_parses_and_sorts(self):
        from unittest.mock import patch, MagicMock
        from shipping.providers import get_provider
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"rates": [
            {"object_id": "r2", "provider": "UPS",
             "servicelevel": {"name": "Ground"}, "amount": "12.50",
             "currency": "USD", "estimated_days": 3},
            {"object_id": "r1", "provider": "USPS",
             "servicelevel": {"name": "Ground Advantage"}, "amount": "8.15",
             "currency": "USD", "estimated_days": 4},
        ], "messages": []}
        with patch("shipping.providers.shippo.requests.request",
                   return_value=fake) as mock_req:
            rates = get_provider("shippo").get_rates(self.shipment)
        payload = mock_req.call_args.kwargs["json"]
        self.assertEqual(payload["address_to"]["city"], "Tampa")
        self.assertEqual(payload["parcels"][0]["weight"], "5.00")  # 2 x 2.5lb
        self.assertEqual(payload["parcels"][0]["length"], "14.0")
        self.assertEqual(rates[0]["amount"], "8.15")  # cheapest first
        self.assertEqual(rates[0]["carrier"], "USPS")

    def test_buy_label_success(self):
        from unittest.mock import patch, MagicMock
        from shipping.providers import get_provider
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"status": "SUCCESS",
                                  "tracking_number": "9400TEST123",
                                  "label_url": "https://deliver.goshippo.com/label.pdf",
                                  "tracking_url_provider": "https://tools.usps.com/x"}
        with patch("shipping.providers.shippo.requests.request", return_value=fake):
            result = get_provider("shippo").buy_label("r1")
        self.assertEqual(result["tracking_number"], "9400TEST123")
        self.assertTrue(result["label_url"].endswith(".pdf"))

    def test_no_parcels_gives_clear_error(self):
        from shipping.models import Shipment
        from shipping.providers import get_provider
        from shipping.providers.base import ProviderError
        bare = Shipment.objects.create(po=self.po, provider="shippo")
        with self.assertRaises(ProviderError):
            get_provider("shippo")._parcels(bare)

    def test_not_configured_raises(self):
        from core.models import CompanySettings
        from shipping.providers import get_provider
        from shipping.providers.base import ProviderNotConfigured
        cs = CompanySettings.get()
        cs.shippo_api_token = ""
        cs.save()
        with self.assertRaises(ProviderNotConfigured):
            get_provider("shippo").get_rates(self.shipment)
