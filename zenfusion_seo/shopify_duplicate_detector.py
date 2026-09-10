#!/usr/bin/env python3
"""
Shopify Duplicate URL Detector & Canonical Chain Mapper

Finds Shopify-generated duplicate URLs and maps canonical chains.
Functions:
  1. crawl_shopify_urls(domain, limit=200) — crawl domain, classify by Shopify page type
  2. find_duplicate_groups(urls) — group URLs pointing to the same content, identify canonical
  3. check_canonicalization(urls, groups) — fetch canonical tags, detect leaks
  4. generate_duplicate_report(...) — HTML report with summary, SVG chart, tables, recommendations

Usage:
  python3 shopify_duplicate_detector.py --domain softprowatersystems.com --limit 50 --output /tmp/test_shopify_dups

Credentials in ~/.hermes/.env:
  SCRAPEOWL_API_KEY (fallback for blocked pages)
  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (unused here but loaded)
"""

import os
import sys
import re
import json
import time
import argparse
import hashlib
import html as html_mod
import urllib.request
import urllib.parse
import urllib.error
import ssl
from datetime import datetime
from urllib.parse import urlparse, urldefrag, parse_qs, urlencode
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
USER_AGENT = "Mozilla/5.0 (compatible; ZenfusionShopifyDupBot/1.0)"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Env ─────────────────────────────────────────────────────────────────────

def load_env():
    env = {}
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env

ENV = load_env()

# ── HTTP fetch ──────────────────────────────────────────────────────────────

