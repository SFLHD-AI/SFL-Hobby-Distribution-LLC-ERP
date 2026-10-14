"""
Plaid provider — plain ``requests`` calls, no SDK.

Credentials come from Settings (``CompanySettings.plaid_client_id`` /
``plaid_secret`` / ``plaid_env``) and fall back to the ``PLAID_*`` values in
``config/settings.py`` (the .env file). ``plaid_env`` picks the host:
sandbox -> https://sandbox.plaid.com, production -> https://production.plaid.com.

Sign convention: Plaid reports **positive for money leaving the account**
(a purchase on a card or a debit from checking is +25.00). We negate so
every BankTransaction is + into / − out of the account.

Dates: Plaid ``authorized_date`` is when the card was swiped and ``date`` is
when it posted (or, while pending, the date Plaid expects it to post). We
store ``date = authorized_date or date`` and ``posted_date = date`` once the
row is no longer pending.
"""
from datetime import date
from decimal import Decimal

import requests
from django.conf import settings

from banking.providers.base import (BankFeedProvider, ProviderError, ProviderNotConfigured,
                                    map_account_type)

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://sandbox.plaid.com",   # Plaid retired development; sandbox is the test env
    "production": "https://production.plaid.com",
}
TIMEOUT = 30
SYNC_PAGE = 500
MAX_SYNC_PAGES = 200


def _to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _dec(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


class PlaidProvider(BankFeedProvider):
    name = "plaid"
    label = "Plaid"

    def __init__(self, client_id=None, secret=None, env=None, session=None):
        if client_id is None or secret is None or env is None:
            cs = self._company_settings()
            client_id = client_id if client_id is not None else (
                (cs.plaid_client_id if cs else "") or getattr(settings, "PLAID_CLIENT_ID", ""))
            secret = secret if secret is not None else (
                (cs.plaid_secret if cs else "") or getattr(settings, "PLAID_SECRET", ""))
            env = env if env is not None else (
                (cs.plaid_env if cs and cs.plaid_env else "") or getattr(settings, "PLAID_ENV", "sandbox"))
        self.client_id = (client_id or "").strip()
        self.secret = (secret or "").strip()
        self.env = (env or "sandbox").strip().lower()
        self.base_url = HOSTS.get(self.env, HOSTS["sandbox"])
        self.session = session or requests.Session()

    @staticmethod
    def _company_settings():
        try:
            from core.models import CompanySettings
            return CompanySettings.get()
        except Exception:    # pragma: no cover - settings table missing during setup
            return None

    # --- plumbing ------------------------------------------------------------
    def is_configured(self):
        return bool(self.client_id and self.secret)

    def _post(self, path, payload):
        if not self.is_configured():
            raise ProviderNotConfigured(
                "Plaid is not set up. Enter the client id and secret in Settings or the .env file.")
        body = {"client_id": self.client_id, "secret": self.secret}
        body.update(payload)
        try:
            resp = self.session.post(f"{self.base_url}{path}", json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach Plaid: {exc}", code="NETWORK") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error_code")):
            code = data.get("error_code", f"HTTP_{resp.status_code}") if isinstance(data, dict) else ""
            msg = (data.get("error_message") or data.get("display_message")
                   or f"Plaid returned HTTP {resp.status_code}") if isinstance(data, dict) else ""
            raise ProviderError(msg, code=code, payload=data)
        return data

    # --- interface -------------------------------------------------------------
    def create_link_token(self, user, redirect_uri=None):
        cs = self._company_settings()
        payload = {
            "user": {"client_user_id": str(getattr(user, "pk", None) or "owner")},
            "client_name": (cs.company_name if cs else None) or "SFL ERP System",
            "products": list(getattr(settings, "PLAID_PRODUCTS", ["transactions"])),
            "country_codes": list(getattr(settings, "PLAID_COUNTRY_CODES", ["US"])),
            "language": "en",
            "transactions": {"days_requested": 730},
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        data = self._post("/link/token/create", payload)
        return data["link_token"]

    def exchange_public_token(self, public_token):
        data = self._post("/item/public_token/exchange", {"public_token": public_token})
        return {"access_token": data["access_token"], "item_id": data.get("item_id", "")}

    def get_accounts(self, access_token):
        data = self._post("/accounts/get", {"access_token": access_token})
        out = []
        for a in data.get("accounts", []):
            bal = a.get("balances") or {}
            out.append({
                "external_account_id": a.get("account_id", ""),
                "name": a.get("name") or a.get("official_name") or "Account",
                "official_name": a.get("official_name") or "",
                "mask": a.get("mask") or "",
                "type": a.get("type") or "",
                "subtype": a.get("subtype") or "",
                "account_type": map_account_type(a.get("type"), a.get("subtype")),
                "current_balance": _dec(bal.get("current")),
                "available_balance": _dec(bal.get("available")),
            })
        return out

    def sync_transactions(self, access_token, cursor=""):
        added, modified, removed = [], [], []
        next_cursor = cursor or ""
        for _ in range(MAX_SYNC_PAGES):
            payload = {"access_token": access_token, "count": SYNC_PAGE,
                       "options": {"include_original_description": True}}
            if next_cursor:
                payload["cursor"] = next_cursor
            data = self._post("/transactions/sync", payload)
            added += [self._map_txn(t) for t in data.get("added", [])]
            modified += [self._map_txn(t) for t in data.get("modified", [])]
            removed += [r.get("transaction_id", "") for r in data.get("removed", []) if r.get("transaction_id")]
            next_cursor = data.get("next_cursor", next_cursor) or next_cursor
            if not data.get("has_more"):
                break
        return {"added": added, "modified": modified, "removed": removed, "next_cursor": next_cursor}

    def remove_item(self, access_token):
        self._post("/item/remove", {"access_token": access_token})

    # --- mapping ---------------------------------------------------------------
    @staticmethod
    def _map_txn(t):
        pending = bool(t.get("pending"))
        posted = _to_date(t.get("date"))
        authorized = _to_date(t.get("authorized_date"))
        amount = _dec(t.get("amount")) or Decimal("0.00")
        return {
            "external_id": t.get("transaction_id", ""),
            "external_account_id": t.get("account_id", ""),
            "date": authorized or posted,
            "posted_date": None if pending else posted,
            "amount": -amount,                               # Plaid: + = money out
            "description": (t.get("name") or t.get("original_description") or t.get("merchant_name") or "")[:300],
            "original_description": (t.get("original_description") or "")[:500],
            "merchant_name": (t.get("merchant_name") or "")[:200],
            "pending": pending,
            "check_number": (t.get("check_number") or "")[:20],
            "pending_external_id": t.get("pending_transaction_id") or "",
        }
