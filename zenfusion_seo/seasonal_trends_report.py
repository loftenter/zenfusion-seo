#!/usr/bin/env python3
"""
Seasonal Trends Report — standalone CLI
========================================
Shows monthly search volume trends for market keywords, identifying seasonal
patterns, peak/low months, and providing content timing + ad spend recommendations.

Pipeline:
  1. fetch_keyword_trends(keywords, location_code)  — DataForSEO historical_keyword_data
  2. compute_seasonality(trends)                     — peak/low months, seasonal index
  3. generate_trends_report(seed, trends, output)   — HTML report (white SaaS theme)

Usage:
  python3 seasonal_trends_report.py --seed 'water softener' \
    --keywords 'water softener,best water softener,...' \
    --output /tmp/test_seasonal_trends

Environment:
  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD  in ~/.hermes/.env
"""

import os
import sys
import json
import base64
import argparse
import datetime
from collections import defaultdict

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------------------
# Config / credentials
# ---------------------------------------------------------------------------

ENV_PATH = os.path.expanduser("~/.hermes/.env")
MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]

# ---------------------------------------------------------------------------
# .env + API plumbing
# ---------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

def get_creds():
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        load_env()
        login = os.environ.get("DATAFORSEO_LOGIN")
        password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        sys.stderr.write("ERROR: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not found.\n")
        sys.exit(2)
    return login, password

def _auth_header():
    login, password = get_creds()
    raw = f"{login}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()

def dfs_post(path, task, timeout=120, retries=2):
    """POST one task to a DataForSEO endpoint; return raw API response dict."""
    if requests is None:
        sys.stderr.write("requests library not installed\n")
        return None
    url = "https://api.dataforseo.com/v3/" + path.lstrip("/")
    headers = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    body = json.dumps([task]).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=timeout)
            data = r.json()
            if r.status_code == 401 or data.get("status_code") == 40101:
                sys.stderr.write("DataForSEO 401 — bad credentials.\n")
                return None
            if data.get("status_code") not in (20000, 20001):
                sys.stderr.write(
                    f"DataForSEO {path} status {data.get('status_code')}: "
                    f"{data.get('status_message', '')}\n"
                )
                if attempt < retries:
                    import time as _time
                    _time.sleep(1.5)
                    continue
                return None
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                import time as _time
                _time.sleep(1.5)
                continue
    sys.stderr.write(f"DataForSEO {path} request failed: {last_err}\n")
    return None

# ---------------------------------------------------------------------------
# 1. fetch_keyword_trends
# ---------------------------------------------------------------------------

