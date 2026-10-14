"""
Seed realistic demo data: 30 SKUs, 10 customers, 20 POs in various statuses,
plus receipts, invoices, payments, expenses, two bank accounts with ~70
transactions (deposits matched to payments, supplier ACH payments, card
charges auto-categorized by rules, transfers, an owner draw, a dozen left
to review), vendors, category rules, a recurring expense, mileage and
budgets — so every screen has life in it.

    python manage.py seed_demo            # adds demo data (idempotent-ish)
    python manage.py seed_demo --wipe     # flush first (DANGER: dev only)
"""
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import CompanySettings, NumberSequence
from customers.models import Contact, Customer, ResaleCertificate, ShippingAddress
from expenses.models import Expense, ExpenseCategory
from inventory.models import (Brand, Category, PriceList, PriceListItem,
                              Product, Receipt, ReceiptLine, StockMovement, Supplier)
from invoicing.models import Invoice
from orders.models import POLine, PurchaseOrder
from payments.models import Payment

random.seed(42)

D = Decimal

BRANDS = ["Topps", "Panini", "Upper Deck", "Bowman", "Leaf", "Ultra PRO", "BCW"]
CATEGORIES = ["Baseball", "Basketball", "Football", "Hockey", "Soccer",
              "Pokemon TCG", "Supplies"]

PRODUCTS = [
    # (sku, name, brand, category, cost, price, msrp, case, upc_suffix, reorder)
    ("TOP26S1-HOB", "2026 Topps Series 1 Baseball Hobby Box", "Topps", "Baseball", 58, 74.50, 99.99, 12, "0001", 24),
    ("TOP26S1-BLA", "2026 Topps Series 1 Baseball Blaster", "Topps", "Baseball", 17, 22.75, 29.99, 40, "0002", 60),
    ("TOP26HER-HOB", "2026 Topps Heritage Hobby Box", "Topps", "Baseball", 89, 112.00, 149.99, 12, "0003", 12),
    ("BOW26CH-HOB", "2026 Bowman Chrome Hobby Box", "Bowman", "Baseball", 175, 219.00, 299.99, 12, "0004", 12),
    ("BOW26P-JUM", "2026 Bowman Paper Jumbo Box", "Bowman", "Baseball", 120, 152.00, 199.99, 8, "0005", 8),
    ("PAN26PZ-HOB", "2025-26 Panini Prizm Basketball Hobby", "Panini", "Basketball", 320, 399.00, 499.99, 12, "0006", 6),
    ("PAN26PZ-BLA", "2025-26 Panini Prizm Basketball Blaster", "Panini", "Basketball", 21, 27.25, 34.99, 40, "0007", 60),
    ("PAN26SEL-HOB", "2025-26 Panini Select Basketball Hobby", "Panini", "Basketball", 240, 299.00, 399.99, 12, "0008", 6),
    ("PAN26MOS-MEGA", "2025-26 Panini Mosaic Basketball Mega", "Panini", "Basketball", 28, 36.50, 49.99, 20, "0009", 40),
    ("PAN26DON-HOB", "2026 Panini Donruss Football Hobby", "Panini", "Football", 145, 185.00, 249.99, 12, "0010", 10),
    ("PAN26PZF-BLA", "2026 Panini Prizm Football Blaster", "Panini", "Football", 24, 31.00, 39.99, 40, "0011", 50),
    ("UD26S1-HOB", "2025-26 Upper Deck Series 1 Hockey Hobby", "Upper Deck", "Hockey", 68, 87.00, 119.99, 12, "0012", 12),
    ("UD26YG-TIN", "2025-26 Upper Deck Young Guns Tin", "Upper Deck", "Hockey", 15, 19.50, 24.99, 24, "0013", 36),
    ("TOP26UCL-HOB", "2025-26 Topps UEFA CL Chrome Hobby", "Topps", "Soccer", 98, 124.00, 159.99, 12, "0014", 8),
    ("PKM-SV10-BB", "Pokemon SV10 Booster Box", "Leaf", "Pokemon TCG", 105, 132.00, 161.64, 6, "0015", 18),
    ("PKM-SV10-ETB", "Pokemon SV10 Elite Trainer Box", "Leaf", "Pokemon TCG", 34, 43.50, 54.99, 10, "0016", 30),
    ("PKM-151-UPC", "Pokemon 151 Ultra Premium Collection", "Leaf", "Pokemon TCG", 84, 108.00, 129.99, 6, "0017", 10),
    ("LEAF26-META", "2026 Leaf Metal Draft Football Hobby", "Leaf", "Football", 72, 92.00, 124.99, 12, "0018", 8),
    ("UP-TL34-25", "Ultra PRO 3x4 Toploaders (25ct pack)", "Ultra PRO", "Supplies", 1.35, 2.10, 3.49, 100, "0019", 400),
    ("UP-SLV100", "Ultra PRO Soft Sleeves (100ct)", "Ultra PRO", "Supplies", 0.85, 1.40, 2.49, 200, "0020", 500),
    ("UP-ONE-MAG", "Ultra PRO ONE-TOUCH Magnetic 35pt", "Ultra PRO", "Supplies", 1.10, 1.75, 2.99, 200, "0021", 300),
    ("BCW-CS1-200", "BCW Card Saver I Semi-Rigid (200ct)", "BCW", "Supplies", 11.50, 15.75, 21.99, 20, "0022", 40),
    ("BCW-3200BX", "BCW 3200ct Cardboard Storage Box", "BCW", "Supplies", 2.60, 3.95, 5.99, 25, "0023", 50),
    ("BCW-TEAMBAG", "BCW Resealable Team Bags (100ct)", "BCW", "Supplies", 1.20, 1.95, 2.99, 100, "0024", 200),
    ("TOP26CHR-HOB", "2026 Topps Chrome Baseball Hobby", "Topps", "Baseball", 132, 168.00, 219.99, 12, "0025", 12),
    ("PAN26OPT-BLA", "2026 Panini Donruss Optic Blaster", "Panini", "Football", 26, 33.75, 44.99, 40, "0026", 40),
    ("UD26EXT-HOB", "2025-26 UD Extended Hockey Hobby", "Upper Deck", "Hockey", 62, 79.00, 104.99, 12, "0027", 10),
    ("PKM-PRISM-BB", "Pokemon Prismatic Evolutions Booster Bundle", "Leaf", "Pokemon TCG", 26, 34.00, 44.99, 12, "0028", 30),
    ("TOP26GAL-HOB", "2026 Topps Gallery Baseball Hobby", "Topps", "Baseball", 76, 96.00, 129.99, 12, "0029", 8),
    ("PAN26PH-MEGA", "2026 Panini Phoenix Football Mega", "Panini", "Football", 31, 39.50, 54.99, 20, "0030", 30),
]

