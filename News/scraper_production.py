"""
Zolotel — Production News Scraper for AI Healthcare Intelligence (Async)
Scrapes Google News RSS feeds for the 7 healthcare-AI vertical keywords.

Filter logic (Option A): keep EVERY article returned by a vertical include-keyword
search. No exclude-keyword filtering and no entity requirement at scrape time —
the LLM processor decides relevance downstream. Each article is tagged with the
seed vertical of the keyword that found it.

Reads keywords from:  Healthcare_AI_Taxonomy.xlsx  ->  sheet "Vertical Keywords"
Writes raw articles to PostgreSQL (Neon) table:  raw_articles
"""

import asyncio
import aiohttp
import feedparser
import psycopg
from psycopg.rows import dict_row
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
import os
import logging
import random
import re
import pandas as pd
from typing import List, Dict, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ─── Configuration ────────────────────────────────────────────────────────────
LOOKBACK_DAYS = 15            # how far back the RSS query looks
SAVE_WINDOW_DAYS = 1          # keep only articles published within the last N days
                              #   (daily-cron incremental pattern; set higher for backfill)
MAX_CONCURRENT_REQUESTS = 5   # limit concurrent RSS requests
REQUEST_DELAY = 1             # base delay between requests (seconds)
EXCEL_FILE_PATH = 'Healthcare_AI_Taxonomy.xlsx'
KEYWORDS_SHEET = 'Vertical Keywords'

# Google News region — healthcare-AI coverage (FDA / CMS / HHS / EMA) is US-led.
# Switch to 'en-IN' / 'IN' / 'IN:en' if India-region coverage is preferred.
GOOGLE_NEWS_HL = 'en-US'
GOOGLE_NEWS_GL = 'US'
GOOGLE_NEWS_CEID = 'US:en'

# Rotate User-Agents to avoid fingerprinting
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

# UTM / tracking params stripped when building canonical_url
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'gclid', 'fbclid', 'mc_cid', 'mc_eid', 'igshid', 'ref', 'cmpid',
}


def load_keywords_from_excel() -> List[Tuple[str, str]]:
    """
    Load vertical include-keywords from the taxonomy workbook.

    Returns a list of (keyword, vertical) pairs. Each keyword carries the
    vertical it belongs to so scraped articles can be tagged with a seed vertical.
    Exclude keywords are intentionally NOT loaded (Option A).
    """
    logging.info("Loading vertical keywords from taxonomy workbook...")

    # Title in row 1, column headers in row 2 -> header=1 (0-indexed)
    df = pd.read_excel(EXCEL_FILE_PATH, sheet_name=KEYWORDS_SHEET, header=1)

    keyword_vertical_pairs: List[Tuple[str, str]] = []
    seen = set()

    for _, row in df.iterrows():
        vertical_raw = row.get('Vertical')
        include_raw = row.get('Include Keywords')

        if pd.isna(vertical_raw) or pd.isna(include_raw):
            continue

        # "V01 — Drug Discovery & R&D" -> "Drug Discovery & R&D"
        vertical = str(vertical_raw)
        if '—' in vertical:
            vertical = vertical.split('—', 1)[1]
        elif '-' in vertical and vertical[:1].upper() == 'V':
            vertical = vertical.split('-', 1)[1]
        vertical = vertical.strip()

        # Extract keywords between double quotes
        keywords = re.findall(r'"([^"]+)"', str(include_raw))
        for kw in keywords:
            key = (kw.lower(), vertical.lower())
            if key in seen:
                continue
            seen.add(key)
            keyword_vertical_pairs.append((kw, vertical))

    n_verticals = len({v for _, v in keyword_vertical_pairs})
    logging.info(
        f"Loaded {len(keyword_vertical_pairs)} include-keywords across {n_verticals} verticals"
    )
    return keyword_vertical_pairs


def extract_domain(url: str) -> str:
    """Return the registrable host of a URL (lowercased, no leading www.)."""
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith('www.') else host
    except Exception:
        return ''


def canonicalize_url(url: str) -> str:
    """Strip tracking params and fragment for a stable canonical_url."""
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    except Exception:
        return url


def clean_title(raw_title: str, source: str) -> str:
    """Remove the trailing ' - Source' / ' | Source' suffix Google News appends."""
    title = raw_title
    if source:
        patterns = [f' - {source}', f' | {source}', f' – {source}',
                    f'- {source}', f'| {source}']
        for pattern in patterns:
            if title.endswith(pattern):
                return title[:-len(pattern)].strip()
    # Fallback: drop anything after the last separator
    return raw_title.rsplit(' - ', 1)[0].rsplit(' | ', 1)[0].rsplit(' – ', 1)[0].strip()


