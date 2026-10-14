"""
CSV statement import.

Three stages, each usable on its own:

1. ``parse_csv(file_bytes)`` — decode (utf-8-sig, cp1252, latin-1), sniff the
   delimiter, find the header row (banks like Bank of America put a summary
   block above it; Wells Fargo has no header at all) and return clean rows.
2. ``detect_preset(headers, sample_row)`` picks a bank preset from the header
   signature; ``resolve_mapping(preset, headers)`` turns a preset into a
   concrete column mapping; ``map_rows(rows, mapping)`` turns raw cells into
   normalised dicts with a **signed Decimal amount (+ into the account)**.
3. ``import_rows(account, rows, ...)`` writes ``BankTransaction`` rows,
   skipping duplicates and rows before the account's opening date, then
   runs the category rules.

Every preset carries a *sign rule*: card exports that list charges as
positive numbers (Amex, Discover) are negated so the whole system speaks
one language: negative = money left the account.
"""
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from banking.models import BankTransaction, ImportBatch

CENTS = Decimal("0.01")
MAX_ROWS = 5000

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d",
    "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d-%b-%Y", "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
]

# Row statuses used by the preview/import.
NEW, DUPLICATE, SKIPPED_DATE, SKIPPED_ERROR = "new", "duplicate", "before_opening", "error"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
# Column references are *normalised header names* (lower-case, single spaces)
# or, for headerless files, integer indexes. ``debit``/``credit`` columns are
# money out / money in regardless of how the bank signs them (abs() is taken).
# ``negate`` flips a single signed amount column (positive-is-charge exports).
# ``skip_when`` = {"column": ..., "values": [...]} drops rows such as pending
# card authorisations that will reappear once they post.
PRESETS = {
    "chase": {
        "label": "Chase (checking or credit card)",
        "layouts": [
            {"signature": ["transaction date", "post date", "description", "type", "amount"],
             "columns": {"date": "transaction date", "posted_date": "post date",
                         "description": "description", "amount": "amount"},
             "negate": False},
            {"signature": ["details", "posting date", "description", "amount", "type", "balance"],
             "columns": {"date": "posting date", "description": "description", "amount": "amount",
                         "check_number": "check or slip #"},
             "negate": False},
        ],
    },
    "amex": {
        "label": "American Express",
        "layouts": [
            # "Date,Description,Amount" alone is NOT enough to auto-detect Amex:
            # plenty of banks export exactly those three columns with ordinary
            # signs. Only auto-select when one of Amex's own headers is there.
            {"signature": ["date", "description", "amount"],
             "require_any": ["card member", "account #", "reference", "extended details",
                             "appears on your statement as"],
             "columns": {"date": "date", "description": "description", "amount": "amount",
                         "external_id": "reference"},
             "negate": True},
        ],
    },
    "capital_one": {
        "label": "Capital One",
        "layouts": [
            {"signature": ["transaction date", "posted date", "description", "debit", "credit"],
             "columns": {"date": "transaction date", "posted_date": "posted date",
                         "description": "description", "debit": "debit", "credit": "credit"},
             "negate": False},
        ],
    },
    "bank_of_america": {
        "label": "Bank of America",
        "layouts": [
            {"signature": ["date", "description", "amount", "running bal."],
             "columns": {"date": "date", "description": "description", "amount": "amount"},
             "negate": False},
        ],
    },
    "wells_fargo": {
        "label": "Wells Fargo (no header row)",
        "layouts": [
            {"signature": None, "headerless": True, "min_columns": 5,
             "columns": {"date": 0, "amount": 1, "check_number": 3, "description": 4},
             "negate": False},
        ],
    },
    "citi": {
        "label": "Citi",
        "layouts": [
            {"signature": ["status", "date", "description", "debit", "credit"],
             "columns": {"date": "date", "description": "description", "debit": "debit",
                         "credit": "credit"},
             "skip_when": {"column": "status", "values": ["pending"]},
             "negate": False},
        ],
    },
    "discover": {
        "label": "Discover",
        "layouts": [
            {"signature": ["trans. date", "post date", "description", "amount"],
             "columns": {"date": "trans. date", "posted_date": "post date",
                         "description": "description", "amount": "amount"},
             "negate": True},
        ],
    },
    "paypal": {
        "label": "PayPal (activity download)",
        "layouts": [
            {"signature": ["date", "time", "timezone", "name", "type", "status", "currency",
                           "gross", "fee", "net"],
             "columns": {"date": "date", "description": "name", "description_alt": "type",
                         "merchant": "name", "amount": "net", "external_id": "transaction id"},
             "skip_when": {"column": "status",
                           "values": ["pending", "denied", "removed", "canceled", "cancelled",
                                      "failed", "placed", "unclaimed"]},
             "negate": False},
        ],
    },
    "square": {
        "label": "Square (transactions export)",
        "layouts": [
            {"signature": ["date", "time", "gross sales", "net total", "transaction id"],
             "columns": {"date": "date", "description": "description",
                         "description_alt": "customer name", "merchant": "customer name",
                         "amount": "net total", "external_id": "transaction id"},
             "negate": False},
        ],
    },
    "generic": {
        "label": "Other bank (pick the columns yourself)",
        "layouts": [],
    },
}

