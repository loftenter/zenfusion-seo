#!/usr/bin/env python3
"""
Structured Data Audit — domain-level schema markup coverage report.

Crawls all pages of a domain (via DataForSEO on_page/instant_pages + ScrapeOwl
JS rendering), extracts JSON-LD + microdata + RDFa from each page, classifies the
schema types present, validates schema.org required properties, and emits an
expandable white-SaaS-theme HTML report.

Usage:
  python3 structured_data_audit.py --domain example.com --limit 100 --output /tmp/schema_audit

Environment (from ~/.hermes/.env):
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD, SCRAPEOWL_API_KEY
"""

import sys, os, json, re, base64, urllib.request, urllib.parse, ssl, time, html
from urllib.parse import urlparse
from collections import defaultdict, Counter

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Requires: pip3 install beautifulsoup4")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

REQUIRED_PROPS = {
    "Product": ["name", "image", "description", "offers", "brand", "aggregateRating"],
    "Article": ["headline", "author", "datePublished", "image"],
    "Organization": ["name", "url", "logo", "sameAs", "contactPoint"],
    "FAQPage": ["mainEntity"],
    "BreadcrumbList": ["itemListElement"],
    "Review": ["reviewRating", "author", "datePublished"],
    "AggregateRating": ["ratingValue", "reviewCount", "bestRating"],
    "Offer": ["price", "availability", "url"],
    "Event": ["name", "startDate", "location"],
    "Recipe": ["name", "recipeIngredient", "recipeInstructions"],
    "VideoObject": ["name", "uploadDate", "thumbnailUrl", "contentUrl"],
    "Person": ["name"],
    "WebSite": ["name", "url"],
    "WebPage": ["name"],
    "CollectionPage": ["name"],
    "ItemList": ["itemListElement"],
    "SearchAction": ["target", "query-input"],
    "ItemList": ["itemListElement"],
}

# Recommended schema per page-type (heuristic detection)
PAGE_TYPE_SCHEMA = {
    "product": {"recommended": ["Product", "Offer", "Review", "AggregateRating", "BreadcrumbList"],
                "label": "Product Page"},
    "article": {"recommended": ["Article", "Person", "BreadcrumbList", "FAQPage"],
                "label": "Blog/Article Page"},
    "homepage": {"recommended": ["Organization", "WebSite", "SearchAction"],
                 "label": "Homepage"},
    "collection": {"recommended": ["CollectionPage", "BreadcrumbList", "ItemList"],
                   "label": "Collection Page"},
    "default": {"recommended": ["WebPage", "BreadcrumbList"],
                "label": "Standard Page"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Credential loader
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
                    env[k] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

# ─────────────────────────────────────────────────────────────────────────────
# API helpers (urllib only — no requests)
# ─────────────────────────────────────────────────────────────────────────────

def _dataforseo_creds():
    login = ENV.get("DATAFORSEO_LOGIN", "")
    password = ENV.get("DATAFORSEO_PASSWORD", "")
    return login, password


def dataforseo_instant_pages(domain, limit=100):
    """POST /v3/on_page/instant_pages — returns pages list with on-page data."""
    login, password = _dataforseo_creds()
    if not login or not password:
        print("ERROR: DATAFORSEO credentials missing from ~/.hermes/.env")
        return []

    url = "https://api.dataforseo.com/v3/on_page/instant_pages"
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    payload = json.dumps([{
        "target": domain,
        "max_crawl_pages": limit,
        "load_content": False,   # we only need the page list + meta, not full HTML
    }]).encode()

    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"DataForSEO instant_pages error: {e}")
        return []

    if data.get("status_code") != 20000:
        print(f"DataForSEO API error: {data.get('status_code')} {data.get('status_message')}")
        return []

    tasks = data.get("tasks", [])
    pages = []
    for t in tasks:
        result = t.get("result", [])
        for r in (result if isinstance(result, list) else []):
            items = r.get("items", [])
            for item in items:
                pages.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "status_code": item.get("status_code", 0),
                    "meta": item.get("meta", {}),
                    "page_type": item.get("page_type", ""),
                    "content": item.get("content", {}),
                })
    return pages


