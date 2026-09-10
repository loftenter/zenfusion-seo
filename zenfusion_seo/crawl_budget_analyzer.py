#!/usr/bin/env python3
"""
Crawl Budget Analyzer — Identifies pages that waste Googlebot's crawl budget.

Discovers URLs from the domain's sitemap, calls DataForSEO on_page/instant_pages
for each, classifies pages as Budget Waste / Low Value / Healthy, and generates
an HTML report with summary cards, SVG distribution, waste breakdown table,
thin content list, largest/slowest pages, and recommendations.

Usage:
  python3 crawl_budget_analyzer.py --domain example.com --limit 20 [--output /tmp/report]

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD  — in ~/.hermes/.env
  SCRAPEOWL_API_KEY (optional fallback)  — in ~/.hermes/.env
"""

import os
import sys
import json
import base64
import ssl
import time
import argparse
import html as html_mod
import urllib.request
import urllib.parse
from datetime import datetime
from urllib.parse import urlparse, urldefrag
from xml.etree import ElementTree as ET

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
API_BASE = "https://api.dataforseo.com/v3"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

USER_AGENT = "Mozilla/5.0 (compatible; ZenfusionCrawlBudgetAnalyzer/1.0)"
DELAY = 0.3  # seconds between API calls
DEFAULT_LIMIT = 50

# Classification thresholds
THIN_CONTENT_WORDS = 200       # < this = thin content
LARGE_PAGE_BYTES = 3 * 1024 * 1024  # 3 MB
SLOW_PAGE_MS = 3000            # > this = slow
REDIRECT_CODES = {301, 302, 303, 307, 308}
WASTE_CODES = {400, 401, 403, 404, 405, 410, 451, 500, 502, 503, 504}


# ── Credentials ────────────────────────────────────────────────────────────

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


def get_credentials():
    """Return (dfs_login, dfs_password, scrapeowl_key)."""
    env = load_env()
    login = env.get("DATAFORSEO_LOGIN") or os.environ.get("DATAFORSEO_LOGIN")
    password = env.get("DATAFORSEO_PASSWORD") or os.environ.get("DATAFORSEO_PASSWORD")
    scrapeowl = env.get("SCRAPEOWL_API_KEY") or os.environ.get("SCRAPEOWL_API_KEY")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found", file=sys.stderr)
        sys.exit(1)
    return login, password, scrapeowl


# ── Sitemap discovery ──────────────────────────────────────────────────────

def get_domain_root(domain):
    """Normalize a domain input to https://domain."""
    domain = domain.strip()
    if domain.startswith("http://") or domain.startswith("https://"):
        parsed = urlparse(domain)
        return f"{parsed.scheme}://{parsed.netloc}", parsed.netloc
    return f"https://{domain}", domain


