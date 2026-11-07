"""
Install the starter category rules (once) and run auto-categorize over every
bank transaction still waiting for review. Safe to run any time: starter
rules already present are skipped and only unreviewed rows are touched.

Runs on every deploy (start.sh) and hourly (setup_schedules), so imports
uploaded before this feature existed get their suggestions too.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Install starter rules once, then auto-categorize everything still unreviewed."

    def add_arguments(self, parser):
        parser.add_argument("--no-starters", action="store_true", help="Skip installing the starter rules")
        parser.add_argument("--limit", type=int, default=20000)

    def handle(self, *args, **opts):
        from banking.tasks import auto_categorize_pending
        msg = auto_categorize_pending(install_starters=not opts["no_starters"], limit=opts["limit"])
        self.stdout.write(msg)
