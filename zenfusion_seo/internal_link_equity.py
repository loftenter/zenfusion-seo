#!/usr/bin/env python3
"""
Internal Link Equity Analyzer
=============================
Crawls a domain's pages (via ScrapeOwl + direct HTTP fallback), extracts all
internal links, builds a directed link graph, and computes link-equity metrics:

  - incoming / outgoing internal link counts per page
  - link equity score (incoming links / total pages)
  - hub pages (many outgoing), authority pages (many incoming)
  - orphan pages (no incoming), dead-end pages (no outgoing)
  - crawl depth from homepage (BFS)
  - link gap recommendations (high-value pages under-linked)

Generates a white-SaaS-themed HTML report with summary cards, top
authority/hub tables (expandable), orphan list, dead-end list, an SVG
histogram of incoming-link counts, crawl-depth analysis, and specific
"link from A to B because…" recommendations.

Usage:
  python3 internal_link_equity.py --domain softprowatersystems.com \\
      --limit 50 --output /tmp/test_link_equity

Environment:
  SCRAPEOWL_API_KEY  — in ~/.hermes/.env
"""

import os
import re
import sys
import ssl
import json
import argparse
import urllib.request
from html import escape as html_escape
from urllib.parse import urlparse, urljoin, urldefrag
from html.parser import HTMLParser
from datetime import datetime
from collections import deque, defaultdict

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
REQUEST_TIMEOUT = 60
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# High-value page type keywords (path-based detection)
HIGH_VALUE_PATTERNS = {
    "product":   ["/products/", "/product/", "/item/"],
    "collection":["/collections/", "/collection/", "/category/", "/catalog/"],
    "blog":      ["/blog/", "/blogs/", "/articles/", "/post/", "/posts/", "/news/"],
    "page":      ["/pages/"],
}

# ── Credentials / env ────────────────────────────────────────────────────────

def load_env():
    """Load environment variables from ~/.hermes/.env into a dict."""
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


ENV = load_env()
SCRAPEOWL_KEY = ENV.get("SCRAPEOWL_API_KEY") or os.environ.get("SCRAPEOWL_API_KEY", "")


# ── Page fetching (ScrapeOwl primary, direct HTTP fallback) ────────────────

def _scrapeowl_fetch(url, render_js=True):
    """Fetch a page via ScrapeOwl. Returns (html, status) or (None, 0)."""
    if not SCRAPEOWL_KEY:
        return None, 0
    payload = json.dumps({
        "api_key": SCRAPEOWL_KEY,
        "url": url,
        "render_js": render_js,
    }).encode()
    req = urllib.request.Request(
        "https://api.scrapeowl.com/v1/scrape",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            html = (data.get("html") or data.get("body")
                    or data.get("content") or data.get("data") or "")
            return html, 200 if html else 0
    except Exception as e:
        print(f"  [scrapeowl] {url} failed: {e}", file=sys.stderr)
        return None, 0


def _direct_fetch(url):
    """Fetch a page directly via urllib. Returns (html, status) or (None, 0)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return raw, resp.status
    except Exception as e:
        print(f"  [direct] {url} failed: {e}", file=sys.stderr)
        return None, 0


def fetch_page_html(url):
    """Fetch HTML for a URL — ScrapeOwl first (JS-rendered), direct HTTP fallback."""
    # 1) ScrapeOwl with JS rendering (best for Shopify / JS-heavy)
    html, status = _scrapeowl_fetch(url, render_js=True)
    if html and len(html) > 500 and ("<body" in html.lower() or "<a " in html.lower()):
        return html, status
    # 2) ScrapeOwl without JS
    html, status = _scrapeowl_fetch(url, render_js=False)
    if html and len(html) > 500:
        return html, status
    # 3) Direct HTTP fallback
    html, status = _direct_fetch(url)
    if html and len(html) > 200:
        return html, status
    return "", 0


# ── Link extraction ─────────────────────────────────────────────────────────

class LinkExtractor(HTMLParser):
    """Minimal HTML parser that collects href values from <a> tags."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self.links.append(v)


def normalize_url(raw_url, base_url, domain):
    """Resolve + normalize a URL. Returns absolute URL if internal, else None.
    Strips fragments, lowercases host, drops query strings for canonicalization
    of internal crawl targets (keeps crawl graph clean)."""
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    if not raw_url or raw_url.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    absolute = urljoin(base_url, raw_url)
    absolute, _frag = urldefrag(absolute)
    parsed = urlparse(absolute)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parsed.netloc or "").lower()
    # Match domain or subdomains
    if host != domain and not host.endswith("." + domain):
        return None
    # Normalize: https, no trailing slash on root path, lowercase host
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    normalized = f"https://{host}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def extract_internal_links(html, base_url, domain):
    """Return a list of normalized internal target URLs found on the page."""
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen = set()
    for href in parser.links:
        norm = normalize_url(href, base_url, domain)
        if norm and norm != base_url:
            seen.add(norm)
    return sorted(seen)