CUSTOMERS = [
    ("Grand Slam Cards & Comics", "Stamford", "CT", "net30", "orders@grandslamcc.com"),
    ("The Dugout Sportscards", "Cherry Hill", "NJ", "net15", "buy@dugoutcards.com"),
    ("Rookie Season Collectibles", "Tampa", "FL", "net30", "purchasing@rookieseason.com"),
    ("Cardboard Kings", "Brooklyn", "NY", "due_on_receipt", "kings@cardboardkings.nyc"),
    ("Midwest Breaks LLC", "Columbus", "OH", "net30", "supply@midwestbreaks.tv"),
    ("Pacific Coast Cards", "San Diego", "CA", "net45", "po@pacificcoastcards.com"),
    ("Hometown Hobby Shop", "Franklin", "TN", "net15", "hometownhobbytn@gmail.com"),
    ("Vintage Vault Sports", "Scottsdale", "AZ", "net30", "vault@vintagevaultaz.com"),
    ("Top Shelf Sports Cards", "Minneapolis", "MN", "net30", "orders@topshelfmn.com"),
    ("East Side Collectibles", "Queens", "NY", "due_on_receipt", "eastsidecollect@outlook.com"),
]

# Vendors the bank feed should recognise.
# (name, default expense category (None = not an expense category), aliases, is_1099)
VENDORS = [
    ("Pirate Ship", None, "PIRATE SHIP\nPIRATESHIP", False),
    ("USPS", None, "USPS PO\nUSPS.COM\nUSPS POSTAGE", False),
    ("ULINE", "Shipping supplies", "ULINE *SHIP SUPPLIES\nULINE SHIP", False),
    ("Amazon", "Office supplies", "AMZN Mktp\nAMAZON.COM\nAMZN", False),
    ("Shopify", "Software & subscriptions", "SHOPIFY*", False),
    ("Google Workspace", "Software & subscriptions", "GOOGLE *WORKSPACE\nGOOGLE*GSUITE", False),
    ("FPL", "Utilities & phone", "FPL DIRECT DEBIT\nFLORIDA POWER", False),
    ("Tampa Bay Sports Card Show", "Trade shows & events", "TAMPA BAY CARD SHOW\nTB CARD SHOW", False),
    ("Commerce Park LLC", "Rent & storage", "COMMERCE PARK", False),
    ("Chase", "Bank & merchant fees", "MONTHLY SERVICE FEE", False),
    ("Marcus Reyes (show help)", "Contract labor", "ZELLE PAYMENT TO MARCUS REYES", True),
]

# Hand-entered demo expenses: (category, vendor) pairs drawn at random. The
# category names are the setup_accounts defaults, so the demo never creates
# a second "Rent" next to "Rent & storage".
MANUAL_EXPENSE_VENDORS = {
    "Shipping supplies": "ULINE",
    "Software & subscriptions": "QuickBooks",
    "Rent & storage": "Commerce Park LLC",
    "Contract labor": "Marcus Reyes (show help)",
    "Bank & merchant fees": "Stripe",
    "Utilities & phone": "FPL",
}

# Category names older demo seeds created (pre-4.0) -> the default they belong
# to. On re-run their expenses, vendors, budgets and rules are moved to the
# default category and the duplicate is soft-deleted.
LEGACY_CATEGORY_NAMES = {
    "Software": "Software & subscriptions",
    "Rent": "Rent & storage",
    "Labor": "Contract labor",
    "Fees": "Bank & merchant fees",
    "Utilities": "Utilities & phone",
}

# Note stamped on the expenses this seed creates, so re-runs can tell them
# apart from bills the owner typed in.
DEMO_EXPENSE_NOTE = "Demo expense — owner paid personally"


def _aware(day, hour=10):
    """Local-time datetime on ``day`` (used to backdate ledger timestamps)."""
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


