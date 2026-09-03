"""PerigonAdapter -- credential-gated, configured via a news_source.api
entry with type: perigon (see news_sources._api_sources_from_settings)."""

from datetime import datetime

import requests

from news_adapters._util import _parse_iso_published, _raise_for_status


class PerigonAdapter:
    TYPE = "perigon"

    def initialize(self, config: dict) -> None:
        self._api_key = config["api-key"]

    def pull(self, query: str, max_results: int = 5, since: datetime | None = None,
             section: str | None = None) -> list[dict]:
        """Deliberately ignores `since` and `section` (accepted only to
        satisfy the NewsSourceAdapter Protocol uniformly):
          - no `since` -- Perigon's date-filter behavior is simply
            unverified (no API key available to test against the live
            service, same caveat as its response-shape mapping below) --
            not worth trusting an unconfirmed server-side param when
            news_ingest.py's client-side filter works regardless.
          - no `section` -- Perigon's API has no top-headlines equivalent,
            so there's nothing to switch to; it always runs the same
            query-based search."""
        resp = requests.get(
            "https://api.perigon.io/v1/all",
            params={"q": query, "size": max_results, "apiKey": self._api_key},
            timeout=10,
        )
        _raise_for_status(resp)
        return [
            {
                "title": a.get("title"),
                "link": a.get("url"),
                "source": (a.get("source") or {}).get("domain", "Perigon"),
                "summary": a.get("summary"),
                "published": a.get("pubDate"),
                "published_dt": _parse_iso_published(a.get("pubDate")),
            }
            for a in resp.json().get("articles", [])
        ]