def fetch_keyword_trends(keywords, location_code=2840):
    """
    Fetch monthly search volume data for each keyword using DataForSEO
    historical_keyword_data endpoint (data since 2019).

    Returns dict keyed by keyword:
        {
            keyword: {
                "monthly_volumes": [ {"year":2024, "month":1, "search_volume":1200}, ... ],
                "avg_monthly": [ jan_avg, feb_avg, ..., dec_avg ],  # 12 values, averaged across years
                "annual_volume": int,   # sum of latest 12 months
                "cpc": float,
                "competition": float,
            }
        }
    """
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    keywords = [k.lower() for k in keywords]
    print(f"  Fetching trends for {len(keywords)} keywords via DataForSEO "
          f"historical_keyword_data ...")

    # DataForSEO allows up to 700 keywords per request — we can batch all at once
    task = {
        "keywords": keywords,
        "location_code": location_code,
        "language_code": "en",
    }
    data = dfs_post("dataforseo_labs/google/historical_keyword_data/live", task, timeout=120)
    if not data:
        sys.stderr.write("  ! historical_keyword_data failed; falling back to keyword_overview\n")
        return _fetch_via_keyword_overview(keywords, location_code)

    tasks = data.get("tasks") or []
    if not tasks:
        return {}
    result = tasks[0].get("result") or []
    if not result:
        return {}
    items = result[0].get("items") or []

    trends = {}
    for item in items:
        kw = (item.get("keyword") or "").strip().lower()
        if not kw:
            continue
        history = item.get("history") or []
        monthly_volumes = []
        cpc_val = 0.0
        comp_val = None
        for entry in history:
            yr = entry.get("year")
            mo = entry.get("month")
            ki = entry.get("keyword_info") or {}
            sv = ki.get("search_volume", 0) or 0
            monthly_volumes.append({"year": yr, "month": mo, "search_volume": sv})
            cpc_val = ki.get("cpc", cpc_val) or cpc_val
            comp_val = ki.get("competition", comp_val)

        # sort by year, month
        monthly_volumes.sort(key=lambda x: (x["year"], x["month"]))

        # compute avg by calendar month (1-12) across all years
        month_sums = defaultdict(list)
        for mv in monthly_volumes:
            month_sums[mv["month"]].append(mv["search_volume"])
        avg_monthly = []
        for m in range(1, 13):
            vals = month_sums.get(m, [])
            avg_monthly.append(int(sum(vals) / len(vals)) if vals else 0)

        # annual volume = sum of latest 12 months available
        recent_12 = monthly_volumes[-12:] if len(monthly_volumes) >= 12 else monthly_volumes
        annual_volume = sum(mv["search_volume"] for mv in recent_12)

        trends[kw] = {
            "monthly_volumes": monthly_volumes,
            "avg_monthly": avg_monthly,
            "annual_volume": annual_volume,
            "cpc": round(cpc_val, 2) if cpc_val else 0.0,
            "competition": comp_val,
        }
        print(f"  v {kw}: annual={annual_volume:,} | months={len(monthly_volumes)}")

    # Fill in any keywords that got no data
    for kw in keywords:
        if kw not in trends:
            print(f"  x No data for '{kw}', using zeros")
            trends[kw] = {
                "monthly_volumes": [],
                "avg_monthly": [0] * 12,
                "annual_volume": 0,
                "cpc": 0.0,
                "competition": None,
            }

    return trends

def _fetch_via_keyword_overview(keywords, location_code):
    """Fallback: keyword_overview returns monthly_searches (last 12 months)."""
    task = {
        "keywords": keywords,
        "location_code": location_code,
        "language_code": "en",
    }
    data = dfs_post("dataforseo_labs/google/keyword_overview/live", task, timeout=120)
    if not data:
        return {kw: _empty_trend(kw) for kw in keywords}
    tasks = data.get("tasks") or []
    if not tasks:
        return {kw: _empty_trend(kw) for kw in keywords}
    result = tasks[0].get("result") or []
    if not result:
        return {kw: _empty_trend(kw) for kw in keywords}
    items = result[0].get("items") or []

    trends = {}
    for item in items:
        kw = (item.get("keyword") or "").strip().lower()
        ki = item.get("keyword_info") or {}
        ms = ki.get("monthly_searches") or []
        monthly_volumes = []
        for m in ms:
            monthly_volumes.append({
                "year": m.get("year", 0),
                "month": m.get("month", 0),
                "search_volume": m.get("search_volume", 0) or 0,
            })
        monthly_volumes.sort(key=lambda x: (x["year"], x["month"]))

        month_sums = defaultdict(list)
        for mv in monthly_volumes:
            if mv["month"]:
                month_sums[mv["month"]].append(mv["search_volume"])
        avg_monthly = []
        for m in range(1, 13):
            vals = month_sums.get(m, [])
            avg_monthly.append(int(sum(vals) / len(vals)) if vals else 0)

        annual_volume = sum(mv["search_volume"] for mv in monthly_volumes[-12:])

        trends[kw] = {
            "monthly_volumes": monthly_volumes,
            "avg_monthly": avg_monthly,
            "annual_volume": annual_volume,
            "cpc": round(ki.get("cpc", 0) or 0, 2),
            "competition": ki.get("competition"),
        }
        print(f"  v {kw}: annual={annual_volume:,} | months={len(monthly_volumes)}")

    for kw in keywords:
        if kw not in trends:
            trends[kw] = _empty_trend(kw)
    return trends

def _empty_trend(kw):
    return {
        "monthly_volumes": [],
        "avg_monthly": [0] * 12,
        "annual_volume": 0,
        "cpc": 0.0,
        "competition": None,
    }

