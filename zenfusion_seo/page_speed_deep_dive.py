#!/usr/bin/env python3
"""
Page Speed Deep Dive — Single-Page Full Lighthouse Audit
=========================================================
Runs a full Lighthouse audit on ONE page via the Google PSI API
and produces an HTML report showing every audit detail:
Core Web Vitals cards, category score rings, all audit results
grouped by category with pass/warn/fail status and expandable fix
recommendations, opportunities table, diagnostics table, lab-vs-field
comparison, and a prioritized action list.

Usage:
    python3 page_speed_deep_dive.py \\
        --url https://www.example.com/page \\
        --output /tmp/deep_dive

Requires:
    GOOGLE_API_KEY in ~/.hermes/.env
    (Reuses psi_helper.py for the actual PSI API call.)
"""

import os
import sys
import json
import html
import argparse
from datetime import datetime

# ── ensure we can import psi_helper ──────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from psi_helper import get_psi_report, _get_api_key
except Exception as _imp_err:  # pragma: no cover
    print(f"WARNING: could not import psi_helper: {_imp_err}")
    get_psi_report = None
    _get_api_key = None

# ── Direct PSI fetch fallback (full raw response) ─────────────────────────────
import urllib.request
import urllib.parse

# ── Constants ────────────────────────────────────────────────────────────────
ALL_CATEGORIES = ["performance", "accessibility", "best-practices", "seo"]

CWV_THRESHOLDS = {
    "lcp":  {"good": 2500, "ni": 4000, "unit": "ms"},   # ms
    "inp":  {"good": 200,  "ni": 500,  "unit": "ms"},   # ms
    "fid":  {"good": 100,  "ni": 300,  "unit": "ms"},   # ms
    "cls":  {"good": 0.1,  "ni": 0.25, "unit": ""},     # score
    "fcp":  {"good": 1800, "ni": 3000, "unit": "ms"},   # ms
    "ttfb": {"good": 800,  "ni": 1800, "unit": "ms"},   # ms
}

CWV_LABELS = {
    "lcp":  "Largest Contentful Paint",
    "inp":  "Interaction to Next Paint",
    "fid":  "First Input Delay",
    "cls":  "Cumulative Layout Shift",
    "fcp":  "First Contentful Paint",
    "ttfb": "Time to First Byte",
}

# Map CrUX metric keys → short keys
CRUX_KEY_MAP = {
    "LARGEST_CONTENTFUL_PAINT_MS":       "lcp",
    "INTERACTION_TO_NEXT_PAINT":         "inp",
    "FIRST_INPUT_DELAY_MS":              "fid",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE":     "cls",
    "FIRST_CONTENTFUL_PAINT_MS":         "fcp",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE":   "ttfb",
}

# Map Lighthouse audit IDs → short CWV keys (lab data)
LAB_KEY_MAP = {
    "largest-contentful-paint": "lcp",
    "first-contentful-paint":   "fcp",
    "cumulative-layout-shift":  "cls",
    "total-blocking-time":       "tbt",
    "speed-index":               "si",
    "interactive":               "tti",
    "server-response-time":      "ttfb",
    "experimental-interaction-to-next-paint": "inp",
}

CATEGORY_TITLES = {
    "performance":    "Performance",
    "accessibility":  "Accessibility",
    "best-practices": "Best Practices",
    "seo":            "SEO",
}


# ── 1. fetch_lighthouse_deep ─────────────────────────────────────────────────

def fetch_lighthouse_deep(url, strategy="mobile"):
    """
    Call Google PSI API and return full Lighthouse + CrUX data for a single page.

    Args:
        url:      Page URL to audit.
        strategy: 'mobile' or 'desktop'.

    Returns:
        {
            'url':               original url,
            'final_url':         final resolved url,
            'strategy':          strategy,
            'timestamp':         analysis UTC timestamp,
            'categories':        {cat_id: {title, score, ...}},
            'audits':            {audit_id: {title, score, displayValue, description, ...}},
            'crux_page':         parsed CrUX field data (page-level) or None,
            'crux_origin':       parsed CrUX field data (origin-level) or None,
            'cwv_field':         {lcp, inp, fid, cls, fcp, ttfb} from CrUX,
            'cwv_lab':           {lcp, fcp, cls, tbt, si, ttfb} from Lighthouse,
            'raw':               full Lighthouse JSON,
        }
    """
    if get_psi_report is None:
        raise RuntimeError("psi_helper not available — cannot fetch PSI data")

    # psi_helper returns a processed report; but we also need the FULL
    # lighthouseResult to enumerate every audit.  Reuse the helper's
    # report for CWV/opportunities, then also fetch raw Lighthouse data.
    report = get_psi_report(
        url, strategy=strategy, categories=ALL_CATEGORIES
    )

    # Fetch raw Lighthouse response for full audit enumeration
    raw = _fetch_raw_psi(url, strategy)

    lighthouse = raw.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})

    # CrUX field data
    crux_page = _parse_crux(raw.get("loadingExperience", {}))
    crux_origin = _parse_crux(raw.get("originLoadingExperience", {}))

    # Field CWV (prefer page-level, fallback origin)
    field_src = crux_page or crux_origin
    cwv_field = {}
    if field_src and field_src.get("metrics"):
        for crux_key, short_key in CRUX_KEY_MAP.items():
            m = field_src["metrics"].get(crux_key)
            if m:
                cwv_field[short_key] = m.get("percentile")

    # Lab CWV from Lighthouse audits
    cwv_lab = {}
    for audit_id, short_key in LAB_KEY_MAP.items():
        audit = audits.get(audit_id)
        if audit:
            cwv_lab[short_key] = audit.get("numericValue")

    return {
        "url":          url,
        "final_url":    lighthouse.get("finalUrl", url),
        "strategy":     strategy,
        "timestamp":    lighthouse.get("fetchTime", datetime.utcnow().isoformat() + "Z"),
        "categories":   categories,
        "audits":       audits,
        "crux_page":    crux_page,
        "crux_origin":  crux_origin,
        "cwv_field":    cwv_field,
        "cwv_lab":      cwv_lab,
        "raw":          raw,
    }


