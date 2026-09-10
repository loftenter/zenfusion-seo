#!/usr/bin/env python3
"""
Shopify Crawl Budget Analyzer — Identifies Shopify-specific crawl budget waste
and generates robots.txt + meta robots recommendations.

Discovers URLs from the Shopify sitemap (and by crawling the homepage + key
collection pages via ScrapeOwl to surface variant/faceted/pagination/search
URLs that sitemaps omit), classifies each URL by Shopify type, flags crawl
waste / indexable waste / low value / healthy, and generates an HTML report
with summary cards, an SVG distribution chart, an expandable waste breakdown
table, copyable robots.txt rules, meta robots recommendations, and a crawl
budget estimation.

Usage:
  python3 shopify_crawl_budget.py --domain softprowatersystems.com --limit 50 \
      --output /tmp/test_shopify_crawl

Environment:
  SCRAPEOWL_API_KEY (optional, used for JS-rendered crawling)  — in ~/.hermes/.env
"""

import os
import sys
import re
import json
import ssl
import time
import argparse
import html as html_mod
import urllib.request
import urllib.parse
from datetime import datetime
from urllib.parse import urlparse, urldefrag, parse_qs
from xml.etree import ElementTree as ET

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

USER_AGENT = "Mozilla/5.0 (compatible; ZenfusionShopifyCrawlBudget/1.0)"
DELAY = 0.25  # seconds between fetches
DEFAULT_LIMIT = 200
HOMEPAGE_CRAWL_BUDGET = 6  # how many pages to ScrapeOwl-crawl for link discovery

# Shopify URL type taxonomy
URL_TYPES = [
    "product",
    "collection",
    "collection-product",
    "variant",
    "pagination",
    "faceted",
    "search",
    "cart",
    "checkout",
    "account",
    "blog",
    "page",
    "tagged",
    "other",
]

# When we crawl pages to discover waste URLs, how many links per page to keep.
MAX_LINKS_PER_PAGE = 200


# ── Credentials / env ───────────────────────────────────────────────────────

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
                    env[k] = v
    return env


def get_scrapeowl_key():
    env = load_env()
    return env.get("SCRAPEOWL_API_KEY") or os.environ.get("SCRAPEOWL_API_KEY") or ""


# ── URL helpers ─────────────────────────────────────────────────────────────

def get_domain_root(domain):
    """Normalize a domain input to https://domain."""
    domain = domain.strip()
    if domain.startswith("http://") or domain.startswith("https://"):
        parsed = urlparse(domain)
        return f"{parsed.scheme}://{parsed.netloc}", parsed.netloc
    return f"https://{domain}", domain


def normalize_url(url, root_netloc):
    """Strip fragment, normalize scheme/host, return None if external."""
    if not url:
        return None
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc_bare = netloc[4:]
    else:
        netloc_bare = netloc
    root_bare = root_netloc.lower()
    if root_bare.startswith("www."):
        root_bare = root_bare[4:]
    if netloc_bare != root_bare:
        return None
    path = parsed.path or "/"
    query = parsed.query
    out = f"{scheme}://{netloc}{path}"
    if query:
        out += f"?{query}"
    return out


# ── URL type classification ─────────────────────────────────────────────────

def classify_url_type(url):
    """Classify a Shopify URL into one of URL_TYPES.

    Pattern priority matters: variant/faceted/pagination are detected by query
    string, so check those before the path-based product/collection checks.
    """
    if not url:
        return "other"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)
    q_keys = set(k.lower() for k in qs.keys())

    # Query-string-driven types (check first — they trump path type)
    if "variant" in q_keys:
        return "variant"
    faceted_keys = {"pr_prod_strat", "pr_sid", "pr_redirc", "view", "sort_by",
                    "filter", "filter_value", "constraint", "facets"}
    if q_keys & faceted_keys:
        return "faceted"
    if "page" in q_keys and re.match(r"^/collections/", path, re.I):
        return "pagination"
    if "q" in q_keys and re.match(r"^/search", path, re.I):
        return "search"

    # Path-based types
    low = path.lower()
    if low.startswith("/cart"):
        return "cart"
    if low.startswith("/checkout") or low.startswith("/checkouts"):
        return "checkout"
    if (low.startswith("/account") or low.startswith("/pages/login")
            or low.startswith("/pages/register") or low.startswith("/pages/account")):
        return "account"
    if low.startswith("/search"):
        return "search"
    if re.match(r"^/blogs/[^/]+/tagged/", low):
        return "tagged"
    if re.match(r"^/collections/[^/]+/tagged/", low):
        return "tagged"
    if re.match(r"^/collections/all($|/)", low):
        # /collections/all and /collections/all?page=N handled above for pagination
        return "collection"  # will be re-tagged as collection-all in waste step
    if re.match(r"^/collections/[^/]+/products/", low):
        return "collection-product"
    if re.match(r"^/products/", low):
        return "product"
    if re.match(r"^/collections/", low):
        return "collection"
    if re.match(r"^/blogs/[^/]+/[^/]+$", low) and not low.endswith("/tagged"):
        return "blog"
    if re.match(r"^/blogs/", low):
        return "blog"
    if re.match(r"^/pages/", low):
        return "page"
    if low == "/" or low == "":
        return "page"
    return "other"