# ---------------------------------------------------------------------------
# 2. compute_seasonality
# ---------------------------------------------------------------------------

def compute_seasonality(trends):
    """
    For each keyword, compute:
      - peak_month: calendar month (1-12) with highest avg volume
      - low_month:  calendar month (1-12) with lowest avg volume
      - seasonality_score: coefficient of variation (std/mean * 100)
      - seasonality_index: per-month ratio (month_avg / overall_avg)
      - peak_intensity: peak / low ratio
    Enriches the trends dict in place and also returns it.
    """
    for kw, data in trends.items():
        avg = data["avg_monthly"]
        overall_avg = sum(avg) / 12.0 if sum(avg) > 0 else 0

        if overall_avg > 0:
            # peak/low month (1-indexed)
            peak_m = max(range(12), key=lambda i: avg[i]) + 1
            low_m = min(range(12), key=lambda i: avg[i]) + 1
            # coefficient of variation as seasonality score
            mean = overall_avg
            variance = sum((v - mean) ** 2 for v in avg) / 12.0
            std = variance ** 0.5
            seasonality_score = round((std / mean) * 100, 1) if mean > 0 else 0
            peak_intensity = round(avg[peak_m - 1] / avg[low_m - 1], 2) if avg[low_m - 1] > 0 else 0
            seasonality_index = [round(v / mean, 2) for v in avg]
        else:
            peak_m = 0
            low_m = 0
            seasonality_score = 0
            peak_intensity = 0
            seasonality_index = [0] * 12

        data["peak_month"] = peak_m
        data["low_month"] = low_m
        data["seasonality_score"] = seasonality_score
        data["peak_intensity"] = peak_intensity
        data["seasonality_index"] = seasonality_index

    # Compute market-level (aggregate) totals
    market_monthly = [0] * 12
    for kw, data in trends.items():
        for i, v in enumerate(data["avg_monthly"]):
            market_monthly[i] += v
    market_total_annual = sum(market_monthly)
    if market_total_annual > 0:
        market_peak_m = max(range(12), key=lambda i: market_monthly[i]) + 1
        market_low_m = min(range(12), key=lambda i: market_monthly[i]) + 1
        m_mean = market_total_annual / 12.0
        m_var = sum((v - m_mean) ** 2 for v in market_monthly) / 12.0
        m_std = m_var ** 0.5
        market_seasonality = round((m_std / m_mean) * 100, 1) if m_mean > 0 else 0
    else:
        market_peak_m = 0
        market_low_m = 0
        market_seasonality = 0

    trends["_market"] = {
        "monthly_totals": market_monthly,
        "total_annual_volume": market_total_annual,
        "peak_month": market_peak_m,
        "low_month": market_low_m,
        "seasonality_score": market_seasonality,
    }
    return trends

# ---------------------------------------------------------------------------
# 3. generate_trends_report — SVG + HTML
# ---------------------------------------------------------------------------

def _svg_heatmap(trends, keywords):
    """Seasonality heatmap: keywords × months, color = relative volume intensity."""
    rows = len(keywords)
    cols = 12
    cell_w = 56
    cell_h = 34
    label_w = 180
    header_h = 30
    width = label_w + cols * cell_w + 20
    height = header_h + rows * cell_h + 20

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="sans-serif" font-size="11">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff" rx="4"/>',
    ]

    # header months
    for i, mname in enumerate(MONTH_NAMES):
        x = label_w + i * cell_w + cell_w // 2
        parts.append(
            f'<text x="{x}" y="{header_h - 8}" text-anchor="middle" '
            f'fill="#6B7280" font-weight="600">{mname}</text>'
        )

    # rows
    for ri, kw in enumerate(keywords):
        data = trends.get(kw, {})
        avg = data.get("avg_monthly", [0] * 12)
        max_v = max(avg) if max(avg) > 0 else 1
        y = header_h + ri * cell_h

        # keyword label
        display_kw = kw if len(kw) <= 24 else kw[:22] + "…"
        parts.append(
            f'<text x="{label_w - 8}" y="{y + cell_h // 2 + 4}" text-anchor="end" '
            f'fill="#1F2937" font-size="11">{display_kw}</text>'
        )

        for ci in range(cols):
            v = avg[ci]
            intensity = v / max_v if max_v > 0 else 0
            # blue gradient: low = white, high = #2563EB
            r_val = int(255 - intensity * (255 - 37))
            g_val = int(255 - intensity * (255 - 99))
            b_val = int(255 - intensity * (255 - 235))
            fill = f"rgb({r_val},{g_val},{b_val})"
            x = label_w + ci * cell_w
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_w - 2}" height="{cell_h - 2}" '
                f'fill="{fill}" rx="2" stroke="#E5E7EB" stroke-width="0.5"/>'
            )
            # value text (only if non-zero)
            if v > 0:
                txt_color = "#fff" if intensity > 0.5 else "#1F2937"
                parts.append(
                    f'<text x="{x + cell_w // 2}" y="{y + cell_h // 2 + 4}" '
                    f'text-anchor="middle" fill="{txt_color}" font-size="10">{v:,}</text>'
                )

    parts.append('</svg>')
    return "\n".join(parts)

