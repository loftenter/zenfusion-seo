#!/usr/bin/env python3
"""
Competitor Page Monitor — Scans competitor sites for recently published pages.

Uses DataForSEO Labs (google/pages/live) to list a domain's ranking pages, then
fetches each page (direct HTTP with ScrapeOwl fallback) to extract publish/modify
dates from JSON-LD schema, Open Graph tags, meta tags, and HTTP headers. Pages
published/modified in the last N days are flagged as "new" and analyzed via the
DataForSEO on_page/instant_pages endpoint for title, meta description, H1, word
count, internal links, schema types, and topic detection.

Produces a White SaaS themed HTML report with overview cards, an SVG publishing
timeline, an expandable new-pages table, content strategy analysis, a gap alert
(client vs competitor keyword coverage), and recommendations.

Usage:
  python3 competitor_page_monitor.py \
      --competitors aquasana.com,culligan.com,springwellwater.com \
      --client softprowatersystems.com \
      --days 30 \
      --output /tmp/test_page_monitor

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD  — in ~/.hermes/.env
  SCRAPEOWL_API_KEY                     — optional, in ~/.hermes/.env
"""

import os
import sys
import json
import ssl
import re
import base64
import argparse
import html as html_mod
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict


# ── Config ─────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
API_BASE = "https://api.dataforseo.com/v3"
REQUEST_TIMEOUT = 60
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# Limit how many pages we deep-fetch per competitor during the scan to keep the
# run bounded.  DataForSEO Labs returns up to `limit` pages; we fetch dates for
# at most MAX_DATE_FETCH of them.
MAX_DATE_FETCH = 100


# ── Credentials ─────────────────────────────────────────────────────────────

def load_env():
    """Load environment variables from ~/.hermes/.env."""
    env = {}
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def get_credentials():
    """Return (dfs_login, dfs_password, scrapeowl_key)."""
    env = load_env()
    login = env.get("DATAFORSEO_LOGIN") or os.environ.get("DATAFORSEO_LOGIN")
    password = env.get("DATAFORSEO_PASSWORD") or os.environ.get("DATAFORSEO_PASSWORD")
    scrapeowl = env.get("SCRAPEOWL_API_KEY") or os.environ.get("SCRAPEOWL_API_KEY")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.",
              file=sys.stderr)
        print("Set them in ~/.hermes/.env or as environment variables.",
              file=sys.stderr)
        sys.exit(1)
    return login, password, scrapeowl


# ── DataForSEO helpers ─────────────────────────────────────────────────────

def dfs_request(path, payload=None, method="POST", timeout=REQUEST_TIMEOUT):
    """Make a DataForSEO API request and return parsed JSON or None."""
    login, password, _ = get_credentials()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"{API_BASE}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    if method == "POST" and payload is not None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            body = resp.read()
            return json.loads(body)
    except urllib.error.HTTPError as e:
        print(f"[WARN] DataForSEO HTTP {e.code} on {path}: {e.reason}",
              file=sys.stderr)
        try:
            err_body = e.read().decode()[:300]
            print(f"  body: {err_body}", file=sys.stderr)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[WARN] DataForSEO request failed on {path}: {e}", file=sys.stderr)
        return None


# ── Page fetching (ScrapeOwl / direct HTTP) ─────────────────────────────────

