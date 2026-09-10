#!/usr/bin/env python3
"""
Shopify Schema Auditor — page-type-specific schema markup audit for Shopify stores.

Crawls a Shopify domain, classifies pages by Shopify page type (product, collection,
blog, homepage, page, other), extracts JSON-LD blocks via ScrapeOwl JS rendering,
validates Shopify-specific schema requirements per page type, and emits an expandable
white-SaaS-theme HTML report with summary cards, coverage breakdown, missing-schema
table, Liquid theme fix instructions, and a prioritized fix list.

Usage:
  python3 shopify_schema_auditor.py --domain softprowatersystems.com --limit 20 --output /tmp/test_shopify_schema

Environment (from ~/.hermes/.env):
  SCRAPEOWL_API_KEY
"""

import sys, os, json, re, ssl, time, html, argparse, urllib.request, urllib.parse
from urllib.parse import urlparse, urljoin
from collections import defaultdict, Counter
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Requires: pip3 install beautifulsoup4")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# Required schema types and properties per Shopify page type
SHOPIFY_PAGE_SCHEMA = {
    "product": {
        "label": "Product Page",
        "required_types": {
            "Product": ["name", "image", "description", "offers", "brand", "aggregateRating"],
            "Offer": ["price", "priceCurrency", "availability"],
            "AggregateRating": ["ratingValue", "reviewCount", "bestRating"],
        },
        "optional_types": ["Review", "BreadcrumbList"],
        "liquid_template": "product-template.liquid (or sections/main-product.liquid)",
    },
    "collection": {
        "label": "Collection Page",
        "required_types": {
            "BreadcrumbList": ["itemListElement"],
        },
        "alt_types": {
            "CollectionPage": ["name"],
            "ItemList": ["itemListElement"],
        },
        "optional_types": ["WebPage"],
        "liquid_template": "collection-template.liquid (or sections/main-collection.liquid)",
    },
    "blog": {
        "label": "Blog/Article Page",
        "required_types": {
            "Article": ["headline", "author", "datePublished", "image"],
            "Person": ["name"],
        },
        "optional_types": ["BreadcrumbList", "FAQPage"],
        "liquid_template": "article-template.liquid (or sections/main-article.liquid)",
    },
    "homepage": {
        "label": "Homepage",
        "required_types": {
            "Organization": ["name", "url", "logo", "sameAs"],
            "WebSite": ["name", "url", "potentialAction"],
        },
        "optional_types": ["BreadcrumbList"],
        "liquid_template": "theme.liquid (layout) and index.liquid / sections/main-banner.liquid",
    },
    "page": {
        "label": "Standard Page",
        "required_types": {
            "BreadcrumbList": ["itemListElement"],
        },
        "optional_types": ["WebPage"],
        "liquid_template": "page.liquid (or sections/main-page.liquid)",
    },
    "other": {
        "label": "Other Page",
        "required_types": {},
        "optional_types": ["BreadcrumbList", "WebPage"],
        "liquid_template": "theme.liquid (layout)",
    },
}