def fetch_url(url, timeout=15):
    """Fetch a URL and return (status_code, text)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except Exception:
        return 0, ""


def discover_urls_from_sitemap(root_url, domain, limit):
    """Discover URLs from sitemap.xml (including sitemap index files)."""
    urls = []
    seen = set()

    def _parse_sitemap(sm_url):
        if len(urls) >= limit * 2:
            return
        status, text = fetch_url(sm_url)
        if status != 200 or not text:
            return
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        # Strip XML namespaces for simplicity
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "loc" and elem.text:
                loc = elem.text.strip()
                if not loc:
                    continue
                # If this is a sitemap index, recurse
                if sm_url == loc:
                    continue
                # Check if it's a sub-sitemap
                if loc.endswith(".xml") or "sitemap" in loc.lower():
                    _parse_sitemap(loc)
                    continue
                # Filter to same domain
                parsed = urlparse(loc)
                netloc = parsed.netloc.replace("www.", "")
                d = domain.replace("www.", "")
                if netloc == d or netloc.endswith("." + d):
                    norm = urldefrag(loc)[0].rstrip("/")
                    if norm and norm not in seen and not norm.endswith((".jpg", ".png", ".gif", ".css", ".js", ".svg", ".ico", ".pdf")):
                        seen.add(norm)
                        urls.append(loc)

    # Try common sitemap locations
    for sm_path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]:
        _parse_sitemap(f"{root_url}{sm_path}")
        if len(urls) >= limit:
            break

    return urls[:limit]


def discover_urls_from_robots(root_url, limit):
    """Fallback: parse robots.txt for sitemap directives."""
    urls = []
    status, text = fetch_url(f"{root_url}/robots.txt")
    if status != 200 or not text:
        return urls
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            sm_url = line.split(":", 1)[1].strip()
            if sm_url:
                sub = discover_urls_from_sitemap(sm_url, urlparse(sm_url).netloc, limit)
                urls.extend(sub)
    return urls[:limit]


def discover_urls(root_url, domain, limit):
    """Discover URLs via sitemap, with robots.txt fallback."""
    print(f"  Discovering URLs from sitemap...", flush=True)
    urls = discover_urls_from_sitemap(root_url, domain, limit)
    if len(urls) < 3:
        print(f"  Sitemap yielded {len(urls)} URLs, trying robots.txt...", flush=True)
        robots_urls = discover_urls_from_robots(root_url, limit)
        for u in robots_urls:
            if u not in urls:
                urls.append(u)
        urls = urls[:limit]
    if not urls:
        # Last resort: just crawl the homepage
        urls = [f"{root_url}/"]
    print(f"  Discovered {len(urls)} URLs to analyze", flush=True)
    return urls


# ── DataForSEO instant_pages ────────────────────────────────────────────────

def call_instant_pages(url, login, password):
    """Call DataForSEO on_page/instant_pages for a single URL.
    Returns the parsed item dict or None."""
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    endpoint = f"{API_BASE}/on_page/instant_pages"
    payload = json.dumps([{"url": url}]).encode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(endpoint, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [WARN] API request failed for {url}: {e}", file=sys.stderr)
        return None

    if data.get("status_code") != 20000:
        return None

    tasks = data.get("tasks", [])
    if not tasks or tasks[0].get("status_code") != 20000:
        return None

    try:
        items = tasks[0]["result"][0]["items"]
    except (KeyError, IndexError, TypeError):
        return None

    if not items:
        return None
    return items[0]


def analyze_pages(urls, login, password):
    """Call instant_pages for each URL and return list of page dicts."""
    pages = []
    total = len(urls)
    for i, url in enumerate(urls):
        print(f"  [{i+1}/{total}] Analyzing: {url[:80]}", flush=True)
        item = call_instant_pages(url, login, password)
        if item:
            pages.append(extract_page_data(item, url))
        else:
            # If API fails, record a minimal entry
            pages.append({
                "url": url,
                "status_code": 0,
                "title": "",
                "word_count": 0,
                "page_size_bytes": 0,
                "load_time_ms": 0,
                "is_redirect": False,
                "is_4xx": False,
                "is_5xx": False,
                "is_broken": True,
                "duplicate_title": False,
                "low_content": True,
                "large_page": False,
                "slow_page": False,
                "canonical": "",
                "meta_description": "",
                "checks": {},
            })
        if i < total - 1:
            time.sleep(DELAY)
    return pages


def extract_page_data(item, original_url):
    """Extract relevant fields from instant_pages response item."""
    meta = item.get("meta", {}) or {}
    checks = item.get("checks", {}) or {}
    content = meta.get("content", {}) or {}
    page_timing = item.get("page_timing", {}) or {}

    status_code = item.get("status_code", 200) or 200
    word_count = content.get("plain_text_word_count", 0) or 0
    page_size = item.get("size", 0) or 0
    load_time_ms = page_timing.get("duration_time", 0) or 0

    is_redirect = status_code in REDIRECT_CODES or checks.get("is_redirect", False)
    is_4xx = checks.get("is_4xx_code", False) or (400 <= status_code < 500)
    is_5xx = checks.get("is_5xx_code", False) or (500 <= status_code < 600)
    is_broken = checks.get("is_broken", False) or is_4xx or is_5xx
    duplicate_title = checks.get("duplicate_title_tag", False)
    low_content = checks.get("low_content_rate", False) or checks.get("low_character_count", False) or word_count < THIN_CONTENT_WORDS
    large_page = checks.get("large_page_size", False) or page_size > LARGE_PAGE_BYTES
    slow_page = checks.get("high_loading_time", False) or load_time_ms > SLOW_PAGE_MS

    return {
        "url": item.get("url", original_url),
        "status_code": status_code,
        "title": meta.get("title", "") or "",
        "word_count": word_count,
        "page_size_bytes": page_size,
        "load_time_ms": load_time_ms,
        "is_redirect": is_redirect,
        "is_4xx": is_4xx,
        "is_5xx": is_5xx,
        "is_broken": is_broken,
        "duplicate_title": duplicate_title,
        "low_content": low_content,
        "large_page": large_page,
        "slow_page": slow_page,
        "canonical": meta.get("canonical", "") or "",
        "meta_description": meta.get("description", "") or "",
        "checks": checks,
    }


# ── Classification ──────────────────────────────────────────────────────────

def classify_page(page):
    """Classify a page as 'Budget Waste', 'Low Value', or 'Healthy'."""
    waste_reasons = []
    if page["is_4xx"] or page["is_5xx"] or page["is_broken"]:
        waste_reasons.append(f"HTTP {page['status_code']}" if page["status_code"] else "Broken/Unreachable")
    if page["is_redirect"]:
        waste_reasons.append("Redirect")
    if page["large_page"]:
        waste_reasons.append("Oversized page")
    if page["slow_page"]:
        waste_reasons.append("Slow loading")
    if page["word_count"] < THIN_CONTENT_WORDS and page["word_count"] > 0:
        waste_reasons.append("Thin content")

    if waste_reasons:
        page["classification"] = "Budget Waste"
        page["reasons"] = waste_reasons
        return "Budget Waste"

    low_value_reasons = []
    if page["low_content"]:
        low_value_reasons.append("Low content rate")
    if page["duplicate_title"]:
        low_value_reasons.append("Duplicate title")
    if not page["canonical"]:
        low_value_reasons.append("Missing canonical")
    if page["word_count"] > 0 and page["word_count"] < THIN_CONTENT_WORDS:
        low_value_reasons.append("Thin content")

    if low_value_reasons:
        page["classification"] = "Low Value"
        page["reasons"] = low_value_reasons
        return "Low Value"

    page["classification"] = "Healthy"
    page["reasons"] = []
    return "Healthy"


def classify_all(pages):
    """Classify all pages and return summary stats."""
    for p in pages:
        classify_page(p)

    waste = [p for p in pages if p["classification"] == "Budget Waste"]
    low_value = [p for p in pages if p["classification"] == "Low Value"]
    healthy = [p for p in pages if p["classification"] == "Healthy"]

    return {
        "total": len(pages),
        "waste_count": len(waste),
        "low_value_count": len(low_value),
        "healthy_count": len(healthy),
        "waste_pct": (len(waste) / len(pages) * 100) if pages else 0,
        "low_value_pct": (len(low_value) / len(pages) * 100) if pages else 0,
        "healthy_pct": (len(healthy) / len(pages) * 100) if pages else 0,
    }


# ── Waste breakdown ────────────────────────────────────────────────────────

def build_waste_breakdown(pages):
    """Count each waste reason across all pages."""
    reason_counts = {}
    for p in pages:
        for reason in p.get("reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    # Sort by count descending
    return sorted(reason_counts.items(), key=lambda x: -x[1])


# ── Recommendations ─────────────────────────────────────────────────────────

def generate_recommendations(stats, waste_breakdown, pages):
    """Generate actionable recommendations based on findings."""
    recs = []

    # 4xx/5xx errors
    error_pages = [p for p in pages if p["is_4xx"] or p["is_5xx"] or p["is_broken"]]
    if error_pages:
        recs.append({
            "priority": "Critical",
            "title": f"Fix {len(error_pages)} error pages ({len(error_pages)} pages)",
            "description": "Pages returning 4xx/5xx errors waste crawl budget and signal poor site quality. "
                          "Fix broken links, restore removed pages, or return 410 for permanently removed content.",
        })

    # Redirects
    redirect_pages = [p for p in pages if p["is_redirect"]]
    if redirect_pages:
        recs.append({
            "priority": "High",
            "title": f"Eliminate redirect chains ({len(redirect_pages)} pages)",
            "description": "Redirects consume crawl budget on each hop. Update internal links to point "
                          "directly to the final destination URL instead of chaining through redirects.",
        })

    # Thin content
    thin_pages = [p for p in pages if p["word_count"] > 0 and p["word_count"] < THIN_CONTENT_WORDS]
    if thin_pages:
        recs.append({
            "priority": "High",
            "title": f"Improve {len(thin_pages)} thin content pages",
            "description": f"Pages with fewer than {THIN_CONTENT_WORDS} words provide little value. "
                          "Expand content, merge similar pages, or add noindex to low-quality pages.",
        })

    # Large pages
    large_pages = [p for p in pages if p["large_page"]]
    if large_pages:
        recs.append({
            "priority": "Medium",
            "title": f"Optimize {len(large_pages)} oversized pages",
            "description": f"Pages larger than {LARGE_PAGE_BYTES // (1024*1024)} MB slow crawling and rendering. "
                          "Compress images, minify CSS/JS, and lazy-load below-the-fold resources.",
        })

    # Slow pages
    slow_pages = [p for p in pages if p["slow_page"]]
    if slow_pages:
        recs.append({
            "priority": "Medium",
            "title": f"Speed up {len(slow_pages)} slow pages",
            "description": f"Pages slower than {SLOW_PAGE_MS}ms reduce crawl efficiency. "
                          "Enable caching, use a CDN, and reduce server response time.",
        })

    # Duplicate titles
    dup_title_pages = [p for p in pages if p["duplicate_title"]]
    if dup_title_pages:
        recs.append({
            "priority": "Medium",
            "title": f"Fix {len(dup_title_pages)} pages with duplicate titles",
            "description": "Duplicate title tags confuse crawlers. Ensure each page has a unique, descriptive title.",
        })

    # Missing canonicals
    no_canonical = [p for p in pages if not p["canonical"] and p["status_code"] == 200]
    if no_canonical:
        recs.append({
            "priority": "Low",
            "title": f"Add canonical tags to {len(no_canonical)} pages",
            "description": "Missing canonical tags risk duplicate content issues. Add self-referencing canonicals "
                          "to all indexable pages.",
        })

    # Overall budget assessment
    if stats["waste_pct"] > 30:
        recs.append({
            "priority": "Critical",
            "title": "Crawl budget is significantly wasted",
            "description": f"{stats['waste_pct']:.0f}% of crawled pages are classified as Budget Waste. "
                          "Prioritize fixing errors, redirects, and thin content to recover crawl budget.",
        })
    elif stats["waste_pct"] > 15:
        recs.append({
            "priority": "High",
            "title": "Crawl budget needs optimization",
            "description": f"{stats['waste_pct']:.0f}% of pages waste crawl budget. Address the issues below "
                          "to improve crawl efficiency.",
        })
    elif stats["waste_pct"] <= 15 and stats["waste_count"] == 0 and stats["low_value_count"] == 0:
        recs.append({
            "priority": "Info",
            "title": "Crawl budget looks healthy",
            "description": "No significant crawl budget waste detected. Continue monitoring regularly.",
        })

    if not recs:
        recs.append({
            "priority": "Info",
            "title": "No major issues detected",
            "description": "All crawled pages appear healthy. Regular monitoring is recommended.",
        })

    return recs


# ── HTML Report Generation ─────────────────────────────────────────────────

def esc(text):
    """HTML-escape text."""
    return html_mod.escape(str(text)) if text else ""


def fmt_size(size_bytes):
    """Format bytes as human-readable string."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024*1024):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def generate_svg_distribution(stats):
    """Generate a simple SVG bar chart for page classification distribution."""
    total = stats["total"] or 1
    waste_h = (stats["waste_count"] / total) * 200
    low_val_h = (stats["low_value_count"] / total) * 200
    healthy_h = (stats["healthy_count"] / total) * 200

    return f"""
    <svg viewBox="0 0 400 260" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:400px;">
      <rect x="0" y="0" width="400" height="260" fill="#fff" rx="8"/>
      <!-- Bars -->
      <rect x="40" y="{230 - waste_h}" width="80" height="{waste_h}" fill="#e74c3c" rx="4"/>
      <rect x="160" y="{230 - low_val_h}" width="80" height="{low_val_h}" fill="#f39c12" rx="4"/>
      <rect x="280" y="{230 - healthy_h}" width="80" height="{healthy_h}" fill="#27ae60" rx="4"/>
      <!-- Baseline -->
      <line x1="20" y1="230" x2="380" y2="230" stroke="#ccc" stroke-width="1"/>
      <!-- Labels -->
      <text x="80" y="250" text-anchor="middle" font-size="12" fill="#1a1a1a">Budget Waste</text>
      <text x="200" y="250" text-anchor="middle" font-size="12" fill="#1a1a1a">Low Value</text>
      <text x="320" y="250" text-anchor="middle" font-size="12" fill="#1a1a1a">Healthy</text>
      <!-- Counts -->
      <text x="80" y="{225 - waste_h if waste_h > 20 else 225}" text-anchor="middle" font-size="14" font-weight="bold" fill="#1a1a1a">{stats['waste_count']}</text>
      <text x="200" y="{225 - low_val_h if low_val_h > 20 else 225}" text-anchor="middle" font-size="14" font-weight="bold" fill="#1a1a1a">{stats['low_value_count']}</text>
      <text x="320" y="{225 - healthy_h if healthy_h > 20 else 225}" text-anchor="middle" font-size="14" font-weight="bold" fill="#1a1a1a">{stats['healthy_count']}</text>
    </svg>"""


