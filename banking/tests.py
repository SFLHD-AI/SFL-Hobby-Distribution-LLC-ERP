"""
Banking module tests: CSV presets and sign normalisation, de-duplication,
rules, vendor + learned suggestions, matching, review actions through the
views, permissions, the fake-provider sync (pending replacement, opening
balance derivation) and the cash position math.
"""
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import Account
from banking import importers, matching
from banking.models import BankAccount, BankConnection, BankTransaction, CategoryRule, ImportBatch
from banking.providers import register_provider, unregister_provider
from banking.providers.base import BankFeedProvider, ProviderError
from banking.providers.plaid import PlaidProvider
from banking.rules import apply_rules, apply_single_rule, rule_from_transaction
from banking.services import cash_position, needs_attention
from banking.tasks import sync_all_connections, sync_connection
from core.models import CompanySettings
from expenses.models import Expense, ExpenseCategory, Vendor

D = Decimal
TODAY = timezone.localdate()
MEDIA = tempfile.mkdtemp(prefix="sfl-banking-tests-")

# ---------------------------------------------------------------------------
# CSV fixtures (dates fixed in 2025; test accounts open 2020-01-01)
# ---------------------------------------------------------------------------
CHASE_CARD = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
01/15/2025,01/16/2025,PIRATE SHIP 8449500000,Shipping,Sale,-45.20,
01/20/2025,01/21/2025,Payment Thank You-Mobile,,Payment,500.00,
01/22/2025,01/23/2025,ULINE SHIP SUPPLIES,Business,Sale,-120.00,
"""
CHASE_CHECKING = """Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,01/15/2025,CHASE CREDIT CRD AUTOPAY,-500.00,ACH_DEBIT,4500.00,
CREDIT,01/17/2025,DEPOSIT ID NUMBER 123,"1,250.00",DEPOSIT,5750.00,
CHECK,01/18/2025,CHECK 1042,-300.00,CHECK,5450.00,1042
"""
# Real Amex export: "Card Member" / "Account #" distinguish it from the many
# banks whose files are a bare Date,Description,Amount.
AMEX = """Date,Description,Card Member,Account #,Amount
01/10/2025,AMAZON MARKETPLACE,JANE OWNER,-31005,89.99
01/12/2025,ONLINE PAYMENT - THANK YOU,JANE OWNER,-31005,-400.00
"""
BARE_THREE = """Date,Description,Amount
01/10/2025,AMAZON MARKETPLACE,-89.99
01/12/2025,DEPOSIT FROM CARD SHOP,400.00
"""
CHECKING_FOUR = """Date,Description,Amount,Balance
01/10/2025,USPS PO 1234,-45.00,1955.00
01/12/2025,ZELLE FROM CARD SHOP,400.00,2355.00
"""
CAPITAL_ONE = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-01-05,2025-01-06,1234,SHOPIFY,Other Services,29.00,
2025-01-08,2025-01-09,1234,CAPITAL ONE MOBILE PYMT,Payment/Credit,,250.00
"""
BOFA = """Description,,Summary Amt.
Beginning balance as of 01/01/2025,,"2,500.00"
Total credits,,"1,000.00"
Total debits,,"-300.00"
Ending balance as of 01/31/2025,,"3,200.00"

Date,Description,Amount,Running Bal.
01/01/2025,Beginning balance as of 01/01/2025,,"2,500.00"
01/10/2025,ZELLE PAYMENT FROM CARD SHOP,"1,000.00","3,500.00"
01/12/2025,USPS PO 1234,-300.00,"3,200.00"
"""
WELLS = """"01/15/2025","-45.00","*","","PIRATE SHIP 844-950-0000 CA"
"01/16/2025","2500.00","*","","ONLINE TRANSFER FROM SAVINGS"
"01/17/2025","-120.50","*","1043","CHECK"
"""
CITI = """Status,Date,Description,Debit,Credit
Cleared,01/15/2025,BEST BUY,199.99,
Cleared,01/16/2025,ONLINE PAYMENT THANK YOU,,-300.00
Pending,01/17/2025,STARBUCKS,5.25,
"""
DISCOVER = """Trans. Date,Post Date,Description,Amount,Category
01/15/2025,01/16/2025,ULINE,75.00,Merchandise
01/18/2025,01/18/2025,INTERNET PAYMENT - THANK YOU,-200.00,Payments and Credits
"""
PAYPAL = """Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,From Email Address,To Email Address,Transaction ID
01/15/2025,10:00:00,PST,Card Shop LLC,Express Checkout Payment,Completed,USD,"1,000.00",-29.30,970.70,buyer@example.com,me@example.com,TXN123
01/16/2025,10:00:00,PST,Pirate Ship,PreApproved Payment Bill User Payment,Completed,USD,-45.00,0.00,-45.00,me@example.com,ps@example.com,TXN124
01/17/2025,10:00:00,PST,Someone,Payment,Pending,USD,10.00,0.00,10.00,,,TXN125
"""
SQUARE = """Date,Time,Time Zone,Gross Sales,Net Sales,Fees,Net Total,Transaction ID,Description,Customer Name
2025-01-15,14:02:11,Eastern Time (US & Canada),250.00,250.00,-6.55,243.45,SQ1,Show table sale,Walk-in
2025-01-16,09:30:00,Eastern Time (US & Canada),0.00,0.00,0.00,-20.00,SQ2,Refund,Walk-in
"""
GENERIC_LATIN1 = "Posted;Payee;Debit;Credit\n2025-01-15;Caf\xe9 Supplies;12.50;\n2025-01-16;Refund from Caf\xe9;;5.00\n".encode("latin-1")


def _parse(text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    parsed = importers.parse_csv(data)
    return parsed


def _mapped(text, preset=None):
    parsed = _parse(text)
    preset = preset or importers.detect_preset(parsed.headers, parsed.rows[0])
    rows = importers.map_rows(parsed.rows, preset, headers=parsed.headers)
    return parsed, preset, rows


def amounts(rows):
    return [r["amount"] for r in rows if not r["error"]]


class FakeProvider(BankFeedProvider):
    name = "fake"

    def __init__(self, accounts=None, pages=None, error=None):
        self.accounts = accounts or []
        self.pages = list(pages or [])
        self.error = error
        self.removed_items = []
        self.cursors = []

    def is_configured(self):
        return True

    def create_link_token(self, user, redirect_uri=None):
        return "link-sandbox-token"

    def exchange_public_token(self, public_token):
        return {"access_token": f"access-{public_token}", "item_id": "item-1"}

    def get_accounts(self, access_token):
        return self.accounts

    def sync_transactions(self, access_token, cursor=""):
        self.cursors.append(cursor)
        if self.error is not None:
            raise self.error
        if not self.pages:
            return {"added": [], "modified": [], "removed": [], "next_cursor": cursor}
        return self.pages.pop(0)

    def remove_item(self, access_token):
        self.removed_items.append(access_token)


def plaid_txn(ext, acct, day, amount, desc, pending=False, pending_ext="", merchant=""):
    return {"external_id": ext, "external_account_id": acct, "date": day,
            "posted_date": None if pending else day, "amount": D(amount), "description": desc,
            "merchant_name": merchant, "pending": pending, "check_number": "",
            "pending_external_id": pending_ext}


@override_settings(MEDIA_ROOT=MEDIA)
class BankingBase(TestCase):
    def setUp(self):
        Account.ensure_system_accounts()
        self.user = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(self.user)
        self.checking = BankAccount.objects.create(
            name="Checking", account_type="checking", opening_balance=D("1000.00"),
            opening_date=date(2020, 1, 1), is_default=True)
        self.card = BankAccount.objects.create(
            name="Visa", account_type="credit_card", opening_balance=D("250.00"),
            opening_date=date(2020, 1, 1))
        self.shipping = ExpenseCategory.objects.create(name="Shipping supplies")
        self.software = ExpenseCategory.objects.create(name="Software")
        self.shipping_acct = self.shipping.ensure_account()
        self.software_acct = self.software.ensure_account()

    def txn(self, account=None, amount="-10.00", desc="SOMETHING", day=None, **kw):
        return BankTransaction.objects.create(account=account or self.card, date=day or TODAY,
                                              amount=D(amount), description=desc, **kw)


# ===========================================================================
# Importers
# ===========================================================================
class PresetParsingTests(BankingBase):
    def test_detects_every_preset(self):
        for text, slug in [(CHASE_CARD, "chase"), (CHASE_CHECKING, "chase"), (AMEX, "amex"),
                           (CAPITAL_ONE, "capital_one"), (BOFA, "bank_of_america"), (CITI, "citi"),
                           (DISCOVER, "discover"), (PAYPAL, "paypal"), (SQUARE, "square")]:
            parsed = _parse(text)
            self.assertEqual(importers.detect_preset(parsed.headers, parsed.rows[0]), slug, text[:30])
        parsed = _parse(WELLS)
        self.assertIsNone(parsed.headers)
        self.assertEqual(importers.detect_preset(parsed.headers, parsed.rows[0]), "wells_fargo")
        self.assertIsNone(importers.detect_preset(["foo", "bar"]))

    def test_chase_card_keeps_bank_signs(self):
        _, preset, rows = _mapped(CHASE_CARD)
        self.assertEqual(preset, "chase")
        self.assertEqual(amounts(rows), [D("-45.20"), D("500.00"), D("-120.00")])
        self.assertEqual(rows[0]["date"], date(2025, 1, 15))
        self.assertEqual(rows[0]["posted_date"], date(2025, 1, 16))
        self.assertEqual(rows[0]["description"], "PIRATE SHIP 8449500000")

    def test_chase_checking_layout_and_check_number(self):
        _, preset, rows = _mapped(CHASE_CHECKING)
        self.assertEqual(preset, "chase")
        self.assertEqual(amounts(rows), [D("-500.00"), D("1250.00"), D("-300.00")])
        self.assertEqual(rows[2]["check_number"], "1042")

    def test_amex_negates_positive_charges(self):
        _, preset, rows = _mapped(AMEX)
        self.assertEqual(preset, "amex")
        self.assertEqual(amounts(rows), [D("-89.99"), D("400.00")])

    def test_capital_one_debit_credit_columns(self):
        _, preset, rows = _mapped(CAPITAL_ONE)
        self.assertEqual(preset, "capital_one")
        self.assertEqual(amounts(rows), [D("-29.00"), D("250.00")])
        self.assertEqual(rows[0]["date"], date(2025, 1, 5))

    def test_wells_fargo_headerless(self):
        parsed, preset, rows = _mapped(WELLS)
        self.assertEqual(preset, "wells_fargo")
        self.assertEqual(amounts(rows), [D("-45.00"), D("2500.00"), D("-120.50")])
        self.assertEqual(rows[0]["description"], "PIRATE SHIP 844-950-0000 CA")
        self.assertEqual(rows[2]["check_number"], "1043")

    def test_bank_of_america_summary_block_is_skipped(self):
        parsed, preset, rows = _mapped(BOFA)
        self.assertEqual(preset, "bank_of_america")
        self.assertEqual(parsed.header_row_index, 5)
        self.assertEqual(amounts(rows), [D("1000.00"), D("-300.00")])
        self.assertEqual(rows[0]["error"], "No valid amount")       # beginning-balance line

    def test_citi_skips_pending_and_handles_negative_credit(self):
        _, preset, rows = _mapped(CITI)
        self.assertEqual(preset, "citi")
        self.assertEqual(amounts(rows), [D("-199.99"), D("300.00")])
        self.assertTrue(rows[2]["error"].startswith("Skipped"))

    def test_discover_negates(self):
        _, _, rows = _mapped(DISCOVER)
        self.assertEqual(amounts(rows), [D("-75.00"), D("200.00")])

    def test_paypal_uses_net_name_and_transaction_id(self):
        _, preset, rows = _mapped(PAYPAL)
        self.assertEqual(preset, "paypal")
        self.assertEqual(amounts(rows), [D("970.70"), D("-45.00")])
        self.assertEqual(rows[0]["merchant_name"], "Card Shop LLC")
        self.assertEqual(rows[0]["external_id"], "TXN123")
        self.assertIn("Card Shop LLC", rows[0]["description"])
        self.assertTrue(rows[2]["error"])

    def test_square_net_total(self):
        _, preset, rows = _mapped(SQUARE)
        self.assertEqual(preset, "square")
        self.assertEqual(amounts(rows), [D("243.45"), D("-20.00")])
        self.assertEqual(rows[0]["external_id"], "SQ1")

    def test_generic_mapping_latin1_semicolon(self):
        parsed = importers.parse_csv(GENERIC_LATIN1)
        self.assertEqual(parsed.delimiter, ";")
        self.assertEqual(parsed.headers, ["posted", "payee", "debit", "credit"])
        mapping = importers.mapping_from_form({"col_date": "0", "col_description": "1", "col_debit": "2",
                                               "col_credit": "3", "positive_is_charge": False})
        rows = importers.map_rows(parsed.rows, mapping)
        self.assertEqual(amounts(rows), [D("-12.50"), D("5.00")])
        self.assertEqual(rows[0]["description"], "Café Supplies")
        # single signed column where positive = charge
        mapping = importers.mapping_from_form({"col_date": "0", "col_description": "1", "col_amount": "2",
                                               "positive_is_charge": True})
        rows = importers.map_rows(parsed.rows, mapping)
        self.assertEqual(rows[0]["amount"], D("-12.50"))
        self.assertEqual(rows[1]["error"], "No valid amount")

    def test_amount_parsing_edge_cases(self):
        self.assertEqual(importers.parse_amount("$1,234.56"), D("1234.56"))
        self.assertEqual(importers.parse_amount("(12.00)"), D("-12.00"))
        self.assertEqual(importers.parse_amount("12.00-"), D("-12.00"))
        self.assertEqual(importers.parse_amount("5 CR"), D("5.00"))
        self.assertIsNone(importers.parse_amount(""))
        self.assertIsNone(importers.parse_amount("abc"))
        self.assertEqual(importers.parse_date("01/15/25"), date(2025, 1, 15))
        self.assertEqual(importers.parse_date("2025-01-15 10:11:12"), date(2025, 1, 15))
        self.assertIsNone(importers.parse_date("yesterday"))

    def test_sign_warning_for_card_file_with_positive_charges(self):
        rows = [{"amount": D("10.00"), "error": ""} for _ in range(6)]
        self.assertIn("Flip signs", importers.sign_warning(self.card, rows))
        self.assertEqual(importers.sign_warning(self.checking, rows[:4]), "")
        rows = [{"amount": D("-10.00"), "error": ""} for _ in range(6)]
        self.assertEqual(importers.sign_warning(self.card, rows), "")


class ImportRowsTests(BankingBase):
    def _import(self, text, account=None, preset=None):
        parsed, slug, rows = _mapped(text, preset)
        return importers.import_rows(account or self.card, rows, user=self.user, preset=slug, filename="x.csv")

    def test_import_creates_transactions_and_counts(self):
        batch = self._import(CHASE_CARD)
        self.assertEqual(batch.rows_total, 3)
        self.assertEqual(batch.rows_imported, 3)
        self.assertEqual(batch.rows_duplicate, 0)
        self.assertEqual(batch.date_min, date(2025, 1, 15))
        self.assertEqual(batch.date_max, date(2025, 1, 22))
        t = BankTransaction.objects.get(description="PIRATE SHIP 8449500000")
        self.assertEqual(t.amount, D("-45.20"))
        self.assertEqual(t.source, "csv")
        self.assertEqual(t.import_batch, batch)
        self.assertEqual(t.status, BankTransaction.UNREVIEWED)
        self.assertTrue(t.fingerprint)

    def test_reimport_skips_duplicates(self):
        self._import(CHASE_CARD)
        batch = self._import(CHASE_CARD)
        self.assertEqual(batch.rows_imported, 0)
        self.assertEqual(batch.rows_duplicate, 3)
        self.assertEqual(BankTransaction.objects.filter(account=self.card).count(), 3)

    def test_external_id_dedupes_even_when_description_changes(self):
        self._import(PAYPAL, account=self.checking)
        changed = PAYPAL.replace("Card Shop LLC", "CARD SHOP LLC (renamed)")
        batch = self._import(changed, account=self.checking)
        self.assertEqual(batch.rows_imported, 0)
        self.assertEqual(batch.rows_duplicate, 2)

    def test_identical_same_day_rows_in_one_file_both_import(self):
        text = ("Date,Description,Amount\n01/10/2025,COFFEE,4.50\n01/10/2025,COFFEE,4.50\n")
        batch = self._import(text, preset="amex")        # bare 3 columns no longer auto-detect
        self.assertEqual(batch.rows_imported, 2)
        again = self._import(text, preset="amex")
        self.assertEqual(again.rows_imported, 0)
        self.assertEqual(again.rows_duplicate, 2)

    def test_rows_before_opening_date_are_skipped(self):
        late = BankAccount.objects.create(name="New card", account_type="credit_card",
                                          opening_date=date(2025, 1, 21))
        batch = self._import(CHASE_CARD, account=late)
        self.assertEqual(batch.rows_imported, 1)
        self.assertEqual(batch.rows_skipped, 2)
        self.assertEqual(late.transactions.get().description, "ULINE SHIP SUPPLIES")

    def test_unreadable_rows_count_as_skipped(self):
        batch = self._import(BOFA, account=self.checking)
        self.assertEqual(batch.rows_imported, 2)
        self.assertEqual(batch.rows_skipped, 1)

    def test_import_runs_rules_and_counts_auto(self):
        CategoryRule.objects.create(name="Pirate", pattern="pirate ship", category_account=self.shipping_acct)
        batch = self._import(CHASE_CARD)
        self.assertEqual(batch.rows_auto_categorized, 1)
        t = BankTransaction.objects.get(description__startswith="PIRATE")
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)
        self.assertTrue(t.auto_categorized)
        self.assertEqual(Expense.objects.get().amount, D("45.20"))

    def test_classify_rows_marks_statuses(self):
        self._import(CHASE_CARD)
        parsed, _, rows = _mapped(CHASE_CARD + "01/01/2019,01/02/2019,OLD ROW,Misc,Sale,-1.00,\n")
        late = BankAccount.objects.create(name="Other", account_type="credit_card", opening_date=date(2020, 1, 1))
        rows, counts = importers.classify_rows(self.card, rows)
        self.assertEqual(counts[importers.DUPLICATE], 3)
        self.assertEqual(counts[importers.SKIPPED_DATE], 1)
        rows, counts = importers.classify_rows(late, importers.map_rows(parsed.rows, "chase", parsed.headers))
        self.assertEqual(counts[importers.NEW], 3)