def _svg_line_chart(market_monthly):
    """Line chart of total market volume by month."""
    width = 720
    height = 300
    pad_l = 60
    pad_r = 20
    pad_t = 20
    pad_b = 40
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    max_val = max(market_monthly) if max(market_monthly) > 0 else 1
    min_val = 0

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="sans-serif" font-size="11">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff" rx="4"/>',
    ]

    # grid lines + Y labels
    for i in range(5):
        y = pad_t + (chart_h * i / 4)
        val = int(max_val - (max_val * i / 4))
        parts.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{width - pad_r}" y2="{y}" '
            f'stroke="#E5E7EB" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4}" text-anchor="end" '
            f'fill="#6B7280" font-size="10">{val:,}</text>'
        )

    # X labels + data points
    pts = []
    for i, val in enumerate(market_monthly):
        x = pad_l + (chart_w * i / 11) if len(market_monthly) > 1 else pad_l
        y = pad_t + chart_h - (val / max_val * chart_h) if max_val > 0 else pad_t + chart_h
        pts.append((x, y, val))
        # X label
        parts.append(
            f'<text x="{x}" y="{height - pad_b + 16}" text-anchor="middle" '
            f'fill="#6B7280" font-size="10">{MONTH_NAMES[i]}</text>'
        )

    # area fill under line
    if pts:
        path_d = f"M {pts[0][0]},{pad_t + chart_h}"
        for x, y, v in pts:
            path_d += f" L {x},{y}"
        path_d += f" L {pts[-1][0]},{pad_t + chart_h} Z"
        parts.append(f'<path d="{path_d}" fill="#2563EB" opacity="0.08"/>')

    # line
    if len(pts) >= 2:
        line_d = "M " + " L ".join(f"{x},{y}" for x, y, v in pts)
        parts.append(
            f'<path d="{line_d}" fill="none" stroke="#2563EB" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # dots
    for x, y, v in pts:
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#2563EB" stroke="#fff" stroke-width="1.5"/>')
        parts.append(
            f'<text x="{x}" y="{y - 10}" text-anchor="middle" '
            f'fill="#1F2937" font-size="9" font-weight="600">{v:,}</text>'
        )

    parts.append('</svg>')
    return "\n".join(parts)

def _content_recommendations(trends):
    """Generate content timing recommendations based on seasonality."""
    market = trends.get("_market", {})
    monthly_totals = market.get("monthly_totals", [0] * 12)
    peak_month = market.get("peak_month", 0)
    low_month = market.get("low_month", 0)

    if sum(monthly_totals) == 0:
        return [
            "<div class='rec-item'><b>Publish year-round</b> — no significant seasonality detected. "
            "Maintain consistent content cadence.</div>"
        ]

    # sort months by volume
    sorted_months = sorted(range(12), key=lambda i: monthly_totals[i], reverse=True)
    top_3 = sorted_months[:3]
    bottom_3 = sorted_months[-3:]

    recs = []

    # Publishing: publish 1-2 months BEFORE peak to let content age and rank
    publish_months = [(peak_month - 2) % 12 or 12, (peak_month - 1) % 12 or 12]
    publish_names = ", ".join(MONTH_NAMES[m - 1] for m in publish_months)
    recs.append(
        f"<div class='rec-item'><b>Publish new content in {publish_names}</b> — "
        f"lead the peak month ({MONTH_NAMES[peak_month - 1]}) by 1-2 months so pages "
        f"have time to index and rank before demand spikes.</div>"
    )

    # Promote during peak
    promote_names = ", ".join(MONTH_NAMES[m] for m in top_3)
    recs.append(
        f"<div class='rec-item'><b>Promote / refresh during {promote_names}</b> — "
        f"these are the top 3 months by total market search volume. Update existing "
        f"pages, push social, and run email campaigns.</div>"
    )

    # Off-season content gaps
    off_names = ", ".join(MONTH_NAMES[m] for m in bottom_3)
    recs.append(
        f"<div class='rec-item'><b>Build topical authority during {off_names}</b> — "
        f"low-season months are ideal for publishing long-tail and educational content "
        f"that can rank by peak season.</div>"
    )

    return recs

def _ad_spend_recommendations(trends):
    """Generate ad spend recommendations based on seasonality + CPC."""
    market = trends.get("_market", {})
    monthly_totals = market.get("monthly_totals", [0] * 12)
    max_vol = max(monthly_totals) if max(monthly_totals) > 0 else 1

    if sum(monthly_totals) == 0:
        return ["<div class='rec-item'>Maintain steady ad spend — no seasonal signal detected.</div>"]

    sorted_months = sorted(range(12), key=lambda i: monthly_totals[i], reverse=True)
    top_3 = sorted_months[:3]
    bottom_3 = sorted_months[-3:]

    recs = []

    increase_names = ", ".join(MONTH_NAMES[m] for m in top_3)
    recs.append(
        f"<div class='rec-item'><b>Increase bids by 15-30% in {increase_names}</b> — "
        f"search demand peaks in these months. Higher CPC competition is offset by "
        f"greater conversion volume. Prioritize top-of-page bids.</div>"
    )

    decrease_names = ", ".join(MONTH_NAMES[m] for m in bottom_3)
    recs.append(
        f"<div class='rec-item'><b>Reduce bids by 10-20% in {decrease_names}</b> — "
        f"demand troughs. Lower CPCs to maintain ROI, shift budget to evergreen "
        f"campaigns or brand terms.</div>"
    )

    # Budget shift guidance
    total = sum(monthly_totals)
    if total > 0:
        peak_share = monthly_totals[top_3[0]] / total * 100
        recs.append(
            f"<div class='rec-item'><b>Allocate ~{peak_share:.0f}% of monthly budget to "
            f"{MONTH_NAMES[top_3[0]]}</b> — this month alone accounts for a disproportionate "
            f"share of annual search demand.</div>"
        )

    return recs

def generate_trends_report(seed_keyword, trends, output_path):
    """
    Generate an HTML seasonal trends report.

    Args:
        seed_keyword: the seed keyword that the market was built around
        trends: output of compute_seasonality(fetch_keyword_trends(...))
        output_path: directory to write the report into (created if needed)
    Returns:
        path to the written HTML file
    """
    os.makedirs(output_path, exist_ok=True)

    # Separate market-level from per-keyword
    market = trends.get("_market", {
        "monthly_totals": [0] * 12,
        "total_annual_volume": 0,
        "peak_month": 0,
        "low_month": 0,
        "seasonality_score": 0,
    })
    keywords = [k for k in trends.keys() if k != "_market"]
    # sort by annual_volume desc
    keywords.sort(key=lambda k: trends[k].get("annual_volume", 0), reverse=True)

    date_gen = datetime.date.today().strftime("%Y-%m-%d")
    html_path = os.path.join(output_path, "seasonal_trends_report.html")

    # --- Build body sections ---
    body = []

    # Header
    body.append(f"""
    <header>
      <h1>Seasonal Trends Report — {seed_keyword}</h1>
      <div class="meta">Generated {date_gen} • US Market (location 2840) • DataForSEO Historical Keyword Data</div>
    </header>
    """)

    # --- Market Overview KPIs ---
    total_keywords = len(keywords)
    total_annual = market["total_annual_volume"]
    peak_month = market["peak_month"]
    low_month = market["low_month"]
    peak_name = MONTH_NAMES[peak_month - 1] if peak_month else "N/A"
    low_name = MONTH_NAMES[low_month - 1] if low_month else "N/A"
    market_season = market.get("seasonality_score", 0)

    body.append("<h2>Market Overview</h2>")
    body.append('<div class="kpis">')
    body.append(f'<div class="kpi"><div class="label">Keywords Analyzed</div>'
                f'<div class="value">{total_keywords}</div></div>')
    body.append(f'<div class="kpi"><div class="label">Total Annual Volume</div>'
                f'<div class="value">{total_annual:,}</div></div>')
    body.append(f'<div class="kpi"><div class="label">Peak Month</div>'
                f'<div class="value">{peak_name}</div></div>')
    body.append(f'<div class="kpi"><div class="label">Low Month</div>'
                f'<div class="value">{low_name}</div></div>')
    body.append(f'<div class="kpi"><div class="label">Market Seasonality</div>'
                f'<div class="value">{market_season}%</div>'
                f'<div class="sub">CoV — higher = more seasonal</div></div>')
    body.append('</div>')

    # --- Seasonality Heatmap ---
    body.append("<h2>Seasonality Heatmap</h2>")
    body.append("<p class='desc'>Relative search volume intensity by keyword × month. "
                "Darker blue = higher volume. Values are monthly averages across all "
                "available years of historical data.</p>")
    body.append(f'<div class="chart-container">{_svg_heatmap(trends, keywords)}</div>')

    # --- Monthly Volume Trend Chart ---
    body.append("<h2>Monthly Volume Trend (Total Market)</h2>")
    body.append("<p class='desc'>Aggregate search volume across all analyzed keywords "
                "by calendar month, showing the market's seasonal demand curve.</p>")
    body.append(f'<div class="chart-container">{_svg_line_chart(market["monthly_totals"])}</div>')

    # --- Top Seasonal Keywords Table ---
    body.append("<h2>Top Seasonal Keywords</h2>")
    body.append("<p class='desc'>Keywords ranked by annual volume, with peak/low months "
                "and seasonality score (coefficient of variation).</p>")
    body.append("<table><thead><tr>"
                "<th>Keyword</th><th>Peak Month</th><th>Low Month</th>"
                "<th>Seasonality Score</th><th>Peak/Low Ratio</th>"
                "<th>Annual Volume</th><th>CPC</th><th>Competition</th>"
                "</tr></thead><tbody>")
    for kw in keywords:
        d = trends[kw]
        pk = MONTH_NAMES[d["peak_month"] - 1] if d["peak_month"] else "N/A"
        lo = MONTH_NAMES[d["low_month"] - 1] if d["low_month"] else "N/A"
        comp = d.get("competition")
        comp_str = f"{comp:.2f}" if comp is not None else "N/A"
        body.append(
            f"<tr><td>{kw}</td><td>{pk}</td><td>{lo}</td>"
            f'<td>{d["seasonality_score"]}%</td>'
            f'<td>{d["peak_intensity"]}x</td>'
            f'<td>{d["annual_volume"]:,}</td>'
            f'<td>${d["cpc"]:.2f}</td>'
            f'<td>{comp_str}</td></tr>'
        )
    body.append("</tbody></table>")

    # --- Content Timing Recommendations ---
    body.append("<h2>Content Timing Recommendations</h2>")
    body.append('<div class="recs">')
    for r in _content_recommendations(trends):
        body.append(r)
    body.append('</div>')

    # --- Ad Spend Recommendations ---
    body.append("<h2>Ad Spend Recommendations</h2>")
    body.append('<div class="recs">')
    for r in _ad_spend_recommendations(trends):
        body.append(r)
    body.append('</div>')

    # --- Per-Keyword Expandable Breakdown ---
    body.append("<h2>Per-Keyword Monthly Volume Breakdown</h2>")
    body.append("<p class='desc'>Click a keyword to expand its month-by-month volume detail.</p>")
    for kw in keywords:
        d = trends[kw]
        avg = d["avg_monthly"]
        idx = d.get("seasonality_index", [0] * 12)
        pk = MONTH_NAMES[d["peak_month"] - 1] if d["peak_month"] else "N/A"

        body.append('<details class="kw-detail">')
        body.append(
            f'<summary><b>{kw}</b> — Annual: {d["annual_volume"]:,} | '
            f'Peak: {pk} | Seasonality: {d["seasonality_score"]}%</summary>'
        )
        body.append('<div class="kw-detail-body">')
        body.append('<table class="monthly-tbl"><thead><tr>')
        for mname in MONTH_NAMES:
            body.append(f"<th>{mname}</th>")
        body.append("</tr></thead><tbody><tr>")
        for v in avg:
            body.append(f"<td>{v:,}</td>")
        body.append("</tr></tbody></table>")

        # seasonality index row
        body.append('<div class="idx-row"><b>Seasonality Index:</b> ')
        idx_parts = []
        for i, iv in enumerate(idx):
            cls = "idx-high" if iv > 1.1 else ("idx-low" if iv < 0.9 else "idx-mid")
            idx_parts.append(
                f'<span class="idx-pill {cls}">{MONTH_NAMES[i]} {iv:.2f}</span>'
            )
        body.append(" ".join(idx_parts))
        body.append('</div>')

        # raw historical monthly volumes (if available)
        mvs = d.get("monthly_volumes", [])
        if mvs:
            body.append('<div class="raw-vols"><b>Historical Monthly Data:</b><table><thead><tr>'
                        '<th>Year</th><th>Month</th><th>Volume</th></tr></thead><tbody>')
            for mv in mvs[-12:]:
                body.append(
                    f'<tr><td>{mv["year"]}</td><td>{MONTH_NAMES[mv["month"]-1] if mv["month"] else "?"}</td>'
                    f'<td>{mv["search_volume"]:,}</td></tr>'
                )
            body.append('</tbody></table></div>')

        body.append('</div></details>')

    # --- Footer ---
    body.append(f"""
    <footer>
      Generated {date_gen} • DataForSEO Historical Keyword Data •
      Seasonal Trends Report for "{seed_keyword}" •
      <code>seasonal_trends_report.py</code>
    </footer>
    """)

    # --- Assemble full HTML ---
    html = HTML_TMPL.format(
        seed=seed_keyword,
        date_generated=date_gen,
        body="\n".join(body),
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Also save raw JSON data
    json_path = os.path.join(output_path, "seasonal_trends_data.json")
    export = {}
    for k, v in trends.items():
        if k == "_market":
            export["_market"] = v
        else:
            export[k] = {
                "monthly_volumes": v.get("monthly_volumes", []),
                "avg_monthly": v.get("avg_monthly", [0] * 12),
                "annual_volume": v.get("annual_volume", 0),
                "cpc": v.get("cpc", 0),
                "competition": v.get("competition"),
                "peak_month": v.get("peak_month", 0),
                "low_month": v.get("low_month", 0),
                "seasonality_score": v.get("seasonality_score", 0),
                "peak_intensity": v.get("peak_intensity", 0),
                "seasonality_index": v.get("seasonality_index", [0] * 12),
            }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2)

    print(f"\n  ✓ Report written: {html_path}")
    print(f"  ✓ JSON data: {json_path}")
    return html_path

# ---------------------------------------------------------------------------
# HTML template (white SaaS theme)
# ---------------------------------------------------------------------------

HTML_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Seasonal Trends — {seed}</title>
<style>
  :root {{ --primary:#1F2937; --accent:#2563EB; --success:#16A34A; --warn:#D97706;
          --bg:#F3F4F6; --muted:#6B7280; --grid:#E5E7EB; --soft:#EFF6FF; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         margin:0; color:var(--primary); background:#fff; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px; }}
  header {{ border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:28px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  h2 {{ color:var(--accent); border-bottom:1px solid var(--grid); padding-bottom:6px; margin-top:36px; }}
  .desc {{ font-size:13px; color:var(--muted); margin-bottom:12px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--bg); border-top:3px solid var(--accent); border-radius:6px;
          padding:14px 18px; min-width:160px; flex:1; }}
  .kpi .label {{ font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
  .kpi .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .kpi .sub {{ font-size:11px; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }}
  th {{ background:var(--primary); color:#fff; text-align:left; padding:8px 10px; font-weight:600; }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }}
  tr:nth-child(even) td {{ background:var(--bg); }}
  .chart-container {{ background:var(--bg); border-radius:6px; padding:16px; margin:12px 0; overflow-x:auto; }}
  .recs {{ margin:12px 0; }}
  .rec-item {{ background:var(--soft); border-left:3px solid var(--accent); padding:10px 14px;
              margin:8px 0; border-radius:0 4px 4px 0; font-size:14px; }}
  .kw-detail {{ border:1px solid var(--grid); border-radius:6px; margin:8px 0; }}
  .kw-detail summary {{ cursor:pointer; padding:10px 14px; font-size:14px;
                       background:var(--bg); border-radius:6px; }}
  .kw-detail[open] summary {{ border-bottom:1px solid var(--grid); border-radius:6px 6px 0 0; }}
  .kw-detail-body {{ padding:12px 14px; }}
  .monthly-tbl {{ font-size:12px; margin:8px 0; }}
  .monthly-tbl th {{ padding:6px 8px; }}
  .monthly-tbl td {{ padding:6px 8px; text-align:right; }}
  .idx-row {{ margin:8px 0; font-size:13px; }}
  .idx-pill {{ display:inline-block; padding:2px 8px; border-radius:10px;
              font-size:11px; margin:2px; }}
  .idx-high {{ background:#DBEAFE; color:#1E40AF; }}
  .idx-mid {{ background:var(--bg); color:var(--muted); }}
  .idx-low {{ background:#FEF3C7; color:#92400E; }}
  .raw-vols {{ margin-top:10px; }}
  .raw-vols table {{ font-size:12px; }}
  footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
           color:var(--muted); font-size:11px; text-align:center; }}
  code {{ background:var(--bg); padding:1px 4px; border-radius:3px; font-size:12px; }}
</style></head><body><div class="wrap">
{body}
<footer>Generated {date_generated} • DataForSEO Historical Keyword Data • White-label seasonal trends report</footer>
</div></body></html>"""

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Seasonal Trends Report")
    ap.add_argument("--seed", required=True, help="Seed keyword (market label)")
    ap.add_argument("--keywords", required=True,
                    help="Comma-separated list of keywords to analyze")
    ap.add_argument("--output", required=True,
                    help="Output directory for report")
    ap.add_argument("--location", type=int, default=2840,
                    help="DataForSEO location code (default: 2840 = US)")
    args = ap.parse_args()

    keyword_list = [k.strip() for k in args.keywords.split(",") if k.strip()]
    print(f"\n{'='*60}")
    print(f"Seasonal Trends Report — '{args.seed}'")
    print(f"  Keywords: {len(keyword_list)}")
    print(f"  Location: {args.location}")
    print(f"  Output:   {args.output}")
    print(f"{'='*60}\n")

    # Step 1: fetch
    print("\n[1/3] Fetching keyword trends ...")
    trends = fetch_keyword_trends(keyword_list, location_code=args.location)
    print(f"  Fetched data for {len(trends)} keywords")

    # Step 2: compute seasonality
    print("\n[2/3] Computing seasonality ...")
    trends = compute_seasonality(trends)
    market = trends.get("_market", {})
    print(f"  Market total annual volume: {market.get('total_annual_volume', 0):,}")
    print(f"  Market peak month: {MONTH_NAMES[market.get('peak_month', 0) - 1] if market.get('peak_month') else 'N/A'}")

    # Step 3: generate report
    print("\n[3/3] Generating report ...")
    report_path = generate_trends_report(args.seed, trends, args.output)

    print(f"\n{'='*60}")
    print(f"✓ Complete! Report: {report_path}")
    print(f"{'='*60}")
    return report_path

if __name__ == "__main__":
    main()
