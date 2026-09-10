#!/usr/bin/env python3
"""
Instant Page Audit — Lightweight single-URL SEO health check.

Uses DataForSEO's on_page/instant_pages endpoint (one API call, no crawling).
Returns an HTML report with issue summary cards, detailed issue table,
on-page elements overview, and quick recommendations.

Usage:
  python3 instant_page_audit.py --url https://example.com/page [--output /tmp/report]

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD  — in ~/.hermes/.env
"""

import os
import sys
import json
import base64
import ssl
import argparse
import html as html_mod
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_VERSION = "1.0"
API_BASE = "https://api.dataforseo.com/v3"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# SEO thresholds
TITLE_MIN_LEN = 30
TITLE_MAX_LEN = 60
DESC_MIN_LEN = 70
DESC_MAX_LEN = 160
PAGE_SIZE_LIMIT_BYTES = 3 * 1024 * 1024  # 3 MB


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
    """Get DataForSEO credentials from env or ~/.hermes/.env."""
    env = load_env()
    login = env.get("DATAFORSEO_LOGIN") or os.environ.get("DATAFORSEO_LOGIN")
    password = env.get("DATAFORSEO_PASSWORD") or os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.", file=sys.stderr)
        print("Set them in ~/.hermes/.env or as environment variables.", file=sys.stderr)
        sys.exit(1)
    return login, password


# ── 1. fetch_on_page_data ───────────────────────────────────────────────────

def fetch_on_page_data(url):
    """
    Call DataForSEO on_page/instant_pages for the given URL.
    Returns the parsed item dict from the API response, or None on error.
    """
    login, password = get_credentials()
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
        print(f"ERROR: API request failed: {e}", file=sys.stderr)
        return None

    if data.get("status_code") != 20000:
        print(f"ERROR: API returned status {data.get('status_code')}: "
              f"{data.get('status_message')}", file=sys.stderr)
        return None

    tasks = data.get("tasks", [])
    if not tasks or tasks[0].get("status_code") != 20000:
        task_msg = tasks[0].get("status_message", "Unknown") if tasks else "No tasks"
        task_code = tasks[0].get("status_code", "?") if tasks else "?"
        print(f"ERROR: Task failed with status {task_code}: {task_msg}", file=sys.stderr)
        return None

    try:
        items = tasks[0]["result"][0]["items"]
    except (KeyError, IndexError, TypeError):
        print("ERROR: No items in API response.", file=sys.stderr)
        return None

    if not items:
        print("ERROR: Empty items array in API response.", file=sys.stderr)
        return None

    return items[0]


# ── 2. analyze_on_page ─────────────────────────────────────────────────────