# ===========================================================================
# Rules
# ===========================================================================
class RulesTests(BankingBase):
    def rule(self, **kw):
        defaults = dict(name="r", pattern="pirate ship", action="categorize",
                        category_account=self.shipping_acct, auto_confirm=True)
        defaults.update(kw)
        return CategoryRule.objects.create(**defaults)

    def test_contains_auto_categorizes_and_counts(self):
        r = self.rule()
        t = self.txn(desc="PIRATE SHIP 8449500000", amount="-45.20")
        c = apply_rules([t])
        t.refresh_from_db(); r.refresh_from_db()
        self.assertEqual(c["auto"], 1)
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)
        self.assertEqual(t.rule_applied, r)
        self.assertTrue(t.auto_categorized)
        self.assertEqual(r.times_applied, 1)
        self.assertIsNotNone(r.last_applied_at)
        exp = t.expense
        self.assertEqual(exp.amount, D("45.20"))
        self.assertEqual(exp.category, self.shipping)
        self.assertFalse(exp.is_reviewed)      # auto rows wait for a human look

    def test_direction_and_amount_bounds(self):
        self.rule(direction="out", amount_min=D("10"), amount_max=D("100"))
        small = self.txn(desc="PIRATE SHIP", amount="-5.00")
        big = self.txn(desc="PIRATE SHIP", amount="-500.00")
        refund = self.txn(desc="PIRATE SHIP REFUND", amount="45.00")
        ok = self.txn(desc="PIRATE SHIP", amount="-45.00")
        apply_rules([small, big, refund, ok])
        for t, status in [(small, "unreviewed"), (big, "unreviewed"), (refund, "unreviewed"), (ok, "categorized")]:
            t.refresh_from_db()
            self.assertEqual(t.status, status, t.description)

    def test_regex_and_account_filter(self):
        self.rule(match_type="regex", pattern=r"^SQ \*", account_filter=self.checking)
        t1 = self.txn(account=self.checking, desc="SQ *COFFEE 123", amount="-4.00")
        t2 = self.txn(account=self.card, desc="SQ *COFFEE 123", amount="-4.00")
        apply_rules([t1, t2])
        t1.refresh_from_db(); t2.refresh_from_db()
        self.assertEqual(t1.status, BankTransaction.CATEGORIZED)
        self.assertEqual(t2.status, BankTransaction.UNREVIEWED)
        bad = self.rule(match_type="regex", pattern="(unclosed")
        self.assertFalse(bad.matches(t2))

    def test_suggest_only_rule_sets_suggestion_not_status(self):
        v = Vendor.objects.create(name="Pirate Ship")
        r = self.rule(auto_confirm=False, vendor=v)
        t = self.txn(desc="PIRATE SHIP", amount="-45.00")
        c = apply_rules([t])
        t.refresh_from_db(); r.refresh_from_db()
        self.assertEqual(c["suggested"], 1)
        self.assertEqual(t.status, BankTransaction.UNREVIEWED)
        self.assertEqual(t.suggested_account, self.shipping_acct)
        self.assertEqual(t.vendor, v)
        self.assertEqual(r.times_applied, 1)
        # running again does not double count
        apply_rules([t])
        r.refresh_from_db()
        self.assertEqual(r.times_applied, 1)
        self.assertEqual(Expense.objects.count(), 0)

    def test_ignore_rule(self):
        r = self.rule(action="ignore", pattern="personal card", category_account=None)
        t = self.txn(desc="PERSONAL CARD PAYMENT", amount="-20.00")
        c = apply_rules([t])
        t.refresh_from_db()
        self.assertEqual(c["ignored"], 1)
        self.assertEqual(t.status, BankTransaction.IGNORED)
        self.assertEqual(t.rule_applied, r)
        self.assertIn("Rule:", t.notes)

    def test_transfer_rule_pairs_twin_or_suggests(self):
        self.rule(action="transfer", pattern="autopay", category_account=None)
        out = self.txn(account=self.checking, desc="CHASE CREDIT CRD AUTOPAY", amount="-500.00")
        twin = self.txn(account=self.card, desc="Payment Thank You", amount="500.00", day=TODAY - timedelta(days=1))
        c = apply_rules([out])
        out.refresh_from_db(); twin.refresh_from_db()
        self.assertEqual(c["transfers"], 1)
        self.assertEqual(out.status, BankTransaction.TRANSFER)
        self.assertEqual(out.transfer_peer, twin)
        self.assertEqual(twin.status, BankTransaction.TRANSFER)
        self.assertEqual(out.category_account, self.card.ledger_account)
        lonely = self.txn(account=self.checking, desc="AUTOPAY TO SOMEWHERE", amount="-77.00")
        c = apply_rules([lonely])
        lonely.refresh_from_db()
        self.assertEqual(c["suggested"], 1)
        self.assertEqual(lonely.status, BankTransaction.UNREVIEWED)
        self.assertIn("transfer", lonely.notes.lower())

    def test_priority_order_first_match_wins(self):
        self.rule(name="low", priority=200, category_account=self.software_acct)
        self.rule(name="high", priority=10, category_account=self.shipping_acct)
        t = self.txn(desc="PIRATE SHIP", amount="-1.00")
        apply_rules([t])
        t.refresh_from_db()
        self.assertEqual(t.category_account, self.shipping_acct)
        inactive = self.rule(name="off", priority=1, active=False, category_account=self.software_acct)
        t2 = self.txn(desc="PIRATE SHIP", amount="-2.00")
        apply_rules([t2])
        t2.refresh_from_db()
        self.assertEqual(t2.category_account, self.shipping_acct)
        self.assertFalse(inactive.matches(t2))

    def test_vendor_recognition_suggests_default_category(self):
        Vendor.objects.create(name="Amazon", aliases="AMZN Mktp\nAMAZON.COM", default_category=self.software)
        t = self.txn(desc="AMZN MKTP US*2K3J4", amount="-19.99")
        c = apply_rules([t])
        t.refresh_from_db()
        self.assertEqual(c["vendor"], 1)
        self.assertEqual(t.status, BankTransaction.UNREVIEWED)
        self.assertEqual(t.vendor.name, "Amazon")
        self.assertEqual(t.suggested_account, self.software_acct)

    def test_learned_suggestion_from_past_choices(self):
        old = self.txn(desc="STARBUCKS STORE 12345", amount="-6.00", day=TODAY - timedelta(days=30))
        old.categorize(self.software_acct)
        other_account = self.txn(account=self.checking, desc="STARBUCKS STORE 99999", amount="-6.00")
        new = self.txn(desc="STARBUCKS STORE 67890", amount="-7.00")
        c = apply_rules([new, other_account])
        new.refresh_from_db(); other_account.refresh_from_db()
        self.assertEqual(c["learned"], 1)
        self.assertEqual(new.suggested_account, self.software_acct)
        self.assertIsNone(other_account.suggested_account)     # learning is per account
        self.assertEqual(c["unmatched"], 1)

    def test_rule_from_transaction_and_apply_single(self):
        t = self.txn(desc="PIRATE SHIP 8449500000 CA", amount="-45.20")
        others = [self.txn(desc=f"PIRATE SHIP 84495{i:05d} CA", amount="-10.00") for i in range(3)]
        refund = self.txn(desc="PIRATE SHIP REFUND", amount="10.00")
        t.categorize(self.shipping_acct, user=self.user)       # the inbox saves first, then builds the rule
        r = rule_from_transaction(t, self.shipping_acct, scope="account", user=self.user)
        self.assertEqual(r.pattern, "PIRATE SHIP")
        self.assertEqual(r.match_field, "description")
        self.assertEqual(r.account_filter, self.card)
        self.assertEqual(r.direction, "out")
        self.assertEqual(apply_single_rule(r), 3)
        refund.refresh_from_db()
        self.assertEqual(refund.status, BankTransaction.UNREVIEWED)
        again = rule_from_transaction(t, self.shipping_acct, scope="account")
        self.assertEqual(again.pk, r.pk)
        m = self.txn(desc="SQ *FOO 1", amount="-3.00", merchant_name="Foo Coffee")
        r2 = rule_from_transaction(m, self.software_acct)
        self.assertEqual((r2.pattern, r2.match_field, r2.account_filter), ("Foo Coffee", "merchant", None))
        from banking.rules import clean_pattern
        self.assertEqual(clean_pattern("SQ *COFFEE 1234 TAMPA"), "SQ *COFFEE")
        self.assertEqual(clean_pattern("AMZN MKTP US*2K3J4 AMZN.COM/BILL"), "AMZN MKTP US*")
        self.assertEqual(clean_pattern("12345"), "12345")