def fetch_page_html(url, scrapeowl_key=None, timeout=REQUEST_TIMEOUT):
    """Fetch raw HTML for a URL.  Tries direct HTTP first, falls back to ScrapeOwl.
    Returns (status_code, html_text, headers_dict) or (0, '', {})."""
    headers = {"User-Agent": USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except Exception:
        pass
    # ScrapeOwl fallback
    if scrapeowl_key:
        try:
            api_url = "https://api.scrapeowl.com/v1/"
            params = urllib.parse.urlencode({
                "api_key": scrapeowl_key, "url": url, "render_js": "1",
            })
            full = f"{api_url}?{params}"
            req2 = urllib.request.Request(full)
            with urllib.request.urlopen(req2, timeout=timeout, context=SSL_CTX) as resp:
                data = json.loads(resp.read())
                if data.get("status") == "success":
                    return 200, data.get("data", ""), {}
        except Exception:
            pass
    return 0, "", {}


# ── Date extraction ────────────────────────────────────────────────────────

def _parse_iso_date(date_str):
    """Parse an ISO-8601 date string and return a timezone-aware datetime or None."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    # Try common ISO formats
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str[:len(fmt) + 10], fmt) if "%z" in fmt else datetime.strptime(date_str[:19] if "T" in date_str else date_str[:10], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    # Fallback: fromisoformat
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def extract_dates_from_html(html_text, headers=None):
    """Extract published/modified dates from page HTML + headers.
    Returns (published_dt_or_None, modified_dt_or_None)."""
    published = None
    modified = None

    if not html_text:
        return None, None

    # 1. JSON-LD datePublished / dateModified
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.DOTALL | re.IGNORECASE,
    ):
        try:
            blob = json.loads(m.group(1))
        except Exception:
            continue
        for obj in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(obj, dict):
                continue
            for key in ("datePublished", "datePublishedISO", "dateCreated"):
                val = obj.get(key)
                if val and not published:
                    published = _parse_iso_date(val)
            for key in ("dateModified", "dateUpdatedISO"):
                val = obj.get(key)
                if val and not modified:
                    modified = _parse_iso_date(val)
        if published and modified:
            break

    # 2. Open Graph article:published_time / article:modified_time
    if not published:
        m = re.search(
            r'<meta[^>]+property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']',
            html_text, re.IGNORECASE,
        )
        if m:
            published = _parse_iso_date(m.group(1))
    if not modified:
        m = re.search(
            r'<meta[^>]+property=["\']article:modified_time["\']\s+content=["\']([^"\']+)["\']',
            html_text, re.IGNORECASE,
        )
        if m:
            modified = _parse_iso_date(m.group(1))

    # 3. Meta name="published" / "date" / "lastmod"
    for meta_name, target in (
        ("published", "published"), ("date", "published"),
        ("lastmod", "modified"), ("revised", "modified"),
    ):
        m = re.search(
            rf'<meta[^>]+name=["\']{meta_name}["\']\s+content=["\']([^"\']+)["\']',
            html_text, re.IGNORECASE,
        )
        if m:
            dt = _parse_iso_date(m.group(1))
            if dt and target == "published" and not published:
                published = dt
            if dt and target == "modified" and not modified:
                modified = dt

    # 4. <time> elements
    if not published:
        m = re.search(
            r'<time[^>]+datetime=["\']([^"\']+)["\'][^>]*(?:class=["\'][^"\']*entry-date[^"\']*["\'])?',
            html_text, re.IGNORECASE,
        )
        if m:
            published = _parse_iso_date(m.group(1))

    # 5. HTTP Last-Modified header (best as modified)
    if headers and not modified:
        lm = headers.get("Last-Modified") or headers.get("last-modified")
        if lm:
            try:
                dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
                dt = dt.replace(tzinfo=timezone.utc)
                modified = dt
            except Exception:
                pass

    # If we have modified but not published, treat modified as published proxy
    if modified and not published:
        published = modified
    return published, modified


# ── 1. fetch_competitor_pages ──────────────────────────────────────────────

def fetch_competitor_pages(domain, limit=100):
    """Fetch a list of pages for a competitor domain using DataForSEO Labs
    google/ranked_keywords/live, enriched with publish/modify dates fetched
    from the actual pages.

    We aggregate unique URLs from ranked keywords — each ranked keyword item
    contains a serp_item with the URL, title, and description of the ranking
    page.  We then deep-fetch each unique page to extract publish/modify dates
    from JSON-LD schema, Open Graph tags, meta tags, and HTTP headers.

    Returns a list of dicts:
        {url, title, description, published_date, modified_date, source}
    """
    print(f"[INFO] Fetching pages for {domain} (limit={limit}) ...")
    _, _, scrapeowl_key = get_credentials()

    # DataForSEO Labs: google/ranked_keywords/live
    # Returns keywords the domain ranks for, each with a serp_item containing
    # the ranking URL.  We aggregate unique URLs from these results.
    payload = [{"target": domain, "limit": min(limit * 3, 1000)}]
    resp = dfs_request("dataforseo_labs/google/ranked_keywords/live", payload=payload)
    if not resp:
        print(f"[WARN] No data returned for {domain}.", file=sys.stderr)
        return []

    if resp.get("status_code") != 20000:
        print(f"[WARN] DataForSEO status {resp.get('status_code')}: "
              f"{resp.get('status_message')}", file=sys.stderr)
        return []

    tasks = resp.get("tasks", [])
    if not tasks or tasks[0].get("status_code") != 20000:
        msg = tasks[0].get("status_message", "Unknown") if tasks else "No tasks"
        print(f"[WARN] Task failed for {domain}: {msg}", file=sys.stderr)
        return []

    try:
        items = tasks[0]["result"][0].get("items", [])
    except (KeyError, IndexError, TypeError):
        print(f"[WARN] No result in response for {domain}.", file=sys.stderr)
        return []

    # Aggregate unique URLs from ranked keywords
    page_map = {}  # url -> {title, description}
    for item in items:
        rse = item.get("ranked_serp_element", {}) or {}
        serp = rse.get("serp_item", {}) or {}
        url = serp.get("url", "")
        if not url or url in page_map:
            continue
        page_map[url] = {
            "title": serp.get("title", ""),
            "description": serp.get("description") or serp.get("pre_snippet", ""),
        }
        if len(page_map) >= limit:
            break

    if not page_map:
        print(f"[INFO] No ranking pages found for {domain}.", file=sys.stderr)
        return []

    print(f"[INFO] {domain}: {len(page_map)} unique ranking pages found. "
          f"Fetching dates ...")

    # Deep-fetch each page to extract publish/modify dates
    pages = []
    fetched = 0
    for url, info in page_map.items():
        published = None
        modified = None
        if fetched < MAX_DATE_FETCH:
            status, html_text, hdrs = fetch_page_html(url, scrapeowl_key=scrapeowl_key)
            if status == 200 and html_text:
                pub, mod = extract_dates_from_html(html_text, hdrs)
                published, modified = pub, mod
            fetched += 1

        pages.append({
            "url": url,
            "title": info["title"],
            "description": info["description"],
            "published_date": published,
            "modified_date": modified,
            "source": "dataforseo_ranked_keywords",
        })

    print(f"[OK] {domain}: {len(pages)} pages ({fetched} deep-fetched for dates).")
    return pages


# ── 2. identify_new_pages ──────────────────────────────────────────────────

def identify_new_pages(pages, days_back=30):
    """Filter to pages published/modified within the last N days.
    Pages without any date are excluded (we cannot determine freshness)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    new_pages = []
    for p in pages:
        pub = p.get("published_date")
        mod = p.get("modified_date")
        # Use the most recent of published/modified as the "freshness" date
        freshness = max(
            (d for d in (pub, mod) if d is not None),
            default=None,
        )
        if freshness and freshness >= cutoff:
            new_pages.append({
                **p,
                "freshness_date": freshness,
            })
    return new_pages


# ── 3. analyze_page_content ────────────────────────────────────────────────

def analyze_page_content(url):
    """Analyze a single URL using DataForSEO on_page/instant_pages.
    Returns a dict with title, meta_description, h1, word_count, topic,
    internal_links, schema_types, page_type."""
    print(f"[INFO] Analyzing {url} ...")
    payload = json.dumps([{"url": url}]).encode()
    login, password, _ = get_credentials()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    endpoint = f"{API_BASE}/on_page/instant_pages"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(endpoint, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[WARN] instant_pages failed for {url}: {e}", file=sys.stderr)
        return _analyze_page_fallback(url)

    if data.get("status_code") != 20000:
        print(f"[WARN] API status {data.get('status_code')}: {data.get('status_message')}", file=sys.stderr)
        return _analyze_page_fallback(url)

    tasks = data.get("tasks", [])
    if not tasks or tasks[0].get("status_code") != 20000:
        msg = tasks[0].get("status_message", "?") if tasks else "no tasks"
        print(f"[WARN] Task failed for {url}: {msg}", file=sys.stderr)
        return _analyze_page_fallback(url)

    try:
        item = tasks[0]["result"][0]["items"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[WARN] No items for {url}.", file=sys.stderr)
        return _analyze_page_fallback(url)

    meta = item.get("meta", {}) or {}
    checks = item.get("checks", {}) or {}
    htags = meta.get("htags", {}) or {}
    h1_list = htags.get("h1", []) if htags else []
    h1 = h1_list[0] if h1_list else ""

    title = meta.get("title", "")
    meta_desc = meta.get("description", "")

    # Word count: DataForSEO stores it in meta.content.plain_text_word_count
    content_info = meta.get("content", {}) or {}
    word_count = content_info.get("plain_text_word_count", 0) or 0
    if not word_count:
        # Fallback: estimate from content size
        text_size = content_info.get("plain_text_size", 0) or 0
        if text_size > 0:
            word_count = text_size // 6  # rough: avg 6 chars/word

    # Internal links: available directly in meta
    internal_links = meta.get("internal_links_count", 0) or 0

    # Schema types: instant_pages only reports has_micromarkup (boolean),
    # not the actual types.  We check the bool and try to extract types from
    # the raw HTML if we have it.  Fall back to "StructuredData" tag.
    schema_types = []
    if checks.get("has_micromarkup"):
        # Try to get actual types from the page content via a quick fetch
        _, _, scrapeowl_key = get_credentials()
        status, html_text, _ = fetch_page_html(url, scrapeowl_key=scrapeowl_key)
        if status == 200 and html_text:
            found_types = set()
            for m in re.finditer(
                r'"@type"\s*:\s*"([^"]+)"', html_text
            ):
                found_types.add(m.group(1))
            schema_types = list(found_types) if found_types else ["StructuredData"]
        else:
            schema_types = ["StructuredData"]

    # Topic detection from title + h1 + meta description
    topic = detect_topic(title, h1, meta_desc)

    # Page type classification
    page_type = classify_page_type(url, title, h1, schema_types, word_count)

    # Extract last_modified date from the API response (dict with
    # header/sitemap/meta_tag keys, each a date string or None)
    api_modified = item.get("last_modified")
    api_mod_date = None
    if isinstance(api_modified, dict):
        for k in ("meta_tag", "header", "sitemap"):
            v = api_modified.get(k)
            if v:
                api_mod_date = _parse_iso_date(v)
                if api_mod_date:
                    break

    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "h1": h1,
        "word_count": word_count,
        "topic": topic,
        "internal_links": internal_links,
        "schema_types": schema_types,
        "page_type": page_type,
        "api_modified_date": api_mod_date,
    }


def _analyze_page_fallback(url):
    """Lightweight fallback if the DataForSEO instant_pages call fails.
    Fetches HTML directly and extracts what we can."""
    _, _, scrapeowl_key = get_credentials()
    status, html_text, _ = fetch_page_html(url, scrapeowl_key=scrapeowl_key)
    if status != 200 or not html_text:
        return {
            "url": url, "title": "", "meta_description": "", "h1": "",
            "word_count": 0, "topic": "Unknown", "internal_links": 0,
            "schema_types": [], "page_type": "unknown",
        }
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.DOTALL | re.IGNORECASE)
    title = title_m.group(1).strip() if title_m else ""
    desc_m = re.search(
        r'<meta[^>]+name=["\']description["\']\s+content=["\']([^"\']*)["\']',
        html_text, re.IGNORECASE,
    )
    meta_desc = desc_m.group(1) if desc_m else ""
    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.DOTALL | re.IGNORECASE)
    h1 = re.sub(r"<[^>]+>", "", h1_m.group(1)).strip() if h1_m else ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    word_count = len(text.split())
    internal_links = len(re.findall(r'href=["\']/?([^"\']+)["\']', html_text))
    schema_types = re.findall(r'"@type"\s*:\s*"([^"]+)"', html_text)
    topic = detect_topic(title, h1, meta_desc)
    page_type = classify_page_type(url, title, h1, schema_types, word_count)
    return {
        "url": url, "title": title, "meta_description": meta_desc, "h1": h1,
        "word_count": word_count, "topic": topic, "internal_links": internal_links,
        "schema_types": list(set(schema_types)), "page_type": page_type,
    }


