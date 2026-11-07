"""
Starter category rules — the merchants a sports-card / TCG distributor sees
on a Chase business checking and an Amex business card every month.

Each entry: (name, pattern, direction, action, target, auto_confirm)
  * target is a system-account key ("shipping_expense", "inventory_purchases",
    "other_income", "sales", "owner_draw", …) or ("category", "Expense
    category name") for an expense category (created if missing).
  * auto_confirm=True rules categorize for real; False only suggest, a
    person confirms in the inbox. Anything ambiguous (Amazon, Walmart) is
    suggest-only.

Installed from Banking → Rules → "Add starter rules". Idempotent: a rule
whose name already exists is skipped, and a pattern you removed before is
not re-added unless you tick "re-add removed".
"""

STARTER_RULES = [
    # --- credit-card payments & transfers between your own accounts -------
    ("Amex payment from checking", "american express", "out", "transfer", None, True),
    ("Amex payment from checking (ACH)", "amex epayment", "out", "transfer", None, True),
    ("Card payment received (Amex side)", "payment received - thank you", "in", "transfer", None, True),
    ("Card payment received (autopay)", "autopay payment - thank you", "in", "transfer", None, True),
    ("Card payment received (mobile)", "mobile payment - thank you", "in", "transfer", None, True),
    ("Chase card autopay", "chase credit crd", "out", "transfer", None, True),
    ("Online transfer between accounts", "online transfer", "any", "transfer", None, True),

    # --- shipping -----------------------------------------------------------
    ("Shippo labels", "shippo", "out", "categorize", "shipping_expense", True),
    ("Pirate Ship labels", "pirate ship", "out", "categorize", "shipping_expense", True),
    ("Pirate Ship labels (alt)", "pirateship", "out", "categorize", "shipping_expense", True),
    ("USPS postage", "usps", "out", "categorize", "shipping_expense", True),
    ("UPS shipping", "ups", "out", "categorize", "shipping_expense", False),
    ("FedEx shipping", "fedex", "out", "categorize", "shipping_expense", True),
    ("Stamps.com postage", "stamps.com", "out", "categorize", "shipping_expense", True),
    ("EasyPost labels", "easypost", "out", "categorize", "shipping_expense", True),
    ("ShipStation", "shipstation", "out", "categorize", ("category", "Software & subscriptions"), True),

    # --- inventory purchases -------------------------------------------------
    ("Tradepost purchases", "tradepost", "out", "categorize", "inventory_purchases", True),
    ("TCGplayer purchases", "tcgplayer", "out", "categorize", "inventory_purchases", False),
    ("GTS Distribution", "gts distribution", "out", "categorize", "inventory_purchases", True),
    ("Southern Hobby", "southern hobby", "out", "categorize", "inventory_purchases", True),
    ("Pokemon Center", "pokemon center", "out", "categorize", "inventory_purchases", True),
    ("Dave & Adam's", "dave and adam", "out", "categorize", "inventory_purchases", True),
    ("Dave & Adam's (alt)", "dacardworld", "out", "categorize", "inventory_purchases", True),
    ("Blowout Cards", "blowout cards", "out", "categorize", "inventory_purchases", True),
    ("Steel City Collectibles", "steel city", "out", "categorize", "inventory_purchases", True),
    ("Magazine Exchange", "magazine exchange", "out", "categorize", "inventory_purchases", True),
    ("Alliance / Diamond distribution", "alliance game", "out", "categorize", "inventory_purchases", True),
    ("ACD Distribution", "acd distribution", "out", "categorize", "inventory_purchases", True),
    ("Topps direct", "topps", "out", "categorize", "inventory_purchases", True),
    ("Panini direct", "panini", "out", "categorize", "inventory_purchases", True),
    ("Fanatics Collectibles", "fanatics", "out", "categorize", "inventory_purchases", True),
    ("Upper Deck", "upper deck", "out", "categorize", "inventory_purchases", True),
    ("Leaf Trading Cards", "leaf trading", "out", "categorize", "inventory_purchases", True),
    ("Walmart purchases", "walmart", "out", "categorize", "inventory_purchases", True),
    ("Walmart purchases (alt)", "wal-mart", "out", "categorize", "inventory_purchases", True),
    ("Walmart purchases (alt 2)", "wm supercenter", "out", "categorize", "inventory_purchases", True),
    ("Target purchases", "target", "out", "categorize", "inventory_purchases", True),
    ("Whatnot purchases", "whatnot", "out", "categorize", "inventory_purchases", False),
    ("eBay purchases", "ebay", "out", "categorize", "inventory_purchases", False),

    # --- selling platform & processor fees -----------------------------------
    ("Stripe fees", "stripe", "out", "categorize", ("category", "Merchant & processing fees"), True),
    ("Square fees", "square inc", "out", "categorize", ("category", "Merchant & processing fees"), True),
    ("PayPal fees", "paypal", "out", "categorize", ("category", "Merchant & processing fees"), False),
    ("Shopify subscription", "shopify", "out", "categorize", ("category", "Software & subscriptions"), False),

    # --- payouts from selling platforms (money in) ---------------------------
    ("Stripe payout", "stripe", "in", "categorize", "sales", False),
    ("Square payout", "square inc", "in", "categorize", "sales", False),
    ("Shopify payout", "shopify", "in", "categorize", "sales", False),
    ("Whatnot payout", "whatnot", "in", "categorize", "sales", False),
    ("eBay payout", "ebay", "in", "categorize", "sales", False),
    ("PayPal transfer in", "paypal", "in", "categorize", "sales", False),
    ("Tradepost payout (sold on Tradepost)", "tradepost", "in", "categorize", "sales", False),

    # --- software & services -------------------------------------------------
    ("Google Workspace", "google", "out", "categorize", ("category", "Software & subscriptions"), False),
    ("Microsoft 365", "microsoft", "out", "categorize", ("category", "Software & subscriptions"), False),
    ("QuickBooks", "intuit", "out", "categorize", ("category", "Software & subscriptions"), True),
    ("Canva", "canva", "out", "categorize", ("category", "Software & subscriptions"), True),
    ("Adobe", "adobe", "out", "categorize", ("category", "Software & subscriptions"), True),
    ("Zoom", "zoom.us", "out", "categorize", ("category", "Software & subscriptions"), True),
    ("Railway hosting", "railway", "out", "categorize", ("category", "Software & subscriptions"), True),
    ("GoDaddy / domains", "godaddy", "out", "categorize", ("category", "Software & subscriptions"), True),

    # --- supplies & packaging ------------------------------------------------
    ("Uline supplies", "uline", "out", "categorize", ("category", "Supplies & packaging"), True),
    ("Amazon (check what it was)", "amazon", "out", "categorize", ("category", "Supplies & packaging"), False),
    ("Amazon (alt)", "amzn", "out", "categorize", ("category", "Supplies & packaging"), False),
    ("Home Depot", "home depot", "out", "categorize", ("category", "Supplies & packaging"), False),
    ("Office Depot", "office depot", "out", "categorize", ("category", "Supplies & packaging"), True),
    ("Staples", "staples", "out", "categorize", ("category", "Supplies & packaging"), True),
    ("BCW supplies", "bcw", "out", "categorize", ("category", "Supplies & packaging"), True),
    ("Ultra Pro supplies", "ultra pro", "out", "categorize", ("category", "Supplies & packaging"), True),

    # --- bank & card fees, interest ------------------------------------------
    ("Chase monthly service fee", "monthly service fee", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Chase wire fee", "wire fee", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Amex annual fee", "annual membership fee", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Amex interest", "interest charge", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Late fee", "late fee", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Foreign transaction fee", "foreign transaction fee", "out", "categorize", ("category", "Bank fees & interest"), True),
    ("Interest earned", "interest payment", "in", "categorize", "other_income", True),
    ("Amex cash back / credit", "membership rewards", "in", "categorize", "other_income", False),
    ("Statement credit", "statement credit", "in", "categorize", "other_income", False),

    # --- taxes, insurance, government ----------------------------------------
    ("IRS payment", "irs usataxpymt", "out", "categorize", ("category", "Taxes & licenses"), False),
    ("State tax / Dept of Revenue", "dept of revenue", "out", "categorize", ("category", "Taxes & licenses"), False),
    ("Sunbiz / state filing", "sunbiz", "out", "categorize", ("category", "Taxes & licenses"), True),
    ("Business insurance", "insurance", "out", "categorize", ("category", "Insurance"), False),

    # --- vehicle -------------------------------------------------------------
    ("Tesla Supercharging", "tesla supercharger", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("Tesla Supercharging (alt)", "tesla, inc", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("Tesla Supercharging (alt 2)", "tesla inc", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("Tesla (app / charging)", "tesla motors", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("EV charging (ChargePoint)", "chargepoint", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("EV charging (Electrify America)", "electrify america", "out", "categorize", ("category", "Vehicle & charging"), True),
    ("EV charging (EVgo)", "evgo", "out", "categorize", ("category", "Vehicle & charging"), True),

    # --- travel, shows, meals ------------------------------------------------
    ("Gas stations", "shell oil", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Gas stations (Wawa)", "wawa", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Gas stations (Exxon)", "exxon", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Hotels", "marriott", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Hotels (Hilton)", "hilton", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Airlines (Southwest)", "southwest", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Airlines (American)", "american air", "out", "categorize", ("category", "Travel & mileage"), False),
    ("Card show / convention fees", "eventbrite", "out", "categorize", ("category", "Advertising & shows"), False),
    ("Meals (Starbucks)", "starbucks", "out", "categorize", ("category", "Meals"), False),
    ("Meals (Chick-fil-A)", "chick-fil-a", "out", "categorize", ("category", "Meals"), False),

    # --- advertising ---------------------------------------------------------
    ("Facebook / Meta ads", "facebk", "out", "categorize", ("category", "Advertising & shows"), True),
    ("Facebook / Meta ads (alt)", "meta platforms", "out", "categorize", ("category", "Advertising & shows"), True),
    ("Google Ads", "google ads", "out", "categorize", ("category", "Advertising & shows"), True),
    ("Vistaprint", "vistaprint", "out", "categorize", ("category", "Advertising & shows"), True),

    # --- owner money ---------------------------------------------------------
    ("Zelle out (owner draw — check)", "zelle payment to", "out", "categorize", "owner_draw", False),
    ("Zelle in (owner contribution — check)", "zelle payment from", "in", "categorize", "owner_contribution", False),
    ("ATM withdrawal", "atm withdrawal", "out", "categorize", "owner_draw", False),
]

# Built-in "looks like a card payment / transfer" hints used by the smart
# pass even before any rule exists. Lower-case substrings.
TRANSFER_HINTS = (
    "american express", "amex epayment", "payment - thank you", "payment received",
    "autopay", "chase credit crd", "cardmember serv", "online transfer", "transfer to",
    "transfer from", "capital one autopay", "discover e-payment", "citi autopay",
    "online payment thank you",
)


# Starter rules from earlier versions that were replaced. They are retired
# (soft-deleted) on install so the new definition takes over — unless the
# owner edited them, in which case they are left alone.
SUPERSEDED = {
    "Walmart (check what it was)": "Walmart purchases",
    "Target (check what it was)": "Target purchases",
}


def install(user=None, readd_removed=False):
    """Create the starter rules. Returns (created, skipped). Re-running adds
    any starter rules introduced since, and retires superseded ones."""
    from accounting.models import Account
    from banking.models import CategoryRule
    from expenses.models import ExpenseCategory
    from django.utils import timezone

    for old_name in SUPERSEDED:
        for rule in CategoryRule.objects.filter(name=old_name, priority=200, times_applied=0):
            rule.is_deleted, rule.deleted_at = True, timezone.now()
            rule.save(update_fields=["is_deleted", "deleted_at"])

    existing = set(CategoryRule.all_objects.values_list("name", flat=True)) if not readd_removed \
        else set(CategoryRule.objects.values_list("name", flat=True))
    created = skipped = 0
    cat_cache = {}
    for name, pattern, direction, action, target, auto in STARTER_RULES:
        if name in existing:
            skipped += 1
            continue
        account = None
        if action == "categorize":
            if isinstance(target, tuple):
                cat_name = target[1]
                if cat_name not in cat_cache:
                    cat, _ = ExpenseCategory.objects.get_or_create(name=cat_name)
                    cat_cache[cat_name] = cat.ensure_account()
                account = cat_cache[cat_name]
            else:
                account = Account.system(target)
        CategoryRule.objects.create(
            name=name, pattern=pattern, direction=direction, action=action,
            category_account=account, auto_confirm=auto, priority=200,   # after your own rules
            match_field="any", match_type="contains",
            created_by=user if getattr(user, "is_authenticated", False) else None)
        created += 1
    return created, skipped
