#!/usr/bin/env python3
"""
Page Speed Audit — Domain-Level PageSpeed Insights + CrUX Report
=================================================================
Crawls a domain's sitemap, fetches PSI (Lighthouse + CrUX) for each page
(mobile + desktop), and generates an HTML report with trends, CWV
distribution, per-page expandable rows, slowest pages, and prioritized
recommendations.

Usage:
    python3 page_speed_audit.py --domain example.com --limit 50 --output /tmp/audit

Requires:
    psi_helper.py in same directory (~/.hermes/scripts/)
    GOOGLE_API_KEY in ~/.hermes/.env (optional — free tier works at 1 req/sec)
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

# ── ensure we can import psi_helper ──────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from psi_helper import get_psi_report, get_core_web_vitals
except Exception as _imp_err:  # pragma: no cover
    print(f"WARNING: could not import psi_helper: {_imp_err}")
    get_psi_report = None
    get_core_web_vitals = None

# ── Constants ─────────────────────────────────────────────────────────────────
RATE_LIMIT_S = 1.0  # 1 PSI request per second
USER_AGENT = "Mozilla/5.0 (compatible; PageSpeedAudit/1.0)"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Core Web Vitals thresholds (Google official)
CWV_THRESHOLDS = {
    "lcp": {"good": 2500, "ni": 4000},        # ms
    "inp": {"good": 200, "ni": 500},          # ms
    "cls": {"good": 0.1, "ni": 0.25},         # score
    "fcp": {"good": 1800, "ni": 3000},        # ms
    "ttfb": {"good": 800, "ni": 1800},        # ms
}

CWV_LABELS = {
    "lcp": "Largest Contentful Paint",
    "inp": "Interaction to Next Paint",
    "cls": "Cumulative Layout Shift",
    "fcp": "First Contentful Paint",
    "ttfb": "Time to First Byte",
    "fid": "First Input Delay",
    "si": "Speed Index",
    "tbt": "Total Blocking Time",
}


# ── Sitemap / page discovery ─────────────────────────────────────────────────

def _normalize_domain(domain):
    """Ensure domain has a scheme."""
    domain = domain.strip()
    if not domain.startswith("http"):
        domain = "https://" + domain
    return domain.rstrip("/")


def discover_pages(domain, limit=50):
    """
    Discover page URLs for a domain by fetching sitemap.xml.
    Handles sitemap index files and nested sitemaps.
    Returns a list of URL strings (up to `limit`).
    """
    base = _normalize_domain(domain)
    urls = []
    seen = set()

    def _fetch(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception:
            return None

    def _parse_sitemap(xml_bytes, depth=0):
        if depth > 3 or not xml_bytes:
            return
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return
        tag = root.tag
        # Sitemap index
        if "sitemapindex" in tag:
            for sm in root.findall("sm:sitemap", SITEMAP_NS):
                loc = sm.find("sm:loc", SITEMAP_NS)
                if loc is not None and loc.text:
                    child_xml = _fetch(loc.text.strip())
                    if child_xml:
                        _parse_sitemap(child_xml, depth + 1)
                        if len(urls) >= limit:
                            return
        # URL set
        elif "urlset" in tag:
            for u in root.findall("sm:url", SITEMAP_NS):
                loc = u.find("sm:loc", SITEMAP_NS)
                if loc is not None and loc.text:
                    u_str = loc.text.strip()
                    if u_str not in seen:
                        seen.add(u_str)
                        urls.append(u_str)
                        if len(urls) >= limit:
                            return

    # Try /sitemap.xml first, then /sitemap_index.xml
    for sitemap_path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz"]:
        xml_data = _fetch(base + sitemap_path)
        if xml_data:
            _parse_sitemap(xml_data)
            break

    # Fallback: if no sitemap, just audit the homepage
    if not urls:
        urls = [base + "/"]

    return urls[:limit]


# ── Core Web Vitals classification ────────────────────────────────────────────

def classify_cwv(metrics):
    """
    Classify Core Web Vitals as Good / Needs Improvement / Poor
    per Google thresholds.

    Args:
        metrics: dict with keys like 'lcp', 'inp', 'cls', 'fcp', 'ttfb'
                 Values are numeric (ms for timing metrics, unitless for CLS).

    Returns:
        dict mapping each metric key to {
            'value': original value,
            'rating': 'good' | 'needs_improvement' | 'poor' | 'n/a',
            'label': human-readable metric name,
        }
    """
    result = {}
    for key, thresholds in CWV_THRESHOLDS.items():
        raw = metrics.get(key)
        label = CWV_LABELS.get(key, key.upper())
        if raw is None or raw == 0:
            result[key] = {"value": raw, "rating": "n/a", "label": label}
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            result[key] = {"value": raw, "rating": "n/a", "label": label}
            continue
        if val < thresholds["good"]:
            rating = "good"
        elif val < thresholds["ni"]:
            rating = "needs_improvement"
        else:
            rating = "poor"
        result[key] = {"value": val, "rating": rating, "label": label}
    return result


# ── Domain audit ─────────────────────────────────────────────────────────────

def audit_domain_speed(domain, limit=50, strategies=("mobile", "desktop")):
    """
    For each page discovered on `domain`, fetch PSI data (mobile + desktop).

    Returns:
        {
            'domain': domain,
            'pages': [
                {
                    'url': url,
                    'mobile': {psi_report...} or {'error': msg},
                    'desktop': {psi_report...} or {'error': msg},
                    'cwv_mobile': {classified metrics},
                    'cwv_desktop': {classified metrics},
                },
                ...
            ],
            'audited_at': ISO timestamp,
            'total_pages': int,
        }
    """
    if get_psi_report is None:
        raise RuntimeError("psi_helper not available — cannot fetch PSI data")

    pages = discover_pages(domain, limit=limit)
    print(f"Discovered {len(pages)} pages for {domain}")

    results = []
    for i, page_url in enumerate(pages):
        page_result = {"url": page_url, "mobile": None, "desktop": None}
        print(f"  [{i+1}/{len(pages)}] {page_url}")
        for strategy in strategies:
            try:
                report = get_psi_report(page_url, strategy=strategy,
                                        categories=["performance", "seo",
                                                     "accessibility", "best-practices"])
                page_result[strategy] = report
                # Extract CrUX / lab CWV metrics for classification
                cwv_data = _extract_cwv_metrics(report)
                page_result[f"cwv_{strategy}"] = classify_cwv(cwv_data)
            except Exception as e:
                page_result[strategy] = {"error": str(e)}
                page_result[f"cwv_{strategy}"] = {}
            time.sleep(RATE_LIMIT_S)  # Rate limit: 1 req/sec
        results.append(page_result)

    return {
        "domain": domain,
        "pages": results,
        "audited_at": datetime.utcnow().isoformat() + "Z",
        "total_pages": len(results),
    }


def _extract_cwv_metrics(psi_report):
    """
    Extract LCP, INP, CLS, FCP, TTFB, SI, TBT from a PSI report.
    Prefers CrUX field data; falls back to Lighthouse lab data.
    """
    metrics = {}

    # CrUX field data (real user metrics in ms / score)
    crux = psi_report.get("crux_page") or psi_report.get("crux_origin")
    if crux and crux.get("metrics"):
        cm = crux["metrics"]
        metrics["lcp"] = cm.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile")
        metrics["cls"] = cm.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile")
        metrics["inp"] = cm.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile") or \
                         cm.get("FIRST_INPUT_DELAY_MS", {}).get("percentile")
        metrics["fcp"] = cm.get("FIRST_CONTENTFUL_PAINT_MS", {}).get("percentile")
        metrics["ttfb"] = cm.get("EXPERIMENTAL_TIME_TO_FIRST_BYTE", {}).get("percentile")

    # Lighthouse lab data (fallback / supplement)
    lab = psi_report.get("metrics", {})
    if lab:
        if not metrics.get("lcp"):
            metrics["lcp"] = lab.get("lcp", {}).get("numeric")
        if not metrics.get("cls"):
            metrics["cls"] = lab.get("cls", {}).get("numeric")
        if not metrics.get("fcp"):
            metrics["fcp"] = lab.get("fcp", {}).get("numeric")
        metrics.setdefault("si", lab.get("si", {}).get("numeric"))
        metrics.setdefault("tbt", lab.get("tbt", {}).get("numeric"))

    # INP fallback: TBT as proxy if no INP
    if not metrics.get("inp") and metrics.get("tbt"):
        metrics["inp"] = metrics["tbt"]

    return metrics


# ── Aggregation helpers ──────────────────────────────────────────────────────

def _avg(values):
    """Average of non-None, non-zero numeric values."""
    vals = [v for v in values if v is not None and v != 0]
    return sum(vals) / len(vals) if vals else 0


def _aggregate_overview(results):
    """Compute domain-level averages from audit results."""
    mobile_lcp, mobile_cls, mobile_inp, mobile_fcp, mobile_ttfb, mobile_si = [], [], [], [], [], []
    desktop_lcp, desktop_cls, desktop_inp, desktop_fcp, desktop_ttfb, desktop_si = [], [], [], [], [], []

    for page in results["pages"]:
        for strat, buckets in [("mobile", (mobile_lcp, mobile_cls, mobile_inp, mobile_fcp, mobile_ttfb, mobile_si)),
                                ("desktop", (desktop_lcp, desktop_cls, desktop_inp, desktop_fcp, desktop_ttfb, desktop_si))]:
            cwv = page.get(f"cwv_{strat}", {})
            if not cwv:
                continue
            lcp_v = cwv.get("lcp", {}).get("value")
            cls_v = cwv.get("cls", {}).get("value")
            inp_v = cwv.get("inp", {}).get("value")
            fcp_v = cwv.get("fcp", {}).get("value")
            ttfb_v = cwv.get("ttfb", {}).get("value")
            si_v = cwv.get("si", {}).get("value")
            buckets[0].append(lcp_v)
            buckets[1].append(cls_v)
            buckets[2].append(inp_v)
            buckets[3].append(fcp_v)
            buckets[4].append(ttfb_v)
            buckets[5].append(si_v)

    return {
        "mobile": {
            "lcp": _avg(mobile_lcp), "cls": _avg(mobile_cls), "inp": _avg(mobile_inp),
            "fcp": _avg(mobile_fcp), "ttfb": _avg(mobile_ttfb), "si": _avg(mobile_si),
        },
        "desktop": {
            "lcp": _avg(desktop_lcp), "cls": _avg(desktop_cls), "inp": _avg(desktop_inp),
            "fcp": _avg(desktop_fcp), "ttfb": _avg(desktop_ttfb), "si": _avg(desktop_si),
        },
    }


def _cwv_distribution(results, strategy="mobile"):
    """Count Good / Needs Improvement / Poor for each CWV metric."""
    dist = {"lcp": {"good": 0, "needs_improvement": 0, "poor": 0, "n/a": 0},
            "inp": {"good": 0, "needs_improvement": 0, "poor": 0, "n/a": 0},
            "cls": {"good": 0, "needs_improvement": 0, "poor": 0, "n/a": 0}}
    for page in results["pages"]:
        cwv = page.get(f"cwv_{strategy}", {})
        for key in dist:
            rating = cwv.get(key, {}).get("rating", "n/a")
            dist[key][rating] = dist[key].get(rating, 0) + 1
    return dist


def _slowest_pages(results, strategy="mobile", top_n=10):
    """Return pages sorted by LCP descending."""
    pages_with_lcp = []
    for page in results["pages"]:
        cwv = page.get(f"cwv_{strategy}", {})
        lcp = cwv.get("lcp", {}).get("value")
        if lcp and lcp > 0:
            pages_with_lcp.append({
                "url": page["url"],
                "lcp": lcp,
                "cls": cwv.get("cls", {}).get("value"),
                "inp": cwv.get("inp", {}).get("value"),
                "rating": cwv.get("lcp", {}).get("rating", "n/a"),
            })
    pages_with_lcp.sort(key=lambda x: -x["lcp"])
    return pages_with_lcp[:top_n]


def _aggregate_recommendations(results):
    """Aggregate opportunities across all pages, prioritized by impact (count + numeric value)."""
    rec_counter = {}  # id -> {title, display, total_numeric, count}
    for page in results["pages"]:
        for strat in ["mobile", "desktop"]:
            report = page.get(strat)
            if not report or isinstance(report, dict) and report.get("error"):
                continue
            opps = report.get("opportunities", [])
            for opp in opps:
                key = opp.get("id", opp.get("title", ""))
                if key not in rec_counter:
                    rec_counter[key] = {
                        "id": key,
                        "title": opp.get("title", key),
                        "display": opp.get("display", ""),
                        "total_numeric": 0,
                        "count": 0,
                    }
                rec_counter[key]["total_numeric"] += opp.get("numeric", 0)
                rec_counter[key]["count"] += 1
    recs = list(rec_counter.values())
    recs.sort(key=lambda x: (-x["count"], -x["total_numeric"]))
    return recs[:20]


# ── HTML report generation ───────────────────────────────────────────────────

def _fmt_ms(val):
    """Format milliseconds value."""
    if not val or val == 0:
        return "N/A"
    if val >= 1000:
        return f"{val/1000:.2f} s"
    return f"{val:.0f} ms"


def _fmt_cls(val):
    if val is None or val == 0:
        return "N/A"
    return f"{val:.3f}"


def _rating_badge(rating):
    colors = {
        "good": "#16a34a",
        "needs_improvement": "#f59e0b",
        "poor": "#dc2626",
        "n/a": "#6b7280",
    }
    labels = {
        "good": "Good",
        "needs_improvement": "Needs Improvement",
        "poor": "Poor",
        "n/a": "N/A",
    }
    color = colors.get(rating, "#6b7280")
    label = labels.get(rating, rating)
    return f'<span style="background:{color};color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;">{label}</span>'


def _metric_card(label, mobile_val, desktop_val, fmt_fn=_fmt_ms, unit=""):
    """Render a metric overview card."""
    mv = fmt_fn(mobile_val) if mobile_val else "N/A"
    dv = fmt_fn(desktop_val) if desktop_val else "N/A"
    return f"""
    <div class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-values">
        <div><span class="strategy-tag mobile">Mobile</span> <span class="metric-num">{mv}</span></div>
        <div><span class="strategy-tag desktop">Desktop</span> <span class="metric-num">{dv}</span></div>
      </div>
    </div>"""


def _distribution_bar(dist, metric_key):
    """Render a horizontal distribution bar for a CWV metric."""
    total = sum(dist[metric_key].values())
    if total == 0:
        return "<div class='dist-empty'>No data</div>"
    good = dist[metric_key]["good"]
    ni = dist[metric_key]["needs_improvement"]
    poor = dist[metric_key]["poor"]
    na = dist[metric_key]["n/a"]
    parts = [
        ("good", good, "#16a34a"),
        ("needs_improvement", ni, "#f59e0b"),
        ("poor", poor, "#dc2626"),
        ("n/a", na, "#d1d5db"),
    ]
    bar = ""
    for label, count, color in parts:
        if count > 0:
            pct = (count / total) * 100
            bar += f'<div class="dist-seg" style="width:{pct}%;background:{color};" title="{label}: {count}"></div>'
    legend = " ".join(
        f'<span class="dist-legend"><span class="dist-dot" style="background:{color}"></span>{label} ({count})</span>'
        for label, count, color in parts if count > 0
    )
    label_full = CWV_LABELS.get(metric_key, metric_key.upper())
    return f"""
    <div class="dist-row">
      <div class="dist-label">{label_full}</div>
      <div class="dist-bar">{bar}</div>
      <div class="dist-legend-row">{legend}</div>
    </div>"""


def _page_row_html(idx, page):
    """Render an expandable table row for a single page."""
    url = page["url"]
    mobile = page.get("mobile", {})
    desktop = page.get("desktop", {})
    cwv_m = page.get("cwv_mobile", {})
    cwv_d = page.get("cwv_desktop", {})

    # Mobile scores
    scores_m = mobile.get("scores", {}) if isinstance(mobile, dict) and not mobile.get("error") else {}
    scores_d = desktop.get("scores", {}) if isinstance(desktop, dict) and not desktop.get("error") else {}

    perf_m = scores_m.get("performance", 0)
    perf_d = scores_d.get("performance", 0)
    seo_m = scores_m.get("seo", 0)
    lcp_m = cwv_m.get("lcp", {}).get("value")
    cls_m = cwv_m.get("cls", {}).get("value")
    inp_m = cwv_m.get("inp", {}).get("value")
    lcp_rating = cwv_m.get("lcp", {}).get("rating", "n/a")

    # Expanded detail content
    opps_m = mobile.get("opportunities", []) if isinstance(mobile, dict) else []
    opps_html = ""
    if opps_m:
        opps_html = "<table class='audit-table'><tr><th>Audit</th><th>Value</th><th>Impact</th></tr>"
        for o in opps_m[:10]:
            score = o.get("score", 0)
            impact = "High" if score < 0.5 else "Medium" if score < 0.9 else "Low"
            opps_html += f"<tr><td>{o.get('title','')}</td><td>{o.get('display','')}</td><td>{impact}</td></tr>"
        opps_html += "</table>"
    else:
        opps_html = "<p>No opportunities detected.</p>"

    # Full Lighthouse audit details
    metrics_detail_m = ""
    if isinstance(mobile, dict) and mobile.get("metrics"):
        metrics_detail_m = "<table class='audit-table'><tr><th>Metric</th><th>Value</th><th>Score</th></tr>"
        for k, v in mobile["metrics"].items():
            metrics_detail_m += f"<tr><td>{CWV_LABELS.get(k, k.upper())}</td><td>{v.get('display','N/A')}</td><td>{_rating_badge(v.get('rating','n/a'))}</td></tr>"
        metrics_detail_m += "</table>"

    metrics_detail_d = ""
    if isinstance(desktop, dict) and desktop.get("metrics"):
        metrics_detail_d = "<table class='audit-table'><tr><th>Metric</th><th>Value</th><th>Score</th></tr>"
        for k, v in desktop["metrics"].items():
            metrics_detail_d += f"<tr><td>{CWV_LABELS.get(k, k.upper())}</td><td>{v.get('display','N/A')}</td><td>{_rating_badge(v.get('rating','n/a'))}</td></tr>"
        metrics_detail_d += "</table>"

    crux_m = mobile.get("crux_page") if isinstance(mobile, dict) else None
    crux_html = ""
    if crux_m and crux_m.get("metrics"):
        crux_html = "<table class='audit-table'><tr><th>CrUX Metric</th><th>Percentile</th><th>Category</th></tr>"
        for k, v in crux_m["metrics"].items():
            crux_html += f"<tr><td>{k.replace('_',' ').title()}</td><td>{_fmt_ms(v.get('percentile',0))}</td><td>{v.get('category','N/A')}</td></tr>"
        crux_html += "</table>"
    else:
        crux_html = "<p>No CrUX field data available for this page.</p>"

    expandable = f"""
    <div class="page-detail" id="detail-{idx}" style="display:none;">
      <div class="detail-grid">
        <div class="detail-section">
          <h4>Mobile Lighthouse Metrics</h4>
          {metrics_detail_m}
        </div>
        <div class="detail-section">
          <h4>Desktop Lighthouse Metrics</h4>
          {metrics_detail_d}
        </div>
      </div>
      <div class="detail-section">
        <h4>CrUX Field Data (Mobile)</h4>
        {crux_html}
      </div>
      <div class="detail-section">
        <h4>Top Opportunities (Mobile)</h4>
        {opps_html}
      </div>
    </div>"""

    perf_color = "#dc2626" if perf_m < 50 else "#f59e0b" if perf_m < 90 else "#16a34a"

    return f"""
    <tr class="page-row" onclick="document.getElementById('detail-{idx}').style.display = document.getElementById('detail-{idx}').style.display === 'none' ? 'block' : 'none'; this.classList.toggle('expanded');">
      <td><span class="expand-icon">▶</span> {url}</td>
      <td><span class="score-badge" style="background:{perf_color}">{perf_m}</span></td>
      <td><span class="score-badge" style="background:{perf_color}">{perf_d}</span></td>
      <td>{_fmt_ms(lcp_m)}</td>
      <td>{_fmt_cls(cls_m)}</td>
      <td>{_fmt_ms(inp_m)}</td>
      <td>{_rating_badge(lcp_rating)}</td>
    </tr>
    <tr class="detail-row"><td colspan="7">{expandable}</td></tr>"""


def generate_speed_report(domain, results, output_path):
    """
    Generate an HTML speed audit report.

    Args:
        domain: domain name audited
        results: dict from audit_domain_speed()
        output_path: directory to write the report (report.html inside)

    Returns:
        Path to the generated HTML report.
    """
    os.makedirs(output_path, exist_ok=True)
    report_path = os.path.join(output_path, "report.html")

    overview = _aggregate_overview(results)
    dist_mobile = _cwv_distribution(results, "mobile")
    dist_desktop = _cwv_distribution(results, "desktop")
    slowest = _slowest_pages(results, "mobile")
    recommendations = _aggregate_recommendations(results)

    ov_m = overview["mobile"]
    ov_d = overview["desktop"]

    # Metric cards
    cards_html = ""
    for key, label, fmt_fn in [
        ("lcp", "Largest Contentful Paint", _fmt_ms),
        ("inp", "Interaction to Next Paint", _fmt_ms),
        ("cls", "Cumulative Layout Shift", _fmt_cls),
        ("ttfb", "Time to First Byte", _fmt_ms),
        ("fcp", "First Contentful Paint", _fmt_ms),
        ("si", "Speed Index", _fmt_ms),
    ]:
        cards_html += _metric_card(label, ov_m.get(key), ov_d.get(key), fmt_fn)

    # Distribution bars
    dist_html = ""
    for metric_key in ["lcp", "inp", "cls"]:
        dist_html += f"<h3>Mobile</h3>{_distribution_bar(dist_mobile, metric_key)}"
        dist_html += f"<h3>Desktop</h3>{_distribution_bar(dist_desktop, metric_key)}"

    # Slowest pages
    slowest_html = ""
    if slowest:
        slowest_html = "<table class='data-table'><tr><th>URL</th><th>LCP</th><th>CLS</th><th>INP</th><th>Rating</th></tr>"
        for p in slowest:
            slowest_html += f"<tr><td>{p['url']}</td><td>{_fmt_ms(p['lcp'])}</td><td>{_fmt_cls(p['cls'])}</td><td>{_fmt_ms(p['inp'])}</td><td>{_rating_badge(p['rating'])}</td></tr>"
        slowest_html += "</table>"
    else:
        slowest_html = "<p>No LCP data available.</p>"

    # Recommendations
    recs_html = ""
    if recommendations:
        recs_html = "<table class='data-table'><tr><th>#</th><th>Recommendation</th><th>Pages Affected</th><th>Typical Value</th></tr>"
        for i, r in enumerate(recommendations, 1):
            recs_html += f"<tr><td>{i}</td><td>{r['title']}</td><td>{r['count']}</td><td>{r['display']}</td></tr>"
        recs_html += "</table>"
    else:
        recs_html = "<p>No recommendations available.</p>"

    # Per-page table rows
    page_rows = ""
    for i, page in enumerate(results["pages"]):
        page_rows += _page_row_html(i, page)

    total = results["total_pages"]
    audited_at = results.get("audited_at", "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="robots" content="noindex, nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Speed Audit — {domain}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
      font-size: 16px;
      line-height: 1.6;
      color: #1f2937;
      background: #f9fafb;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px; }}
    h1 {{ font-size: 24px; font-weight: 700; color: #111827; margin-bottom: 4px; }}
    h2 {{ font-size: 22px; font-weight: 700; color: #111827; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #e5e7eb; }}
    h3 {{ font-size: 20px; font-weight: 600; color: #374151; margin: 16px 0 8px; }}
    h4 {{ font-size: 18px; font-weight: 600; color: #374151; margin-bottom: 8px; }}
    .subtitle {{ color: #6b7280; font-size: 14px; margin-bottom: 24px; }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .metric-card {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .metric-label {{ font-size: 14px; color: #6b7280; margin-bottom: 8px; font-weight: 500; }}
    .metric-values {{ display: flex; flex-direction: column; gap: 4px; }}
    .metric-num {{ font-size: 18px; font-weight: 700; color: #111827; }}
    .strategy-tag {{
      display: inline-block;
      font-size: 11px;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 4px;
      margin-right: 6px;
    }}
    .strategy-tag.mobile {{ background: #dbeafe; color: #1e40af; }}
    .strategy-tag.desktop {{ background: #ede9fe; color: #5b21b6; }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .data-table th, .data-table td {{
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid #f3f4f6;
      font-size: 14px;
    }}
    .data-table th {{ background: #f9fafb; font-weight: 600; color: #374151; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .data-table tr:hover {{ background: #f9fafb; }}
    .page-row {{ cursor: pointer; }}
    .page-row:hover {{ background: #eff6ff; }}
    .page-row.expanded {{ background: #eff6ff; }}
    .expand-icon {{ font-size: 12px; color: #9ca3af; margin-right: 4px; }}
    .detail-row {{ background: #f9fafb; }}
    .detail-row td {{ padding: 0 !important; border: none; }}
    .page-detail {{ padding: 16px 20px; background: #fff; border: 1px solid #e5e7eb; margin: 8px; border-radius: 8px; }}
    .detail-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
    .detail-section {{ margin-bottom: 16px; }}
    .audit-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .audit-table th, .audit-table td {{
      padding: 6px 10px;
      text-align: left;
      border-bottom: 1px solid #f3f4f6;
    }}
    .audit-table th {{ background: #f9fafb; font-weight: 600; }}
    .score-badge {{
      display: inline-block;
      color: #fff;
      font-weight: 700;
      font-size: 13px;
      padding: 2px 10px;
      border-radius: 6px;
      min-width: 36px;
      text-align: center;
    }}
    .dist-row {{ margin-bottom: 16px; }}
    .dist-label {{ font-size: 14px; font-weight: 600; margin-bottom: 4px; }}
    .dist-bar {{ display: flex; height: 24px; border-radius: 6px; overflow: hidden; border: 1px solid #e5e7eb; }}
    .dist-seg {{ transition: width 0.3s; }}
    .dist-legend-row {{ margin-top: 4px; display: flex; gap: 16px; flex-wrap: wrap; }}
    .dist-legend {{ font-size: 12px; color: #6b7280; display: flex; align-items: center; gap: 4px; }}
    .dist-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; }}
    .dist-empty {{ color: #9ca3af; font-size: 14px; }}
    .summary-box {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 24px;
      display: flex;
      gap: 32px;
      flex-wrap: wrap;
    }}
    .summary-stat {{ text-align: center; }}
    .summary-stat .num {{ font-size: 28px; font-weight: 700; color: #111827; }}
    .summary-stat .lbl {{ font-size: 13px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }}
    .footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #9ca3af; text-align: center; }}
    @media (max-width: 768px) {{
      .detail-grid {{ grid-template-columns: 1fr; }}
      .cards-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Page Speed Audit Report</h1>
    <p class="subtitle">{domain} — {total} pages audited — {audited_at}</p>

    <div class="summary-box">
      <div class="summary-stat"><div class="num">{total}</div><div class="lbl">Pages Audited</div></div>
      <div class="summary-stat"><div class="num">{ov_m.get('lcp',0)/1000:.2f}s</div><div class="lbl">Avg Mobile LCP</div></div>
      <div class="summary-stat"><div class="num">{ov_m.get('cls',0):.3f}</div><div class="lbl">Avg Mobile CLS</div></div>
      <div class="summary-stat"><div class="num">{_fmt_ms(ov_m.get('inp',0))}</div><div class="lbl">Avg Mobile INP</div></div>
    </div>

    <h2>Domain Speed Overview</h2>
    <div class="cards-grid">
      {cards_html}
    </div>

    <h2>Core Web Vitals Distribution</h2>
    {dist_html}

    <h2>Slowest Pages (by Mobile LCP)</h2>
    {slowest_html}

    <h2>Per-Page Results</h2>
    <table class="data-table">
      <tr>
        <th>URL</th>
        <th>Perf (M)</th>
        <th>Perf (D)</th>
        <th>LCP</th>
        <th>CLS</th>
        <th>INP</th>
        <th>Rating</th>
      </tr>
      {page_rows}
    </table>

    <h2>Recommendations (Prioritized by Impact)</h2>
    {recs_html}

    <div class="footer">
      Generated by page_speed_audit.py — PageSpeed Insights + CrUX data from Google PSI API
    </div>
  </div>
</body>
</html>"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Also save raw JSON results
    json_path = os.path.join(output_path, "results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    return report_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Domain-level PageSpeed Insights audit")
    parser.add_argument("--domain", required=True, help="Domain to audit (e.g., example.com)")
    parser.add_argument("--limit", type=int, default=50, help="Max pages to audit (default: 50)")
    parser.add_argument("--output", default="/tmp/speed_audit",
                        help="Output directory for report (default: /tmp/speed_audit)")
    parser.add_argument("--strategies", default="mobile,desktop",
                        help="Comma-separated strategies (default: mobile,desktop)")
    args = parser.parse_args()

    strategies = tuple(s.strip() for s in args.strategies.split(","))

    print(f"\n{'='*60}")
    print(f"  Page Speed Audit — {args.domain}")
    print(f"  Limit: {args.limit} pages | Strategies: {', '.join(strategies)}")
    print(f"{'='*60}\n")

    results = audit_domain_speed(args.domain, limit=args.limit, strategies=strategies)

    report_path = generate_speed_report(args.domain, results, args.output)

    print(f"\n{'='*60}")
    print(f"  Audit complete!")
    print(f"  Pages audited: {results['total_pages']}")
    print(f"  Report: {report_path}")
    print(f"  JSON:  {os.path.join(args.output, 'results.json')}")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    main()