# ── Topic & page-type detection ─────────────────────────────────────────────

WATER_TREATMENT_TOPICS = {
    "water softener": ["softener", "softening", "soft water"],
    "water filtration": ["filtration", "filter", "filtered", "filtration system"],
    "reverse osmosis": ["reverse osmosis", "ro system", "ro water"],
    "whole house": ["whole house", "whole-home", "whole home"],
    "well water": ["well water", "well pump", "well treatment"],
    "iron removal": ["iron removal", "iron filter", "rust"],
    "chlorine": ["chlorine", "chlorination", "dechlorination"],
    "water testing": ["water test", "testing", "water quality", "contamination"],
    "salt-free": ["salt-free", "salt free", "saltless", "conditioner"],
    "drinking water": ["drinking water", "purifier", "purification"],
    "hard water": ["hard water", "hardness", "scale"],
    "maintenance": ["maintenance", "service", "repair", "install"],
    "cost & pricing": ["cost", "price", "pricing", "how much"],
    "comparison": ["vs", "versus", "compare", "comparison", "best"],
    "contaminants": ["contaminant", "lead", "pfas", "bacteria", "arsenic"],
    "shower & bath": ["shower", "bath", "bathroom"],
    "commercial": ["commercial", "industrial", "business"],
}


def detect_topic(title, h1, meta_desc):
    """Classify the page's topic from title/H1/meta description text."""
    combined = f"{title} {h1} {meta_desc}".lower()
    best_topic = "General"
    best_score = 0
    for topic, keywords in WATER_TREATMENT_TOPICS.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


