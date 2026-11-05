"""Copy the single tracking number each receipt used to carry into a
ReceiptPackage row, so orders can now have several."""
from django.db import migrations


def forwards(apps, schema_editor):
    Receipt = apps.get_model("inventory", "Receipt")
    ReceiptPackage = apps.get_model("inventory", "ReceiptPackage")
    for r in Receipt.objects.exclude(tracking_number=""):
        ReceiptPackage.objects.get_or_create(
            receipt=r, tracking_number=r.tracking_number,
            defaults={"carrier": r.tracking_carrier, "status": r.tracking_status,
                      "detail": r.tracking_detail, "location": r.tracking_location,
                      "event_at": r.tracking_event_at, "eta": r.tracking_eta,
                      "checked_at": r.tracking_checked_at, "error": r.tracking_error})


def backwards(apps, schema_editor):
    Receipt = apps.get_model("inventory", "Receipt")
    ReceiptPackage = apps.get_model("inventory", "ReceiptPackage")
    for p in ReceiptPackage.objects.order_by("receipt_id", "id"):
        r = p.receipt
        if r.tracking_number:
            continue
        for f in ("carrier", "tracking_number", "status", "detail", "location", "event_at", "eta", "checked_at", "error"):
            setattr(r, "tracking_" + f if f != "tracking_number" else f, getattr(p, f))
        r.save()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0009_productbarcode_receiptpackage")]
    operations = [migrations.RunPython(forwards, backwards)]