# Liquid code snippets to inject for each missing schema type
LIQUID_FIXES = {
    "Product": '''{% comment %} Add to product-template.liquid / sections/main-product.liquid {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": {{ product.title | json }},
  "image": [{{ product.featured_image | image_url: width: 1200 | prepend: 'https:' | json }}],
  "description": {{ product.description | strip_html | truncate: 200 | json }},
  "brand": { "@type": "Brand", "name": {{ product.vendor | json }} },
  "offers": {
    "@type": "Offer",
    "price": {{ product.price | money_without_currency | json }},
    "priceCurrency": {{ cart.currency.iso_code | json }},
    "availability": "https://schema.org/{% if product.available %}InStock{% else %}OutOfStock{% endif %}",
    "url": {{ shop.url | append: product.url | json }}
  },
  "aggregateRating": {
    "@type": "AggregateRating",
    "ratingValue": "{{ product.metafields.reviews.rating }}",
    "reviewCount": "{{ product.metafields.reviews.count }}",
    "bestRating": "5"
  }
}
</script>''',
    "Offer": '''{% comment %} Inside Product offers block — see Product snippet {% endcomment %}
"offers": {
  "@type": "Offer",
  "price": {{ product.price | money_without_currency | json }},
  "priceCurrency": {{ cart.currency.iso_code | json }},
  "availability": "https://schema.org/{% if product.available %}InStock{% else %}OutOfStock{% endif %}"
}''',
    "AggregateRating": '''{% comment %} Inside Product aggregateRating — requires reviews app {% endcomment %}
"aggregateRating": {
  "@type": "AggregateRating",
  "ratingValue": "{{ product.metafields.reviews.rating }}",
  "reviewCount": "{{ product.metafields.reviews.count }}",
  "bestRating": "5"
}''',
    "BreadcrumbList": '''{% comment %} Add to collection-template.liquid / page.liquid / theme.liquid {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    { "@type": "ListItem", "position": 1, "name": "Home", "item": {{ shop.url | json }} },
    { "@type": "ListItem", "position": 2, "name": {{ collection.title | json }}, "item": {{ shop.url | append: collection.url | json }} }
  ]
}
</script>''',
    "CollectionPage": '''{% comment %} Add to collection-template.liquid {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "CollectionPage",
  "name": {{ collection.title | json }},
  "url": {{ shop.url | append: collection.url | json }},
  "description": {{ collection.description | strip_html | truncate: 200 | json }}
}
</script>''',
    "ItemList": '''{% comment %} Add to collection-template.liquid — lists products {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "itemListElement": [
    {% for product in collection.products limit: 20 %}
    { "@type": "ListItem", "position": {{ forloop.index }}, "url": {{ shop.url | append: product.url | json }} }{% unless forloop.last %},{% endunless %}
    {% endfor %}
  ]
}
</script>''',
    "Article": '''{% comment %} Add to article-template.liquid / sections/main-article.liquid {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": {{ article.title | json }},
  "author": { "@type": "Person", "name": {{ article.author | json }} },
  "datePublished": {{ article.published_at | date: '%Y-%m-%dT%H:%M:%SZ' | json }},
  "image": [{{ article.image | image_url: width: 1200 | prepend: 'https:' | json }}],
  "publisher": { "@type": "Organization", "name": {{ shop.name | json }} }
}
</script>''',
    "Person": '''{% comment %} Add near Article author block {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Person",
  "name": {{ article.author | json }},
  "jobTitle": "Contributor"
}
</script>''',
    "Organization": '''{% comment %} Add to theme.liquid layout (head) for homepage {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": {{ shop.name | json }},
  "url": {{ shop.url | json }},
  "logo": {{ shop.brand.logo | image_url: width: 600 | prepend: 'https:' | json }},
  "sameAs": [{{ settings.social_twitter_link | json }}, {{ settings.social_facebook_link | json }}, {{ settings.social_instagram_link | json }}]
}
</script>''',
    "WebSite": '''{% comment %} Add to theme.liquid layout (head) for homepage {% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "WebSite",
  "name": {{ shop.name | json }},
  "url": {{ shop.url | json }},
  "potentialAction": {
    "@type": "SearchAction",
    "target": {{ shop.url | append: '/search?q={search_term_string}' | json }},
    "query-input": "required name=search_term_string"
  }
}
</script>''',
}

# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────

def load_env():
    env = {}
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

# ─────────────────────────────────────────────────────────────────────────────
# Fetching (ScrapeOwl for JS-rendered, urllib fallback)
# ─────────────────────────────────────────────────────────────────────────────

