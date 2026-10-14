"""
Seed realistic demo data: 30 SKUs, 10 customers, 20 POs in various statuses,
plus receipts, invoices, payments, expenses — so every screen has life in it.

    python manage.py seed_demo            # adds demo data (idempotent-ish)
    python manage.py seed_demo --wipe     # flush first (DANGER: dev only)
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import CompanySettings, NumberSequence
from customers.models import Contact, Customer, ResaleCertificate, ShippingAddress
from expenses.models import Expense, ExpenseCategory
from inventory.models import (Brand, Category, PriceList, PriceListItem,
                              Product, Receipt, ReceiptLine, Supplier)
from invoicing.models import Invoice
from orders.models import POLine, PurchaseOrder
from payments.models import Payment

random.seed(42)

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


class Command(BaseCommand):
    help = "Load realistic demo data for testing."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true")

    def handle(self, *args, **options):
        if options["wipe"]:
            call_command("flush", "--noinput")
        call_command("setup_roles")

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
        # attach role profiles
        from core.models import Role, UserProfile
        for u, role_name in [(admin, "Admin"), (office, "Office"),
                             (picker, "Warehouse"), (driver, "Fulfillment Manager")]:
            role = Role.objects.filter(name=role_name).first()
            profile, _ = UserProfile.objects.get_or_create(user=u)
            if role and not profile.role_id:
                profile.role = role
                profile.save(update_fields=["role"])
        self.stdout.write("users: admin / office / picker / driver (password: sflerp123)")

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
                receipt = Receipt.objects.create(
                    supplier=random.choice(suppliers),
                    date=timezone.localdate() - timedelta(days=45 - wk * 15),
                    reference=f"SUP-{7000 + wk}",
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
                        unit_cost=(Decimal(str(cost)) * jitter).quantize(Decimal("0.0001")))
                receipt.post(user=admin)
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
                exp = timezone.localdate() + timedelta(days=12)      # expiring soon
            elif i == 8:
                exp = timezone.localdate() - timedelta(days=20)      # expired
            elif i == 9:
                exp = None                                           # none
            else:
                exp = timezone.localdate() + timedelta(days=200 + i * 30)
            if exp:
                ResaleCertificate.objects.create(
                    customer=c, certificate_number=f"{state}-RC-{2200 + i}",
                    issuing_state=state,
                    issue_date=timezone.localdate() - timedelta(days=300),
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
            finalized = list(PurchaseOrder.objects.filter(status=PurchaseOrder.FINALIZED))
            # consolidate two same-customer POs when possible
            by_cust = {}
            for po in finalized:
                by_cust.setdefault(po.customer_id, []).append(po)
            for cust_id, pos in by_cust.items():
                take = pos[:2]
                try:
                    inv = Invoice.build_from_pos(take, user=office)
                except Exception:
                    continue
                Invoice.objects.filter(pk=inv.pk).update(
                    date=timezone.localdate() - timedelta(days=random.randint(0, 40)))
                inv.refresh_from_db()
                inv.due_date = inv.date + timedelta(days=inv.customer.term_days)
                inv.save(update_fields=["due_date"])
                roll = random.random()
                if roll < 0.4:      # fully paid
                    pay = Payment.objects.create(
                        customer=inv.customer, amount=inv.total,
                        date=inv.date + timedelta(days=random.randint(1, 20)),
                        method=random.choice(["check", "ach", "zelle", "wire"]),
                        reference=f"CHK{random.randint(1000, 9999)}",
                        created_by=office)
                    pay.apply_to_invoice(inv, inv.total, user=office)
                elif roll < 0.7:    # partial
                    part = (inv.total / 2).quantize(Decimal("0.01"))
                    pay = Payment.objects.create(
                        customer=inv.customer, amount=part,
                        date=inv.date + timedelta(days=random.randint(1, 15)),
                        method="check", reference=f"CHK{random.randint(1000, 9999)}",
                        created_by=office)
                    pay.apply_to_invoice(inv, part, user=office)
            self.stdout.write(f"invoices: {Invoice.objects.count()}, "
                              f"payments: {Payment.objects.count()}")

        # --- expenses ---------------------------------------------------------------
        if not Expense.objects.exists():
            cats_exp = ["Shipping supplies", "Software", "Rent", "Labor", "Fees", "Utilities"]
            for name in cats_exp:
                ExpenseCategory.objects.get_or_create(name=name)
            vendors = {"Shipping supplies": "Uline", "Software": "QuickBooks",
                       "Rent": "Commerce Park LLC", "Labor": "Payroll",
                       "Fees": "Stripe", "Utilities": "TECO"}
            for i in range(18):
                cat = random.choice(cats_exp)
                Expense.objects.create(
                    date=timezone.localdate() - timedelta(days=random.randint(0, 55)),
                    category=ExpenseCategory.objects.get(name=cat),
                    vendor=vendors[cat],
                    amount=Decimal(str(round(random.uniform(35, 1800), 2))),
                    note="", created_by=admin)
            self.stdout.write("expenses: 18")

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
                    ship_date=timezone.localdate(),
                    ship_to_text=local_po.ship_to.as_text() if local_po.ship_to else "",
                    created_by=admin)
                self.stdout.write(f"local delivery scheduled today: {local_po.number}")

        try:
            call_command("setup_schedules")
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(
            "\nDemo data ready. Log in as admin / office / picker "
            "(password: sflerp123)."))
