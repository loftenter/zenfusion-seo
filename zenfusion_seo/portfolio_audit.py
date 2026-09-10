#!/usr/bin/env python3
"""
Portfolio SEO Audit — standalone script for auditing multiple Zenfusion brands.

Runs 7 DataForSEO endpoint types per domain, aggregates results, and generates
an HTML dashboard with portfolio overview cards, cross-portfolio Top 10 fix list,
and per-brand expandable detail sections with traffic-light indicators.

Usage:
    python3 portfolio_audit.py --domains softprowatersystems.com,qualitywatertreatment.com --output /tmp/portfolio_audit_test
    python3 portfolio_audit.py --all --output /tmp/portfolio_audit
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests

# ── Constants ──────────────────────────────────────────────────────────────

API_BASE = "https://api.dataforseo.com/v3"
RATE_LIMIT_DELAY = 0.5  # seconds between API calls
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = "en"

ALL_BRANDS = [
    "softprowatersystems.com",
    "qualitywatertreatment.com",
    "plumbingsupplyandmore.com",
    "myhomewaterfilter.com",
    "watergenpros.com",
    "thrivegarden.com",
    "elregalonicaragua.com",
]

# Default keywords per brand type (water treatment / garden)
DEFAULT_KEYWORDS = [
    "water filter system",
    "water softener",
    "water treatment",
]

# ── Credential loading ─────────────────────────────────────────────────────


def load_credentials(env_path="~/.hermes/.env"):
    """Load DataForSEO credentials from .env file."""
    env_path = os.path.expanduser(env_path)
    login = None
    password = None
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATAFORSEO_LOGIN="):
                    login = line.split("=", 1)[1]
                elif line.startswith("DATAFORSEO_PASSWORD="):
                    password = line.split("=", 1)[1]
    # Fall back to environment variables
    login = login or os.environ.get("DATAFORSEO_LOGIN")
    password = password or os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        print("ERROR: DataForSEO credentials not found. Set DATAFORSEO_LOGIN and "
              "DATAFORSEO_PASSWORD in ~/.hermes/.env or environment.", file=sys.stderr)
        sys.exit(1)
    return login, password


# ── API client ─────────────────────────────────────────────────────────────


class DataForSEOClient:
    """Thin wrapper around DataForSEO REST API with rate-limit handling."""

    def __init__(self, login, password):
        self.session = requests.Session()
        self.session.auth = (login, password)
        self.session.headers.update({"Content-Type": "application/json"})

    def post(self, path, payload):
        """POST to a DataForSEO v3 endpoint and return the parsed JSON."""
        url = f"{API_BASE}{path}"
        try:
            resp = self.session.post(url, json=payload, timeout=120)
            time.sleep(RATE_LIMIT_DELAY)
            data = resp.json()
            return data
        except requests.exceptions.Timeout:
            return {"status_code": 0, "status_message": "Request timed out"}
        except Exception as e:
            return {"status_code": 0, "status_message": str(e)}


# ── Endpoint wrappers ──────────────────────────────────────────────────────


def _safe_get(d, *keys, default=None):
    """Safely traverse nested dict keys."""
    for k in keys:
        if d is None or not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def _status_ok(data):
    """Check if the API response status is OK (20000)."""
    if not isinstance(data, dict):
        return False
    # Could be top-level (flat .ai mode) or nested tasks structure
    sc = data.get("status_code")
    if sc == 20000:
        return True
    # Check nested tasks structure
    tasks = data.get("tasks", [])
    if tasks:
        return tasks[0].get("status_code") == 20000
    return False


def _get_items(data):
    """Extract items array from DataForSEO nested response structure.
    Handles: data.tasks[0].result[0].items[]  (most endpoints)
    Also:    data.items[]                     (flat .ai mode, if used)
    """
    if not isinstance(data, dict):
        return []
    # Flat .ai mode: items at top level
    items = data.get("items")
    if items and isinstance(items, list):
        return items
    # Nested: tasks[0].result[0].items
    tasks = data.get("tasks", [])
    if tasks:
        result = tasks[0].get("result")
        if isinstance(result, list) and result:
            r0 = result[0]
            if isinstance(r0, dict):
                return r0.get("items", [])
        elif isinstance(result, dict):
            return result.get("items", [])
    return []


def _get_result(data):
    """Extract result[0] dict from DataForSEO nested response.
    For endpoints where result[0] IS the data (e.g. backlinks_summary).
    """
    if not isinstance(data, dict):
        return {}
    # Flat .ai mode
    if "items" in data:
        items = data.get("items", [])
        return items[0] if items else {}
    # Nested: tasks[0].result[0]
    tasks = data.get("tasks", [])
    if tasks:
        result = tasks[0].get("result")
        if isinstance(result, list) and result:
            return result[0] if result else {}
        elif isinstance(result, dict):
            return result
    return {}


# ── 7 Endpoint calls ──────────────────────────────────────────────────────


def call_on_page_instant_pages(client, url):
    """Endpoint 1: On-page issues + indexability."""
    payload = [{"url": url}]
    data = client.post("/on_page/instant_pages", payload)
    items = _get_items(data)
    if not items:
        sc = data.get("status_code")
        # Check task-level error (e.g. 40501 domain not found)
        tasks = data.get("tasks", [])
        if tasks:
            sc = tasks[0].get("status_code", sc)
        return {"error": True, "status_code": sc,
                "status_message": data.get("status_message", "No data")}
    item = items[0]
    meta = item.get("meta", {})
    checks = item.get("checks", {})
    page_timing = item.get("page_timing", {})

    # broken_links: False (boolean) when none, or array when present
    raw_broken = item.get("broken_links", 0)
    if isinstance(raw_broken, bool):
        broken_links_count = 0
    elif isinstance(raw_broken, list):
        broken_links_count = len(raw_broken)
    else:
        broken_links_count = raw_broken or 0

    # no_image_alt: boolean (True = images without alt exist)
    no_image_alt = checks.get("no_image_alt", False)

    result = {
        "url": url,
        "meta_title": meta.get("title", ""),
        "meta_description": meta.get("description", ""),
        "h1_list": meta.get("htags", {}).get("h1", []) if meta.get("htags") else [],
        "canonical_url": meta.get("canonical", ""),
        "has_canonical": checks.get("canonical", False),
        "is_https": checks.get("is_https", False),
        "is_robots_allowed": checks.get("is_robots_allowed", True),
        "no_image_alt": no_image_alt,
        "broken_links": broken_links_count,
        "page_load_time_ms": page_timing.get("time_to_interactive", 0) or 0,
        "internal_links": meta.get("internal_links_count", 0) or 0,
        "raw": item,
    }
    return result


def call_lighthouse(client, url):
    """Endpoint 2: Core Web Vitals + Lighthouse scores."""
    payload = [{"url": url}]
    data = client.post("/on_page/lighthouse/live/json", payload)
    # Lighthouse: result[0] IS the data (no items[] inside)
    item = _get_result(data)
    if not item or not isinstance(item, dict) or "categories" not in item:
        sc = data.get("status_code")
        tasks = data.get("tasks", [])
        if tasks:
            sc = tasks[0].get("status_code", sc)
        return {"error": True, "status_code": sc,
                "status_message": data.get("status_message", "No data")}
    categories = item.get("categories", {})
    audits = item.get("audits", {})

    def _score(cat_name):
        cat = categories.get(cat_name, {})
        score = cat.get("score")
        return int(round(score * 100)) if score is not None else None

    lcp_audit = audits.get("largest-contentful-paint", {})
    tbt_audit = audits.get("total-blocking-time", {})
    cls_audit = audits.get("cumulative-layout-shift", {})

    return {
        "performance_score": _score("performance"),
        "seo_score": _score("seo"),
        "accessibility_score": _score("accessibility"),
        "best_practices_score": _score("best-practices"),
        "largest_contentful_paint_ms": lcp_audit.get("numericValue"),
        "total_blocking_time_ms": tbt_audit.get("numericValue"),
        "cumulative_layout_shift": cls_audit.get("numericValue"),
        "raw": item,
    }


def call_domain_rank_overview(client, domain, location_code=DEFAULT_LOCATION_CODE,
                              language_code=DEFAULT_LANGUAGE_CODE):
    """Endpoint 3: Visibility trend + estimated traffic."""
    payload = [{"target": domain, "location_code": location_code,
                "language_code": language_code}]
    data = client.post("/dataforseo_labs/google/domain_rank_overview/live", payload)
    items = _get_items(data)
    if not items:
        # Fallback: check result[0] directly
        r0 = _get_result(data)
        if r0 and isinstance(r0, dict):
            metrics = r0.get("metrics", {})
            organic = metrics.get("organic", {})
            return {"organic_etv": organic.get("etv", 0) or 0,
                    "organic_count": organic.get("count", 0) or 0, "raw": r0}
        return {"error": True, "organic_etv": 0, "organic_count": 0}
    item = items[0]
    metrics = item.get("metrics", {})
    organic = metrics.get("organic", {})
    return {
        "organic_etv": organic.get("etv", 0) or 0,
        "organic_count": organic.get("count", 0) or 0,
        "raw": item,
    }


def call_serp_organic_advanced(client, keyword, location_code=DEFAULT_LOCATION_CODE,
                               language_code=DEFAULT_LANGUAGE_CODE, depth=10):
    """Endpoint 4: SERP features displacing client rankings."""
    payload = [{"keyword": keyword, "location_code": location_code,
                "language_code": language_code, "depth": depth,
                "use_improved_etv": True}]
    data = client.post("/serp/google/organic/live/advanced", payload)
    items = _get_items(data)
    return items  # list of SERP result items


def call_backlinks_summary(client, domain):
    """Endpoint 5: Overall backlink health."""
    payload = [{"target": domain, "limit": 1}]
    data = client.post("/backlinks/summary/live", payload)
    # backlinks_summary: result[0] IS the data (no items[] inside)
    item = _get_result(data)
    if not item or not isinstance(item, dict):
        return {"error": True, "referring_domains": 0, "backlinks": 0,
                "broken_backlinks": 0, "spam_score": 0}
    info = item.get("info", {})
    return {
        "referring_domains": item.get("referring_domains", 0) or 0,
        "backlinks": item.get("backlinks", 0) or 0,
        "broken_backlinks": item.get("broken_backlinks", 0) or 0,
        "spam_score": info.get("target_spam_score", 0) or 0,
        "raw": item,
    }


def call_backlinks_timeseries(client, domain, date_from, date_to):
    """Endpoint 6: Lost-link spike detection (last 30 days)."""
    payload = [{"target": domain, "date_from": date_from, "date_to": date_to,
                "group_range": "week"}]
    data = client.post("/backlinks/timeseries_summary/live", payload)
    items = _get_items(data)
    if not items:
        return {"error": True, "items": [], "lost_in_period": 0, "lost_pct": 0}
    # Items sorted chronologically (oldest first)
    first = items[0] if items else {}
    last = items[-1] if items else {}
    baseline_rd = first.get("referring_domains", 0) or 0
    final_rd = last.get("referring_domains", 0) or 0
    lost = baseline_rd - final_rd
    lost_pct = (lost / baseline_rd * 100) if baseline_rd > 0 else 0
    return {
        "items": items,
        "baseline_referring_domains": baseline_rd,
        "final_referring_domains": final_rd,
        "lost_in_period": max(0, lost),
        "lost_pct": round(lost_pct, 1),
        "high_risk": lost_pct > 20,
    }


# ── Scoring (per skill Step 4A) ─────────────────────────────────────────────


def compute_scores(audit_data):
    """Compute Technical, Visibility, Backlink Risk, and Overall scores."""

    # Technical Health Score
    lh = audit_data.get("lighthouse", {})
    domain_not_found = audit_data.get("on_page", {}).get("error", False) and \
        audit_data.get("on_page", {}).get("status_code") == 40501

    if domain_not_found:
        technical_score = 0
    else:
        scores = [s for s in [
            lh.get("performance_score"),
            lh.get("seo_score"),
            lh.get("accessibility_score"),
            lh.get("best_practices_score"),
        ] if s is not None]
        lighthouse_avg = sum(scores) / len(scores) if scores else 0
        critical_count = sum(1 for i in audit_data.get("issues", [])
                             if i["severity"] == "CRITICAL")
        issue_penalty = min(40, critical_count * 10)
        technical_score = max(0, round(lighthouse_avg - issue_penalty))

    # Visibility Score
    etv = audit_data.get("domain_rank", {}).get("organic_etv", 0)
    if etv and etv > 0:
        visibility_score = min(100, round(math.log10(etv) * 20))
    else:
        visibility_score = 0

    # Backlink Risk Score
    bl = audit_data.get("backlinks_summary", {})
    ts = audit_data.get("backlinks_timeseries", {})
    risk = 0
    spam_score = bl.get("spam_score", 0) or 0
    if spam_score > 60:
        risk += 40
    if ts.get("high_risk", False):
        risk += 40
    broken_ratio = 0
    if bl.get("backlinks", 0) > 0:
        broken_ratio = bl.get("broken_backlinks", 0) / bl.get("backlinks", 1)
    if broken_ratio > 0.05:
        risk += 20
    backlink_risk_score = risk

    # Overall Health
    overall = round((technical_score + visibility_score +
                     (100 - backlink_risk_score)) / 3)

    # Traffic light
    if overall >= 75:
        traffic_light = "green"
    elif overall >= 50:
        traffic_light = "amber"
    else:
        traffic_light = "red"

    return {
        "technical_score": technical_score,
        "visibility_score": visibility_score,
        "backlink_risk_score": backlink_risk_score,
        "overall_score": overall,
        "traffic_light": traffic_light,
    }


def _score_color(score):
    if score is None:
        return "score-gray"
    if score >= 90:
        return "score-green"
    if score >= 50:
        return "score-amber"
    return "score-red"


def _dot_color(score):
    if score >= 75:
        return "green"
    if score >= 50:
        return "amber"
    return "red"


# ── Issue collection ──────────────────────────────────────────────────────


def collect_issues(domain, audit_data):
    """Collect all issues for a domain across endpoint results."""
    issues = []

    on_page = audit_data.get("on_page", {})
    lh = audit_data.get("lighthouse", {})
    bl = audit_data.get("backlinks_summary", {})
    ts = audit_data.get("backlinks_timeseries", {})
    serp = audit_data.get("serp_features", {})

    # Domain not found
    if on_page.get("error") and on_page.get("status_code") == 40501:
        issues.append({
            "severity": "CRITICAL", "category": "Technical",
            "issue": "Domain unresolvable — site returned 'not found' on crawl",
            "fix": "Check DNS and ensure the domain is live and accessible",
            "impact": "HIGH", "effort": "LOW",
        })
        return issues

    # On-page critical issues
    if not on_page.get("is_https", True):
        issues.append({"severity": "CRITICAL", "category": "Technical",
                       "issue": "Site is not served over HTTPS",
                       "fix": "Install SSL certificate and redirect HTTP to HTTPS",
                       "impact": "HIGH", "effort": "MEDIUM"})

    if not on_page.get("is_robots_allowed", True):
        issues.append({"severity": "CRITICAL", "category": "Technical",
                       "issue": "robots.txt blocks all crawling",
                       "fix": "Update robots.txt to allow search engine crawlers",
                       "impact": "HIGH", "effort": "LOW"})

    if not on_page.get("meta_title"):
        issues.append({"severity": "CRITICAL", "category": "Technical",
                       "issue": "Missing meta title on homepage",
                       "fix": "Add a descriptive title tag (50-60 chars)",
                       "impact": "HIGH", "effort": "LOW"})

    if not on_page.get("h1_list"):
        issues.append({"severity": "CRITICAL", "category": "Technical",
                       "issue": "Missing H1 on homepage",
                       "fix": "Add a single H1 heading to the homepage",
                       "impact": "HIGH", "effort": "LOW"})

    if not on_page.get("canonical_url") and not on_page.get("has_canonical", False):
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": "Missing canonical URL on homepage",
                       "fix": "Add a self-referencing canonical tag",
                       "impact": "MEDIUM", "effort": "LOW"})

    if not on_page.get("meta_description"):
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": "Missing meta description on homepage",
                       "fix": "Add a meta description (120-158 chars)",
                       "impact": "MEDIUM", "effort": "LOW"})

    if on_page.get("broken_links", 0) > 0:
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": f"Broken links found on homepage ({on_page['broken_links']} links)",
                       "fix": "Fix or remove broken internal and external links",
                       "impact": "MEDIUM", "effort": "MEDIUM"})

    if on_page.get("page_load_time_ms", 0) > 3000:
        issues.append({"severity": "MEDIUM", "category": "Technical",
                       "issue": f"Slow page load time ({on_page['page_load_time_ms']}ms)",
                       "fix": "Optimize images, minify resources, use caching/CDN",
                       "impact": "MEDIUM", "effort": "HIGH"})

    # Lighthouse issues
    perf = lh.get("performance_score")
    if perf is not None and perf < 50:
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": f"Poor Lighthouse performance score ({perf}/100)",
                       "fix": "Optimize page speed: compress images, reduce JS, use lazy loading",
                       "impact": "HIGH", "effort": "HIGH"})

    seo_score = lh.get("seo_score")
    if seo_score is not None and seo_score < 50:
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": f"Poor Lighthouse SEO score ({seo_score}/100)",
                       "fix": "Fix on-page SEO issues flagged by Lighthouse",
                       "impact": "HIGH", "effort": "MEDIUM"})

    lcp = lh.get("largest_contentful_paint_ms")
    if lcp is not None and lcp > 4000:
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": f"Poor LCP ({lcp:.0f}ms > 4000ms threshold)",
                       "fix": "Optimize largest contentful paint: preload hero images, reduce server response time",
                       "impact": "HIGH", "effort": "HIGH"})

    cls = lh.get("cumulative_layout_shift")
    if cls is not None and cls > 0.25:
        issues.append({"severity": "HIGH", "category": "Technical",
                       "issue": f"Poor CLS ({cls:.3f} > 0.25 threshold)",
                       "fix": "Set explicit dimensions for images, ads, and dynamic content",
                       "impact": "MEDIUM", "effort": "MEDIUM"})

    # Backlink issues
    if bl.get("spam_score", 0) > 60:
        issues.append({"severity": "HIGH", "category": "Backlinks",
                       "issue": f"High spam score ({bl['spam_score']}/100)",
                       "fix": "Audit backlink profile and disavow toxic links",
                       "impact": "HIGH", "effort": "MEDIUM"})

    if ts.get("high_risk", False):
        issues.append({"severity": "HIGH", "category": "Backlinks",
                       "issue": f"Lost-link spike: {ts.get('lost_in_period', 0)} referring "
                                f"domains lost in 30 days ({ts.get('lost_pct', 0)}%)",
                       "fix": "Investigate lost links and launch link reclamation campaign",
                       "impact": "HIGH", "effort": "HIGH"})

    if bl.get("backlinks", 0) > 0:
        broken_ratio = bl.get("broken_backlinks", 0) / bl.get("backlinks", 1)
        if broken_ratio > 0.05:
            issues.append({"severity": "HIGH", "category": "Backlinks",
                           "issue": f"High broken backlink ratio ({broken_ratio*100:.1f}%)",
                           "fix": "Fix broken backlinks or build new replacement links",
                           "impact": "MEDIUM", "effort": "MEDIUM"})

    if bl.get("referring_domains", 0) < 10:
        issues.append({"severity": "MEDIUM", "category": "Backlinks",
                       "issue": f"Low referring domain count ({bl.get('referring_domains', 0)})",
                       "fix": "Increase link building efforts to diversify backlink profile",
                       "impact": "MEDIUM", "effort": "HIGH"})

    # SERP displacement
    displacement_rate = serp.get("displacement_rate", 0)
    if displacement_rate > 50:
        issues.append({"severity": "MEDIUM", "category": "SERP",
                       "issue": f"High SERP feature displacement ({displacement_rate}% of keywords)",
                       "fix": "Optimize content for SERP features (featured snippets, local pack)",
                       "impact": "MEDIUM", "effort": "HIGH"})

    return issues


# ── Core audit functions ────────────────────────────────────────────────────


def audit_domain(domain, keywords=None, location_code=DEFAULT_LOCATION_CODE,
                 language_code=DEFAULT_LANGUAGE_CODE):
    """
    Run all 7 endpoint types for one domain.
    Returns structured data dict with all audit results.
    """
    if keywords is None:
        keywords = DEFAULT_KEYWORDS

    login, password = load_credentials()
    client = DataForSEOClient(login, password)

    print(f"\n{'='*60}")
    print(f"  Auditing: {domain}")
    print(f"{'='*60}")

    result = {"domain": domain, "timestamp": datetime.now().isoformat()}

    # Endpoint 1: On-page instant pages (homepage)
    print(f"  [1/7] On-page instant pages (homepage)...")
    on_page = call_on_page_instant_pages(client, f"https://{domain}")
    result["on_page"] = on_page

    # Endpoint 2: Lighthouse
    domain_not_found = on_page.get("error") and on_page.get("status_code") == 40501
    if domain_not_found:
        print(f"  [2/7] Lighthouse — SKIPPED (domain not found)")
        result["lighthouse"] = {"error": True, "reason": "domain_not_found"}
    else:
        print(f"  [2/7] Lighthouse...")
        result["lighthouse"] = call_lighthouse(client, f"https://{domain}")

    # Endpoint 3: Domain rank overview
    print(f"  [3/7] Domain rank overview...")
    result["domain_rank"] = call_domain_rank_overview(
        client, domain, location_code, language_code)

    # Endpoint 4: SERP organic advanced (per keyword)
    print(f"  [4/7] SERP organic advanced ({len(keywords)} keywords)...")
    serp_results = []
    serp_features_summary = {
        "displacement_rate": 0,
        "features_by_keyword": [],
        "client_positions": [],
        "feature_counts": Counter(),
    }
    for kw in keywords:
        items = call_serp_organic_advanced(client, kw, location_code, language_code)
        client_rank = None
        client_url = None
        features_above = []

        for item in items:
            item_type = item.get("type", "")
            if item_type == "organic" and domain in (item.get("domain", "") or ""):
                if client_rank is None:
                    client_rank = item.get("rank_absolute")
                    client_url = item.get("url")
            elif item_type != "organic":
                features_above.append(item_type)

        displacing = []
        if client_rank is not None:
            for item in items:
                item_type = item.get("type", "")
                if item_type != "organic":
                    item_rank = item.get("rank_absolute")
                    if item_rank is not None and item_rank < client_rank:
                        displacing.append(item_type)

        if displacing:
            serp_features_summary["feature_counts"].update(displacing)

        serp_features_summary["client_positions"].append({
            "keyword": kw, "position": client_rank, "url": client_url,
            "displacing_features": displacing,
        })

        serp_results.append({"keyword": kw, "items": items,
                            "client_rank": client_rank, "client_url": client_url})

    # Compute displacement rate
    total_kw = len(keywords)
    displaced_kw = sum(1 for p in serp_features_summary["client_positions"]
                       if len(p["displacing_features"]) > 0)
    serp_features_summary["displacement_rate"] = round(
        displaced_kw / total_kw * 100) if total_kw > 0 else 0
    result["serp_features"] = serp_features_summary
    result["serp_raw"] = serp_results

    # Endpoint 5: Backlinks summary
    print(f"  [5/7] Backlinks summary...")
    result["backlinks_summary"] = call_backlinks_summary(client, domain)

    # Endpoint 6: Backlinks timeseries (last 30 days)
    print(f"  [6/7] Backlinks timeseries (last 30 days)...")
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    result["backlinks_timeseries"] = call_backlinks_timeseries(
        client, domain, date_from, date_to)

    # Endpoint 7: On-page instant pages per landing page (from SERP data)
    print(f"  [7/7] On-page deep scan (per landing page)...")
    if domain_not_found:
        result["landing_pages"] = []
        result["on_page_deep_scan"] = {"pages_crawled": 0, "issues": [], "summary": {}}
    else:
        # Collect unique ranking URLs from SERP data
        landing_urls = set()
        for sr in serp_results:
            if sr["client_url"]:
                landing_urls.add(sr["client_url"])
        # Always include homepage
        landing_urls.add(f"https://{domain}")
        landing_urls.add(f"https://{domain}/")

        # Deduplicate (treat trailing slash as same)
        unique_urls = set()
        for u in landing_urls:
            normalized = u.rstrip("/") if u != f"https://{domain}/" else f"https://{domain}"
            unique_urls.add(normalized)
        landing_pages_audit = []
        for url in sorted(unique_urls):
            if url in [f"https://{domain}", f"https://{domain}/"]:
                page_data = on_page  # reuse homepage result
            else:
                print(f"         Crawling: {url}")
                page_data = call_on_page_instant_pages(client, url)
            landing_pages_audit.append({"url": url, "data": page_data})

        result["landing_pages"] = [{"url": lp["url"]} for lp in landing_pages_audit]
        result["on_page_deep_scan"] = _aggregate_deep_scan(landing_pages_audit, domain)

    # Collect issues and compute scores
    result["issues"] = collect_issues(domain, result)
    result["scores"] = compute_scores(result)

    critical_count = sum(1 for i in result["issues"] if i["severity"] == "CRITICAL")
    print(f"\n  ✓ Done: {domain} — Overall: {result['scores']['overall_score']}/100 "
          f"({result['scores']['traffic_light']}) — {len(result['issues'])} issues "
          f"({critical_count} critical)")

    return result


def _aggregate_deep_scan(landing_pages_audit, domain):
    """Aggregate on-page deep scan results across all crawled pages."""
    summary = {
        "pages_crawled": len(landing_pages_audit),
        "missing_title": 0,
        "title_too_short_long": 0,
        "missing_meta_description": 0,
        "missing_h1": 0,
        "multiple_h1": 0,
        "missing_canonical": 0,
        "non_https": 0,
        "pages_with_broken_links": 0,
        "images_missing_alt": 0,
        "total_broken_links": 0,
        "total_load_times": [],
        "avg_page_load_ms": 0,
    }
    issues = []

    for page in landing_pages_audit:
        url = page["url"]
        data = page["data"]
        if data.get("error"):
            continue

        title = data.get("meta_title", "")
        desc = data.get("meta_description", "")
        h1_list = data.get("h1_list", [])
        canonical = data.get("canonical_url", "")
        has_canonical = data.get("has_canonical", False)
        is_https = data.get("is_https", False)
        broken = data.get("broken_links", 0) or 0
        load_ms = data.get("page_load_time_ms", 0) or 0

        if not title:
            summary["missing_title"] += 1
            issues.append({"url": url, "issue": "Missing meta title",
                          "severity": "CRITICAL"})
        elif len(title) < 30 or len(title) > 65:
            summary["title_too_short_long"] += 1
            issues.append({"url": url, "issue": f"Title length suboptimal ({len(title)} chars)",
                          "severity": "MEDIUM"})

        if not desc:
            summary["missing_meta_description"] += 1
            issues.append({"url": url, "issue": "Missing meta description",
                          "severity": "HIGH"})

        if not h1_list:
            summary["missing_h1"] += 1
            issues.append({"url": url, "issue": "Missing H1", "severity": "CRITICAL"})
        elif len(h1_list) > 1:
            summary["multiple_h1"] += 1
            issues.append({"url": url, "issue": f"Multiple H1s ({len(h1_list)})",
                          "severity": "HIGH"})

        if not canonical and not has_canonical:
            summary["missing_canonical"] += 1
            issues.append({"url": url, "issue": "Missing canonical URL",
                          "severity": "HIGH"})

        if not is_https:
            summary["non_https"] += 1
            issues.append({"url": url, "issue": "Page not served over HTTPS",
                          "severity": "CRITICAL"})

        if broken > 0:
            summary["pages_with_broken_links"] += 1
            summary["total_broken_links"] += broken
            issues.append({"url": url, "issue": f"Broken links ({broken})",
                          "severity": "HIGH"})

        if load_ms > 0:
            summary["total_load_times"].append(load_ms)
            if load_ms > 3000:
                issues.append({"url": url, "issue": f"Slow page load ({load_ms}ms)",
                              "severity": "MEDIUM"})

    if summary["total_load_times"]:
        summary["avg_page_load_ms"] = round(
            sum(summary["total_load_times"]) / len(summary["total_load_times"]))

    return {"pages_crawled": summary["pages_crawled"], "summary": summary,
            "issues": issues}


def audit_portfolio(domains, keywords_map=None, location_code=DEFAULT_LOCATION_CODE,
                    language_code=DEFAULT_LANGUAGE_CODE):
    """Run audit_domain for all domains and aggregate results."""
    results = []
    for domain in domains:
        kws = (keywords_map or {}).get(domain, DEFAULT_KEYWORDS)
        try:
            result = audit_domain(domain, kws, location_code, language_code)
            results.append(result)
        except Exception as e:
            print(f"  ✗ ERROR auditing {domain}: {e}", file=sys.stderr)
            results.append({
                "domain": domain,
                "error": str(e),
                "scores": {"overall_score": 0, "traffic_light": "red",
                          "technical_score": 0, "visibility_score": 0,
                          "backlink_risk_score": 100},
                "issues": [{"severity": "CRITICAL", "category": "Technical",
                           "issue": f"Audit failed: {e}", "fix": "Retry audit",
                           "impact": "HIGH", "effort": "LOW"}],
                "on_page": {}, "lighthouse": {}, "domain_rank": {},
                "backlinks_summary": {}, "backlinks_timeseries": {},
                "serp_features": {}, "landing_pages": [],
                "on_page_deep_scan": {},
            })
    return results


# ── HTML Dashboard generation ─────────────────────────────────────────────


def _risk_label(risk_score):
    if risk_score >= 60:
        return "HIGH", "badge-red"
    if risk_score >= 30:
        return "MEDIUM", "badge-amber"
    return "LOW", "badge-green"


def _sev_badge(severity):
    return {"CRITICAL": "badge-red", "HIGH": "badge-amber",
            "MEDIUM": "badge-gray"}.get(severity, "badge-gray")


def _lighthouse_color(score):
    if score is None:
        return "score-gray"
    if score >= 90:
        return "score-green"
    if score >= 50:
        return "score-amber"
    return "score-red"


def _format_lcp(ms):
    if ms is None:
        return "N/A"
    if ms >= 4000:
        return f"{ms/1000:.1f}s"
    return f"{ms/1000:.2f}s"


def _format_tbt(ms):
    if ms is None:
        return "N/A"
    return f"{ms:.0f}ms"


def _format_cls(cls):
    if cls is None:
        return "N/A"
    return f"{cls:.3f}"


def _cwv_lcp_color(ms):
    if ms is None:
        return "score-gray"
    if ms <= 2500:
        return "score-green"
    if ms <= 4000:
        return "score-amber"
    return "score-red"


def _cwv_tbt_color(ms):
    if ms is None:
        return "score-gray"
    if ms <= 200:
        return "score-green"
    if ms <= 600:
        return "score-amber"
    return "score-red"


def _cwv_cls_color(cls):
    if cls is None:
        return "score-gray"
    if cls <= 0.1:
        return "score-green"
    if cls <= 0.25:
        return "score-amber"
    return "score-red"


def _flag(passed, label_pass, label_fail):
    if passed:
        return "flag-pass", "✓", label_pass
    return "flag-fail", "✗", label_fail


def _slug(domain):
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


def _format_number(n):
    if n is None:
        return "N/A"
    if isinstance(n, (int, float)):
        if n >= 1000000:
            return f"{n/1000000:.1f}M"
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return str(int(n))
    return str(n)


def generate_portfolio_dashboard(results, output_path):
    """Generate an HTML dashboard with portfolio overview, Top 10 fixes, and per-brand details."""
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    html_file = output_dir / f"portfolio_audit_{date_str}.html"
    json_file = output_dir / f"portfolio_audit_{date_str}.json"

    # Save JSON for re-use
    with open(json_file, "w") as f:
        json.dump(results, f, indent=2, default=str)

    generated_date = datetime.now().strftime("%B %d, %Y")

    # ── Portfolio overview rows ──
    portfolio_rows = ""
    red_count = amber_count = green_count = 0
    for r in results:
        domain = r["domain"]
        scores = r.get("scores", {})
        tech = scores.get("technical_score", 0)
        vis = scores.get("visibility_score", 0)
        risk = scores.get("backlink_risk_score", 0)
        overall = scores.get("overall_score", 0)
        tl = scores.get("traffic_light", "red")

        if tl == "green":
            green_count += 1
        elif tl == "amber":
            amber_count += 1
        else:
            red_count += 1

        risk_label, risk_badge = _risk_label(risk)
        tech_color = _score_color(tech)
        vis_color = _score_color(vis)
        displacement = r.get("serp_features", {}).get("displacement_rate", 0)
        critical_count = sum(1 for i in r.get("issues", [])
                             if i["severity"] == "CRITICAL")

        portfolio_rows += f"""<tr>
            <td>
              <div class="client-name">{domain}</div>
              <div class="domain">{domain}</div>
            </td>
            <td><span class="score-cell {tech_color}">{tech}/100</span></td>
            <td><span class="score-cell {vis_color}">{vis}/100</span></td>
            <td><span class="badge {risk_badge}">{risk_label}</span></td>
            <td>{displacement}% of keywords</td>
            <td><span class="badge badge-red">{critical_count} issues</span></td>
            <td>
              <div class="overall-cell">
                <span class="dot dot-{tl}"></span>
                {overall}/100
              </div>
            </td>
          </tr>\n"""

    # ── Portfolio takeaway ──
    takeaway = (f"{red_count} of {len(results)} brands are in the red, "
                f"{amber_count} amber, {green_count} green. ")
    worst = max(results, key=lambda r: r.get("scores", {}).get("backlink_risk_score", 0))
    best = max(results, key=lambda r: r.get("scores", {}).get("overall_score", 0))
    takeaway += (f"{best['domain']} has the highest overall health score "
                 f"({best['scores']['overall_score']}/100). ")
    if red_count > 0:
        red_brands = [r["domain"] for r in results
                      if r.get("scores", {}).get("traffic_light") == "red"]
        takeaway += f"Critical brands needing immediate attention: {', '.join(red_brands)}."

    # ── Cross-portfolio Top 10 fixes ──
    all_issues = []
    for r in results:
        for issue in r.get("issues", []):
            all_issues.append({**issue, "domain": r["domain"]})

    # Group similar issues across brands
    issue_groups = defaultdict(list)
    for i in all_issues:
        # Use issue text as the grouping key (normalize)
        key = re.sub(r'\d+', '#', i["issue"])  # normalize numbers
        issue_groups[key].append(i)

    # Rank by severity and cross-brand frequency
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    ranked = sorted(issue_groups.items(),
                    key=lambda kv: (sev_rank.get(kv[1][0]["severity"], 3),
                                    -len(set(i["domain"] for i in kv[1]))))

    top10_items = ""
    for rank, (key, group) in enumerate(ranked[:10], 1):
        domains_affected = sorted(set(i["domain"] for i in group))
        first = group[0]
        sev = first["severity"]
        domains_str = ", ".join(domains_affected[:3])
        if len(domains_affected) > 3:
            domains_str += f" +{len(domains_affected) - 3} more"

        top10_items += f"""<li class="priority-item">
        <div class="priority-num">{rank}</div>
        <div>
          <div class="priority-client-label">{domains_str}</div>
          <div class="priority-issue">{first['issue']}</div>
          <div class="priority-fix">→ {first['fix']}</div>
        </div>
        <div class="priority-badges">
          <span class="badge {_sev_badge(sev)}">{sev}</span>
          <span class="badge badge-blue">{first['impact']} impact</span>
          <span class="badge badge-gray">{first['effort']} effort</span>
        </div>
      </li>\n"""

    if not top10_items:
        top10_items = "<li class='priority-item'><div class='priority-num'>—</div>" \
                      "<div><div class='priority-issue'>No issues found</div></div></li>"

    top10_takeaway = ("Focus on CRITICAL issues first — they affect indexability and "
                      "should be resolved before any content investment. "
                      "Cross-brand patterns indicate systemic issues worth a template fix.")

    # ── Per-brand detail sections ──
    client_sections = ""
    for r in results:
        domain = r["domain"]
        scores = r.get("scores", {})
        tl = scores.get("traffic_light", "red")
        tech = scores.get("technical_score", 0)
        vis = scores.get("visibility_score", 0)
        risk = scores.get("backlink_risk_score", 0)
        risk_label, _ = _risk_label(risk)

        lh = r.get("lighthouse", {})
        on_page = r.get("on_page", {})
        bl = r.get("backlinks_summary", {})
        ts = r.get("backlinks_timeseries", {})
        dr = r.get("domain_rank", {})
        serp = r.get("serp_features", {})
        deep_scan = r.get("on_page_deep_scan", {})

        slug = _slug(domain)
        domain_not_found = on_page.get("error") and on_page.get("status_code") == 40501

        # Lighthouse scores
        perf = lh.get("performance_score")
        seo_s = lh.get("seo_score")
        a11y = lh.get("accessibility_score")
        bp = lh.get("best_practices_score")

        # CWV
        lcp = lh.get("largest_contentful_paint_ms")
        tbt = lh.get("total_blocking_time_ms")
        cls = lh.get("cumulative_layout_shift")

        # Flags
        https_flag = _flag(on_page.get("is_https", False), "HTTPS", "HTTPS")
        robots_flag = _flag(on_page.get("is_robots_allowed", False), "robots.txt", "robots.txt")
        canonical_flag = _flag(bool(on_page.get("canonical_url")), "Canonical", "Canonical")
        h1_flag = _flag(bool(on_page.get("h1_list")), "H1", "H1")
        title_flag = _flag(bool(on_page.get("meta_title")), "Meta title", "Meta title")
        desc_flag = _flag(bool(on_page.get("meta_description")), "Meta description", "Meta description")

        # Alert banners
        alert_html = ""
        if domain_not_found:
            alert_html += ('<div class="alert-banner alert-critical">'
                           '⛔ Domain unresolvable — site returned "not found" on crawl. '
                           'This is the #1 priority.</div>\n')
        if ts.get("high_risk", False):
            alert_html += (f'<div class="alert-banner alert-high">⚠️ Backlink spike detected — '
                           f'{ts.get("lost_in_period", 0)} referring domains lost in last '
                           f'30 days ({ts.get("lost_pct", 0)}% of total).</div>\n')

        # On-page deep scan summary
        ds_summary = deep_scan.get("summary", {}) if deep_scan else {}
        pages_crawled = deep_scan.get("pages_crawled", 0) if deep_scan else 0

        onpage_stats_grid = ""
        stat_defs = [
            ("missing_title", "Missing title"),
            ("title_too_short_long", "Title too short / long"),
            ("missing_meta_description", "Missing meta description"),
            ("missing_h1", "Missing H1"),
            ("multiple_h1", "Multiple H1s"),
            ("missing_canonical", "Missing canonical"),
            ("non_https", "Non-HTTPS pages"),
            ("pages_with_broken_links", "Pages with broken links"),
        ]
        for key, label in stat_defs:
            val = ds_summary.get(key, 0)
            cls_ = "has-issues" if val > 0 else "ok"
            onpage_stats_grid += f"""<div class="onpage-stat-card">
            <div class="onpage-stat-num {cls_}">{val}</div>
            <div class="onpage-stat-label">{label}</div>
          </div>\n"""

        avg_load = ds_summary.get("avg_page_load_ms", 0)
        onpage_stats_grid += f"""<div class="onpage-stat-card">
            <div class="onpage-stat-num">{avg_load} ms</div>
            <div class="onpage-stat-label">Avg page load time</div>
          </div>\n"""

        # On-page issues table
        onpage_issues_html = ""
        ds_issues = deep_scan.get("issues", []) if deep_scan else []
        if ds_issues:
            rows = ""
            for issue in ds_issues[:50]:  # cap at 50 rows
                rows += f"""<tr>
              <td><div class="page-url">{issue['url']}</div></td>
              <td>{issue['issue']}</td>
              <td><span class="badge {_sev_badge(issue['severity'])}">{issue['severity']}</span></td>
            </tr>\n"""
            onpage_issues_html = f"""<table class="onpage-issues-table">
          <thead><tr><th>Page URL</th><th>Issue</th><th style="width:90px">Severity</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
        else:
            onpage_issues_html = "<p style='color:var(--gray);font-size:12px'>No per-page issues found.</p>"

        # Action items
        action_items_html = ""
        if ds_issues:
            # Sort by severity
            sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
            sorted_actions = sorted(ds_issues, key=lambda i: sev_order.get(i["severity"], 3))
            # Group by issue
            action_groups = defaultdict(list)
            for i in sorted_actions:
                action_groups[i["issue"]].append(i)

            action_items = ""
            for idx, (issue_text, group) in enumerate(list(action_groups.items())[:15], 1):
                sev = group[0]["severity"]
                urls = [g["url"] for g in group]
                pages_str = f"{len(urls)} pages" if len(urls) > 2 else ", ".join(urls)
                action_items += f"""<li class="action-item">
              <div class="action-num">{idx}</div>
              <div>
                <div class="action-issue">{issue_text}</div>
                <div class="action-pages">Affects: {pages_str}</div>
              </div>
              <span class="badge {_sev_badge(sev)}">{sev}</span>
            </li>\n"""
            action_items_html = f'<ol class="action-items-list">{action_items}</ol>'
        else:
            action_items_html = "<p style='color:var(--gray);font-size:12px'>No action items.</p>"

        # Top issues table
        issue_rows = ""
        for issue in r.get("issues", [])[:8]:
            issue_rows += f"""<tr>
              <td><span class="badge {_sev_badge(issue['severity'])}">{issue['severity']}</span></td>
              <td><span class="badge badge-gray">{issue['category']}</span></td>
              <td><div class="issue-text">{issue['issue']}</div></td>
              <td><div class="fix-text">{issue['fix']}</div></td>
            </tr>\n"""
        if not issue_rows:
            issue_rows = "<tr><td colspan='4' style='color:var(--gray)'>No issues found.</td></tr>"

        # SERP features
        serp_rows = ""
        feature_counts = serp.get("feature_counts", {})
        total_kw = len(serp.get("client_positions", []))
        if feature_counts and total_kw > 0:
            for feat, count in feature_counts.most_common(10):
                feat_display = feat.replace("_", " ").title()
                pct = round(count / total_kw * 100)
                serp_rows += f"""<li class="serp-item">
            <span class="serp-feature-name">{feat_display}</span>
            <span class="serp-pct">{pct}% of tracked keywords</span>
          </li>\n"""
        if not serp_rows:
            serp_rows = "<li class='serp-item'><span class='serp-feature-name'>No SERP features displacing rankings</span></li>"

        # Copy fixes
        copy_fixes_lines = []
        for issue in r.get("issues", []):
            copy_fixes_lines.append(
                f"[{issue['severity']}] {issue['issue']}\n  Fix: {issue['fix']}")
        copy_fixes_text = "\n\n".join(copy_fixes_lines) or "No issues found."

        # Client takeaway
        client_takeaway = (f"Overall health: {scores.get('overall_score', 0)}/100 "
                           f"({tl}). {len(r.get('issues', []))} issues found, "
                           f"{sum(1 for i in r.get('issues', []) if i['severity'] == 'CRITICAL')} critical. "
                           f"Traffic estimate: {_format_number(dr.get('organic_etv', 0))}/mo, "
                           f"{_format_number(dr.get('organic_count', 0))} ranked keywords, "
                           f"{_format_number(bl.get('referring_domains', 0))} referring domains.")

        # Build client section
        client_sections += f"""<details class="client-section">
    <summary>
      <span class="dot dot-{tl}"></span>
      <span class="summary-name">{domain}</span>
      <span class="summary-domain">{domain}</span>
      <div class="summary-scores">
        Tech {tech} · Vis {vis} · {risk_label} risk
      </div>
      <span class="chevron">▶</span>
    </summary>
    <div class="client-body">
      {alert_html}
      <div class="client-sub-title">Lighthouse Scores</div>
      <div class="scores-row">
        <div class="score-card"><div class="score-val {_lighthouse_color(perf)}">{perf if perf is not None else 'N/A'}</div><div class="score-label">Performance</div></div>
        <div class="score-card"><div class="score-val {_lighthouse_color(seo_s)}">{seo_s if seo_s is not None else 'N/A'}</div><div class="score-label">SEO</div></div>
        <div class="score-card"><div class="score-val {_lighthouse_color(a11y)}">{a11y if a11y is not None else 'N/A'}</div><div class="score-label">Accessibility</div></div>
        <div class="score-card"><div class="score-val {_lighthouse_color(bp)}">{bp if bp is not None else 'N/A'}</div><div class="score-label">Best Practices</div></div>
      </div>
      <div class="client-sub-title">Core Web Vitals</div>
      <div class="cwv-row">
        <div class="cwv-card"><div class="cwv-icon">🖼</div><div><div class="cwv-val {_cwv_lcp_color(lcp)}">{_format_lcp(lcp)}</div><div class="cwv-name">LCP</div></div></div>
        <div class="cwv-card"><div class="cwv-icon">⏱</div><div><div class="cwv-val {_cwv_tbt_color(tbt)}">{_format_tbt(tbt)}</div><div class="cwv-name">TBT</div></div></div>
        <div class="cwv-card"><div class="cwv-icon">📐</div><div><div class="cwv-val {_cwv_cls_color(cls)}">{_format_cls(cls)}</div><div class="cwv-name">CLS</div></div></div>
      </div>
      <div class="client-sub-title">Indexability</div>
      <div class="flags-row">
        <span class="flag {https_flag[0]}">{https_flag[1]} {https_flag[2]}</span>
        <span class="flag {robots_flag[0]}">{robots_flag[1]} {robots_flag[2]}</span>
        <span class="flag {canonical_flag[0]}">{canonical_flag[1]} {canonical_flag[2]}</span>
        <span class="flag {h1_flag[0]}">{h1_flag[1]} {h1_flag[2]}</span>
        <span class="flag {title_flag[0]}">{title_flag[1]} {title_flag[2]}</span>
        <span class="flag {desc_flag[0]}">{desc_flag[1]} {desc_flag[2]}</span>
      </div>
      <div class="client-sub-title">Technical On-Page Deep Scan ({pages_crawled} pages crawled)</div>
      {onpage_stats_grid}
      <div class="client-sub-title" style="margin-top:14px">On-Page Issues by Page</div>
      {onpage_issues_html}
      <div class="client-sub-title" style="margin-top:14px">Action Items (prioritized)</div>
      {action_items_html}
      <div class="client-sub-title">Top Issues (all categories)</div>
      <table class="issues-table">
        <thead><tr><th style="width:90px">Severity</th><th style="width:90px">Category</th><th>Issue</th><th>Fix</th></tr></thead>
        <tbody>{issue_rows}</tbody>
      </table>
      <div class="client-sub-title">Organic Visibility</div>
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-val">{_format_number(dr.get('organic_etv', 0))}</div><div class="stat-label">Est. Monthly Organic Visits</div></div>
        <div class="stat-card"><div class="stat-val">{_format_number(dr.get('organic_count', 0))}</div><div class="stat-label">Ranked Keywords</div></div>
        <div class="stat-card"><div class="stat-val">{_format_number(bl.get('referring_domains', 0))}</div><div class="stat-label">Referring Domains</div></div>
      </div>
      <div class="client-sub-title">SERP Feature Displacement ({serp.get('displacement_rate', 0)}% of keywords)</div>
      <ul class="serp-list">{serp_rows}</ul>
      <div class="client-sub-title">Backlink Health</div>
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-val">{_format_number(bl.get('referring_domains', 0))}</div><div class="stat-label">Referring Domains</div></div>
        <div class="stat-card"><div class="stat-val">{_format_number(bl.get('backlinks', 0))}</div><div class="stat-label">Total Backlinks</div></div>
        <div class="stat-val" style="display:none">{bl.get('spam_score', 0)}</div>
        <div class="stat-card"><div class="stat-val {_lighthouse_color(bl.get('spam_score', 0))}">{bl.get('spam_score', 0)}/100</div><div class="stat-label">Spam Score</div></div>
      </div>
      <div class="copy-block">
        <div class="copy-block-header">
          <span class="copy-block-title">📋 Copy fixes for {domain}</span>
          <button class="copy-btn" onclick="copyFixes('fixes-{slug}')">Copy</button>
        </div>
        <pre id="fixes-{slug}">{copy_fixes_text}</pre>
      </div>
      <div class="client-takeaway">{client_takeaway}</div>
    </div>
  </details>\n"""

    # ── Assemble full HTML ──
    html = _build_html(
        agency_name="Zenfusion",
        report_period=datetime.now().strftime("%B %Y"),
        generated_date=generated_date,
        client_count=len(results),
        portfolio_rows=portfolio_rows,
        portfolio_takeaway=takeaway,
        top10_items=top10_items,
        top10_takeaway=top10_takeaway,
        client_sections=client_sections,
    )

    with open(html_file, "w") as f:
        f.write(html)

    return str(html_file), str(json_file)


