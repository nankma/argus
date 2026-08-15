"""
Mock response data for news_sources.py fetchers, shaped to match real
responses captured during live verification (see docs/current/ai-news-sources.md
for when/how each source was checked). Perigon's shape is NOT independently
verified — no API key was available to test against the real service, so
its fixture only matches what news_sources.fetch_perigon expects, not a
confirmed real response.
"""

HACKERNEWS_RESPONSE = {
    "hits": [
        {
            "title": "Show HN: A tool for X",
            "url": "https://example.com/show-hn-x",
            "created_at": "2026-08-05T12:00:00.000Z",
            "points": 42,
            "objectID": "12345",
        },
        {
            "title": "Ask HN: How do you do Y?",
            "url": None,
            "created_at": "2026-08-05T11:00:00.000Z",
            "points": 10,
            "objectID": "12346",
        },
    ]
}

ARXIV_RESPONSE = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2608.00001v1</id>
    <title>A Study of Fake Papers for Testing</title>
    <updated>2026-08-05T00:00:00Z</updated>
    <published>2026-08-04T00:00:00Z</published>
    <link href="https://arxiv.org/abs/2608.00001v1" rel="alternate" type="text/html"/>
    <summary>This is a fake abstract used only for testing arXiv parsing.</summary>
  </entry>
</feed>
"""

RSS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Fake Blog</title>
    <link>https://example.com/blog</link>
    <item>
      <title>Fake Blog Post One</title>
      <link>https://example.com/blog/post-one</link>
      <pubDate>Wed, 05 Aug 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Fake Blog Post Two</title>
      <link>https://example.com/blog/post-two</link>
      <pubDate>Wed, 05 Aug 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

NEWSAPI_RESPONSE = {
    "status": "ok",
    "totalResults": 1,
    "articles": [
        {
            "source": {"id": "fake-source", "name": "Fake News Outlet"},
            "title": "Fake NewsAPI Article",
            "description": "A fake article for testing.",
            "url": "https://example.com/newsapi-article",
            "publishedAt": "2026-08-05T12:00:00Z",
        }
    ],
}

GNEWS_RESPONSE = {
    "totalArticles": 1,
    "articles": [
        {
            "title": "Fake GNews Article",
            "description": "A fake article for testing.",
            "url": "https://example.com/gnews-article",
            "publishedAt": "2026-08-05T12:00:00Z",
            "source": {"name": "Fake GNews Outlet", "url": "https://example.com"},
        }
    ],
}

PERIGON_RESPONSE = {
    "status": "OK",
    "numResults": 1,
    "articles": [
        {
            "title": "Fake Perigon Article",
            "url": "https://example.com/perigon-article",
            "summary": "A fake article for testing.",
            "pubDate": "2026-08-05T12:00:00Z",
            "source": {"domain": "example.com"},
        }
    ],
}
