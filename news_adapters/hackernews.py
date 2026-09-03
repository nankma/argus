"""HackerNewsAdapter -- free, no API key, always registered directly by
news_sources._always_on_sources (not read from news_source.api -- see
that function's docstring)."""

from datetime import datetime

import requests

from news_adapters._util import _REQUEST_HEADERS, _parse_iso_published


class HackerNewsAdapter:
    TYPE = "hackernews"

    def initialize(self, config: dict) -> None:
        pass

    def pull(self, query: str, max_results: int = 5, since: datetime | None = None,
             section: str | None = None) -> list[dict]:
        """`since`, when given, adds Algolia's `numericFilters=created_at_i>X`
        -- confirmed live 2026-08-16 (45 hits in a 6h window for one query, all
        strictly after the cutoff) -- so news_ingest.py can ask for everything
        new since its last pull instead of a flat top-N regardless of how much
        is genuinely new. Omitted (server returns its default top-N) when
        `since` is None, e.g. the first-ever pull or agent.py's search_news,
        which has no "last pull" concept."""
        # `section` replaces the query for scheduled ingestion. HN's own
        # front_page ranking is a better relevance signal than anything a
        # query could express, and it carries no sampling bias toward what
        # subscribers already named.
        if section:
            params = {"tags": section, "hitsPerPage": max_results}
        else:
            params = {"query": query, "tags": "story", "hitsPerPage": max_results}
        if since is not None:
            params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"
        # front_page is a RANKING; search_by_date would re-sort it into
        # chronological order and throw away the only thing it was for.
        endpoint = "search" if section == "front_page" else "search_by_date"
        resp = requests.get(
            f"https://hn.algolia.com/api/v1/{endpoint}",
            params=params,
            timeout=10,
            headers=_REQUEST_HEADERS,
        )
        resp.raise_for_status()
        return [
            {
                "title": hit.get("title"),
                "link": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "source": "Hacker News",
                "summary": None,
                "published": hit.get("created_at"),
                "published_dt": _parse_iso_published(hit.get("created_at")),
            }
            for hit in resp.json().get("hits", [])
        ]