def fetch_url(url, timeout=20):
    """Fetch a URL and return (status_code, text)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception:
            return e.code, ""
    except Exception:
        return 0, ""


def fetch_page(url):
    """Fetch page HTML — direct first, ScrapeOwl fallback."""
    code, text = fetch_url(url)
    if code == 200 and text:
        return text, code
    api_key = ENV.get("SCRAPEOWL_API_KEY", "")
    if api_key:
        try:
            payload = json.dumps({"api_key": api_key, "url": url}).encode()
            req = urllib.request.Request(
                "https://api.scrapeowl.com/v1/",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                return data.get("data", ""), 200
        except Exception:
            pass
    return text or "", code

# ── URL normalization & classification ──────────────────────────────────────

def normalize_domain(domain):
    domain = domain.strip()
    if domain.startswith("http://") or domain.startswith("https://"):
        p = urlparse(domain)
        return p.netloc, f"{p.scheme}://{p.netloc}"
    return domain, f"https://{domain}"


def clean_url(url):
    """Strip fragment, normalize trailing slash for non-file URLs."""
    url, _ = urldefrag(url)
    # keep query for classification but normalize
    return url


def classify_shopify_url(path, query):
    """Classify a Shopify URL by page type."""
    p = path.lower().rstrip("/")
    q = (query or "").lower()
    # Order matters
    if p.startswith("/products/"):
        if "variant=" in q:
            return "variant"
        return "product"
    if "/collections/" in p and "/products/" in p:
        return "collection-product"
    if p.startswith("/collections/"):
        if "page=" in q or "page=" in (query or ""):
            return "pagination"
        if any(k in q for k in ("pr_prod_strat=", "view=", "sort_by=", "filter_", "?q=", "constraint=")):
            return "faceted"
        return "collection"
    if p.startswith("/pages/"):
        return "page"
    if p.startswith("/blogs/"):
        return "blog"
    if p == "/cart" or p.startswith("/cart/"):
        return "cart"
    if p == "/checkout" or p.startswith("/checkout"):
        return "checkout"
    if p == "/search" or p.startswith("/search"):
        return "search"
    if p.startswith("/account") or p.startswith("/login") or p.startswith("/register"):
        return "account"
    # parameter-only duplicate detection on any path
    if any(k in q for k in ("variant=", "pr_prod_strat=", "view=", "sort_by=", "?q=")):
        return "faceted"
    return "other"


def canonical_for_url(url, ptype, path, query):
    """Return the expected canonical URL for a given Shopify URL."""
    qs = parse_qs(query)
    if ptype == "collection-product":
        # /collections/{coll}/products/{handle} -> /products/{handle}
        m = re.match(r"^/collections/[^/]+/products/([^/?#]+)", path, re.I)
        if m:
            return urldefrag(rebuild_url(url, f"/products/{m.group(1)}", ""))[0]
    if ptype == "variant":
        m = re.match(r"^/products/([^/?#]+)", path, re.I)
        if m:
            return urldefrag(rebuild_url(url, f"/products/{m.group(1)}", ""))[0]
    if ptype == "pagination":
        # pagination of a collection — canonical is the base collection (page 1)
        m = re.match(r"^/collections/([^/?#]+)", path, re.I)
        if m:
            return urldefrag(rebuild_url(url, f"/collections/{m.group(1)}", ""))[0]
    if ptype == "faceted":
        # faceted/parameter URL — canonical is the URL with the params stripped
        base = url.split("?")[0]
        return urldefrag(base)[0]
    return urldefrag(url)[0]


def rebuild_url(src_url, new_path, new_query):
    """Replace path/query of src_url with new_path/new_query."""
    p = urlparse(src_url)
    return urllib.parse.urlunparse((p.scheme, p.netloc, new_path, "", new_query, ""))


def product_handle_from_url(url):
    m = re.search(r"/products/([^/?#]+)", url, re.I)
    return m.group(1) if m else None


def collection_handle_from_url(url):
    m = re.search(r"/collections/([^/?#]+)/products/", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/collections/([^/?#]+)", url, re.I)
    return m.group(1) if m else None

# ── Mini link extractor (HTMLParser-based, no deps) ──────────────────────────

class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._in_a = False
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            self._href = d.get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append(self._href)
            self._href = None


def extract_links(html_text, base_url):
    """Extract all href links from HTML, resolved against base_url."""
    links = set()
    parser = LinkExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    for href in getattr(parser, "links", []):
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        abs_url = urllib.parse.urljoin(base_url, href)
        abs_url, _ = urldefrag(abs_url)
        links.add(abs_url)
    return links


def extract_canonical(html_text, base_url):
    """Extract the <link rel=canonical href> value, resolved to absolute."""
    m = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        html_text, re.I,
    )
    if not m:
        m = re.search(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
            html_text, re.I,
        )
    if not m:
        return None
    href = m.group(1).strip()
    return urllib.parse.urljoin(base_url, href)

# ── Sitemap discovery ───────────────────────────────────────────────────────

def fetch_sitemap_urls(base_url, domain):
    """Try sitemap.xml and robots.txt to gather URLs."""
    urls = set()
    candidates = [
        f"{base_url}/sitemap.xml",
        f"{base_url}/sitemap.xml.gz",
        f"{base_url}/sitemap_products.xml",
        f"{base_url}/sitemap_collections.xml",
        f"{base_url}/sitemap_pages.xml",
        f"{base_url}/sitemap_1.xml",
    ]
    # robots.txt -> Sitemap directives
    code, txt = fetch_url(f"{base_url}/robots.txt")
    if code == 200 and txt:
        for line in txt.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
    for sm in candidates:
        sc, st = fetch_url(sm)
        if sc == 200 and st:
            try:
                root = ET.fromstring(st)
            except ET.ParseError:
                continue
            # handle namespaces
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            for loc in root.iter(f"{ns}loc") if ns else root.iter("loc"):
                if loc.text:
                    u = loc.text.strip()
                    if domain in u or urlparse(u).netloc == domain:
                        urls.add(urldefrag(u)[0])
    return urls

# ── Function 1: crawl_shopify_urls ──────────────────────────────────────────

def crawl_shopify_urls(domain, limit=200):
    """Crawl a Shopify domain, extract and classify URLs.

    Returns list of dicts: {url, type, path, query, status}
    """
    domain, base_url = normalize_domain(domain)
    print(f"[*] Crawling {domain} (limit {limit})")

    # 1) Gather seed URLs from sitemaps
    seed_urls = fetch_sitemap_urls(base_url, domain)
    print(f"[*] Sitemap yielded {len(seed_urls)} URLs")

    # 2) Always include the homepage and /collections/all as crawl seeds
    seed_urls.add(base_url + "/")
    seed_urls.add(base_url + "/collections/all")

    # 3) BFS crawl with limit
    seen = set()
    classified = []
    queue = sorted(seed_urls)
    crawled = 0
    while queue and len(classified) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        if parsed.netloc != domain and parsed.netloc != domain.replace("www.", "") and \
           parsed.netloc.replace("www.", "") != domain.replace("www.", ""):
            continue
        # skip non-http
        if parsed.scheme not in ("http", "https"):
            continue
        ptype = classify_shopify_url(parsed.path, parsed.query)
        classified.append({
            "url": url,
            "type": ptype,
            "path": parsed.path,
            "query": parsed.query,
            "status": 0,
        })
        crawled += 1
        if crawled % 10 == 0:
            print(f"[*] Collected {len(classified)} URLs ({crawled} fetched)")
        if len(classified) >= limit:
            break
        # fetch homepage to discover more links (limited depth)
        if ptype in ("product", "collection", "page", "other") and crawled <= 30:
            code, text = fetch_page(url)
            if code == 200 and text:
                for link in extract_links(text, url):
                    lp = urlparse(link)
                    if lp.netloc == domain or lp.netloc == domain.replace("www.", ""):
                        if link not in seen and len(classified) < limit:
                            queue.append(link)

    print(f"[*] Total classified URLs: {len(classified)}")
    return classified

# ── Function 2: find_duplicate_groups ──────────────────────────────────────

def find_duplicate_groups(urls):
    """Group URLs pointing to the same content.

    Returns list of groups:
      {key, type, canonical_url, urls:[...], duplicate_count}
    """
    groups = {}
    for u in urls:
        ptype = u["type"]
        path = u["path"]
        query = u["query"]
        full = u["url"]

        if ptype == "product":
            key = ("product", path.lower().rstrip("/"))
        elif ptype == "collection-product":
            handle = product_handle_from_url(full)
            key = ("product", f"/products/{handle}") if handle else ("cp", full.lower())
        elif ptype == "variant":
            handle = product_handle_from_url(full)
            key = ("product", f"/products/{handle}") if handle else ("variant", full.lower())
        elif ptype == "collection":
            coll = collection_handle_from_url(full) or path.lower().rstrip("/")
            key = ("collection", f"/collections/{coll}")
        elif ptype == "pagination":
            coll = collection_handle_from_url(full) or path.lower().rstrip("/")
            key = ("collection", f"/collections/{coll}")
        elif ptype == "faceted":
            base = full.split("?")[0].lower().rstrip("/")
            # group with the underlying product or collection page
            if "/products/" in path:
                handle = product_handle_from_url(full)
                key = ("product", f"/products/{handle}") if handle else ("faceted", base)
            elif "/collections/" in path:
                coll = collection_handle_from_url(full) or path.lower().rstrip("/")
                key = ("collection", f"/collections/{coll}")
            else:
                key = ("faceted", base)
        else:
            # unique page — its own group
            key = ("unique", full.lower())

        g = groups.setdefault(key, {
            "key": key,
            "type": ptype,
            "urls": [],
            "canonical_url": canonical_for_url(full, ptype, path, query),
        })
        g["urls"].append(full)

    result = []
    for key, g in groups.items():
        g["duplicate_count"] = len(g["urls"])
        g["is_duplicate_group"] = len(g["urls"]) > 1
        # ensure canonical_url is consistent
        result.append(g)

    # sort: duplicate groups first, then by size
    result.sort(key=lambda x: (-int(x["is_duplicate_group"]), -x["duplicate_count"]))
    return result

# ── Function 3: check_canonicalization ──────────────────────────────────────

def check_canonicalization(urls, groups):
    """For each duplicate group, fetch canonical tags and detect leaks.

    Returns dict:
      {checked, correct, leaking, missing, issues: [...]}
    """
    issues = []
    checked = 0
    correct = 0
    leaking = 0
    missing = 0

    # Only check duplicate groups to conserve API calls
    dup_groups = [g for g in groups if g["is_duplicate_group"]]
    print(f"[*] Checking canonicalization for {len(dup_groups)} duplicate groups")

    for i, g in enumerate(dup_groups):
        expected_canonical = g["canonical_url"]
        for url in g["urls"]:
            checked += 1
            code, text = fetch_page(url)
            actual_canonical = extract_canonical(text, url) if text else None

            if actual_canonical is None:
                missing += 1
                issues.append({
                    "url": url,
                    "group_type": g["type"],
                    "expected_canonical": expected_canonical,
                    "actual_canonical": "",
                    "status": "missing",
                    "message": "Canonical tag missing",
                })
            elif normalize_url_cmp(actual_canonical) == normalize_url_cmp(expected_canonical):
                correct += 1
                issues.append({
                    "url": url,
                    "group_type": g["type"],
                    "expected_canonical": expected_canonical,
                    "actual_canonical": actual_canonical,
                    "status": "correct",
                    "message": "Canonical matches expected",
                })
            else:
                leaking += 1
                issues.append({
                    "url": url,
                    "group_type": g["type"],
                    "expected_canonical": expected_canonical,
                    "actual_canonical": actual_canonical,
                    "status": "leak",
                    "message": f"Canonical points to {actual_canonical} (expected {expected_canonical})",
                })

            if (i + 1) % 5 == 0:
                print(f"[*] Checked {checked} URLs")
            time.sleep(0.3)  # polite

    return {
        "checked": checked,
        "correct": correct,
        "leaking": leaking,
        "missing": missing,
        "issues": issues,
    }


def normalize_url_cmp(url):
    """Normalize URL for comparison (strip trailing slash, lowercase host, strip query for canonical compare)."""
    if not url:
        return ""
    p = urlparse(url)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc.lower()}{path}".rstrip("/")

# ── Function 4: generate_duplicate_report ───────────────────────────────────

def generate_duplicate_report(domain, urls, groups, canonical_issues, output_path):
    """Generate an HTML report with summary cards, SVG chart, and tables."""
    os.makedirs(output_path, exist_ok=True) if not os.path.exists(output_path) else None
    if os.path.isdir(output_path) or output_path.endswith("/"):
        report_file = os.path.join(output_path, "shopify_duplicates_report.html")
    else:
        report_file = output_path
        os.makedirs(os.path.dirname(report_file), exist_ok=True)

    total_urls = len(urls)
    type_counts = {}
    for u in urls:
        type_counts[u["type"]] = type_counts.get(u["type"], 0) + 1

    dup_groups = [g for g in groups if g["is_duplicate_group"]]
    dup_urls = sum(g["duplicate_count"] for g in dup_groups)
    unique_pages = total_urls - sum(g["duplicate_count"] - 1 for g in dup_groups)
    dup_pct = round((dup_urls / total_urls * 100) if total_urls else 0, 1)

    correct = canonical_issues.get("correct", 0)
    leaks = canonical_issues.get("leaking", 0)
    missing = canonical_issues.get("missing", 0)

    # Distribution chart data (by duplicate group type)
    dist = {}
    for g in dup_groups:
        dist[g["type"]] = dist.get(g["type"], 0) + 1

    report = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="robots" content="noindex,nofollow">
<title>Shopify Duplicate URL Report — {html_mod.escape(domain)}</title>
<style>
  body {{ background:#fff; color:#1a1a1a; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:16px; margin:0; padding:24px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  h2 {{ font-size:20px; margin:28px 0 12px; border-bottom:1px solid #eee; padding-bottom:6px; }}
  a {{ color:#0066cc; }}
  .meta {{ color:#666; font-size:14px; margin-bottom:20px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }}
  .card {{ border:1px solid #e0e0e0; border-radius:8px; padding:16px 20px; min-width:160px; background:#fafafa; }}
  .card .num {{ font-size:28px; font-weight:700; }}
  .card .lbl {{ font-size:13px; color:#555; margin-top:4px; }}
  .card.leak .num {{ color:#c0392b; }}
  .card.ok .num {{ color:#27ae60; }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }}
  th,td {{ border:1px solid #ddd; padding:8px 10px; text-align:left; vertical-align:top; }}
  th {{ background:#f4f4f4; font-weight:600; }}
  tr:hover {{ background:#fafafa; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }}
  .badge.correct {{ background:#e8f5e9; color:#2e7d32; }}
  .badge.leak {{ background:#fbe9e7; color:#c62828; }}
  .badge.missing {{ background:#fff3e0; color:#e65100; }}
  .group-row {{ cursor:pointer; }}
  .group-row .toggle {{ color:#0066cc; font-weight:600; }}
  .dup-urls {{ display:none; }}
  .dup-urls.open {{ display:table-row; background:#fbfbfb; }}
  .url {{ word-break:break-all; font-family:monospace; font-size:12px; }}
  svg {{ max-width:600px; }}
  .rec {{ background:#f7fbff; border-left:3px solid #0066cc; padding:12px 16px; margin:10px 0; border-radius:4px; }}
  .rec code {{ background:#eef; padding:1px 4px; border-radius:3px; font-size:13px; }}
  .note {{ font-size:13px; color:#777; }}
</style>
</head>
<body>
<h1>Shopify Duplicate URL Report</h1>
<div class="meta">{html_mod.escape(domain)} · generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · v{SCRIPT_VERSION}</div>

<h2>Summary</h2>
<div class="cards">
  <div class="card"><div class="num">{total_urls}</div><div class="lbl">Total URLs</div></div>
  <div class="card"><div class="num">{unique_pages}</div><div class="lbl">Unique Pages</div></div>
  <div class="card"><div class="num">{dup_urls}</div><div class="lbl">Duplicate URLs</div></div>
  <div class="card"><div class="num">{dup_pct}%</div><div class="lbl">Duplicate %</div></div>
  <div class="card ok"><div class="num">{correct}</div><div class="lbl">Canonicalized Correctly</div></div>
  <div class="card leak"><div class="num">{leaks + missing}</div><div class="lbl">Canonical Leaks</div></div>
</div>

<h2>Duplicate Distribution by Type</h2>
{render_dist_chart(dist)}

<h2>Duplicate Groups</h2>
{render_groups_table(dup_groups, canonical_issues)}

<h2>Canonical Leaks</h2>
{render_leak_table(canonical_issues)}

<h2>Shopify-Specific Recommendations</h2>
{render_recommendations(dist, canonical_issues)}

<div class="note">Report generated by shopify_duplicate_detector.py v{SCRIPT_VERSION}. noindex/nofollow.</div>

<script>
function toggleGroup(id) {{
  var el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}}
</script>
</body>
</html>
"""
    with open(report_file, "w") as f:
        f.write(report)
    print(f"[*] Report written to {report_file}")
    return report_file


