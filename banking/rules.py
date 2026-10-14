"""
Category rules + the two "soft" suggestion passes.

``apply_rules(transactions)`` runs on every freshly imported/synced row and
on demand from the inbox:

1. **Rules** in priority order (lower number first). The first rule that
   matches decides:
   * categorize + auto_confirm  -> categorized for real (an Expense row is
     created for expense categories), ``rule_applied`` set,
   * categorize, no auto_confirm -> only ``suggested_account`` (and vendor)
     is set; a person confirms in the inbox,
   * ignore                     -> marked ignored (not business),
   * transfer                   -> paired with its twin in another account
     when one exists, otherwise left for review with a suggestion.
2. **Vendor recognition** for rows no rule settled: a saved Vendor whose
   name or aliases appear in the description pre-fills the vendor and its
   default category as the suggestion.
3. **Learned suggestions**: the category most often chosen before for the
   same (normalised) description in the same account.

Returns a dict of counts so callers can tell the owner what happened.
"""
import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from banking.models import BankTransaction, CategoryRule, normalize_description

LEARN_SCAN_LIMIT = 3000   # categorized rows scanned per account when learning


def _empty_counts():
    return {"scanned": 0, "auto": 0, "suggested": 0, "ignored": 0, "transfers": 0,
            "vendor": 0, "learned": 0, "unmatched": 0}


def find_transfer_twin(txn, days=3, include_flagged=True):
    """Opposite sign, same |amount|, another account, within ``days``, still
    open (unreviewed, or transfer-flagged without a peer)."""
    qs = (BankTransaction.objects
          .filter(is_deleted=False, amount=-txn.amount,
                  date__gte=txn.date - timedelta(days=days),
                  date__lte=txn.date + timedelta(days=days))
          .exclude(account_id=txn.account_id).exclude(pk=txn.pk)
          .select_related("account"))
    if include_flagged:
        from django.db.models import Q
        qs = qs.filter(Q(status=BankTransaction.UNREVIEWED)
                       | Q(status=BankTransaction.TRANSFER, transfer_peer__isnull=True))
    else:
        qs = qs.filter(status=BankTransaction.UNREVIEWED)
    return list(qs.order_by("date")[:5])


def _bump(rule):
    CategoryRule.objects.filter(pk=rule.pk).update(
        times_applied=F("times_applied") + 1, last_applied_at=timezone.now())


def _apply_rule(txn, rule, user, counts):
    """Apply one matching rule. Returns True when the row is settled (no
    further suggestion passes needed)."""
    if rule.action == "ignore":
        txn.rule_applied = rule
        txn.auto_categorized = True
        txn.mark_ignored(user=user, note=f"Rule: {rule.name}"[:500])
        _bump(rule)
        counts["ignored"] += 1
        return True
    if rule.action == "transfer":
        twins = find_transfer_twin(txn)
        if len(twins) == 1 or (twins and all(t.account_id == twins[0].account_id for t in twins)):
            txn.rule_applied = rule
            txn.auto_categorized = True
            txn.mark_transfer(twins[0], user=user)
            _bump(rule)
            counts["transfers"] += 1
            return True
        changed = False
        if rule.category_account_id and txn.suggested_account_id != rule.category_account_id:
            txn.suggested_account = rule.category_account
            changed = True
        if not txn.notes:
            txn.notes = f"Looks like a transfer ({rule.name}) — pick the matching transaction."[:500]
            changed = True
        if changed:
            txn.save(update_fields=["suggested_account", "notes"])
            _bump(rule)
        counts["suggested"] += 1
        return True
    # categorize
    if rule.category_account_id is None:
        return False
    if rule.auto_confirm:
        txn.categorize(rule.category_account, user=user, vendor=rule.vendor, rule=rule, auto=True)
        counts["auto"] += 1
        return True
    changed = False
    if txn.suggested_account_id != rule.category_account_id:
        txn.suggested_account = rule.category_account
        changed = True
    if rule.vendor_id and txn.vendor_id is None:
        txn.vendor = rule.vendor
        changed = True
    if changed:
        txn.save(update_fields=["suggested_account", "vendor"])
        _bump(rule)
    counts["suggested"] += 1
    return True


def _learned_index(account_ids):
    """{(account_id, normalized description): Counter(category_account_id)}
    built from rows a person (or a confirmed rule) categorized before."""
    index = defaultdict(Counter)
    for acct_id in account_ids:
        # CATEGORIZED only: MATCHED rows carry system accounts (undeposited
        # funds, inventory purchases) that are not in the review dropdown,
        # so a suggestion learned from them could never be selected.
        qs = (BankTransaction.objects
              .filter(account_id=acct_id, is_deleted=False,
                      status=BankTransaction.CATEGORIZED,
                      category_account__isnull=False)
              .order_by("-id")
              .values_list("description", "category_account_id")[:LEARN_SCAN_LIMIT])
        for desc, cat_id in qs:
            key = normalize_description(desc)
            if key:
                index[(acct_id, key)][cat_id] += 1
    return index


