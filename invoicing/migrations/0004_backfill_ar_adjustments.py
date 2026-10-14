"""Flag any AR adjustments created before the field existed (the Manual AR
button always stamped notes='AR adjustment', so this is a safe backfill)."""
from django.db import migrations


def backfill(apps, schema_editor):
    Invoice = apps.get_model("invoicing", "Invoice")
    Invoice.objects.filter(notes="AR adjustment").update(is_ar_adjustment=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0003_invoice_is_ar_adjustment")]
    operations = [migrations.RunPython(backfill, noop)]
