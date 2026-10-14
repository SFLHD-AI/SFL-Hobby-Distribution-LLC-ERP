"""Address saving (the silent-failure bug) and PO ship-to scoping."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Role, UserProfile
from customers.forms import ShippingAddressForm
from customers.models import Customer, ShippingAddress
from orders.forms import POForm


class AddressSaveTests(TestCase):
    def setUp(self):
        role = Role.objects.create(name="OfficeT",
                                   modules=["dashboard", "customers", "orders"])
        self.user = User.objects.create_user("off", password="x")
        UserProfile.objects.create(user=self.user, role=role)
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(business_name="Addr Test Co")

    def test_blank_label_no_longer_blocks_save(self):
        form = ShippingAddressForm({"label": "", "line1": "1 Main St",
                                    "city": "Tampa", "state": "FL",
                                    "zip_code": "33610"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["label"], "Main")

    def test_add_address_via_customer_page(self):
        r = self.client.post(f"/customers/{self.customer.pk}/", {
            "action": "add_address", "label": "", "attention": "",
            "line1": "42 Store Rd", "line2": "", "city": "Tampa",
            "state": "FL", "zip_code": "33610", "phone": ""}, follow=True)
        addr = self.customer.shipping_addresses.filter(is_deleted=False).first()
        self.assertIsNotNone(addr, "address was not saved")
        self.assertEqual(addr.line1, "42 Store Rd")
        self.assertTrue(addr.is_default)  # first address becomes default

    def test_invalid_address_shows_error_message(self):
        r = self.client.post(f"/customers/{self.customer.pk}/", {
            "action": "add_address", "label": "X", "line1": "",  # missing line1
            "city": "Tampa", "state": "FL", "zip_code": "33610"}, follow=True)
        self.assertContains(r, "Address not saved")
        self.assertFalse(self.customer.shipping_addresses.filter(is_deleted=False).exists())


class POShipToScopingTests(TestCase):
    def setUp(self):
        self.c1 = Customer.objects.create(business_name="Customer One")
        self.c2 = Customer.objects.create(business_name="Customer Two")
        self.a1 = ShippingAddress.objects.create(
            customer=self.c1, label="Store", line1="1 One St", city="Tampa",
            state="FL", zip_code="33610", is_default=True)
        self.a2 = ShippingAddress.objects.create(
            customer=self.c2, label="Store", line1="2 Two St", city="Miami",
            state="FL", zip_code="33101", is_default=True)

    def test_other_customers_address_rejected(self):
        form = POForm({"customer": self.c1.pk, "ship_to": self.a2.pk,
                       "priority": 3, "order_discount_pct": 0,
                       "shipping_charge": 0, "tax_rate": 0})
        self.assertFalse(form.is_valid())
        self.assertIn("ship_to", form.errors)

    def test_own_address_accepted(self):
        form = POForm({"customer": self.c1.pk, "ship_to": self.a1.pk,
                       "priority": 3, "order_discount_pct": 0,
                       "shipping_charge": 0, "tax_rate": 0})
        self.assertTrue(form.is_valid(), form.errors)
