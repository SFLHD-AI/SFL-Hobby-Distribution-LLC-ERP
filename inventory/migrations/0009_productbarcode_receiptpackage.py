import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0008_backfill_received_tracking")]

    operations = [
        migrations.CreateModel(
            name="ProductBarcode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(db_index=True, max_length=64, unique=True)),
                ("note", models.CharField(blank=True, help_text="e.g. 2nd print run, case UPC", max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="barcodes", to="inventory.product")),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="ReceiptPackage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("carrier", models.CharField(blank=True, choices=[("", "—"), ("usps", "USPS"), ("ups", "UPS"), ("fedex", "FedEx"), ("dhl_express", "DHL Express"), ("ontrac", "OnTrac"), ("lasership", "LaserShip"), ("amazon", "Amazon Logistics"), ("other", "Other / freight")], max_length=20)),
                ("tracking_number", models.CharField(db_index=True, max_length=100)),
                ("status", models.CharField(blank=True, max_length=20)),
                ("detail", models.CharField(blank=True, max_length=300)),
                ("location", models.CharField(blank=True, max_length=150)),
                ("event_at", models.DateTimeField(blank=True, null=True)),
                ("eta", models.DateField(blank=True, null=True)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("receipt", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="packages", to="inventory.receipt")),
            ],
            options={"ordering": ["eta", "id"]},
        ),
        migrations.AddConstraint(
            model_name="receiptpackage",
            constraint=models.UniqueConstraint(fields=("receipt", "tracking_number"), name="uniq_receipt_tracking_number"),
        ),
    ]
