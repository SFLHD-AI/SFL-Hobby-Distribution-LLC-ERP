"""
Provider registry. ``get_provider("plaid")`` returns a configured instance.
Tests register a fake with ``register_provider``.
"""
from banking.providers.base import BankFeedProvider, ProviderError, ProviderNotConfigured  # noqa: F401

_REGISTRY = {}


def register_provider(name, factory):
    """``factory`` is a zero-arg callable returning a BankFeedProvider."""
    _REGISTRY[name] = factory


def unregister_provider(name):
    _REGISTRY.pop(name, None)


def get_provider(name="plaid"):
    if name in _REGISTRY:
        return _REGISTRY[name]()
    if name == "plaid":
        from banking.providers.plaid import PlaidProvider
        return PlaidProvider()
    raise ProviderNotConfigured(f"No bank feed provider named '{name}'.")