def scrapeowl_fetch(url):
    """Fetch rendered HTML via ScrapeOwl (renders JS). Returns html string or ''."""
    api_key = ENV.get("SCRAPEOWL_API_KEY", "")
    if not api_key:
        print("WARNING: SCRAPEOWL_API_KEY not found in ~/.hermes/.env")
        return ""

    endpoint = "https://api.scrapeowl.com/v1/scrape"
    payload = json.dumps({
        "api_key": api_key,
        "url": url,
        "render_js": True,
    }).encode()
    req = urllib.request.Request(endpoint, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
        html_body = data.get("html") or data.get("body") or data.get("content") or data.get("data") or ""
        return html_body
    except Exception:
        # retry without JS render
        try:
            payload2 = json.dumps({"api_key": api_key, "url": url, "render_js": False}).encode()
            req2 = urllib.request.Request(endpoint, data=payload2,
                                           headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req2, timeout=60, context=SSL_CTX) as resp:
                data = json.loads(resp.read())
            return data.get("html") or data.get("body") or data.get("content") or ""
        except Exception:
            return ""


def direct_fetch(url):
    """Simple urllib fetch (no JS)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


_FETCH_CACHE = {}

def fetch_page(url):
    """Fetch page HTML — try ScrapeOwl first (JS-rendered), then urllib.
    Caches results in-process to avoid re-fetching the same URL."""
    if url in _FETCH_CACHE:
        return _FETCH_CACHE[url]
    h = scrapeowl_fetch(url)
    if h and len(h) > 500:
        _FETCH_CACHE[url] = h
        return h
    h2 = direct_fetch(url)
    _FETCH_CACHE[url] = h2
    return h2

# ─────────────────────────────────────────────────────────────────────────────
# Page classification & crawling
# ─────────────────────────────────────────────────────────────────────────────

def classify_shopify_page(url, soup=None):
    """Classify a URL (and optionally its HTML) by Shopify page type."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    host = parsed.netloc.lower()

    # Homepage
    if path == "" or path == "/":
        return "homepage"

    # Product: Shopify pattern /products/<handle>
    if "/products/" in path:
        return "product"

    # Collection: /collections/<handle> (but /collections/all is still a collection)
    if "/collections/" in path:
        return "collection"

    # Blog article: /blogs/<blog>/<article>  (3+ segments after /blogs/)
    if "/blogs/" in path:
        parts = path.split("/blogs/")[-1].split("/")
        if len(parts) >= 2 and parts[1]:
            return "blog"
        # /blogs/<handle> is the blog index — treat as collection-like "page"
        return "page"

    # Static pages: /pages/<handle>
    if "/pages/" in path:
        return "page"

    # Search
    if path.endswith("/search") or "/search" in path:
        return "other"

    # If we have HTML, look for Shopify-specific markers
    if soup:
        # Product markers
        if soup.find("div", class_=re.compile("product-(template|single|grid)", re.I)):
            return "product"
        if soup.find(attrs={"data-product-id": True}):
            return "product"
        # Collection markers
        if soup.find("div", class_=re.compile("collection-(template|grid)", re.I)):
            return "collection"

    return "other"


def crawl_shopify_pages(domain, limit=100):
    """Crawl a Shopify domain, classify pages by Shopify type.

    Strategy:
      1. Start from the homepage, extract internal links.
      2. Prioritise Shopify URL patterns (/products/, /collections/, /blogs/).
      3. Fetch each via ScrapeOwl, classify, collect unique pages up to `limit`.

    Returns list of dicts: [{url, title, page_type, status}].
    """
    domain = domain.strip().rstrip("/")
    if not domain.startswith("http"):
        base_url = "https://" + domain
    else:
        base_url = domain
        domain = urlparse(domain).netloc

    parsed = urlparse(base_url)
    host = parsed.netloc

    def _norm(u):
        """Normalise a URL for dedup: drop fragment, strip trailing slash (keep root '/')."""
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme}://{p.netloc}{path}"

    base_url = _norm(base_url)

    pages = []
    queued = set()   # URLs already in the queue (dedup)
    crawled = set()  # URLs already fetched
    queue = []
    # Recursively parse Shopify sitemap index + nested sitemaps for URLs.
    # Prioritise product/collection/blog sitemaps; cap total URLs collected
    # so we don't download thousands of pages-sitemap entries for small limits.
    sitemap_seeds = [f"https://{domain}/sitemap.xml"]
    SITEMAP_QUEUE_CAP = max(limit * 20, 500)  # generous cap for dedup

    def _parse_sitemap(sm_url, depth=0):
        if len(queued) >= SITEMAP_QUEUE_CAP:
            return
        if depth > 3:
            return
        try:
            raw = direct_fetch(sm_url)
        except Exception:
            return
        if not raw:
            return
        sub_locs = re.findall(r"<loc>\s*([^<]+)\s*</loc>", raw)
        if "<sitemapindex" in raw or "<sitemap>" in raw:
            # Prioritise: products, collections, blogs first; skip huge pages_ sitemaps
            # unless we still need more URLs
            priority_subs = []
            other_subs = []
            for sub in sub_locs:
                sub_clean = sub.strip().split("?")[0]
                if any(k in sub_clean for k in ("products", "collections", "blogs", "agentic")):
                    priority_subs.append(sub.strip())
                else:
                    other_subs.append(sub.strip())
            for sub in priority_subs:
                if len(queued) >= SITEMAP_QUEUE_CAP:
                    break
                _parse_sitemap(sub, depth + 1)
            # Only parse pages_ sitemaps if we still need more URLs
            if len(queued) < SITEMAP_QUEUE_CAP:
                for sub in other_subs[:2]:  # cap at 2 pages sitemaps
                    if len(queued) >= SITEMAP_QUEUE_CAP:
                        break
                    _parse_sitemap(sub, depth + 1)
        else:
            for loc in sub_locs:
                loc = _norm(loc.strip())
                if host in urlparse(loc).netloc and loc not in queued:
                    queue.append(loc)
                    queued.add(loc)
                    if len(queued) >= SITEMAP_QUEUE_CAP:
                        return

    for sm in sitemap_seeds:
        _parse_sitemap(sm)

    # Always include the homepage first
    if base_url not in queued:
        queue.insert(0, base_url)
        queued.add(base_url)

    # Crawl
    results = []
    fetched = 0
    ptype_counts = Counter()
    for url in queue:
        if len(results) >= limit:
            break
        if url in crawled:
            continue
        crawled.add(url)

        raw = fetch_page(url)
        if not raw:
            continue
        fetched += 1

        soup = BeautifulSoup(raw, "html.parser")
        page_type = classify_shopify_page(url, soup)
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        results.append({
            "url": url,
            "title": title,
            "page_type": page_type,
            "status": "ok" if raw else "empty",
        })
        ptype_counts[page_type] += 1

        # Extract internal links to keep crawling
        if len(results) < limit:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
                    continue
                abs_url = urljoin(url, href)
                ap = urlparse(abs_url)
                if ap.netloc != host:
                    continue
                # normalise — drop fragments, trailing slash
                clean = ap.scheme + "://" + ap.netloc + ap.path.rstrip("/")
                if clean not in queued and ("/products/" in ap.path or "/collections/" in ap.path
                                          or "/blogs/" in ap.path or "/pages/" in ap.path):
                    queue.append(clean)
                    queued.add(clean)

        print(f"  crawled [{len(results)}/{limit}] {page_type:12s} {url[:90]}")

    print(f"\n  Crawl complete: {len(results)} pages, {fetched} fetched")
    print(f"  Page type breakdown: {dict(ptype_counts)}")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Schema extraction & validation
