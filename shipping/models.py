"""
Shipments recorded against POs, provider-agnostic.

Actual carrier communication lives in shipping/providers/ behind the
ShippingProvider interface (ShipStation API, uShip API/manual, Pirate Ship
CSV batch + manual paste-back).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import SoftDeleteModel


class Shipment(SoftDeleteModel):
    PROVIDERS = [
        ("shipstation", "ShipStation"),
        ("shippo", "Shippo"),
        ("uship", "uShip (freight/LTL)"),
        ("pirateship", "Pirate Ship"),
        ("local", "Local delivery (our driver)"),
        ("manual", "Manual / other"),
    ]
    STATUS = [
        ("pending", "Pending"),
        ("pushed", "Pushed to provider"),
        ("label_created", "Label created"),
        ("out_for_delivery", "Out for delivery"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    po = models.ForeignKey("orders.PurchaseOrder", on_delete=models.PROTECT,
                           related_name="shipments")
    provider = models.CharField(max_length=15, choices=PROVIDERS, default="manual")
    service = models.CharField(max_length=100, blank=True,
                               help_text="e.g. UPS Ground, USPS Priority, LTL")
    status = models.CharField(max_length=20, choices=STATUS, default="pending")

    ship_to_text = models.TextField(blank=True)
    weight_lbs = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    length_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    width_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    height_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)

    tracking_number = models.CharField(max_length=100, blank=True, db_index=True)
    label_url = models.URLField(max_length=500, blank=True,
                                help_text="Purchased label PDF (Shippo)")
    cost = models.DecimalField("Label/freight cost", max_digits=12, decimal_places=2,
                               null=True, blank=True)
    ship_date = models.DateField(null=True, blank=True)

    external_id = models.CharField(max_length=100, blank=True,
                                   help_text="ShipStation orderId / uShip listing ref")
    external_status = models.CharField(max_length=60, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    tracking_emailed_at = models.DateTimeField(null=True, blank=True)

    # Local delivery (our driver)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="deliveries_made")
    proof_photo = models.ImageField(upload_to="delivery_proof/%Y/%m/", blank=True, null=True,
                                    help_text="Photo taken by the driver at drop-off")
    delivery_notes = models.CharField(max_length=500, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.po.number} via {self.get_provider_display()} {self.tracking_number or '(no tracking)'}"

    @property
    def packages_weight_lbs(self):
        from decimal import Decimal
        total = Decimal("0")
        for p in self.packages.filter(is_deleted=False):
            if p.weight_lbs:
                total += p.weight_lbs
        return total

    @property
    def maps_destination(self):
        """One-line address for Google Maps."""
        return " ".join((self.ship_to_text or "").replace("\n", ", ").split())


from shipping.routing import GeocodeCache  # noqa: E402  (registers the model)


class ShipmentPackage(SoftDeleteModel):
    """One physical box within a shipment — the 'split up items in a PO' unit.

    Weight auto-sums from the items assigned to it (using per-unit product
    weights) but can be overridden after weighing the packed box.
    """

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="packages")
    number = models.PositiveSmallIntegerField(default=1)
    length_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    width_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    height_in = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    weight_lbs = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                     help_text="Auto-estimated from contents; override after weighing")
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"{self.shipment.po.number} box {self.number}"

    @property
    def dims_display(self):
        if self.length_in and self.width_in and self.height_in:
            return f'{self.length_in}×{self.width_in}×{self.height_in}"'
        return "—"

    @property
    def estimated_weight_lbs(self):
        from decimal import Decimal
        total = Decimal("0")
        for item in self.items.all():
            w = item.po_line.product.weight_lbs
            if w:
                total += Decimal(item.qty) * w
        return total.quantize(Decimal("0.01"))

    def refresh_weight(self):
        self.weight_lbs = self.estimated_weight_lbs
        self.save(update_fields=["weight_lbs"])


class PackageItem(models.Model):
    package = models.ForeignKey(ShipmentPackage, on_delete=models.CASCADE, related_name="items")
    po_line = models.ForeignKey("orders.POLine", on_delete=models.CASCADE,
                                related_name="package_items")
    qty = models.PositiveIntegerField()

    class Meta:
        unique_together = [("package", "po_line")]

    def __str__(self):
        return f"{self.package} · {self.po_line.product.sku} x{self.qty}"