def _fetch_raw_psi(url, strategy="mobile"):
    """Fetch the raw PSI JSON response (full lighthouseResult)."""
    api_key = ""
    if _get_api_key:
        try:
            api_key = _get_api_key()
        except Exception:
            pass

    query_parts = [
        f"url={urllib.parse.quote(url)}",
        f"strategy={strategy}",
    ]
    if api_key:
        query_parts.append(f"key={api_key}")
    for cat in ALL_CATEGORIES:
        query_parts.append(f"category={cat}")

    api_url = (
        "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?"
        + "&".join(query_parts)
    )

    req = urllib.request.Request(api_url, headers={"User-Agent": "PageSpeedDeepDive/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def _parse_crux(crux_data):
    """Parse CrUX loadingExperience into a simple dict."""
    if not crux_data or not crux_data.get("metrics"):
        return None
    result = {
        "category": crux_data.get("overall_category", "N/A"),
        "metrics": {},
    }
    for metric, data in crux_data.get("metrics", {}).items():
        result["metrics"][metric] = {
            "percentile": data.get("percentile", 0),
            "category":   data.get("category", "N/A"),
        }
    return result


# ── 2. classify_audit_results ────────────────────────────────────────────────

def classify_audit_results(lighthouse_data):
    """
    For each audit category (performance, accessibility, best-practices, SEO),
    classify every audit as pass / warn / fail.

    Args:
        lighthouse_data: dict returned by fetch_lighthouse_deep()

    Returns:
        {
            '<category_id>': {
                'title':   category title,
                'score':   0-100,
                'audits':  [
                    {
                        'id':           audit id,
                        'title':        audit title,
                        'score':        raw score (0-1 or None),
                        'displayValue': display value string,
                        'description':  audit description,
                        'status':       'pass' | 'warn' | 'fail' | 'n/a',
                        'scoreDisplayMode': mode,
                        'numericValue':  numeric value or None,
                        'details':      audit details (raw),
                    }, ...
                ],
            }, ...
        }
    """
    categories = lighthouse_data.get("categories", {})
    audits = lighthouse_data.get("audits", {})

    result = {}
    for cat_id, cat_data in categories.items():
        cat_title = cat_data.get("title", CATEGORY_TITLES.get(cat_id, cat_id))
        cat_score = cat_data.get("score")
        cat_score_100 = round(cat_score * 100) if cat_score is not None else None

        audit_refs = cat_data.get("auditRefs", [])
        cat_audits = []
        for aref in audit_refs:
            audit_id = aref.get("id", "")
            audit = audits.get(audit_id)
            if not audit:
                continue
            status = _classify_single_audit(audit)
            cat_audits.append({
                "id":              audit_id,
                "title":           audit.get("title", audit_id),
                "score":           audit.get("score"),
                "displayValue":    audit.get("displayValue", ""),
                "description":     audit.get("description", ""),
                "status":          status,
                "scoreDisplayMode": audit.get("scoreDisplayMode", ""),
                "numericValue":    audit.get("numericValue"),
                "details":         audit.get("details"),
                "weight":          aref.get("weight", 1),
            })

        # Sort: fails first, then warns, then passes — by weight desc within each
        status_order = {"fail": 0, "warn": 1, "n/a": 2, "pass": 3}
        cat_audits.sort(key=lambda a: (status_order.get(a["status"], 9), -(a["weight"] or 0)))

        result[cat_id] = {
            "title":  cat_title,
            "score":  cat_score_100,
            "audits": cat_audits,
        }
    return result


def _classify_single_audit(audit):
    """Classify a single Lighthouse audit as pass/warn/fail/n/a."""
    score = audit.get("score")
    mode = audit.get("scoreDisplayMode", "")

    if mode == "notApplicable" or mode == "manual":
        return "n/a"
    if mode == "informative":
        return "n/a"
    if mode == "error":
        return "fail"
    if score is None:
        return "n/a"
    if score >= 0.9:
        return "pass"
    if score >= 0.5:
        return "warn"
    return "fail"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _classify_cwv_value(short_key, value):
    """Return 'good' / 'needs_improvement' / 'poor' / 'n/a' for a CWV value."""
    if value is None:
        return "n/a"
    thresholds = CWV_THRESHOLDS.get(short_key)
    if not thresholds:
        return "n/a"
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if val < thresholds["good"]:
        return "good"
    elif val < thresholds["ni"]:
        return "needs_improvement"
    else:
        return "poor"


def _rating_color(rating):
    """Map a rating to a CSS color."""
    return {
        "good":               "#16a34a",
        "needs_improvement":  "#f59e0b",
        "poor":               "#dc2626",
        "n/a":                "#9ca3af",
        "pass":               "#16a34a",
        "warn":               "#f59e0b",
        "fail":               "#dc2626",
    }.get(rating, "#6b7280")


def _rating_bg(rating):
    """Map a rating to a light background color."""
    return {
        "good":               "#dcfce7",
        "needs_improvement":  "#fef3c7",
        "poor":               "#fee2e2",
        "n/a":                "#f3f4f6",
        "pass":               "#dcfce7",
        "warn":               "#fef3c7",
        "fail":               "#fee2e2",
    }.get(rating, "#f3f4f6")


def _format_cwv_value(short_key, value):
    """Format a CWV value for display."""
    if value is None:
        return "N/A"
    try:
        val = float(value)
    except (TypeError, ValueError):
        return str(value)
    if short_key == "cls":
        return f"{val:.3f}"
    if val >= 1000:
        return f"{val/1000:.2f}s"
    return f"{val:.0f}ms"


def _score_color(score):
    """Color for a 0-100 score."""
    if score is None:
        return "#9ca3af"
    if score >= 90:
        return "#16a34a"
    if score >= 50:
        return "#f59e0b"
    return "#dc2626"


def _score_label(score):
    """Label for a 0-100 score."""
    if score is None:
        return "N/A"
    if score >= 90:
        return "Good"
    if score >= 50:
        return "Needs Work"
    return "Poor"


def _status_icon(status):
    return {
        "pass": "&#10003;",   # checkmark
        "warn": "&#9888;",    # warning
        "fail": "&#10007;",   # cross
        "n/a":  "&ndash;",
    }.get(status, "&ndash;")


# ── 3. generate_deep_dive_report ─────────────────────────────────────────────

def generate_deep_dive_report(url, data, output_path):
    """
    Generate a comprehensive HTML deep-dive report.

    Args:
        url:         the audited URL
        data:        dict returned by fetch_lighthouse_deep()
        output_path: directory to write report.html + results.json

    Returns:
        report_path (str): path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    report_path = os.path.join(output_path, "report.html")

    classified = classify_audit_results(data)

    # ── Build sections ───────────────────────────────────────────────────────
    cwv_cards_html = _build_cwv_cards(data)
    score_rings_html = _build_score_rings(classified)
    audit_sections_html = _build_audit_sections(classified)
    opportunities_html = _build_opportunities_table(data, classified)
    diagnostics_html = _build_diagnostics_table(classified)
    lab_vs_field_html = _build_lab_vs_field(data)
    action_list_html = _build_action_list(classified, data)

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    html_doc = _HTML_TEMPLATE.format(
        url=html.escape(url),
        final_url=html.escape(data.get("final_url", url)),
        strategy=data.get("strategy", "mobile"),
        timestamp=data.get("timestamp", ""),
        cwv_cards=cwv_cards_html,
        score_rings=score_rings_html,
        audit_sections=audit_sections_html,
        opportunities=opportunities_html,
        diagnostics=diagnostics_html,
        lab_vs_field=lab_vs_field_html,
        action_list=action_list_html,
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    # Also save raw JSON
    json_path = os.path.join(output_path, "results.json")
    safe_data = {k: v for k, v in data.items() if k != "raw"}
    safe_data["raw_lighthouse"] = data.get("raw", {})
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, indent=2, default=str)

    return report_path


# ── Section builders ──────────────────────────────────────────────────────────

def _build_cwv_cards(data):
    """Build Core Web Vitals cards (LCP, INP, FID, CLS, FCP, TTFB)."""
    cards = []
    field = data.get("cwv_field", {})
    lab = data.get("cwv_lab", {})

    for short_key in ["lcp", "inp", "fid", "cls", "fcp", "ttfb"]:
        label = CWV_LABELS.get(short_key, short_key.upper())
        field_val = field.get(short_key)
        lab_val = lab.get(short_key)

        # Prefer field data for rating, fallback to lab
        primary_val = field_val if field_val is not None else lab_val
        rating = _classify_cwv_value(short_key, primary_val)
        color = _rating_color(rating)
        bg = _rating_bg(rating)

        field_display = _format_cwv_value(short_key, field_val) if field_val is not None else "—"
        lab_display = _format_cwv_value(short_key, lab_val) if lab_val is not None else "—"

        cards.append(f"""
        <div class="cwv-card" style="border-top:4px solid {color};background:{bg};">
          <div class="cwv-label">{label}</div>
          <div class="cwv-value" style="color:{color};">{_format_cwv_value(short_key, primary_val)}</div>
          <div class="cwv-rating" style="color:{color};">{rating.replace('_', ' ').title()}</div>
          <div class="cwv-sub">Field: {field_display}</div>
          <div class="cwv-sub">Lab: {lab_display}</div>
        </div>""")

    return "\n".join(cards)


def _build_score_rings(classified):
    """Build circular progress indicators for category scores."""
    rings = []
    for cat_id in ["performance", "accessibility", "best-practices", "seo"]:
        cat = classified.get(cat_id)
        if not cat:
            continue
        score = cat["score"]
        title = cat["title"]
        color = _score_color(score)
        circumference = 2 * 3.14159 * 42
        offset = circumference if score is None else circumference - (score / 100) * circumference
        score_text = str(score) if score is not None else "N/A"
        label = _score_label(score)

        rings.append(f"""
        <div class="score-ring-card">
          <svg class="score-ring" width="120" height="120" viewBox="0 0 100 100">
            <circle cx="50" cy="50" r="42" fill="none" stroke="#e5e7eb" stroke-width="8"/>
            <circle cx="50" cy="50" r="42" fill="none" stroke="{color}" stroke-width="8"
                    stroke-linecap="round" stroke-dasharray="{circumference:.2f}"
                    stroke-dashoffset="{offset:.2f}"
                    transform="rotate(-90 50 50)"/>
          </svg>
          <div class="score-ring-num" style="color:{color};">{score_text}</div>
          <div class="score-ring-label">{title}</div>
          <div class="score-ring-sub" style="color:{color};">{label}</div>
        </div>""")
    return "\n".join(rings)


def _build_audit_sections(classified):
    """Build expandable audit sections grouped by category."""
    sections = []
    for cat_id in ["performance", "accessibility", "best-practices", "seo"]:
        cat = classified.get(cat_id)
        if not cat:
            continue
        title = cat["title"]
        score = cat["score"]
        score_text = f"{score}/100" if score is not None else "N/A"
        color = _score_color(score)
        score_bg = _rating_bg("good" if (score is not None and score >= 90) else
                              "warn" if (score is not None and score >= 50) else "fail")

        rows = []
        for audit in cat["audits"]:
            aid = html.escape(audit["id"])
            atitle = html.escape(audit["title"] or audit["id"])
            desc = html.escape(audit["description"] or "")
            dval = html.escape(str(audit["displayValue"] or ""))
            status = audit["status"]
            scolor = _rating_color(status)
            sbg = _rating_bg(status)
            icon = _status_icon(status)
            details_json = html.escape(json.dumps(audit.get("details") or {}, default=str))
            recommendation = _get_recommendation(audit["id"], audit)

            rows.append(f"""
          <div class="audit-row" onclick="this.classList.toggle('expanded');">
            <div class="audit-header">
              <span class="audit-status" style="background:{sbg};color:{scolor};">{icon}</span>
              <span class="audit-title">{atitle}</span>
              <span class="audit-display">{dval}</span>
              <span class="audit-expand">&#9654;</span>
            </div>
            <div class="audit-detail" style="display:none;">
              <p class="audit-desc">{desc}</p>
              {recommendation}
              <details class="audit-raw">
                <summary>Raw Lighthouse details</summary>
                <pre>{details_json}</pre>
              </details>
            </div>
          </div>""")
        rows_html = "\n".join(rows)

        sections.append(f"""
      <div class="category-section">
        <h3>{title} <span class="cat-score" style="background:{score_bg};color:{color};">{score_text}</span></h3>
        <div class="audit-list">
          {rows_html}
        </div>
      </div>""")
    return "\n".join(sections)


def _get_recommendation(audit_id, audit):
    """Generate a fix recommendation string for an audit."""
    recommendations = {
        "render-blocking-resources": "Eliminate render-blocking resources by inlining critical CSS/JS or deferring non-critical ones with <code>defer</code> or <code>async</code>.",
        "uses-responsive-images": "Serve appropriately sized images using <code>srcset</code> and <code>sizes</code> attributes to avoid downloading oversized images on mobile.",
        "uses-optimized-images": "Compress and convert images to next-gen formats (WebP, AVIF). Use tools like Squoosh, ImageOptim, or a CDN with automatic optimization.",
        "modern-image-formats": "Convert images to WebP or AVIF format. Use <code>&lt;picture&gt;</code> element with fallbacks.",
        "unused-css-rules": "Remove unused CSS rules or split CSS into critical and non-critical. Inline critical CSS and lazy-load the rest.",
        "unused-javascript": "Remove unused JavaScript, use code-splitting, tree-shaking, and dynamic imports.",
        "uses-text-compression": "Enable Brotli or gzip text compression on your server.",
        "total-byte-weight": "Reduce overall page weight by optimizing images, minifying CSS/JS, and removing unnecessary resources.",
        "dom-size": "Reduce DOM node count by simplifying the page structure, using virtual scrolling, or lazy-loading content.",
        "mainthread-work-breakdown": "Reduce JavaScript execution time by breaking up long tasks, using web workers, and removing unused code.",
        "bootup-time": "Reduce JavaScript boot-up time by code-splitting, removing unused polyfills, and deferring non-critical scripts.",
        "server-response-time": "Improve server response time (TTFB) by using a CDN, optimizing database queries, upgrading hosting, and enabling caching.",
        "redirects": "Eliminate unnecessary redirects — each redirect adds latency.",
        "uses-long-cache-ttl": "Set long cache TTL headers (Cache-Control, Expires) for static assets.",
        "efficient-animated-content": "Replace animated GIFs with HTML5 video for better performance.",
        "unminified-javascript": "Minify JavaScript using a build tool (esbuild, terser, webpack).",
        "unminified-css": "Minify CSS using a build tool (cssnano, clean-css).",
        "network-requests": "Reduce the number of network requests by bundling resources and using HTTP/2 multiplexing.",
        "uses-rel-preconnect": "Add <code>rel=&quot;preconnect&quot;</code> hints for important third-party origins.",
        "uses-rel-preload": "Add <code>rel=&quot;preload&quot;</code> hints for critical resources (fonts, LCP image, critical CSS).",
        "font-display": "Set <code>font-display: swap</code> on @font-face declarations to prevent FOIT.",
        "image-size-responsive": "Set explicit width and height attributes on images to prevent layout shifts.",
        "largest-contentful-paint-element": "Optimize the LCP element — preload it, reduce its size, or use a simpler element.",
        "cumulative-layout-shift": "Prevent layout shifts by setting explicit dimensions on images, ads, embeds, and dynamically injected content.",
        "layout-shift-elements": "Set explicit width/height on elements that shift to reduce CLS.",
        "color-contrast": "Improve text color contrast ratios to meet WCAG AA (4.5:1 for normal text, 3:1 for large text).",
        "image-alt": "Add descriptive alt text to all meaningful images for accessibility and SEO.",
        "meta-description": "Add a unique, descriptive meta description (120-160 characters) for the page.",
        "title-element": "Add a descriptive <code>&lt;title&gt;</code> element to the page.",
        "canonical": "Add a <code>rel=&quot;canonical&quot;</code> link tag to prevent duplicate content issues.",
        "robots-txt": "Ensure robots.txt is valid and does not block important resources.",
        "hreflang": "Add correct hreflang annotations for multi-language pages.",
        "viewport": "Add a <code>&lt;meta name=&quot;viewport&quot;&gt;</code> tag for mobile responsiveness.",
        "tap-targets": "Make tap targets at least 48x48px with sufficient spacing between them.",
        "is-on-https": "Serve all resources over HTTPS to protect user data.",
        "uses-http2": "Upgrade to HTTP/2 or HTTP/3 for better connection multiplexing.",
    }
    rec = recommendations.get(audit_id)
    if rec:
        return f'<div class="fix-rec"><strong>Fix:</strong> {rec}</div>'
    # Generic recommendation based on status
    status = audit.get("status")
    if status == "fail":
        return '<div class="fix-rec"><strong>Fix:</strong> Review this audit in the Lighthouse report and address the failing items.</div>'
    elif status == "warn":
        return '<div class="fix-rec"><strong>Fix:</strong> This audit needs improvement — review the details above.</div>'
    return ""


def _build_opportunities_table(data, classified):
    """Build opportunities table — audits with potential savings, sorted."""
    audits = data.get("audits", {})
    opportunities = []
    for aid, audit in audits.items():
        mode = audit.get("scoreDisplayMode", "")
        details = audit.get("details") or {}
        # Look for overallSavingsMs in details
        savings_ms = None
        savings_bytes = None
        if isinstance(details, dict):
            if "overallSavingsMs" in details:
                savings_ms = details["overallSavingsMs"]
            if "overallSavingsBytes" in details:
                savings_bytes = details["overallSavingsBytes"]
        # Also check for numericValue as a proxy
        if savings_ms is None and audit.get("numericValue") and mode != "notApplicable":
            # Only include if it seems like a timing-based opportunity
            pass

        score = audit.get("score")
        if score is not None and score < 0.9 and savings_ms and savings_ms > 0:
            opportunities.append({
                "id":          aid,
                "title":       audit.get("title", aid),
                "savings_ms":  savings_ms,
                "savings_bytes": savings_bytes,
                "display":    audit.get("displayValue", ""),
                "score":       score,
            })

    opportunities.sort(key=lambda x: -x["savings_ms"])

    if not opportunities:
        return '<p class="empty-msg">No performance opportunities detected.</p>'

    rows = []
    for opp in opportunities:
        title = html.escape(opp["title"])
        s_ms = opp["savings_ms"]
        s_str = f"{s_ms/1000:.2f}s" if s_ms >= 1000 else f"{s_ms:.0f}ms"
        s_bytes = opp["savings_bytes"]
        s_bytes_str = f"{s_bytes/1024:.0f}KB" if s_bytes else "—"
        scolor = _rating_color("warn" if opp["score"] < 0.5 else "fail")
        rows.append(f"""
      <tr>
        <td>{title}</td>
        <td style="color:{scolor};font-weight:600;">{s_str}</td>
        <td>{s_bytes_str}</td>
        <td>{html.escape(opp['display'])}</td>
      </tr>""")
    rows_html = "\n".join(rows)

    return f"""
    <table class="data-table">
      <tr><th>Opportunity</th><th>Est. Savings</th><th>Size Savings</th><th>Detail</th></tr>
      {rows_html}
    </table>"""


def _build_diagnostics_table(classified):
    """Build diagnostics table — audits without numeric scores (informational)."""
    diag_rows = []
    for cat_id in ["performance", "accessibility", "best-practices", "seo"]:
        cat = classified.get(cat_id)
        if not cat:
            continue
        for audit in cat["audits"]:
            mode = audit.get("scoreDisplayMode", "")
            if mode in ("notApplicable", "informative", "manual") or audit["score"] is None:
                if mode != "notApplicable":
                    diag_rows.append({
                        "category": cat["title"],
                        "title":    audit["title"],
                        "display":  audit["displayValue"] or "",
                        "mode":     mode,
                    })

    if not diag_rows:
        return '<p class="empty-msg">No additional diagnostics.</p>'

    rows = []
    for d in diag_rows:
        rows.append(f"""
      <tr>
        <td>{html.escape(d['category'])}</td>
        <td>{html.escape(d['title'])}</td>
        <td>{html.escape(str(d['display']))}</td>
        <td><span class="mode-tag">{html.escape(d['mode'])}</span></td>
      </tr>""")
    rows_html = "\n".join(rows)

    return f"""
    <table class="data-table">
      <tr><th>Category</th><th>Diagnostic</th><th>Value</th><th>Type</th></tr>
      {rows_html}
    </table>"""


def _build_lab_vs_field(data):
    """Build lab vs field data comparison table."""
    field = data.get("cwv_field", {})
    lab = data.get("cwv_lab", {})

    if not field and not lab:
        return '<p class="empty-msg">No CrUX field data available for this page.</p>'

    rows = []
    for short_key in ["lcp", "inp", "fid", "cls", "fcp", "ttfb"]:
        label = CWV_LABELS.get(short_key, short_key.upper())
        f_val = field.get(short_key)
        l_val = lab.get(short_key)
        if f_val is None and l_val is None:
            continue
        f_display = _format_cwv_value(short_key, f_val) if f_val is not None else "—"
        l_display = _format_cwv_value(short_key, l_val) if l_val is not None else "—"
        f_rating = _classify_cwv_value(short_key, f_val)
        l_rating = _classify_cwv_value(short_key, l_val)
        f_color = _rating_color(f_rating)
        l_color = _rating_color(l_rating)

        rows.append(f"""
      <tr>
        <td><strong>{label}</strong></td>
        <td style="color:{f_color};font-weight:600;">{f_display}</td>
        <td><span class="rating-tag" style="background:{_rating_bg(f_rating)};color:{f_color};">{f_rating.replace('_',' ').title()}</span></td>
        <td style="color:{l_color};font-weight:600;">{l_display}</td>
        <td><span class="rating-tag" style="background:{_rating_bg(l_rating)};color:{l_color};">{l_rating.replace('_',' ').title()}</span></td>
      </tr>""")
    rows_html = "\n".join(rows)

    return f"""
    <table class="data-table">
      <tr><th>Metric</th><th>Field (CrUX)</th><th>Field Rating</th><th>Lab (Lighthouse)</th><th>Lab Rating</th></tr>
      {rows_html}
    </table>"""


def _build_action_list(classified, data):
    """Build a prioritized action list based on impact."""
    actions = []

    # Collect failed/warn audits with weights
    for cat_id in ["performance", "accessibility", "best-practices", "seo"]:
        cat = classified.get(cat_id)
        if not cat:
            continue
        for audit in cat["audits"]:
            if audit["status"] == "fail":
                actions.append({
                    "priority":   1,
                    "category":   cat["title"],
                    "title":      audit["title"],
                    "status":     "fail",
                    "weight":     audit.get("weight", 1),
                    "id":         audit["id"],
                })
            elif audit["status"] == "warn":
                actions.append({
                    "priority":   2,
                    "category":   cat["title"],
                    "title":      audit["title"],
                    "status":     "warn",
                    "weight":     audit.get("weight", 1),
                    "id":         audit["id"],
                })

    # Sort: fails first (priority 1), then by weight desc, then warns (priority 2)
    actions.sort(key=lambda a: (a["priority"], -a["weight"]))

    if not actions:
        return '<p class="empty-msg">No issues detected — great job!</p>'

    rows = []
    for i, action in enumerate(actions[:20], 1):
        scolor = _rating_color(action["status"])
        sbg = _rating_bg(action["status"])
        icon = _status_icon(action["status"])
        rows.append(f"""
      <tr>
        <td>{i}</td>
        <td><span class="audit-status" style="background:{sbg};color:{scolor};">{icon}</span></td>
        <td>{html.escape(action['category'])}</td>
        <td>{html.escape(action['title'])}</td>
        <td>{action['weight']:.1f}</td>
      </tr>""")
    rows_html = "\n".join(rows)

    return f"""
    <table class="data-table">
      <tr><th>#</th><th>Status</th><th>Category</th><th>Action Item</th><th>Weight</th></tr>
      {rows_html}
    </table>"""


# ── HTML Template ────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="robots" content="noindex, nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Speed Deep Dive — {url}</title>
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
    h1 {{ font-size: 26px; font-weight: 700; color: #111827; margin-bottom: 4px; }}
    h2 {{ font-size: 22px; font-weight: 700; color: #111827; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #e5e7eb; }}
    h3 {{ font-size: 20px; font-weight: 600; color: #374151; margin: 16px 0 8px; display: flex; align-items: center; gap: 12px; }}
    .subtitle {{ color: #6b7280; font-size: 14px; margin-bottom: 24px; }}
    .subtitle a {{ color: #2563eb; text-decoration: none; word-break: break-all; }}

    /* CWV cards */
    .cwv-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .cwv-card {{
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .cwv-label {{ font-size: 12px; color: #6b7280; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
    .cwv-value {{ font-size: 26px; font-weight: 700; margin-bottom: 4px; }}
    .cwv-rating {{ font-size: 13px; font-weight: 600; margin-bottom: 8px; }}
    .cwv-sub {{ font-size: 12px; color: #6b7280; }}

    /* Score rings */
    .score-rings-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      margin-bottom: 32px;
    }}
    .score-ring-card {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 20px;
      text-align: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .score-ring {{
      display: block;
      margin: 0 auto 8px;
    }}
    .score-ring-num {{ font-size: 24px; font-weight: 700; margin-top: -75px; margin-bottom: 8px; }}
    .score-ring-label {{ font-size: 16px; font-weight: 600; color: #374151; }}
    .score-ring-sub {{ font-size: 13px; font-weight: 500; margin-top: 2px; }}

    /* Tables */
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      margin-bottom: 24px;
    }}
    .data-table th, .data-table td {{
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid #f3f4f6;
      font-size: 14px;
    }}
    .data-table th {{ background: #f9fafb; font-weight: 600; color: #374151; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .data-table tr:hover {{ background: #f9fafb; }}

    /* Audit list */
    .category-section {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 24px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .cat-score {{
      display: inline-block;
      font-size: 14px;
      font-weight: 700;
      padding: 2px 12px;
      border-radius: 6px;
    }}
    .audit-list {{ margin-top: 12px; }}
    .audit-row {{
      border: 1px solid #f3f4f6;
      border-radius: 6px;
      margin-bottom: 8px;
      overflow: hidden;
    }}
    .audit-row.expanded .audit-detail {{ display: block !important; }}
    .audit-row.expanded .audit-expand {{ transform: rotate(90deg); }}
    .audit-header {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      cursor: pointer;
    }}
    .audit-header:hover {{ background: #f9fafb; }}
    .audit-status {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      font-size: 14px;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .audit-title {{ flex: 1; font-weight: 600; font-size: 14px; color: #1f2937; }}
    .audit-display {{ font-size: 13px; color: #6b7280; font-weight: 500; }}
    .audit-expand {{ font-size: 12px; color: #9ca3af; transition: transform 0.2s; flex-shrink: 0; }}
    .audit-detail {{
      padding: 12px 14px 16px 48px;
      border-top: 1px solid #f3f4f6;
      background: #f9fafb;
    }}
    .audit-desc {{ font-size: 14px; color: #4b5563; margin-bottom: 8px; }}
    .fix-rec {{
      font-size: 14px;
      color: #1f2937;
      background: #eff6ff;
      border-left: 3px solid #2563eb;
      padding: 8px 12px;
      border-radius: 0 6px 6px 0;
      margin-bottom: 8px;
    }}
    .fix-rec code {{ background: #dbeafe; padding: 1px 4px; border-radius: 3px; font-size: 13px; }}
    .audit-raw {{ margin-top: 8px; }}
    .audit-raw summary {{ cursor: pointer; font-size: 13px; color: #2563eb; }}
    .audit-raw pre {{
      font-size: 12px;
      background: #f3f4f6;
      padding: 12px;
      border-radius: 6px;
      overflow-x: auto;
      max-height: 300px;
      margin-top: 8px;
    }}

    /* Rating tags */
    .rating-tag {{
      display: inline-block;
      font-size: 12px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .mode-tag {{
      display: inline-block;
      font-size: 11px;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 4px;
      background: #f3f4f6;
      color: #6b7280;
    }}

    .empty-msg {{ color: #9ca3af; font-size: 14px; padding: 16px; text-align: center; }}

    .footer {{
      margin-top: 40px;
      padding-top: 16px;
      border-top: 1px solid #e5e7eb;
      font-size: 12px;
      color: #9ca3af;
      text-align: center;
    }}

    @media (max-width: 768px) {{
      .score-rings-grid {{ grid-template-columns: 1fr 1fr; }}
      .cwv-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Page Speed Deep Dive Report</h1>
    <p class="subtitle">URL: <a href="{url}" rel="nofollow">{url}</a><br>
       Final URL: <a href="{final_url}" rel="nofollow">{final_url}</a><br>
       Strategy: {strategy} &middot; Audited: {timestamp}</p>

    <h2>Core Web Vitals</h2>
    <div class="cwv-grid">
      {cwv_cards}
    </div>

    <h2>Lighthouse Category Scores</h2>
    <div class="score-rings-grid">
      {score_rings}
    </div>

    <h2>All Audit Results (by Category)</h2>
    {audit_sections}

    <h2>Opportunities (Sorted by Estimated Savings)</h2>
    {opportunities}

    <h2>Diagnostics</h2>
    {diagnostics}

    <h2>Lab Data vs Field Data</h2>
    {lab_vs_field}

    <h2>Prioritized Action List</h2>
    {action_list}

    <div class="footer">
      Generated by page_speed_deep_dive.py &mdash; Lighthouse + CrUX data from Google PSI API
    </div>
  </div>
</body>
</html>"""


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Single-page deep Lighthouse audit with full audit detail"
    )
    parser.add_argument("--url", required=True, help="Page URL to audit")
    parser.add_argument("--strategy", default="mobile",
                        choices=["mobile", "desktop"],
                        help="Audit strategy (default: mobile)")
    parser.add_argument("--output", default="/tmp/deep_dive",
                        help="Output directory (default: /tmp/deep_dive)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Page Speed Deep Dive — {args.url}")
    print(f"  Strategy: {args.strategy}")
    print(f"  Output:   {args.output}")
    print(f"{'='*60}\n")

    print("Fetching Lighthouse data from Google PSI API...")
    data = fetch_lighthouse_deep(args.url, strategy=args.strategy)

    print(f"  Categories: {len(data.get('categories', {}))}")
    print(f"  Total audits: {len(data.get('audits', {}))}")
    print(f"  CrUX page data: {'yes' if data.get('crux_page') else 'no'}")
    print(f"  CrUX origin data: {'yes' if data.get('crux_origin') else 'no'}")

    print("\nClassifying audit results...")
    classified = classify_audit_results(data)
    for cat_id, cat in classified.items():
        audit_count = len(cat["audits"])
        fails = sum(1 for a in cat["audits"] if a["status"] == "fail")
        warns = sum(1 for a in cat["audits"] if a["status"] == "warn")
        passes = sum(1 for a in cat["audits"] if a["status"] == "pass")
        print(f"  {cat['title']:20s} Score={cat['score']} | "
              f"{passes} pass, {warns} warn, {fails} fail ({audit_count} total)")

    print("\nGenerating HTML report...")
    report_path = generate_deep_dive_report(args.url, data, args.output)

    print(f"\n{'='*60}")
    print(f"  Deep dive complete!")
    print(f"  Report: {report_path}")
    print(f"  JSON:   {os.path.join(args.output, 'results.json')}")
    print(f"{'='*60}\n")

    return data


if __name__ == "__main__":
    main()