# ── 1. crawl_internal_links ─────────────────────────────────────────────────

def crawl_internal_links(domain, limit=100):
    """Crawl up to `limit` pages on the domain, building a link graph
    (source_url -> [target_urls]). Starts from the homepage and BFS-expands.
    Uses ScrapeOwl for page fetching."""
    if not domain:
        raise ValueError("domain is required")
    domain = domain.lower().strip()
    if domain.startswith(("http://", "https://")):
        domain = urlparse(domain).netloc.lower()
    start = f"https://{domain}/"
    print(f"[crawl] Starting crawl of {domain} (limit={limit}) from {start}")

    link_graph = {}      # source -> [targets]
    visited = set()
    queue = deque([start])
    fetched = 0
    failed = 0

    while queue and fetched < limit:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        print(f"  [crawl {fetched+1}/{limit}] {url}")
        html, status = fetch_page_html(url)
        if not html or status != 200:
            failed += 1
            continue
        fetched += 1
        targets = extract_internal_links(html, url, domain)
        link_graph[url] = targets
        # enqueue newly discovered internal URLs
        for t in targets:
            if t not in visited and t not in queue:
                queue.append(t)

    print(f"[crawl] Done: {fetched} pages fetched, {failed} failed, "
          f"{len(link_graph)} pages in link graph.")
    return link_graph


# ── 2. compute_equity_scores ────────────────────────────────────────────────

def compute_equity_scores(link_graph, domain):
    """For each page compute incoming/outgoing link counts and an equity score.
    Identify hub pages (many outgoing), authority pages (many incoming),
    orphan pages (no incoming), dead-end pages (no outgoing).

    Returns a dict: {page: {incoming, outgoing, equity, depth, type}}
    plus lists: hubs, authorities, orphans, dead_ends.
    """
    all_pages = set()
    for src, targets in link_graph.items():
        all_pages.add(src)
        for t in targets:
            all_pages.add(t)

    incoming = defaultdict(list)   # target -> [source, ...]
    outgoing = defaultdict(list)    # source -> [target, ...]
    for src, targets in link_graph.items():
        # de-duplicate outgoing for counting
        uniq = sorted(set(targets))
        outgoing[src] = uniq
        for t in uniq:
            incoming[t].append(src)

    total = max(1, len(all_pages))
    scores = {}
    for p in all_pages:
        inc = len(incoming.get(p, []))
        out = len(outgoing.get(p, []))
        scores[p] = {
            "incoming": inc,
            "outgoing": out,
            "equity": round(inc / total, 4),
            "incoming_sources": sorted(incoming.get(p, [])),
            "outgoing_targets": outgoing.get(p, []),
            "type": classify_page_type(p),
        }

    # Sortable derived lists
    hubs = sorted(all_pages, key=lambda p: scores[p]["outgoing"], reverse=True)
    authorities = sorted(all_pages, key=lambda p: scores[p]["incoming"], reverse=True)
    orphans = sorted(p for p in all_pages if scores[p]["incoming"] == 0)
    dead_ends = sorted(p for p in all_pages if scores[p]["outgoing"] == 0)

    # Crawl depth (BFS from homepage)
    depths = compute_crawl_depth(link_graph, domain)
    for p, d in depths.items():
        scores[p]["depth"] = d
    for p in all_pages:
        scores[p].setdefault("depth", None)

    return {
        "scores": scores,
        "hubs": hubs,
        "authorities": authorities,
        "orphans": orphans,
        "dead_ends": dead_ends,
        "incoming_map": {k: v for k, v in incoming.items()},
        "outgoing_map": {k: v for k, v in outgoing.items()},
        "all_pages": sorted(all_pages),
        "total_pages": len(all_pages),
    }