# ─────────────────────────────────────────────────────────────────────────────

def extract_jsonld_blocks(soup):
    """Extract all JSON-LD <script> blocks as list of dicts."""
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            try:
                cleaned = re.sub(r",\s*}", "}", raw)
                cleaned = re.sub(r",\s*]", "]", cleaned)
                parsed = json.loads(cleaned)
            except Exception:
                continue
        if isinstance(parsed, list):
            blocks.extend(parsed)
        elif isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _flatten_graph(blocks):
    """Flatten @graph arrays so we get a flat list of typed objects."""
    flat = []
    for b in blocks:
        if isinstance(b, dict) and "@graph" in b and isinstance(b["@graph"], list):
            flat.extend(b["@graph"])
        else:
            flat.append(b)
    return flat


def _get_types(obj):
    """Return list of @type strings from a JSON-LD object."""
    t = obj.get("@type", "")
    if isinstance(t, list):
        return [x if isinstance(x, str) else "" for x in t]
    if isinstance(t, str):
        return [t]
    return []


def _check_prop(obj, prop):
    """Check if a property exists and is non-empty on a JSON-LD object."""
    if prop not in obj:
        return False
    val = obj[prop]
    if val is None:
        return False
    if isinstance(val, str) and val.strip() == "":
        return False
    if isinstance(val, (list, dict)) and len(val) == 0:
        return False
    return True


def _find_objects_by_type(flat_blocks, type_name):
    """Find all JSON-LD objects matching a given @type (exact or list-member)."""
    matches = []
    for obj in flat_blocks:
        if not isinstance(obj, dict):
            continue
        if type_name in _get_types(obj):
            matches.append(obj)
    return matches