# ===========================================================================
# Matching
# ===========================================================================
class MatchingTests(BankingBase):
    def setUp(self):
        super().setUp()
        from orders.tests import make_customer
        self.customer = make_customer("Card Shop")

    def pay(self, amount, day=None, method="check"):
        from payments.models import Payment
        return Payment.objects.create(customer=self.customer, amount=D(amount), date=day or TODAY, method=method)

    def test_exact_payment_within_window(self):
        p = self.pay("500.00", TODAY - timedelta(days=3))
        self.pay("500.00", TODAY - timedelta(days=20))              # too old
        linked = self.pay("500.00", TODAY)
        self.txn(account=self.checking, desc="OTHER DEPOSIT", amount="500.00").link_payments([linked])
        dep = self.txn(account=self.checking, desc="DEPOSIT", amount="500.00")
        s = matching.suggestions(dep)
        self.assertEqual(s["payments"], [p])
        self.assertEqual(matching.suggestions(self.txn(amount="-500.00"))["payments"], [])

    def test_subset_sum_of_same_day_payments(self):
        day = TODAY - timedelta(days=1)
        a, b, c = self.pay("100.00", day), self.pay("200.00", day), self.pay("300.00", day)
        self.pay("50.00", day)
        self.pay("600.00", TODAY - timedelta(days=30))
        dep = self.txn(account=self.checking, desc="DEPOSIT", amount="600.00")
        s = matching.suggestions(dep)
        self.assertEqual(s["payments"], [])
        self.assertTrue(s["payment_groups"])
        self.assertEqual({p.pk for p in s["payment_groups"][0]}, {a.pk, b.pk, c.pk})

    def test_receipt_within_one_percent(self):
        from inventory.models import Product, Receipt, ReceiptLine, Supplier
        sup = Supplier.objects.create(name="Panini")
        prod = Product.objects.create(sku="BOX-1", name="Hobby box", default_price=D("150"))
        r = Receipt.objects.create(supplier=sup, date=TODAY)
        ReceiptLine.objects.create(receipt=r, product=prod, qty=10, unit_cost=D("100.00"))
        far = Receipt.objects.create(supplier=sup, date=TODAY)
        ReceiptLine.objects.create(receipt=far, product=prod, qty=11, unit_cost=D("100.00"))
        paid = Receipt.objects.create(supplier=sup, date=TODAY, paid_date=TODAY)
        ReceiptLine.objects.create(receipt=paid, product=prod, qty=10, unit_cost=D("100.00"))
        out = self.txn(account=self.checking, desc="PANINI AMERICA", amount="-1005.00")
        s = matching.suggestions(out)
        self.assertEqual(s["receipts"], [r])
        self.assertEqual(matching.suggestions(self.txn(amount="1005.00"))["receipts"], [])

    def test_hand_entered_expense(self):
        e = Expense.objects.create(date=TODAY - timedelta(days=2), category=self.shipping, vendor="Uline",
                                   amount=D("120.00"), paid_from=self.card)
        Expense.objects.create(date=TODAY - timedelta(days=30), category=self.shipping, vendor="Uline",
                               amount=D("120.00"))
        linked = Expense.objects.create(date=TODAY, category=self.shipping, vendor="Uline", amount=D("120.00"))
        self.txn(desc="ULINE EARLIER", amount="-120.00").link_expense(linked)
        t = self.txn(desc="ULINE SHIP SUPPLIES", amount="-120.00")
        self.assertEqual(matching.suggestions(t)["expenses"], [e])

    def test_transfer_twin(self):
        out = self.txn(account=self.checking, desc="AUTOPAY", amount="-500.00")
        twin = self.txn(account=self.card, desc="PAYMENT", amount="500.00", day=TODAY - timedelta(days=2))
        self.txn(account=self.card, desc="PAYMENT OLD", amount="500.00", day=TODAY - timedelta(days=9))
        self.txn(account=self.checking, desc="SAME ACCOUNT", amount="500.00")
        self.assertEqual(matching.suggestions(out)["transfer_twins"], [twin])


