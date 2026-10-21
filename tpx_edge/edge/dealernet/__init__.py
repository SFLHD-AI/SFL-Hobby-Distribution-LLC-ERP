"""Dealernet price sources."""


def live_source(settings):
    """The automatic Dealernet backend picked by DEALERNET_ACCESS, or None for manual/csv."""
    if settings.dealernet_access == "api":
        from .api import ApiSource

        key = settings.dealernet_api_key.get_secret_value() if settings.dealernet_api_key else ""
        return ApiSource(key, settings.dealernet_api_url)
    if settings.dealernet_access == "scrape":
        from .scrape import ScrapeSource  # raises NotImplementedError with guidance until built

        return ScrapeSource()
    return None
