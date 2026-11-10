"""
Allow-list of reliable data sources. Headlines are only ever collected from the
outlets below - nothing else is fetched.

Note: CNN's RSS feeds stopped updating in 2023, so CNN is read from lite.cnn.com,
CNN's official text-only edition.
"""
from __future__ import annotations

NEWS_SOURCES: list[dict] = [
    {"id": "cnn", "outlet": "CNN", "feed": "Top stories", "kind": "cnn_lite",
     "url": "https://lite.cnn.com/", "region": "US"},
    {"id": "toi-top", "outlet": "The Times of India", "feed": "Top stories", "kind": "rss",
     "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "region": "IN"},
    {"id": "toi-world", "outlet": "The Times of India", "feed": "World", "kind": "rss",
     "url": "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms", "region": None},
    {"id": "toi-business", "outlet": "The Times of India", "feed": "Business", "kind": "rss",
     "url": "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms", "region": "IN"},
    {"id": "nyt-world", "outlet": "The New York Times", "feed": "World", "kind": "rss",
     "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "region": None},
    {"id": "nyt-business", "outlet": "The New York Times", "feed": "Business", "kind": "rss",
     "url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "region": "US"},
    {"id": "bbc-world", "outlet": "BBC News", "feed": "World", "kind": "rss",
     "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "region": None},
    {"id": "bbc-business", "outlet": "BBC News", "feed": "Business", "kind": "rss",
     "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "region": "GB"},
    {"id": "guardian-world", "outlet": "The Guardian", "feed": "World", "kind": "rss",
     "url": "https://www.theguardian.com/world/rss", "region": None},
    {"id": "aljazeera", "outlet": "Al Jazeera", "feed": "All news", "kind": "rss",
     "url": "https://www.aljazeera.com/xml/rss/all.xml", "region": None},
    {"id": "et-markets", "outlet": "The Economic Times", "feed": "Markets", "kind": "rss",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "region": "IN"},
    {"id": "hindu-intl", "outlet": "The Hindu", "feed": "International", "kind": "rss",
     "url": "https://www.thehindu.com/news/international/feeder/default.rss", "region": None},
    {"id": "cnbc-world", "outlet": "CNBC", "feed": "World", "kind": "rss",
     "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html", "region": None},
    {"id": "dw", "outlet": "Deutsche Welle", "feed": "All news", "kind": "rss",
     "url": "https://rss.dw.com/rdf/rss-en-all", "region": None},
    {"id": "npr-world", "outlet": "NPR", "feed": "World", "kind": "rss",
     "url": "https://feeds.npr.org/1004/rss.xml", "region": None},
    {"id": "japantimes", "outlet": "The Japan Times", "feed": "Latest", "kind": "rss",
     "url": "https://www.japantimes.co.jp/feed/", "region": "JP"},
]

# Yahoo Finance chart API symbols. "country" links an instrument to the globe.
MARKETS: list[dict] = [
    {"symbol": "^NSEI", "name": "NIFTY 50", "kind": "index", "country": "IN", "primary": True},
    {"symbol": "^BSESN", "name": "BSE SENSEX", "kind": "index", "country": "IN"},
    {"symbol": "^INDIAVIX", "name": "India VIX", "kind": "volatility", "country": "IN"},
    {"symbol": "^GSPC", "name": "S&P 500", "kind": "index", "country": "US", "primary": True},
    {"symbol": "^IXIC", "name": "Nasdaq Composite", "kind": "index", "country": "US"},
    {"symbol": "^DJI", "name": "Dow Jones", "kind": "index", "country": "US"},
    {"symbol": "^VIX", "name": "CBOE VIX", "kind": "volatility", "country": "US"},
    {"symbol": "^N225", "name": "Nikkei 225", "kind": "index", "country": "JP", "primary": True},
    {"symbol": "000001.SS", "name": "SSE Composite", "kind": "index", "country": "CN", "primary": True},
    {"symbol": "^HSI", "name": "Hang Seng", "kind": "index", "country": "CN"},
    {"symbol": "^FTSE", "name": "FTSE 100", "kind": "index", "country": "GB", "primary": True},
    {"symbol": "^GDAXI", "name": "DAX", "kind": "index", "country": "DE", "primary": True},
    {"symbol": "^FCHI", "name": "CAC 40", "kind": "index", "country": "FR", "primary": True},
    {"symbol": "^KS11", "name": "KOSPI", "kind": "index", "country": "KR", "primary": True},
    {"symbol": "^TWII", "name": "TAIEX", "kind": "index", "country": "TW", "primary": True},
    {"symbol": "^AXJO", "name": "S&P/ASX 200", "kind": "index", "country": "AU", "primary": True},
    {"symbol": "^BVSP", "name": "Bovespa", "kind": "index", "country": "BR", "primary": True},
    {"symbol": "^GSPTSE", "name": "S&P/TSX", "kind": "index", "country": "CA", "primary": True},
    {"symbol": "^MXX", "name": "IPC Mexico", "kind": "index", "country": "MX", "primary": True},
    {"symbol": "^JKSE", "name": "Jakarta Composite", "kind": "index", "country": "ID", "primary": True},
    {"symbol": "^SSMI", "name": "SMI", "kind": "index", "country": "CH", "primary": True},
    {"symbol": "INR=X", "name": "USD/INR", "kind": "fx", "country": "IN"},
    {"symbol": "JPY=X", "name": "USD/JPY", "kind": "fx", "country": "JP"},
    {"symbol": "CNY=X", "name": "USD/CNY", "kind": "fx", "country": "CN"},
    {"symbol": "EURUSD=X", "name": "EUR/USD", "kind": "fx", "country": None},
    {"symbol": "GBPUSD=X", "name": "GBP/USD", "kind": "fx", "country": "GB"},
    {"symbol": "CL=F", "name": "WTI Crude Oil", "kind": "commodity", "country": None},
    {"symbol": "BZ=F", "name": "Brent Crude", "kind": "commodity", "country": None},
    {"symbol": "GC=F", "name": "Gold", "kind": "commodity", "country": None},
    {"symbol": "BTC-USD", "name": "Bitcoin", "kind": "crypto", "country": None},
]

PRIMARY_INDEX = {m["country"]: m["symbol"] for m in MARKETS if m.get("primary")}
FX_BY_COUNTRY = {m["country"]: m["symbol"] for m in MARKETS if m["kind"] == "fx" and m.get("country")}