def render_dist_chart(dist):
    """Render a horizontal bar chart in SVG."""
    if not dist:
        return "<p>No duplicate groups found.</p>"
    colors = {
        "collection-product": "#0066cc",
        "variant": "#9b59b6",
        "pagination": "#e67e22",
        "faceted": "#27ae60",
        "product": "#34495e",
        "collection": "#16a085",
    }
    max_val = max(dist.values())
    bar_h = 32
    gap = 12
    width = 500
    label_w = 140
    total_h = len(dist) * (bar_h + gap) + 30
    parts = [f'<svg width="{width + 100}" height="{total_h}" xmlns="http://www.w3.org/2000/svg">']
    y = 10
    for t, v in sorted(dist.items(), key=lambda x: -x[1]):
        bar_w = int((v / max_val) * width) if max_val else 0
        color = colors.get(t, "#888")
        parts.append(f'<text x="0" y="{y + 20}" font-size="13" fill="#1a1a1a">{html_mod.escape(t)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{bar_w}" height="{bar_h}" fill="{color}" rx="3"/>')
        parts.append(f'<text x="{label_w + bar_w + 8}" y="{y + 20}" font-size="13" fill="#333">{v}</text>')
        y += bar_h + gap
    parts.append("</svg>")
    return "\n".join(parts)


