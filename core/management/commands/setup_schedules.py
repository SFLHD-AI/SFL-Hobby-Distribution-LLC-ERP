from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Register recurring background jobs with Django-Q: hourly ShipStation "
            "tracking sync and daily low-stock digest (7am).")

    def handle(self, *args, **options):
        from django_q.models import Schedule
        jobs = [
            ("shipstation-sync", "shipping.tasks.sync_shipstation_tracking",
             Schedule.HOURLY, None),
            ("low-stock-digest", "shipping.tasks.send_low_stock_digest",
             Schedule.CRON, "0 7 * * *"),
        ]
        for name, func, stype, cron in jobs:
            defaults = {"func": func, "schedule_type": stype}
            if cron:
                defaults["cron"] = cron
            try:
                Schedule.objects.update_or_create(name=name, defaults=defaults)
                self.stdout.write(f"scheduled: {name} -> {func}")
            except ImportError:
                # croniter missing: fall back to a plain daily schedule so
                # startup never fails over an optional dependency.
                defaults.pop("cron", None)
                defaults["schedule_type"] = Schedule.DAILY
                Schedule.objects.update_or_create(name=name, defaults=defaults)
                self.stdout.write(self.style.WARNING(
                    f"scheduled: {name} -> {func} (daily fallback; "
                    f"install croniter for cron expressions)"))