def classify_page_type(url, title, h1, schema_types, word_count):
    """Classify page as blog, product, landing, or other."""
    url_lower = url.lower()
    title_lower = (title or "").lower()
    h1_lower = (h1 or "").lower()
    schema_str = " ".join(schema_types).lower() if schema_types else ""

    # Product indicators
    if (
        "product" in schema_str
        or "/product" in url_lower
        or "/products/" in url_lower
        or "buy" in title_lower
        or "shop" in title_lower
    ):
        return "product"

    # Blog / article indicators
    if (
        "article" in schema_str
        or "blog" in schema_str
        or "blogpost" in schema_str
        or "/blog/" in url_lower
        or "/blog" in url_lower
        or "news" in schema_str
        or word_count > 800
    ):
        return "blog"

    # Landing / category / pillar
    if (
        "/guide" in url_lower
        or "/learn" in url_lower
        or "/resource" in url_lower
        or "/category" in url_lower
        or "guide" in title_lower
        or "how to" in h1_lower
        or "ultimate" in title_lower
        or word_count > 500
    ):
        return "landing"

    return "other"


# ── Client keyword gap detection ────────────────────────────────────────────

def fetch_client_keywords(client_domain, limit=100):
    """Fetch top organic keywords the client ranks for (for gap detection)."""
    print(f"[INFO] Fetching client keywords for {client_domain} ...")
    payload = [{
        "target": client_domain,
        "limit": limit,
    }]
    resp = dfs_request("dataforseo_labs/google/ranked_keywords/live", payload=payload)
    if not resp:
        return []
    try:
        items = resp["tasks"][0]["result"][0].get("items", [])
    except (KeyError, IndexError, TypeError):
        return []
    keywords = []
    for item in items:
        kw = item.get("keyword_data", {}).get("keyword", "")
        if kw:
            keywords.append(kw.lower())
    print(f"[OK] Client ranks for {len(keywords)} keywords.")
    return keywords