# ===========================================================================
# Views
# ===========================================================================
class ViewRenderTests(BankingBase):
    """Every page renders for an admin."""

    def setUp(self):
        super().setUp()
        self.t = self.txn(desc="PIRATE SHIP 8449500000", amount="-45.20")
        self.t.suggested_account = self.shipping_acct
        self.t.save()
        self.done = self.txn(desc="ULINE", amount="-120.00")
        self.done.categorize(self.shipping_acct, user=self.user)
        self.rule = CategoryRule.objects.create(name="r", pattern="x", category_account=self.shipping_acct)
        self.batch = ImportBatch.objects.create(account=self.card, filename="a.csv", rows_imported=1)
        self.conn = BankConnection.objects.create(provider="plaid", institution_name="Test Bank",
                                                  access_token="tok", status="active")
        register_provider("plaid", lambda: FakeProvider(accounts=[
            {"external_account_id": "acc1", "name": "Biz Checking", "official_name": "Business Checking",
             "mask": "1234", "type": "depository", "subtype": "checking", "account_type": "checking",
             "current_balance": D("1500.00"), "available_balance": D("1500.00")}]))

    def tearDown(self):
        unregister_provider("plaid")

    def test_all_get_pages_render(self):
        urls = [
            reverse("bank_inbox"), reverse("bank_inbox") + "?status=all&q=ship&account=%d&from=2020-01-01&to=2030-01-01&min=1&max=1000&batch=%d" % (self.card.pk, self.batch.pk),
            reverse("bank_inbox") + "?status=suggested", reverse("bank_inbox") + "?status=categorized",
            reverse("bank_transaction_detail", args=[self.t.pk]),
            reverse("bank_transaction_detail", args=[self.done.pk]),
            reverse("bank_transaction_add"), reverse("bank_transaction_add") + f"?account={self.card.pk}",
            reverse("bank_accounts"), reverse("bank_accounts") + "?all=1",
            reverse("bank_account_create"), reverse("bank_account_edit", args=[self.card.pk]),
            reverse("bank_account_detail", args=[self.card.pk]),
            reverse("bank_account_detail", args=[self.card.pk]) + "?q=uline&status=categorized&from=2020-01-01",
            reverse("bank_account_detail", args=[self.card.pk]) + f"?statement_date={TODAY.isoformat()}&ending_balance=415.20",
            reverse("bank_import"), reverse("bank_import") + f"?account={self.card.pk}",
            reverse("bank_import_result", args=[self.batch.pk]),
            reverse("bank_rules"), reverse("bank_rule_create"), reverse("bank_rule_edit", args=[self.rule.pk]),
            reverse("bank_connect"), reverse("bank_connect_accounts", args=[self.conn.pk]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
        csv = self.client.get(reverse("bank_account_detail", args=[self.card.pk]) + "?export=csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("running_balance", csv.content.decode())
        self.assertIn("ULINE", csv.content.decode())

    def test_inbox_shows_suggestion_and_counts(self):
        resp = self.client.get(reverse("bank_inbox"))
        self.assertContains(resp, "Suggested: Shipping supplies")
        self.assertContains(resp, "PIRATE SHIP")
        self.assertNotContains(resp, ">ULINE<")
        self.assertContains(resp, "1</span> waiting for review")

    def test_connect_page_without_provider_shows_instructions(self):
        unregister_provider("plaid")
        resp = self.client.get(reverse("bank_connect"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "not set up yet")
        self.assertContains(resp, "CSV import works")
        self.assertNotContains(resp, "link-initialize.js")

    def test_connect_page_with_provider_shows_link(self):
        resp = self.client.get(reverse("bank_connect"))
        self.assertContains(resp, "link-initialize.js")
        self.assertContains(resp, "Test Bank")

    def test_reconcile_box_math(self):
        # card: opening 250 owed, -45.20 and -120 charges -> 415.20 owed
        url = reverse("bank_account_detail", args=[self.card.pk]) + f"?statement_date={TODAY.isoformat()}&ending_balance=400.00"
        resp = self.client.get(url)
        self.assertEqual(resp.context["recon"]["system"], D("415.20"))
        self.assertEqual(resp.context["recon"]["difference"], D("-15.20"))
        self.assertEqual(resp.context["balance"], D("415.20"))

    def test_action_endpoints_redirect_on_get(self):
        for name in ("bank_inbox_bulk", "bank_rules_run"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        self.assertEqual(self.client.get(reverse("bank_inbox_action", args=[self.t.pk])).status_code, 302)
        # sync is a state change: POST only (the dashboard link is a form)
        resp = self.client.get(reverse("bank_sync_now"))
        self.assertEqual(resp.status_code, 405)


class ReviewActionViewTests(BankingBase):
    def post_action(self, txn, **data):
        return self.client.post(reverse("bank_inbox_action", args=[txn.pk]), data, follow=True)

    def test_save_categorizes_creates_expense_and_vendor(self):
        t = self.txn(desc="PIRATE SHIP 8449500000", amount="-45.20")
        resp = self.post_action(t, action="save", category=self.shipping_acct.pk, vendor="Pirate Ship", note="labels")
        self.assertEqual(resp.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)
        self.assertEqual(t.category_account, self.shipping_acct)
        self.assertEqual(t.notes, "labels")
        v = Vendor.objects.get(name="Pirate Ship")
        self.assertEqual(t.vendor, v)
        self.assertEqual(v.default_category, self.shipping)       # learned for next time
        exp = Expense.objects.get()
        self.assertEqual((exp.amount, exp.category, exp.vendor_link, exp.paid_from, exp.source),
                         (D("45.20"), self.shipping, v, self.card, "bank"))
        self.assertEqual(exp.paid_date, t.date)
        self.assertTrue(exp.is_reviewed)

    def test_save_without_category_shows_error(self):
        t = self.txn(desc="X", amount="-1.00")
        resp = self.post_action(t, action="save", category="")
        self.assertContains(resp, "Pick a category first.")
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.UNREVIEWED)

    def test_always_creates_rule_and_applies_to_others(self):
        t = self.txn(desc="PIRATE SHIP 8449500000", amount="-45.20")
        o1 = self.txn(desc="PIRATE SHIP 8449511111", amount="-9.00")
        o2 = self.txn(account=self.checking, desc="PIRATE SHIP 8449522222", amount="-19.00")
        resp = self.post_action(t, action="save", category=self.shipping_acct.pk, always="1")
        self.assertContains(resp, "applied to 2 more")
        rule = CategoryRule.objects.get()
        self.assertEqual(rule.category_account, self.shipping_acct)
        self.assertEqual(rule.times_applied, 2)
        for o in (o1, o2):
            o.refresh_from_db()
            self.assertEqual(o.status, BankTransaction.CATEGORIZED)
            self.assertEqual(o.rule_applied, rule)
        self.assertEqual(Expense.objects.count(), 3)

    def test_ignore_transfer_personal_reset(self):
        t = self.txn(desc="X", amount="-1.00")
        self.post_action(t, action="ignore", note="dup")
        t.refresh_from_db()
        self.assertEqual((t.status, t.notes), (BankTransaction.IGNORED, "dup"))

        out = self.txn(account=self.checking, desc="AUTOPAY", amount="-500.00")
        twin = self.txn(account=self.card, desc="PAYMENT", amount="500.00")
        self.post_action(out, action="transfer")
        out.refresh_from_db(); twin.refresh_from_db()
        self.assertEqual(out.status, BankTransaction.TRANSFER)
        self.assertEqual(twin.transfer_peer, out)

        lonely = self.txn(account=self.checking, desc="LONELY", amount="-77.00")
        resp = self.post_action(lonely, action="transfer")
        self.assertContains(resp, "No matching transaction")
        lonely.refresh_from_db()
        self.assertEqual(lonely.status, BankTransaction.UNREVIEWED)

        p = self.txn(desc="GROCERIES", amount="-60.00")
        self.post_action(p, action="personal")
        p.refresh_from_db()
        self.assertEqual(p.category_account, Account.system("owner_draw"))
        self.assertEqual(Expense.objects.count(), 0)
        contrib = self.txn(account=self.checking, desc="OWNER DEPOSIT", amount="1000.00")
        self.post_action(contrib, action="personal")
        contrib.refresh_from_db()
        self.assertEqual(contrib.category_account, Account.system("owner_contribution"))

        c = self.txn(desc="ULINE", amount="-120.00")
        c.categorize(self.shipping_acct, user=self.user)
        self.assertEqual(Expense.objects.count(), 1)
        self.post_action(c, action="reset")
        c.refresh_from_db()
        self.assertEqual(c.status, BankTransaction.UNREVIEWED)
        self.assertIsNone(c.category_account)
        self.assertEqual(Expense.objects.count(), 0)
        self.post_action(out, action="reset")
        twin.refresh_from_db()
        self.assertEqual(twin.status, BankTransaction.UNREVIEWED)

    def test_bulk_actions(self):
        a = self.txn(desc="A", amount="-1.00")
        b = self.txn(desc="B", amount="-2.00")
        url = reverse("bank_inbox_bulk")
        self.client.post(url, {"ids": [a.pk, b.pk], "bulk_action": "categorize", "bulk_category": self.software_acct.pk})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual({a.status, b.status}, {BankTransaction.CATEGORIZED})
        self.assertEqual(Expense.objects.count(), 2)
        self.client.post(url, {"ids": [a.pk], "bulk_action": "reset"})
        a.refresh_from_db()
        self.assertEqual(a.status, BankTransaction.UNREVIEWED)
        self.client.post(url, {"ids": [a.pk], "bulk_action": "ignore"})
        a.refresh_from_db()
        self.assertEqual(a.status, BankTransaction.IGNORED)
        resp = self.client.post(url, {"bulk_action": "categorize"}, follow=True)
        self.assertContains(resp, "Select at least one")
        CategoryRule.objects.create(name="c", pattern="C", category_account=self.shipping_acct)
        c = self.txn(desc="C", amount="-3.00")
        resp = self.client.post(url, {"bulk_action": "run_rules"}, follow=True)
        self.assertContains(resp, "1 categorized")
        c.refresh_from_db()
        self.assertEqual(c.status, BankTransaction.CATEGORIZED)

    def test_rules_run_button(self):
        CategoryRule.objects.create(name="c", pattern="ULINE", category_account=self.shipping_acct)
        t = self.txn(desc="ULINE 1", amount="-3.00")
        resp = self.client.post(reverse("bank_rules_run"), {"next": reverse("bank_inbox")}, follow=True)
        self.assertContains(resp, "1 categorized")
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)


class TransactionDetailViewTests(BankingBase):
    def setUp(self):
        super().setUp()
        from orders.tests import make_customer
        self.customer = make_customer("Card Shop")

    def test_link_payments_requires_exact_total(self):
        from payments.models import Payment
        p1 = Payment.objects.create(customer=self.customer, amount=D("300.00"), date=TODAY, method="check")
        p2 = Payment.objects.create(customer=self.customer, amount=D("200.00"), date=TODAY, method="check")
        dep = self.txn(account=self.checking, desc="DEPOSIT", amount="500.00")
        url = reverse("bank_transaction_detail", args=[dep.pk])
        resp = self.client.get(url)
        self.assertContains(resp, "These 2 payments add up")
        resp = self.client.post(url, {"action": "link_payments", "payment_ids": [p1.pk]}, follow=True)
        self.assertContains(resp, "add up to $300.00")
        dep.refresh_from_db()
        self.assertEqual(dep.status, BankTransaction.UNREVIEWED)
        self.client.post(url, {"action": "link_payments", "payment_ids": [p1.pk, p2.pk]})
        dep.refresh_from_db()
        self.assertEqual(dep.status, BankTransaction.MATCHED)
        self.assertEqual(set(dep.payments.all()), {p1, p2})
        self.assertEqual(dep.category_account, Account.system("undeposited"))
        self.assertContains(self.client.get(url), "Customer payment: Card Shop")

    def test_link_receipt_marks_bill_paid(self):
        from inventory.models import Product, Receipt, ReceiptLine, Supplier
        sup = Supplier.objects.create(name="Panini")
        prod = Product.objects.create(sku="BOX-1", name="Hobby box", default_price=D("150"))
        r = Receipt.objects.create(supplier=sup, date=TODAY)
        ReceiptLine.objects.create(receipt=r, product=prod, qty=10, unit_cost=D("100.00"))
        out = self.txn(account=self.checking, desc="PANINI", amount="-1000.00")
        url = reverse("bank_transaction_detail", args=[out.pk])
        self.assertContains(self.client.get(url), "RCV-%d" % r.pk)
        self.client.post(url, {"action": "link_receipts", "receipt_ids": [r.pk]})
        out.refresh_from_db(); r.refresh_from_db()
        self.assertEqual(out.status, BankTransaction.MATCHED)
        self.assertEqual(r.paid_date, out.date)
        self.assertEqual(r.paid_from, self.checking)
        self.assertEqual(out.category_account, Account.system("inventory_purchases"))

    def test_link_expense_transfer_receipt_note(self):
        e = Expense.objects.create(date=TODAY, category=self.shipping, vendor="Uline", amount=D("120.00"))
        t = self.txn(desc="ULINE", amount="-120.00")
        url = reverse("bank_transaction_detail", args=[t.pk])
        self.assertContains(self.client.get(url), "Expenses entered by hand")
        self.client.post(url, {"action": "link_expense", "expense_id": e.pk})
        t.refresh_from_db(); e.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.MATCHED)
        self.assertEqual(t.expense, e)
        self.assertEqual(e.paid_from, self.card)
        self.assertEqual(e.payment_method, "card")
        other = self.txn(desc="ULINE 2", amount="-120.00")
        resp = self.client.post(reverse("bank_transaction_detail", args=[other.pk]),
                                {"action": "link_expense", "expense_id": e.pk}, follow=True)
        self.assertContains(resp, "already linked")

        out = self.txn(account=self.checking, desc="AUTOPAY", amount="-500.00")
        twin = self.txn(account=self.card, desc="PAYMENT", amount="500.00")
        self.client.post(reverse("bank_transaction_detail", args=[out.pk]), {"action": "transfer_with", "peer_id": twin.pk})
        out.refresh_from_db()
        self.assertEqual(out.transfer_peer, twin)

        f = SimpleUploadedFile("receipt.jpg", b"\xff\xd8\xff\xe0 fake jpeg", content_type="image/jpeg")
        self.client.post(url, {"action": "upload_receipt", "receipt_file": f})
        t.refresh_from_db(); e.refresh_from_db()
        self.assertTrue(t.receipt_file)
        self.assertTrue(e.receipt_file)             # propagated to the expense row
        self.client.post(url, {"action": "note", "note": "hello"})
        t.refresh_from_db()
        self.assertEqual(t.notes, "hello")
        self.assertContains(self.client.get(url), "hello")
        self.client.post(url, {"action": "reset"})
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.UNREVIEWED)
        self.assertIsNone(t.expense)
        self.assertTrue(Expense.objects.filter(pk=e.pk).exists())   # hand-entered rows survive a reset

    def test_detail_save_from_page(self):
        t = self.txn(desc="ULINE", amount="-120.00")
        url = reverse("bank_transaction_detail", args=[t.pk])
        resp = self.client.post(url, {"action": "save", "category": self.shipping_acct.pk, "vendor": "Uline"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.redirect_chain[-1][0], url)
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)
        self.assertContains(resp, "An expense row was created")


class AccountRuleFormViewTests(BankingBase):
    def test_account_create_and_edit(self):
        resp = self.client.post(reverse("bank_account_create"), {
            "name": "Amex Gold", "institution": "American Express", "account_type": "credit_card",
            "last4": "1005", "opening_date": "2025-01-01", "opening_balance": "412.50", "active": "on"})
        acct = BankAccount.objects.get(name="Amex Gold")
        self.assertRedirects(resp, reverse("bank_account_detail", args=[acct.pk]))
        self.assertIsNotNone(acct.ledger_account)
        self.assertEqual(acct.current_balance, D("412.50"))
        resp = self.client.post(reverse("bank_account_create"), {
            "name": "Bad", "account_type": "checking", "last4": "12ab", "opening_date": "2025-01-01",
            "opening_balance": "0"})
        self.assertContains(resp, "exactly four digits")
        resp = self.client.post(reverse("bank_account_edit", args=[acct.pk]), {
            "name": "Amex Gold Biz", "institution": "Amex", "account_type": "credit_card", "last4": "1005",
            "opening_date": "2025-01-01", "opening_balance": "412.50", "active": "on", "is_default": "on"})
        acct.refresh_from_db(); self.checking.refresh_from_db()
        self.assertEqual(acct.name, "Amex Gold Biz")
        self.assertTrue(acct.is_default)
        self.assertFalse(self.checking.is_default)
        self.assertEqual(acct.ledger_account.name, str(acct))

    def test_rule_create_test_edit_delete(self):
        self.txn(desc="PIRATE SHIP", amount="-1.00")
        data = {"name": "Pirate", "priority": 100, "active": "on", "match_field": "any", "match_type": "contains",
                "pattern": "pirate ship", "direction": "any", "action": "categorize",
                "category_account": self.shipping_acct.pk, "auto_confirm": "on"}
        resp = self.client.post(reverse("bank_rule_create"), dict(data, test="1"))
        self.assertContains(resp, "would match 1 transaction")
        self.assertEqual(CategoryRule.objects.count(), 0)
        resp = self.client.post(reverse("bank_rule_create"), dict(data, apply_now="1"), follow=True)
        self.assertContains(resp, "Applied to 1 transaction")
        rule = CategoryRule.objects.get()
        self.assertEqual(BankTransaction.objects.get().status, BankTransaction.CATEGORIZED)
        resp = self.client.post(reverse("bank_rule_create"), dict(data, match_type="regex", pattern="(oops"))
        self.assertContains(resp, "not a valid regular expression")
        resp = self.client.post(reverse("bank_rule_create"), dict(data, category_account=""))
        self.assertContains(resp, "Pick the category")
        resp = self.client.post(reverse("bank_rule_create"), dict(data, amount_min="10", amount_max="5"))
        self.assertContains(resp, "must be greater")
        resp = self.client.post(reverse("bank_rule_edit", args=[rule.pk]), dict(data, name="Renamed"))
        self.assertRedirects(resp, reverse("bank_rules"))
        rule.refresh_from_db()
        self.assertEqual(rule.name, "Renamed")
        self.assertContains(self.client.get(reverse("bank_rules")), "Renamed")
        self.client.post(reverse("bank_rule_delete", args=[rule.pk]))
        self.assertEqual(CategoryRule.objects.count(), 0)
        self.assertEqual(CategoryRule.all_objects.count(), 1)       # soft delete

    def test_manual_transaction_add(self):
        resp = self.client.post(reverse("bank_transaction_add"), {
            "account": self.checking.pk, "date": TODAY.isoformat(), "direction": "out", "amount": "25.00",
            "description": "USPS stamps", "merchant_name": "USPS", "check_number": "", "notes": ""})
        t = BankTransaction.objects.get()
        self.assertRedirects(resp, reverse("bank_transaction_detail", args=[t.pk]))
        self.assertEqual((t.amount, t.source, t.merchant_name), (D("-25.00"), "manual", "USPS"))
        resp = self.client.post(reverse("bank_transaction_add"), {
            "account": self.checking.pk, "date": "2019-01-01", "direction": "in", "amount": "25.00",
            "description": "Too early"})
        self.assertContains(resp, "pick a date on or after")
        resp = self.client.post(reverse("bank_transaction_add"), {
            "account": self.checking.pk, "date": TODAY.isoformat(), "direction": "in", "amount": "0",
            "description": "zero"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BankTransaction.objects.count(), 1)


class ImportFlowViewTests(BankingBase):
    def upload(self, text, account=None, preset="auto", name="chase.csv"):
        f = SimpleUploadedFile(name, text.encode("utf-8") if isinstance(text, str) else text, content_type="text/csv")
        return self.client.post(reverse("bank_import"), {"step": "upload", "account": (account or self.card).pk,
                                                         "file": f, "preset": preset})

    def test_upload_preview_confirm_result(self):
        resp = self.upload(CHASE_CARD)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Chase (checking or credit card)")
        self.assertContains(resp, "PIRATE SHIP")
        self.assertEqual(resp.context["counts"]["new"], 3)
        token = resp.context["token"]
        self.assertEqual(BankTransaction.objects.count(), 0)
        resp = self.client.post(reverse("bank_import"), {"step": "confirm", "token": token})
        batch = ImportBatch.objects.get()
        self.assertRedirects(resp, reverse("bank_import_result", args=[batch.pk]))
        self.assertEqual(batch.rows_imported, 3)
        self.assertTrue(batch.file.name.startswith("bank_imports/pending/"))
        resp = self.client.get(reverse("bank_import_result", args=[batch.pk]))
        self.assertContains(resp, "Review these transactions")
        self.assertContains(resp, f"batch={batch.pk}")
        # expired token
        resp = self.client.post(reverse("bank_import"), {"step": "confirm", "token": token}, follow=True)
        self.assertContains(resp, "expired")
        # re-upload: everything duplicate, import button disabled
        resp = self.upload(CHASE_CARD)
        self.assertEqual(resp.context["counts"]["duplicate"], 3)
        self.assertContains(resp, "Nothing new to import")

    def test_sign_warning_and_flip(self):
        inverted = CHASE_CARD.replace("-45.20", "45.20").replace("-120.00", "120.00").replace("500.00", "-500.00")
        inverted += "".join(f"01/2{i}/2025,01/2{i}/2025,SHOP {i},Misc,Sale,{i}.00,\n" for i in range(3, 9))
        resp = self.upload(inverted)
        self.assertContains(resp, "Flip signs")
        self.assertTrue(resp.context["sign_warning"])
        self.client.post(reverse("bank_import"), {"step": "confirm", "token": resp.context["token"], "flip": "1"})
        t = BankTransaction.objects.get(description__startswith="PIRATE")
        self.assertEqual(t.amount, D("-45.20"))

    def test_generic_flow_with_column_picker(self):
        resp = self.upload(GENERIC_LATIN1, account=self.checking, name="bank.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["step"], "map")
        self.assertContains(resp, "Pick the columns")
        token = resp.context["token"]
        resp = self.client.post(reverse("bank_import"), {"step": "remap", "token": token, "col_date": "0",
                                                         "col_description": "1"})
        self.assertContains(resp, "Pick either one amount column")
        resp = self.client.post(reverse("bank_import"), {"step": "remap", "token": token, "col_date": "0",
                                                         "col_description": "1", "col_debit": "2", "col_credit": "3"})
        self.assertEqual(resp.context["counts"]["new"], 2)
        self.assertContains(resp, "Café Supplies")
        self.client.post(reverse("bank_import"), {"step": "confirm", "token": token})
        self.assertEqual(list(BankTransaction.objects.order_by("date").values_list("amount", flat=True)),
                         [D("-12.50"), D("5.00")])

    def test_forced_preset_that_does_not_fit_falls_back_to_picker(self):
        resp = self.upload(CHASE_CARD, preset="paypal")
        self.assertEqual(resp.context["step"], "map")
        self.assertContains(resp, "did not fit")

    def test_bad_upload_shows_errors(self):
        resp = self.client.post(reverse("bank_import"), {"step": "upload", "account": self.card.pk, "preset": "auto",
                                                         "file": SimpleUploadedFile("x.pdf", b"%PDF", content_type="application/pdf")})
        self.assertContains(resp, "Upload the CSV export")
        resp = self.upload("\n\n")
        self.assertContains(resp, "No transactions found")


class PermissionTests(BankingBase):
    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        resp = self.client.get(reverse("bank_inbox"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_user_with_no_modules_is_redirected_by_middleware(self):
        bob = User.objects.create_user("nobody", password="pw")
        self.client.force_login(bob)
        self.assertEqual(self.client.get(reverse("bank_inbox")).status_code, 302)

    def test_user_without_module_gets_403(self):
        from core.models import Role, UserProfile
        role = Role.objects.create(name="Front desk", modules=["dashboard"])
        bob = User.objects.create_user("bob", password="pw")
        UserProfile.objects.create(user=bob, role=role)
        self.client.force_login(bob)
        t = self.txn()
        for url in [reverse("bank_inbox"), reverse("bank_accounts"), reverse("bank_rules"), reverse("bank_import"),
                    reverse("bank_connect"), reverse("bank_transaction_detail", args=[t.pk]),
                    reverse("bank_transaction_add"), reverse("bank_sync_now")]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(reverse("bank_inbox_action", args=[t.pk]), {"action": "ignore"}).status_code, 403)

    def test_role_with_banking_module_allowed(self):
        from core.models import Role, UserProfile
        role = Role.objects.create(name="Books", modules=["banking"])
        bob = User.objects.create_user("bob2", password="pw")
        UserProfile.objects.create(user=bob, role=role)
        self.client.force_login(bob)
        self.assertEqual(self.client.get(reverse("bank_inbox")).status_code, 200)
        self.assertEqual(self.client.get(reverse("bank_accounts")).status_code, 200)
        self.assertEqual(self.client.get(reverse("bank_rules")).status_code, 200)
        # other modules stay closed (middleware steers them back to banking)
        self.assertEqual(self.client.get("/expenses/").status_code, 302)


# ===========================================================================
# Provider + sync
# ===========================================================================
class FakeProviderSyncTests(BankingBase):
    def setUp(self):
        super().setUp()
        self.conn = BankConnection.objects.create(provider="plaid", institution_name="Test Bank",
                                                  access_token="tok", status="active")
        self.pchk = BankAccount.objects.create(name="Plaid Checking", account_type="checking", connection=self.conn,
                                               external_account_id="acc1", opening_date=TODAY, opening_balance=0)
        self.pcard = BankAccount.objects.create(name="Plaid Card", account_type="credit_card", connection=self.conn,
                                                external_account_id="acc2", opening_date=TODAY, opening_balance=0)
        self.accounts = [
            {"external_account_id": "acc1", "name": "Checking", "official_name": "", "mask": "1111",
             "type": "depository", "subtype": "checking", "account_type": "checking",
             "current_balance": D("1500.00"), "available_balance": D("1450.00")},
            {"external_account_id": "acc2", "name": "Card", "official_name": "", "mask": "2222",
             "type": "credit", "subtype": "credit card", "account_type": "credit_card",
             "current_balance": D("300.00"), "available_balance": D("4700.00")},
        ]
        self.page1 = {"added": [
            plaid_txn("t1", "acc1", TODAY - timedelta(days=10), "-100.00", "USPS PO"),
            plaid_txn("t2", "acc1", TODAY - timedelta(days=5), "200.00", "ZELLE FROM CARD SHOP"),
            plaid_txn("t3", "acc1", TODAY - timedelta(days=1), "-40.00", "PENDING COFFEE", pending=True),
            plaid_txn("c1", "acc2", TODAY - timedelta(days=7), "-50.00", "ULINE", merchant="Uline"),
            plaid_txn("c2", "acc2", TODAY - timedelta(days=3), "20.00", "PAYMENT THANK YOU"),
        ], "modified": [], "removed": [], "next_cursor": "cur1"}

    def test_first_sync_sets_opening_date_and_balance(self):
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1])
        s = sync_connection(self.conn, provider=fake)
        self.assertEqual(s["added"], 5)
        self.assertEqual(sorted(s["accounts_initialized"]), ["Plaid Card", "Plaid Checking"])
        self.pchk.refresh_from_db(); self.pcard.refresh_from_db(); self.conn.refresh_from_db()
        # checking: current 1500 = opening + (-100 + 200) -> opening 1400 ; pending row ignored
        self.assertEqual(self.pchk.opening_date, TODAY - timedelta(days=10))
        self.assertEqual(self.pchk.opening_balance, D("1400.00"))
        self.assertEqual(self.pchk.balance_as_of(TODAY), D("1500.00"))
        # card: owed 300 = opening - (-50 + 20) -> opening 270
        self.assertEqual(self.pcard.opening_date, TODAY - timedelta(days=7))
        self.assertEqual(self.pcard.opening_balance, D("270.00"))
        self.assertEqual(self.pcard.balance_as_of(TODAY), D("300.00"))
        self.assertEqual(self.conn.sync_cursor, "cur1")
        self.assertEqual(self.conn.status, "active")
        self.assertIsNotNone(self.conn.last_synced_at)
        t3 = BankTransaction.objects.get(external_id="t3")
        self.assertTrue(t3.pending)
        self.assertIsNone(t3.posted_date)
        self.assertEqual(t3.source, "plaid")
        batches = ImportBatch.objects.filter(source="plaid")
        self.assertEqual(batches.count(), 2)
        self.assertEqual(batches.get(account=self.pchk).rows_imported, 3)

    def test_pending_replaced_modified_and_removed(self):
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1, {
            "added": [plaid_txn("t4", "acc1", TODAY - timedelta(days=1), "-42.00", "COFFEE SHOP", pending_ext="t3")],
            "modified": [plaid_txn("t1", "acc1", TODAY - timedelta(days=10), "-110.00", "USPS PO")],
            "removed": ["t3", "c2"], "next_cursor": "cur2"}])
        sync_connection(self.conn, provider=fake)
        # a human categorizes the pending row before it posts
        t3 = BankTransaction.objects.get(external_id="t3")
        t3.categorize(self.shipping_acct, user=self.user)
        s = sync_connection(self.conn, provider=fake)
        self.assertEqual(fake.cursors, ["", "cur1"])
        self.assertEqual((s["added"], s["modified"], s["removed"]), (0, 2, 1))
        self.assertFalse(BankTransaction.objects.filter(external_id="t3").exists())
        t4 = BankTransaction.objects.get(external_id="t4")
        self.assertEqual(t4.pk, t3.pk)                       # same row, new id
        self.assertFalse(t4.pending)
        self.assertEqual(t4.amount, D("-42.00"))
        self.assertEqual(t4.status, BankTransaction.CATEGORIZED)   # review work survived
        self.assertEqual(t4.expense.amount, D("42.00"))      # bank-created expense follows the amount
        t1 = BankTransaction.objects.get(external_id="t1")
        self.assertEqual(t1.amount, D("-110.00"))
        self.assertFalse(BankTransaction.objects.filter(external_id="c2").exists())
        self.assertTrue(BankTransaction.all_objects.filter(external_id="c2", is_deleted=True).exists())
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.sync_cursor, "cur2")
        self.assertEqual(self.pchk.transactions.count(), 3)

    def test_sync_runs_rules_and_skips_before_opening_after_first_sync(self):
        CategoryRule.objects.create(name="usps", pattern="USPS", category_account=self.shipping_acct)
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1, {
            "added": [plaid_txn("old", "acc1", TODAY - timedelta(days=400), "-1.00", "ANCIENT")],
            "modified": [], "removed": [], "next_cursor": "cur2"}])
        s = sync_connection(self.conn, provider=fake)
        self.assertEqual(s["auto"], 1)
        self.assertEqual(BankTransaction.objects.get(external_id="t1").status, BankTransaction.CATEGORIZED)
        self.assertEqual(ImportBatch.objects.get(account=self.pchk).rows_auto_categorized, 1)
        s = sync_connection(self.conn, provider=fake)
        self.assertEqual((s["added"], s["skipped"]), (0, 1))

    def test_csv_row_adopted_instead_of_duplicated(self):
        self.pchk.opening_date = TODAY - timedelta(days=30)
        self.pchk.save()
        BankTransaction.objects.create(account=self.pchk, date=TODAY - timedelta(days=5), amount=D("200.00"),
                                       description="ZELLE FROM CARD SHOP", source="csv")
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1])
        s = sync_connection(self.conn, provider=fake)
        self.assertEqual((s["added"], s["modified"]), (4, 1))
        self.assertEqual(s["accounts_initialized"], ["Plaid Card"])    # checking already had history
        self.assertEqual(BankTransaction.objects.filter(account=self.pchk, amount=D("200.00")).count(), 1)
        self.assertEqual(BankTransaction.objects.get(external_id="t2").source, "plaid")

    def test_login_required_sets_relink_and_other_errors(self):
        fake = FakeProvider(error=ProviderError("Please re-login", code="ITEM_LOGIN_REQUIRED"))
        s = sync_connection(self.conn, provider=fake)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, "relink")
        self.assertIn("ITEM_LOGIN_REQUIRED", self.conn.last_error)
        self.assertEqual(s["status"], "relink")
        fake = FakeProvider(error=ProviderError("boom", code="INTERNAL_SERVER_ERROR"))
        self.conn.status = "active"; self.conn.save()
        sync_connection(self.conn, provider=fake)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, "error")

    def test_sync_all_connections(self):
        cs = CompanySettings.get()
        cs.bank_sync_enabled = False
        cs.save()
        self.assertIn("turned off", sync_all_connections())
        cs.bank_sync_enabled = True
        cs.save()
        BankConnection.objects.create(provider="plaid", institution_name="Dead", status="disconnected")
        BankConnection.objects.create(provider="plaid", institution_name="Relink me", status="relink")
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1])
        msg = sync_all_connections(provider=fake)
        self.assertIn("Synced 1 of 1", msg)
        self.assertIn("5 new", msg)
        self.assertEqual(BankTransaction.objects.count(), 5)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, "active")
        relink = FakeProvider(error=ProviderError("x", code="ITEM_LOGIN_REQUIRED"))
        msg = sync_all_connections(provider=relink)
        self.assertIn("Needs re-authentication: Test Bank", msg)

    def test_connect_views_with_fake_provider(self):
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1])
        register_provider("plaid", lambda: fake)
        try:
            resp = self.client.post(reverse("bank_connect_link_token"))
            self.assertEqual(resp.json()["link_token"], "link-sandbox-token")
            resp = self.client.post(reverse("bank_connect_exchange"), {
                "public_token": "pub-1", "metadata": '{"institution": {"name": "Big Bank", "institution_id": "ins_1"}}'})
            conn = BankConnection.objects.get(institution_name="Big Bank")
            self.assertRedirects(resp, reverse("bank_connect_accounts", args=[conn.pk]))
            self.assertEqual(conn.access_token, "access-pub-1")
            resp = self.client.get(reverse("bank_connect_accounts", args=[conn.pk]))
            self.assertContains(resp, "••••1111")
            # map acc1 to an existing manual account, create acc2 new
            manual = BankAccount.objects.create(name="My Checking", account_type="checking", opening_date=TODAY)
            resp = self.client.post(reverse("bank_connect_accounts", args=[conn.pk]), {
                "track_acc1": "1", "map_acc1": manual.pk, "track_acc2": "1", "map_acc2": "new"}, follow=True)
            self.assertEqual(resp.status_code, 200)
            manual.refresh_from_db()
            self.assertEqual(manual.connection, conn)
            self.assertEqual(manual.external_account_id, "acc1")
            new_card = BankAccount.objects.get(external_account_id="acc2", connection=conn)
            self.assertEqual(new_card.account_type, "credit_card")
            self.assertEqual(new_card.last4, "2222")
            self.assertEqual(new_card.institution, "Big Bank")
            self.assertEqual(manual.transactions.count(), 3)
            self.assertEqual(manual.opening_balance, D("1400.00"))
            self.assertContains(resp, "First sync pulled 5 transactions")
            # sync one connection + disconnect
            resp = self.client.post(reverse("bank_connection_sync", args=[conn.pk]), follow=True)
            self.assertContains(resp, "0 new")
            resp = self.client.post(reverse("bank_connection_disconnect", args=[conn.pk]), follow=True)
            conn.refresh_from_db(); manual.refresh_from_db()
            self.assertEqual(conn.status, "disconnected")
            self.assertEqual(conn.access_token, "")
            self.assertIsNone(manual.connection)
            self.assertEqual(fake.removed_items, ["access-pub-1"])
            self.assertEqual(self.client.post(reverse("bank_connect_exchange"), {"public_token": ""}, follow=True).status_code, 200)
        finally:
            unregister_provider("plaid")

    def test_sync_now_view(self):
        fake = FakeProvider(accounts=self.accounts, pages=[self.page1])
        register_provider("plaid", lambda: fake)
        try:
            resp = self.client.post(reverse("bank_sync_now"), follow=True)
            self.assertContains(resp, "5 new")
        finally:
            unregister_provider("plaid")


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.responses.pop(0)


