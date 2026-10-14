"""
Background jobs for the Expenses module (scheduled by ``setup_schedules``
through Django-Q2, and runnable on demand from the Recurring page).
"""
from django.utils import timezone

from core.models import log_activity
from expenses.models import RecurringExpense


def generate_recurring_expenses(user=None, today=None):
    """Create every expense that is due from active recurring templates.

    Each template advances its ``next_date`` past ``today`` (anchored to its
    day of month: Jan 31 -> Feb 28 -> Mar 31, no drift). Templates that ran
    past their end date retire themselves. Returns a one-line summary
    suitable for the task log and the UI."""
    today = today or timezone.localdate()
    templates = (RecurringExpense.objects.filter(active=True, next_date__lte=today)
                 .select_related("vendor_link", "category", "paid_from"))
    created_total = 0
    touched = 0
    for tpl in templates:
        created = tpl.generate_due(today=today, user=user)
        if created:
            touched += 1
            created_total += len(created)
            log_activity(user, "recurring_generated", tpl,
                         note=f"{len(created)} expense(s) created through {today}")
    if created_total == 0:
        return "Nothing due. No recurring expenses were created."
    return (f"Created {created_total} expense{'s' if created_total != 1 else ''} "
            f"from {touched} recurring template{'s' if touched != 1 else ''}.")
