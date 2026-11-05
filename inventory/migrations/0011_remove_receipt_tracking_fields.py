from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("inventory", "0010_move_tracking_to_packages")]
    operations = [migrations.RemoveField(model_name="receipt", name=f) for f in (
        "tracking_carrier", "tracking_number", "tracking_status", "tracking_detail",
        "tracking_location", "tracking_event_at", "tracking_eta", "tracking_checked_at", "tracking_error")]