def scrapeowl_fetch(url):
    """Fetch rendered HTML via ScrapeOwl (renders JS).  Returns html string or ''."""
    api_key = ENV.get("SCRAPEOWL_API_KEY", "")
    if not api_key:
        return ""

    endpoint = "https://api.scrapeowl.com/v1/scrape"
    payload = json.dumps({
        "api_key": api_key,
        "url": url,
        "render_js": True,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
        # ScrapeOwl response field name varies
        html_body = data.get("html") or data.get("body") or data.get("content") or data.get("data") or ""
        return html_body
    except Exception:
        # Try without JS render as fallback
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
    """Simple urllib fetch as a last-resort fallback (no JS)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Schema extraction & classification
# ─────────────────────────────────────────────────────────────────────────────

def _extract_jsonld(soup):
    """Extract all JSON-LD blocks; return list of dicts."""
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # try to salvage by removing trailing commas
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


def _extract_microdata(soup):
    """Extract microdata (itemscope/itemtype) — return list of dicts."""
    items = []
    for el in soup.find_all(attrs={"itemtype": True}):
        itemtype = el.get("itemtype", "")
        item = {"_source": "microdata", "@type": "", "properties": {}}
        if itemtype:
            # e.g. https://schema.org/Product
            m = re.search(r"/(\w+)$", itemtype)
            if m:
                item["@type"] = m.group(1)
        for prop in el.find_all(attrs={"itemprop": True}):
            pname = prop.get("itemprop")
            pval = prop.get("content") or prop.get("href") or prop.get("src") or prop.get_text(strip=True)
            if pname and pval:
                item["properties"][pname] = pval
        items.append(item)
    return items


def _extract_rdfa(soup):
    """Extract RDFa (typeof/vocab) — return list of dicts."""
    items = []
    for el in soup.find_all(attrs={"typeof": True}):
        rtype = el.get("typeof", "")
        item = {"_source": "rdfa", "@type": rtype, "properties": {}}
        for prop in el.find_all(attrs={"property": True}):
            pname = prop.get("property")
            pval = prop.get("content") or prop.get("resource") or prop.get("href") or prop.get_text(strip=True)
            if pname and pval:
                item["properties"][pname] = pval
        items.append(item)
    return items


def classify_schema_types(soup):
    """Detect all schema types present on the page.

    Returns dict: {
        "json_ld": [ {dict}, ... ],
        "microdata": [ {dict}, ... ],
        "rdfa": [ {dict}, ... ],
        "types_found": set/list of @type strings,
        "has_schema": bool,
    }
    """
    jsonld = _extract_jsonld(soup)
    microdata = _extract_microdata(soup)
    rdfa = _extract_rdfa(soup)

    types = set()

    def add_type(obj):
        t = obj.get("@type", "")
        if isinstance(t, list):
            for tt in t:
                if tt:
                    types.add(str(tt))
        elif t:
            types.add(str(t))
        # also handle nested @graph
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for g in graph:
                add_type(g)
        elif isinstance(graph, dict):
            add_type(graph)

    for obj in jsonld:
        add_type(obj)
    for obj in microdata:
        t = obj.get("@type", "")
        if t:
            types.add(str(t))
    for obj in rdfa:
        t = obj.get("@type", "")
        if t:
            types.add(str(t))

    return {
        "json_ld": jsonld,
        "microdata": microdata,
        "rdfa": rdfa,
        "types_found": sorted(types),
        "has_schema": len(types) > 0 or len(jsonld) > 0 or len(microdata) > 0 or len(rdfa) > 0,
    }


def _flatten_jsonld_keys(obj, prefix=""):
    """Recursively flatten a JSON-LD dict into a set of property keys."""
    keys = set()
    if not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        if k.startswith("@"):
            continue
        keys.add(k)
        if isinstance(v, dict):
            keys |= _flatten_jsonld_keys(v, prefix=f"{prefix}{k}.")
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    keys |= _flatten_jsonld_keys(item, prefix=f"{prefix}{k}.")
    return keys


def validate_schema_type(schema_type, detected_props):
    """Check required properties for a schema type.

    Returns: (missing_required: list[str], present: list[str])
    """
    required = REQUIRED_PROPS.get(schema_type, [])
    missing = [p for p in required if p not in detected_props]
    present = [p for p in required if p in detected_props]
    return missing, present


def get_all_properties(jsonld_list, microdata_list, rdfa_list):
    """Collect all property keys detected on the page across all sources."""
    props = set()
    for obj in jsonld_list:
        if isinstance(obj, dict):
            props |= _flatten_jsonld_keys(obj)
    for obj in microdata_list:
        for k in obj.get("properties", {}):
            props.add(k)
    for obj in rdfa_list:
        for k in obj.get("properties", {}):
            props.add(k)
    return props


# ─────────────────────────────────────────────────────────────────────────────
# Page-type detection heuristic
# ─────────────────────────────────────────────────────────────────────────────

def detect_page_type(url, title, dfs_page_type=""):
    """Heuristic page-type detection."""
    path = urlparse(url).path.lower()
    pt = (dfs_page_type or "").lower()

    if path in ("", "/", "/index", "/index.html", "/home"):
        return "homepage"
    if pt == "ecommerce/product" or "/product" in path or re.search(r"/products?/", path):
        return "product"
    if any(x in path for x in ["/blog", "/article", "/post", "/news", "/blog/"]):
        return "article"
    if any(x in path for x in ["/collection", "/category", "/collections", "/shop", "/catalog"]):
        return "collection"
    if "/faq" in path:
        return "article"  # FAQ falls under article recommendations
    return "default"


# ─────────────────────────────────────────────────────────────────────────────
# Main audit function
# ─────────────────────────────────────────────────────────────────────────────

def audit_domain_schema(domain, limit=100):
    """Crawl domain pages and extract schema markup from each.

    Returns list of per-page result dicts.
    """
    domain = domain.strip().lower()
    if domain.startswith("http"):
        domain = urlparse(domain).netloc

    print(f"[1/3] Fetching page list from DataForSEO instant_pages for {domain} (limit {limit})...")
    dfs_pages = dataforseo_instant_pages(domain, limit=limit)
    if not dfs_pages:
        print("  No pages returned from DataForSEO. Falling back to direct crawl of root + common paths.")
        # Minimal fallback: just the homepage
        dfs_pages = [{"url": f"https://{domain}/", "title": "", "status_code": 0,
                      "meta": {}, "page_type": "", "content": {}}]

    print(f"  Got {len(dfs_pages)} pages from DataForSEO.")

    # Cap to limit
    dfs_pages = dfs_pages[:limit]

    results = []
    total = len(dfs_pages)

    for i, page in enumerate(dfs_pages, 1):
        url = page.get("url", "")
        if not url:
            continue
        title = page.get("title", page.get("meta", {}).get("title", "")) if isinstance(page.get("meta"), dict) else ""
        if not title and isinstance(page.get("content"), dict):
            title = page.get("content", {}).get("title", "")

        print(f"[2/3] ({i}/{total}) Scraping {url} ...")

        # ScrapeOwl first (JS rendering), direct fetch fallback
        raw_html = scrapeowl_fetch(url)
        fetch_source = "scrapeowl"
        if not raw_html or len(raw_html) < 500:
            raw_html = direct_fetch(url)
            fetch_source = "direct"
        if not raw_html:
            raw_html = ""

        soup = BeautifulSoup(raw_html, "html.parser") if raw_html else None
        schema_data = classify_schema_types(soup) if soup else {
            "json_ld": [], "microdata": [], "rdfa": [], "types_found": [], "has_schema": False,
        }

        # Detect page type
        page_type = detect_page_type(url, title, page.get("page_type", ""))

        # Get all detected properties for validation
        all_props = get_all_properties(
            schema_data["json_ld"],
            schema_data["microdata"],
            schema_data["rdfa"],
        )

        # Validate each schema type found
        validation = {}
        for stype in schema_data["types_found"]:
            missing, present = validate_schema_type(stype, all_props)
            validation[stype] = {"missing": missing, "present": present}

        # Missing recommended schema for page type
        recommended = PAGE_TYPE_SCHEMA.get(page_type, PAGE_TYPE_SCHEMA["default"])["recommended"]
        missing_recommended = [r for r in recommended if r not in schema_data["types_found"]]

        result = {
            "url": url,
            "title": title,
            "status_code": page.get("status_code", 0),
            "page_type": page_type,
            "page_type_label": PAGE_TYPE_SCHEMA.get(page_type, PAGE_TYPE_SCHEMA["default"])["label"],
            "fetch_source": fetch_source,
            "has_schema": schema_data["has_schema"],
            "types_found": schema_data["types_found"],
            "json_ld": schema_data["json_ld"],
            "microdata": schema_data["microdata"],
            "rdfa": schema_data["rdfa"],
            "all_properties": sorted(all_props),
            "validation": validation,
            "missing_recommended": missing_recommended,
            "recommended": recommended,
            "html_size": len(raw_html),
        }
        results.append(result)

        # Be nice to APIs
        time.sleep(0.3)

    print(f"[3/3] Processed {len(results)} pages. Generating report...")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# HTML Report Generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_schema_report(domain, results, output_path):
    """Generate white-SaaS-theme HTML report with expandable rows."""

    total_pages = len(results)
    pages_with_schema = sum(1 for r in results if r["has_schema"])
    coverage_pct = round((pages_with_schema / total_pages * 100), 1) if total_pages else 0

    # Schema type distribution
    type_counter = Counter()
    for r in results:
        for t in r["types_found"]:
            type_counter[t] += 1

    # Pages missing critical schema
    missing_critical = []
    for r in results:
        if r["missing_recommended"]:
            missing_critical.append(r)

    # Validation issues
    validation_issues = []
    for r in results:
        for stype, val in r["validation"].items():
            if val["missing"]:
                validation_issues.append({
                    "url": r["url"],
                    "schema_type": stype,
                    "missing": val["missing"],
                })

    # Build type distribution bars
    type_dist_html = ""
    for stype, count in type_counter.most_common():
        pct = round(count / total_pages * 100, 1) if total_pages else 0
        type_dist_html += f"""
        <div class="type-bar-row">
            <span class="type-name">{html.escape(stype)}</span>
            <div class="bar-container"><div class="bar-fill" style="width:{pct}%"></div></div>
            <span class="type-count">{count} ({pct}%)</span>
        </div>"""

    # Build page rows (expandable)
    page_rows_html = ""
    for i, r in enumerate(results):
        types_badge = " ".join(
            f'<span class="badge">{html.escape(t)}</span>' for t in r["types_found"]
        )
        status_class = "status-ok" if r["has_schema"] else "status-bad"
        status_text = "✓ Has schema" if r["has_schema"] else "✗ No schema"

        missing_rec = ", ".join(r["missing_recommended"]) if r["missing_recommended"] else "None"
        missing_rec_class = "missing-list" if r["missing_recommended"] else "ok-text"

        # Validation issues for this page
        val_issues_html = ""
        for stype, val in r["validation"].items():
            if val["missing"]:
                val_issues_html += f'<div class="val-issue"><strong>{html.escape(stype)}</strong>: missing {", ".join(html.escape(m) for m in val["missing"])}</div>'

        # Schema detail (JSON-LD pretty-printed, truncated)
        jsonld_pretty = ""
        for j in r["json_ld"]:
            pretty = json.dumps(j, indent=2, ensure_ascii=False)
            if len(pretty) > 800:
                pretty = pretty[:800] + "\n... (truncated)"
            jsonld_pretty += html.escape(pretty) + "\n\n"

        microdata_html = ""
        for m in r["microdata"]:
            microdata_html += html.escape(json.dumps(m, indent=2, ensure_ascii=False)) + "\n\n"

        rdfa_html = ""
        for m in r["rdfa"]:
            rdfa_html += html.escape(json.dumps(m, indent=2, ensure_ascii=False)) + "\n\n"

        detail_id = f"detail-{i}"
        props_str = ", ".join(r["all_properties"][:30])
        if len(r["all_properties"]) > 30:
            props_str += f" ... (+{len(r['all_properties'])-30} more)"

        page_rows_html += f"""
        <div class="page-row" onclick="toggleDetail('{detail_id}')">
            <div class="page-row-main">
                <span class="expand-icon">▸</span>
                <span class="{status_class}">{status_text}</span>
                <span class="page-url">{html.escape(r['url'])}</span>
                <span class="page-type-tag">{html.escape(r['page_type_label'])}</span>
                <span class="page-types-badges">{types_badge}</span>
            </div>
        </div>
        <div id="{detail_id}" class="page-detail" style="display:none;">
            <table class="detail-table">
                <tr><th>Title</th><td>{html.escape(str(r['title'] or '—'))}</td></tr>
                <tr><th>Page Type</th><td>{html.escape(r['page_type_label'])} ({html.escape(r['page_type'])})</td></tr>
                <tr><th>HTTP Status</th><td>{r['status_code']}</td></tr>
                <tr><th>Fetch Source</th><td>{html.escape(r['fetch_source'])}</td></tr>
                <tr><th>HTML Size</th><td>{r['html_size']:,} bytes</td></tr>
                <tr><th>Schema Types Found</th><td>{types_badge if types_badge else '<em>None</em>'}</td></tr>
                <tr><th>Properties Detected</th><td>{html.escape(props_str) if props_str else '<em>None</em>'}</td></tr>
                <tr><th>Missing Recommended</th><td class="{missing_rec_class}">{html.escape(missing_rec)}</td></tr>
                <tr><th>Validation Issues</th><td>{val_issues_html if val_issues_html else '<span class="ok-text">All required properties present</span>'}</td></tr>
                <tr><th>JSON-LD</th><td><pre class="json-block">{jsonld_pretty if jsonld_pretty else 'No JSON-LD found'}</pre></td></tr>
                <tr><th>Microdata</th><td><pre class="json-block">{microdata_html if microdata_html else 'No microdata found'}</pre></td></tr>
                <tr><th>RDFa</th><td><pre class="json-block">{rdfa_html if rdfa_html else 'No RDFa found'}</pre></td></tr>
            </table>
        </div>"""

    # Recommendations section
    recs = []
    if coverage_pct < 100:
        recs.append(f"<strong>{total_pages - pages_with_schema} pages</strong> have no structured data at all — add schema markup.")
    if missing_critical:
        recs.append(f"<strong>{len(missing_critical)} pages</strong> are missing critical schema types for their page category.")
    if validation_issues:
        recs.append(f"<strong>{len(validation_issues)} schema instances</strong> are missing required properties — see validation table.")
    if not type_counter.get("Organization") and not type_counter.get("WebSite"):
        recs.append("No <strong>Organization</strong> or <strong>WebSite</strong> schema detected on the homepage — add these for brand entity signals.")
    if not type_counter.get("BreadcrumbList"):
        recs.append("No <strong>BreadcrumbList</strong> schema found on any page — add breadcrumbs to improve navigation signals.")
    if not type_counter.get("FAQPage"):
        recs.append("No <strong>FAQPage</strong> schema found — adding FAQ schema can help win rich results.")
    recs.append("Ensure all <strong>Product</strong> pages have Offer, Review, and AggregateRating for rich snippets.")
    recs.append("Ensure all <strong>Article</strong> pages have Person (author) and datePublished for author signals.")
    recs_html = "".join(f"<li>{r}</li>" for r in recs)

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="robots" content="noindex,nofollow">
<title>Schema Audit Report — {html.escape(domain)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #ffffff;
    color: #1a1a2e;
    font-size: 16px;
    line-height: 1.6;
    margin: 0;
    padding: 0;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 40px 24px; }}
  h1 {{ font-size: 24px; color: #1a1a2e; margin-bottom: 4px; }}
  h2 {{ font-size: 20px; color: #1a1a2e; margin-top: 32px; margin-bottom: 12px; border-bottom: 2px solid #e8ecf1; padding-bottom: 8px; }}
  h3 {{ font-size: 18px; color: #333; margin-top: 20px; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .header {{ margin-bottom: 24px; }}
  .header .domain {{ font-size: 16px; color: #666; }}
  .header .date {{ font-size: 14px; color: #999; }}

  .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 24px 0; }}
  .stat-card {{ background: #f8f9fb; border: 1px solid #e8ecf1; border-radius: 8px; padding: 20px; text-align: center; }}
  .stat-card .stat-value {{ font-size: 28px; font-weight: 700; color: #0066cc; }}
  .stat-card .stat-label {{ font-size: 14px; color: #666; margin-top: 4px; }}

  .coverage-bar-container {{ background: #e8ecf1; border-radius: 6px; height: 24px; margin: 12px 0; overflow: hidden; }}
  .coverage-bar-fill {{ background: #0066cc; height: 100%; border-radius: 6px; transition: width 0.5s; }}
  .coverage-label {{ font-size: 14px; color: #666; }}

  .type-bar-row {{ display: flex; align-items: center; gap: 12px; margin: 6px 0; }}
  .type-bar-row .type-name {{ width: 180px; font-size: 14px; font-weight: 500; }}
  .bar-container {{ flex: 1; background: #e8ecf1; border-radius: 4px; height: 16px; overflow: hidden; }}
  .bar-fill {{ background: #0066cc; height: 100%; border-radius: 4px; }}
  .type-count {{ width: 80px; font-size: 14px; color: #666; text-align: right; }}

  .page-row {{ border: 1px solid #e8ecf1; border-radius: 6px; margin: 6px 0; cursor: pointer; padding: 12px 16px; transition: background 0.15s; }}
  .page-row:hover {{ background: #f8f9fb; }}
  .page-row-main {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .expand-icon {{ font-size: 14px; color: #999; width: 16px; }}
  .status-ok {{ color: #1a7a3a; font-size: 14px; font-weight: 500; }}
  .status-bad {{ color: #c62828; font-size: 14px; font-weight: 500; }}
  .page-url {{ font-size: 14px; color: #0066cc; flex: 1; min-width: 200px; word-break: break-all; }}
  .page-type-tag {{ font-size: 12px; background: #e8ecf1; color: #555; border-radius: 4px; padding: 2px 8px; }}
  .page-types-badges {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .badge {{ font-size: 12px; background: #e3f0ff; color: #0066cc; border-radius: 4px; padding: 2px 8px; }}

  .page-detail {{ background: #f8f9fb; border: 1px solid #e8ecf1; border-radius: 6px; padding: 16px; margin-bottom: 8px; }}
  .detail-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  .detail-table th {{ text-align: left; width: 180px; padding: 8px 12px; border-bottom: 1px solid #e8ecf1; color: #555; vertical-align: top; }}
  .detail-table td {{ padding: 8px 12px; border-bottom: 1px solid #e8ecf1; }}

  .json-block {{ background: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 6px; font-size: 13px; overflow-x: auto; white-space: pre-wrap; max-height: 300px; overflow-y: auto; }}
  .missing-list {{ color: #c62828; font-weight: 500; }}
  .ok-text {{ color: #1a7a3a; }}
  .val-issue {{ margin: 2px 0; font-size: 13px; }}

  .recs-list {{ list-style: none; padding: 0; }}
  .recs-list li {{ padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 15px; }}
  .recs-list li::before {{ content: "→ "; color: #0066cc; font-weight: bold; }}

  .missing-table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin: 12px 0; }}
  .missing-table th {{ background: #f8f9fb; text-align: left; padding: 8px 12px; border: 1px solid #e8ecf1; font-size: 13px; }}
  .missing-table td {{ padding: 8px 12px; border: 1px solid #e8ecf1; }}

  .footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #e8ecf1; font-size: 13px; color: #999; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Structured Data Audit Report</h1>
    <div class="domain">{html.escape(domain)}</div>
    <div class="date">Generated: {time.strftime("%Y-%m-%d %H:%M:%S")} — {total_pages} pages analyzed</div>
  </div>

  <h2>Schema Coverage Overview</h2>
  <div class="stats-grid">
    <div class="stat-card"><div class="stat-value">{total_pages}</div><div class="stat-label">Total Pages</div></div>
    <div class="stat-card"><div class="stat-value">{pages_with_schema}</div><div class="stat-label">Pages with Schema</div></div>
    <div class="stat-card"><div class="stat-value">{coverage_pct}%</div><div class="stat-label">Coverage Rate</div></div>
    <div class="stat-card"><div class="stat-value">{len(type_counter)}</div><div class="stat-label">Schema Types Detected</div></div>
  </div>
  <div class="coverage-bar-container"><div class="coverage-bar-fill" style="width:{coverage_pct}%"></div></div>
  <div class="coverage-label">{coverage_pct}% of pages have structured data markup</div>

  <h2>Schema Type Distribution</h2>
  {type_dist_html if type_dist_html else '<p>No schema types detected across any pages.</p>'}

  <h2>Pages Missing Critical Schema Types</h2>
  {'<table class="missing-table"><thead><tr><th>Page URL</th><th>Page Type</th><th>Missing Schema Types</th></tr></thead><tbody>' + ''.join(
        f'<tr><td>{html.escape(r["url"])}</td><td>{html.escape(r["page_type_label"])}</td><td class="missing-list">{html.escape(", ".join(r["missing_recommended"]))}</td></tr>'
        for r in missing_critical
    ) + '</tbody></table>' if missing_critical else '<p class="ok-text">All pages have their recommended schema types ✓</p>'}

  <h2>Schema Validation Issues (Missing Required Properties)</h2>
  {'<table class="missing-table"><thead><tr><th>Page URL</th><th>Schema Type</th><th>Missing Properties</th></tr></thead><tbody>' + ''.join(
        f'<tr><td>{html.escape(vi["url"])}</td><td>{html.escape(vi["schema_type"])}</td><td class="missing-list">{html.escape(", ".join(vi["missing"]))}</td></tr>'
        for vi in validation_issues
    ) + '</tbody></table>' if validation_issues else '<p class="ok-text">All schema instances have their required properties ✓</p>'}

  <h2>Recommendations</h2>
  <ul class="recs-list">
    {recs_html}
  </ul>

  <h2>Page-by-Page Schema Details (click to expand)</h2>
  {page_rows_html if page_rows_html else '<p>No pages to display.</p>'}

  <div class="footer">
    Structured Data Audit Report — Zenfusion SEO Toolkit — {html.escape(domain)} — {time.strftime("%Y-%m-%d")}
  </div>
</div>
<script>
function toggleDetail(id) {{
  var el = document.getElementById(id);
  var row = el.previousElementSibling;
  var icon = row.querySelector('.expand-icon');
  if (el.style.display === 'none') {{
    el.style.display = 'block';
    icon.textContent = '▾';
  }} else {{
    el.style.display = 'none';
    icon.textContent = '▸';
  }}
}}
</script>
</body>
</html>"""

    # Ensure output directory exists
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, "schema_audit_report.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_html)

    # Also save raw JSON results
    json_file = os.path.join(output_path, "schema_audit_results.json")
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"  Report saved: {report_file}")
    print(f"  JSON saved: {json_file}")
    return report_file


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Audit structured data across a domain.")
    parser.add_argument("--domain", required=True, help="Domain to audit (e.g. example.com)")
    parser.add_argument("--limit", type=int, default=100, help="Max pages to crawl (default 100)")
    parser.add_argument("--output", default="/tmp/schema_audit", help="Output directory for report")
    args = parser.parse_args()

    results = audit_domain_schema(args.domain, limit=args.limit)
    report_path = generate_schema_report(args.domain, results, args.output)
    print(f"\nDone! Report: {report_path}")
    return report_path


if __name__ == "__main__":
    main()