class PlaidProviderTests(TestCase):
    def provider(self, responses, env="sandbox"):
        return PlaidProvider(client_id="cid", secret="sec", env=env, session=FakeSession(responses))

    def test_not_configured_without_credentials(self):
        p = PlaidProvider(client_id="", secret="", env="sandbox", session=FakeSession([]))
        self.assertFalse(p.is_configured())
        with self.assertRaises(ProviderError):
            p.get_accounts("x")
        self.assertEqual(PlaidProvider(client_id="a", secret="b", env="production", session=FakeSession([])).base_url,
                         "https://production.plaid.com")

    def test_credentials_fall_back_to_company_settings(self):
        cs = CompanySettings.get()
        cs.plaid_client_id, cs.plaid_secret, cs.plaid_env = "from-settings", "s3", "production"
        cs.save()
        p = PlaidProvider(session=FakeSession([]))
        self.assertTrue(p.is_configured())
        self.assertEqual((p.client_id, p.env, p.base_url), ("from-settings", "production", "https://production.plaid.com"))

    def test_sync_pages_and_sign_mapping(self):
        p = self.provider([
            FakeResponse(200, {"added": [{"transaction_id": "a1", "account_id": "acc", "amount": 25.5, "date": "2025-01-16",
                                          "authorized_date": "2025-01-15", "name": "ULINE", "merchant_name": "Uline",
                                          "pending": False}],
                               "modified": [], "removed": [], "has_more": True, "next_cursor": "c1"}),
            FakeResponse(200, {"added": [{"transaction_id": "a2", "account_id": "acc", "amount": -500, "date": "2025-01-20",
                                          "authorized_date": None, "name": "PAYMENT", "pending": True,
                                          "pending_transaction_id": None}],
                               "modified": [], "removed": [{"transaction_id": "a0"}], "has_more": False, "next_cursor": "c2"}),
        ])
        r = p.sync_transactions("tok", "")
        self.assertEqual(len(p.session.calls), 2)
        self.assertEqual(p.session.calls[0][0], "https://sandbox.plaid.com/transactions/sync")
        self.assertNotIn("cursor", p.session.calls[0][1])
        self.assertEqual(p.session.calls[1][1]["cursor"], "c1")
        self.assertEqual(p.session.calls[0][1]["client_id"], "cid")
        self.assertEqual(r["next_cursor"], "c2")
        self.assertEqual(r["removed"], ["a0"])
        a1, a2 = r["added"]
        self.assertEqual(a1["amount"], D("-25.50"))                 # Plaid + = money out
        self.assertEqual(a1["date"], date(2025, 1, 15))             # authorised date is the real date
        self.assertEqual(a1["posted_date"], date(2025, 1, 16))
        self.assertEqual(a1["merchant_name"], "Uline")
        self.assertEqual(a2["amount"], D("500.00"))
        self.assertEqual(a2["date"], date(2025, 1, 20))
        self.assertIsNone(a2["posted_date"])
        self.assertTrue(a2["pending"])

    def test_error_codes_become_provider_errors(self):
        p = self.provider([FakeResponse(400, {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login again"})])
        with self.assertRaises(ProviderError) as ctx:
            p.sync_transactions("tok")
        self.assertTrue(ctx.exception.needs_relink)
        self.assertEqual(ctx.exception.code, "ITEM_LOGIN_REQUIRED")

    def test_accounts_and_tokens(self):
        p = self.provider([
            FakeResponse(200, {"link_token": "lt"}),
            FakeResponse(200, {"access_token": "at", "item_id": "it"}),
            FakeResponse(200, {"accounts": [{"account_id": "x", "name": "Plat Card", "mask": "9", "type": "credit",
                                             "subtype": "credit card", "balances": {"current": 412.5, "available": 100}}]}),
            FakeResponse(200, {}),
        ])
        self.assertEqual(p.create_link_token(User(pk=7)), "lt")
        self.assertEqual(p.session.calls[0][1]["user"]["client_user_id"], "7")
        self.assertEqual(p.exchange_public_token("pub"), {"access_token": "at", "item_id": "it"})
        accts = p.get_accounts("at")
        self.assertEqual(accts[0]["account_type"], "credit_card")
        self.assertEqual(accts[0]["current_balance"], D("412.50"))
        p.remove_item("at")
        self.assertEqual(p.session.calls[-1][0], "https://sandbox.plaid.com/item/remove")


# ===========================================================================
# Services
# ===========================================================================
class ServicesTests(BankingBase):
    def test_cash_position_math_for_a_card(self):
        self.txn(account=self.card, amount="-40.00", desc="ULINE")           # charge -> owe more
        self.txn(account=self.card, amount="100.00", desc="PAYMENT")         # payment -> owe less
        self.txn(account=self.card, amount="-999.00", desc="PENDING", pending=True)
        ign = self.txn(account=self.card, amount="-5.00", desc="DUP")
        ign.mark_ignored()
        self.txn(account=self.checking, amount="-100.00", desc="CARD PAYMENT")
        BankAccount.objects.create(name="Closed", account_type="checking", opening_balance=D("999"), active=False)
        pos = cash_position()
        self.assertEqual(pos["cards"], D("190.00"))            # 250 + 40 - 100
        self.assertEqual(pos["cash"], D("900.00"))
        self.assertEqual(pos["net"], D("710.00"))
        self.assertEqual({a.name for a, _ in pos["accounts"]}, {"Checking", "Visa"})
        self.assertEqual(self.card.current_balance, D("190.00"))

    def test_needs_attention(self):
        a = self.txn(desc="A")
        b = self.txn(desc="B")
        b.suggested_account = self.shipping_acct
        b.save()
        self.txn(desc="C").mark_ignored()
        BankConnection.objects.create(provider="plaid", institution_name="X", status="relink")
        BankConnection.objects.create(provider="plaid", institution_name="Y", status="active")
        n = needs_attention()
        self.assertEqual(n, {"unreviewed": 2, "suggested": 1, "connections_error": 1})
        a.categorize(self.shipping_acct)
        self.assertEqual(needs_attention()["unreviewed"], 1)


# ===========================================================================
# Review fixes (independent code review, round 2)
# ===========================================================================
class TransferPairingViewTests(BankingBase):
    """#1 picking a twin: re-pairing works (used to IntegrityError) and a
    peer that is already reviewed is refused with a message, not a 500."""

    def setUp(self):
        super().setUp()
        self.out = self.txn(account=self.checking, desc="AUTOPAY", amount="-500.00")
        self.twin_a = self.txn(account=self.card, desc="PAYMENT A", amount="500.00")
        self.twin_b = self.txn(account=self.card, desc="PAYMENT B", amount="500.00", day=TODAY - timedelta(days=1))
        self.url = reverse("bank_transaction_detail", args=[self.out.pk])

    def test_repair_to_another_twin_releases_the_old_one(self):
        self.out.mark_transfer(self.twin_a, user=self.user)
        resp = self.client.post(self.url, {"action": "transfer_with", "peer_id": self.twin_b.pk}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Paired as a transfer")
        self.out.refresh_from_db(); self.twin_a.refresh_from_db(); self.twin_b.refresh_from_db()
        self.assertEqual(self.out.transfer_peer, self.twin_b)
        self.assertEqual(self.twin_b.transfer_peer, self.out)
        self.assertEqual((self.twin_a.status, self.twin_a.transfer_peer), (BankTransaction.UNREVIEWED, None))
        # pairing with the same twin again is a harmless no-op
        resp = self.client.post(self.url, {"action": "transfer_with", "peer_id": self.twin_b.pk}, follow=True)
        self.assertContains(resp, "Paired as a transfer")

    def test_reviewed_peer_is_refused_with_a_message(self):
        self.twin_a.categorize(self.shipping_acct, user=self.user)
        resp = self.client.post(self.url, {"action": "transfer_with", "peer_id": self.twin_a.pk}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already reviewed")
        self.out.refresh_from_db()
        self.assertEqual(self.out.status, BankTransaction.UNREVIEWED)
        # a peer that is paired with somebody else is refused too
        other_out = self.txn(account=self.checking, desc="OTHER", amount="-500.00")
        other_out.mark_transfer(self.twin_b, user=self.user)
        resp = self.client.post(self.url, {"action": "transfer_with", "peer_id": self.twin_b.pk}, follow=True)
        self.assertContains(resp, "already paired")
        self.twin_b.refresh_from_db()
        self.assertEqual(self.twin_b.transfer_peer, other_out)
        # bogus / missing ids never 500
        for bad in ("abc", "", "999999", str(self.out.pk)):
            resp = self.client.post(self.url, {"action": "transfer_with", "peer_id": bad}, follow=True)
            self.assertEqual(resp.status_code, 200, bad)

    def test_inbox_transfer_button_accepts_flagged_twin_with_stale_peer(self):
        # twin flagged as a transfer by a rule, but its own peer went away
        self.twin_a.status = BankTransaction.TRANSFER
        self.twin_a.save(update_fields=["status"])
        self.twin_b.delete(user=self.user)
        resp = self.client.post(reverse("bank_inbox_action", args=[self.out.pk]), {"action": "transfer"}, follow=True)
        self.assertContains(resp, "Paired with Visa")
        self.out.refresh_from_db(); self.twin_a.refresh_from_db()
        self.assertEqual(self.out.transfer_peer, self.twin_a)
        self.assertEqual(self.twin_a.transfer_peer, self.out)

    def test_peer_problem_helper(self):
        from banking.views import _peer_problem
        self.assertIsNone(_peer_problem(self.twin_a, self.out))
        self.assertIn("own transfer twin", _peer_problem(self.out, self.out))
        self.twin_a.mark_ignored(user=self.user)
        self.assertIn("already reviewed", _peer_problem(self.twin_a, self.out))


class CsvDetectionTests(BankingBase):
    """#2 a bare Date,Description,Amount header must not become Amex."""

    def test_bare_three_columns_fall_back_to_generic(self):
        parsed = _parse(BARE_THREE)
        self.assertEqual(parsed.headers, ["date", "description", "amount"])
        self.assertIsNone(importers.detect_preset(parsed.headers, parsed.rows[0]))
        self.assertIn("card export", importers.generic_hint(parsed.headers))
        self.assertEqual(importers.guess_generic_mapping(parsed.headers),
                         {"date": 0, "description": 1, "amount": 2})

    def test_four_column_checking_file_is_not_amex(self):
        parsed = _parse(CHECKING_FOUR)
        self.assertIsNone(importers.detect_preset(parsed.headers, parsed.rows[0]))
        self.assertEqual(importers.generic_hint(parsed.headers), "")          # a balance column = a register
        guess = importers.guess_generic_mapping(parsed.headers)
        self.assertEqual((guess["date"], guess["description"], guess["amount"]), (0, 1, 2))
        self.assertNotIn(3, guess.values())

    def test_real_amex_header_is_detected_and_negated(self):
        parsed, preset, rows = _mapped(AMEX)
        self.assertEqual(preset, "amex")
        self.assertEqual(amounts(rows), [D("-89.99"), D("400.00")])
        # BofA (4 matching headers) still beats the generic 3 of Amex
        bofa = _parse(BOFA)
        self.assertEqual(importers.detect_preset(bofa.headers, bofa.rows[0]), "bank_of_america")

    def test_amex_stays_selectable_by_hand(self):
        parsed = _parse(BARE_THREE)
        mapping = importers.resolve_mapping("amex", parsed.headers, parsed.rows[0])
        self.assertTrue(mapping["negate"])
        rows = importers.map_rows(parsed.rows, mapping)
        self.assertEqual(amounts(rows), [D("89.99"), D("-400.00")])
        with self.assertRaises(ValueError):
            importers.map_rows(parsed.rows, None)

    def test_upload_view_shows_picker_with_hint_and_guesses(self):
        f = SimpleUploadedFile("card.csv", BARE_THREE.encode(), content_type="text/csv")
        resp = self.client.post(reverse("bank_import"), {"step": "upload", "account": self.card.pk, "file": f,
                                                         "preset": "auto"})
        self.assertEqual(resp.context["step"], "map")
        self.assertContains(resp, "Looks like a card export?")
        self.assertContains(resp, "Amounts are positive for charges")
        form = resp.context["mapping_form"]
        self.assertEqual((form["col_date"].value(), form["col_description"].value(), form["col_amount"].value()),
                         ("0", "1", "2"))
        self.assertNotContains(resp, "American Express</strong>")
        # forcing Amex by hand still works
        f = SimpleUploadedFile("card.csv", BARE_THREE.encode(), content_type="text/csv")
        resp = self.client.post(reverse("bank_import"), {"step": "upload", "account": self.card.pk, "file": f,
                                                         "preset": "amex"})
        self.assertEqual(resp.context["preset"], "amex")
        self.assertEqual([r["amount"] for r in resp.context["rows"]], [D("89.99"), D("-400.00")])


class RegexGuardFormTests(BankingBase):
    """#3 the rule form refuses patterns that could hang imports."""

    def data(self, **kw):
        d = {"name": "Rx", "priority": 100, "active": "on", "match_field": "any", "match_type": "regex",
             "pattern": "pirate\\s+ship", "direction": "any", "action": "categorize",
             "category_account": self.shipping_acct.pk, "auto_confirm": "on"}
        d.update(kw)
        return d

    def test_nested_quantifier_rejected_plainly(self):
        from banking.forms import CategoryRuleForm
        for bad in (r"^(\w+\s?)*$", r"(a+)+", r"(ab*)+c"):
            form = CategoryRuleForm(self.data(pattern=bad))
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("repeats a group", form.errors["pattern"][0])
            self.assertNotIn("Traceback", form.errors["pattern"][0])
        resp = self.client.post(reverse("bank_rule_create"), self.data(pattern=r"^(\w+\s?)*$"))
        self.assertContains(resp, "repeats a group")
        self.assertEqual(CategoryRule.objects.count(), 0)

    def test_valid_regex_still_saves_and_invalid_still_reported(self):
        from banking.forms import CategoryRuleForm
        self.assertTrue(CategoryRuleForm(self.data()).is_valid())
        form = CategoryRuleForm(self.data(pattern="(oops"))
        self.assertIn("not a valid regular expression", form.errors["pattern"][0])
        self.client.post(reverse("bank_rule_create"), self.data())
        self.assertEqual(CategoryRule.objects.get().pattern, "pirate\\s+ship")
        # 'contains' rules are never run through the regex guard
        self.assertTrue(CategoryRuleForm(self.data(match_type="contains", pattern="(a+)+")).is_valid())


class GenericRulePatternTests(BankingBase):
    """#4 'always categorize like this' must not learn PAYPAL -> Shipping."""

    def test_is_generic_pattern(self):
        from banking.rules import is_generic_pattern
        for g in ("PayPal", "SQ *", "Square", "Venmo", "ZELLE PAYMENT", "Cash App", "APPLE PAY", "GOOGLE PAY",
                  "AMAZON", "AMZN MKTP US*", "EBAY", "STRIPE", "CHECK", "ONLINE PAYMENT - THANK YOU",
                  "DEPOSIT", "TRANSFER", "ACH DEBIT", "POS PURCHASE", "DEBIT CARD PURCHASE", "CREDIT",
                  "ONLINE", "WITHDRAWAL", "ATM", "ULINE", "12345", "", "   "):
            self.assertTrue(is_generic_pattern(g), g)
        for ok in ("PIRATE SHIP", "SQ *COFFEE", "PAYPAL *PIRATESHIP", "Foo Coffee", "USPS PO", "STARBUCKS STORE",
                   "SHOPIFY PAYOUT", "Uline Shipping Supplies", "GODADDY.COM"):
            self.assertFalse(is_generic_pattern(ok), ok)

    def test_suggested_pattern_falls_back_from_processor_merchant(self):
        from banking.rules import suggested_pattern
        t = self.txn(desc="PAYPAL *PIRATESHIP 402935", amount="-45.20", merchant_name="PayPal")
        self.assertEqual(suggested_pattern(t), ("PAYPAL *PIRATESHIP", "description", False))
        g = self.txn(desc="ZELLE PAYMENT 88812", amount="-45.20", merchant_name="Zelle")
        pattern, field, generic = suggested_pattern(g)
        self.assertTrue(generic)
        self.assertTrue(pattern)

    def test_generic_rule_is_suggest_only_and_not_bulk_applied(self):
        t = self.txn(desc="ZELLE PAYMENT 123456", amount="-40.00")
        other = self.txn(desc="ZELLE PAYMENT 654321", amount="-90.00")
        resp = self.client.post(reverse("bank_inbox_action", args=[t.pk]),
                                {"action": "save", "category": self.shipping_acct.pk, "always": "1"}, follow=True)
        self.assertContains(resp, "suggestion only")
        self.assertContains(resp, "too generic")
        rule = CategoryRule.objects.get()
        self.assertFalse(rule.auto_confirm)
        self.assertEqual(rule.times_applied, 0)
        other.refresh_from_db()
        self.assertEqual(other.status, BankTransaction.UNREVIEWED)
        self.assertIsNone(other.suggested_account)                # not even suggested in bulk
        # ...but the next import run suggests it, never applies it
        c = apply_rules([other])
        other.refresh_from_db()
        self.assertEqual((c["suggested"], other.status, other.suggested_account),
                         (1, BankTransaction.UNREVIEWED, self.shipping_acct))

    def test_inbox_shows_editable_pattern_and_uses_the_posted_value(self):
        t = self.txn(desc="PIRATE SHIP 8449500000 CA", amount="-45.20")
        resp = self.client.get(reverse("bank_inbox"))
        self.assertContains(resp, 'name="rule_pattern" value="PIRATE SHIP"')
        detail = self.client.get(reverse("bank_transaction_detail", args=[t.pk]))
        self.assertContains(detail, 'name="rule_pattern" value="PIRATE SHIP"')
        o1 = self.txn(desc="PIRATE SHIP 8449511111", amount="-9.00")
        o2 = self.txn(desc="PIRATE SHIP 8449522222", amount="-3.00")
        o3 = self.txn(desc="PIRATE SHIP 1234 OTHER", amount="-3.00")       # outside the narrowed text
        resp = self.client.post(reverse("bank_inbox_action", args=[t.pk]),
                                {"action": "save", "category": self.shipping_acct.pk, "always": "1",
                                 "rule_pattern": "  pirate ship 8449  "}, follow=True)
        self.assertContains(resp, "applied to 2 more")
        rule = CategoryRule.objects.get()
        self.assertEqual((rule.pattern, rule.match_field, rule.auto_confirm), ("pirate ship 8449", "description", True))
        for o in (o1, o2):
            o.refresh_from_db()
            self.assertEqual(o.status, BankTransaction.CATEGORIZED)
        o3.refresh_from_db()
        self.assertEqual(o3.status, BankTransaction.UNREVIEWED)
        # a short one-word edit ("pirate") is treated as generic: saved, but suggest-only
        short = self.txn(desc="PIRATE SHIP 8449533333", amount="-2.00")
        resp = self.client.post(reverse("bank_inbox_action", args=[short.pk]),
                                {"action": "save", "category": self.shipping_acct.pk, "always": "1",
                                 "rule_pattern": "pirate"}, follow=True)
        self.assertContains(resp, "suggestion only")
        self.assertFalse(CategoryRule.objects.get(pattern="pirate").auto_confirm)
        # a generic edit is honoured but only as a suggestion
        g = self.txn(desc="SQ *FOO 1", amount="-3.00", merchant_name="Foo Coffee")
        resp = self.client.post(reverse("bank_inbox_action", args=[g.pk]),
                                {"action": "save", "category": self.software_acct.pk, "always": "1",
                                 "rule_pattern": "SQ *"}, follow=True)
        self.assertContains(resp, "suggestion only")
        self.assertFalse(CategoryRule.objects.get(pattern="SQ *").auto_confirm)
        # a pattern typed by hand that matches the merchant name is a merchant rule
        m = self.txn(desc="SQ *BAR 2", amount="-3.00", merchant_name="Bar Bakery")
        rule = rule_from_transaction(m, self.software_acct, pattern="Bar Bakery")
        self.assertEqual(rule.match_field, "merchant")
        self.assertFalse(rule.generic_pattern)

    def test_generic_badge_in_inbox(self):
        self.txn(desc="VENMO PAYMENT", amount="-20.00")
        resp = self.client.get(reverse("bank_inbox"))
        self.assertContains(resp, "too generic to auto-apply")


class QueryStringRobustnessTests(BankingBase):
    """#5 sync is POST only; #6 junk ids / decimals never 500."""

    def test_sync_now_requires_post(self):
        self.assertEqual(self.client.get(reverse("bank_sync_now")).status_code, 405)
        resp = self.client.post(reverse("bank_sync_now"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No connected banks")

    def test_helpers(self):
        from banking.views import _int_or_none, _parse_decimal
        self.assertEqual([_int_or_none(v) for v in ("5", " 7 ", "abc", "", None, "0", "-3", "1.5")],
                         [5, 7, None, None, None, None, None, None])
        self.assertEqual(_parse_decimal("$1,234.50"), D("1234.50"))
        for bad in ("abc", "NaN", "nan", "Infinity", "-inf", "1e999", "99999999999999", "", None):
            self.assertIsNone(_parse_decimal(bad), bad)

    def test_inbox_and_friends_survive_junk_query_strings(self):
        self.txn(desc="X", amount="-1.00")
        base = reverse("bank_inbox")
        for qs in ("?account=abc", "?account=999999", "?batch=abc", "?batch=-1", "?min=abc&max=NaN",
                   "?min=1e999", "?min=Infinity", "?from=2025-99-99&to=xyz", "?page=zzz", "?status=bogus",
                   "?account=1.5&batch=1e3"):
            resp = self.client.get(base + qs)
            self.assertEqual(resp.status_code, 200, qs)
        self.assertEqual(self.client.get(reverse("bank_transaction_add") + "?account=abc").status_code, 200)
        self.assertEqual(self.client.get(reverse("bank_import") + "?account=abc").status_code, 200)
        resp = self.client.post(reverse("bank_rules_run"), {"account": "abc"}, follow=True)
        self.assertContains(resp, "Rules checked")
        t = BankTransaction.objects.get()
        resp = self.client.post(reverse("bank_inbox_action", args=[t.pk]), {"action": "save", "category": "abc"},
                                follow=True)
        self.assertContains(resp, "Pick a category first.")
        resp = self.client.post(reverse("bank_inbox_bulk"), {"ids": [t.pk], "bulk_action": "categorize",
                                                             "bulk_category": "1e5"}, follow=True)
        self.assertContains(resp, "Pick a category")


class ImportSessionTests(BankingBase):
    """#7 only a pointer to the saved file lives in the session; the preview
    honours 'flip signs'."""

    def upload(self, text, account=None, preset="auto", name="chase.csv"):
        f = SimpleUploadedFile(name, text.encode("utf-8") if isinstance(text, str) else text, content_type="text/csv")
        return self.client.post(reverse("bank_import"), {"step": "upload", "account": (account or self.card).pk,
                                                         "file": f, "preset": preset})

    def test_session_holds_pointer_not_rows(self):
        from banking.views import SESSION_PREFIX
        resp = self.upload(CHASE_CARD)
        token = resp.context["token"]
        state = self.client.session[SESSION_PREFIX + token]
        self.assertEqual(set(state), {"account_id", "filename", "stored_path", "preset", "mapping", "flip", "created"})
        self.assertNotIn("rows", state)
        self.assertEqual(state["preset"], "chase")
        # preview after a preset change and the import itself re-read the saved file
        resp = self.client.post(reverse("bank_import"), {"step": "reparse", "token": token, "preset": "chase"})
        self.assertEqual(resp.context["counts"]["new"], 3)
        self.client.post(reverse("bank_import"), {"step": "confirm", "token": token})
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_generic_remap_reparses_file(self):
        resp = self.upload(GENERIC_LATIN1, account=self.checking, name="bank.csv")
        token = resp.context["token"]
        resp = self.client.post(reverse("bank_import"), {"step": "remap", "token": token, "col_date": "0",
                                                         "col_description": "1", "col_debit": "2", "col_credit": "3"})
        self.assertEqual(resp.context["counts"]["new"], 2)
        from banking.views import SESSION_PREFIX
        self.assertNotIn("rows", self.client.session[SESSION_PREFIX + token])
        self.client.post(reverse("bank_import"), {"step": "confirm", "token": token})
        self.assertEqual(BankTransaction.objects.count(), 2)

    def test_preview_shows_flipped_rows_before_confirm(self):
        resp = self.upload(CHASE_CARD)
        token = resp.context["token"]
        self.assertEqual([r["amount"] for r in resp.context["rows"]], [D("-45.20"), D("500.00"), D("-120.00")])
        resp = self.client.post(reverse("bank_import"), {"step": "preview", "token": token, "flip": "1"})
        self.assertTrue(resp.context["flip"])
        self.assertEqual([r["amount"] for r in resp.context["rows"]], [D("45.20"), D("-500.00"), D("120.00")])
        self.assertContains(resp, "Signs flipped")
        self.assertContains(resp, 'name="flip" value="1" class="rounded border-slate-300" checked')
        # unticking goes back
        resp = self.client.post(reverse("bank_import"), {"step": "preview", "token": token})
        self.assertFalse(resp.context["flip"])
        self.assertEqual(resp.context["rows"][0]["amount"], D("-45.20"))
        # the no-script fallback button on the confirm form re-previews instead of importing
        resp = self.client.post(reverse("bank_import"), {"step": "confirm", "token": token, "flip": "1", "preview": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["flip"])
        self.assertEqual(BankTransaction.objects.count(), 0)
        self.client.post(reverse("bank_import"), {"step": "confirm", "token": token, "flip": "1"})
        self.assertEqual(BankTransaction.objects.get(description__startswith="PIRATE").amount, D("45.20"))

    def test_missing_file_means_expired(self):
        from django.core.files.storage import default_storage
        from banking.views import SESSION_PREFIX
        resp = self.upload(CHASE_CARD)
        token = resp.context["token"]
        default_storage.delete(self.client.session[SESSION_PREFIX + token]["stored_path"])
        resp = self.client.post(reverse("bank_import"), {"step": "confirm", "token": token}, follow=True)
        self.assertContains(resp, "expired")
        self.assertNotIn(SESSION_PREFIX + token, self.client.session)
        resp = self.client.post(reverse("bank_import"), {"step": "confirm", "token": "nope"}, follow=True)
        self.assertContains(resp, "expired")


class PlaidInitDefaultsTests(BankingBase):
    """#8 first sync only derives opening figures for accounts at defaults."""

    def test_hand_configured_account_is_never_reset(self):
        from banking.tasks import at_defaults
        conn = BankConnection.objects.create(provider="plaid", institution_name="Bank", access_token="tok")
        fresh = BankAccount.objects.create(name="Fresh", account_type="checking", connection=conn,
                                           external_account_id="acc1", opening_date=TODAY, opening_balance=0)
        configured = BankAccount.objects.create(name="Configured", account_type="checking", connection=conn,
                                                external_account_id="acc2", opening_date=date(2025, 1, 1),
                                                opening_balance=D("1000.00"))
        zero_but_backdated = BankAccount.objects.create(name="Backdated", account_type="savings", connection=conn,
                                                        external_account_id="acc3", opening_date=date(2025, 6, 1),
                                                        opening_balance=0)
        self.assertTrue(at_defaults(fresh))
        self.assertFalse(at_defaults(configured))
        self.assertFalse(at_defaults(zero_but_backdated))
        accounts = [{"external_account_id": e, "name": e, "official_name": "", "mask": "", "type": "depository",
                     "subtype": "checking", "account_type": "checking", "current_balance": D("500.00"),
                     "available_balance": D("500.00")} for e in ("acc1", "acc2", "acc3")]
        page = {"added": [
            plaid_txn("f1", "acc1", TODAY - timedelta(days=10), "-100.00", "USPS"),
            plaid_txn("c1", "acc2", date(2024, 12, 20), "-50.00", "BEFORE OPENING"),      # skipped
            plaid_txn("c2", "acc2", TODAY - timedelta(days=2), "-60.00", "ULINE"),
            plaid_txn("b1", "acc3", TODAY - timedelta(days=2), "25.00", "INTEREST"),
        ], "modified": [], "removed": [], "next_cursor": "c"}
        s = sync_connection(conn, provider=FakeProvider(accounts=accounts, pages=[page]))
        self.assertEqual(s["accounts_initialized"], ["Fresh"])
        self.assertEqual(s["skipped"], 1)
        fresh.refresh_from_db(); configured.refresh_from_db(); zero_but_backdated.refresh_from_db()
        self.assertEqual((fresh.opening_date, fresh.opening_balance), (TODAY - timedelta(days=10), D("600.00")))
        self.assertEqual((configured.opening_date, configured.opening_balance), (date(2025, 1, 1), D("1000.00")))
        self.assertEqual((zero_but_backdated.opening_date, zero_but_backdated.opening_balance), (date(2025, 6, 1), D("0")))
        self.assertFalse(BankTransaction.objects.filter(external_id="c1").exists())
        self.assertFalse(at_defaults(fresh))            # has transactions now


class LearnedSuggestionSourceTests(BankingBase):
    """#9 learn from categorized rows only; matched rows carry system
    accounts the dropdown does not offer."""

    def test_matched_rows_do_not_teach(self):
        from orders.tests import make_customer
        from payments.models import Payment
        cust = make_customer("Card Shop")
        p = Payment.objects.create(customer=cust, amount=D("500.00"), date=TODAY, method="zelle")
        old = self.txn(account=self.checking, desc="ZELLE FROM CARD SHOP 11111", amount="500.00",
                       day=TODAY - timedelta(days=20))
        old.link_payments([p], user=self.user)
        new = self.txn(account=self.checking, desc="ZELLE FROM CARD SHOP 22222", amount="500.00")
        c = apply_rules([new])
        new.refresh_from_db()
        self.assertEqual(c["learned"], 0)
        self.assertIsNone(new.suggested_account)
        # a categorized sibling does teach, and the suggestion is selectable
        cat = self.txn(account=self.checking, desc="ZELLE FROM CARD SHOP 33333", amount="500.00",
                       day=TODAY - timedelta(days=10))
        cat.categorize(Account.system("sales"), user=self.user)
        c = apply_rules([new])
        new.refresh_from_db()
        self.assertEqual((c["learned"], new.suggested_account), (1, Account.system("sales")))
        offered = {a.pk for _, accts in Account.bank_category_choices() for a in accts}
        self.assertIn(new.suggested_account_id, offered)

    def test_inbox_selects_reviewed_by(self):
        done = self.txn(desc="ULINE", amount="-120.00")
        done.categorize(self.shipping_acct, user=self.user)
        resp = self.client.get(reverse("bank_inbox") + "?status=categorized")
        self.assertContains(resp, self.user.username)
        self.assertIn("reviewed_by", str(resp.context["page"].object_list.query))


class BankAccountFormDefaultsTests(BankingBase):
    """#10 a new account opens on the books start date."""

    def test_new_account_defaults_to_books_start_date(self):
        from banking.forms import BankAccountForm
        settings_obj = CompanySettings.get()
        settings_obj.books_start_date = date(2025, 1, 1)
        settings_obj.save()
        form = BankAccountForm()
        self.assertEqual(form.fields["opening_date"].initial, date(2025, 1, 1))
        self.assertIn("Your books start on Jan 1, 2025", form.fields["opening_date"].help_text)
        self.assertIn("transactions before the opening date are skipped", form.fields["opening_date"].help_text)
        resp = self.client.get(reverse("bank_account_create"))
        self.assertContains(resp, 'value="2025-01-01"')
        self.assertContains(resp, "Your books start on Jan 1, 2025")
        # editing never overrides what the owner chose
        edit = BankAccountForm(instance=self.checking)
        self.assertEqual(edit["opening_date"].value(), date(2020, 1, 1))
        self.assertNotIn("Your books start", edit.fields["opening_date"].help_text)
        # no books start date -> plain default (today)
        settings_obj.books_start_date = None
        settings_obj.save()
        form = BankAccountForm()
        self.assertNotIn("Your books start", form.fields["opening_date"].help_text)


class ManualTransactionDetailTests(BankingBase):
    """#11a hand-entered rows can be edited while unreviewed and deleted."""

    def test_edit_and_delete_manual_row(self):
        t = self.txn(account=self.checking, desc="USPS stamps", amount="-25.00", source="manual")
        url = reverse("bank_transaction_detail", args=[t.pk])
        resp = self.client.get(url)
        self.assertContains(resp, "Hand-entered transaction")
        self.assertContains(resp, 'name="action" value="edit"')
        self.assertContains(resp, "Delete this transaction")
        old_fp = t.fingerprint
        resp = self.client.post(url, {"action": "edit", "date": (TODAY - timedelta(days=1)).isoformat(),
                                      "direction": "in", "amount": "30.00", "description": "USPS refund"}, follow=True)
        self.assertContains(resp, "Transaction updated.")
        t.refresh_from_db()
        self.assertEqual((t.date, t.amount, t.description, t.original_description),
                         (TODAY - timedelta(days=1), D("30.00"), "USPS refund", "USPS refund"))
        self.assertNotEqual(t.fingerprint, old_fp)
        # validation errors come back as messages
        resp = self.client.post(url, {"action": "edit", "date": "2019-01-01", "direction": "out",
                                      "amount": "1", "description": "x"}, follow=True)
        self.assertContains(resp, "pick a date on or after")
        resp = self.client.post(url, {"action": "edit", "date": TODAY.isoformat(), "direction": "out",
                                      "amount": "0", "description": "x"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.amount, D("30.00"))
        # once reviewed, no edit form (but delete stays)
        t.categorize(Account.system("sales"), user=self.user)
        resp = self.client.get(url)
        self.assertNotContains(resp, 'name="action" value="edit"')
        self.assertContains(resp, "Delete this transaction")
        resp = self.client.post(url, {"action": "edit", "date": TODAY.isoformat(), "direction": "in",
                                      "amount": "31", "description": "x"}, follow=True)
        self.assertContains(resp, "Undo the review first")
        # delete: soft, and releases documents
        e = Expense.objects.create(date=TODAY, category=self.shipping, vendor="Uline", amount=D("120.00"))
        m = self.txn(desc="ULINE", amount="-120.00", source="manual")
        m.link_expense(e, user=self.user)
        resp = self.client.post(reverse("bank_transaction_detail", args=[m.pk]), {"action": "delete"}, follow=True)
        self.assertContains(resp, "Transaction deleted.")
        self.assertEqual(resp.redirect_chain[-1][0], reverse("bank_inbox"))
        self.assertFalse(BankTransaction.objects.filter(pk=m.pk).exists())
        self.assertTrue(BankTransaction.all_objects.get(pk=m.pk).is_deleted)
        e.refresh_from_db()
        self.assertIsNone(e.paid_date)
        self.assertEqual(self.client.get(reverse("bank_transaction_detail", args=[m.pk])).status_code, 404)

    def test_bank_rows_cannot_be_edited_or_deleted(self):
        t = self.txn(desc="PIRATE SHIP", amount="-45.20", source="csv")
        url = reverse("bank_transaction_detail", args=[t.pk])
        resp = self.client.get(url)
        self.assertNotContains(resp, "Hand-entered transaction")
        resp = self.client.post(url, {"action": "delete"}, follow=True)
        self.assertContains(resp, "Only hand-entered transactions can be deleted")
        self.assertTrue(BankTransaction.objects.filter(pk=t.pk).exists())
        resp = self.client.post(url, {"action": "edit", "date": TODAY.isoformat(), "direction": "out",
                                      "amount": "1", "description": "x"}, follow=True)
        self.assertContains(resp, "Only hand-entered transactions can be edited")


class RecordPaymentLinkTests(BankingBase):
    """#11b deposits link to a pre-filled payment form with a customer guess."""

    def test_payment_url_and_customer_guess(self):
        from orders.tests import make_customer
        from banking.views import _guess_customer
        shop = make_customer("Card Shop LLC")
        make_customer("Tampa Cards")
        dep = self.txn(account=self.checking, desc="ZELLE PAYMENT FROM CARD SHOP 998", amount="1250.00")
        self.assertEqual(_guess_customer(dep), shop)
        self.assertIsNone(_guess_customer(self.txn(account=self.checking, desc="DEPOSIT 123", amount="5.00")))
        resp = self.client.get(reverse("bank_transaction_detail", args=[dep.pk]))
        self.assertContains(resp, "Record a customer payment for this deposit")
        url = resp.context["payment_url"]
        self.assertTrue(url.startswith(reverse("payment_create") + "?"))
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["bank_txn"], [str(dep.pk)])
        self.assertEqual(q["amount"], ["1250.00"])
        self.assertEqual(q["date"], [TODAY.isoformat()])
        self.assertEqual(q["reference"], ["ZELLE PAYMENT FROM CARD SHOP 998"])
        self.assertEqual(q["customer"], [str(shop.pk)])
        # follows through to the payment form
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="bank_txn" value="%d"' % dep.pk)
        self.assertContains(resp, 'value="1250.00"')
        # outflows and matched deposits do not offer it
        out = self.txn(account=self.checking, desc="USPS", amount="-5.00")
        self.assertIsNone(self.client.get(reverse("bank_transaction_detail", args=[out.pk])).context["payment_url"])
        dep.mark_ignored(user=self.user)
        self.assertIsNone(self.client.get(reverse("bank_transaction_detail", args=[dep.pk])).context["payment_url"])


class SplitViewTests(BankingBase):
    """#11c split a transaction across categories from the detail page."""

    def test_split_form_post_errors_and_undo(self):
        from banking.models import BankTransactionSplit
        t = self.txn(desc="STAPLES 1234", amount="-100.00")
        url = reverse("bank_transaction_detail", args=[t.pk])
        resp = self.client.get(url)
        self.assertContains(resp, "Split this transaction")
        self.assertContains(resp, 'name="split_account_1"')
        self.assertContains(resp, 'name="split_amount_5"')
        self.assertContains(resp, 'id="splitRemaining"')
        # parts must add up
        resp = self.client.post(url, {"action": "split", "split_account_1": self.shipping_acct.pk, "split_amount_1": "60",
                                      "split_account_2": self.software_acct.pk, "split_amount_2": "30"}, follow=True)
        self.assertContains(resp, "add up to $100.00")
        self.assertFalse(t.splits.exists())
        resp = self.client.post(url, {"action": "split", "split_account_1": self.shipping_acct.pk, "split_amount_1": "100"},
                                follow=True)
        self.assertContains(resp, "at least two parts")
        resp = self.client.post(url, {"action": "split", "split_account_1": self.shipping_acct.pk, "split_amount_1": "60",
                                      "split_account_2": "", "split_amount_2": "40"}, follow=True)
        self.assertContains(resp, "Pick a category for part 2")
        resp = self.client.post(url, {"action": "split", "split_account_1": self.shipping_acct.pk, "split_amount_1": "abc",
                                      "split_account_2": self.software_acct.pk, "split_amount_2": "40"}, follow=True)
        self.assertContains(resp, "positive amount for part 1")
        # success: expense rows per part, row shows as split in the inbox
        resp = self.client.post(url, {"action": "split",
                                      "split_account_1": self.shipping_acct.pk, "split_amount_1": "60.00", "split_note_1": "boxes",
                                      "split_account_2": self.software_acct.pk, "split_amount_2": "40.00",
                                      "split_account_3": "", "split_amount_3": ""}, follow=True)
        self.assertContains(resp, "Split into 2 parts")
        t.refresh_from_db()
        self.assertEqual(t.status, BankTransaction.CATEGORIZED)
        self.assertTrue(t.is_split)
        self.assertEqual(BankTransactionSplit.objects.count(), 2)
        self.assertEqual(sorted(Expense.objects.values_list("amount", flat=True)), [D("40.00"), D("60.00")])
        self.assertContains(resp, "Split across 2 categories")
        self.assertContains(resp, "Undo split")
        self.assertNotContains(resp, "Split this transaction")      # already split
        inbox = self.client.get(reverse("bank_inbox") + "?status=categorized")
        self.assertContains(inbox, "Split · 2 parts")
        # undo = reset
        self.client.post(url, {"action": "reset"})
        t.refresh_from_db()
        self.assertEqual((t.status, t.is_split, Expense.objects.count()), (BankTransaction.UNREVIEWED, False, 0))


class InboxPhoneLayoutTests(BankingBase):
    """#13 sync visible everywhere; compact rows with a More… toggle."""

    def test_sync_button_and_compact_rows(self):
        BankConnection.objects.create(provider="plaid", institution_name="Bank", access_token="tok")
        self.txn(desc="PIRATE SHIP", amount="-45.20")
        resp = self.client.get(reverse("bank_inbox"))
        html = resp.content.decode()
        self.assertIn("Sync now", html)
        import re as _re
        sync_form = _re.search(r'<form method="post" action="%s"[^>]*>' % reverse("bank_sync_now"), html).group(0)
        self.assertNotIn("hidden", sync_form)
        self.assertIn('class="rowMore hidden sm:block', html)
        self.assertIn("rowMoreBtn", html)
        self.assertIn("More…", html)
        # the compact Save and the full-width Save are both there (one per breakpoint)
        self.assertEqual(html.count('name="action" value="save"'), 2)


class InboxDateAliasTests(BankingBase):
    """The month-close page links with ?start=&end=; they filter like from/to."""

    def test_start_end_alias_from_to(self):
        inside = self.txn(desc="INSIDE", amount="-1.00", day=date(2025, 3, 10))
        before = self.txn(desc="BEFORE", amount="-1.00", day=date(2025, 2, 28))
        after = self.txn(desc="AFTER", amount="-1.00", day=date(2025, 4, 1))
        url = reverse("bank_inbox") + "?status=unreviewed&start=2025-03-01&end=2025-03-31"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t.pk for t in resp.context["transactions"]], [inside.pk])
        # the filter form reflects the range so the owner sees why rows are missing
        self.assertContains(resp, 'name="from" value="2025-03-01"')
        self.assertContains(resp, 'name="to" value="2025-03-31"')
        # explicit from/to win when both spellings are present; junk never 500s
        resp = self.client.get(reverse("bank_inbox") + "?from=2025-02-01&start=2025-03-01&end=2025-03-31")
        self.assertEqual({t.pk for t in resp.context["transactions"]}, {inside.pk, before.pk})
        self.assertEqual(self.client.get(reverse("bank_inbox") + "?start=garbage&end=2025-13-45").status_code, 200)
        self.assertEqual(self.client.get(reverse("bank_inbox") + "?start=2025-03-01").context["transactions"][0].pk,
                         after.pk)