def _find_nested_objects(parent, type_name):
    """Recursively find nested objects of a given type within a parent JSON-LD object."""
    found = []
    if not isinstance(parent, dict):
        return found
    if type_name in _get_types(parent):
        found.append(parent)
    for k, v in parent.items():
        if k == "@type":
            continue
        if isinstance(v, dict):
            found.extend(_find_nested_objects(v, type_name))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    found.extend(_find_nested_objects(item, type_name))
    return found


def check_page_schema(url, page_type):
    """Fetch a page, extract JSON-LD, validate per Shopify page-type requirements.

    Returns dict:
      {
        url, page_type, fetched (bool),
        types_found: [str], has_schema (bool),
        missing_types: [str], missing_properties: { type_name: [prop, ...] },
        present_types: [str], score (int 0-100),
        fix_instructions: { type_name: liquid_snippet },
      }
    """
    raw = fetch_page(url)
    if not raw:
        return {
            "url": url, "page_type": page_type, "fetched": False,
            "types_found": [], "has_schema": False,
            "missing_types": [], "missing_properties": {},
            "present_types": [], "score": 0,
            "fix_instructions": {},
        }

    soup = BeautifulSoup(raw, "html.parser")
    blocks = extract_jsonld_blocks(soup)
    flat = _flatten_graph(blocks)

    types_found = set()
    for obj in flat:
        for t in _get_types(obj):
            if t:
                types_found.add(t)
    types_found = sorted(types_found)

    schema_spec = SHOPIFY_PAGE_SCHEMA.get(page_type, SHOPIFY_PAGE_SCHEMA["other"])
    required_types = schema_spec.get("required_types", {})
    alt_types = schema_spec.get("alt_types", {})
    optional_types = schema_spec.get("optional_types", [])

    missing_types = []
    missing_properties = {}
    present_types = []

    # Check each required type
    for type_name, required_props in required_types.items():
        objs = _find_objects_by_type(flat, type_name)
        if not objs:
            # Try nested (e.g. Offer inside Product)
            for parent in flat:
                if isinstance(parent, dict):
                    nested = _find_nested_objects(parent, type_name)
                    if nested:
                        objs = nested
                        break
        if not objs:
            missing_types.append(type_name)
            missing_properties[type_name] = required_props
        else:
            present_types.append(type_name)
            # Check required properties on the first matching object
            obj = objs[0]
            missing_props = [p for p in required_props if not _check_prop(obj, p)]
            if missing_props:
                missing_properties[type_name] = missing_props

    # Alternative types (e.g. collection needs EITHER CollectionPage OR ItemList)
    if alt_types:
        alt_present = False
        for alt_name, alt_props in alt_types.items():
            objs = _find_objects_by_type(flat, alt_name)
            if objs:
                alt_present = True
                present_types.append(alt_name)
                obj = objs[0]
                missing_props = [p for p in alt_props if not _check_prop(obj, p)]
                if missing_props:
                    missing_properties[alt_name] = missing_props
        if not alt_present:
            for alt_name, alt_props in alt_types.items():
                missing_types.append(alt_name)
                missing_properties[alt_name] = alt_props

    # Fix instructions
    fix_instructions = {}
    for mt in missing_types:
        if mt in LIQUID_FIXES:
            fix_instructions[mt] = LIQUID_FIXES[mt]
    for tname, props in missing_properties.items():
        if tname not in fix_instructions and tname in LIQUID_FIXES:
            fix_instructions[tname] = LIQUID_FIXES[tname]

    # Score: start 100, deduct for each missing type and each missing property
    total_required = sum(len(v) for v in required_types.values()) + sum(len(v) for v in alt_types.values())
    total_types = len(required_types) + (1 if alt_types else 0)  # alt group counts as one
    missing_type_count = len(missing_types)
    missing_prop_count = sum(len(v) for v in missing_properties.values())
    if total_required + total_types == 0:
        score = 100
    else:
        score = max(0, 100 - (missing_type_count * 25 + missing_prop_count * 5))
    if not missing_types and not missing_properties:
        score = 100

    return {
        "url": url,
        "page_type": page_type,
        "fetched": True,
        "types_found": types_found,
        "has_schema": len(types_found) > 0,
        "missing_types": missing_types,
        "missing_properties": missing_properties,
        "present_types": present_types,
        "score": score,
        "fix_instructions": fix_instructions,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title}</title>