def analyze_on_page(data, url):
    """
    Analyze the DataForSEO instant_pages item for common SEO issues.
    Returns a list of issue dicts: {severity, category, issue, description, fix}.
    Severity: 'critical' | 'warning' | 'pass'
    """
    issues = []
    meta = data.get("meta", {}) or {}
    checks = data.get("checks", {}) or {}
    item_url = data.get("url", url)

    # --- Title tag ---
    title = meta.get("title") or ""
    title_len = meta.get("title_length", len(title))

    if checks.get("no_title") or not title:
        issues.append({
            "severity": "critical",
            "category": "Title",
            "issue": "Missing title tag",
            "description": "The page has no <title> tag. Title tags are critical for SEO and search result display.",
            "fix": "Add a descriptive, keyword-rich <title> tag (30-60 characters).",
        })
    elif checks.get("title_too_long") or title_len > TITLE_MAX_LEN:
        issues.append({
            "severity": "warning",
            "category": "Title",
            "issue": f"Title too long ({title_len} chars)",
            "description": f"Title is {title_len} characters, exceeding the recommended maximum of {TITLE_MAX_LEN}. Search engines may truncate it.",
            "fix": f"Shorten the title to {TITLE_MIN_LEN}-{TITLE_MAX_LEN} characters while keeping primary keywords.",
        })
    elif checks.get("title_too_short") or title_len < TITLE_MIN_LEN:
        issues.append({
            "severity": "warning",
            "category": "Title",
            "issue": f"Title too short ({title_len} chars)",
            "description": f"Title is only {title_len} characters. Short titles may not provide enough context for search engines.",
            "fix": f"Expand the title to {TITLE_MIN_LEN}-{TITLE_MAX_LEN} characters with relevant keywords.",
        })
    else:
        issues.append({
            "severity": "pass",
            "category": "Title",
            "issue": "Title tag optimal",
            "description": f"Title is {title_len} characters — within the recommended range.",
            "fix": "No action needed.",
        })

    # --- Meta description ---
    desc = meta.get("description") or ""
    desc_len = meta.get("description_length", len(desc))

    if checks.get("no_description") or not desc:
        issues.append({
            "severity": "critical",
            "category": "Meta Description",
            "issue": "Missing meta description",
            "description": "The page has no meta description. This appears in search results and influences click-through rate.",
            "fix": f"Add a compelling meta description ({DESC_MIN_LEN}-{DESC_MAX_LEN} characters) with target keywords.",
        })
    elif desc_len > DESC_MAX_LEN:
        issues.append({
            "severity": "warning",
            "category": "Meta Description",
            "issue": f"Meta description too long ({desc_len} chars)",
            "description": f"Description is {desc_len} characters, exceeding the recommended maximum of {DESC_MAX_LEN}. Search engines may truncate.",
            "fix": f"Shorten the meta description to {DESC_MIN_LEN}-{DESC_MAX_LEN} characters.",
        })
    elif desc_len < DESC_MIN_LEN and desc_len > 0:
        issues.append({
            "severity": "warning",
            "category": "Meta Description",
            "issue": f"Meta description too short ({desc_len} chars)",
            "description": f"Description is only {desc_len} characters. May not provide enough snippet text for search results.",
            "fix": f"Expand the meta description to {DESC_MIN_LEN}-{DESC_MAX_LEN} characters.",
        })
    else:
        issues.append({
            "severity": "pass",
            "category": "Meta Description",
            "issue": "Meta description optimal",
            "description": f"Description is {desc_len} characters — within the recommended range.",
            "fix": "No action needed.",
        })

    # --- H1 tags ---
    htags = meta.get("htags") or {}
    h1_list = htags.get("h1", []) if htags else []

    if checks.get("no_h1_tag") or not h1_list:
        issues.append({
            "severity": "critical",
            "category": "Headings",
            "issue": "Missing H1 tag",
            "description": "The page has no H1 heading. H1 is a primary on-page SEO signal.",
            "fix": "Add exactly one H1 tag containing the page's primary keyword.",
        })
    elif len(h1_list) > 1:
        issues.append({
            "severity": "warning",
            "category": "Headings",
            "issue": f"Multiple H1 tags ({len(h1_list)})",
            "description": f"Page has {len(h1_list)} H1 tags. Best practice is exactly one H1 per page.",
            "fix": "Use only one H1 tag; convert additional H1s to H2 or H3 as appropriate.",
        })
    else:
        issues.append({
            "severity": "pass",
            "category": "Headings",
            "issue": "Single H1 tag present",
            "description": f"H1: \"{h1_list[0][:80]}\"",
            "fix": "No action needed.",
        })

    # --- Canonical ---
    canonical = meta.get("canonical") or ""

    if checks.get("canonical") and canonical:
        issues.append({
            "severity": "pass",
            "category": "Canonical",
            "issue": "Canonical tag present",
            "description": f"Canonical URL: {canonical}",
            "fix": "No action needed.",
        })
    else:
        issues.append({
            "severity": "warning",
            "category": "Canonical",
            "issue": "Missing canonical tag",
            "description": "No canonical link tag found. Canonical tags prevent duplicate content issues.",
            "fix": 'Add <link rel="canonical" href="https://your-canonical-url.com" /> to the page head.',
        })

    # --- Hreflang ---
    # DataForSEO instant_pages does not directly report hreflang tags.
    # We check for it as a note since the endpoint doesn't include it.
    issues.append({
        "severity": "warning",
        "category": "Hreflang",
        "issue": "Hreflang not available via instant API",
        "description": "The DataForSEO instant_pages endpoint does not return hreflang tag data. "
                       "For multi-region sites, verify hreflang tags manually.",
        "fix": 'If targeting multiple languages/regions, add <link rel="alternate" hreflang="xx-YY" href="..." /> tags.',
    })

    # --- HTTPS ---
    if checks.get("is_https"):
        issues.append({
            "severity": "pass",
            "category": "HTTPS",
            "issue": "Page served over HTTPS",
            "description": "The page is served over a secure HTTPS connection.",
            "fix": "No action needed.",
        })
    else:
        issues.append({
            "severity": "critical",
            "category": "HTTPS",
            "issue": "Page not served over HTTPS",
            "description": "The page is not using HTTPS. Search engines prioritize secure pages.",
            "fix": "Install an SSL certificate and redirect all HTTP traffic to HTTPS.",
        })

    # --- Images missing alt text ---
    images_count = meta.get("images_count", 0) or 0
    if checks.get("no_image_alt"):
        issues.append({
            "severity": "warning",
            "category": "Images",
            "issue": f"Images missing alt text ({images_count} images on page)",
            "description": "One or more images on the page are missing alt text. Alt text improves accessibility and image SEO.",
            "fix": "Add descriptive alt attributes to all images. Use empty alt=\"\" for purely decorative images.",
        })
    elif images_count > 0:
        issues.append({
            "severity": "pass",
            "category": "Images",
            "issue": f"All images have alt text ({images_count} images)",
            "description": "All images on the page have alt text attributes.",
            "fix": "No action needed.",
        })
    else:
        issues.append({
            "severity": "pass",
            "category": "Images",
            "issue": "No images on page",
            "description": "The page contains no images.",
            "fix": "No action needed.",
        })

    # --- Schema / Structured data ---
    if checks.get("has_micromarkup"):
        if checks.get("has_micromarkup_errors"):
            issues.append({
                "severity": "warning",
                "category": "Schema",
                "issue": "Structured data has errors",
                "description": "The page has schema.org markup but it contains validation errors.",
                "fix": "Test your structured data with Google's Rich Results Test and fix any errors.",
            })
        else:
            issues.append({
                "severity": "pass",
                "category": "Schema",
                "issue": "Structured data present and valid",
                "description": "The page has schema.org structured data markup without errors.",
                "fix": "No action needed.",
            })
    else:
        issues.append({
            "severity": "warning",
            "category": "Schema",
            "issue": "Missing structured data",
            "description": "No schema.org structured data found. Schema helps search engines understand page content and enables rich results.",
            "fix": "Add relevant schema.org markup (e.g., Product, Article, BreadcrumbList) via JSON-LD in the page head.",
        })

    # --- Broken links ---
    raw_broken = data.get("broken_links", False)
    if isinstance(raw_broken, bool):
        broken_count = 0 if not raw_broken else 1
    elif isinstance(raw_broken, list):
        broken_count = len(raw_broken)
    else:
        broken_count = raw_broken or 0

    if broken_count > 0:
        issues.append({
            "severity": "critical",
            "category": "Broken Links",
            "issue": f"Broken links detected ({broken_count})",
            "description": f"The page has {broken_count} broken link(s). Broken links harm UX and SEO.",
            "fix": "Identify and fix or remove all broken links (4xx/5xx responses).",
        })
    else:
        issues.append({
            "severity": "pass",
            "category": "Broken Links",
            "issue": "No broken links",
            "description": "All links on the page return valid responses.",
            "fix": "No action needed.",
        })

    # --- Page size ---
    page_size = data.get("size", 0) or 0
    page_size_mb = page_size / (1024 * 1024)

    if checks.get("large_page_size") or page_size > PAGE_SIZE_LIMIT_BYTES:
        issues.append({
            "severity": "warning",
            "category": "Page Size",
            "issue": f"Page size too large ({page_size_mb:.2f} MB)",
            "description": f"Page is {page_size_mb:.2f} MB, exceeding the 3 MB recommendation. Large pages load slowly.",
            "fix": "Optimize images, minify CSS/JS, enable compression (gzip/brotli), and reduce unnecessary resources.",
        })
    else:
        size_str = f"{page_size_mb:.2f} MB" if page_size_mb >= 0.01 else f"{page_size / 1024:.0f} KB"
        issues.append({
            "severity": "pass",
            "category": "Page Size",
            "issue": f"Page size acceptable ({size_str})",
            "description": f"Page size is {size_str}, within acceptable limits.",
            "fix": "No action needed.",
        })

    # --- OG tags ---
    social_tags = meta.get("social_media_tags") or {}
    og_keys = [k for k in social_tags if k.startswith("og:")]

    if og_keys:
        missing_og = []
        for required in ["og:title", "og:description", "og:image", "og:url"]:
            if not social_tags.get(required):
                missing_og.append(required)

        if missing_og:
            issues.append({
                "severity": "warning",
                "category": "Open Graph",
                "issue": f"Partial OG tags ({len(og_keys)} found, missing: {', '.join(missing_og)})",
                "description": f"Open Graph tags found but some are missing: {', '.join(missing_og)}.",
                "fix": f"Add the missing OG tags: {'; '.join(missing_og)}.",
            })
        else:
            issues.append({
                "severity": "pass",
                "category": "Open Graph",
                "issue": f"All key OG tags present ({len(og_keys)} tags)",
                "description": "Essential Open Graph tags (og:title, og:description, og:image, og:url) are present.",
                "fix": "No action needed.",
            })
    else:
        issues.append({
            "severity": "warning",
            "category": "Open Graph",
            "issue": "Missing Open Graph tags",
            "description": "No Open Graph tags found. OG tags control how the page appears when shared on social media.",
            "fix": "Add og:title, og:description, og:image, and og:url meta tags to the page head.",
        })

    # --- Bonus checks from DataForSEO ---
    if checks.get("is_broken"):
        issues.append({
            "severity": "critical",
            "category": "Page Status",
            "issue": f"Page is broken (HTTP {data.get('status_code', '?')})",
            "description": "The page returned a broken status code.",
            "fix": "Fix the server-side issue causing the error response.",
        })

    if checks.get("is_4xx_code"):
        issues.append({
            "severity": "critical",
            "category": "Page Status",
            "issue": f"4xx error (HTTP {data.get('status_code', '?')})",
            "description": "The page returned a 4xx client error.",
            "fix": "Fix the URL or server configuration to return 200 OK.",
        })

    if checks.get("is_5xx_code"):
        issues.append({
            "severity": "critical",
            "category": "Page Status",
            "issue": f"5xx error (HTTP {data.get('status_code', '?')})",
            "description": "The page returned a 5xx server error.",
            "fix": "Resolve the server-side error.",
        })

    if checks.get("duplicate_title_tag"):
        issues.append({
            "severity": "warning",
            "category": "Duplicate Content",
            "issue": "Duplicate title tag",
            "description": "This page's title tag is duplicated on other pages.",
            "fix": "Ensure each page has a unique title tag.",
        })

    if checks.get("has_render_blocking_resources"):
        issues.append({
            "severity": "warning",
            "category": "Performance",
            "issue": "Render-blocking resources detected",
            "description": "The page has render-blocking CSS or JavaScript resources.",
            "fix": "Defer non-critical JS, inline critical CSS, or use async loading.",
        })

    if checks.get("high_loading_time"):
        issues.append({
            "severity": "warning",
            "category": "Performance",
            "issue": "High page loading time",
            "description": "The page takes longer than recommended to load.",
            "fix": "Optimize page resources, enable caching, and reduce server response time.",
        })

    if checks.get("no_favicon"):
        issues.append({
            "severity": "warning",
            "category": "Branding",
            "issue": "Missing favicon",
            "description": "The page does not have a favicon.",
            "fix": "Add a favicon.ico file to the site root.",
        })

    if checks.get("low_content_rate") or checks.get("low_character_count"):
        word_count = (meta.get("content") or {}).get("plain_text_word_count", 0) or 0
        issues.append({
            "severity": "warning",
            "category": "Content",
            "issue": f"Low content volume ({word_count} words)",
            "description": "The page has relatively little text content, which may limit keyword relevance signals.",
            "fix": "Add more substantive, relevant content to the page.",
        })

    return issues