def apply_rules(transactions, user=None):
    """Run rules + suggestion passes over ``transactions`` (iterable or
    queryset). Only unreviewed, live rows are touched. Returns counts."""
    from accounting.models import Account
    from expenses.models import Vendor

    counts = _empty_counts()
    txns = [t for t in transactions
            if t.status == BankTransaction.UNREVIEWED and not t.is_deleted]
    if not txns:
        return counts
    counts["scanned"] = len(txns)
    rules = list(CategoryRule.objects.filter(active=True).select_related(
        "category_account", "vendor").order_by("priority", "name"))
    vendors = list(Vendor.objects.filter(active=True).select_related("default_category"))

    leftovers = []
    for txn in txns:
        settled = False
        for rule in rules:
            if rule.matches(txn):
                with transaction.atomic():
                    settled = _apply_rule(txn, rule, user, counts)
                if settled:
                    break
        if not settled:
            leftovers.append(txn)

    # vendor recognition
    still_open = []
    for txn in leftovers:
        if txn.suggested_account_id:
            still_open.append(txn)
            continue
        hay = f"{txn.description} {txn.merchant_name} {txn.original_description}"
        vendor = next((v for v in vendors if v.matches(hay)), None)
        if vendor is not None:
            fields = []
            if txn.vendor_id is None:
                txn.vendor = vendor
                fields.append("vendor")
            if vendor.default_category_id:
                acct = vendor.default_category.ensure_account()
                if txn.suggested_account_id != acct.pk:
                    txn.suggested_account = acct
                    fields.append("suggested_account")
            if fields:
                txn.save(update_fields=fields)
            if vendor.default_category_id:
                counts["vendor"] += 1
                continue
        still_open.append(txn)

    # learned suggestions
    unmatched = []
    if still_open:
        index = _learned_index({t.account_id for t in still_open})
        account_cache = {}
        for txn in still_open:
            if txn.suggested_account_id:
                continue
            votes = index.get((txn.account_id, normalize_description(txn.description)))
            if not votes:
                unmatched.append(txn)
                continue
            cat_id = votes.most_common(1)[0][0]
            if cat_id not in account_cache:
                account_cache[cat_id] = Account.objects.filter(pk=cat_id, active=True).first()
            acct = account_cache[cat_id]
            if acct is None:
                unmatched.append(txn)
                continue
            txn.suggested_account = acct
            txn.save(update_fields=["suggested_account"])
            counts["learned"] += 1
    counts["unmatched"] = len(unmatched)
    return counts


def apply_single_rule(rule, user=None, limit=5000):
    """Apply one rule to every unreviewed transaction it matches (used right
    after 'always do this' creates a rule). Returns number settled/suggested."""
    counts = _empty_counts()
    qs = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
    if rule.account_filter_id:
        qs = qs.filter(account_id=rule.account_filter_id)
    if rule.direction == "out":
        qs = qs.filter(amount__lt=0)
    elif rule.direction == "in":
        qs = qs.filter(amount__gt=0)
    n = 0
    for txn in qs.select_related("account")[:limit]:
        if rule.matches(txn):
            with transaction.atomic():
                if _apply_rule(txn, rule, user, counts):
                    n += 1
    return n


def test_rule(rule, limit=10):
    """How many unreviewed rows the (unsaved) rule would hit, plus a sample."""
    qs = BankTransaction.objects.filter(is_deleted=False, status=BankTransaction.UNREVIEWED)
    if rule.account_filter_id:
        qs = qs.filter(account_id=rule.account_filter_id)
    hits, sample = 0, []
    for txn in qs.select_related("account").order_by("-date")[:5000]:
        if rule.matches(txn):
            hits += 1
            if len(sample) < limit:
                sample.append(txn)
    return hits, sample


def clean_pattern(text):
    """The longest digit-free stretch of a description, so the rule still
    matches next month when the reference number changes:
    'PIRATE SHIP 8449500000 CA' -> 'PIRATE SHIP'; 'SQ *COFFEE 1234 TAMPA'
    -> 'SQ *COFFEE'. Falls back to the whole (collapsed) text."""
    text = re.sub(r"\s+", " ", text or "").strip()
    chunks = [c.strip(" -#/:,.") for c in re.split(r"\d+", text)]
    chunks = [c for c in chunks if len(c) >= 3]
    best = max(chunks, key=len) if chunks else text
    return best[:200]