def detect_gap_alert(new_pages, client_keywords):
    """Identify new competitor pages whose topics/keywords the client doesn't cover.
    Returns list of {url, title, topic, page_type, gap_reason}."""
    client_kw_set = set(client_keywords)
    gaps = []
    for page in new_pages:
        topic = page.get("topic", "General")
        title = page.get("title", "")
        # Build candidate keywords from title + topic
        candidate_keywords = set()
        for word in re.findall(r"[a-z]{3,}", f"{title} {topic}".lower()):
            candidate_keywords.add(word)
        # Also add the topic itself
        candidate_keywords.add(topic.lower())
        # Does the client rank for any of these?
        overlap = any(
            ck in client_kw_set or any(w in ck for w in candidate_keywords if len(w) > 4)
            for ck in client_keywords
        )
        if not overlap:
            gaps.append({
                "url": page.get("url", ""),
                "title": title,
                "topic": topic,
                "page_type": page.get("page_type", "unknown"),
                "gap_reason": f"Client has no content targeting '{topic}'",
            })
    return gaps


# ── 4. generate_monitor_report ─────────────────────────────────────────────

def _esc(text):
    return html_mod.escape(str(text), quote=True)


def _fmt_date(dt):
    if dt is None:
        return "—"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d")


def _build_timeline_svg(new_pages, days_back):
    """Build an SVG timeline showing when each competitor published, by date."""
    if not new_pages:
        return '<p class="muted">No new pages to display on timeline.</p>'

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    total_days = days_back

    # Group by domain + date
    domain_pages = defaultdict(lambda: defaultdict(list))
    domains = sorted({p.get("domain", "") for p in new_pages})
    for p in new_pages:
        d = p.get("freshness_date") or p.get("published_date")
        if d and d >= start:
            day_str = d.strftime("%Y-%m-%d")
            domain_pages[p.get("domain", "")][day_str].append(p)

    # SVG dimensions
    width = 900
    height = max(180, 40 + len(domains) * 50)
    margin_left = 140
    margin_right = 30
    margin_top = 30
    plot_w = width - margin_left - margin_right
    day_w = plot_w / max(total_days, 1)

    # X-axis date labels (every ~7 days)
    x_labels = ""
    for i in range(0, total_days + 1, 7):
        x = margin_left + i * day_w
        label_date = (start + timedelta(days=i)).strftime("%m/%d")
        x_labels += f'<text x="{x:.1f}" y="{height - 8}" font-size="10" fill="#64748b" text-anchor="middle">{label_date}</text>'

    # Y-axis domain rows + dots
    rows_svg = ""
    for idx, domain in enumerate(domains):
        y = margin_top + idx * 50 + 20
        rows_svg += f'<text x="{margin_left - 10}" y="{y + 4}" font-size="11" fill="#334155" text-anchor="end" font-weight="600">{_esc(domain)}</text>'
        rows_svg += f'<line x1="{margin_left}" y1="{y}" x2="{width - margin_right}" y2="{y}" stroke="#e2e8f0" stroke-width="1"/>'
        day_map = domain_pages[domain]
        for day_str, pages in day_map.items():
            d = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_offset = (d - start).days
            if day_offset < 0 or day_offset > total_days:
                continue
            x = margin_left + day_offset * day_w
            count = len(pages)
            radius = min(4 + count * 2, 14)
            rows_svg += (
                f'<circle cx="{x:.1f}" cy="{y}" r="{radius}" '
                f'fill="#2563eb" opacity="0.75">'
                f'<title>{_esc(domain)} — {day_str}: {count} new page(s)</title>'
                f'</circle>'
            )
            if count > 1:
                rows_svg += (
                    f'<text x="{x:.1f}" y="{y + 4}" font-size="9" fill="#fff" '
                    f'text-anchor="middle" font-weight="700">{count}</text>'
                )

    svg = f'''<svg viewBox="0 0 {width} {height}" width="100%" style="max-height:{height}px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px">
    <text x="{margin_left}" y="18" font-size="12" fill="#475569" font-weight="600">Publishing timeline (last {days_back} days)</text>
    {x_labels}
    {rows_svg}
    </svg>'''
    return svg