# ── 3. generate_instant_report ─────────────────────────────────────────────

def generate_instant_report(url, data, issues, output_path):
    """
    Generate an HTML report from the instant audit results.
    Returns the path to the saved HTML file.
    """
    meta = data.get("meta", {}) or {}
    checks = data.get("checks", {}) or {}
    page_timing = data.get("page_timing", {}) or {}
    content = meta.get("content") or {}

    # Overview data
    title = meta.get("title") or "No title"
    description = meta.get("description") or "No description"
    word_count = content.get("plain_text_word_count", 0) or 0
    page_size = data.get("size", 0) or 0
    page_size_str = f"{page_size / (1024 * 1024):.2f} MB" if page_size >= 1024 * 1024 else f"{page_size / 1024:.0f} KB"
    onpage_score = data.get("onpage_score", "N/A")
    status_code = data.get("status_code", "N/A")
    actual_url = data.get("url", url)
    canonical = meta.get("canonical") or "Not set"

    htags = meta.get("htags") or {}
    h1_tags = htags.get("h1", []) if htags else []
    h2_tags = htags.get("h2", []) if htags else []
    h3_tags = htags.get("h3", []) if htags else []
    h4_tags = htags.get("h4", []) if htags else []
    h5_tags = htags.get("h5", []) if htags else []
    h6_tags = htags.get("h6", []) if htags else []

    internal_links = meta.get("internal_links_count", 0) or 0
    external_links = meta.get("external_links_count", 0) or 0
    images_count = meta.get("images_count", 0) or 0
    scripts_count = meta.get("scripts_count", 0) or 0
    stylesheets_count = meta.get("stylesheets_count", 0) or 0

    social_tags = meta.get("social_media_tags") or {}

    # Issue counts
    critical_count = sum(1 for i in issues if i["severity"] == "critical")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    pass_count = sum(1 for i in issues if i["severity"] == "pass")

    # Quick recommendations (only from critical + warning issues)
    recommendations = [
        f"Fix: {i['issue']} — {i['fix']}"
        for i in issues if i["severity"] in ("critical", "warning")
    ]
    if not recommendations:
        recommendations = ["No critical issues found. Continue monitoring page health regularly."]

    # Build on-page elements table rows
    def _esc(text):
        return html_mod.escape(str(text)) if text else ""

    # HTML template
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Instant Page Audit — {_esc(url[:60])}</title>
<style>
  :root {{
    --bg: #f8fafc;
    --card-bg: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --primary: #4f46e5;
    --primary-light: #eef2ff;
    --critical: #ef4444;
    --critical-light: #fef2f2;
    --warning: #f59e0b;
    --warning-light: #fffbeb;
    --pass: #10b981;
    --pass-light: #f0fdf4;
    --radius: 10px;
    --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 24px;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  .header {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 28px; flex-wrap: wrap; gap: 12px;
  }}
  .header h1 {{ font-size: 24px; font-weight: 700; }}
  .header .meta {{ font-size: 13px; color: var(--text-muted); }}
  .badge {{
    display: inline-block; padding: 4px 12px; border-radius: 20px;
    font-size: 12px; font-weight: 600;
  }}
  .badge-score {{ background: var(--primary-light); color: var(--primary); }}
  .section {{
    background: var(--card-bg); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 24px; margin-bottom: 20px;
  }}
  .section h2 {{ font-size: 18px; margin-bottom: 16px; }}
  /* Summary cards */
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
  .card {{
    border-radius: var(--radius); padding: 20px; text-align: center;
  }}
  .card.critical {{ background: var(--critical-light); border: 1px solid #fecaca; }}
  .card.warning {{ background: var(--warning-light); border: 1px solid #fde68a; }}
  .card.pass {{ background: var(--pass-light); border: 1px solid #bbf7d0; }}
  .card .number {{ font-size: 36px; font-weight: 800; line-height: 1; }}
  .card.critical .number {{ color: var(--critical); }}
  .card.warning .number {{ color: var(--warning); }}
  .card.pass .number {{ color: var(--pass); }}
  .card .label {{ font-size: 14px; font-weight: 600; margin-top: 6px; color: var(--text-muted); }}
  /* Overview grid */
  .overview-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; }}
  .overview-item {{ display: flex; flex-direction: column; }}
  .overview-item .key {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 4px; }}
  .overview-item .val {{ font-size: 15px; font-weight: 600; word-break: break-word; }}
  /* Table */
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 14px; }}
  th {{ font-weight: 600; color: var(--text-muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
  tr:last-child td {{ border-bottom: none; }}
  .sev {{
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .sev-critical {{ background: var(--critical-light); color: var(--critical); }}
  .sev-warning {{ background: var(--warning-light); color: var(--warning); }}
  .sev-pass {{ background: var(--pass-light); color: var(--pass); }}
  .expand-btn {{
    cursor: pointer; background: none; border: none; color: var(--primary);
    font-size: 13px; font-weight: 600; padding: 0;
  }}
  .expand-btn:hover {{ text-decoration: underline; }}
  .detail-row {{ display: none; }}
  .detail-row.open {{ display: table-row; }}
  .detail-row td {{ background: var(--bg); padding: 16px; }}
  .detail-content {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .detail-content .block {{ }}
  .detail-content .block h4 {{ font-size: 12px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; }}
  .detail-content .block p {{ font-size: 13px; }}
  /* Elements table */
  .elem-table td {{ vertical-align: top; }}
  .tag-list {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .tag {{
    display: inline-block; padding: 2px 8px; background: var(--primary-light);
    color: var(--primary); border-radius: 4px; font-size: 12px; margin-bottom: 4px;
  }}
  .tag-list .tag {{ max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  /* Recommendations */
  .rec-list {{ list-style: none; counter-reset: rec; }}
  .rec-list li {{
    counter-increment: rec; padding: 10px 0 10px 36px; position: relative;
    border-bottom: 1px solid var(--border); font-size: 14px;
  }}
  .rec-list li:last-child {{ border-bottom: none; }}
  .rec-list li::before {{
    content: counter(rec); position: absolute; left: 0; top: 10px;
    width: 24px; height: 24px; border-radius: 50%; background: var(--primary);
    color: white; display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700;
  }}
  .footer {{ text-align: center; padding: 20px; font-size: 12px; color: var(--text-muted); }}
  .footer a {{ color: var(--primary); text-decoration: none; }}
  @media (max-width: 768px) {{
    .detail-content {{ grid-template-columns: 1fr; }}
    .cards {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div>
      <h1>⚡ Instant Page Audit</h1>
      <div class="meta">Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · v{SCRIPT_VERSION} · DataForSEO on_page/instant_pages</div>
    </div>
    <div>
      <span class="badge badge-score">On-Page Score: {onpage_score if onpage_score != 'N/A' else 'N/A'}</span>
    </div>
  </div>

  <!-- Page Overview -->
  <div class="section">
    <h2>📋 Page Overview</h2>
    <div class="overview-grid">
      <div class="overview-item">
        <div class="key">URL</div>
        <div class="val">{_esc(actual_url)}</div>
      </div>
      <div class="overview-item">
        <div class="key">Title</div>
        <div class="val">{_esc(title)}</div>
      </div>
      <div class="overview-item">
        <div class="key">Meta Description</div>
        <div class="val">{_esc(description[:200])}</div>
      </div>
      <div class="overview-item">
        <div class="key">Word Count</div>
        <div class="val">{word_count:,}</div>
      </div>
      <div class="overview-item">
        <div class="key">Page Size</div>
        <div class="val">{page_size_str}</div>
      </div>
      <div class="overview-item">
        <div class="key">HTTP Status</div>
        <div class="val">{status_code}</div>
      </div>
      <div class="overview-item">
        <div class="key">Canonical URL</div>
        <div class="val">{_esc(canonical)}</div>
      </div>
      <div class="overview-item">
        <div class="key">On-Page Score</div>
        <div class="val">{onpage_score if onpage_score != 'N/A' else 'N/A'}</div>
      </div>
    </div>
  </div>

  <!-- Issue Summary Cards -->
  <div class="section">
    <h2>📊 Issue Summary</h2>
    <div class="cards">
      <div class="card critical">
        <div class="number">{critical_count}</div>
        <div class="label">Critical Issues</div>
      </div>
      <div class="card warning">
        <div class="number">{warning_count}</div>
        <div class="label">Warnings</div>
      </div>
      <div class="card pass">
        <div class="number">{pass_count}</div>
        <div class="label">Passed Checks</div>
      </div>
    </div>
  </div>

  <!-- Issue Details -->
  <div class="section">
    <h2>🔍 Issue Details</h2>
    <table>
      <thead>
        <tr>
          <th style="width:90px">Severity</th>
          <th style="width:140px">Category</th>
          <th>Issue</th>
          <th style="width:80px">Details</th>
        </tr>
      </thead>
      <tbody>
"""

    # Issue detail rows
    for idx, issue in enumerate(issues):
        sev_class = f"sev-{issue['severity']}"
        sev_label = issue["severity"].capitalize()
        html += f"""        <tr>
          <td><span class="sev {sev_class}">{sev_label}</span></td>
          <td>{_esc(issue['category'])}</td>
          <td>{_esc(issue['issue'])}</td>
          <td><button class="expand-btn" onclick="toggleDetail({idx})">▼ View</button></td>
        </tr>
        <tr class="detail-row" id="detail-{idx}">
          <td colspan="4">
            <div class="detail-content">
              <div class="block">
                <h4>Description</h4>
                <p>{_esc(issue['description'])}</p>
              </div>
              <div class="block">
                <h4>Recommended Fix</h4>
                <p>{_esc(issue['fix'])}</p>
              </div>
            </div>
          </td>
        </tr>
"""

    html += f"""      </tbody>
    </table>
  </div>

  <!-- On-Page Elements -->
  <div class="section">
    <h2>🏗️ On-Page Elements</h2>
    <table class="elem-table">
      <thead>
        <tr><th style="width:180px">Element</th><th>Value</th></tr>
      </thead>
      <tbody>
        <tr><td><strong>Title Tag</strong></td><td>{_esc(title)} <span style="color:var(--text-muted)">({meta.get('title_length', len(title))} chars)</span></td></tr>
        <tr><td><strong>Meta Description</strong></td><td>{_esc(description[:300]) if description else '<em style="color:var(--critical)">Missing</em>'} <span style="color:var(--text-muted)">({meta.get('description_length', len(description))} chars)</span></td></tr>
        <tr><td><strong>Canonical</strong></td><td>{_esc(canonical) if canonical else '<em style="color:var(--warning)">Not set</em>'}</td></tr>
        <tr><td><strong>H1 Tags ({len(h1_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h)}</span>' for h in h1_tags) + '</div>' if h1_tags else '<em style="color:var(--critical)">No H1 found</em>'}</td></tr>
        <tr><td><strong>H2 Tags ({len(h2_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h[:100])}</span>' for h in h2_tags[:10]) + ('</div>' if len(h2_tags) <= 10 else f' <span style="color:var(--text-muted)">+{len(h2_tags)-10} more</span></div>') if h2_tags else '<em style="color:var(--text-muted)">None</em>'}</td></tr>
        <tr><td><strong>H3 Tags ({len(h3_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h[:100])}</span>' for h in h3_tags[:10]) + ('</div>' if len(h3_tags) <= 10 else f' <span style="color:var(--text-muted)">+{len(h3_tags)-10} more</span></div>') if h3_tags else '<em style="color:var(--text-muted)">None</em>'}</td></tr>
        <tr><td><strong>H4 Tags ({len(h4_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h[:100])}</span>' for h in h4_tags[:5]) + ('</div>' if len(h4_tags) <= 5 else f' <span style="color:var(--text-muted)">+{len(h4_tags)-5} more</span></div>') if h4_tags else '<em style="color:var(--text-muted)">None</em>'}</td></tr>
        <tr><td><strong>H5 Tags ({len(h5_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h[:100])}</span>' for h in h5_tags[:5]) + ('</div>' if len(h5_tags) <= 5 else f' <span style="color:var(--text-muted)">+{len(h5_tags)-5} more</span></div>') if h5_tags else '<em style="color:var(--text-muted)">None</em>'}</td></tr>
        <tr><td><strong>H6 Tags ({len(h6_tags)})</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(h[:100])}</span>' for h in h6_tags[:5]) + ('</div>' if len(h6_tags) <= 5 else f' <span style="color:var(--text-muted)">+{len(h6_tags)-5} more</span></div>') if h6_tags else '<em style="color:var(--text-muted)">None</em>'}</td></tr>
        <tr><td><strong>Images</strong></td><td>{images_count} images on page · {'Alt text issues: ' + str(checks.get('no_image_alt', False)) if checks.get('no_image_alt') else 'All images have alt text'}</td></tr>
        <tr><td><strong>Internal Links</strong></td><td>{internal_links}</td></tr>
        <tr><td><strong>External Links</strong></td><td>{external_links}</td></tr>
        <tr><td><strong>Scripts</strong></td><td>{scripts_count} scripts · {meta.get('scripts_size', 0) or 0} bytes</td></tr>
        <tr><td><strong>Stylesheets</strong></td><td>{stylesheets_count} stylesheets · {meta.get('stylesheets_size', 0) or 0} bytes</td></tr>
        <tr><td><strong>Schema / Structured Data</strong></td><td>{'✅ Present' + (' (with errors)' if checks.get('has_micromarkup_errors') else ' (valid)') if checks.get('has_micromarkup') else '❌ Missing'}</td></tr>
        <tr><td><strong>HTTPS</strong></td><td>{'✅ Yes' if checks.get('is_https') else '❌ No'}</td></tr>
        <tr><td><strong>Robots Meta</strong></td><td>Index: {_esc('follow' if meta.get('follow') else 'nofollow')} · DataForSEO checks for robots directives</td></tr>
        <tr><td><strong>Open Graph Tags</strong></td><td>{'<div class="tag-list">' + ''.join(f'<span class="tag">{_esc(k)}</span>' for k in social_tags if k.startswith('og:')) + '</div>' if any(k.startswith('og:') for k in social_tags) else '<em style="color:var(--warning)">No OG tags</em>'}</td></tr>
        <tr><td><strong>Page Load Time</strong></td><td>{page_timing.get('duration_time', 'N/A')} ms total · {page_timing.get('time_to_interactive', 'N/A')} ms to interactive</td></tr>
        <tr><td><strong>Cumulative Layout Shift</strong></td><td>{meta.get('cumulative_layout_shift', 'N/A')}</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Quick Recommendations -->
  <div class="section">
    <h2>✅ Quick Recommendations</h2>
    <ol class="rec-list">
"""

    for rec in recommendations:
        html += f"      <li>{_esc(rec)}</li>\n"

    html += f"""    </ol>
  </div>

  <div class="footer">
    <p>Generated by <strong>Instant Page Audit</strong> v{SCRIPT_VERSION} · Powered by <a href="https://dataforseo.com">DataForSEO</a> on_page/instant_pages API</p>
    <p>Single-URL instant audit · One API call · No crawling · No SERP comparison</p>
  </div>

</div>

<script>
  function toggleDetail(idx) {{
    const row = document.getElementById('detail-' + idx);
    row.classList.toggle('open');
    const btn = event.target;
    btn.textContent = row.classList.contains('open') ? '▲ Hide' : '▼ View';
  }}
</script>
</body>
</html>"""

    # Write file
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, "instant_page_audit.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)

    return report_file


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Instant Page Audit — Single-URL SEO health check")
    parser.add_argument("--url", required=True, help="URL to audit")
    parser.add_argument("--output", default="/tmp/instant_audit", help="Output directory for report")
    args = parser.parse_args()

    url = args.url
    output_path = args.output

    print(f"⚡ Instant Page Audit — {url}")
    print(f"   Output: {output_path}")
    print()

    # Step 1: Fetch on-page data
    print("[1/3] Fetching on-page data from DataForSEO...")
    data = fetch_on_page_data(url)
    if not data:
        print("ERROR: Failed to fetch on-page data.", file=sys.stderr)
        sys.exit(1)
    print(f"   ✅ Received data for: {data.get('url', url)}")
    print(f"   On-page score: {data.get('onpage_score', 'N/A')}")

    # Step 2: Analyze
    print("[2/3] Analyzing SEO issues...")
    issues = analyze_on_page(data, url)
    critical = sum(1 for i in issues if i["severity"] == "critical")
    warning = sum(1 for i in issues if i["severity"] == "warning")
    passed = sum(1 for i in issues if i["severity"] == "pass")
    print(f"   ✅ Found {critical} critical, {warning} warnings, {passed} passed")

    # Step 3: Generate report
    print("[3/3] Generating HTML report...")
    report_path = generate_instant_report(url, data, issues, output_path)
    print(f"   ✅ Report saved: {report_path}")
    print()
    print("=" * 60)
    print(f"  INSTANT AUDIT COMPLETE")
    print(f"  Critical: {critical}  Warnings: {warning}  Passed: {passed}")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
