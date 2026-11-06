"""
Turn a supplier's order export (CSV / TSV / pasted order-confirmation text)
into draft receiving lines.

Works with any distributor portal — Tradepost, GTS, Southern Hobby, a
Pokémon Center invoice copied from an email — because it does not rely on a
fixed layout:

* If the text has a header row we recognise (upc / sku / item, qty /
  quantity, cost / price, description / product), the columns are used.
* Otherwise every line is scanned for a barcode-looking number, a quantity
  (``x4``, ``4 x``, ``Qty 4`` or a lone small integer) and one or two money
  amounts (unit cost, or unit cost + line total).

Each parsed row is matched to a Product by UPC, extra barcode or SKU.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

CODE_HEADERS = ("upc", "barcode", "ean", "gtin", "sku", "item #", "item number", "item no",
                "item", "product code", "code", "part", "mfg", "isbn")
QTY_HEADERS = ("qty", "quantity", "units", "ordered", "qty ordered", "cases", "count", "shipped")
COST_HEADERS = ("unit cost", "unit price", "cost", "price", "each", "unit", "rate", "net price",
                "wholesale", "your price")
TOTAL_HEADERS = ("total", "line total", "ext", "extended", "amount", "subtotal", "ext price")
NAME_HEADERS = ("description", "product", "name", "item name", "title", "product name", "desc")

MONEY = re.compile(r"\$?\s*(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,4})?|-?\d+\.\d{1,4}|-?\d+)(?!\d)")
CODE = re.compile(r"(?<![\d.])(\d{8,14})(?![\d.])")
QTY = re.compile(r"(?:\bqty\.?\s*:?\s*(\d{1,5})\b|\bx\s*(\d{1,5})\b|\b(\d{1,5})\s*x\b|\b(\d{1,5})\s*(?:ea|each|pcs|units?|cases?)\b)", re.I)


@dataclass
class ParsedLine:
    code: str = ""
    name: str = ""
    qty: int | None = None
    cost: Decimal | None = None
    raw: str = ""
    product_id: int | None = None
    product_label: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def line_total(self):
        if self.qty and self.cost is not None:
            return (self.cost * self.qty).quantize(Decimal("0.01"))
        return None

    @property
    def ok(self):
        return not self.problems and self.product_id is not None


def _money(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    if not s or s in ("-", "—"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _int(s):
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        try:
            d = Decimal(str(s).strip())
            return int(d) if d == int(d) else None
        except (InvalidOperation, ValueError):
            return None


def _header_index(headers, candidates):
    """Index of the first header matching any candidate (exact or word-start)."""
    norm = [h.strip().lower() for h in headers]
    for cand in candidates:
        for i, h in enumerate(norm):
            if h == cand:
                return i
    for cand in candidates:
        for i, h in enumerate(norm):
            if h.startswith(cand) or h.endswith(" " + cand):
                return i
    return None


def _split_table(text):
    """Detect delimiter and return rows (list of lists)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    delimiter = None
    for d in ("\t", ",", ";", "|"):
        if all(d in l for l in lines[:3]):
            delimiter = d
            break
    if delimiter is None:
        return None
    try:
        return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        return None


def parse_text(text):
    """Return list[ParsedLine] (unmatched; call match_products next)."""
    text = (text or "").strip()
    if not text:
        return []
    rows = _split_table(text)
    if rows and len(rows[0]) >= 2:
        parsed = _parse_table(rows)
        if parsed is not None:
            return parsed
    return _parse_freeform(text)


def _parse_table(rows):
    headers = rows[0]
    ci = _header_index(headers, CODE_HEADERS)
    qi = _header_index(headers, QTY_HEADERS)
    pi = _header_index(headers, COST_HEADERS)
    ti = _header_index(headers, TOTAL_HEADERS)
    ni = _header_index(headers, NAME_HEADERS)
    if ci is None and ni is None:
        return None       # no header row we understand — fall back to free-form
    if pi is not None and pi == ti:
        pi = None
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        get = lambda i: (r[i].strip() if i is not None and i < len(r) else "")
        line = ParsedLine(raw=", ".join(c for c in r if c.strip()))
        line.code = get(ci)
        line.name = get(ni)
        line.qty = _int(get(qi)) if qi is not None else None
        line.cost = _money(get(pi)) if pi is not None else None
        total = _money(get(ti)) if ti is not None else None
        if line.cost is None and total is not None and line.qty:
            line.cost = (total / line.qty).quantize(Decimal("0.0001"))
        if not line.code:
            m = CODE.search(line.name) or CODE.search(line.raw)
            if m:
                line.code = m.group(1)
        if line.qty is None:
            line.qty = 1 if (line.code or line.name) else None
        _flag(line)
        out.append(line)
    return out