def generate_monitor_report(competitors, new_pages, output_path,
                            client_domain=None, days_back=30):
    """Generate the HTML monitor report.
    competitors: list of domain strings
    new_pages: list of analyzed page dicts (with analysis fields + domain + freshness_date)
    output_path: directory to write the report into
    Returns the path to the HTML file."""
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, "competitor_page_monitor_report.html")

    now = datetime.now(timezone.utc)
    run_date = now.strftime("%Y-%m-%d %H:%M UTC")

    # Overview by domain
    by_domain = Counter(p.get("domain", "unknown") for p in new_pages)
    total_new = len(new_pages)
    total_competitors = len(competitors)

    # Content strategy analysis
    topic_counter = Counter(p.get("topic", "General") for p in new_pages)
    type_counter = Counter(p.get("page_type", "other") for p in new_pages)
    avg_word_count = (
        sum(p.get("word_count", 0) for p in new_pages) / total_new if total_new else 0
    )

    # Gap alert
    gaps = []
    if client_domain:
        client_keywords = fetch_client_keywords(client_domain)
        gaps = detect_gap_alert(new_pages, client_keywords)

    # Recommendations
    recommendations = _build_recommendations(topic_counter, type_counter, gaps, new_pages)

    # Timeline SVG
    timeline_svg = _build_timeline_svg(new_pages, days_back)

    # Overview cards
    cards_html = ""
    card_data = [
        ("New Pages Found", str(total_new), f"across {total_competitors} competitors"),
        ("Competitors Scanned", str(total_competitors), ", ".join(competitors)),
        ("Top Topic", topic_counter.most_common(1)[0][0] if topic_counter else "—",
         f"{topic_counter.most_common(1)[0][1] if topic_counter else 0} pages"),
        ("Avg Word Count", f"{avg_word_count:.0f}", "across new pages"),
    ]
    for label, value, sub in card_data:
        cards_html += f"""
        <div class="card">
          <div class="card-label">{label}</div>
          <div class="card-value">{value}</div>
          <div class="card-sub">{_esc(sub)}</div>
        </div>"""

    # By-domain breakdown
    by_domain_rows = ""
    for domain, count in by_domain.most_common():
        by_domain_rows += (
            f'<tr><td class="domain">{_esc(domain)}</td>'
            f'<td class="num">{count}</td></tr>'
        )

    # Topic analysis rows
    topic_rows = ""
    for topic, count in topic_counter.most_common():
        pct = (count / total_new * 100) if total_new else 0
        bar_w = min(int(pct), 100)
        topic_rows += (
            f'<tr><td>{_esc(topic)}</td><td class="num">{count}</td>'
            f'<td><div class="bar-wrap"><div class="bar" style="width:{bar_w}%"></div></div></td>'
            f'<td class="num">{pct:.0f}%</td></tr>'
        )

    # Page type rows
    type_rows = ""
    for ptype, count in type_counter.most_common():
        pct = (count / total_new * 100) if total_new else 0
        type_rows += (
            f'<tr><td><span class="badge badge-{_esc(ptype)}">{_esc(ptype)}</span></td>'
            f'<td class="num">{count}</td><td class="num">{pct:.0f}%</td></tr>'
        )

    # New pages table (expandable)
    table_rows = ""
    for i, p in enumerate(sorted(new_pages,
                                  key=lambda x: x.get("freshness_date") or now,
                                  reverse=True)):
        domain = p.get("domain", "")
        url = p.get("url", "")
        title = p.get("title") or p.get("h1") or url
        pub_date = _fmt_date(p.get("freshness_date") or p.get("published_date"))
        topic = p.get("topic", "—")
        wc = p.get("word_count", 0)
        ptype = p.get("page_type", "—")
        internal_links = p.get("internal_links", 0)
        schema = ", ".join(p.get("schema_types", [])) or "—"
        meta_desc = p.get("meta_description") or "—"
        h1 = p.get("h1") or "—"
        row_id = f"row-{i}"
        table_rows += f"""
        <tr class="parent-row" onclick="toggle('{row_id}')">
          <td class="domain">{_esc(domain)}</td>
          <td class="url"><a href="{_esc(url)}" target="_blank" rel="nofollow noopener">{_esc(url)}</a></td>
          <td>{_esc(title[:80])}</td>
          <td class="num">{pub_date}</td>
          <td>{_esc(topic)}</td>
          <td class="num">{wc:,}</td>
          <td><span class="badge badge-{_esc(ptype)}">{_esc(ptype)}</span></td>
          <td class="toggle-cell">▾</td>
        </tr>
        <tr class="child-row" id="{row_id}">
          <td colspan="8">
            <div class="detail-grid">
              <div><strong>H1:</strong> {_esc(h1)}</div>
              <div><strong>Meta Description:</strong> {_esc(meta_desc[:200])}</div>
              <div><strong>Internal Links:</strong> {internal_links}</div>
              <div><strong>Schema Types:</strong> {_esc(schema)}</div>
            </div>
          </td>
        </tr>"""

    # Gap alert rows
    gap_rows = ""
    if gaps:
        for g in gaps:
            gap_rows += (
                f'<tr><td class="url"><a href="{_esc(g["url"])}" target="_blank" rel="nofollow noopener">{_esc(g["url"])}</a></td>'
                f'<td>{_esc(g["title"][:80])}</td>'
                f'<td>{_esc(g["topic"])}</td>'
                f'<td><span class="badge badge-{_esc(g["page_type"])}">{_esc(g["page_type"])}</span></td>'
                f'<td>{_esc(g["gap_reason"])}</td></tr>'
            )
    else:
        gap_rows = '<tr><td colspan="5" class="muted">No gaps detected — client covers the same topics as new competitor pages.</td></tr>'

    # Recommendation list
    rec_html = ""
    if recommendations:
        for rec in recommendations:
            rec_html += f'<li>{_esc(rec)}</li>'
    else:
        rec_html = '<li class="muted">No specific recommendations — continue monitoring.</li>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Competitor Page Monitor Report — {_esc(client_domain or 'Client')}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #f8f9fb; color: #1a1d28; line-height: 1.6; padding: 20px 40px;
  }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h1 {{ font-size: 28px; margin-bottom: 4px; font-weight: 700; color: #0f172a; }}
  h2 {{ font-size: 22px; margin-bottom: 16px; padding-bottom: 10px;
       border-bottom: 1px solid #e2e8f0; color: #0f172a; }}
  h3 {{ font-size: 16px; color: #334155; margin: 20px 0 10px; }}
  .subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 30px; }}
  .generated {{ color: #94a3b8; font-size: 12px; }}
  .muted {{ color: #94a3b8; font-size: 13px; }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
           gap: 16px; margin-bottom: 36px; }}
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
          padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  .card-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
                color: #64748b; margin-bottom: 8px; }}
  .card-value {{ font-size: 32px; font-weight: 700; color: #0f172a; }}
  .card-sub {{ font-size: 13px; color: #94a3b8; margin-top: 4px; }}

  .section {{ margin-bottom: 36px; }}
  .table-wrap {{ overflow-x: auto; margin-bottom: 36px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff;
          border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  th {{ text-align: left; padding: 10px 12px; background: #f1f5f9; color: #475569;
       font-weight: 600; text-transform: uppercase; font-size: 11px;
       letter-spacing: 0.05em; border-bottom: 2px solid #e2e8f0; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #f1f5f9; color: #334155; }}
  tr:hover td {{ background: #f8fafc; }}
  td.domain {{ font-weight: 600; color: #0f172a; }}
  td.url {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}

  .bar-wrap {{ background: #e2e8f0; border-radius: 4px; height: 18px; min-width: 60px; }}
  .bar {{ background: #2563eb; border-radius: 4px; height: 18px; }}

  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
           font-size: 11px; font-weight: 600; text-transform: lowercase; }}
  .badge-blog {{ background: #dbeafe; color: #1e40af; }}
  .badge-product {{ background: #dcfce7; color: #166534; }}
  .badge-landing {{ background: #fef3c7; color: #92400e; }}
  .badge-other {{ background: #f1f5f9; color: #64748b; }}
  .badge-unknown {{ background: #f1f5f9; color: #64748b; }}

  .parent-row {{ cursor: pointer; }}
  .child-row {{ display: none; background: #f8fafc; }}
  .child-row td {{ padding: 12px 16px; }}
  .toggle-cell {{ text-align: center; color: #94a3b8; font-size: 14px; }}
  .detail-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; font-size: 12px; color: #475569; }}

  .gap-alert {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
               padding: 16px 20px; margin-bottom: 20px; }}
  .gap-alert h3 {{ color: #991b1b; margin-top: 0; }}

  .rec-list {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
              padding: 20px 24px; }}
  .rec-list li {{ margin-bottom: 10px; color: #334155; }}

  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 768px) {{ .two-col, .detail-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
  <h1>Competitor Page Monitor Report</h1>
  <p class="subtitle">Client: <strong>{_esc(client_domain or 'N/A')}</strong> &middot;
     Scanning: {_esc(', '.join(competitors))} &middot; Window: last {days_back} days</p>
  <p class="generated">Generated {run_date} &middot; Zenfusion competitor_page_monitor v{SCRIPT_VERSION}</p>

  <div class="cards">
    {cards_html}
  </div>

  <div class="section">
    <h2>New Pages by Domain</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Domain</th><th>New Pages</th></tr></thead>
        <tbody>{by_domain_rows or '<tr><td colspan="2" class="muted">No new pages found.</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Publishing Timeline</h2>
    {timeline_svg}
  </div>

  <div class="section">
    <h2>New Pages Detail</h2>
    <p class="muted">Click any row to expand details (H1, meta description, internal links, schema).</p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Domain</th><th>URL</th><th>Title</th><th>Publish Date</th>
          <th>Topic</th><th>Words</th><th>Type</th><th></th>
        </tr></thead>
        <tbody>{table_rows or '<tr><td colspan="8" class="muted">No new pages found in the selected window.</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Content Strategy Analysis</h2>
    <div class="two-col">
      <div>
        <h3>Topics Competitors Are Targeting</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Topic</th><th>Pages</th><th>Share</th><th>%</th></tr></thead>
            <tbody>{topic_rows or '<tr><td colspan="4" class="muted">No data.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h3>Page Types Being Created</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Type</th><th>Pages</th><th>%</th></tr></thead>
            <tbody>{type_rows or '<tr><td colspan="3" class="muted">No data.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Gap Alert</h2>
    <div class="gap-alert">
      <h3>Competitor pages targeting keywords the client doesn't cover</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>URL</th><th>Title</th><th>Topic</th><th>Type</th><th>Gap Reason</th></tr></thead>
          <tbody>{gap_rows}</tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Recommendations</h2>
    <ul class="rec-list">
      {rec_html}
    </ul>
  </div>

  <p class="generated" style="margin-top:40px">SEO data collected via DataForSEO. This report is internal — noindex/nofollow.</p>

<script>
function toggle(id) {{
  var el = document.getElementById(id);
  if (el) {{ el.style.display = (el.style.display === 'table-row') ? 'none' : 'table-row'; }}
}}
</script>
</body>
</html>"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] Report written to {report_file}")
    return report_file


def _build_recommendations(topic_counter, type_counter, gaps, new_pages):
    """Generate actionable recommendation strings."""
    recs = []
    if not new_pages:
        recs.append("No new competitor pages detected in the monitoring window. "
                     "Continue monitoring — competitors may publish on a longer cycle.")
        return recs

    # Top topics
    top_topics = topic_counter.most_common(3)
    for topic, count in top_topics:
        recs.append(
            f"Create content targeting '{topic}' — competitors published {count} "
            f"new page(s) on this topic. This is an active content area."
        )

    # Page type mix
    top_type = type_counter.most_common(1)[0] if type_counter else ("other", 0)
    if top_type[0] == "blog":
        recs.append(
            f"Competitors are investing heavily in blog content ({top_type[1]} pages). "
            "Match their publishing cadence with in-depth, well-researched articles."
        )
    elif top_type[0] == "product":
        recs.append(
            f"Competitors added {top_type[1]} new product pages. "
            "Review your product catalog for gaps and add detailed product pages "
            "with schema.org Product markup."
        )
    elif top_type[0] == "landing":
        recs.append(
            f"Competitors created {top_type[1]} new landing/guide pages. "
            "Build pillar pages targeting the same topics with comprehensive coverage."
        )

    # Gaps
    if gaps:
        gap_topics = set(g["topic"] for g in gaps)
        recs.append(
            f"Gap alert: {len(gaps)} new competitor pages target topics the client "
            f"has no content for ({', '.join(sorted(gap_topics))}). "
            "Prioritize creating content for these gaps."
        )

    # Word count guidance
    avg_wc = sum(p.get("word_count", 0) for p in new_pages) / len(new_pages) if new_pages else 0
    if avg_wc > 0:
        if avg_wc < 600:
            recs.append(
                f"Competitor pages average {avg_wc:.0f} words — relatively short. "
                "Outrank them with longer, more comprehensive content (1,500+ words)."
            )
        elif avg_wc > 1200:
            recs.append(
                f"Competitor pages average {avg_wc:.0f} words — in-depth content. "
                "Match or exceed their depth and add unique data, visuals, or original research."
            )
        else:
            recs.append(
                f"Competitor pages average {avg_wc:.0f} words. "
                "Aim for 1,200–2,000 word comprehensive pages to compete."
            )

    return recs


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scan competitor sites for recently published pages."
    )
    parser.add_argument(
        "--competitors", required=True,
        help="Comma-separated competitor domains (e.g. aquasana.com,culligan.com)",
    )
    parser.add_argument(
        "--client", default=None,
        help="Client domain for gap analysis (e.g. softprowatersystems.com)",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Number of days back to consider pages 'new' (default: 30)",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output directory for the HTML report",
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Max pages to fetch per competitor (default: 100)",
    )
    parser.add_argument(
        "--max-analyze", type=int, default=50,
        help="Max new pages to deep-analyze via instant_pages (default: 50)",
    )
    args = parser.parse_args()

    competitors = [c.strip() for c in args.competitors.split(",") if c.strip()]
    if not competitors:
        print("ERROR: No competitors provided.", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("COMPETITOR PAGE MONITOR")
    print("=" * 60)
    print(f"  Client:      {args.client or 'N/A'}")
    print(f"  Competitors:  {', '.join(competitors)}")
    print(f"  Days back:   {args.days}")
    print(f"  Limit/comp:  {args.limit}")
    print(f"  Output:      {args.output}")
    print("=" * 60)

    all_new_pages = []

    for domain in competitors:
        print(f"\n>>> Scanning {domain} ...")
        pages = fetch_competitor_pages(domain, limit=args.limit)
        new_pages = identify_new_pages(pages, days_back=args.days)
        print(f"  {len(new_pages)} new page(s) in the last {args.days} days.")

        # Analyze each new page
        analyzed = []
        for i, p in enumerate(new_pages):
            if i >= args.max_analyze:
                print(f"  [INFO] Reached max-analyze limit ({args.max_analyze}), "
                      f"skipping {len(new_pages) - i} remaining.")
                # Still include unanalyzed pages with basic info
                analyzed.append({
                    **p,
                    "domain": domain,
                    "title": p.get("title", ""),
                    "meta_description": p.get("description", ""),
                    "h1": "",
                    "word_count": 0,
                    "topic": detect_topic(p.get("title", ""), "", p.get("description", "")),
                    "internal_links": 0,
                    "schema_types": [],
                    "page_type": "unknown",
                })
                continue
            analysis = analyze_page_content(p["url"])
            analysis["domain"] = domain
            analysis["published_date"] = p.get("published_date")
            analysis["modified_date"] = p.get("modified_date")
            analysis["freshness_date"] = p.get("freshness_date")
            # If the direct HTTP fetch didn't find a date, use the API's
            # last_modified as a supplementary date source
            if not analysis.get("freshness_date") and analysis.get("api_modified_date"):
                analysis["freshness_date"] = analysis["api_modified_date"]
            analyzed.append(analysis)

        all_new_pages.extend(analyzed)

    # Generate report
    print(f"\n>>> Generating report ...")
    report_path = generate_monitor_report(
        competitors=competitors,
        new_pages=all_new_pages,
        output_path=args.output,
        client_domain=args.client,
        days_back=args.days,
    )

    print("\n" + "=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)
    print(f"  Total new pages:  {len(all_new_pages)}")
    print(f"  Report:           {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
