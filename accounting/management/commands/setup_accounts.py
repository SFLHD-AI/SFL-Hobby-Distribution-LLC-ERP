"""
Seed the chart of accounts and a sensible starter set of expense categories.

    python manage.py setup_accounts            # idempotent, safe to re-run

Runs automatically on container start (see docker-compose.yml).
"""
from django.core.management.base import BaseCommand

from accounting.models import Account

# (name, Schedule C line, deductible %, description)
DEFAULT_EXPENSE_CATEGORIES = [
    ("Advertising & marketing", "8", 100, "Ads, promos, sponsorships, giveaways"),
    ("Bank & merchant fees", "10", 100, "Card processing, Square/PayPal/Stripe fees, bank charges"),
    ("Contract labor", "11", 100, "1099 contractors and freelancers"),
    ("Equipment & tools", "13", 100, "Scanners, printers, shelving, computers"),
    ("Insurance", "15", 100, "Business liability, inventory, vehicle insurance"),
    ("Interest & loan fees", "16b", 100, "Interest on business credit cards and loans"),
    ("Legal & professional services", "17", 100, "Accountant, attorney, consultants"),
    ("Meals (50% deductible)", "24b", 50, "Business meals with customers or suppliers"),
    ("Office supplies", "18", 100, "Paper, ink, toner, pens, labels"),
    ("Rent & storage", "20b", 100, "Warehouse, storage units, booth rental"),
    ("Repairs & maintenance", "21", 100, ""),
    ("Shipping supplies", "22", 100, "Boxes, bubble mailers, tape, team bags, toploaders"),
    ("Software & subscriptions", "18", 100, "ShipStation, QuickBooks, pricing tools, cloud storage"),
    ("Taxes & licenses", "23", 100, "Business licenses, permits, state fees"),
    ("Trade shows & events", "27a", 100, "Card show tables, convention fees"),
    ("Travel", "24a", 100, "Flights, hotels, mileage reimbursed at the standard rate"),
    ("Utilities & phone", "25", 100, "Internet, phone, electricity"),
    ("Vehicle & fuel", "9", 100, "Gas, tolls, parking for deliveries and pickups"),
    ("Payroll & wages", "26", 100, "W-2 employees"),
    ("Other expenses", "27a", 100, "Anything that doesn't fit elsewhere"),
]


class Command(BaseCommand):
    help = "Create the system chart of accounts and default expense categories (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--no-categories", action="store_true",
                            help="Only create system accounts, skip default expense categories")

    def handle(self, *args, **options):
        accounts = Account.ensure_system_accounts()
        self.stdout.write(f"system accounts: {len(accounts)} present")
        if options["no_categories"]:
            return
        from expenses.models import ExpenseCategory
        created = 0
        for name, line, pct, desc in DEFAULT_EXPENSE_CATEGORIES:
            cat = ExpenseCategory.all_objects.filter(name__iexact=name).first()
            if cat is None:
                ExpenseCategory.objects.create(name=name, tax_line=line,
                                               deductible_pct=pct, description=desc)
                created += 1
            else:
                changed = []
                if cat.is_deleted:
                    cat.is_deleted = False; changed.append("is_deleted")
                if not cat.tax_line:
                    cat.tax_line = line; changed.append("tax_line")
                if changed:
                    cat.save(update_fields=changed)
                cat.ensure_account()
        # make sure every category (including pre-existing ones) has a ledger account
        for cat in ExpenseCategory.objects.filter(account__isnull=True):
            cat.ensure_account()
        self.stdout.write(self.style.SUCCESS(
            f"expense categories: {created} created, "
            f"{ExpenseCategory.objects.count()} total, all mapped to ledger accounts"))