def render_groups_table(dup_groups, canonical_issues):
    """Render expandable duplicate groups table."""
    issue_map = {(i["url"]): i for i in canonical_issues.get("issues", [])}
    rows = []
    for i, g in enumerate(dup_groups[:50]):  # cap at 50 for page weight
        gid = f"grp-{i}"
        canonical = html_mod.escape(g["canonical_url"])
        dup_list = "<br>".join(
            f'<span class="url">{html_mod.escape(u)}</span>' for u in g["urls"]
        )
        # determine overall status
        statuses = [issue_map.get(u, {}).get("status", "unchecked") for u in g["urls"]]
        if "leak" in statuses:
            badge = '<span class="badge leak">Leak</span>'
        elif "missing" in statuses:
            badge = '<span class="badge missing">Missing</span>'
        elif "correct" in statuses and not statuses.count("unchecked") == len(statuses):
            badge = '<span class="badge correct">Correct</span>'
        else:
            badge = '<span class="badge" style="background:#eee;color:#555">Unchecked</span>'
        rows.append(f"""
<tr class="group-row" onclick="toggleGroup('{gid}')">
  <td><span class="toggle">▶</span></td>
  <td><span class="url">{canonical}</span></td>
  <td>{html_mod.escape(g['type'])}</td>
  <td>{g['duplicate_count']}</td>
  <td>{badge}</td>
</tr>
<tr class="dup-urls" id="{gid}">
  <td colspan="5"><strong>Duplicate URLs:</strong><br>{dup_list}</td>
</tr>""")
    if not rows:
        return "<p>No duplicate groups detected.</p>"
    return """<table>
<tr><th></th><th>Canonical URL</th><th>Type</th><th># Duplicates</th><th>Status</th></tr>
""" + "\n".join(rows) + "</table>"