PRESET_CHOICES = [("auto", "Detect automatically")] + [
    (slug, p["label"]) for slug, p in PRESETS.items()]

# Field keys a mapping may carry, in the order the generic column picker shows them.
MAPPING_FIELDS = [
    ("date", "Date", True),
    ("description", "Description", True),
    ("amount", "Amount (one signed column)", False),
    ("debit", "Money out column", False),
    ("credit", "Money in column", False),
    ("posted_date", "Posted date", False),
    ("merchant", "Merchant / payee", False),
    ("check_number", "Check #", False),
    ("external_id", "Transaction id", False),
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class ParsedCSV:
    headers: list | None
    rows: list
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    header_row_index: int | None = None
    truncated: bool = False
    warnings: list = field(default_factory=list)

    @property
    def column_count(self):
        if self.headers:
            return len(self.headers)
        return max((len(r) for r in self.rows), default=0)

    def column_labels(self):
        """Labels for the generic column picker."""
        n = self.column_count
        if self.headers:
            return [(i, h or f"Column {i + 1}") for i, h in enumerate(self.headers)]
        sample = self.rows[0] if self.rows else []
        return [(i, f"Column {i + 1}" + (f" (e.g. {sample[i][:25]})" if i < len(sample) and sample[i] else ""))
                for i in range(n)]


def normalize_header(text):
    t = (text or "").replace("﻿", "").strip().strip('"').strip("'").lower()
    return re.sub(r"\s+", " ", t)


def decode_bytes(data):
    if isinstance(data, str):
        return data, "str"
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _sniff_delimiter(text):
    sample = "\n".join(text.splitlines()[:25])
    try:
        d = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        d = ","
    # sanity: the sniffer sometimes picks a rare character; prefer the one
    # that yields the most consistent column count across the sample
    best, best_score = d, -1
    for cand in (d, ",", ";", "\t", "|"):
        counts = [len(r) for r in csv.reader(io.StringIO(sample), delimiter=cand) if r]
        if not counts:
            continue
        common = Counter(counts).most_common(1)[0]
        score = common[1] * (1 if common[0] > 1 else 0)
        if score > best_score:
            best, best_score = cand, score
    return best


def parse_amount(text):
    """'$1,234.56' -> Decimal; '(12.00)' -> -12.00; '' -> None."""
    if text is None:
        return None
    t = str(text).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not t or t in ("-", "--"):
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.endswith("-"):
        neg, t = True, t[:-1]
    low = t.lower()
    if low.endswith("cr"):
        t = t[:-2]
    elif low.endswith("dr"):
        neg, t = True, t[:-2]
    try:
        val = Decimal(t)
    except InvalidOperation:
        return None
    val = val.quantize(CENTS)
    return -val if neg else val


def parse_date(text, formats=None):
    if text is None:
        return None
    t = str(text).strip().strip('"')
    if not t:
        return None
    for fmt in (formats or DATE_FORMATS):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    # last resort: drop a trailing time component
    head = t.split(" ")[0].split("T")[0]
    if head != t:
        return parse_date(head, formats)
    return None


def _looks_like_data_cell(cell):
    return parse_date(cell) is not None or parse_amount(cell) is not None


def _row_matches_signature(row, signature):
    norm = {normalize_header(c) for c in row}
    return all(s in norm for s in signature)


def _find_header_index(rows):
    limit = min(25, len(rows))
    for i in range(limit):
        for preset in PRESETS.values():
            for layout in preset["layouts"]:
                sig = layout.get("signature")
                if sig and _row_matches_signature(rows[i], sig):
                    return i
    first = rows[0]
    non_empty = [c for c in first if c.strip()]
    if len(non_empty) >= 2 and not any(_looks_like_data_cell(c) for c in non_empty):
        return 0
    return None


def parse_csv(file_bytes):
    """Decode, sniff, locate the header. Returns ParsedCSV."""
    text, encoding = decode_bytes(file_bytes)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ParsedCSV(headers=None, rows=[], encoding=encoding, warnings=["The file is empty."])
    delimiter = _sniff_delimiter("\n".join(lines))
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = []
    for raw in reader:
        cells = [c.strip() for c in raw]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ParsedCSV(headers=None, rows=[], delimiter=delimiter, encoding=encoding,
                         warnings=["No rows found."])
    header_idx = _find_header_index(rows)
    warnings = []
    if header_idx is None:
        headers, data = None, rows
    else:
        headers = [normalize_header(c) for c in rows[header_idx]]
        data = rows[header_idx + 1:]
        if header_idx > 0:
            warnings.append(f"Skipped {header_idx} summary line(s) above the header.")
    truncated = len(data) > MAX_ROWS
    if truncated:
        warnings.append(f"Only the first {MAX_ROWS} rows are imported; split larger files.")
        data = data[:MAX_ROWS]
    return ParsedCSV(headers=headers, rows=data, delimiter=delimiter, encoding=encoding,
                     header_row_index=header_idx, truncated=truncated, warnings=warnings)


# ---------------------------------------------------------------------------
# Preset detection and mapping
# ---------------------------------------------------------------------------
def _layout_for(slug, headers, sample_row=None):
    preset = PRESETS.get(slug)
    if not preset:
        return None
    norm = {normalize_header(h) for h in (headers or [])}
    for layout in preset["layouts"]:
        if layout.get("headerless"):
            if headers is None or _is_wells_row(sample_row):
                return layout
            continue
        if layout["signature"] and all(s in norm for s in layout["signature"]):
            return layout
    # fall back to the first layout so a user-forced preset still works
    return preset["layouts"][0] if preset["layouts"] else None


def _is_wells_row(row):
    if not row or len(row) < 5:
        return False
    return (parse_date(row[0]) is not None and parse_amount(row[1]) is not None
            and row[2].strip() in ("*", "") and bool(row[4].strip()))


def _layout_fits(layout, norm):
    """True when every signature header is present and, for layouts that
    need one, at least one distinguishing header (``require_any``)."""
    sig = layout.get("signature")
    if not sig or not all(s in norm for s in sig):
        return False
    need = layout.get("require_any")
    if need and not any(h in norm for h in need):
        return False
    return True


def detect_preset(headers, sample_row=None):
    """Return the preset slug whose header signature fits best, else None.
    The longest matching signature wins (Bank of America beats Amex even
    though Amex's three columns are a subset of BofA's). A layout with
    ``require_any`` is only picked when one of those headers is present, so
    a bare "Date,Description,Amount" file falls back to the generic picker."""
    if headers is None:
        return "wells_fargo" if _is_wells_row(sample_row) else None
    norm = {normalize_header(h) for h in headers}
    best, best_len = None, 0
    for slug, preset in PRESETS.items():
        for layout in preset["layouts"]:
            if _layout_fits(layout, norm) and len(layout["signature"]) > best_len:
                best, best_len = slug, len(layout["signature"])
    return best


# Header words that identify each generic field, checked in order.
_GUESS = [
    ("date", ("transaction date", "trans. date", "posting date", "posted date", "post date", "date")),
    ("posted_date", ("posted date", "post date", "posting date")),
    ("description", ("description", "payee", "memo", "details", "name", "narrative", "transaction")),
    ("amount", ("amount", "transaction amount", "amt")),
    ("debit", ("debit", "withdrawal", "withdrawals", "money out", "paid out", "charge")),
    ("credit", ("credit", "deposit", "deposits", "money in", "paid in")),
    ("check_number", ("check #", "check number", "check or slip #", "check", "cheque")),
    ("external_id", ("transaction id", "reference", "reference number", "id")),
]
_BALANCE_WORDS = ("balance", "running bal", "running bal.")


def guess_generic_mapping(headers):
    """Best-guess ``{field: column index}`` for the generic column picker,
    from header names alone. Only used to pre-fill the picker; the owner
    confirms. Returns {} for headerless files."""
    if not headers:
        return {}
    norm = [normalize_header(h) for h in headers]
    guess = {}
    for key, words in _GUESS:
        for w in words:
            for i, h in enumerate(norm):
                if h == w and i not in guess.values():
                    guess[key] = i
                    break
            if key in guess:
                break
    if "posted_date" in guess and guess["posted_date"] == guess.get("date"):
        del guess["posted_date"]
    if "amount" in guess:
        guess.pop("debit", None)
        guess.pop("credit", None)
    return guess


def generic_hint(headers):
    """Plain-language nudge for files that look like a card export with
    positive charges (one signed amount column, no balance column). Empty
    when the layout is clearly a checking register."""
    if not headers:
        return ""
    norm = {normalize_header(h) for h in headers}
    if not ({"date", "description", "amount"} <= norm):
        return ""
    if any(b in norm for b in _BALANCE_WORDS) or ("debit" in norm or "credit" in norm):
        return ""
    return ("Looks like a card export? If purchases show as positive numbers, tick "
            "'Amounts are positive for charges' below.")


def resolve_mapping(preset, headers, sample_row=None):
    """Turn a preset slug into an index-based mapping for these headers.
    Returns None when the preset's columns are not all present."""
    layout = _layout_for(preset, headers, sample_row)
    if layout is None:
        return None
    index = {}
    if headers:
        for i, h in enumerate(headers):
            index.setdefault(normalize_header(h), i)
    mapping = {"preset": preset, "negate": bool(layout.get("negate")), "columns": {}}
    for key, ref in layout["columns"].items():
        if isinstance(ref, int):
            mapping["columns"][key] = ref
        elif ref in index:
            mapping["columns"][key] = index[ref]
    required = ["date", "description"]
    if not all(k in mapping["columns"] for k in required):
        return None
    if "amount" not in mapping["columns"] and not (
            "debit" in mapping["columns"] or "credit" in mapping["columns"]):
        return None
    skip = layout.get("skip_when")
    if skip and skip["column"] in index:
        mapping["skip_when"] = {"column": index[skip["column"]], "values": list(skip["values"])}
    return mapping


def mapping_from_form(cleaned, preset="generic"):
    """Build a mapping dict from the generic column-picker form data."""
    columns = {}
    for key, _label, _req in MAPPING_FIELDS:
        val = cleaned.get(f"col_{key}")
        if val not in (None, ""):
            columns[key] = int(val)
    return {"preset": preset, "negate": bool(cleaned.get("positive_is_charge")), "columns": columns}


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def map_rows(rows, preset_or_mapping, headers=None):
    """Normalise raw CSV rows into transaction dicts.

    Each dict: date, posted_date, amount (Decimal, + into account),
    description, merchant_name, check_number, external_id, row_number, and
    ``error`` (str) when the row cannot be imported."""
    if isinstance(preset_or_mapping, str):
        mapping = resolve_mapping(preset_or_mapping, headers, rows[0] if rows else None)
        if mapping is None:
            raise ValueError(f"The '{preset_or_mapping}' preset does not fit this file's columns.")
    else:
        mapping = preset_or_mapping
    if not mapping:
        raise ValueError("No column mapping: pick the columns first.")
    cols = mapping.get("columns", {})
    negate = bool(mapping.get("negate"))
    skip = mapping.get("skip_when")
    out = []
    for n, row in enumerate(rows, start=1):
        rec = {"row_number": n, "date": None, "posted_date": None, "amount": None,
               "description": "", "merchant_name": "", "check_number": "", "external_id": "",
               "error": ""}
        if skip is not None:
            status = _cell(row, skip["column"]).lower()
            if status in skip["values"]:
                rec["error"] = f"Skipped ({status})"
                out.append(rec)
                continue
        rec["date"] = parse_date(_cell(row, cols.get("date")))
        if "posted_date" in cols:
            rec["posted_date"] = parse_date(_cell(row, cols["posted_date"]))
        desc = _cell(row, cols.get("description"))
        alt = _cell(row, cols.get("description_alt")) if "description_alt" in cols else ""
        if alt and alt.lower() not in desc.lower():
            desc = f"{desc} · {alt}" if desc else alt
        rec["description"] = re.sub(r"\s+", " ", desc)[:300]
        rec["merchant_name"] = _cell(row, cols.get("merchant"))[:200] if "merchant" in cols else ""
        rec["check_number"] = _cell(row, cols.get("check_number"))[:20] if "check_number" in cols else ""
        rec["external_id"] = _cell(row, cols.get("external_id"))[:120] if "external_id" in cols else ""
        if "amount" in cols:
            amt = parse_amount(_cell(row, cols["amount"]))
            if amt is not None and negate:
                amt = -amt
        else:
            debit = parse_amount(_cell(row, cols.get("debit"))) if "debit" in cols else None
            credit = parse_amount(_cell(row, cols.get("credit"))) if "credit" in cols else None
            if debit is None and credit is None:
                amt = None
            else:
                amt = (abs(credit) if credit is not None else Decimal("0")) - \
                      (abs(debit) if debit is not None else Decimal("0"))
                if negate:
                    amt = -amt
        rec["amount"] = amt
        rec["abs_amount"] = abs(amt) if amt is not None else None
        if rec["date"] is None:
            rec["error"] = "No valid date"
        elif amt is None:
            rec["error"] = "No valid amount"
        elif not rec["description"]:
            rec["description"] = rec["merchant_name"] or "(no description)"
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Preview + import
# ---------------------------------------------------------------------------
def _existing_fingerprints(account):
    return Counter(BankTransaction.all_objects.filter(account=account)
                   .values_list("fingerprint", flat=True))


def _existing_external_ids(account):
    return set(BankTransaction.all_objects.filter(account=account)
               .exclude(external_id="").values_list("external_id", flat=True))


def classify_rows(account, mapped_rows):
    """Tag each mapped row with a status (new / duplicate / before_opening /
    error) the same way ``import_rows`` will treat it. Returns (rows, counts).

    Duplicates are judged per fingerprint *count*: two identical coffee
    purchases on the same day in one file are both imported, but re-importing
    the same statement imports nothing."""
    fps = _existing_fingerprints(account)
    ext_ids = _existing_external_ids(account)
    counts = Counter()
    seen_ext = set()
    for rec in mapped_rows:
        if rec["error"]:
            rec["status"] = SKIPPED_ERROR
        elif rec["date"] < account.opening_date:
            rec["status"] = SKIPPED_DATE
        else:
            ext = rec["external_id"]
            fp = BankTransaction.make_fingerprint(account.pk, rec["date"], rec["amount"], rec["description"])
            rec["fingerprint"] = fp
            if ext and (ext in ext_ids or ext in seen_ext):
                rec["status"] = DUPLICATE
            elif fps[fp] > 0:
                fps[fp] -= 1
                rec["status"] = DUPLICATE
            else:
                rec["status"] = NEW
                if ext:
                    seen_ext.add(ext)
        counts[rec["status"]] += 1
    return mapped_rows, counts


def sign_warning(account, mapped_rows):
    """Plain-language warning when the file's signs look inverted."""
    amounts = [r["amount"] for r in mapped_rows if r.get("amount") is not None and r["amount"] != 0]
    if len(amounts) < 5:
        return ""
    positive = sum(1 for a in amounts if a > 0) / len(amounts)
    if account.is_liability and positive > 0.6:
        return ("Most rows in this file are positive. On a credit card, charges should import as "
                "money out (negative). If these are charges, tick 'Flip signs' before importing.")
    if not account.is_liability and positive > 0.85:
        return ("Almost every row is positive. If this file lists payments as positive numbers, "
                "tick 'Flip signs' before importing.")
    return ""


def flip_rows(mapped_rows):
    for r in mapped_rows:
        if r.get("amount") is not None:
            r["amount"] = -r["amount"]
            r["abs_amount"] = abs(r["amount"])
    return mapped_rows


def serialize_rows(mapped_rows):
    """JSON-safe copy for session storage."""
    out = []
    for r in mapped_rows:
        c = dict(r)
        for k in ("date", "posted_date"):
            c[k] = c[k].isoformat() if isinstance(c[k], date) else None
        c["amount"] = str(c["amount"]) if c["amount"] is not None else None
        c["abs_amount"] = str(c["abs_amount"]) if c.get("abs_amount") is not None else None
        out.append(c)
    return out


def deserialize_rows(data):
    out = []
    for r in data:
        c = dict(r)
        for k in ("date", "posted_date"):
            c[k] = date.fromisoformat(c[k]) if c[k] else None
        c["amount"] = Decimal(c["amount"]) if c["amount"] is not None else None
        c["abs_amount"] = abs(c["amount"]) if c["amount"] is not None else None
        out.append(c)
    return out


def parse_stored(stored_path):
    """Re-parse a statement saved by the upload step (``default_storage``).
    Returns a ParsedCSV, or None when the file is gone."""
    from django.core.files.storage import default_storage
    if not stored_path or not default_storage.exists(stored_path):
        return None
    with default_storage.open(stored_path, "rb") as fh:
        return parse_csv(fh.read())


def import_rows(account, rows, user=None, preset="", filename="", file=None,
                stored_path=None, source="csv"):
    """Create BankTransaction rows for ``rows`` (output of ``map_rows``).

    * rows dated before ``account.opening_date`` are skipped (counted),
    * duplicates (fingerprint count already present, or external id seen)
      are skipped (counted),
    * everything else becomes an unreviewed transaction, then the category
      rules run and ``rows_auto_categorized`` records how many they settled.
    Returns the ImportBatch."""
    from banking.rules import apply_rules
    rows, counts = classify_rows(account, rows)
    batch = ImportBatch(account=account, source=source, filename=filename[:200], preset=preset[:40],
                        rows_total=len(rows),
                        created_by=user if getattr(user, "is_authenticated", False) else None)
    if file is not None:
        batch.file.save(filename or "statement.csv", file, save=False)
    elif stored_path:
        batch.file.name = stored_path
    created = []
    with transaction.atomic():
        batch.save()
        for rec in rows:
            if rec["status"] != NEW:
                continue
            txn = BankTransaction(
                account=account, date=rec["date"], posted_date=rec["posted_date"],
                amount=rec["amount"], description=rec["description"],
                merchant_name=rec["merchant_name"], check_number=rec["check_number"],
                external_id=rec["external_id"], fingerprint=rec.get("fingerprint", ""),
                source=source, import_batch=batch)
            txn.save()
            created.append(txn)
        batch.rows_imported = len(created)
        batch.rows_duplicate = counts[DUPLICATE]
        batch.rows_skipped = counts[SKIPPED_DATE] + counts[SKIPPED_ERROR]
        if created:
            batch.date_min = min(t.date for t in created)
            batch.date_max = max(t.date for t in created)
        batch.save()
    if created:
        result = apply_rules(created, user=user)
        batch.rows_auto_categorized = result["auto"] + result["ignored"] + result["transfers"]
        batch.save(update_fields=["rows_auto_categorized"])
    return batch
