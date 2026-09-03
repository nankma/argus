"""GNewsAdapter -- credential-gated, configured via a news_source.api
entry with type: gnews (see news_sources._api_sources_from_settings)."""

from datetime import datetime

import requests

from news_adapters._util import _parse_iso_published, _raise_for_status


class GNewsAdapter:
    TYPE = "gnews"

    def initialize(self, config: dict) -> None:
        self._api_key = config["api-key"]

    def pull(self, query: str, max_results: int = 5, since: datetime | None = None,
             section: str | None = None) -> list[dict]:
        """`since`, when given, adds GNews's documented `from` (ISO 8601) date
        filter -- confirmed live 2026-08-16 (30 articles in a 24h window for
        one query, all recent). Note: `max` is capped at 10/request by GNews's
        own free tier regardless of what's asked (docs/current/ai-news-sources.md), so
        news_ingest.py's generous safety cap for time-filterable sources just
        gets silently clamped here, not an error."""
        params = {"lang": "en", "max": max_results, "apikey": self._api_key}
        if section:
            params["topic"] = section
        else:
            params["q"] = query
        if since is not None:
            params["from"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = requests.get(
            "https://gnews.io/api/v4/top-headlines" if section else "https://gnews.io/api/v4/search",
            params=params,
            timeout=10,
        )
        _raise_for_status(resp)
        return [
            {
                "title": a.get("title"),
                "link": a.get("url"),
                "source": (a.get("source") or {}).get("name", "GNews"),
                "summary": a.get("description"),
                "published": a.get("publishedAt"),
                "published_dt": _parse_iso_published(a.get("publishedAt")),
            }
            for a in resp.json().get("articles", [])
        ]