def render_leak_table(canonical_issues):
    """Render table of URLs where canonical is wrong/missing."""
    issues = [i for i in canonical_issues.get("issues", []) if i["status"] in ("leak", "missing")]
    if not issues:
        return "<p>No canonical leaks detected. ✅</p>"
    rows = []
    for i in issues:
        badge = f'<span class="badge {i["status"]}">{i["status"].title()}</span>'
        rows.append(f"""
<tr>
  <td><span class="url">{html_mod.escape(i['url'])}</span></td>
  <td>{html_mod.escape(i['group_type'])}</td>
  <td>{badge}</td>
  <td><span class="url">{html_mod.escape(i['expected_canonical'])}</span></td>
  <td><span class="url">{html_mod.escape(i['actual_canonical'])}</span></td>
  <td>{html_mod.escape(i['message'])}</td>
</tr>""")
    return """<table>
<tr><th>URL</th><th>Type</th><th>Status</th><th>Expected Canonical</th><th>Actual Canonical</th><th>Issue</th></tr>
""" + "\n".join(rows) + "</table>"


def render_recommendations(dist, canonical_issues):
    """Shopify-specific recommendations based on findings."""
    recs = []
    leaks = canonical_issues.get("leaking", 0)
    missing = canonical_issues.get("missing", 0)

    if dist.get("collection-product", 0) > 0:
        recs.append(
            "<div class='rec'><strong>Block collection-product paths in robots.txt</strong><br>"
            "Product pages accessible via <code>/collections/*/products/*</code> create duplicates. "
            "Add to robots.txt:<br><code>Disallow: /collections/*/products/*</code><br>"
            "Or set canonical in theme to <code>/products/{handle}</code> in "
            "<code>sections/product-template.liquid</code> or <code>theme.liquid</code>.</div>"
        )
    if dist.get("variant", 0) > 0:
        recs.append(
            "<div class='rec'><strong>Canonicalize variant URLs</strong><br>"
            "URLs with <code>?variant=</code> should canonicalize to the base product URL. "
            "In your theme, ensure the canonical tag ignores the variant parameter, or "
            "add to robots.txt: <code>Disallow: /*?variant=</code>.</div>"
        )
    if dist.get("pagination", 0) > 0:
        recs.append(
            "<div class='rec'><strong>Handle pagination canonically</strong><br>"
            "Paginated collection URLs (<code>?page=N</code>) should either self-canonicalize "
            "each page or canonicalize to page 1. Add <code>Disallow: /*?page=</code> to "
            "robots.txt if paginated pages are thin. Prefer self-canonical with rel=next/prev.</div>"
        )
    if dist.get("faceted", 0) > 0:
        recs.append(
            "<div class='rec'><strong>Block faceted/parameter URLs</strong><br>"
            "Faceted navigation URLs (<code>?pr_prod_strat=</code>, <code>?view=</code>, "
            "<code>?sort_by=</code>, <code>?filter_*</code>) create duplicates. "
            "Add to robots.txt:<br>"
            "<code>Disallow: /*?pr_prod_strat=</code><br>"
            "<code>Disallow: /*?view=</code><br>"
            "<code>Disallow: /*?sort_by=</code><br>"
            "<code>Disallow: /*?filter_*</code></div>"
        )
    if leaks > 0:
        recs.append(
            f"<div class='rec'><strong>Fix {leaks} canonical leak(s) in theme</strong><br>"
            "Some pages have a canonical tag pointing to the wrong URL. Inspect "
            "<code>theme.liquid</code> and product/collection templates to ensure "
            "the canonical tag is computed correctly. For product pages, use:<br>"
            "<code>&lt;link rel=\"canonical\" href=\"{{{{ product.url | absolute_url }}}}\"&gt;</code><br>"
            "NOT the full request URL including collection path.</div>"
        )
    if missing > 0:
        recs.append(
            f"<div class='rec'><strong>{missing} page(s) missing canonical tag</strong><br>"
            "Add a canonical tag in <code>theme.liquid</code> head for all page types.</div>"
        )
    if not recs:
        recs.append("<div class='rec'><strong>No major duplicate issues detected.</strong> "
                    "Keep monitoring sitemap and canonical tags regularly.</div>")
    return "\n".join(recs)

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Shopify Duplicate URL Detector")
    ap.add_argument("--domain", required=True, help="Shopify domain (e.g. softprowatersystems.com)")
    ap.add_argument("--limit", type=int, default=200, help="Max URLs to crawl (default 200)")
    ap.add_argument("--output", default="/tmp/shopify_dups", help="Output dir or HTML file path")
    ap.add_argument("--skip-canonical-check", action="store_true",
                    help="Skip fetching canonical tags (faster)")
    args = ap.parse_args()

    domain = args.domain
    print(f"=== Shopify Duplicate Detector — {domain} ===")

    # 1. Crawl
    urls = crawl_shopify_urls(domain, limit=args.limit)
    print(f"\n[*] Found {len(urls)} URLs")

    # 2. Group duplicates
    groups = find_duplicate_groups(urls)
    dup_count = sum(1 for g in groups if g["is_duplicate_group"])
    print(f"[*] {dup_count} duplicate groups, {sum(g['duplicate_count'] for g in groups if g['is_duplicate_group'])} duplicate URLs")

    # 3. Check canonicalization
    if args.skip_canonical_check:
        canonical_issues = {"checked": 0, "correct": 0, "leaking": 0, "missing": 0, "issues": []}
    else:
        canonical_issues = check_canonicalization(urls, groups)

    # 4. Generate report
    report_path = generate_duplicate_report(domain, urls, groups, canonical_issues, args.output)

    # Summary to stdout
    print(f"\n=== Summary ===")
    print(f"Total URLs: {len(urls)}")
    print(f"Duplicate groups: {dup_count}")
    print(f"Canonical checked: {canonical_issues['checked']}")
    print(f"Canonical correct: {canonical_issues['correct']}")
    print(f"Canonical leaks: {canonical_issues['leaking']}")
    print(f"Canonical missing: {canonical_issues['missing']}")
    print(f"Report: {report_path}")

    # JSON summary for automation
    summary = {
        "domain": domain,
        "total_urls": len(urls),
        "duplicate_groups": dup_count,
        "canonical_issues": canonical_issues["leaking"] + canonical_issues["missing"],
        "report_path": report_path,
    }
    summary_path = os.path.join(os.path.dirname(report_path), "summary.json") \
        if os.path.dirname(report_path) else "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary JSON: {summary_path}")


if __name__ == "__main__":
    main()
