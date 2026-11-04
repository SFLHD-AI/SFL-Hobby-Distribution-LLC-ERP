"""Orders received before the 'mark received settles tracking' rule: set their
expected date to the day they were received and mark tracking Delivered."""
from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Receipt = apps.get_model("inventory", "Receipt")
    for r in Receipt.objects.filter(posted=True):
        day = timezone.localtime(r.posted_at).date() if r.posted_at else r.date
        fields = []
        if r.expected_date != day:
            r.expected_date = day
            fields.append("expected_date")
        if r.tracking_number and r.tracking_status != "DELIVERED":
            r.tracking_status = "DELIVERED"
            r.tracking_detail = r.tracking_detail or "Marked received"
            r.tracking_eta = day
            r.tracking_error = ""
            if not r.tracking_event_at:
                r.tracking_event_at = r.posted_at
            fields += ["tracking_status", "tracking_detail", "tracking_eta", "tracking_error", "tracking_event_at"]
        if fields:
            r.save(update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0007_receipt_tracking")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