def _build_html(agency_name, report_period, generated_date, client_count,
                portfolio_rows, portfolio_takeaway, top10_items, top10_takeaway,
                client_sections):
    """Build the full HTML dashboard with white SaaS theme."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{agency_name} — Portfolio SEO Audit — {report_period}</title>
  <style>
    :root {{
      --brand:        #1B2A4A;
      --brand-light:  #2C3E6B;
      --accent:       #4A90D9;
      --accent-light: #EBF5FB;
      --green:        #27AE60;
      --green-bg:     #EAFAF1;
      --amber:        #F39C12;
      --amber-bg:     #FEF9E7;
      --red:          #E74C3C;
      --red-bg:       #FDEDEC;
      --gray:         #7F8C8D;
      --gray-bg:      #F0F0F0;
      --bg:           #F4F6F9;
      --card:         #FFFFFF;
      --text:         #2C3E50;
      --border:       #E0E6ED;
      --font:         -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: var(--font); background: var(--bg); color: var(--text);
           font-size: 14px; line-height: 1.5; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .site-header {{ background: var(--brand); color: white; padding: 20px 40px;
                    display: flex; align-items: center; gap: 20px; }}
    .site-header .header-text h1 {{ font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
    .site-header .header-meta {{ font-size: 12px; opacity: 0.65; margin-top: 3px; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 32px 40px; }}
    .section-title {{ font-size: 16px; font-weight: 700; color: var(--brand);
      margin: 36px 0 14px; padding-bottom: 8px; border-bottom: 2px solid var(--accent);
      display: flex; align-items: center; gap: 8px; }}
    .section-title:first-of-type {{ margin-top: 0; }}
    .takeaway {{ background: var(--accent-light); border-left: 4px solid var(--accent);
      padding: 10px 16px; border-radius: 0 6px 6px 0; font-size: 13px; margin-top: 12px; }}
    .takeaway strong {{ color: var(--brand); }}
    .dot {{ display: inline-block; width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }}
    .dot-green {{ background: var(--green); }}
    .dot-amber {{ background: var(--amber); }}
    .dot-red {{ background: var(--red); }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
      font-size: 11px; font-weight: 700; letter-spacing: 0.3px; }}
    .badge-red {{ background: var(--red-bg); color: var(--red); }}
    .badge-amber {{ background: var(--amber-bg); color: #B7770D; }}
    .badge-green {{ background: var(--green-bg); color: var(--green); }}
    .badge-gray {{ background: var(--gray-bg); color: var(--gray); }}
    .badge-blue {{ background: var(--accent-light); color: var(--accent); }}
    .summary-wrap {{ border-radius: 10px; overflow: hidden; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }}
    .summary-table {{ width: 100%; border-collapse: collapse; background: var(--card); }}
    .summary-table thead tr {{ background: var(--brand); }}
    .summary-table th {{ color: white; padding: 10px 14px; text-align: left;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; white-space: nowrap; }}
    .summary-table td {{ padding: 11px 14px; font-size: 13px;
      border-bottom: 1px solid var(--border); vertical-align: middle; }}
    .summary-table tbody tr:last-child td {{ border-bottom: none; }}
    .summary-table tbody tr:hover td {{ background: #F7FAFF; }}
    .summary-table .client-name {{ font-weight: 600; }}
    .summary-table .domain {{ font-size: 11px; color: var(--gray); margin-top: 1px; }}
    .score-cell {{ font-weight: 700; font-size: 15px; }}
    .overall-cell {{ display: flex; align-items: center; gap: 7px; font-weight: 700; }}
    .priority-list {{ list-style: none; }}
    .priority-item {{ background: var(--card); border-radius: 8px; padding: 14px 18px;
      margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.07);
      display: grid; grid-template-columns: 36px 1fr auto; gap: 14px; align-items: start; }}
    .priority-num {{ font-size: 22px; font-weight: 800; color: var(--accent); line-height: 1; padding-top: 2px; }}
    .priority-client-label {{ font-size: 11px; font-weight: 700; color: var(--gray);
      text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px; }}
    .priority-issue {{ font-weight: 600; font-size: 13px; margin-bottom: 4px; }}
    .priority-fix {{ font-size: 12px; color: var(--gray); }}
    .priority-badges {{ display: flex; flex-direction: column; gap: 5px; align-items: flex-end; }}
    details.client-section {{ background: var(--card); border-radius: 10px;
      margin-bottom: 12px; box-shadow: 0 1px 5px rgba(0,0,0,0.07); overflow: hidden; }}
    details.client-section summary {{ padding: 16px 22px; cursor: pointer; list-style: none;
      display: flex; align-items: center; gap: 12px; user-select: none; }}
    details.client-section summary::-webkit-details-marker {{ display: none; }}
    details.client-section summary .summary-name {{ font-weight: 700; font-size: 15px; flex: 1; }}
    details.client-section summary .summary-domain {{ font-size: 12px; color: var(--gray); }}
    details.client-section summary .summary-scores {{ display: flex; gap: 10px;
      align-items: center; font-size: 12px; color: var(--gray); }}
    details.client-section summary .chevron {{ font-size: 11px; color: var(--gray); transition: transform 0.2s; }}
    details.client-section[open] summary .chevron {{ transform: rotate(90deg); }}
    details.client-section[open] summary {{ border-bottom: 1px solid var(--border); }}
    .client-body {{ padding: 20px 22px 24px; }}
    .scores-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }}
    .score-card {{ background: var(--bg); border-radius: 8px; padding: 14px 12px;
      text-align: center; border: 1px solid var(--border); }}
    .score-card .score-val {{ font-size: 30px; font-weight: 800; line-height: 1; margin-bottom: 4px; }}
    .score-card .score-label {{ font-size: 11px; color: var(--gray); font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.4px; }}
    .score-green {{ color: var(--green); }}
    .score-amber {{ color: var(--amber); }}
    .score-red {{ color: var(--red); }}
    .score-gray {{ color: var(--gray); }}
    .cwv-row {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 20px; }}
    .cwv-card {{ background: var(--bg); border-radius: 8px; padding: 12px 14px;
      border: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }}
    .cwv-icon {{ font-size: 18px; }}
    .cwv-val {{ font-size: 17px; font-weight: 700; }}
    .cwv-name {{ font-size: 11px; color: var(--gray); }}
    .client-sub-title {{ font-size: 12px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.5px; color: var(--brand); margin: 18px 0 8px; }}
    .client-sub-title:first-child {{ margin-top: 0; }}
    .issues-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .issues-table th {{ text-align: left; padding: 7px 10px; background: var(--bg);
      color: var(--gray); font-weight: 700; font-size: 11px; text-transform: uppercase;
      letter-spacing: 0.4px; border-bottom: 1px solid var(--border); }}
    .issues-table td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); }}
    .issues-table tr:last-child td {{ border-bottom: none; }}
    .issues-table .issue-text {{ font-weight: 600; margin-bottom: 2px; }}
    .issues-table .fix-text {{ color: var(--gray); }}
    .flags-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 4px; }}
    .flag {{ display: flex; align-items: center; gap: 6px; padding: 5px 10px;
      border-radius: 20px; font-size: 12px; font-weight: 600; }}
    .flag-pass {{ background: var(--green-bg); color: var(--green); }}
    .flag-fail {{ background: var(--red-bg); color: var(--red); }}
    .flag-warn {{ background: var(--amber-bg); color: #B7770D; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    .stat-card {{ background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px 14px; }}
    .stat-val {{ font-size: 20px; font-weight: 700; color: var(--text); }}
    .stat-label {{ font-size: 11px; color: var(--gray); margin-top: 2px; }}
    .serp-list {{ list-style: none; display: flex; flex-direction: column; gap: 6px; }}
    .serp-item {{ display: flex; align-items: center; justify-content: space-between;
      padding: 7px 12px; background: var(--bg); border-radius: 6px;
      font-size: 12px; border: 1px solid var(--border); }}
    .serp-feature-name {{ font-weight: 600; }}
    .serp-pct {{ color: var(--gray); }}
    .copy-block {{ background: #F0F4FF; border: 1px solid #C8D8F5;
      border-radius: 8px; padding: 14px 16px; margin-top: 20px; }}
    .copy-block-header {{ display: flex; align-items: center;
      justify-content: space-between; margin-bottom: 10px; }}
    .copy-block-title {{ font-size: 11px; font-weight: 700; color: var(--brand);
      text-transform: uppercase; letter-spacing: 0.5px; }}
    .copy-btn {{ font-size: 11px; background: var(--brand); color: white;
      border: none; border-radius: 4px; padding: 4px 10px; cursor: pointer;
      font-weight: 600; }}
    .copy-btn:hover {{ background: var(--brand-light); }}
    .copy-block pre {{ font-size: 12px; line-height: 1.7; white-space: pre-wrap;
      font-family: 'SF Mono', 'Consolas', 'Menlo', monospace; color: var(--text); }}
    .client-takeaway {{ margin-top: 16px; background: var(--accent-light);
      border-left: 3px solid var(--accent); padding: 9px 14px;
      border-radius: 0 6px 6px 0; font-size: 13px; }}
    .alert-banner {{ display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; margin-bottom: 12px; }}
    .alert-critical {{ background: var(--red-bg); color: var(--red); border: 1px solid #F5C6C2; }}
    .alert-high {{ background: var(--amber-bg); color: #B7770D; border: 1px solid #FAD9A1; }}
    footer {{ text-align: center; padding: 24px 40px; font-size: 11px;
      color: var(--gray); border-top: 1px solid var(--border); margin-top: 40px; }}
    .onpage-stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }}
    .onpage-stat-card {{ background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 12px; display: flex; align-items: center; gap: 10px; }}
    .onpage-stat-num {{ font-size: 22px; font-weight: 800; line-height: 1; min-width: 32px; }}
    .onpage-stat-num.has-issues {{ color: var(--red); }}
    .onpage-stat-num.ok {{ color: var(--green); }}
    .onpage-stat-label {{ font-size: 11px; color: var(--gray); line-height: 1.3; }}
    .onpage-issues-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .onpage-issues-table th {{ text-align: left; padding: 7px 10px; background: var(--bg);
      color: var(--gray); font-weight: 700; font-size: 11px; text-transform: uppercase;
      letter-spacing: 0.4px; border-bottom: 1px solid var(--border); }}
    .onpage-issues-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    .onpage-issues-table tr:last-child td {{ border-bottom: none; }}
    .onpage-issues-table .page-url {{ font-size: 11px; color: var(--accent); word-break: break-all; max-width: 260px; }}
    .action-items-list {{ list-style: none; display: flex; flex-direction: column; gap: 8px; }}
    .action-item {{ background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px 14px; display: grid;
      grid-template-columns: 28px 1fr auto; gap: 10px; align-items: start; }}
    .action-num {{ font-size: 16px; font-weight: 800; color: var(--brand); line-height: 1.2; }}
    .action-issue {{ font-weight: 600; font-size: 13px; margin-bottom: 3px; }}
    .action-pages {{ font-size: 11px; color: var(--gray); margin-bottom: 2px; }}
    .action-fix {{ font-size: 12px; color: var(--text); }}
    @media print {{ .copy-btn {{ display: none; }}
      details.client-section {{ break-inside: avoid; }}
      details.client-section[open] {{ display: block; }} }}
  </style>
</head>
<body>

<header class="site-header">
  <div class="header-text">
    <h1>{agency_name} — Portfolio SEO Audit</h1>
    <div class="header-meta">{report_period} &nbsp;·&nbsp; Generated {generated_date} &nbsp;·&nbsp; {client_count} brands</div>
  </div>
</header>

<div class="container">

  <div class="section-title">📊 Portfolio Overview</div>
  <div class="summary-wrap">
    <table class="summary-table">
      <thead>
        <tr>
          <th>Brand</th>
          <th>Technical Health</th>
          <th>Visibility</th>
          <th>Backlink Risk</th>
          <th>SERP Displacement</th>
          <th>Critical Issues</th>
          <th>Overall</th>
        </tr>
      </thead>
      <tbody>
        {portfolio_rows}
      </tbody>
    </table>
  </div>
  <div class="takeaway"><strong>Portfolio snapshot:</strong> {portfolio_takeaway}</div>

  <div class="section-title">🎯 Cross-Portfolio Top 10 Fixes This Week</div>
  <ol class="priority-list">
    {top10_items}
  </ol>
  <div class="takeaway"><strong>Where to focus:</strong> {top10_takeaway}</div>

  <div class="section-title">🔍 Per-Brand Detail</div>
  {client_sections}

</div>

<footer>
  {agency_name} · Portfolio SEO Audit · {report_period}<br>
  <span style="opacity:0.6">SEO Data collected via DataForSEO</span>
</footer>

<script>
  function copyFixes(id) {{
    const el = document.getElementById(id);
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {{
      const btn = el.closest('.copy-block').querySelector('.copy-btn');
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = orig, 1800);
    }});
  }}
</script>
</body>
</html>"""