# Words that name a payment rail, a marketplace or a bank's own boilerplate
# rather than a merchant. A pattern made only of these (plus numbers and
# punctuation) would match half the statement, so it is never auto-applied.
GENERIC_WORDS = frozenset("""
paypal pp sq square venmo zelle cash app cashapp apple pay applepay google googlepay gpay
amazon amzn amz mktp mktplace marketplace ebay stripe str shopify
check chk cheque payment pmt pymt pay deposit dep transfer xfer trnsfr ach pos debit dbt credit crd
purchase online withdrawal wdl atm card visa mastercard mc amex discover
fee fees charge bill mobile web recurring autopay auto thank you
id ref reference number no trans transaction txn tran electronic epay eft wire
from to the of and for at in on by us usa com net www inc llc co ltd
""".split())
MIN_SPECIFIC_LEN = 8


def _words(text):
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w]


def is_generic_pattern(pattern):
    """True when ``pattern`` would not single out one merchant: it is only a
    payment processor / marketplace / bank boilerplate (PAYPAL, SQ *, ZELLE
    PAYMENT, ONLINE TRANSFER, ACH DEBIT, CHECK ...), or it is short
    (< 8 chars) with fewer than two words."""
    text = (pattern or "").strip()
    if not text:
        return True
    words = _words(text)
    if not words:
        return True
    if len(text) < MIN_SPECIFIC_LEN and len(words) < 2:
        return True
    specific = [w for w in words if w not in GENERIC_WORDS and not w.isdigit() and len(w) >= 3]
    return not specific


def suggested_pattern(txn):
    """(pattern, match_field, generic) that 'always categorize like this'
    would create for ``txn``. Merchant name first, then the digit-free core
    of the description; ``generic`` is True when neither names a merchant."""
    candidates = []
    merchant = (txn.merchant_name or "").strip()[:200]
    if merchant:
        candidates.append((merchant, "merchant"))
    core = clean_pattern(txn.description)
    if core:
        candidates.append((core, "description"))
    whole = (txn.description or "").strip()[:200]
    if whole:
        candidates.append((whole, "description"))
    for pattern, field in candidates:
        if not is_generic_pattern(pattern):
            return pattern, field, False
    if candidates:
        pattern, field = candidates[0] if not merchant else candidates[-1]
        return pattern, field, True
    return "", "description", True


def _field_for_pattern(txn, pattern):
    p = pattern.lower()
    if p and p in (txn.merchant_name or "").lower():
        return "merchant"
    if p and (p in (txn.description or "").lower() or p in (txn.original_description or "").lower()):
        return "description"
    return "any"


def rule_from_transaction(txn, account, vendor=None, scope="all", user=None, auto_confirm=True,
                          action="categorize", pattern=None):
    """Create (or reuse) a 'contains' rule from a reviewed transaction. Used
    by the inbox 'always categorize like this' checkbox.

    scope: 'all' -> every bank account; 'account' -> only txn.account.
    pattern: text the owner typed over the suggestion (optional).

    A *generic* pattern (payment processor, marketplace, bank boilerplate,
    or something very short) becomes a suggest-only rule (auto_confirm
    False) so it never silently recategorizes half the statement. The
    returned rule carries ``generic_pattern`` (bool, not stored) so callers
    can skip bulk-applying it and tell the owner."""
    pattern = (pattern or "").strip()[:200]
    if pattern:
        match_field = _field_for_pattern(txn, pattern)
        generic = is_generic_pattern(pattern)
    else:
        pattern, match_field, generic = suggested_pattern(txn)
    if not pattern:
        pattern, match_field, generic = txn.description.strip()[:200], "description", True
    if generic:
        auto_confirm = False
    direction = "out" if txn.amount < 0 else "in"
    account_filter = txn.account if scope == "account" else None
    existing = CategoryRule.objects.filter(
        pattern__iexact=pattern, match_type="contains", action=action,
        category_account=account if action == "categorize" else None,
        account_filter=account_filter).first()
    if existing is not None:
        changed = []
        if not existing.active:
            existing.active = True
            changed.append("active")
        if vendor is not None and existing.vendor_id != vendor.pk:
            existing.vendor = vendor
            changed.append("vendor")
        if existing.direction != "any" and existing.direction != direction:
            existing.direction = "any"
            changed.append("direction")
        if changed:
            existing.save(update_fields=changed)
        existing.generic_pattern = generic
        return existing
    target = account.name if account is not None else dict(CategoryRule.ACTIONS).get(action, action)
    name = f"{pattern[:60]} → {target}"[:150]
    rule = CategoryRule.objects.create(
        name=name, pattern=pattern, match_field=match_field, match_type="contains",
        direction=direction, account_filter=account_filter, action=action,
        category_account=account if action == "categorize" else None,
        vendor=vendor, auto_confirm=auto_confirm,
        created_by=user if getattr(user, "is_authenticated", False) else None)
    rule.generic_pattern = generic
    return rule