def _parse_freeform(text):
    out = []
    for raw in text.replace("\r", "").split("\n"):
        s = raw.strip()
        if not s or s.lower().startswith(("subtotal", "total", "tax", "shipping", "order #", "order number")):
            continue
        line = ParsedLine(raw=s)
        m = CODE.search(s)
        if m:
            line.code = m.group(1)
            s_wo = s[:m.start()] + " " + s[m.end():]
        else:
            s_wo = s
        q = QTY.search(s_wo)
        if q:
            line.qty = int(next(g for g in q.groups() if g))
            s_wo = s_wo[:q.start()] + " " + s_wo[q.end():]
        amounts = [_money(a) for a in re.findall(r"\$\s*[\d,]+(?:\.\d{1,4})?", s_wo)]
        if not amounts:
            amounts = [_money(a) for a in re.findall(r"(?<![\d.])\d{1,3}(?:,\d{3})*\.\d{2}(?![\d.])", s_wo)]
        amounts = [a for a in amounts if a is not None]
        if line.qty is None:
            # a lone small integer is the quantity (e.g. "...Hobby Box   4   1,288.76")
            ints = [int(t) for t in re.findall(r"(?:^|\s)(\d{1,4})(?=\s|$)", s_wo)]
            ints = [i for i in ints if 0 < i < 10000]
            if len(ints) == 1:
                line.qty = ints[0]
        if amounts:
            if len(amounts) >= 2 and line.qty:
                a, b = amounts[-2], amounts[-1]
                # unit then total, or total then unit — pick the pair that multiplies
                if (a * line.qty).quantize(Decimal("0.01")) == b.quantize(Decimal("0.01")):
                    line.cost = a
                elif (b * line.qty).quantize(Decimal("0.01")) == a.quantize(Decimal("0.01")):
                    line.cost = b
                else:
                    line.cost = min(a, b)
            else:
                line.cost = amounts[0]
        # whatever is left (minus money/qty noise) is the description
        name = re.sub(r"\$\s*[\d,]+(?:\.\d{1,4})?", " ", s_wo)
        name = re.sub(r"(?<![\d.,])\d{1,3}(?:,\d{3})*\.\d{2,4}(?![\d.])", " ", name)
        name = re.sub(r"\b(qty|quantity|each|ea|pcs|units?|x)\b\.?:?", " ", name, flags=re.I)
        name = re.sub(r"^[\s\-–—:|,.]+|[\s\-–—:|,.]+$", "", name)
        name = re.sub(r"\(\s*\)|\[\s*\]|(?<!\S)[@#:|,](?!\S)", " ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" -–|,@:")
        if line.qty is not None:
            # drop the lone quantity token that is still sitting in the description
            name = re.sub(r"(?:^|\s)" + str(line.qty) + r"(?=\s|$)", " ", name, count=1)
            name = re.sub(r"\s{2,}", " ", name).strip()
        line.name = name
        if not line.code and not line.name:
            continue
        if line.qty is None:
            line.qty = 1
        _flag(line)
        out.append(line)
    return out


def _flag(line):
    if not line.qty or line.qty <= 0:
        line.problems.append("quantity")
    if line.cost is None or line.cost < 0:
        line.problems.append("unit cost")


def match_products(lines, Product):
    """Fill product_id / product_label by UPC, extra barcode or SKU."""
    for line in lines:
        p = Product.find_by_code(line.code) if line.code else None
        if p is None and line.name:
            exact = Product.objects.filter(name__iexact=line.name).first()
            p = exact
        if p is not None:
            line.product_id = p.pk
            line.product_label = f"{p.sku} — {p.name}"
        elif "product" not in line.problems:
            line.problems.append("product")
    return lines