def classify_page_type(url):
    """Classify a URL into a high-level page type by path patterns."""
    path = urlparse(url).path.lower()
    for ptype, patterns in HIGH_VALUE_PATTERNS.items():
        for pat in patterns:
            if pat in path:
                return ptype
    if path in ("/", ""):
        return "home"
    return "other"


def compute_crawl_depth(link_graph, domain):
    """BFS from the homepage to compute click-depth for every reachable page.
    Returns {url: depth}. Unreachable pages are absent (depth=None upstream)."""
    start = f"https://{domain}/"
    depths = {start: 0}
    queue = deque([start])
    # Build adjacency from the link graph (only edges we crawled)
    adj = {}
    for src, targets in link_graph.items():
        adj[src] = set(targets)
    while queue:
        cur = queue.popleft()
        d = depths[cur]
        for nbr in adj.get(cur, []):
            if nbr not in depths:
                depths[nbr] = d + 1
                queue.append(nbr)
    return depths


# ── 3. identify_link_gaps ───────────────────────────────────────────────────

def identify_link_gaps(link_graph, domain):
    """Find pages that should be linked to but aren't:
    - high-value pages (product/collection) with low incoming links
    - blog posts not linked from relevant product pages
    - new pages with no internal links at all (orphans among high-value)
    Returns a list of dicts: {page, type, incoming, reason, suggestion}.
    """
    equity = compute_equity_scores(link_graph, domain)
    scores = equity["scores"]
    gaps = []

    # Inverse index: which pages link to product/blog, for "relevant" matching
    product_pages = [p for p in scores if scores[p]["type"] == "product"]
    blog_pages = [p for p in scores if scores[p]["type"] == "blog"]
    collection_pages = [p for p in scores if scores[p]["type"] == "collection"]

    # 1) High-value pages with low incoming links (<=2)
    for p in product_pages + collection_pages:
        inc = scores[p]["incoming"]
        if inc <= 2:
            gaps.append({
                "page": p,
                "type": scores[p]["type"],
                "incoming": inc,
                "reason": f"High-value {scores[p]['type']} page with only {inc} "
                          f"incoming internal link(s).",
                "suggestion": _suggest_link_source(p, scores, equity),
            })

    # 2) Blog posts not linked from relevant product/collection pages
    for b in blog_pages:
        sources = scores[b]["incoming_sources"]
        linked_from_commercial = any(
            scores.get(s, {}).get("type") in ("product", "collection")
            for s in sources
        )
        if not linked_from_commercial and product_pages + collection_pages:
            # suggest the most-authority commercial page
            candidate = max(
                product_pages + collection_pages,
                key=lambda p: scores[p]["incoming"],
                default=None,
            )
            gaps.append({
                "page": b,
                "type": "blog",
                "incoming": scores[b]["incoming"],
                "reason": "Blog post has no internal links from product/collection "
                          "pages — missing topical relevance signal.",
                "suggestion": ([candidate] if candidate else []),
            })

    # 3) Orphan high-value pages (no incoming links)
    for p in equity["orphans"]:
        if scores[p]["type"] in ("product", "collection", "blog"):
            gaps.append({
                "page": p,
                "type": scores[p]["type"],
                "incoming": 0,
                "reason": f"Orphan {scores[p]['type']} page — no internal links "
                          f"point to it.",
                "suggestion": _suggest_link_source(p, scores, equity),
            })

    # De-duplicate by page (keep the first / most-specific reason)
    seen = set()
    deduped = []
    for g in gaps:
        if g["page"] in seen:
            continue
        seen.add(g["page"])
        deduped.append(g)

    return deduped


def _suggest_link_source(target_url, scores, equity):
    """Suggest up to 3 candidate pages that should link to target_url.
    Prefers hub pages (high outgoing) and topically related pages."""
    candidates = []
    # Hub pages with many outgoing links make good link sources
    for hub in equity["hubs"][:20]:
        if hub == target_url:
            continue
        # don't suggest if it already links to target
        if target_url in scores[hub]["outgoing_targets"]:
            continue
        candidates.append(hub)
        if len(candidates) >= 3:
            break
    return candidates


# ── 4. generate_equity_report ───────────────────────────────────────────────