async def fetch_feed_async(session: aiohttp.ClientSession, keyword: str, vertical: str,
                           lookback_days: int, semaphore: asyncio.Semaphore) -> Dict:
    """Asynchronously fetch one RSS feed with rate limiting and retry logic."""
    encoded_keyword = quote(keyword)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_keyword}+when:{lookback_days}d"
        f"&hl={GOOGLE_NEWS_HL}&gl={GOOGLE_NEWS_GL}&ceid={GOOGLE_NEWS_CEID}"
    )

    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
    }

    async with semaphore:
        for attempt in range(3):
            try:
                jitter = random.uniform(0.5, 1.5)
                await asyncio.sleep(REQUEST_DELAY * jitter)

                async with session.get(rss_url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 503:
                        wait = (attempt + 1) * 10
                        logging.warning(f"503 for '{keyword}' (attempt {attempt+1}/3), waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    if response.status == 429:
                        wait = (attempt + 1) * 30
                        logging.warning(f"429 rate limited for '{keyword}', waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    content = await response.text()
                    feed = feedparser.parse(content)

                    if feed.bozo and hasattr(feed, 'bozo_exception'):
                        logging.warning(f"Feed parse warning for '{keyword}': {feed.bozo_exception}")

                    return {'keyword': keyword, 'vertical': vertical, 'feed': feed, 'success': True}

            except Exception as e:
                logging.error(f"Error fetching feed for '{keyword}' (attempt {attempt+1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep((attempt + 1) * 5)

    return {'keyword': keyword, 'vertical': vertical, 'feed': None, 'success': False}


async def scrape_news_async(keyword_vertical_pairs: List[Tuple[str, str]],
                            lookback_days: int = LOOKBACK_DAYS) -> List[Dict]:
    """Scrape news asynchronously for every (keyword, vertical) pair (Option A: keep all)."""
    all_articles: List[Dict] = []
    seen_links = set()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            fetch_feed_async(session, kw, vert, lookback_days, semaphore)
            for kw, vert in keyword_vertical_pairs
        ]
        logging.info(f"Fetching {len(tasks)} RSS feeds (max {MAX_CONCURRENT_REQUESTS} concurrent)...")
        results = await asyncio.gather(*tasks)

    successful_fetches = 0
    for result in results:
        if not result['success'] or not result['feed'] or not result['feed'].entries:
            continue

        successful_fetches += 1
        keyword = result['keyword']
        vertical = result['vertical']
        feed = result['feed']

        for entry in feed.entries:
            raw_title = entry.get("title", "")
            link = entry.get("link", "")
            if not link or link in seen_links:
                continue

            # Parse date
            try:
                pubdate = datetime(*entry.published_parsed[:6])
            except Exception:
                pubdate = datetime.now()

            # Extract source name + raw snippet from the RSS description
            source = ""
            raw_snippet = ""
            if "description" in entry:
                soup = BeautifulSoup(entry.description, "html.parser")
                font_tag = soup.find("font")
                if font_tag:
                    source = font_tag.text.strip()
                raw_snippet = soup.get_text(" ", strip=True)

            title = clean_title(raw_title, source)

            seen_links.add(link)

            # Option A: no exclude filtering, no entity requirement — keep it.
            all_articles.append({
                "title": title,
                "url": link,
                "canonical_url": canonicalize_url(link),
                "source": source,
                "source_domain": extract_domain(link),
                "published_date": pubdate,
                "raw_snippet": raw_snippet,
                "raw_content": "",          # full-text fetch left to the processor stage
                "search_keyword": keyword,
                "vertical_seed": vertical,  # seed only; LLM assigns the real primary_vertical
                "source_type": None,        # classified later by the LLM processor
            })

    logging.info(f"Successfully fetched {successful_fetches}/{len(keyword_vertical_pairs)} feeds")
    logging.info(f"Collected {len(all_articles)} unique articles (Option A: all keyword matches kept)")
    return all_articles


def get_db_connection():
    """Get database connection from environment variable (Neon)."""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL environment variable not set")
    return psycopg.connect(database_url, row_factory=dict_row)


def save_to_database(articles: List[Dict]):
    """Save scraped articles to the raw_articles table."""
    if not articles:
        logging.info("No articles to save")
        return

    # Keep only articles published within the save window (daily-incremental pattern)
    cutoff = date.today() - timedelta(days=SAVE_WINDOW_DAYS)
    articles = [a for a in articles if a['published_date'].date() >= cutoff]
    logging.info(f"After date filter (>= {cutoff}): {len(articles)} articles")

    if not articles:
        logging.info("No articles in date range")
        return

    conn = get_db_connection()

    insert_query = """
        INSERT INTO raw_articles (
            title, url, canonical_url, source, source_domain,
            published_date, raw_snippet, raw_content,
            search_keyword, vertical_seed, source_type, processing_status
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new'
        )
        ON CONFLICT (url) DO NOTHING
    """

    saved_count = 0
    failed_count = 0

    for article in articles:
        try:
            cur = conn.cursor()
            cur.execute(insert_query, (
                article['title'],
                article['url'],
                article['canonical_url'],
                article['source'],
                article['source_domain'],
                article['published_date'],
                article['raw_snippet'],
                article['raw_content'],
                article['search_keyword'],
                article['vertical_seed'],
                article['source_type'],
            ))
            conn.commit()
            cur.close()
            saved_count += 1
        except Exception as e:
            conn.rollback()
            failed_count += 1
            logging.error(f"Error saving '{article.get('title', 'Unknown')[:50]}...': {e}")

    conn.close()

    logging.info(f"✅ Saved {saved_count} new articles to raw_articles")
    if failed_count > 0:
        logging.warning(f"⚠️  Failed to save {failed_count} articles")


async def main_async():
    """Main async scraping function."""
    logging.info("=" * 60)
    logging.info("Zolotel — Healthcare AI News Scraping Job (Async)")
    logging.info("=" * 60)

    keyword_vertical_pairs = load_keywords_from_excel()

    logging.info(f"Searching {len(keyword_vertical_pairs)} vertical keywords")
    logging.info(f"Lookback period: {LOOKBACK_DAYS} days | Save window: {SAVE_WINDOW_DAYS} day(s)")

    articles = await scrape_news_async(
        keyword_vertical_pairs=keyword_vertical_pairs,
        lookback_days=LOOKBACK_DAYS,
    )

    save_to_database(articles)

    logging.info("=" * 60)
    logging.info("Scraping Job Complete")
    logging.info("=" * 60)


def main():
    """Entry point for the scraper."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
