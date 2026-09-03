"""NewsApiAdapter -- credential-gated, configured via a news_source.api
entry with type: newsapi (see news_sources._api_sources_from_settings)."""

from datetime import datetime

import requests

from news_adapters._util import _parse_iso_published, _raise_for_status


def _newsapi_articles(resp) -> list[dict]:
    """Shared by both NewsAPI endpoints -- /v2/everything for a real search
    and /v2/top-headlines for a section pull. Same response shape."""
    return [
        {
            "title": a.get("title"),
            "link": a.get("url"),
            "source": (a.get("source") or {}).get("name", "NewsAPI"),
            "summary": a.get("description"),
            "published": a.get("publishedAt"),
            "published_dt": _parse_iso_published(a.get("publishedAt")),
        }
        for a in resp.json().get("articles", [])
    ]


class NewsApiAdapter:
    TYPE = "newsapi"

    def initialize(self, config: dict) -> None:
        self._api_key = config["api-key"]

    def pull(self, query: str, max_results: int = 5, since: datetime | None = None,
             section: str | None = None) -> list[dict]:
        """Deliberately ignores `since` (accepted only to satisfy the
        NewsSourceAdapter Protocol uniformly) -- news_ingest.py gets
        "everything since last pull" for this source via a client-side
        filter on published_dt instead (see its own module docstring),
        which sidesteps a real finding from live-testing this 2026-08-16:
        NewsAPI's free "Developer" tier has an undocumented ~24-36h article
        delay -- `from=<24h ago>` returned 0 results live, `from=<36h ago>`
        returned 380. Since news_ingest.py pulls NewsAPI once every 24h,
        a server-side `from=last_pulled_at` would frequently return
        nothing at all -- worse than a flat top-N, not better. Client-side
        filtering has no such failure mode: it just takes whatever
        NewsAPI's own delayed index currently has and keeps what's new.

        A `section` switches to /v2/top-headlines, which needs no query at
        all. That matters more here than anywhere else: this source is a
        multilingual aggregator, so an unconstrained query returns whatever
        matches globally. Measured 2026-08-21 -- "AOI" came back half Chinese
        (AOI is heavily covered by the Taiwanese electronics press) plus
        Japanese anime (AOI is also a name), and "Bitcoin" returned
        Spanish-language finance. All 65 cached articles from this source were
        Chinese, against 1 from every other source combined."""
        if section:
            params = {
                "category": section,
                "language": "en",
                "pageSize": max_results,
                "apiKey": self._api_key,
            }
            resp = requests.get("https://newsapi.org/v2/top-headlines",
                                params=params, timeout=10)
            _raise_for_status(resp)
            return _newsapi_articles(resp)
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                # Pinned to English, matching GNewsAdapter. Without it this
                # source returned 65 of 65 articles in Chinese, because
                # news_ingest._queries_for_source rotates through subscriber
                # interest text as the query and several subscribers store
                # theirs in Chinese (機器人科技, 科技財經, 光通訊). NewsAPI
                # obliged; GNews didn't, purely because it had this parameter.
                #
                # Not a cosmetic difference. A monolingual block inside a
                # mostly-English corpus clusters by LANGUAGE rather than
                # subject: those 65 articles formed a 28-strong "hot topic"
                # spanning Taiwanese stocks, optical networking, a Pixel phone
                # review and robot touch sensors, with mean pairwise similarity
                # 0.71 -- and they simultaneously dominated the
                # farthest-from-everything novelty pick, since anything in
                # another script is maximally distant from an English pool.
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": max_results,
                "apiKey": self._api_key,
            },
            timeout=10,
        )
        _raise_for_status(resp)
        return _newsapi_articles(resp)
