from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Register recurring background jobs with Django-Q: hourly ShipStation "
            "tracking sync, hourly bank-feed sync, daily low-stock digest (7am), "
            "daily recurring-expense generation (6am) and the weekly money review "
            "digest (Monday 7am). Safe to re-run; existing schedules are updated.")

    # (name, dotted function path, schedule type, cron expression or None,
    #  fallback schedule type used when croniter is not installed)
    JOBS = [
        ("shipstation-sync", "shipping.tasks.sync_shipstation_tracking",
         "HOURLY", None, None),
        ("low-stock-digest", "shipping.tasks.send_low_stock_digest",
         "CRON", "0 7 * * *", "DAILY"),
        ("bank-sync", "banking.tasks.sync_all_connections",
         "HOURLY", None, None),
        ("recurring-expenses", "expenses.tasks.generate_recurring_expenses",
         "CRON", "0 6 * * *", "DAILY"),
        ("review-digest", "core.tasks.send_review_digest",
         "CRON", "0 7 * * 1", "WEEKLY"),
    ]

    def handle(self, *args, **options):
        from django_q.models import Schedule
        for name, func, stype, cron, fallback in self.JOBS:
            defaults = {"func": func, "schedule_type": getattr(Schedule, stype)}
            if cron:
                defaults["cron"] = cron
            try:
                Schedule.objects.update_or_create(name=name, defaults=defaults)
                self.stdout.write(f"scheduled: {name} -> {func}"
                                  + (f" ({cron})" if cron else " (hourly)"))
            except ImportError:
                # croniter missing: fall back to a plain daily/weekly schedule so
                # startup never fails over an optional dependency.
                defaults.pop("cron", None)
                defaults["schedule_type"] = getattr(Schedule, fallback or "DAILY")
                Schedule.objects.update_or_create(name=name, defaults=defaults)
                self.stdout.write(self.style.WARNING(
                    f"scheduled: {name} -> {func} ({(fallback or 'daily').lower()} fallback; "
                    f"install croniter for cron expressions)"))