<style>
  :root {{
    --bg:#f4f6fb; --card:#ffffff; --text:#1a2233; --muted:#6b7280;
    --border:#e3e8f0; --accent:#3b6fe0; --accent-light:#e8f0ff;
    --green:#16a34a; --red:#dc2626; --amber:#d97706; --code-bg:#1e293b;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--text); margin:0; padding:0; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px 24px; }}
  h1 {{ font-size:1.8rem; margin:0 0 4px; }}
  h2 {{ font-size:1.25rem; margin:32px 0 16px; }}
  .sub {{ color:var(--muted); font-size:.95rem; margin:0 0 24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px; margin-bottom:32px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }}
  .card .label {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
  .card .value {{ font-size:2rem; font-weight:700; margin:4px 0 0; }}
  .card .value.green {{ color:var(--green); }}
  .card .value.red {{ color:var(--red); }}
  .card .value.amber {{ color:var(--amber); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
  th, td {{ padding:10px 14px; text-align:left; font-size:.9rem; border-bottom:1px solid var(--border); }}
  th {{ background:var(--accent-light); font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:6px; font-size:.75rem; font-weight:600; }}
  .badge.product {{ background:#fef3c7; color:#92400e; }}
  .badge.collection {{ background:#dbeafe; color:#1e40af; }}
  .badge.blog {{ background:#e0e7ff; color:#3730a3; }}
  .badge.homepage {{ background:#dcfce7; color:#166534; }}
  .badge.page {{ background:#f1f5f9; color:#475569; }}
  .badge.other {{ background:#fee2e2; color:#991b1b; }}
  details {{ background:var(--card); border:1px solid var(--border); border-radius:8px; margin:8px 0; padding:0; }}
  summary {{ cursor:pointer; padding:12px 16px; font-weight:600; font-size:.9rem; }}
  details[open] summary {{ border-bottom:1px solid var(--border); }}
  .fix-block {{ padding:12px 16px; }}
  pre {{ background:var(--code-bg); color:#e2e8f0; padding:14px; border-radius:8px; overflow-x:auto; font-size:.8rem; line-height:1.5; white-space:pre-wrap; word-wrap:break-word; }}
  .coverage-bar {{ height:8px; border-radius:4px; background:#e3e8f0; margin:4px 0; width:100%; }}
  .coverage-bar > div {{ height:100%; border-radius:4px; background:var(--accent); }}
  .priority {{ display:flex; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--border); }}
  .priority:last-child {{ border:none; }}
  .priority .rank {{ width:28px; height:28px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.85rem; flex-shrink:0; }}
  .priority .impact-high {{ color:var(--red); font-weight:600; font-size:.8rem; }}
  .priority .impact-med {{ color:var(--amber); font-weight:600; font-size:.8rem; }}
  .priority .impact-low {{ color:var(--green); font-weight:600; font-size:.8rem; }}
  .footer {{ margin-top:40px; padding:16px 0; border-top:1px solid var(--border); color:var(--muted); font-size:.8rem; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
"""

HTML_FOOT = """
<div class="footer">Generated by shopify_schema_auditor.py · {ts} · noindex/nofollow</div>
</div>
</body>
</html>
"""


def _badge_class(page_type):
    return page_type if page_type in ("product", "collection", "blog", "homepage", "page", "other") else "other"


def generate_schema_report(domain, pages, schema_results, output_path):
    """Write the HTML report to <output_path>/shopify_schema_report.html. Returns the path."""
    os.makedirs(output_path, exist_ok=True)
    report_path = os.path.join(output_path, "shopify_schema_report.html")

    total = len(pages)
    with_schema = sum(1 for r in schema_results if r.get("has_schema"))
    missing_critical = sum(1 for r in schema_results if r.get("missing_types"))
    avg_score = (sum(r.get("score", 0) for r in schema_results) / total) if total else 0

    # Coverage by page type
    ptype_total = Counter(p["page_type"] for p in pages)
    ptype_with_schema = Counter()
    ptype_missing = Counter()
    coverage_by_type = {}
    for r in schema_results:
        pt = r["page_type"]
        if r.get("has_schema"):
            ptype_with_schema[pt] += 1
        if r.get("missing_types"):
            ptype_missing[pt] += 1
    for pt, cnt in ptype_total.items():
        has = ptype_with_schema.get(pt, 0)
        pct = (has / cnt * 100) if cnt else 0
        coverage_by_type[pt] = {"total": cnt, "with_schema": has, "pct": round(pct, 1)}

    # Missing schema rows
    missing_rows = [r for r in schema_results if r.get("missing_types") or r.get("missing_properties")]

    # Priority ranking: products/collections with most missing types first
    priority_list = []
    for r in missing_rows:
        impact = "high" if r["page_type"] in ("product", "collection", "homepage") else "medium"
        if r["page_type"] in ("blog", "page"):
            impact = "medium"
        if r["page_type"] == "other":
            impact = "low"
        score = len(r.get("missing_types", [])) * 3 + sum(len(v) for v in r.get("missing_properties", {}).values())
        priority_list.append((r, impact, score))
    priority_list.sort(key=lambda x: -x[2])

    # Build HTML
    parts = []
    parts.append(HTML_HEAD.format(title=f"Shopify Schema Audit — {domain}"))
    parts.append(f'<h1>Shopify Schema Audit</h1>')
    parts.append(f'<p class="sub">{html.escape(domain)} · {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · {total} pages audited</p>')

    # Summary cards
    parts.append('<div class="cards">')
    parts.append(f'<div class="card"><div class="label">Total Pages</div><div class="value">{total}</div></div>')
    parts.append(f'<div class="card"><div class="label">With Schema</div><div class="value green">{with_schema}</div></div>')
    parts.append(f'<div class="card"><div class="label">Missing Critical Schema</div><div class="value red">{missing_critical}</div></div>')
    score_class = "green" if avg_score >= 80 else ("amber" if avg_score >= 50 else "red")
    parts.append(f'<div class="card"><div class="label">Schema Score</div><div class="value {score_class}">{avg_score:.0f}</div></div>')
    parts.append('</div>')

    # Coverage by page type
    parts.append('<h2>Schema Coverage by Page Type</h2>')
    parts.append('<table><thead><tr><th>Page Type</th><th>Total</th><th>With Schema</th><th>Coverage</th></tr></thead><tbody>')
    for pt in ["product", "collection", "blog", "homepage", "page", "other"]:
        if pt not in coverage_by_type:
            continue
        c = coverage_by_type[pt]
        label = SHOPIFY_PAGE_SCHEMA.get(pt, {}).get("label", pt.title())
        parts.append(
            f'<tr><td><span class="badge {_badge_class(pt)}">{label}</span></td>'
            f'<td>{c["total"]}</td><td>{c["with_schema"]}</td>'
            f'<td><div class="coverage-bar"><div style="width:{c["pct"]}%"></div></div>{c["pct"]}%</td></tr>'
        )
    parts.append('</tbody></table>')

    # Missing schema table (expandable)
    parts.append('<h2>Missing Schema ({} pages)</h2>'.format(len(missing_rows)))
    if not missing_rows:
        parts.append('<p class="sub">No critical schema gaps detected. 🎉</p>')
    else:
        parts.append('<table><thead><tr><th>URL</th><th>Page Type</th><th>Expected Schema</th><th>Missing</th></tr></thead><tbody>')
        for r in missing_rows:
            url_short = r["url"][:80] + "…" if len(r["url"]) > 80 else r["url"]
            spec = SHOPIFY_PAGE_SCHEMA.get(r["page_type"], {})
            expected = ", ".join(list(spec.get("required_types", {}).keys()) + list(spec.get("alt_types", {}).keys()))
            missing_summary = []
            for mt in r.get("missing_types", []):
                missing_summary.append(f"<strong>{mt}</strong> (type missing)")
            for tname, props in r.get("missing_properties", {}).items():
                if tname not in r.get("missing_types", []):
                    missing_summary.append(f"{tname}: {', '.join(props)}")
            missing_html = "<br>".join(missing_summary) if missing_summary else "—"
            parts.append(
                f'<tr><td><a href="{html.escape(r["url"])}" target="_blank">{html.escape(url_short)}</a></td>'
                f'<td><span class="badge {_badge_class(r["page_type"])}">{SHOPIFY_PAGE_SCHEMA.get(r["page_type"], {}).get("label", r["page_type"])}</span></td>'
                f'<td>{html.escape(expected)}</td><td>{missing_html}</td></tr>'
            )
        parts.append('</tbody></table>')

    # Shopify theme fix instructions
    parts.append('<h2>Shopify Theme Fix Instructions</h2>')
    if not missing_rows:
        parts.append('<p class="sub">No fixes needed.</p>')
    else:
        seen_fixes = set()
        for r in priority_list:
            for fix_type, code in r[0].get("fix_instructions", {}).items():
                if fix_type in seen_fixes:
                    continue
                seen_fixes.add(fix_type)
                spec = SHOPIFY_PAGE_SCHEMA.get(r[0]["page_type"], {})
                tmpl = spec.get("liquid_template", "theme.liquid")
                parts.append(
                    f'<details><summary>Add <code>{html.escape(fix_type)}</code> schema — {html.escape(tmpl)}</summary>'
                    f'<div class="fix-block"><p class="sub">Paste this Liquid snippet into <code>{html.escape(tmpl)}</code> inside the <code>&lt;head&gt;</code> or near the page content:</p>'
                    f'<pre>{html.escape(code)}</pre></div></details>'
                )

    # Schema priority ranking
    parts.append('<h2>Schema Fix Priority Ranking</h2>')
    if not priority_list:
        parts.append('<p class="sub">No fixes needed.</p>')
    else:
        parts.append('<div>')
        for i, (r, impact, score) in enumerate(priority_list[:20], 1):
            label = SHOPIFY_PAGE_SCHEMA.get(r["page_type"], {}).get("label", r["page_type"])
            missing_desc = ", ".join(r.get("missing_types", []))
            parts.append(
                f'<div class="priority">'
                f'<span class="rank">{i}</span>'
                f'<span class="impact-{impact}">{impact.upper()}</span>'
                f'<span><strong>{label}</strong> — {html.escape(r["url"][:70])}<br>'
                f'<span class="sub">Missing: {html.escape(missing_desc or "properties only")}</span></span>'
                f'</div>'
            )
        parts.append('</div>')

    parts.append(HTML_FOOT.format(ts=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")))

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    print(f"\nReport written: {report_path}")
    return report_path

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shopify Schema Auditor")
    parser.add_argument("--domain", required=True, help="Shopify domain (e.g. softprowatersystems.com)")
    parser.add_argument("--limit", type=int, default=100, help="Max pages to crawl (default 100)")
    parser.add_argument("--output", required=True, help="Output directory for HTML report")
    args = parser.parse_args()

    print(f"=== Shopify Schema Auditor ===")
    print(f"Domain: {args.domain}")
    print(f"Limit:   {args.limit}")
    print(f"Output:  {args.output}\n")

    # 1. Crawl
    print("[1/3] Crawling pages…")
    pages = crawl_shopify_pages(args.domain, limit=args.limit)
    if not pages:
        print("ERROR: No pages crawled. Check domain / network / ScrapeOwl key.")
        sys.exit(1)

    # 2. Schema checks
    print(f"\n[2/3] Auditing schema on {len(pages)} pages…")
    schema_results = []
    for p in pages:
        r = check_page_schema(p["url"], p["page_type"])
        schema_results.append(r)
        status = "✓" if not r["missing_types"] else "✗"
        print(f"  {status} [{r['score']:3d}] {p['page_type']:12s} {p['url'][:80]}")

    # 3. Report
    print(f"\n[3/3] Generating report…")
    report_path = generate_schema_report(args.domain, pages, schema_results, args.output)

    # Summary
    total = len(pages)
    with_schema = sum(1 for r in schema_results if r.get("has_schema"))
    missing = sum(1 for r in schema_results if r.get("missing_types"))
    avg = (sum(r.get("score", 0) for r in schema_results) / total) if total else 0
    print(f"\n=== Summary ===")
    print(f"Pages:           {total}")
    print(f"With schema:     {with_schema}")
    print(f"Missing critical: {missing}")
    print(f"Avg score:       {avg:.0f}/100")
    print(f"Report:          {report_path}")


if __name__ == "__main__":
    main()
