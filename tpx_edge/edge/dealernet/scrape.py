"""Playwright scraper backend - NOT BUILT.

Per the working agreement this backend is only implemented once
DEALERNET_ACCESS=scrape is confirmed and Dealernet's terms have been checked.
Design agreed so far:
  * one-time headed login, session persisted to storage_state.json (gitignored)
  * <= 1 page every 3 seconds, only the products explicitly requested
  * left column -> bids, right column -> asks, each with member id + quantity
"""
from __future__ import annotations

from typing import Optional

from .base import DealernetQuote, DealernetSource


class ScrapeSource(DealernetSource):
    name = "scrape"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "DEALERNET_ACCESS=scrape is not enabled yet. Confirm the mode (and Dealernet's terms) "
            "and the Playwright backend will be built; use manual or csv for now."
        )

    def get_quote(self, product: str) -> Optional[DealernetQuote]:  # pragma: no cover
        raise NotImplementedError