def is_collection_all(url, url_type):
    if url_type != "collection":
        return False
    path = urlparse(url).path.lower().rstrip("/")
    return path == "/collections/all" or path.startswith("/collections/all/")


# ── Sitemap discovery ──────────────────────────────────────────────────────

def fetch_url(url, timeout=20):
    """Fetch a URL and return (status_code, text)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except Exception:
        return 0, ""


def parse_sitemap(xml_text):
    """Parse a sitemap XML. Returns (urls, sub_sitemaps)."""
    urls = []
    subs = []
    if not xml_text:
        return urls, subs
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return urls, subs
    # Strip namespace for matching
    raw_tag = root.tag
    ns = ""
    if raw_tag.startswith("{"):
        ns = raw_tag.split("}")[0] + "}"
    sm_tag = ns + "sitemap"
    url_tag = ns + "url"
    loc_tag = ns + "loc"
    for child in root:
        tag = child.tag
        if tag == sm_tag:
            loc = child.find(loc_tag)
            if loc is not None and loc.text:
                subs.append(loc.text.strip())
        elif tag == url_tag:
            loc = child.find(loc_tag)
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    return urls, subs


def discover_sitemap_urls(root_url, netloc, limit):
    """Fetch sitemap.xml, recurse into sub-sitemaps, return list of URLs."""
    found = []
    seen = set()

    def add(u):
        u = normalize_url(u, netloc)
        if u and u not in seen:
            seen.add(u)
            found.append(u)

    # Shopify typically exposes /sitemap.xml which lists sub-sitemaps
    for sm_url in (f"{root_url}/sitemap.xml", f"{root_url}/sitemap_products_1.xml"):
        code, text = fetch_url(sm_url)
        if code and text:
            urls, subs = parse_sitemap(text)
            for u in urls:
                add(u)
            for s in subs:
                code2, text2 = fetch_url(s)
                if code2 and text2:
                    sub_urls, sub_subs = parse_sitemap(text2)
                    for u in sub_urls:
                        add(u)
                    # one level deeper (rare but possible)
                    for s2 in sub_subs:
                        code3, text3 = fetch_url(s2)
                        if code3 and text3:
                            for u in parse_sitemap(text3)[0]:
                                add(u)
            if found:
                break

    # Deduped, limited
    return found[:limit]


# ── Page crawling via ScrapeOwl (link discovery) ────────────────────────────

def scrapeowl_fetch(url, timeout=40):
    """Fetch a page via ScrapeOwl with JS rendering. Returns HTML or None."""
    key = get_scrapeowl_key()
    if not key:
        return None
    try:
        params = urllib.parse.urlencode(
            {"api_key": key, "url": url, "render_js": "true"})
        scrape_url = f"https://api.scrapeowl.com/v1/scrape?{params}"
        req = urllib.request.Request(scrape_url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("status") == 200 and "html" in data:
            return data["html"]
    except Exception:
        return None
    return None


LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_links(html_text, root_netloc):
    """Extract same-domain absolute links from HTML."""
    out = []
    seen = set()
    for m in LINK_RE.finditer(html_text or ""):
        raw = m.group(1).strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if raw.startswith("//"):
            raw = "https:" + raw
        elif raw.startswith("/"):
            raw = f"https://{root_netloc}{raw}"
        elif not raw.startswith("http"):
            continue
        norm = normalize_url(raw, root_netloc)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def crawl_pages_for_links(seed_urls, root_netloc, budget):
    """Crawl up to `budget` seed URLs and return all discovered links."""
    discovered = set()
    for u in seed_urls[:budget]:
        html_text = scrapeowl_fetch(u)
        if not html_text:
            code, html_text = fetch_url(u)
            if not code:
                continue
        for link in extract_links(html_text, root_netloc):
            discovered.add(link)
        time.sleep(DELAY)
    return discovered


# ── Public API: crawl_shopify_urls ─────────────────────────────────────────

def crawl_shopify_urls(domain, limit=200):
    """Crawl a Shopify domain and return a list of {url, type} dicts.

    Strategy:
      1. Discover clean URLs from the Shopify sitemap (products, collections,
         pages, blogs).
      2. Crawl the homepage + a few collection pages (via ScrapeOwl when
         available, falling back to urllib) to discover variant, faceted,
         pagination, search, cart, checkout, account, and tagged URLs that
         sitemaps omit.
      3. Merge, classify each URL by Shopify type, dedupe, cap at `limit`.
    """
    root_url, netloc = get_domain_root(domain)

    # 1. Sitemap URLs
    sitemap_urls = discover_sitemap_urls(root_url, netloc, limit)

    # 2. Crawl homepage + a few collections for link discovery
    seed = [f"{root_url}/"]
    # Add a couple of collection pages and the /collections/all page as seeds
    collections_seen = [u for u in sitemap_urls
                        if classify_url_type(u) == "collection"][:3]
    seed.extend(collections_seen)
    if f"{root_url}/collections/all" not in seed:
        seed.append(f"{root_url}/collections/all")

    discovered = crawl_pages_for_links(seed, netloc, HOMEPAGE_CRAWL_BUDGET)

    # 3. Merge
    all_urls = list(dict.fromkeys(sitemap_urls + sorted(discovered)))

    # Build {url: type} list
    seen = set()
    results = []
    for u in all_urls:
        if u in seen:
            continue
        seen.add(u)
        utype = classify_url_type(u)
        results.append({"url": u, "type": utype})
        if len(results) >= limit:
            break
    return results


# ── Public API: identify_crawl_waste ───────────────────────────────────────

def identify_crawl_waste(urls):
    """Classify each URL as Crawl Waste / Indexable Waste / Low Value / Healthy.

    `urls` is the list of {url, type} dicts from crawl_shopify_urls.
    Returns a list of dicts with keys:
      url, type, classification, reason, recommendation
    """
    out = []
    for entry in urls:
        url = entry["url"]
        utype = entry["type"]
        classification = "Healthy"
        reason = ""
        recommendation = "leave"

        # Indexable Waste — non-indexable Shopify pages being indexed
        if utype in ("cart", "checkout", "account"):
            classification = "Indexable Waste"
            reason = (f"{utype.capitalize()} page should be noindex")
            recommendation = "block"
        # Crawl Waste — duplicate / faceted / search / pagination / tagged
        elif is_collection_all(url, utype):
            classification = "Crawl Waste"
            reason = "/collections/all duplicates individual collections"
            recommendation = "block"
        elif utype == "variant":
            classification = "Crawl Waste"
            reason = "Variant URL duplicates canonical product"
            recommendation = "canonicalize"
        elif utype == "faceted":
            classification = "Crawl Waste"
            reason = "Faceted/filter URL creates duplicate content"
            recommendation = "block"
        elif utype == "search":
            classification = "Crawl Waste"
            reason = "Internal search result page (thin, noindex)"
            recommendation = "block"
        elif utype == "tagged":
            classification = "Crawl Waste"
            reason = "Tag page duplicates collection content"
            recommendation = "block"
        elif utype == "pagination":
            classification = "Crawl Waste"
            reason = "Pagination URL on (possibly thin) collection"
            recommendation = "canonicalize"
        elif utype == "collection-product":
            classification = "Crawl Waste"
            reason = "Product reached via collection path (duplicate of /products/*)"
            recommendation = "canonicalize"
        # Low Value heuristics (without page fetch — pattern-based)
        elif utype == "product":
            # Heuristic: very short product slugs may indicate thin pages, but
            # we can't know without fetching. Mark Healthy unless we fetch.
            classification = "Healthy"
            reason = "Product page (canonical)"
            recommendation = "leave"
        elif utype == "collection":
            classification = "Healthy"
            reason = "Collection page with content"
            recommendation = "leave"
        elif utype == "blog":
            classification = "Healthy"
            reason = "Blog post"
            recommendation = "leave"
        elif utype == "page":
            classification = "Healthy"
            reason = "Static page"
            recommendation = "leave"
        else:
            classification = "Healthy"
            reason = "Uncategorized"
            recommendation = "leave"

        out.append({
            "url": url,
            "type": utype,
            "classification": classification,
            "reason": reason,
            "recommendation": recommendation,
        })
    return out


# ── Public API: generate_robots_recommendations ────────────────────────────

ROBOTS_RULES = [
    ("Disallow: /collections/all*", "/collections/all and its pagination"),
    ("Disallow: /*?variant=", "Product variant URLs"),
    ("Disallow: /*?pr_prod_strat=", "Shopify product-strategy faceted URLs"),
    ("Disallow: /*?view=", "Alternate view/template URLs"),
    ("Disallow: /*?sort_by=", "Sort/filter URLs"),
    ("Disallow: /*?filter*", "Faceted filter URLs"),
    ("Disallow: /search", "Internal search result pages"),
    ("Disallow: /cart", "Cart page"),
    ("Disallow: /checkout", "Checkout page"),
    ("Disallow: /checkouts", "Checkout page (alt path)"),
    ("Disallow: /account", "Account pages"),
    ("Disallow: /blogs/*/tagged/*", "Blog tag pages"),
    ("Disallow: /collections/*/tagged/*", "Collection tag pages"),
]


def generate_robots_recommendations(waste_urls):
    """Generate Shopify-specific robots.txt rules + meta robots recommendations.

    Returns a dict:
      robots_rules:    list of {rule, target, present} (present = True if a
                       waste URL of that type was actually seen)
      robots_block:    copyable robots.txt code block (string)
      meta_robots:     list of {url_type, recommendation, urls}
    """
    types_seen = {w["type"] for w in waste_urls}
    types_seen.add("other")  # always include core rules
    saw_collection_all = any(is_collection_all(w["url"], w["type"])
                            and w["classification"] == "Crawl Waste"
                            for w in waste_urls)

    rules = []
    for rule, target in ROBOTS_RULES:
        # Determine if this rule is "present" (i.e. we saw matching waste)
        present = False
        rlow = rule.lower()
        if "/collections/all" in rlow:
            present = saw_collection_all
        elif "variant" in rlow:
            present = "variant" in types_seen
        elif "pr_prod_strat" in rlow:
            present = "faceted" in types_seen
        elif "view" in rlow and "filter" not in rlow:
            present = "faceted" in types_seen
        elif "sort_by" in rlow:
            present = "faceted" in types_seen or "pagination" in types_seen
        elif "filter" in rlow:
            present = "faceted" in types_seen
        elif "/search" in rlow:
            present = "search" in types_seen
        elif "/cart" in rlow:
            present = "cart" in types_seen
        elif "/checkout" in rlow and "checkouts" not in rlow:
            present = "checkout" in types_seen
        elif "/checkouts" in rlow:
            present = "checkout" in types_seen
        elif "/account" in rlow:
            present = "account" in types_seen
        elif "tagged" in rlow:
            present = "tagged" in types_seen
        rules.append({"rule": rule, "target": target, "present": present})

    # Copyable robots.txt block — only include rules that are relevant
    block_lines = [
        "# Shopify crawl-budget robots.txt additions",
        "# (Shopify's robots.txt is partially managed; add these via theme",
        "#  or a custom robots.txt app where possible.)",
        "User-agent: *",
    ]
    for r in rules:
        block_lines.append(r["rule"])
    robots_block = "\n".join(block_lines)

    # Meta robots recommendations grouped by type
    meta_groups = [
        {"url_type": "cart", "recommendation": 'noindex,nofollow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "cart"]},
        {"url_type": "checkout", "recommendation": 'noindex,nofollow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "checkout"]},
        {"url_type": "account", "recommendation": 'noindex,nofollow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "account"]},
        {"url_type": "search", "recommendation": 'noindex,nofollow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "search"]},
        {"url_type": "tagged", "recommendation": 'noindex,follow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "tagged"]},
        {"url_type": "pagination", "recommendation": 'noindex,follow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "pagination"]},
        {"url_type": "faceted", "recommendation": 'noindex,follow',
         "urls": [w["url"] for w in waste_urls if w["type"] == "faceted"]},
        {"url_type": "variant", "recommendation": 'noindex,follow (canonical to product)',
         "urls": [w["url"] for w in waste_urls if w["type"] == "variant"]},
        {"url_type": "collection-product", "recommendation":
            'noindex,follow (canonical to /products/*)',
         "urls": [w["url"] for w in waste_urls if w["type"] == "collection-product"]},
    ]
    meta_robots = [g for g in meta_groups if g["urls"]]

    return {
        "robots_rules": rules,
        "robots_block": robots_block,
        "meta_robots": meta_robots,
    }


# ── Stats ──────────────────────────────────────────────────────────────────

def compute_stats(urls, waste):
    total = len(urls)
    type_counts = {t: 0 for t in URL_TYPES}
    for u in urls:
        type_counts[u["type"]] = type_counts.get(u["type"], 0) + 1

    crawl_waste = [w for w in waste if w["classification"] == "Crawl Waste"]
    indexable_waste = [w for w in waste if w["classification"] == "Indexable Waste"]
    low_value = [w for w in waste if w["classification"] == "Low Value"]
    healthy = [w for w in waste if w["classification"] == "Healthy"]

    waste_count = len(crawl_waste) + len(indexable_waste)
    waste_pct = (waste_count / total * 100) if total else 0
    # Crawl budget estimation: assume Googlebot crawls ~N URLs per cycle; waste
    # is waste_count of those. We estimate wasted crawl budget as a percentage
    # of a nominal 200-URL crawl cycle, capped at the actual waste count.
    nominal_cycle = 200
    estimated_wasted = min(waste_count, nominal_cycle)
    estimated_wasted_pct = (estimated_wasted / nominal_cycle * 100) if nominal_cycle else 0

    return {
        "total": total,
        "type_counts": type_counts,
        "crawl_waste": len(crawl_waste),
        "indexable_waste": len(indexable_waste),
        "low_value": len(low_value),
        "healthy": len(healthy),
        "waste_count": waste_count,
        "waste_pct": waste_pct,
        "estimated_wasted": estimated_wasted,
        "estimated_wasted_pct": estimated_wasted_pct,
        "nominal_cycle": nominal_cycle,
    }


# ── HTML report helpers ────────────────────────────────────────────────────

def esc(text):
    return html_mod.escape(str(text)) if text else ""


def generate_svg_distribution(stats):
    """SVG bar chart: product vs collection vs duplicate vs waste (and others)."""
    tc = stats["type_counts"]
    product = tc.get("product", 0)
    collection = tc.get("collection", 0)
    duplicate = (tc.get("collection-product", 0) + tc.get("variant", 0))
    waste = (tc.get("faceted", 0) + tc.get("search", 0) +
            tc.get("pagination", 0) + tc.get("tagged", 0) +
            tc.get("cart", 0) + tc.get("checkout", 0) + tc.get("account", 0))
    other = stats["total"] - (product + collection + duplicate + waste)
    if other < 0:
        other = 0

    bars = [
        ("Products", product, "#27ae60"),
        ("Collections", collection, "#3498db"),
        ("Duplicates", duplicate, "#f39c12"),
        ("Waste", waste, "#e74c3c"),
        ("Other", other, "#95a5a6"),
    ]
    total = stats["total"] or 1
    max_h = 200
    svg = '<svg viewBox="0 0 520 280" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:520px;">'
    svg += '<rect x="0" y="0" width="520" height="280" fill="#fff" rx="8"/>'
    bar_w = 80
    gap = 20
    x = 30
    for label, count, color in bars:
        h = (count / total) * max_h
        y = 230 - h
        svg += (f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" '
                f'fill="{color}" rx="4"/>')
        ty = y - 8 if h > 20 else 222
        svg += (f'<text x="{x + bar_w/2}" y="{ty}" text-anchor="middle" '
                f'font-size="14" font-weight="bold" fill="#1a1a1a">{count}</text>')
        svg += (f'<text x="{x + bar_w/2}" y="250" text-anchor="middle" '
                f'font-size="12" fill="#1a1a1a">{label}</text>')
        x += bar_w + gap
    svg += '<line x1="10" y1="230" x2="510" y2="230" stroke="#ccc" stroke-width="1"/>'
    svg += '</svg>'
    return svg


# ── Public API: generate_crawl_report ──────────────────────────────────────

def generate_crawl_report(domain, urls, waste, recommendations, output_path):
    """Generate the full HTML report. Returns the path to index.html."""
    stats = compute_stats(urls, waste)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Summary cards
    cards_html = f"""
    <div class="card card-total"><div class="num">{stats['total']}</div>
      <div class="label">Total URLs</div></div>
    <div class="card card-waste"><div class="num">{stats['waste_count']}</div>
      <div class="label">Crawl Waste ({stats['waste_pct']:.0f}%)</div></div>
    <div class="card card-index"><div class="num">{stats['estimated_wasted']}</div>
      <div class="label">Est. Wasted Budget / {stats['nominal_cycle']}-URL cycle</div></div>
    <div class="card card-idxwaste"><div class="num">{stats['indexable_waste']}</div>
      <div class="label">Indexable Waste</div></div>
    <div class="card card-healthy"><div class="num">{stats['healthy']}</div>
      <div class="label">Healthy</div></div>"""

    # SVG
    svg_chart = generate_svg_distribution(stats)

    # URL type distribution table
    type_rows = ""
    for t in URL_TYPES:
        c = stats["type_counts"].get(t, 0)
        if c == 0:
            continue
        pct = (c / stats["total"] * 100) if stats["total"] else 0
        type_rows += f"""<tr><td>{esc(t)}</td>
          <td style="text-align:center;">{c}</td>
          <td style="text-align:center;">{pct:.1f}%</td></tr>"""

    # Crawl waste breakdown rows (expandable via <details>)
    waste_entries = [w for w in waste if w["classification"] in
                     ("Crawl Waste", "Indexable Waste", "Low Value")]
    waste_rows = ""
    if waste_entries:
        for w in waste_entries:
            rec_color = {
                "block": "#e74c3c",
                "canonicalize": "#f39c12",
                "redirect": "#9b59b6",
                "leave": "#27ae60",
            }.get(w["recommendation"], "#888")
            waste_rows += f"""<tr>
              <td><a href="{esc(w['url'])}" style="color:#0066cc;">{esc(w['url'])}</a></td>
              <td style="text-align:center;">{esc(w['type'])}</td>
              <td style="text-align:center;">{esc(w['classification'])}</td>
              <td>{esc(w['reason'])}</td>
              <td style="text-align:center;"><span style="color:{rec_color};font-weight:bold;">{esc(w['recommendation'])}</span></td>
            </tr>"""
    else:
        waste_rows = ('<tr><td colspan="5" style="text-align:center;color:#888;">'
                      'No crawl waste detected</td></tr>')

    # robots.txt rules
    robots_rule_rows = ""
    for r in recommendations["robots_rules"]:
        present_badge = ('<span style="color:#e74c3c;font-weight:bold;">●</span> seen'
                         if r["present"] else
                         '<span style="color:#aaa;">○</span> preventive')
        robots_rule_rows += f"""<tr>
          <td><code>{esc(r['rule'])}</code></td>
          <td>{esc(r['target'])}</td>
          <td style="text-align:center;">{present_badge}</td></tr>"""

    robots_block = recommendations["robots_block"]

    # meta robots recommendations
    meta_rows = ""
    if recommendations["meta_robots"]:
        for g in recommendations["meta_robots"]:
            sample = ", ".join(g["urls"][:3])
            extra = f" (+{len(g['urls'])-3} more)" if len(g["urls"]) > 3 else ""
            meta_rows += f"""<tr>
              <td style="text-align:center;">{esc(g['url_type'])}</td>
              <td><code>{esc(g['recommendation'])}</code></td>
              <td>{len(g['urls'])}</td>
              <td><span style="word-break:break-all;">{esc(sample)}{extra}</span></td></tr>"""
    else:
        meta_rows = ('<tr><td colspan="4" style="text-align:center;color:#888;">'
                     'No meta robots changes needed</td></tr>')

    # Crawl budget estimation block
    budget_html = f"""
    <div class="budget-box">
      <p><strong>Googlebot crawl cycle estimate (nominal {stats['nominal_cycle']} URLs):</strong></p>
      <ul>
        <li>Wasted on crawl-waste URLs: <strong>{stats['estimated_wasted']}</strong>
            ({stats['estimated_wasted_pct']:.0f}% of a {stats['nominal_cycle']}-URL cycle)</li>
        <li>Crawl waste as % of discovered URLs: <strong>{stats['waste_pct']:.0f}%</strong></li>
        <li>Indexable waste (needs noindex): <strong>{stats['indexable_waste']}</strong></li>
        <li>Healthy URLs worth crawling: <strong>{stats['healthy']}</strong></li>
      </ul>
      <p style="margin-top:8px;color:#666;">Recovering the wasted crawl budget lets Googlebot
      re-crawl product and collection pages more frequently, improving indexation freshness
      and discovery of new/updated products.</p>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex,nofollow">
  <title>Shopify Crawl Budget Analysis — {esc(domain)}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #fff; color: #1a1a1a; font-size: 16px; line-height: 1.6; }}
    .container {{ max-width: 1000px; margin: 0 auto; padding: 32px 24px; }}
    h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; color: #1a1a1a; }}
    h2 {{ font-size: 20px; font-weight: 600; margin: 32px 0 12px; color: #1a1a1a;
          border-bottom: 2px solid #eee; padding-bottom: 8px; }}
    .subtitle {{ font-size: 14px; color: #888; margin-bottom: 24px; }}
    .cards {{ display: flex; gap: 12px; margin-bottom: 32px; flex-wrap: wrap; }}
    .card {{ flex: 1; min-width: 150px; padding: 18px; border-radius: 8px; text-align: center; }}
    .card .num {{ font-size: 28px; font-weight: 700; }}
    .card .label {{ font-size: 12px; margin-top: 4px; }}
    .card-total {{ background: #f5f5f5; border: 1px solid #ddd; }}
    .card-total .num {{ color: #1a1a1a; }}
    .card-waste {{ background: #fdf2f2; border: 1px solid #e74c3c; }}
    .card-waste .num {{ color: #e74c3c; }}
    .card-index {{ background: #fef6f6; border: 1px solid #c0392b; }}
    .card-index .num {{ color: #c0392b; }}
    .card-idxwaste {{ background: #fff8f0; border: 1px solid #e67e22; }}
    .card-idxwaste .num {{ color: #e67e22; }}
    .card-healthy {{ background: #f0faf4; border: 1px solid #27ae60; }}
    .card-healthy .num {{ color: #27ae60; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
    th {{ text-align: left; padding: 10px 12px; background: #f5f5f5; font-size: 14px;
          font-weight: 600; border-bottom: 2px solid #ddd; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 14px;
          word-break: break-word; }}
    tr:hover {{ background: #fafafa; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px;
            background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }}
    .codeblock {{ background: #1e1e1e; color: #f8f8f2; padding: 16px; border-radius: 8px;
                  overflow-x: auto; font-family: "SF Mono", Menlo, Consolas, monospace;
                  font-size: 13px; line-height: 1.5; margin-bottom: 16px;
                  white-space: pre-wrap; }}
    details {{ margin-bottom: 16px; }}
    summary {{ cursor: pointer; font-weight: 600; padding: 10px 12px;
                background: #f5f5f5; border-radius: 4px; }}
    details[open] summary {{ border-bottom: 1px solid #ddd; border-radius: 4px 4px 0 0; }}
    .budget-box {{ background: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 8px;
                    padding: 16px 20px; margin-bottom: 24px; }}
    .budget-box ul {{ margin: 8px 0 0 20px; }}
    .meta {{ font-size: 13px; color: #aaa; margin-top: 40px; padding-top: 16px;
             border-top: 1px solid #eee; }}
    @media (max-width: 700px) {{ .cards {{ flex-direction: column; }} }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Shopify Crawl Budget Analysis</h1>
    <div class="subtitle">{esc(domain)} · Generated {now} · v{SCRIPT_VERSION}</div>

    <!-- Summary Cards -->
    <h2>Summary</h2>
    <div class="cards">
      {cards_html}
    </div>

    <!-- SVG Distribution -->
    <h2>URL Type Distribution</h2>
    {svg_chart}

    <!-- URL type counts -->
    <details open>
      <summary>URL type breakdown ({stats['total']} URLs)</summary>
      <table>
        <thead><tr><th>Type</th><th style="text-align:center;">Count</th>
          <th style="text-align:center;">% of Total</th></tr></thead>
        <tbody>{type_rows}</tbody>
      </table>
    </details>

    <!-- Crawl Waste Breakdown -->
    <h2>Crawl Waste Breakdown</h2>
    <details open>
      <summary>{stats['waste_count']} waste / indexable-waste URLs (click to collapse)</summary>
      <table>
        <thead><tr>
          <th>URL</th><th style="text-align:center;">Type</th>
          <th style="text-align:center;">Classification</th>
          <th>Reason Flagged</th>
          <th style="text-align:center;">Recommendation</th>
        </tr></thead>
        <tbody>{waste_rows}</tbody>
      </table>
    </details>

    <!-- robots.txt recommendations -->
    <h2>robots.txt Recommendations</h2>
    <p style="margin-bottom:12px;color:#666;">Shopify's robots.txt is partially
    managed, but these rules can be added via the theme or a custom robots.txt
    app. Rules marked <span style="color:#e74c3c;font-weight:bold;">●</span> were
    matched by discovered waste URLs; <span style="color:#aaa;">○</span> are
    preventive.</p>
    <table>
      <thead><tr><th>Rule</th><th>Target</th><th style="text-align:center;">Status</th></tr></thead>
      <tbody>{robots_rule_rows}</tbody>
    </table>
    <h3 style="margin:16px 0 8px;font-size:16px;">Copyable robots.txt block</h3>
    <div class="codeblock">{esc(robots_block)}</div>

    <!-- Meta robots recommendations -->
    <h2>Meta Robots Recommendations</h2>
    <p style="margin-bottom:12px;color:#666;">Pages that should carry a
    <code>noindex</code> (and where applicable <code>nofollow</code>) meta robots
    tag, applied via the Shopify theme's <code>head</code> liquid.</p>
    <table>
      <thead><tr>
        <th style="text-align:center;">Page Type</th>
        <th>Recommendation</th>
        <th style="text-align:center;">URLs</th>
        <th>Sample URLs</th>
      </tr></thead>
      <tbody>{meta_rows}</tbody>
    </table>

    <!-- Crawl budget estimation -->
    <h2>Crawl Budget Estimation</h2>
    {budget_html}

    <div class="meta">
      Generated by <strong>Shopify Crawl Budget Analyzer</strong> v{SCRIPT_VERSION} ·
      <meta name="robots" content="noindex,nofollow">
    </div>
  </div>
</body>
</html>"""

    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, "index.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    return report_file


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Identify Shopify-specific crawl budget waste and generate robots.txt recommendations")
    parser.add_argument("--domain", required=True,
                        help="Shopify domain (e.g. softprowatersystems.com)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="Max URLs to analyze (default 200)")
    parser.add_argument("--output", default="/tmp/shopify_crawl_report",
                        help="Output directory for report")
    args = parser.parse_args()

    root_url, domain = get_domain_root(args.domain)
    limit = args.limit
    output_path = args.output

    print(f"\n{'='*70}")
    print(f"  ZENFUSION SHOPIFY CRAWL BUDGET ANALYZER v{SCRIPT_VERSION}")
    print(f"{'='*70}")
    print(f"  Domain: {domain}")
    print(f"  Max URLs: {limit}")
    print(f"  Output: {output_path}")
    print(f"{'='*70}\n")

    # 1. Crawl
    print("  [1/4] Discovering URLs (sitemap + page crawl)...")
    urls = crawl_shopify_urls(domain, limit)
    if not urls:
        print("ERROR: No URLs discovered.", file=sys.stderr)
        sys.exit(1)
    type_summary = {}
    for u in urls:
        type_summary[u["type"]] = type_summary.get(u["type"], 0) + 1
    print(f"  Discovered {len(urls)} URLs. Type breakdown:")
    for t, c in sorted(type_summary.items(), key=lambda x: -x[1]):
        print(f"    {t:20s} {c}")

    # 2. Identify crawl waste
    print("\n  [2/4] Identifying crawl waste...")
    waste = identify_crawl_waste(urls)
    class_counts = {}
    for w in waste:
        class_counts[w["classification"]] = class_counts.get(
            w["classification"], 0) + 1
    for k, v in sorted(class_counts.items(), key=lambda x: -x[1]):
        print(f"    {k:20s} {v}")

    # 3. Robots recommendations
    print("\n  [3/4] Generating robots.txt recommendations...")
    recommendations = generate_robots_recommendations(waste)
    print(f"    {len(recommendations['robots_rules'])} robots rules, "
          f"{len(recommendations['meta_robots'])} meta robots groups")

    # 4. Report
    print(f"\n  [4/4] Generating HTML report -> {output_path}")
    report_file = generate_crawl_report(
        domain, urls, waste, recommendations, output_path)

    stats = compute_stats(urls, waste)
    print(f"\n  Report saved to: {report_file}")
    print(f"\n{'='*70}")

    summary = {
        "domain": domain,
        "total_urls": stats["total"],
        "waste_count": stats["waste_count"],
        "indexable_waste": stats["indexable_waste"],
        "healthy": stats["healthy"],
        "estimated_wasted_per_cycle": stats["estimated_wasted"],
        "report_path": report_file,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