class Command(BaseCommand):
    help = "Load realistic demo data for testing."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true")

    def handle(self, *args, **options):
        if options["wipe"]:
            call_command("flush", "--noinput")
        call_command("setup_roles")
        today = timezone.localdate()

        # --- users -----------------------------------------------------------
        def mkuser(username, group, is_super=False, first="", last=""):
            user = User.objects.filter(username=username).first()
            if not user:
                if is_super:
                    user = User.objects.create_superuser(username, f"{username}@example.com", "sflerp123")
                else:
                    user = User.objects.create_user(username, f"{username}@example.com", "sflerp123")
                user.first_name, user.last_name = first, last
                user.save()
            if group:
                user.groups.add(Group.objects.get(name=group))
            return user

        admin = mkuser("admin", "Admin", is_super=True, first="Ross", last="Owner")
        office = mkuser("office", "Office", first="Olivia", last="Office")
        picker = mkuser("picker", "Warehouse", first="Pete", last="Picker")
        driver = mkuser("driver", None, first="Dan", last="Driver")
        books = mkuser("books", None, first="Bea", last="Books")
        # attach role profiles
        from core.models import Role, UserProfile
        for u, role_name in [(admin, "Admin"), (office, "Office"),
                             (picker, "Warehouse"), (driver, "Fulfillment Manager"),
                             (books, "Bookkeeper")]:
            role = Role.objects.filter(name=role_name).first()
            profile, _ = UserProfile.objects.get_or_create(user=u)
            if role and not profile.role_id:
                profile.role = role
                profile.save(update_fields=["role"])
        self.stdout.write("users: admin / office / picker / driver / books (password: sflerp123)")

        # --- company settings --------------------------------------------------
        cs = CompanySettings.get()
        if cs.company_name == "My Company":
            cs.company_name = "VIP Rips Distribution LLC"
            cs.address_line1 = "4270 Commerce Park Dr"
            cs.address_line2 = "Suite 12"
            cs.city, cs.state, cs.zip_code = "Tampa", "FL", "33610"
            cs.phone, cs.email = "(813) 555-0148", "orders@viprips.com"
            cs.website = "www.viprips.com"
            cs.default_tax_rate = Decimal("6.5")
            cs.invoice_template = "modern"
            cs.invoice_footer = ("Payment due per terms above. Please include the "
                                 "invoice number on your check or wire.")
            cs.alert_email = "orders@viprips.com"
            cs.save()
        for key, prefix, start in [("po", "PO-", 1001), ("invoice", "INV-", 2001),
                                   ("credit", "CM-", 101)]:
            NumberSequence.objects.get_or_create(
                key=key, defaults={"prefix": prefix, "next_number": start})

        # --- catalog ------------------------------------------------------------
        brands = {n: Brand.objects.get_or_create(name=n)[0] for n in BRANDS}
        cats = {n: Category.objects.get_or_create(name=n)[0] for n in CATEGORIES}
        supplier_names = ["GTS Distribution", "Southern Hobby Supply",
                          "Magazine Exchange", "Peach State Hobby"]
        suppliers = [Supplier.objects.get_or_create(name=n)[0] for n in supplier_names]

        bins = [f"{r}-{s:02d}-{lvl}" for r in "ABCD" for s in range(1, 5) for lvl in (1, 2)]
        products = []
        for i, (sku, name, brand, cat, cost, price, msrp, case, upc, reorder) in enumerate(PRODUCTS):
            weight = ("2.1" if "HOB" in sku or "JUM" in sku else
                      "1.0" if "BLA" in sku else
                      "1.6" if "MEGA" in sku else
                      "1.3" if "TIN" in sku else
                      "1.5" if "ETB" in sku or "UPC" in sku else
                      "1.9" if "-BB" in sku else "0.8")
            p, created = Product.all_objects.get_or_create(sku=sku, defaults=dict(
                name=name, brand=brands[brand], category=cats[cat],
                upc=f"81234567{upc}", case_pack_qty=case,
                unit_of_measure="each", weight_lbs=Decimal(weight),
                default_price=Decimal(str(price)), msrp=Decimal(str(msrp)),
                reorder_point=reorder, bin_location=bins[i % len(bins)],
                description=f"{name}. Factory sealed. Case pack {case}.",
            ))
            if p.weight_lbs is None:
                p.weight_lbs = Decimal(weight)
                p.save(update_fields=["weight_lbs"])
            products.append(p)
        self.stdout.write(f"products: {len(products)}")

        # --- receiving (builds stock + weighted average costs) --------------------
        if not Receipt.objects.exists():
            for wk in range(3):
                rcv_date = today - timedelta(days=45 - wk * 15)
                supplier = random.choice(suppliers)
                receipt = Receipt.objects.create(
                    supplier=supplier, date=rcv_date,
                    reference=f"SUP-{7000 + wk}",
                    supplier_invoice_number=f"{''.join(w[0] for w in supplier.name.split())}-{48210 + wk * 37}",
                    due_date=rcv_date + timedelta(days=30),
                    freight_cost=Decimal(random.choice(["120.00", "185.00", "240.00"])),
                    duties_cost=Decimal("0.00"),
                    created_by=admin)
                chosen = random.sample(PRODUCTS, 18)
                for sku, name, brand, cat, cost, *_rest in chosen:
                    p = Product.objects.get(sku=sku)
                    qty = random.choice([24, 36, 48, 60, 120, 240]) if p.default_price < 20 \
                        else random.choice([12, 24, 36, 48])
                    jitter = Decimal(str(round(random.uniform(0.97, 1.05), 3)))
                    ReceiptLine.objects.create(
                        receipt=receipt, product=p, qty=qty,
                        unit_cost=(Decimal(str(cost)) * jitter).quantize(Decimal("0.01")))
                receipt.post(user=admin)
                # Backdate the posting + its stock movements to the receipt date so
                # the accrual ledger sees inventory arrive when it actually did.
                Receipt.objects.filter(pk=receipt.pk).update(posted_at=_aware(rcv_date))
                StockMovement.objects.filter(source_type="Receipt", source_id=str(receipt.pk)).update(
                    timestamp=_aware(rcv_date))
            self.stdout.write("receipts: 3 posted (stock + avg costs built)")

        # --- price list ------------------------------------------------------------
        vip, _ = PriceList.objects.get_or_create(name="VIP Dealer",
                                                 defaults={"description": "Best pricing tier"})
        for p in products[:12]:
            PriceListItem.objects.get_or_create(
                price_list=vip, product=p, is_deleted=False,
                defaults={"price": (p.default_price * Decimal("0.95")).quantize(Decimal("0.01"))})

        # --- customers ---------------------------------------------------------------
        customers = []
        streets = ["88 Main St", "412 Market Ave", "77 Broad St", "1500 Industry Rd",
                   "23 Elm St", "901 Pacific Blvd", "610 Church St", "4040 Camelback Rd",
                   "212 Hennepin Ave", "37-11 Ditmars Blvd"]
        for i, (name, city, state, terms, email) in enumerate(CUSTOMERS):
            c, created = Customer.objects.get_or_create(business_name=name, defaults=dict(
                email=email, phone=f"(555) 01{i:02d}-{1000 + i * 37}",
                billing_line1=streets[i], billing_city=city, billing_state=state,
                billing_zip=f"{10000 + i * 731}", payment_terms=terms,
                price_list=vip if i < 3 else None,
                discount_pct=Decimal("2.5") if 3 <= i < 6 else Decimal("0"),
                credit_limit=Decimal(random.choice(["5000", "10000", "25000"])) if i % 2 else None,
                no_cert_policy="block" if i in (4, 9) else "charge_tax"))
            customers.append(c)
            if not created:
                continue
            ShippingAddress.objects.create(
                customer=c, label="Store", line1=streets[i], city=city,
                state=state, zip_code=f"{10000 + i * 731}", is_default=True)
            if i % 3 == 0:
                ShippingAddress.objects.create(
                    customer=c, label="Warehouse", line1=f"{100 + i} Depot Way",
                    city=city, state=state, zip_code=f"{10000 + i * 731}")
            Contact.objects.create(customer=c, name=f"Contact {i + 1}",
                                   email=email, is_primary=True, role="Owner")
            # certificates: most valid, one expiring, one expired, one missing
            if i == 7:
                exp = today + timedelta(days=12)      # expiring soon
            elif i == 8:
                exp = today - timedelta(days=20)      # expired
            elif i == 9:
                exp = None                            # none
            else:
                exp = today + timedelta(days=200 + i * 30)
            if exp:
                ResaleCertificate.objects.create(
                    customer=c, certificate_number=f"{state}-RC-{2200 + i}",
                    issuing_state=state,
                    issue_date=today - timedelta(days=300),
                    expiration_date=exp)
        self.stdout.write(f"customers: {len(customers)}")

        # --- purchase orders in assorted statuses ---------------------------------------
        products = list(Product.objects.all())  # refresh: receipts changed stock
        if PurchaseOrder.objects.count() < 20:
            def build_po(customer, n_lines, status_flow, days_ago, priority=3):
                po = PurchaseOrder.objects.create(
                    customer=customer, ship_to=customer.default_shipping_address(),
                    priority=priority, created_by=office,
                    charge_shipping=random.random() < 0.5,
                    shipping_charge=Decimal(random.choice(["25.00", "45.00", "85.00"])),
                    charge_tax=not customer.has_valid_certificate,
                    tax_rate=CompanySettings.get().default_tax_rate)
                PurchaseOrder.objects.filter(pk=po.pk).update(
                    created_at=timezone.now() - timedelta(days=days_ago))
                po.refresh_from_db()
                for p in random.sample(products, n_lines):
                    avail = p.qty_available
                    if avail < 2:
                        continue
                    qty = min(random.choice([2, 3, 4, 6, 12, 24]), max(avail // 2, 1))
                    POLine.objects.create(po=po, product=p, qty_ordered=qty,
                                          unit_price=customer.price_for(p),
                                          description=p.name)
                if not po.live_lines.exists():
                    po.hard_delete()
                    return None
                for step in status_flow:
                    try:
                        if step == "confirm":
                            po.confirm(user=office)
                        elif step == "pick":
                            po.start_picking(user=office)
                            for line in po.live_lines.all():
                                line.qty_picked = line.qty_ordered
                                line.save(update_fields=["qty_picked"])
                            po.complete_pick(user=picker)
                        elif step == "shortpick":
                            po.start_picking(user=office)
                            for j, line in enumerate(po.live_lines.all()):
                                if j == 0 and line.qty_ordered > 1:
                                    line.qty_picked = line.qty_ordered - 1
                                    line.short_reason = "oos"
                                else:
                                    line.qty_picked = line.qty_ordered
                                line.save()
                            po.complete_pick(user=picker)
                        elif step == "finalize":
                            po.finalize(user=office)
                        elif step == "cancel":
                            po.cancel(user=office)
                    except Exception:
                        break
                return po

        # 4 drafts, 4 confirmed (pick queue), 2 in picking (partially picked),
        # 2 picked, 4 finalized→invoiced, 2 finalized uninvoiced, 1 cancelled, 1 short→backorder
            flows = (
                [([], 1)] * 4
                + [(["confirm"], 5)] * 4
                + [(["confirm"], 3)] * 2          # will move to picking below
                + [(["confirm", "pick"], 6)] * 2
                + [(["confirm", "pick", "finalize"], random.randint(8, 25))] * 6
                + [(["confirm", "cancel"], 9)]
                + [(["confirm", "shortpick", "finalize"], 4)]
            )
            made = []
            for i, (flow, age) in enumerate(flows):
                cust = customers[i % len(customers)]
                if cust.no_cert_policy == "block" and not cust.has_valid_certificate \
                        and "finalize" in flow:
                    cust = customers[0]
                po = build_po(cust, random.randint(2, 5), flow, age,
                              priority=random.choice([1, 2, 3, 3, 3, 4]))
                if po:
                    made.append(po)

            # put two confirmed POs into active picking with partial progress
            picking_candidates = [p for p in made if p.status == PurchaseOrder.CONFIRMED][:2]
            for po in picking_candidates:
                po.start_picking(user=picker)
                for j, line in enumerate(po.live_lines.all()):
                    if j % 2 == 0:
                        line.qty_picked = max(line.qty_ordered - 1, 0)
                        line.save(update_fields=["qty_picked"])

            # backorder from the short-picked PO
            for po in made:
                if po.status == PurchaseOrder.FINALIZED and any(
                        l.qty_ordered - l.fulfilled_qty > 0 for l in po.live_lines):
                    try:
                        po.create_backorder(user=office)
                    except Exception:
                        pass
                    break
            self.stdout.write(f"purchase orders: {PurchaseOrder.objects.count()}")

            # --- invoices + payments -------------------------------------------------
            # Invoices are dated AFTER the last supplier receipt and spread over the
            # days since, in order of creation; the POs' sale movements are
            # backdated to the invoice date so revenue and COGS land in the same
            # period on the accrual statements.
            finalized = list(PurchaseOrder.objects.filter(status=PurchaseOrder.FINALIZED).order_by("pk"))
            last_receipt = (Receipt.objects.filter(posted=True).order_by("-date")
                            .values_list("date", flat=True).first()) or (today - timedelta(days=15))
            first_inv_day = min(last_receipt + timedelta(days=1), today)
            span = max((today - first_inv_day).days, 1)
            # consolidate two same-customer POs when possible
            by_cust = {}
            for po in finalized:
                by_cust.setdefault(po.customer_id, []).append(po)
            built = []
            for cust_id, pos in by_cust.items():
                take = pos[:2]
                try:
                    inv = Invoice.build_from_pos(take, user=office)
                except Exception:
                    continue
                built.append((inv, take))
            n_inv = max(len(built), 1)
            for idx, (inv, take) in enumerate(built):
                inv_date = first_inv_day + timedelta(days=(span * idx) // n_inv)
                inv_date = min(max(inv_date, max(timezone.localtime(po.created_at).date() for po in take)), today)
                Invoice.objects.filter(pk=inv.pk).update(date=inv_date)
                inv.refresh_from_db()
                inv.due_date = inv.date + timedelta(days=inv.customer.term_days)
                inv.save(update_fields=["due_date"])
                for po in take:
                    StockMovement.objects.filter(source_type="PurchaseOrder", source_id=str(po.pk),
                                                 movement_type="sale").update(timestamp=_aware(inv_date, 12))
                    PurchaseOrder.objects.filter(pk=po.pk).update(finalized_at=_aware(inv_date, 11))
                roll = random.random()
                if roll < 0.4:      # fully paid
                    pay = Payment.objects.create(
                        customer=inv.customer, amount=inv.total,
                        date=min(inv.date + timedelta(days=random.randint(1, 6)), today),
                        method=random.choice(["check", "ach", "zelle", "wire"]),
                        reference=f"CHK{random.randint(1000, 9999)}",
                        created_by=office)
                    pay.apply_to_invoice(inv, inv.total, user=office)
                elif roll < 0.7:    # partial
                    part = (inv.total / 2).quantize(Decimal("0.01"))
                    pay = Payment.objects.create(
                        customer=inv.customer, amount=part,
                        date=min(inv.date + timedelta(days=random.randint(1, 5)), today),
                        method="check", reference=f"CHK{random.randint(1000, 9999)}",
                        created_by=office)
                    pay.apply_to_invoice(inv, part, user=office)
            self.stdout.write(f"invoices: {Invoice.objects.count()}, "
                              f"payments: {Payment.objects.count()}")

        # --- a local delivery scheduled today, for the driver to try ------------
        from shipping.models import Shipment
        if not Shipment.objects.filter(provider="local").exists():
            local_po = (PurchaseOrder.objects
                        .filter(status__in=[PurchaseOrder.FINALIZED, PurchaseOrder.INVOICED])
                        .first())
            if local_po:
                local_po.local_delivery = True
                local_po.save(update_fields=["local_delivery"])
                Shipment.objects.create(
                    po=local_po, provider="local", status="pending",
                    ship_date=today,
                    ship_to_text=local_po.ship_to.as_text() if local_po.ship_to else "",
                    created_by=admin)
                self.stdout.write(f"local delivery scheduled today: {local_po.number}")

        # =====================================================================
        # MONEY: chart of accounts, vendors, bank accounts + feed, expenses,
        # rules, recurring, mileage, budgets
        # =====================================================================
        call_command("setup_accounts")
        self.seed_money(admin, office, today)

        try:
            call_command("setup_schedules")
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(
            "\nDemo data ready. Log in as admin / office / picker / books "
            "(password: sflerp123)."))

    # ------------------------------------------------------------------------
    # Money block
    # ------------------------------------------------------------------------
    def seed_money(self, admin, office, today):
        from accounting.models import Account
        from banking.models import BankAccount, BankTransaction, CategoryRule
        from expenses.models import Budget, MileageEntry, RecurringExpense, Vendor

        self.fold_legacy_categories(admin)

        def category(name):
            cat = ExpenseCategory.objects.filter(name__iexact=name).first()
            if cat is None:
                cat = ExpenseCategory.objects.create(name=name)
            cat.ensure_account()
            return cat

        # --- vendors ------------------------------------------------------------
        vendors = {}
        for name, cat_name, aliases, is_1099 in VENDORS:
            v, created = Vendor.objects.get_or_create(name=name, defaults=dict(
                default_category=category(cat_name) if cat_name else None,
                aliases=aliases, is_1099=is_1099,
                tax_id_last4="4471" if is_1099 else ""))
            vendors[name] = v
        self.stdout.write(f"vendors: {Vendor.objects.count()}")

        # --- bank accounts (guard: the whole feed is seeded once) -----------------
        if BankAccount.objects.exists():
            checking = BankAccount.objects.filter(account_type="checking").first()
            card = BankAccount.objects.filter(account_type="credit_card").first()
            seeded_feed = False
        else:
            opening = today - timedelta(days=90)
            checking = BankAccount.objects.create(
                name="Chase Business Checking", institution="Chase", account_type="checking",
                last4="4471", opening_balance=D("12500.00"), opening_date=opening, is_default=True,
                notes="Demo account. Opening balance as of the books start date.")
            card = BankAccount.objects.create(
                name="Chase Ink Business Cash", institution="Chase", account_type="credit_card",
                last4="8830", opening_balance=D("1840.00"), opening_date=opening,
                notes="Demo card. Opening balance = amount owed on the books start date.")
            seeded_feed = True
            self.stdout.write("bank accounts: Chase Business Checking ($12,500), Chase Ink card ($1,840 owed)")

        # --- hand-entered expenses (18) -----------------------------------------------
        # All paid personally by the owner (an owner contribution on the books).
        # They must NOT be "paid from checking": there is no bank transaction
        # behind them, so the ledger's checking balance would disagree with the
        # register by their sum.
        if not Expense.objects.filter(source="manual").exists():
            cat_names = list(MANUAL_EXPENSE_VENDORS)
            for i in range(18):
                cat_name = random.choice(cat_names)
                cat = category(cat_name)
                vendor_name = MANUAL_EXPENSE_VENDORS[cat_name]
                vendor = vendors.get(vendor_name) or Vendor.objects.filter(name=vendor_name).first()
                day = today - timedelta(days=random.randint(0, 55))
                Expense.objects.create(
                    date=day, category=cat, vendor=vendor_name, vendor_link=vendor,
                    amount=Decimal(str(round(random.uniform(35, 900), 2))),
                    note=DEMO_EXPENSE_NOTE, reference=f"R-{5100 + i}",
                    paid_date=day, paid_from=None, payment_method="personal",
                    source="manual", is_reviewed=True, created_by=admin)
            self.stdout.write("expenses: 18 hand-entered (paid personally by the owner)")
        # Only rows this seed stamped are ever back-filled; a real open bill the
        # owner typed in is never touched.
        for exp in Expense.objects.filter(source="manual", note=DEMO_EXPENSE_NOTE,
                                          paid_date__isnull=True, bank_txn__isnull=True):
            exp.paid_date = exp.date
            exp.paid_from, exp.payment_method = None, "personal"
            exp.save(update_fields=["paid_date", "paid_from", "payment_method", "updated_at"])

        if not seeded_feed:
            self.stdout.write("bank feed already seeded; skipping transactions/rules/budgets")
            return

        # --- category rules (3) ------------------------------------------------------
        shipping_expense = Account.system("shipping_expense")
        rules = [
            CategoryRule.objects.create(
                name="Pirate Ship labels → Shipping labels & freight", priority=10,
                match_field="any", match_type="contains", pattern="PIRATE SHIP", direction="out",
                action="categorize", category_account=shipping_expense, vendor=vendors["Pirate Ship"],
                auto_confirm=True, created_by=admin),
            CategoryRule.objects.create(
                name="USPS postage → Shipping labels & freight", priority=20,
                match_field="any", match_type="contains", pattern="USPS", direction="out",
                action="categorize", category_account=shipping_expense, vendor=vendors["USPS"],
                auto_confirm=True, created_by=admin),
            CategoryRule.objects.create(
                name="ULINE → Shipping supplies", priority=30,
                match_field="any", match_type="contains", pattern="ULINE", direction="out",
                action="categorize", category_account=category("Shipping supplies").ensure_account(),
                vendor=vendors["ULINE"], auto_confirm=True, created_by=admin),
        ]

        # --- bank transactions ----------------------------------------------------------
        def txn(account, days_ago, amount, description, merchant="", **kw):
            return BankTransaction.objects.create(
                account=account, date=today - timedelta(days=days_ago),
                amount=D(str(amount)).quantize(D("0.01")),
                description=description, merchant_name=merchant, source="csv", **kw)

        sys_acct = Account.system

        # 1. supplier bills: pay the two oldest posted receipts by ACH, leave the
        #    newest open and due soon (shows up under "bills due in 7 days")
        receipts = list(Receipt.objects.filter(is_deleted=False, posted=True).order_by("date", "pk"))
        supplier_paid = D("0.00")
        for r in receipts[:-1]:
            pay_day = r.date + timedelta(days=10)
            if pay_day > today:
                pay_day = today
            days_ago = (today - pay_day).days
            supplier_name = (r.supplier.name if r.supplier else "SUPPLIER").upper()
            t = txn(checking, days_ago, -r.landed_total,
                    f"ACH PMT {supplier_name} INV {r.supplier_invoice_number or r.reference}",
                    merchant=r.supplier.name if r.supplier else "")
            t.link_receipts([r], user=admin)
            supplier_paid += r.landed_total
        if receipts:
            newest = receipts[-1]
            newest.due_date = today + timedelta(days=5)
            newest.save(update_fields=["due_date"])

        # 2. owner funds the inventory build-up from personal savings
        contribution = (supplier_paid + D("20000.00") - D("12500.00"))
        contribution = max(D("25000.00"), (contribution / 5000).to_integral_value() * 5000)
        txn(checking, 88, contribution, "ONLINE TRANSFER FROM PERSONAL SAVINGS ...2210 — CAPITAL",
            ).categorize(sys_acct("owner_contribution"), user=admin,
                         note="Owner capital contribution to fund inventory")

        # 3. customer deposits matched to recorded payments (checks batched 2-3 per deposit)
        payments = list(Payment.objects.filter(is_deleted=False, date__lt=today, deposits__isnull=True)
                        .select_related("customer").order_by("date", "pk"))
        check_batch = []
        deposits = 0

        def flush_checks():
            nonlocal deposits
            if not check_batch:
                return
            dep_day = min(max(p.date for p in check_batch) + timedelta(days=1), today)
            total = sum((p.amount for p in check_batch), D("0.00"))
            t = txn(checking, (today - dep_day).days, total,
                    f"MOBILE CHECK DEPOSIT {len(check_batch)} ITEM(S)" if len(check_batch) > 1
                    else f"MOBILE CHECK DEPOSIT {check_batch[0].reference}")
            t.link_payments(list(check_batch), user=admin)
            deposits += 1
            check_batch.clear()

        for p in payments:
            if p.method == "check":
                check_batch.append(p)
                if len(check_batch) == 3:
                    flush_checks()
                continue
            dep_day = min(p.date + timedelta(days=1), today)
            name = p.customer.business_name.upper()
            desc = {"ach": f"ORIG CO NAME:{name[:22]} ACH CREDIT",
                    "wire": f"WIRE TRANSFER IN {name[:24]}",
                    "zelle": f"ZELLE PAYMENT FROM {name[:24]}"}.get(p.method, f"DEPOSIT {name[:24]}")
            t = txn(checking, (today - dep_day).days, p.amount, desc, merchant=p.customer.business_name)
            t.link_payments([p], user=admin)
            deposits += 1
        flush_checks()

        # 4. online store payouts + interest + bank fees + rent + power + contractor
        for days_ago, amt in [(70, "1245.80"), (40, "980.15"), (10, "1512.40")]:
            txn(checking, days_ago, amt, "SHOPIFY PAYOUT", merchant="Shopify").categorize(
                sys_acct("sales"), user=admin, note="Retail web store payout")
        for days_ago, amt in [(61, "3.12"), (31, "3.05"), (1, "3.40")]:
            txn(checking, days_ago, amt, "INTEREST PAYMENT").categorize(sys_acct("other_income"), user=admin)
        fees_acct = category("Bank & merchant fees").ensure_account()
        for days_ago in (61, 31, 1):
            txn(checking, days_ago, "-15.00", "MONTHLY SERVICE FEE", merchant="Chase").categorize(
                fees_acct, user=admin, vendor=vendors["Chase"])
        rent_acct = category("Rent & storage").ensure_account()
        for days_ago in (75, 45, 14):
            txn(checking, days_ago, "-1200.00", "COMMERCE PARK LLC RENT ACH", merchant="Commerce Park LLC"
                ).categorize(rent_acct, user=admin, vendor=vendors["Commerce Park LLC"], note="Unit 12 rent")
        util_acct = category("Utilities & phone").ensure_account()
        for days_ago, amt in [(68, "-187.40"), (38, "-203.15")]:
            txn(checking, days_ago, amt, "FPL DIRECT DEBIT ELEC PYMT", merchant="FPL").categorize(
                util_acct, user=admin, vendor=vendors["FPL"])
        txn(checking, 8, "-176.88", "FPL DIRECT DEBIT ELEC PYMT", merchant="FPL")          # unreviewed
        labor_acct = category("Contract labor").ensure_account()
        txn(checking, 63, "-150.00", "ZELLE PAYMENT TO MARCUS REYES").categorize(
            labor_acct, user=admin, vendor=vendors["Marcus Reyes (show help)"], note="Show setup help")
        txn(checking, 21, "-150.00", "ZELLE PAYMENT TO MARCUS REYES")                        # unreviewed

        # 5. owner draw
        txn(checking, 50, "-2500.00", "ONLINE TRANSFER TO PERSONAL CHK ...9921").categorize(
            sys_acct("owner_draw"), user=admin, note="Owner draw")

        # 6. monthly card payments: a transfer pair (checking out / card in)
        for days_ago, amt in [(75, "1840.00"), (45, "1250.00"), (15, "975.00")]:
            out = txn(checking, days_ago, f"-{amt}", "CHASE CREDIT CRD AUTOPAY", merchant="Chase")
            inn = txn(card, days_ago, amt, "AUTOMATIC PAYMENT - THANK YOU", merchant="Chase")
            out.mark_transfer(inn, user=admin)

        # 7. card charges: labels, supplies, software, show fees
        pirate = [(86, "23.45"), (79, "41.20"), (72, "18.90"), (65, "67.35"), (58, "112.80"),
                  (44, "38.60"), (37, "55.15"), (23, "29.75"), (9, "84.30"), (2, "46.10")]
        for days_ago, amt in pirate:
            txn(card, days_ago, f"-{amt}", "PIRATE SHIP 844-5553355 PA", merchant="Pirate Ship")
        for days_ago, amt in [(60, "9.65"), (33, "31.40"), (6, "14.20")]:
            txn(card, days_ago, f"-{amt}", "USPS PO 1138400455 TAMPA FL", merchant="USPS")
        for days_ago, amt in [(82, "212.48"), (55, "156.90"), (28, "318.22"), (7, "89.40")]:
            txn(card, days_ago, f"-{amt}", "ULINE *SHIP SUPPLIES 800-295-5510 WI", merchant="ULINE")
        office_acct = category("Office supplies").ensure_account()
        amazon = [(77, "48.97", False), (61, "23.14", False), (40, "131.60", True),
                  (19, "17.99", True), (4, "64.50", True)]
        for days_ago, amt, auto in amazon:
            t = txn(card, days_ago, f"-{amt}", "AMZN Mktp US*2K4TR8XZ1", merchant="Amazon")
            # "auto" = the system learned Amazon → Office supplies; those rows still
            # want a human confirmation (is_reviewed=False on the Expense)
            t.categorize(office_acct, user=None if auto else admin, vendor=vendors["Amazon"], auto=auto)
        software_acct = category("Software & subscriptions").ensure_account()
        for days_ago in (80, 50, 20):
            txn(card, days_ago, "-14.40", "GOOGLE *WORKSPACE_sflhobby", merchant="Google Workspace"
                ).categorize(software_acct, user=admin, vendor=vendors["Google Workspace"])
        for days_ago in (84, 54, 24):
            txn(card, days_ago, "-39.00", "SHOPIFY* 118372917", merchant="Shopify").categorize(
                software_acct, user=admin, vendor=vendors["Shopify"], note="Web store plan")
        shows_acct = category("Trade shows & events").ensure_account()
        for days_ago in (63, 21):
            txn(card, days_ago, "-85.00", "TAMPA BAY CARD SHOW TABLE FEE", merchant="Tampa Bay Sports Card Show"
                ).categorize(shows_acct, user=admin, vendor=vendors["Tampa Bay Sports Card Show"])

        # 8. still waiting for a review (realistic mix on both accounts)
        for days_ago, amt, desc, merchant in [
            (21, "-12.50", "SQ *CARD SHOW CONCESSIONS", "Square"),
            (35, "-48.20", "SHELL OIL 57442 TAMPA FL", "Shell"),
            (11, "-14.86", "CHICK-FIL-A #02841", "Chick-fil-A"),
            (27, "-37.91", "HOME DEPOT #6372", "The Home Depot"),
            (8, "-189.99", "MICRO CENTER #151", "Micro Center"),
        ]:
            txn(card, days_ago, amt, desc, merchant=merchant)
        for days_ago, amt, desc, merchant in [
            (5, "640.00", "ZELLE PAYMENT FROM HOMETOWN HOBBY SHOP", "Hometown Hobby Shop"),
            (42, "-200.00", "ATM WITHDRAWAL 00231 TAMPA FL", ""),
            (12, "-129.95", "COMCAST BUSINESS 800-391-3000", "Comcast"),
            (29, "-325.00", "CHECK #1047", ""),
            (6, "-62.30", "PAYPAL *CARDSHOWSUP", "PayPal"),
        ]:
            txn(checking, days_ago, amt, desc, merchant=merchant)

        # 9. run the rules over everything still unreviewed (what an import does)
        applied = 0
        for t in BankTransaction.objects.filter(status=BankTransaction.UNREVIEWED).order_by("date", "pk"):
            for rule in rules:
                if rule.matches(t):
                    t.categorize(rule.category_account, user=None, vendor=rule.vendor,
                                 rule=rule, auto=True)
                    applied += 1
                    break
        unreviewed = BankTransaction.objects.filter(status=BankTransaction.UNREVIEWED).count()
        self.stdout.write(f"bank transactions: {BankTransaction.objects.count()} "
                          f"({deposits} deposits matched to payments, {applied} categorized by rules, "
                          f"{unreviewed} left to review)")

        # --- recurring expense -----------------------------------------------------------
        first_next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        RecurringExpense.objects.get_or_create(name="Google Workspace", defaults=dict(
            vendor_link=vendors["Google Workspace"], category=category("Software & subscriptions"),
            amount=D("14.40"), frequency="monthly", next_date=first_next_month, paid_from=card,
            auto_mark_paid=False, note="Business email + Drive"))

        # --- mileage ------------------------------------------------------------------------
        rate = CompanySettings.get().mileage_rate
        for days_ago, miles, purpose, frm, to, rt in [
            (72, "19.0", "Tampa Bay Card Show — table setup", "Warehouse", "Florida State Fairgrounds", True),
            (58, "12.5", "USPS drop-off, 14 boxes", "Warehouse", "USPS Hillsborough Ave", True),
            (44, "61.0", "Pick up allocation at GTS Distribution", "Warehouse", "GTS Distribution, Lakeland", True),
            (30, "84.0", "Orlando card show (buy singles / meet shops)", "Warehouse", "Orlando", True),
            (16, "9.0", "Bank deposit + ULINE will-call", "Warehouse", "Chase branch / ULINE", True),
            (4, "24.0", "Local delivery — Rookie Season Collectibles", "Warehouse", "Rookie Season, Tampa", False),
        ]:
            MileageEntry.objects.create(date=today - timedelta(days=days_ago), miles=D(miles),
                                        purpose=purpose, from_location=frm, to_location=to,
                                        round_trip=rt, rate=rate, vehicle="Ford Transit Connect",
                                        created_by=admin)

        # --- budgets ------------------------------------------------------------------------
        for cat_name, yearly, monthly in [("Shipping supplies", "4800.00", "400.00"),
                                          ("Software & subscriptions", "1200.00", "100.00"),
                                          ("Advertising & marketing", "2400.00", "200.00"),
                                          ("Trade shows & events", "3000.00", "250.00")]:
            cat = category(cat_name)
            Budget.objects.get_or_create(category=cat, year=today.year, month=None,
                                         defaults={"amount": D(yearly)})
            if cat.monthly_budget is None:
                cat.monthly_budget = D(monthly)
                cat.save(update_fields=["monthly_budget"])

        # --- settings -----------------------------------------------------------------------
        cs = CompanySettings.get()
        changed = []
        if cs.books_start_date is None:
            cs.books_start_date = checking.opening_date
            changed.append("books_start_date")
        if not cs.expense_review_digest:
            cs.expense_review_digest = True
            changed.append("expense_review_digest")
        if changed:
            cs.save(update_fields=changed + ["updated_at"])
        self.stdout.write(f"rules: {CategoryRule.objects.count()}, recurring: 1, mileage: 6, "
                          f"budgets: {Budget.objects.count()}, books start {cs.books_start_date}")

    # ------------------------------------------------------------------------
    # Legacy clean-up
    # ------------------------------------------------------------------------
    def fold_legacy_categories(self, admin):
        """Pre-4.0 demo seeds created short category names ("Rent", "Fees"…)
        that now sit next to the setup_accounts defaults ("Rent & storage",
        "Bank & merchant fees"). Move everything that points at a legacy
        duplicate to the default category and soft-delete the duplicate so
        the demo has one of each. The expenses moved this way were created by
        the old seed with no payment info; they are marked paid personally on
        their date (nothing else is back-filled)."""
        from accounting.models import Account
        from banking.models import BankTransaction, BankTransactionSplit, CategoryRule
        from expenses.models import Budget, RecurringExpense, Vendor

        folded = 0
        for legacy, default_name in LEGACY_CATEGORY_NAMES.items():
            dup = ExpenseCategory.objects.filter(name__iexact=legacy).first()
            if dup is None:
                continue
            target = ExpenseCategory.objects.filter(name__iexact=default_name).first()
            if target is None:                      # no default yet: just rename
                dup.name = default_name
                dup.save()
                folded += 1
                continue
            if target.pk == dup.pk:
                continue
            target.ensure_account()
            moved_ids = list(Expense.all_objects.filter(category=dup).values_list("pk", flat=True))
            Expense.all_objects.filter(pk__in=moved_ids).update(category=target)
            for exp in Expense.objects.filter(pk__in=moved_ids, source="manual",
                                              paid_date__isnull=True, bank_txn__isnull=True):
                exp.paid_date = exp.date
                exp.paid_from, exp.payment_method = None, "personal"
                exp.note = exp.note or DEMO_EXPENSE_NOTE
                exp.save(update_fields=["paid_date", "paid_from", "payment_method", "note",
                                        "updated_at"])
            Vendor.all_objects.filter(default_category=dup).update(default_category=target)
            RecurringExpense.all_objects.filter(category=dup).update(category=target)
            for b in Budget.objects.filter(category=dup):
                if Budget.objects.filter(category=target, year=b.year, month=b.month).exists():
                    b.delete()
                else:
                    b.category = target
                    b.save(update_fields=["category"])
            if dup.account_id and target.account_id:
                CategoryRule.all_objects.filter(category_account=dup.account_id).update(
                    category_account=target.account_id)
                BankTransaction.all_objects.filter(category_account=dup.account_id).update(
                    category_account=target.account_id)
                BankTransaction.all_objects.filter(suggested_account=dup.account_id).update(
                    suggested_account=target.account_id)
                BankTransactionSplit.objects.filter(account=dup.account_id).update(
                    account=target.account_id)
                Account.objects.filter(pk=dup.account_id).update(active=False)
            dup.delete(user=admin)
            folded += 1
        if folded:
            self.stdout.write(f"expense categories: {folded} legacy duplicate(s) folded into the defaults")