def generate_html_report(domain, pages, stats, waste_breakdown, recommendations, output_path):
    """Generate the full HTML report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Thin content list
    thin_pages = sorted(
        [p for p in pages if p["word_count"] > 0 and p["word_count"] < THIN_CONTENT_WORDS],
        key=lambda x: x["word_count"]
    )[:20]

    # Largest pages
    largest_pages = sorted(pages, key=lambda x: -x["page_size_bytes"])[:10]

    # Slowest pages
    slowest_pages = sorted(pages, key=lambda x: -x["load_time_ms"])[:10]

    # Waste breakdown rows
    waste_rows = ""
    if waste_breakdown:
        for reason, count in waste_breakdown:
            pct = (count / stats["total"] * 100) if stats["total"] else 0
            waste_rows += f"""
        <tr>
          <td>{esc(reason)}</td>
          <td style="text-align:center;">{count}</td>
          <td style="text-align:center;">{pct:.1f}%</td>
        </tr>"""
    else:
        waste_rows = '<tr><td colspan="3" style="text-align:center;color:#888;">No waste issues detected</td></tr>'

    # Thin content rows
    thin_rows = ""
    if thin_pages:
        for p in thin_pages:
            thin_rows += f"""
        <tr>
          <td><a href="{esc(p['url'])}" style="color:#0066cc;">{esc(p['url'])}</a></td>
          <td style="text-align:center;">{p['word_count']}</td>
          <td style="text-align:center;">{p['status_code']}</td>
        </tr>"""
    else:
        thin_rows = '<tr><td colspan="3" style="text-align:center;color:#888;">No thin content pages detected</td></tr>'

    # Largest pages rows
    largest_rows = ""
    for p in largest_pages:
        largest_rows += f"""
        <tr>
          <td><a href="{esc(p['url'])}" style="color:#0066cc;">{esc(p['url'])}</a></td>
          <td style="text-align:right;">{fmt_size(p['page_size_bytes'])}</td>
          <td style="text-align:center;">{p['word_count']}</td>
        </tr>"""

    # Slowest pages rows
    slowest_rows = ""
    for p in slowest_pages:
        slowest_rows += f"""
        <tr>
          <td><a href="{esc(p['url'])}" style="color:#0066cc;">{esc(p['url'])}</a></td>
          <td style="text-align:right;">{p['load_time_ms']} ms</td>
          <td style="text-align:center;">{p['status_code']}</td>
        </tr>"""

    # Recommendations
    rec_rows = ""
    priority_colors = {
        "Critical": "#e74c3c",
        "High": "#e67e22",
        "Medium": "#f39c12",
        "Low": "#3498db",
        "Info": "#27ae60",
    }
    for rec in recommendations:
        color = priority_colors.get(rec["priority"], "#888")
        rec_rows += f"""
        <div style="border-left:4px solid {color};padding:12px 16px;margin-bottom:12px;background:#f9f9f9;border-radius:0 4px 4px 0;">
          <div style="font-size:16px;font-weight:bold;color:#1a1a1a;">{esc(rec['title'])}</div>
          <div style="display:inline-block;font-size:11px;font-weight:bold;color:{color};text-transform:uppercase;margin:4px 0;">{esc(rec['priority'])}</div>
          <div style="font-size:14px;color:#555;line-height:1.5;">{esc(rec['description'])}</div>
        </div>"""

    svg_chart = generate_svg_distribution(stats)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex,nofollow">
  <title>Crawl Budget Analysis — {esc(domain)}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #fff; color: #1a1a1a; font-size: 16px; line-height: 1.6; }}
    .container {{ max-width: 960px; margin: 0 auto; padding: 32px 24px; }}
    h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; color: #1a1a1a; }}
    h2 {{ font-size: 20px; font-weight: 600; margin: 32px 0 12px; color: #1a1a1a; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
    .subtitle {{ font-size: 14px; color: #888; margin-bottom: 24px; }}
    .cards {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
    .card {{ flex: 1; min-width: 180px; padding: 20px; border-radius: 8px; text-align: center; }}
    .card .num {{ font-size: 32px; font-weight: 700; }}
    .card .label {{ font-size: 14px; margin-top: 4px; }}
    .card-waste {{ background: #fdf2f2; border: 1px solid #e74c3c; }}
    .card-waste .num {{ color: #e74c3c; }}
    .card-low {{ background: #fff8f0; border: 1px solid #f39c12; }}
    .card-low .num {{ color: #f39c12; }}
    .card-healthy {{ background: #f0faf4; border: 1px solid #27ae60; }}
    .card-healthy .num {{ color: #27ae60; }}
    .card-total {{ background: #f5f5f5; border: 1px solid #ddd; }}
    .card-total .num {{ color: #1a1a1a; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
    th {{ text-align: left; padding: 10px 12px; background: #f5f5f5; font-size: 14px; font-weight: 600; border-bottom: 2px solid #ddd; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 14px; }}
    tr:hover {{ background: #fafafa; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }}
    @media (max-width: 700px) {{ .grid {{ grid-template-columns: 1fr; }} .cards {{ flex-direction: column; }} }}
    .meta {{ font-size: 13px; color: #aaa; margin-top: 40px; padding-top: 16px; border-top: 1px solid #eee; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Crawl Budget Analysis</h1>
    <div class="subtitle">{esc(domain)} · Generated {now} · v{SCRIPT_VERSION} · DataForSEO on_page/instant_pages</div>

    <!-- Summary Cards -->
    <div class="cards">
      <div class="card card-total">
        <div class="num">{stats['total']}</div>
        <div class="label">Total Pages Analyzed</div>
      </div>
      <div class="card card-waste">
        <div class="num">{stats['waste_count']}</div>
        <div class="label">Budget Waste ({stats['waste_pct']:.0f}%)</div>
      </div>
      <div class="card card-low">
        <div class="num">{stats['low_value_count']}</div>
        <div class="label">Low Value ({stats['low_value_pct']:.0f}%)</div>
      </div>
      <div class="card card-healthy">
        <div class="num">{stats['healthy_count']}</div>
        <div class="label">Healthy ({stats['healthy_pct']:.0f}%)</div>
      </div>
    </div>

    <!-- SVG Distribution -->
    <h2>Page Distribution</h2>
    {svg_chart}

    <!-- Waste Breakdown -->
    <h2>Waste Breakdown</h2>
    <table>
      <thead>
        <tr><th>Waste Reason</th><th style="text-align:center;">Pages</th><th style="text-align:center;">% of Total</th></tr>
      </thead>
      <tbody>
        {waste_rows}
      </tbody>
    </table>

    <!-- Thin Content & Distribution Grid -->
    <h2>Thin Content Pages (&lt; {THIN_CONTENT_WORDS} words)</h2>
    <table>
      <thead>
        <tr><th>URL</th><th style="text-align:center;">Words</th><th style="text-align:center;">Status</th></tr>
      </thead>
      <tbody>
        {thin_rows}
      </tbody>
    </table>

    <!-- Largest & Slowest Pages Grid -->
    <div class="grid">
      <div>
        <h2>Largest Pages</h2>
        <table>
          <thead>
            <tr><th>URL</th><th style="text-align:right;">Size</th><th style="text-align:center;">Words</th></tr>
          </thead>
          <tbody>
            {largest_rows}
          </tbody>
        </table>
      </div>
      <div>
        <h2>Slowest Pages</h2>
        <table>
          <thead>
            <tr><th>URL</th><th style="text-align:right;">Load Time</th><th style="text-align:center;">Status</th></tr>
          </thead>
          <tbody>
            {slowest_rows}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Recommendations -->
    <h2>Recommendations</h2>
    {rec_rows}

    <div class="meta">
      Generated by <strong>Crawl Budget Analyzer</strong> v{SCRIPT_VERSION} ·
      Powered by <a href="https://dataforseo.com">DataForSEO</a> on_page/instant_pages API ·
      <meta name="robots" content="noindex,nofollow">
    </div>
  </div>
</body>
</html>"""

    # Write report
    report_file = os.path.join(output_path, "index.html")
    os.makedirs(output_path, exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    return report_file


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze crawl budget waste for a domain")
    parser.add_argument("--domain", required=True, help="Domain to analyze (e.g. example.com)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max pages to analyze")
    parser.add_argument("--output", default="/tmp/crawl_budget_report", help="Output directory for report")
    args = parser.parse_args()

    root_url, domain = get_domain_root(args.domain)
    limit = args.limit
    output_path = args.output

    print(f"\n{'='*70}")
    print(f"  ZENFUSION CRAWL BUDGET ANALYZER v{SCRIPT_VERSION}")
    print(f"{'='*70}")
    print(f"  Domain: {domain}")
    print(f"  Max pages: {limit}")
    print(f"  Output: {output_path}")
    print(f"{'='*70}\n")

    # 1. Discover URLs
    urls = discover_urls(root_url, domain, limit)
    if not urls:
        print("ERROR: No URLs discovered. Check if the domain has a sitemap.", file=sys.stderr)
        sys.exit(1)

    # 2. Analyze pages via DataForSEO
    login, password, _ = get_credentials()
    print(f"\n  Analyzing {len(urls)} pages via DataForSEO instant_pages...\n")
    pages = analyze_pages(urls, login, password)

    # 3. Classify pages
    stats = classify_all(pages)
    waste_breakdown = build_waste_breakdown(pages)
    recommendations = generate_recommendations(stats, waste_breakdown, pages)

    print(f"\n  Results: {stats['waste_count']} waste, {stats['low_value_count']} low value, {stats['healthy_count']} healthy")

    # 4. Generate report
    report_file = generate_html_report(domain, pages, stats, waste_breakdown, recommendations, output_path)
    print(f"\n  Report saved to: {report_file}")
    print(f"\n{'='*70}\n")

    # Output JSON summary for automation
    summary = {
        "domain": domain,
        "total_pages": stats["total"],
        "waste_count": stats["waste_count"],
        "low_value_count": stats["low_value_count"],
        "healthy_count": stats["healthy_count"],
        "report_path": report_file,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
