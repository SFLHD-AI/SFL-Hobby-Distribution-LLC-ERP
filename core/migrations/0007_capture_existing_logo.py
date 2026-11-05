"""If a logo file is still on disk, copy its bytes into the database."""
from django.db import migrations


def forwards(apps, schema_editor):
    CompanySettings = apps.get_model("core", "CompanySettings")
    for c in CompanySettings.objects.exclude(logo=""):
        try:
            c.logo.open("rb"); data = c.logo.read(); c.logo.close()
        except (OSError, ValueError):
            continue
        ext = (c.logo.name or "").rsplit(".", 1)[-1].lower()
        c.logo_data = data
        c.logo_content_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                               "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}.get(ext, "image/png")
        c.save(update_fields=["logo_data", "logo_content_type"])


class Migration(migrations.Migration):
    dependencies = [("core", "0006_company_logo_data")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