# ── CLI ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Portfolio SEO Audit — audit multiple brands in one pass.")
    parser.add_argument("--domains", type=str, default=None,
                        help="Comma-separated list of domains to audit.")
    parser.add_argument("--all", action="store_true",
                        help="Audit all 7 Zenfusion brands.")
    parser.add_argument("--output", type=str, default="/tmp/portfolio_audit",
                        help="Output directory for HTML and JSON files.")
    parser.add_argument("--keywords", type=str, default=None,
                        help="Comma-separated keywords for SERP queries (applied to all domains).")
    parser.add_argument("--location-code", type=int, default=DEFAULT_LOCATION_CODE,
                        help="DataForSEO location code (default: 2840 = US).")
    parser.add_argument("--language-code", type=str, default=DEFAULT_LANGUAGE_CODE,
                        help="DataForSEO language code (default: en).")
    args = parser.parse_args()

    if args.all:
        domains = ALL_BRANDS
    elif args.domains:
        domains = [d.strip() for d in args.domains.split(",")]
    else:
        parser.error("Provide --domains or --all")

    keywords = None
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",")]

    keywords_map = {}
    if keywords:
        keywords_map = {d: keywords for d in domains}

    print(f"\n{'='*60}")
    print(f"  Portfolio SEO Audit")
    print(f"  Brands: {', '.join(domains)}")
    print(f"  Output: {args.output}")
    print(f"{'='*60}")

    results = audit_portfolio(domains, keywords_map,
                              args.location_code, args.language_code)

    print(f"\n{'='*60}")
    print(f"  Generating dashboard...")
    html_path, json_path = generate_portfolio_dashboard(results, args.output)
    print(f"\n  ✓ HTML Dashboard: {html_path}")
    print(f"  ✓ JSON Results:    {json_path}")
    print(f"\n{'='*60}")

    # Summary
    print(f"\n  Audit Summary:")
    for r in results:
        s = r.get("scores", {})
        issues = len(r.get("issues", []))
        critical = sum(1 for i in r.get("issues", []) if i["severity"] == "CRITICAL")
        print(f"    {r['domain']:40s}  Overall: {s.get('overall_score',0):3d}/100  "
              f"({s.get('traffic_light','?'):6s})  "
              f"{issues:2d} issues ({critical} critical)")


if __name__ == "__main__":
    main()