def generate_equity_report(domain, link_graph, equity_scores, gaps, output_path):
    """Write a self-contained HTML report to output_path (a directory or .html file).
    Returns the path to the written HTML file."""
    # Resolve output path
    if not output_path.endswith(".html"):
        os.makedirs(output_path, exist_ok=True)
        html_path = os.path.join(output_path, "link_equity_report.html")
    else:
        html_path = output_path
        os.makedirs(os.path.dirname(html_path) or ".", exist_ok=True)

    scores = equity_scores["scores"]
    total_pages = equity_scores["total_pages"]
    total_links = sum(len(v) for v in link_graph.values())
    avg_links = round(total_links / max(1, len(link_graph)), 2)
    n_orphans = len(equity_scores["orphans"])
    n_hubs = sum(1 for p in equity_scores["all_pages"]
                 if scores[p]["outgoing"] >= 10)
    n_authorities = sum(1 for p in equity_scores["all_pages"]
                        if scores[p]["incoming"] >= 10)

    top_auth = equity_scores["authorities"][:20]
    top_hubs = equity_scores["hubs"][:20]

    today = datetime.now().strftime("%m/%d/%Y")

    # SVG histogram of incoming link counts
    svg_hist = build_histogram_svg(scores, equity_scores["all_pages"])

    # Crawl depth distribution
    depth_counts = defaultdict(int)
    for p in equity_scores["all_pages"]:
        d = scores[p].get("depth")
        if d is None:
            depth_counts["unreachable"] += 1
        else:
            depth_counts[d] += 1

    # Build recommendations HTML
    recs_html = build_recommendations_html(gaps)

    # Expandable tables
    auth_table = build_page_table(top_auth, scores, "authority")
    hub_table = build_page_table(top_hubs, scores, "hub")
    orphan_list = build_simple_list(equity_scores["orphans"], scores,
                                    label="Orphan Pages (0 incoming)")
    deadend_list = build_simple_list(equity_scores["dead_ends"], scores,
                                     label="Dead-End Pages (0 outgoing)")
    depth_html = build_depth_html(depth_counts, scores, equity_scores["all_pages"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Internal Link Equity Report — {html_escape(domain)}</title>
<style>
{REPORT_CSS}
</style>
</head>
<body>
<div class="container">
  <header class="report-header">
    <h1>Internal Link Equity Report</h1>
    <div class="subtitle">{html_escape(domain)} · Generated {today}</div>
  </header>

  <section class="summary-cards">
    <div class="card metric-card"><div class="metric-value">{total_pages}</div><div class="metric-label">Total Pages</div></div>
    <div class="card metric-card"><div class="metric-value">{total_links}</div><div class="metric-label">Total Internal Links</div></div>
    <div class="card metric-card"><div class="metric-value">{avg_links}</div><div class="metric-label">Avg Links / Page</div></div>
    <div class="card metric-card warn"><div class="metric-value">{n_orphans}</div><div class="metric-label">Orphan Pages</div></div>
    <div class="card metric-card good"><div class="metric-value">{n_hubs}</div><div class="metric-label">Hub Pages (≥10 out)</div></div>
    <div class="card metric-card good"><div class="metric-value">{n_authorities}</div><div class="metric-label">Authority Pages (≥10 in)</div></div>
  </section>

  <section class="chart-section">
    <h2>Link Equity Distribution</h2>
    <p class="section-desc">Histogram of incoming internal link counts across all discovered pages.</p>
    {svg_hist}
  </section>

  <section class="block">
    <h2 class="expandable-toggle" data-target="auth-detail">Top 20 Authority Pages (most incoming links) ▸</h2>
    <div id="auth-detail" class="expandable-content">{auth_table}</div>
  </section>

  <section class="block">
    <h2 class="expandable-toggle" data-target="hub-detail">Top 20 Hub Pages (most outgoing links) ▸</h2>
    <div id="hub-detail" class="expandable-content">{hub_table}</div>
  </section>

  <section class="block">
    <h2 class="expandable-toggle" data-target="orphan-detail">Orphan Pages (0 incoming) — Needs Attention ▸</h2>
    <div id="orphan-detail" class="expandable-content">{orphan_list}</div>
  </section>

  <section class="block">
    <h2 class="expandable-toggle" data-target="deadend-detail">Dead-End Pages (0 outgoing) ▸</h2>
    <div id="deadend-detail" class="expandable-content">{deadend_list}</div>
  </section>

  <section class="block">
    <h2>Crawl Depth Analysis</h2>
    <p class="section-desc">How many clicks from the homepage to reach each page (BFS).</p>
    {depth_html}
  </section>

  <section class="block">
    <h2>Internal Linking Recommendations</h2>
    <p class="section-desc">Specific link suggestions to improve equity flow.</p>
    {recs_html}
  </section>

  <footer class="report-footer">
    Internal Link Equity Analyzer v{SCRIPT_VERSION} · Zenfusion · noindex,nofollow
  </footer>
</div>
<script>
document.querySelectorAll('.expandable-toggle').forEach(function(t) {{
  t.addEventListener('click', function() {{
    var d = document.getElementById(t.getAttribute('data-target'));
    if (d) {{ d.style.display = (d.style.display === 'block') ? 'none' : 'block'; }}
    var txt = t.textContent;
    if (d && d.style.display === 'block') {{
      t.textContent = txt.replace('▸','▾');
    }} else {{
      t.textContent = txt.replace('▾','▸');
    }}
  }});
}});
</script>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[report] Written to {html_path}")
    return html_path


# ── Report builders ─────────────────────────────────────────────────────────

REPORT_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f8f9fa; color: #1a1a1a;
  font-size: 16px; line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 24px; }
.report-header { margin-bottom: 24px; }
.report-header h1 { font-size: 30px; margin: 0 0 4px 0; color: #1a1a1a; }
.subtitle { color: #555; font-size: 15px; }
.summary-cards { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 28px; }
.card {
  background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
  padding: 18px 22px; min-width: 140px; flex: 1 1 160px;
}
.metric-card .metric-value { font-size: 30px; font-weight: 700; color: #1a1a1a; }
.metric-card .metric-label { font-size: 14px; color: #555; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.metric-card.warn .metric-value { color: #e67e22; }
.metric-card.good .metric-value { color: #27ae60; }
section { margin-bottom: 28px; }
h2 { font-size: 22px; color: #1a1a1a; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; cursor: default; }
.expandable-toggle { cursor: pointer; user-select: none; }
.expandable-content { display: none; margin-top: 12px; }
.section-desc { color: #555; font-size: 15px; margin-top: -6px; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; border: 1px solid #e0e0e0; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 15px; }
th { background: #f0f2f5; font-weight: 600; color: #333; }
td a { color: #0066cc; text-decoration: none; word-break: break-all; }
td a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; }
.badge.product { background: #0066cc; }
.badge.collection { background: #8e44ad; }
.badge.blog { background: #16a085; }
.badge.page { background: #7f8c8d; }
.badge.home { background: #2c3e50; }
.badge.other { background: #95a5a6; }
.orphan-item, .deadend-item { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px; }
.orphan-item a, .deadend-item a { color: #0066cc; text-decoration: none; word-break: break-all; }
.orphan-item a:hover, .deadend-item a:hover { text-decoration: underline; }
.rec-card { background: #fff; border: 1px solid #e0e0e0; border-left: 4px solid #0066cc; border-radius: 6px; padding: 14px 18px; margin-bottom: 12px; }
.rec-card .rec-page { font-weight: 600; color: #1a1a1a; word-break: break-all; }
.rec-card .rec-reason { color: #444; margin: 6px 0; }
.rec-card .rec-suggest { color: #27ae60; font-size: 14px; }
.rec-card .rec-suggest a { color: #0066cc; text-decoration: none; }
.depth-row { display: flex; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px solid #eee; }
.depth-label { font-weight: 600; min-width: 110px; color: #333; }
.depth-bar-container { flex: 1; background: #f0f2f5; border-radius: 4px; height: 22px; overflow: hidden; }
.depth-bar { height: 100%; background: #0066cc; color: #fff; padding: 0 8px; line-height: 22px; font-size: 13px; }
.depth-count { min-width: 60px; text-align: right; color: #555; }
.chart-section svg { max-width: 100%; height: auto; }
.report-footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid #e0e0e0; color: #555; font-size: 13px; text-align: center; }
"""


def build_page_table(pages, scores, kind):
    """Build an HTML table for top authority or hub pages."""
    if not pages:
        return "<p>No pages found.</p>"
    rows = []
    for p in pages:
        s = scores.get(p, {})
        inc = s.get("incoming", 0)
        out = s.get("outgoing", 0)
        eq = s.get("equity", 0)
        ptype = s.get("type", "other")
        depth = s.get("depth", "—")
        rows.append(
            f"<tr>"
            f"<td><a href=\"{html_escape(p)}\" target=\"_blank\" rel=\"noopener nofollow\">{html_escape(p)}</a></td>"
            f"<td><span class=\"badge {ptype}\">{ptype}</span></td>"
            f"<td>{inc}</td>"
            f"<td>{out}</td>"
            f"<td>{eq}</td>"
            f"<td>{depth if depth is not None else '—'}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>URL</th><th>Type</th><th>Incoming</th><th>Outgoing</th>"
        "<th>Equity</th><th>Depth</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def build_simple_list(pages, scores, label=""):
    """Build a simple list of pages (orphans or dead-ends)."""
    if not pages:
        return f"<p>No {label.lower()} found.</p>"
    items = []
    for p in pages:
        s = scores.get(p, {})
        ptype = s.get("type", "other")
        depth = s.get("depth", "—")
        items.append(
            f"<div class=\"orphan-item\">"
            f"<a href=\"{html_escape(p)}\" target=\"_blank\" rel=\"noopener nofollow\">{html_escape(p)}</a> "
            f"<span class=\"badge {ptype}\">{ptype}</span> "
            f"<span style=\"color:#555;font-size:13px\">depth: {depth if depth is not None else 'unreachable'}</span>"
            f"</div>"
        )
    return "".join(items)


def build_histogram_svg(scores, all_pages):
    """Build an inline SVG histogram of incoming-link counts."""
    # bucket the incoming counts
    incoming_counts = [scores[p]["incoming"] for p in all_pages]
    if not incoming_counts:
        return "<p>No data for histogram.</p>"
    max_inc = max(incoming_counts)
    # buckets: 0, 1, 2-3, 4-5, 6-10, 11-20, 21+
    buckets = [
        ("0", lambda x: x == 0),
        ("1", lambda x: x == 1),
        ("2-3", lambda x: 2 <= x <= 3),
        ("4-5", lambda x: 4 <= x <= 5),
        ("6-10", lambda x: 6 <= x <= 10),
        ("11-20", lambda x: 11 <= x <= 20),
        ("21+", lambda x: x >= 21),
    ]
    counts = [sum(1 for c in incoming_counts if fn(c)) for _, fn in buckets]
    max_count = max(counts) if counts else 1
    max_count = max(max_count, 1)

    w, h = 760, 280
    pad_l, pad_b, pad_t = 50, 40, 20
    chart_w = w - pad_l - 10
    chart_h = h - pad_b - pad_t
    bar_w = chart_w / len(buckets)
    bars = []
    for i, (label, _) in enumerate(buckets):
        cnt = counts[i]
        bh = (cnt / max_count) * chart_h if cnt else 0
        x = pad_l + i * bar_w
        y = pad_t + chart_h - bh
        bars.append(
            f"<rect x=\"{x:.1f}\" y=\"{y:.1f}\" width=\"{bar_w*0.7:.1f}\" height=\"{bh:.1f}\" fill=\"#0066cc\" rx=\"2\"/>"
            f"<text x=\"{x + bar_w*0.35:.1f}\" y=\"{y - 5:.1f}\" text-anchor=\"middle\" font-size=\"12\" fill=\"#333\">{cnt}</text>"
            f"<text x=\"{x + bar_w*0.35:.1f}\" y=\"{pad_t + chart_h + 18:.1f}\" text-anchor=\"middle\" font-size=\"11\" fill=\"#555\">{label}</text>"
        )
    # Y axis
    y_axis = (
        f"<line x1=\"{pad_l}\" y1=\"{pad_t}\" x2=\"{pad_l}\" y2=\"{pad_t+chart_h}\" stroke=\"#ccc\"/>"
        f"<line x1=\"{pad_l}\" y1=\"{pad_t+chart_h}\" x2=\"{pad_l+chart_w}\" y2=\"{pad_t+chart_h}\" stroke=\"#ccc\"/>"
    )
    svg = (
        f"<svg viewBox=\"0 0 {w} {h}\" xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        f"aria-label=\"Histogram of incoming internal link counts\">"
        f"<text x=\"{pad_l}\" y=\"14\" font-size=\"13\" fill=\"#555\">Pages (count) by incoming links</text>"
        f"{y_axis}{''.join(bars)}"
        f"<text x=\"{pad_l+chart_w/2:.0f}\" y=\"{h-4}\" text-anchor=\"middle\" font-size=\"12\" fill=\"#555\">Incoming internal links</text>"
        f"</svg>"
    )
    return svg


def build_depth_html(depth_counts, scores, all_pages):
    """Build crawl-depth distribution bars."""
    if not depth_counts:
        return "<p>No crawl-depth data.</p>"
    total = sum(depth_counts.values()) or 1
    keys = sorted(k for k in depth_counts.keys() if isinstance(k, int))
    if "unreachable" in depth_counts:
        keys.append("unreachable")
    rows = []
    max_c = max(depth_counts.values()) or 1
    for k in keys:
        c = depth_counts[k]
        pct = round(c / total * 100, 1)
        label = f"Depth {k}" if isinstance(k, int) else "Unreachable"
        bar_w = round(c / max_c * 100, 1)
        rows.append(
            f"<div class=\"depth-row\">"
            f"<div class=\"depth-label\">{label}</div>"
            f"<div class=\"depth-bar-container\"><div class=\"depth-bar\" style=\"width:{bar_w}%\">{c}</div></div>"
            f"<div class=\"depth-count\">{pct}%</div>"
            f"</div>"
        )
    return "".join(rows)


def build_recommendations_html(gaps):
    """Build the recommendations section from gap analysis."""
    if not gaps:
        return "<p>No link gaps detected — internal linking looks healthy.</p>"
    cards = []
    for g in gaps:
        page = g["page"]
        ptype = g.get("type", "other")
        reason = g.get("reason", "")
        suggestions = g.get("suggestion", []) or []
        sug_html = ""
        if suggestions:
            links = []
            for s in suggestions:
                links.append(
                    f"<a href=\"{html_escape(s)}\" target=\"_blank\" rel=\"noopener nofollow\">{html_escape(s)}</a>"
                )
            sug_html = "<div class=\"rec-suggest\">Suggested link source(s): " + ", ".join(links) + "</div>"
        cards.append(
            f"<div class=\"rec-card\">"
            f"<div class=\"rec-page\"><span class=\"badge {ptype}\">{ptype}</span> "
            f"<a href=\"{html_escape(page)}\" target=\"_blank\" rel=\"noopener nofollow\">{html_escape(page)}</a></div>"
            f"<div class=\"rec-reason\">{html_escape(reason)}</div>"
            f"{sug_html}"
            f"</div>"
        )
    return "".join(cards)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Internal Link Equity Analyzer")
    ap.add_argument("--domain", required=True, help="Domain to analyze (e.g. softprowatersystems.com)")
    ap.add_argument("--limit", type=int, default=100, help="Max pages to crawl (default 100)")
    ap.add_argument("--output", default="/tmp/link_equity_report",
                    help="Output directory or .html file path")
    args = ap.parse_args()

    domain = args.domain.lower().strip()
    if domain.startswith(("http://", "https://")):
        domain = urlparse(domain).netloc.lower()

    print(f"=== Internal Link Equity Analyzer v{SCRIPT_VERSION} ===")
    print(f"Domain: {domain}  Limit: {args.limit}  Output: {args.output}")
    if not SCRAPEOWL_KEY:
        print("WARNING: SCRAPEOWL_API_KEY not set — falling back to direct HTTP only.",
              file=sys.stderr)

    # 1. Crawl
    link_graph = crawl_internal_links(domain, limit=args.limit)
    if not link_graph:
        print("ERROR: No pages could be fetched. Check the domain or ScrapeOwl key.",
              file=sys.stderr)
        sys.exit(1)

    # 2. Compute equity scores
    equity_scores = compute_equity_scores(link_graph, domain)

    # 3. Identify link gaps
    gaps = identify_link_gaps(link_graph, domain)

    # 4. Generate report
    report_path = generate_equity_report(domain, link_graph, equity_scores,
                                          gaps, args.output)

    # Console summary
    print("\n=== SUMMARY ===")
    print(f"Total pages:         {equity_scores['total_pages']}")
    print(f"Total internal links: {sum(len(v) for v in link_graph.values())}")
    print(f"Orphan pages:        {len(equity_scores['orphans'])}")
    print(f"Dead-end pages:      {len(equity_scores['dead_ends'])}")
    print(f"Link gaps found:     {len(gaps)}")
    print(f"Report:              {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
