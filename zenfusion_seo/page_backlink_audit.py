#!/usr/bin/env python3
"""
Zenfusion Page Backlink Audit

Audits all backlinks pointing to a SPECIFIC page URL (not a domain).
Uses DataForSEO's backlinks/live and summary/live endpoints, cross-references
against the Majestic Million index, and produces a white-themed SaaS HTML report.

Functions:
  1. fetch_page_backlinks(url, limit=1000)
  2. analyze_backlink_quality(backlinks)
  3. generate_page_backlink_report(url, backlinks, output_path)

Usage:
  python3 page_backlink_audit.py --url <page_url> [--output <dir>] [--limit <n>]

Example:
  python3 page_backlink_audit.py \
    --url https://www.softprowatersystems.com/pages/best-water-softener-for-city-water \
    --output /tmp/test_page_backlinks

DataForSEO credentials in ~/.hermes/.env:
  DATAFORSEO_LOGIN
  DATAFORSEO_PASSWORD

Majestic Million index:
  ~/.hermes/audits/factor-engine-data/majestic_million_index.json  (dict: domain -> rank)
"""

import sys
import os
import json
import base64
import urllib.request
import argparse
import re
import time
from datetime import datetime
from urllib.parse import urlparse, unquote
from collections import Counter, defaultdict

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

ENV_PATH = os.path.expanduser("~/.hermes/.env")
MAJESTIC_INDEX = os.path.expanduser(
    "~/.hermes/audits/factor-engine-data/majestic_million_index.json"
)
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/.hermes/audits/page-backlinks")
DFS_BASE = "https://api.dataforseo.com/v3"

# Tier thresholds (DataForSEO domain_from_rank is 0-1000 scale)
TIER1_DS = 700      # domain_strength >= 700 → Tier 1 (high authority)
TIER2_DS = 400      # domain_strength >= 400 → Tier 2 (moderate)
TIER1_MAJ = 10000   # Majestic rank <= 10k   → Tier 1
TIER2_MAJ = 100000  # Majestic rank <= 100k  → Tier 2


# ---------------------------------------------------------------------------
# Credentials & API helpers
# ---------------------------------------------------------------------------

def get_creds():
    """Load DataForSEO credentials from environment or ~/.hermes/.env."""
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATAFORSEO_LOGIN="):
                        login = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("DATAFORSEO_PASSWORD="):
                        password = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.")
        print("Set them in ~/.hermes/.env or as environment variables.")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """POST to a DataForSEO v3 live endpoint.

    ``payload`` is a single task dict; it is wrapped in an array per
    DataForSEO convention.
    """
    login, password = get_creds()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"{DFS_BASE}/{endpoint}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    body = json.dumps([payload]).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if data.get("status_code") != 20000:
            err = data.get("status_message", "Unknown error")
            print(f"  API error ({data.get('status_code')}): {err}")
            return None
        return data
    except Exception as e:
        print(f"  Request error ({endpoint}): {e}")
        return None


def dfs_post_raw(endpoint, payload, timeout=120):
    """Low-level POST that returns the raw parsed JSON (no status filtering)."""
    login, password = get_creds()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"{DFS_BASE}/{endpoint}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    body = json.dumps([payload]).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Request error ({endpoint}): {e}")
        return None


# ---------------------------------------------------------------------------
# Majestic Million
# ---------------------------------------------------------------------------

_majestic_cache = None


def load_majestic():
    """Load the Majestic Million index (dict: domain -> rank)."""
    global _majestic_cache
    if _majestic_cache is not None:
        return _majestic_cache
    if os.path.exists(MAJESTIC_INDEX):
        try:
            with open(MAJESTIC_INDEX) as f:
                _majestic_cache = json.load(f)
            return _majestic_cache
        except Exception as e:
            print(f"  Warning: could not load Majestic index: {e}")
    return {}


def majestic_rank(domain, majestic_db):
    """Return Majestic Million rank for domain, or None if not in top 1M."""
    if not domain or not majestic_db:
        return None
    d = domain.lower().lstrip("www.")
    rank = majestic_db.get(d)
    if rank is None:
        # Try with www.
        rank = majestic_db.get("www." + d)
    return rank


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def normalize_domain(url_or_domain):
    """Extract bare domain (without www, scheme, path) from a URL or domain string."""
    if not url_or_domain:
        return ""
    s = url_or_domain.strip().lower()
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    parsed = urlparse(s)
    domain = parsed.netloc or parsed.path
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def extract_keyword_from_url(url):
    """Extract a target keyword from the URL path for anchor classification."""
    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    # Take the last meaningful segment
    segments = [s for s in path.split("/") if s and s not in ("pages",)]
    if not segments:
        return ""
    last = segments[-1]
    # Replace hyphens/underscores with spaces
    keyword = re.sub(r"[-_]+", " ", last).strip().lower()
    return keyword


def extract_brand_from_url(url):
    """Extract a brand name from the domain."""
    domain = normalize_domain(url)
    # Remove TLD
    parts = domain.split(".")
    if len(parts) >= 2:
        brand = parts[0]
    else:
        brand = domain
    return brand.lower()


