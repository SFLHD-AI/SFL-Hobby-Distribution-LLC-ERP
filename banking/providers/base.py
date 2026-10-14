"""
Bank-feed provider interface.

A provider turns a third-party aggregator (Plaid today) into a handful of
plain Python calls that return **normalised dicts**, so ``banking.tasks``
never sees provider-specific JSON.

Normalised transaction dict (``sync_transactions`` ``added`` / ``modified``):

    {
      "external_id": str,              # provider transaction id
      "external_account_id": str,      # provider account id -> BankAccount.external_account_id
      "date": date,                    # when the money moved (authorised date when known)
      "posted_date": date | None,      # when it posted; None while pending
      "amount": Decimal,               # SIGNED: + into the account, − out of it
      "description": str,
      "merchant_name": str,
      "pending": bool,
      "check_number": str,
      "pending_external_id": str,      # id of the pending row this posted row replaces, or ""
    }

``removed`` is a list of external ids. Account dicts (``get_accounts``):

    {"external_account_id", "name", "official_name", "mask", "type", "subtype",
     "account_type",            # mapped to BankAccount.TYPES
     "current_balance": Decimal | None,   # natural sign: cards = amount owed (positive)
     "available_balance": Decimal | None}
"""


class ProviderError(Exception):
    """Something went wrong talking to the provider. ``code`` carries the
    provider's own error code (e.g. Plaid ``ITEM_LOGIN_REQUIRED``)."""

    def __init__(self, message, code="", payload=None):
        super().__init__(message)
        self.code = code or ""
        self.payload = payload or {}

    @property
    def needs_relink(self):
        return self.code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT",
                             "ITEM_LOCKED", "USER_SETUP_REQUIRED", "INVALID_CREDENTIALS")


class ProviderNotConfigured(ProviderError):
    """Credentials are missing; the UI shows set-up instructions instead."""


class BankFeedProvider:
    name = "base"
    label = "Bank feed"

    def is_configured(self):
        raise NotImplementedError

    def create_link_token(self, user, redirect_uri=None):
        """Short-lived token that opens the provider's connect widget."""
        raise NotImplementedError

    def exchange_public_token(self, public_token):
        """-> {"access_token": str, "item_id": str}"""
        raise NotImplementedError

    def get_accounts(self, access_token):
        """-> list of normalised account dicts (see module docstring)."""
        raise NotImplementedError

    def sync_transactions(self, access_token, cursor=""):
        """-> {"added": [...], "modified": [...], "removed": [ids], "next_cursor": str}"""
        raise NotImplementedError

    def remove_item(self, access_token):
        raise NotImplementedError


def map_account_type(type_, subtype):
    """Provider (Plaid-style) type/subtype -> BankAccount.account_type."""
    t, s = (type_ or "").lower(), (subtype or "").lower()
    if t == "credit":
        return "credit_card"
    if t == "loan":
        return "loan"
    if t == "depository":
        if s in ("savings", "money market", "cd"):
            return "savings"
        if s in ("paypal", "cash management", "prepaid"):
            return "payment_processor"
        return "checking"
    return "other"