def html_escape(s):
    """Escape HTML special characters."""
    if not s:
        return ""
    s = str(s)
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    s = s.replace("'", "&#39;")
    return s


# ---------------------------------------------------------------------------
# 1. fetch_page_backlinks
# ---------------------------------------------------------------------------

def fetch_page_backlinks(url, limit=1000):
    """Fetch all backlinks pointing to a specific page URL.

    Calls DataForSEO /v3/backlinks/backlinks/live with the page URL as
    ``target`` and ``target_type`` set to ``"page"``.

    Returns a list of dicts, each with:
      source_url, target_url, anchor, domain_from, domain_to,
      links_type, is_dofollow, first_seen, last_seen,
      domain_strength, item_type
    """
    payload = {
        "target": url,
        "target_type": "page",
        "mode": "as_is",
        "limit": limit,
        "include_subdomains": True,
    }
    data = dfs_api_call("backlinks/backlinks/live", payload)
    if not data or not data.get("tasks"):
        return []

    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        print(f"  Task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get("result", [{}])[0]
    items = result.get("items", [])

    backlinks = []
    for item in items:
        links_type = item.get("type", "")
        is_dofollow = links_type == "dofollow"
        # item_type from DataForSEO: text_link, image_link, redirect, etc.
        item_type = item.get("item_type", item.get("item_type_code", ""))

        backlinks.append(
            {
                "source_url": item.get("url_from", ""),
                "target_url": item.get("url_to", ""),
                "anchor": item.get("anchor", ""),
                "domain_from": (item.get("domain_from", "") or "").lower(),
                "domain_to": (item.get("domain_to", "") or "").lower(),
                "links_type": links_type,
                "is_dofollow": is_dofollow,
                "first_seen": (item.get("first_seen", "") or "")[:10],
                "last_seen": (item.get("last_seen", "") or "")[:10],
                "domain_strength": item.get("domain_from_rank", 0) or 0,
                "item_type": item_type,
            }
        )

    return backlinks


def fetch_page_backlinks_summary(url):
    """Fetch aggregate backlink metrics for a specific page URL.

    Calls /v3/backlinks/summary/live with the page URL as target.
    Returns a dict with backlinks, referring_domains, dofollow/nofollow counts,
    new_backlinks, lost_backlinks, etc.
    """
    payload = {
        "target": url,
        "target_type": "page",
        "include_subdomains": True,
    }
    data = dfs_api_call("backlinks/summary/live", payload)
    if not data or not data.get("tasks"):
        return {}

    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        print(f"  Summary task error: {task.get('status_message', 'Unknown')}")
        return {}

    result = task.get("result", [{}])[0]
    return {
        "backlinks": result.get("backlinks", 0),
        "referring_domains": result.get("referring_domains", 0),
        "referring_main_domains": result.get("referring_main_domains", 0),
        "backlinks_dofollow": result.get("backlinks_dofollow", 0),
        "backlinks_nofollow": result.get("backlinks_nofollow", 0),
        "lost_backlinks": result.get("lost_backlinks", 0),
        "new_backlinks": result.get("new_backlinks", 0),
    }


# ---------------------------------------------------------------------------
# 2. analyze_backlink_quality
# ---------------------------------------------------------------------------

def _classify_tier(domain_strength, maj_rank):
    """Classify a backlink into Tier 1, 2, or 3.

    Tier 1: domain_strength >= 70  OR  Majestic rank <= 10,000
    Tier 2: domain_strength >= 40  OR  Majestic rank <= 100,000
    Tier 3: everything else
    """
    if domain_strength >= TIER1_DS or (maj_rank is not None and maj_rank <= TIER1_MAJ):
        return "Tier 1"
    if domain_strength >= TIER2_DS or (maj_rank is not None and maj_rank <= TIER2_MAJ):
        return "Tier 2"
    return "Tier 3"


def _classify_anchor(anchor_text, keyword, brand):
    """Classify anchor text as exact_match, partial_match, branded, or generic."""
    anchor = (anchor_text or "").strip().lower()
    if not anchor:
        return "empty"
    kw = (keyword or "").strip().lower()

    if kw:
        # Exact match: anchor is exactly the keyword
        if anchor == kw:
            return "exact_match"
        # Partial match: keyword is a substring of the anchor
        if kw in anchor:
            return "partial_match"

    if brand:
        # Branded: anchor contains the brand name
        if brand in anchor:
            return "branded"

    return "generic"


def analyze_backlink_quality(backlinks, target_url=""):
    """Enrich each backlink dict with quality metrics.

    For each backlink adds:
      - majestic_rank: rank in Majestic Million (int or None)
      - in_majestic: bool
      - tier: "Tier 1" / "Tier 2" / "Tier 3"
      - anchor_type: "exact_match" / "partial_match" / "branded" / "generic" / "empty"

    Returns the enriched list (same list, mutated in-place + returned).
    """
    majestic_db = load_majestic()
    keyword = extract_keyword_from_url(target_url) if target_url else ""
    brand = extract_brand_from_url(target_url) if target_url else ""

    for bl in backlinks:
        domain = bl.get("domain_from", "")
        maj_r = majestic_rank(domain, majestic_db)
        bl["majestic_rank"] = maj_r
        bl["in_majestic"] = maj_r is not None

        ds = bl.get("domain_strength", 0) or 0
        bl["tier"] = _classify_tier(ds, maj_r)

        bl["anchor_type"] = _classify_anchor(bl.get("anchor", ""), keyword, brand)

    return backlinks


# ---------------------------------------------------------------------------
# 3. generate_page_backlink_report
# ---------------------------------------------------------------------------

def generate_page_backlink_report(url, backlinks, output_path):
    """Generate a white-themed SaaS HTML report for page-level backlinks.

    Sections:
      - Summary cards (total backlinks, referring domains, dofollow, avg DS)
      - Backlink quality distribution (Tier 1/2/3)
      - Anchor text analysis (exact/partial/branded/generic)
      - Top referring domains table (expandable)
      - New vs lost backlinks timeline
      - Full backlink table with expandable rows
      - Recommendations for link building

    ``output_path`` is a directory; the HTML file is written inside it.
    Returns the path to the generated HTML file.
    """
    target_domain = normalize_domain(url)
    keyword = extract_keyword_from_url(url)
    brand = extract_brand_from_url(url)
    gen_date = datetime.now().isoformat(timespec="seconds")

    os.makedirs(output_path, exist_ok=True)

    # --- Compute aggregate metrics ---
    total = len(backlinks)
    referring_domains = set()
    dofollow_count = 0
    nofollow_count = 0
    domain_strengths = []
    tier_counts = Counter()
    anchor_type_counts = Counter()
    domain_link_counts = defaultdict(int)
    domain_dofollow = defaultdict(int)
    domain_strength_map = {}

    timeline_data = defaultdict(lambda: {"new": 0, "lost": 0})

    for bl in backlinks:
        d = bl.get("domain_from", "")
        if d:
            referring_domains.add(d)
            domain_link_counts[d] += 1
            if bl.get("is_dofollow"):
                domain_dofollow[d] += 1
            ds = bl.get("domain_strength", 0) or 0
            domain_strength_map[d] = max(domain_strength_map.get(d, 0), ds)

        if bl.get("is_dofollow"):
            dofollow_count += 1
        else:
            nofollow_count += 1

        ds = bl.get("domain_strength", 0) or 0
        if ds:
            domain_strengths.append(ds)

        tier_counts[bl.get("tier", "Tier 3")] += 1
        anchor_type_counts[bl.get("anchor_type", "generic")] += 1

        # Timeline: group by first_seen month
        first_seen = bl.get("first_seen", "")
        if first_seen and len(first_seen) >= 7:
            month = first_seen[:7]
            timeline_data[month]["new"] += 1

        # Lost backlinks: last_seen much older than first_seen (rough heuristic)
        last_seen = bl.get("last_seen", "")
        if first_seen and last_seen and len(last_seen) >= 7:
            lost_month = last_seen[:7]
            if not bl.get("is_dofollow"):
                timeline_data[lost_month]["lost"] += 1

    avg_ds = round(sum(domain_strengths) / len(domain_strengths), 1) if domain_strengths else 0
    total_ref_domains = len(referring_domains)

    # --- Top referring domains (sorted by backlink count) ---
    top_domains = sorted(
        domain_link_counts.items(),
        key=lambda x: x[1],
        reverse=True,
    )[:20]

    # --- Timeline (sorted chronologically) ---
    sorted_months = sorted(timeline_data.keys())

    # --- Recommendations ---
    recommendations = _build_recommendations(
        backlinks,
        total,
        dofollow_count,
        nofollow_count,
        total_ref_domains,
        avg_ds,
        tier_counts,
        anchor_type_counts,
        keyword,
        brand,
    )

    # --- Build HTML ---
    html = _build_html(
        url=url,
        target_domain=target_domain,
        keyword=keyword,
        brand=brand,
        gen_date=gen_date,
        total=total,
        total_ref_domains=total_ref_domains,
        dofollow_count=dofollow_count,
        nofollow_count=nofollow_count,
        avg_ds=avg_ds,
        tier_counts=tier_counts,
        anchor_type_counts=anchor_type_counts,
        top_domains=top_domains,
        domain_link_counts=domain_link_counts,
        domain_dofollow=domain_dofollow,
        domain_strength_map=domain_strength_map,
        sorted_months=sorted_months,
        timeline_data=timeline_data,
        backlinks=backlinks,
        recommendations=recommendations,
    )

    # Write file
    safe_domain = re.sub(r"[^a-zA-Z0-9.-]", "_", target_domain)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"page_backlink_audit_{safe_domain}_{timestamp}.html"
    html_path = os.path.join(output_path, filename)
    with open(html_path, "w") as f:
        f.write(html)

    # Also save JSON
    json_path = os.path.join(output_path, filename.replace(".html", ".json"))
    with open(json_path, "w") as f:
        json.dump(
            {
                "url": url,
                "target_domain": target_domain,
                "keyword": keyword,
                "brand": brand,
                "generated_at": gen_date,
                "total_backlinks": total,
                "referring_domains": total_ref_domains,
                "dofollow_count": dofollow_count,
                "nofollow_count": nofollow_count,
                "avg_domain_strength": avg_ds,
                "tier_distribution": dict(tier_counts),
                "anchor_type_distribution": dict(anchor_type_counts),
                "top_referring_domains": [
                    {
                        "domain": d,
                        "backlinks": c,
                        "dofollow": domain_dofollow.get(d, 0),
                        "domain_strength": domain_strength_map.get(d, 0),
                    }
                    for d, c in top_domains
                ],
                "backlinks": backlinks,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    return html_path


def _build_recommendations(
    backlinks,
    total,
    dofollow,
    nofollow,
    ref_domains,
    avg_ds,
    tier_counts,
    anchor_counts,
    keyword,
    brand,
):
    """Build a list of recommendation strings based on the analysis."""
    recs = []

    # Dofollow ratio
    if total > 0:
        df_ratio = dofollow / total * 100
        if df_ratio < 30:
            recs.append(
                f"Low dofollow ratio ({df_ratio:.0f}%). Most links are nofollow — "
                f"prioritize acquiring dofollow links from authoritative sites to "
                f"pass link equity."
            )
        elif df_ratio > 80:
            recs.append(
                f"High dofollow ratio ({df_ratio:.0f}%). Consider a more natural "
                f"mix — some nofollow links from high-authority sites signal a "
                f"organic profile."
            )

    # Domain strength (DataForSEO domain_from_rank is 0-1000 scale)
    if avg_ds < 200:
        recs.append(
            f"Average domain strength is low ({avg_ds}/1000). Focus outreach on "
            f"higher-authority domains (DS >= 700) to boost page authority."
        )
    elif avg_ds < 400:
        recs.append(
            f"Average domain strength is moderate ({avg_ds}/1000). Target domains "
            f"with DS >= 700 for Tier 1 link building."
        )

    # Tier distribution
    tier1_pct = (tier_counts.get("Tier 1", 0) / total * 100) if total else 0
    if tier1_pct < 10:
        recs.append(
            f"Only {tier1_pct:.0f}% of backlinks are Tier 1 (DS >= 700 or "
            f"Majestic top 10k). Prioritize outreach to high-authority domains."
        )

    # Anchor text
    exact_pct = (anchor_counts.get("exact_match", 0) / total * 100) if total else 0
    branded_pct = (anchor_counts.get("branded", 0) / total * 100) if total else 0
    generic_pct = (anchor_counts.get("generic", 0) / total * 100) if total else 0

    if exact_pct > 40:
        recs.append(
            f"High exact-match anchor ratio ({exact_pct:.0f}%). This risks "
            f"over-optimization penalties — diversify with branded and "
            f"partial-match anchors."
        )
    elif exact_pct < 5 and keyword:
        recs.append(
            f"Very few exact-match anchors ({exact_pct:.0f}%). Consider "
            f"requesting keyword-rich anchors for some future links targeting "
            f"'{keyword}'."
        )

    if branded_pct < 20:
        recs.append(
            f"Low branded anchor ratio ({branded_pct:.0f}%). Build brand "
            f"awareness — branded anchors are natural and safe."
        )

    if generic_pct > 50:
        recs.append(
            f"High generic anchor ratio ({generic_pct:.0f}%). Generic anchors "
            f"like 'click here' don't contribute relevance signal — aim for "
            f"more descriptive anchors."
        )

    # Referring domains diversity
    if total > 0:
        ratio = total / ref_domains if ref_domains else 0
        if ratio > 5:
            recs.append(
                f"High links-per-domain ratio ({ratio:.1f}). Many links come "
                f"from few domains — diversify referring domains for a "
                f"healthier profile."
            )
        if ref_domains < 10:
            recs.append(
                f"Only {ref_domains} referring domains. Focus on domain "
                f"diversification — acquire links from new referring domains."
            )

    if not recs:
        recs.append(
            "Backlink profile looks healthy. Continue building diverse, "
            "high-quality links from authoritative domains."
        )

    return recs


def _build_html(
    url,
    target_domain,
    keyword,
    brand,
    gen_date,
    total,
    total_ref_domains,
    dofollow_count,
    nofollow_count,
    avg_ds,
    tier_counts,
    anchor_type_counts,
    top_domains,
    domain_link_counts,
    domain_dofollow,
    domain_strength_map,
    sorted_months,
    timeline_data,
    backlinks,
    recommendations,
):
    """Build the full HTML document string."""

    # --- Summary cards ---
    df_pct = (dofollow_count / total * 100) if total else 0

    # --- Tier distribution bars ---
    tier_labels = ["Tier 1", "Tier 2", "Tier 3"]
    tier_colors = {"Tier 1": "#16a34a", "Tier 2": "#d97706", "Tier 3": "#94a3b8"}
    tier_bars = []
    for tier in tier_labels:
        count = tier_counts.get(tier, 0)
        pct = (count / total * 100) if total else 0
        tier_bars.append(
            f"""
        <div class="tier-row">
          <span class="tier-label" style="color:{tier_colors[tier]}">{tier}</span>
          <div class="tier-bar-wrap">
            <div class="tier-bar" style="width:{pct:.1f}%;background:{tier_colors[tier]}"></div>
          </div>
          <span class="tier-count">{count} ({pct:.0f}%)</span>
        </div>"""
        )

    # --- Anchor analysis bars ---
    anchor_labels = [
        ("exact_match", "Exact Match", "#2563eb"),
        ("partial_match", "Partial Match", "#7c3aed"),
        ("branded", "Branded", "#16a34a"),
        ("generic", "Generic", "#d97706"),
        ("empty", "Empty", "#94a3b8"),
    ]
    anchor_bars = []
    for key, label, color in anchor_labels:
        count = anchor_type_counts.get(key, 0)
        pct = (count / total * 100) if total else 0
        anchor_bars.append(
            f"""
        <div class="tier-row">
          <span class="tier-label" style="color:{color}">{label}</span>
          <div class="tier-bar-wrap">
            <div class="tier-bar" style="width:{pct:.1f}%;background:{color}"></div>
          </div>
          <span class="tier-count">{count} ({pct:.0f}%)</span>
        </div>"""
        )

    # --- Top referring domains table rows (expandable) ---
    top_domain_rows = []
    for i, (d, count) in enumerate(top_domains, 1):
        ds = domain_strength_map.get(d, 0)
        df = domain_dofollow.get(d, 0)
        nf = count - df
        top_domain_rows.append(
            f"""
        <tr class="expandable" data-domain="{html_escape(d)}">
          <td class="rank">{i}</td>
          <td class="domain"><a href="https://{html_escape(d)}" target="_blank" rel="noopener noindex nofollow">{html_escape(d)}</a></td>
          <td class="num">{count}</td>
          <td class="num">{df}</td>
          <td class="num">{nf}</td>
          <td class="num">{ds}</td>
        </tr>
        <tr class="expand-detail" style="display:none">
          <td colspan="6">
            <div class="detail-box">
              <strong>All backlinks from {html_escape(d)}:</strong>
              <ul class="detail-list">
                {"".join(
                    f'<li><a href="{html_escape(bl.get("source_url",""))}" target="_blank" rel="noopener noindex nofollow">{html_escape(bl.get("source_url","")[:100])}</a> — anchor: &quot;{html_escape(bl.get("anchor","")[:80])}&quot; — {"DoFollow" if bl.get("is_dofollow") else "NoFollow"} — {html_escape(bl.get("first_seen","") or "—")}</li>'
                    for bl in backlinks
                    if bl.get("domain_from") == d
                )}
              </ul>
            </div>
          </td>
        </tr>"""
        )

    # --- Timeline ---
    timeline_bars = ""
    max_timeline = 1
    for m in sorted_months:
        val = timeline_data[m]["new"] + timeline_data[m]["lost"]
        if val > max_timeline:
            max_timeline = val

    if sorted_months:
        timeline_items = []
        for m in sorted_months:
            new_val = timeline_data[m]["new"]
            lost_val = timeline_data[m]["lost"]
            new_h = (new_val / max_timeline * 100) if max_timeline else 0
            lost_h = (lost_val / max_timeline * 100) if max_timeline else 0
            timeline_items.append(
                f"""
          <div class="tl-month">
            <div class="tl-bars">
              <div class="tl-bar tl-new" style="height:{new_h:.0f}%" title="{new_val} new"></div>
              <div class="tl-bar tl-lost" style="height:{lost_h:.0f}%" title="{lost_val} lost"></div>
            </div>
            <div class="tl-label">{m}</div>
            <div class="tl-count">{new_val}+/{lost_val}-</div>
          </div>"""
            )
        timeline_bars = "".join(timeline_items)
    else:
        timeline_bars = (
            '<p style="color:#94a3b8;padding:20px 0">No timeline data '
            "available (first_seen/last_seen not present).</p>"
        )

    # --- Full backlink table rows (expandable) ---
    # Sort by domain_strength descending
    sorted_backlinks = sorted(
        backlinks, key=lambda x: x.get("domain_strength", 0) or 0, reverse=True
    )
    full_rows = []
    for i, bl in enumerate(sorted_backlinks, 1):
        ds = bl.get("domain_strength", 0) or 0
        anchor = bl.get("anchor", "") or ""
        anchor_type = bl.get("anchor_type", "generic")
        df_badge = (
            '<span class="badge badge-df">DF</span>'
            if bl.get("is_dofollow")
            else '<span class="badge badge-nf">NF</span>'
        )
        tier = bl.get("tier", "Tier 3")
        tier_badge = f'<span class="badge badge-tier" style="background:{tier_colors.get(tier,"#94a3b8")}20;color:{tier_colors.get(tier,"#94a3b8")}">{tier}</span>'
        maj_r = bl.get("majestic_rank")
        maj_display = f"#{maj_r:,}" if maj_r else "—"
        source_url = bl.get("source_url", "")
        source_domain = bl.get("domain_from", "")
        link_type = bl.get("links_type", "") or bl.get("item_type", "") or "—"
        first_seen = bl.get("first_seen", "") or "—"
        last_seen = bl.get("last_seen", "") or "—"

        full_rows.append(
            f"""
        <tr class="expandable" data-bl-idx="{i}">
          <td class="rank">{i}</td>
          <td class="url-cell"><a href="{html_escape(source_url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(source_url[:80])}</a><div class="src-domain">{html_escape(source_domain)}</div></td>
          <td class="anchor" title="{html_escape(anchor)}">{html_escape(anchor[:60])}<div class="anchor-type">{anchor_type}</div></td>
          <td class="num">{ds}</td>
          <td>{df_badge}</td>
          <td class="num">{html_escape(link_type)}</td>
          <td class="num">{maj_display}</td>
          <td class="num">{first_seen}</td>
        </tr>
        <tr class="expand-detail" style="display:none">
          <td colspan="8">
            <div class="detail-box">
              <table class="detail-table">
                <tr><td>Source URL</td><td><a href="{html_escape(source_url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(source_url)}</a></td></tr>
                <tr><td>Target URL</td><td>{html_escape(bl.get("target_url",""))}</td></tr>
                <tr><td>Anchor Text</td><td>&quot;{html_escape(anchor)}&quot; ({anchor_type})</td></tr>
                <tr><td>Domain From</td><td>{html_escape(source_domain)} (DS: {ds}, Majestic: {maj_display})</td></tr>
                <tr><td>Link Type</td><td>{html_escape(link_type)} ({"DoFollow" if bl.get("is_dofollow") else "NoFollow"})</td></tr>
                <tr><td>First Seen</td><td>{first_seen}</td></tr>
                <tr><td>Last Seen</td><td>{last_seen}</td></tr>
                <tr><td>Quality Tier</td><td>{tier_badge}</td></tr>
              </table>
            </div>
          </td>
        </tr>"""
        )

    # --- Recommendation cards ---
    rec_cards = []
    for i, rec in enumerate(recommendations, 1):
        rec_cards.append(
            f"""
        <div class="rec-card">
          <div class="rec-rank">{i}</div>
          <div class="rec-body">{html_escape(rec)}</div>
        </div>"""
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Page Backlink Audit — {html_escape(target_domain)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #ffffff;
    color: #1a1a1a;
    font-size: 16px;
    line-height: 1.6;
    padding: 20px 40px;
    max-width: 1400px;
    margin: 0 auto;
  }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h1 {{
    font-size: 24px;
    margin-bottom: 4px;
    font-weight: 700;
    color: #1a1a1a;
  }}
  h2 {{
    font-size: 20px;
    margin-bottom: 16px;
    margin-top: 36px;
    padding-bottom: 10px;
    border-bottom: 1px solid #e2e8f0;
    color: #1a1a1a;
  }}
  .subtitle {{
    color: #64748b;
    font-size: 14px;
    margin-bottom: 30px;
  }}
  .subtitle a {{ color: #0066cc; }}
  .generated {{ color: #94a3b8; font-size: 12px; }}

  /* Summary cards */
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 36px;
  }}
  .card {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .card-label {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #64748b;
    margin-bottom: 8px;
  }}
  .card-value {{
    font-size: 32px;
    font-weight: 700;
    color: #1a1a1a;
  }}
  .card-sub {{
    font-size: 13px;
    color: #94a3b8;
    margin-top: 4px;
  }}

  /* Tier & anchor distribution */
  .dist-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-bottom: 36px;
  }}
  .dist-card {{
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .dist-title {{
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    color: #1a1a1a;
  }}
  .tier-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
  }}
  .tier-label {{
    width: 100px;
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
  }}
  .tier-bar-wrap {{
    flex: 1;
    height: 22px;
    background: #f1f5f9;
    border-radius: 4px;
    overflow: hidden;
  }}
  .tier-bar {{
    height: 100%;
    border-radius: 4px;
    min-width: 2px;
    transition: width 0.3s;
  }}
  .tier-count {{
    font-size: 13px;
    color: #64748b;
    white-space: nowrap;
    min-width: 80px;
    text-align: right;
  }}

  /* Tables */
  .table-wrap {{
    overflow-x: auto;
    margin-bottom: 36px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    background: #fff;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  th {{
    text-align: left;
    padding: 10px 12px;
    background: #f8f9fb;
    color: #475569;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.05em;
    border-bottom: 2px solid #e2e8f0;
    position: sticky;
    top: 0;
  }}
  td {{
    padding: 8px 12px;
    border-bottom: 1px solid #f1f5f9;
    color: #334155;
  }}
  tr:hover td {{ background: #f8f9fb; }}
  tr.expandable {{ cursor: pointer; }}
  tr.expand-detail td {{ background: #f8f9fb; padding: 12px 20px; }}
  .detail-box {{ padding: 8px 0; }}
  .detail-list {{
    list-style: none;
    margin-top: 8px;
    font-size: 12px;
    max-height: 300px;
    overflow-y: auto;
  }}
  .detail-list li {{
    padding: 4px 0;
    border-bottom: 1px dashed #e2e8f0;
  }}
  .detail-table {{
    width: 100%;
    font-size: 12px;
    box-shadow: none;
  }}
  .detail-table td {{
    padding: 4px 8px;
    border-bottom: 1px solid #f1f5f9;
  }}
  .detail-table td:first-child {{
    font-weight: 600;
    color: #64748b;
    width: 120px;
    text-transform: uppercase;
    font-size: 11px;
  }}
  td.rank {{ color: #94a3b8; font-weight: 700; text-align: center; width: 36px; }}
  td.domain {{ font-weight: 600; color: #1a1a1a; }}
  td.anchor {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #64748b; }}
  .anchor-type {{ font-size: 10px; color: #94a3b8; font-style: italic; }}
  .src-domain {{ font-size: 11px; color: #94a3b8; margin-top: 2px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .url-cell {{ max-width: 250px; overflow: hidden; }}
  .url-cell a {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  /* Badges */
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
  }}
  .badge-df {{ background: #dcfce7; color: #16a34a; }}
  .badge-nf {{ background: #fef3c7; color: #d97706; }}

  /* Timeline */
  .timeline-wrap {{
    display: flex;
    align-items: flex-end;
    gap: 4px;
    overflow-x: auto;
    padding: 20px 0;
    min-height: 160px;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .tl-month {{
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 60px;
    flex-shrink: 0;
  }}
  .tl-bars {{
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 100px;
  }}
  .tl-bar {{
    width: 12px;
    min-height: 2px;
    border-radius: 2px 2px 0 0;
  }}
  .tl-new {{ background: #16a34a; }}
  .tl-lost {{ background: #ef4444; }}
  .tl-label {{ font-size: 11px; color: #64748b; margin-top: 6px; }}
  .tl-count {{ font-size: 10px; color: #94a3b8; }}
  .timeline-legend {{
    display: flex;
    gap: 20px;
    margin-bottom: 12px;
    font-size: 13px;
    color: #64748b;
  }}
  .legend-dot {{
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 2px;
    margin-right: 6px;
    vertical-align: middle;
  }}

  /* Recommendations */
  .recs {{
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 40px;
  }}
  .rec-card {{
    display: flex;
    gap: 16px;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 16px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .rec-rank {{
    font-size: 24px;
    font-weight: 700;
    color: #0066cc;
    min-width: 40px;
    text-align: center;
    padding-top: 2px;
  }}
  .rec-body {{ font-size: 14px; color: #334155; line-height: 1.6; }}

  .footer {{
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid #e2e8f0;
    color: #94a3b8;
    font-size: 12px;
    text-align: center;
  }}

  @media (max-width: 768px) {{
    body {{ padding: 12px; }}
    .dist-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<h1>Page Backlink Audit</h1>
<p class="subtitle">
  Target page: <a href="{html_escape(url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(url)}</a><br>
  Domain: <strong>{html_escape(target_domain)}</strong>
  &nbsp;·&nbsp; Target keyword: <strong>{html_escape(keyword or "N/A")}</strong>
  &nbsp;·&nbsp; Brand: <strong>{html_escape(brand or "N/A")}</strong>
  &nbsp;·&nbsp; <span class="generated">Generated {html_escape(gen_date)}</span>
</p>

<!-- Summary Cards -->
<h2>Summary</h2>
<div class="cards">
  <div class="card">
    <div class="card-label">Total Backlinks</div>
    <div class="card-value">{total:,}</div>
    <div class="card-sub">Links pointing to this page</div>
  </div>
  <div class="card">
    <div class="card-label">Referring Domains</div>
    <div class="card-value">{total_ref_domains:,}</div>
    <div class="card-sub">Unique linking domains</div>
  </div>
  <div class="card">
    <div class="card-label">DoFollow Links</div>
    <div class="card-value" style="color:#16a34a">{dofollow_count:,}</div>
    <div class="card-sub">{df_pct:.0f}% of total · {nofollow_count:,} NoFollow</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Domain Strength</div>
    <div class="card-value" style="color:#0066cc">{avg_ds}</div>
    <div class="card-sub">DataForSEO domain_from_rank (0-1000)</div>
  </div>
</div>

<!-- Quality Distribution -->
<h2>Backlink Quality Distribution</h2>
<div class="dist-grid">
  <div class="dist-card">
    <div class="dist-title">Tier Distribution (by Domain Strength + Majestic Rank)</div>
    {''.join(tier_bars)}
  </div>
  <div class="dist-card">
    <div class="dist-title">Anchor Text Analysis</div>
    {''.join(anchor_bars)}
  </div>
</div>

<!-- Top Referring Domains -->
<h2>Top Referring Domains (click to expand)</h2>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Domain</th>
        <th>Total Links</th>
        <th>DoFollow</th>
        <th>NoFollow</th>
        <th>Domain Strength</th>
      </tr>
    </thead>
    <tbody>
      {''.join(top_domain_rows) if top_domain_rows else '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:30px">No referring domains found.</td></tr>'}
    </tbody>
  </table>
</div>

<!-- New vs Lost Timeline -->
<h2>New vs Lost Backlinks Timeline</h2>
<div class="timeline-legend">
  <span><span class="legend-dot" style="background:#16a34a"></span>New (first_seen)</span>
  <span><span class="legend-dot" style="background:#ef4444"></span>Lost (last_seen, nofollow)</span>
</div>
<div class="timeline-wrap">
  {timeline_bars}
</div>

<!-- Full Backlink Table -->
<h2>Full Backlink Table (click rows to expand) — {total} links</h2>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Source URL</th>
        <th>Anchor</th>
        <th>Domain Strength</th>
        <th>DF/NF</th>
        <th>Type</th>
        <th>Majestic</th>
        <th>First Seen</th>
      </tr>
    </thead>
    <tbody>
      {''.join(full_rows) if full_rows else '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:30px">No backlinks found for this page.</td></tr>'}
    </tbody>
  </table>
</div>

<!-- Recommendations -->
<h2>Recommendations for Link Building</h2>
<div class="recs">
  {''.join(rec_cards)}
</div>

<div class="footer">
  Page Backlink Audit · Zenfusion SEO · DataForSEO API · Majestic Million<br>
  Endpoints: backlinks/live · summary/live<br>
  Generated {html_escape(gen_date)} · <code style="color:#cbd5e1">{html_escape(url)}</code>
</div>

<script>
  // Expandable rows
  document.querySelectorAll('tr.expandable').forEach(function(row) {{
    row.addEventListener('click', function() {{
      var detail = row.nextElementSibling;
      if (detail && detail.classList.contains('expand-detail')) {{
        detail.style.display = detail.style.display === 'none' ? 'table-row' : 'none';
      }}
    }});
  }});
</script>

</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Page Backlink Audit — analyze all backlinks pointing to a specific page URL"
    )
    parser.add_argument(
        "--url",
        required=True,
        help="The page URL to audit (e.g., https://www.example.com/page)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output directory for reports (default: ~/.hermes/audits/page-backlinks)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum number of backlinks to fetch (default: 1000)",
    )
    args = parser.parse_args()

    url = args.url.strip()
    output_dir = args.output or DEFAULT_OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    target_domain = normalize_domain(url)

    print(f"\n{'='*70}")
    print(f"  PAGE BACKLINK AUDIT")
    print(f"  URL:   {url}")
    print(f"  Domain: {target_domain}")
    print(f"{'='*70}")

    # Load Majestic Million
    print("\n  Loading Majestic Million index...", end="", flush=True)
    t0 = time.time()
    majestic_db = load_majestic()
    print(f" {len(majestic_db):,} domains ({time.time()-t0:.1f}s)")

    # 1. Fetch page-level backlinks
    print(f"\n  Fetching backlinks for page: {url}")
    print(f"  (limit: {args.limit})")
    t0 = time.time()
    backlinks = fetch_page_backlinks(url, limit=args.limit)
    print(f"  Found {len(backlinks)} backlinks ({time.time()-t0:.1f}s)")

    # Also fetch summary
    print("\n  Fetching backlink summary...", end="", flush=True)
    t0 = time.time()
    summary = fetch_page_backlinks_summary(url)
    print(f" done ({time.time()-t0:.1f}s)")
    if summary:
        print(f"    Summary: {summary.get('backlinks', 0):,} backlinks, "
              f"{summary.get('referring_domains', 0):,} referring domains, "
              f"{summary.get('backlinks_dofollow', 0):,} dofollow, "
              f"{summary.get('lost_backlinks', 0):,} lost, "
              f"{summary.get('new_backlinks', 0):,} new")

    if not backlinks:
        print("\n  ⚠ No backlinks found for this page. Generating report with empty data.")

    # 2. Analyze quality
    print(f"\n  Analyzing backlink quality for {len(backlinks)} links...")
    backlinks = analyze_backlink_quality(backlinks, target_url=url)

    # Quick stats
    tier_counts = Counter(bl.get("tier", "Tier 3") for bl in backlinks)
    anchor_counts = Counter(bl.get("anchor_type", "generic") for bl in backlinks)
    dofollow = sum(1 for bl in backlinks if bl.get("is_dofollow"))
    in_maj = sum(1 for bl in backlinks if bl.get("in_majestic"))
    ref_domains = len(set(bl.get("domain_from", "") for bl in backlinks if bl.get("domain_from")))
    ds_values = [bl.get("domain_strength", 0) or 0 for bl in backlinks if bl.get("domain_strength")]
    avg_ds = round(sum(ds_values) / len(ds_values), 1) if ds_values else 0

    print(f"    Tiers: T1={tier_counts.get('Tier 1',0)} T2={tier_counts.get('Tier 2',0)} T3={tier_counts.get('Tier 3',0)}")
    print(f"    Anchors: exact={anchor_counts.get('exact_match',0)} partial={anchor_counts.get('partial_match',0)} "
          f"branded={anchor_counts.get('branded',0)} generic={anchor_counts.get('generic',0)}")
    print(f"    DoFollow: {dofollow} | In Majestic: {in_maj} | Ref domains: {ref_domains} | Avg DS: {avg_ds}")

    # 3. Generate report
    print(f"\n  Generating HTML report → {output_dir}")
    html_path = generate_page_backlink_report(url, backlinks, output_dir)
    print(f"  HTML report saved: {html_path}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  AUDIT COMPLETE")
    print(f"  Page:             {url}")
    print(f"  Total backlinks:  {len(backlinks)}")
    print(f"  Referring domains: {ref_domains}")
    print(f"  DoFollow:         {dofollow}")
    print(f"  Avg domain strength: {avg_ds}")
    print(f"  In Majestic Million: {in_maj}")
    print(f"  Tier 1: {tier_counts.get('Tier 1', 0)} | Tier 2: {tier_counts.get('Tier 2', 0)} | Tier 3: {tier_counts.get('Tier 3', 0)}")
    print(f"  Report: {html_path}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()