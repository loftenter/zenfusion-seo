#!/usr/bin/env python3
"""
brand_mention_monitor.py — Track brand mentions across AI engines and the web.

Monitors how often a brand is mentioned and cited inside AI assistant answers
(ChatGPT, Google/Gemini) via the DataForSEO AI Optimization mentions dataset, and
across the wider web via the Perplexity Sonar API. Produces a white SaaS themed
HTML report with summary cards, platform breakdown, top citing sources, an
expandable web-mentions table, sentiment indicators, trend deltas (when a prior
snapshot exists) and actionable recommendations.

Functions (importable):
  fetch_ai_brand_mentions(brand_domain, platform='chat_gpt') -> dict
  fetch_web_mentions(brand_name, limit=50) -> dict
  compare_mention_trends(current, previous) -> dict
  generate_mention_report(brand_domain, brand_name, ai_mentions, web_mentions,
                          output_path, trend=None) -> str

Usage:
  python3 brand_mention_monitor.py --domain softprowatersystems.com --brand SoftPro --output /tmp/test_brand_monitor

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD, PERPLEXITY_API_KEY  (from ~/.hermes/.env or env)
"""

import os
import sys
import json
import re
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from urllib.parse import urlparse
from html import escape as html_escape

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = "https://api.dataforseo.com/v3"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

# Platforms tracked for the by-platform breakdown. 'google' maps to the Gemini
# dataset surface. The mention dataset only supports these two platforms.
AI_PLATFORMS = [
    ("chat_gpt", "ChatGPT"),
    ("google", "Google/Gemini"),
]

# Sentiment keyword vocabularies (basic lexicon, case-insensitive word match)
POSITIVE_WORDS = [
    "best", "great", "excellent", "recommend", "recommended", "top", "leading",
    "trusted", "reliable", "love", "amazing", "outstanding", "superior",
    "innovative", "popular", "favorite", "favourite", "impressive", "quality",
    "affordable", "value", "efficient", "effective", "winner", "award",
]
NEGATIVE_WORDS = [
    "bad", "worst", "scam", "poor", "terrible", "avoid", "problem", "issue",
    "complaint", "negative", "fail", "failure", "broken", "disappointing",
    "overpriced", "expensive", "lawsuit", "recall", "warning", "dangerous",
    "cheap", "inferior", "hate", "awful", "unreliable",
]


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _load_env():
    """Load credentials from environment, falling back to ~/.hermes/.env."""
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


def get_dataforseo_creds():
    """Return (login, password) for DataForSEO, exiting if missing."""
    _load_env()
    login = os.environ.get("DATAFORSEO_LOGIN", "")
    password = os.environ.get("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.",
              file=sys.stderr)
        print("Set them in ~/.hermes/.env or as environment variables.",
              file=sys.stderr)
        sys.exit(1)
    return login, password


def get_perplexity_key():
    """Return the Perplexity API key, or '' if missing."""
    _load_env()
    return os.environ.get("PERPLEXITY_API_KEY", "")


# ---------------------------------------------------------------------------
# Low-level API calls
# ---------------------------------------------------------------------------

def _dfs_api_call(path, payload=None, method="POST", timeout=60):
    """Make a DataForSEO v3 API call via urllib. Returns parsed JSON or None."""
    login, password = get_dataforseo_creds()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"{API_BASE}/{path}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    data_bytes = None
    if method == "POST":
        body = [payload] if isinstance(payload, dict) else payload
        data_bytes = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if e.code == 401:
            print(f"ERROR 401 Unauthorized for {url}. Check DataForSEO credentials.",
                  file=sys.stderr)
        elif e.code == 404:
            print(f"WARNING 404 Not Found for {url}. Endpoint may be unavailable.",
                  file=sys.stderr)
        else:
            print(f"HTTP {e.code} for {url}: {body_text}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"Request error for {url}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error for {url}: {e}", file=sys.stderr)
        return None


def _dfs_result_items(resp):
    """Extract the result array from a DataForSEO response envelope."""
    if not resp:
        return []
    tasks = resp.get("tasks", []) or []
    if not tasks:
        return []
    result = tasks[0].get("result", [])
    if isinstance(result, list) and result and isinstance(result[0], list):
        result = result[0]
    if isinstance(result, list):
        return result
    return []


def brand_root(brand_domain):
    """Normalize a brand domain (strip scheme/www/trailing slash, lowercase)."""
    d = (brand_domain or "").lower().strip()
    d = d.removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    return d


def domain_from_url(url):
    """Extract registrable domain from a URL, stripping www. and subdomains."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        m = re.match(r"https?://([^/]+)", url)
        host = (m.group(1) if m else url).lower()
    if not host:
        return ""
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) > 2 and parts[-1] in ("com", "org", "net", "io", "co", "ai", "dev"):
        return ".".join(parts[-2:])
    return host


# ---------------------------------------------------------------------------
# Function 1: fetch_ai_brand_mentions
# ---------------------------------------------------------------------------

def fetch_ai_brand_mentions(brand_domain, platform="chat_gpt"):
    """Fetch pre-aggregated mention/citation data for a brand from the
    DataForSEO ai_optimization/llm_mentions/target_metrics/live endpoint.

    Args:
      brand_domain: domain to track (e.g. 'softprowatersystems.com')
      platform: 'chat_gpt' or 'google' (Gemini dataset surface)

    Returns:
      {
        "brand_domain": str,
        "platform": str,
        "aggregated_metrics": {...}|None,   # raw 'total' metrics block
        "total_mentions": int,              # aggregated mention count
        "citation_count": int,              # number of distinct citing domains
        "top_citing_sources": [{"domain","mentions"}, ...],  # sorted desc
        "raw": {...}|None,
        "status": "ok"|"empty"|"error"
      }
    """
    bd = brand_root(brand_domain)
    payload = {
        "target": [{"domain": bd}],
        "platform": platform,
    }
    path = "ai_optimization/llm_mentions/target_metrics/live"
    resp = _dfs_api_call(path, payload=payload, method="POST")

    if resp is None:
        return {
            "brand_domain": bd,
            "platform": platform,
            "aggregated_metrics": None,
            "total_mentions": 0,
            "citation_count": 0,
            "top_citing_sources": [],
            "raw": None,
            "status": "error",
        }

    items = _dfs_result_items(resp)
    if not items or not isinstance(items[0], dict):
        return {
            "brand_domain": bd,
            "platform": platform,
            "aggregated_metrics": None,
            "total_mentions": 0,
            "citation_count": 0,
            "top_citing_sources": [],
            "raw": resp,
            "status": "empty",
        }

    item = items[0]
    agg = item.get("aggregated_metrics", {}) if isinstance(item.get("aggregated_metrics"), dict) else {}
    total = agg.get("total", {}) if isinstance(agg.get("total"), dict) else {}
    sources = agg.get("sources_domain", []) if isinstance(agg.get("sources_domain"), list) else []

    total_mentions = 0
    if isinstance(total, dict):
        m = total.get("mentions")
        if isinstance(m, (int, float)):
            total_mentions = int(m)

    # Build top citing sources: each source is {key: domain, mentions: N}
    top_sources = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        dom = str(s.get("key", "") or s.get("domain", "") or "")
        m = s.get("mentions", 0)
        try:
            m = int(m) if m is not None else 0
        except (TypeError, ValueError):
            m = 0
        if dom:
            top_sources.append({"domain": dom, "mentions": m})
    top_sources.sort(key=lambda x: x["mentions"], reverse=True)

    return {
        "brand_domain": bd,
        "platform": platform,
        "aggregated_metrics": total,
        "total_mentions": total_mentions,
        "citation_count": len(top_sources),
        "top_citing_sources": top_sources,
        "raw": item,
        "status": "ok" if (total or top_sources) else "empty",
    }


# ---------------------------------------------------------------------------
# Function 2: fetch_web_mentions
# ---------------------------------------------------------------------------

def fetch_web_mentions(brand_name, limit=50):
    """Use the Perplexity Sonar API to search for recent brand mentions across
    the web. Returns mention URLs, sources and surrounding context.

    The prompt is built with string concatenation of the brand_name variable
    (no template placeholders).

    Args:
      brand_name: brand to search for (e.g. 'SoftPro')
      limit: cap on number of mention entries returned

    Returns:
      {
        "brand_name": str,
        "mentions": [{"source","url","title","snippet","date"}, ...],
        "answer": str,
        "citations": [...],
        "count": int,
        "status": "ok"|"error",
        "error": str|None
      }
    """
    api_key = get_perplexity_key()
    if not api_key:
        return {
            "brand_name": brand_name,
            "mentions": [],
            "answer": "",
            "citations": [],
            "count": 0,
            "status": "error",
            "error": "PERPLEXITY_API_KEY not found in ~/.hermes/.env or env",
        }

    # String concatenation (NOT template placeholders) per spec.
    prompt = (
        "Find recent mentions of " + brand_name + " online. "
        "List the websites, articles, reviews, forum threads and news pieces "
        "that mention " + brand_name + ", with their URLs and a short snippet "
        "of the surrounding context for each mention. Include the publication "
        "date if available."
    )
    system_prompt = (
        "You are a research assistant. Provide detailed answers with sources. "
        "Include specific URLs, publication names, and direct context for every "
        "mention you find."
    )

    payload = json.dumps({
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2500,
    }).encode("utf-8")

    req = urllib.request.Request(
        PERPLEXITY_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {
            "brand_name": brand_name,
            "mentions": [],
            "answer": "",
            "citations": [],
            "count": 0,
            "status": "error",
            "error": f"Perplexity HTTP {e.code}: {body_text}",
        }
    except Exception as e:
        return {
            "brand_name": brand_name,
            "mentions": [],
            "answer": "",
            "citations": [],
            "count": 0,
            "status": "error",
            "error": f"Perplexity request failed: {e}",
        }

    answer = ""
    choices = result.get("choices", [])
    if choices:
        answer = choices[0].get("message", {}).get("content", "")
    citations = result.get("citations", []) or []
    search_results = result.get("search_results", []) or []

    # Build a lookup of url -> meta from search_results (richer detail)
    meta_by_url = {}
    for sr in search_results:
        if not isinstance(sr, dict):
            continue
        u = sr.get("url", "")
        if u:
            meta_by_url[u] = {
                "title": sr.get("title", "") or "",
                "date": sr.get("date", "") or "",
            }

    mentions = []
    seen_urls = set()
    # Prefer search_results order (already de-duplicated by Perplexity)
    for sr in search_results:
        if not isinstance(sr, dict):
            continue
        u = sr.get("url", "")
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        dom = domain_from_url(u)
        title = sr.get("title", "") or ""
        date = sr.get("date", "") or ""
        snippet = _extract_snippet(answer, u, brand_name)
        mentions.append({
            "source": dom,
            "url": u,
            "title": title,
            "snippet": snippet,
            "date": date,
        })

    # Fall back to raw citations list if search_results was empty
    if not mentions and citations:
        for u in citations:
            if not isinstance(u, str) or u in seen_urls:
                continue
            seen_urls.add(u)
            dom = domain_from_url(u)
            meta = meta_by_url.get(u, {})
            snippet = _extract_snippet(answer, u, brand_name)
            mentions.append({
                "source": dom,
                "url": u,
                "title": meta.get("title", ""),
                "snippet": snippet,
                "date": meta.get("date", ""),
            })

    mentions = mentions[:limit]
    return {
        "brand_name": brand_name,
        "mentions": mentions,
        "answer": answer,
        "citations": citations,
        "count": len(mentions),
        "status": "ok",
        "error": None,
    }


def _extract_snippet(answer_text, url, brand_name):
    """Pull a short context snippet around a URL/domain or brand name from the
    Perplexity answer. Falls back to the first ~160 chars of the answer."""
    if not answer_text:
        return ""
    # Try to find the URL (or its domain) inside the answer and grab context
    dom = domain_from_url(url)
    needles = [url, dom, brand_name]
    text_lower = answer_text.lower()
    for needle in needles:
        if not needle:
            continue
        idx = text_lower.find(needle.lower())
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(answer_text), idx + len(needle) + 120)
            snippet = answer_text[start:end].strip()
            # collapse whitespace
            snippet = re.sub(r"\s+", " ", snippet)
            return snippet
    # fallback
    snippet = re.sub(r"\s+", " ", answer_text).strip()
    return snippet[:160]


# ---------------------------------------------------------------------------
# Function 3: compare_mention_trends
# ---------------------------------------------------------------------------

def compare_mention_trends(current, previous):
    """Compare the current mention snapshot against a previous one and compute
    deltas.

    Args:
      current:  snapshot dict (see build_snapshot) for this run
      previous:  snapshot dict for the prior run, or None

    Returns:
      {
        "has_previous": bool,
        "current_total_mentions": int,
        "previous_total_mentions": int,
        "mention_delta": int,
        "trend": "up"|"down"|"stable",
        "current_sources": [...],
        "previous_sources": [...],
        "new_sources": [...],        # present now, not before
        "lost_sources": [...],       # present before, not now
        "new_source_count": int,
        "lost_source_count": int,
        "web_count_delta": int,       # web mention count delta
        "web_trend": "up"|"down"|"stable"
      }
      If previous is None, has_previous is False and deltas are 0/empty.
    """
    if not previous:
        return {
            "has_previous": False,
            "current_total_mentions": current.get("total_ai_mentions", 0),
            "previous_total_mentions": 0,
            "mention_delta": 0,
            "trend": "stable",
            "current_sources": current.get("ai_sources", []),
            "previous_sources": [],
            "new_sources": [],
            "lost_sources": [],
            "new_source_count": 0,
            "lost_source_count": 0,
            "web_count_delta": 0,
            "web_trend": "stable",
        }

    cur_total = current.get("total_ai_mentions", 0)
    prev_total = previous.get("total_ai_mentions", 0)
    delta = cur_total - prev_total
    if delta > 0:
        trend = "up"
    elif delta < 0:
        trend = "down"
    else:
        trend = "stable"

    cur_sources = set(current.get("ai_sources", []))
    prev_sources = set(previous.get("ai_sources", []))
    new_sources = sorted(cur_sources - prev_sources)
    lost_sources = sorted(prev_sources - cur_sources)

    cur_web = current.get("total_web_mentions", 0)
    prev_web = previous.get("total_web_mentions", 0)
    web_delta = cur_web - prev_web
    if web_delta > 0:
        web_trend = "up"
    elif web_delta < 0:
        web_trend = "down"
    else:
        web_trend = "stable"

    return {
        "has_previous": True,
        "current_total_mentions": cur_total,
        "previous_total_mentions": prev_total,
        "mention_delta": delta,
        "trend": trend,
        "current_sources": sorted(cur_sources),
        "previous_sources": sorted(prev_sources),
        "new_sources": new_sources,
        "lost_sources": lost_sources,
        "new_source_count": len(new_sources),
        "lost_source_count": len(lost_sources),
        "web_count_delta": web_delta,
        "web_trend": web_trend,
    }


def build_snapshot(ai_mentions, web_mentions):
    """Build a compact, comparable snapshot for trend tracking across runs."""
    total_ai = 0
    sources = set()
    for plat_key, res in (ai_mentions or {}).items():
        total_ai += res.get("total_mentions", 0)
        for s in res.get("top_citing_sources", []):
            sources.add(s.get("domain", ""))
    return {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_ai_mentions": total_ai,
        "ai_sources": sorted(sources),
        "total_web_mentions": (web_mentions or {}).get("count", 0),
        "web_sources": sorted({
            m.get("source", "") for m in (web_mentions or {}).get("mentions", [])
        }),
    }


# ---------------------------------------------------------------------------
# Sentiment helpers
# ---------------------------------------------------------------------------

def classify_sentiment(text):
    """Classify a snippet as positive/neutral/negative using keyword lexicons."""
    if not text:
        return "neutral"
    text_lower = text.lower()
    pos_hits = sum(1 for w in POSITIVE_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower))
    neg_hits = sum(1 for w in NEGATIVE_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower))
    if neg_hits > pos_hits:
        return "negative"
    if pos_hits > neg_hits:
        return "positive"
    return "neutral"


# ---------------------------------------------------------------------------
# White SaaS theme CSS
# ---------------------------------------------------------------------------

_WHITE_SAAS_CSS = """
  body { background:#fff; color:#1a202c; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; margin:0; padding:40px 20px; }
  .container { max-width:1000px; margin:0 auto; }
  h1 { font-size:28px; font-weight:700; margin:0 0 4px; color:#0f2a43; }
  h2 { font-size:20px; font-weight:600; margin:32px 0 12px; color:#0f2a43; border-bottom:1px solid #e2e8f0; padding-bottom:6px; }
  h3 { font-size:16px; font-weight:600; margin:20px 0 8px; color:#2d3748; }
  .meta { color:#718096; font-size:13px; margin-bottom:24px; }
  a { color:#2563eb; text-decoration:none; word-break:break-all; }
  a:hover { text-decoration:underline; }
  table { border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }
  th, td { border:1px solid #e2e8f0; padding:8px 10px; text-align:left; vertical-align:top; }
  th { background:#f7fafc; font-weight:600; color:#2d3748; }
  td.yes { color:#16a34a; font-weight:600; }
  td.no { color:#dc2626; }
  td.na { color:#9ca3af; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:16px 20px; margin:12px 0; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; margin:16px 0 24px; }
  .stat { background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:18px 20px; }
  .stat .label { font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; color:#6b7280; }
  .stat .value { font-size:26px; font-weight:700; color:#0f2a43; margin-top:4px; }
  .stat .sub { font-size:12px; color:#9ca3af; margin-top:2px; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .pill-ok, .pill-up { background:#dcfce7; color:#166534; }
  .pill-warn, .pill-stable { background:#fef9c3; color:#854d0e; }
  .pill-err, .pill-down { background:#fee2e2; color:#991b1b; }
  .pill-info { background:#dbeafe; color:#1e40af; }
  .sent-pos { color:#166534; font-weight:600; }
  .sent-neg { color:#991b1b; font-weight:600; }
  .sent-neu { color:#6b7280; font-weight:600; }
  details { background:#f9fafb; border:1px solid #e2e8f0; border-radius:6px; margin:8px 0; }
  details > summary { cursor:pointer; padding:10px 14px; font-weight:600; color:#2d3748; list-style:none; }
  details > summary::-webkit-details-marker { display:none; }
  details > summary::before { content:"\25B6"; color:#2563eb; margin-right:8px; font-size:10px; }
  details[open] > summary::before { content:"\25BC"; }
  details[open] > .body { padding:0 14px 12px; }
  .small { font-size:14px; color:#6b7280; }
  .rec { background:#fff; border:1px solid #e2e8f0; border-left:4px solid #2563eb; border-radius:6px; padding:12px 16px; margin:10px 0; }
  .rec .title { font-weight:600; color:#0f2a43; margin-bottom:4px; }
  .footer { margin-top:40px; padding-top:16px; border-top:1px solid #e2e8f0; font-size:12px; color:#9ca3af; }
  .trend-up { color:#16a34a; } .trend-down { color:#dc2626; } .trend-stable { color:#6b7280; }
  .noindex-meta { display:none; }
"""


def _sentiment_pill(label):
    cls = {"positive": "sent-pos", "negative": "sent-neg", "neutral": "sent-neu"}.get(label, "sent-neu")
    return f'<span class="{cls}">{html_escape(label.capitalize())}</span>'


def _trend_pill(trend):
    cls = {"up": "pill-up", "down": "pill-down", "stable": "pill-stable"}.get(trend, "pill-stable")
    arrow = {"up": "\u2191", "down": "\u2193", "stable": "\u2194"}.get(trend, "\u2194")
    return f'<span class="pill {cls}">{arrow} {trend}</span>'


# ---------------------------------------------------------------------------
# Function 4: generate_mention_report
# ---------------------------------------------------------------------------

def generate_mention_report(brand_domain, brand_name, ai_mentions, web_mentions,
                            output_path, trend=None):
    """Generate a white SaaS themed HTML mention report and a JSON companion.

    Args:
      brand_domain: target domain
      brand_name: display brand name
      ai_mentions: dict mapping platform key -> fetch_ai_brand_mentions result
      web_mentions: fetch_web_mentions result dict
      output_path: directory to write the report into
      trend: optional compare_mention_trends result (None if no previous run)

    Returns:
      html_report_path (str)
    """
    bd = brand_root(brand_domain)
    os.makedirs(output_path, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    ai_mentions = ai_mentions or {}
    web_mentions = web_mentions or {}

    # ---- summary stats ----
    total_ai_mentions = sum(r.get("total_mentions", 0) for r in ai_mentions.values())
    total_ai_citations = sum(r.get("citation_count", 0) for r in ai_mentions.values())
    total_web_mentions = web_mentions.get("count", 0)
    ai_citation_rate = 0.0
    if total_ai_mentions:
        # citation_rate = share of mention volume that also produced citations
        ai_citation_rate = (total_ai_citations / total_ai_mentions) * 100 if total_ai_mentions else 0.0
    new_this_period = 0
    trend_html = ""
    if trend and trend.get("has_previous"):
        new_this_period = max(0, trend.get("mention_delta", 0))
        trend_html = _render_trend_section(trend)

    # ---- summary cards ----
    cards = f"""
      <div class="stat">
        <div class="label">Total AI Mentions</div>
        <div class="value">{total_ai_mentions}</div>
        <div class="sub">across {len(ai_mentions)} platform(s)</div>
      </div>
      <div class="stat">
        <div class="label">Total Web Mentions</div>
        <div class="value">{total_web_mentions}</div>
        <div class="sub">via Perplexity web search</div>
      </div>
      <div class="stat">
        <div class="label">AI Citation Rate</div>
        <div class="value">{ai_citation_rate:.1f}%</div>
        <div class="sub">{total_ai_citations} citing sources</div>
      </div>
      <div class="stat">
        <div class="label">New Mentions This Period</div>
        <div class="value">{new_this_period}</div>
        <div class="sub">{'vs. previous snapshot' if (trend and trend.get('has_previous')) else 'no prior baseline'}</div>
      </div>
    """

    # ---- AI mention breakdown by platform ----
    platform_rows = ""
    for plat_key, human_label in AI_PLATFORMS:
        res = ai_mentions.get(plat_key)
        if not res:
            platform_rows += (
                f"<tr><td>{html_escape(human_label)}</td>"
                f"<td class='na'>not run</td>"
                f"<td class='na'>—</td>"
                f"<td class='na'>—</td></tr>"
            )
            continue
        mentions = res.get("total_mentions", 0)
        citations = res.get("citation_count", 0)
        status = res.get("status", "")
        st_pill = (
            f'<span class="pill pill-ok">ok</span>' if status == "ok"
            else f'<span class="pill pill-warn">{html_escape(status)}</span>'
        )
        platform_rows += (
            f"<tr><td>{html_escape(human_label)}<br>"
            f"<span class='small'>(platform: {html_escape(res.get('platform', plat_key))})</span></td>"
            f"<td>{mentions}</td>"
            f"<td>{citations}</td>"
            f"<td>{st_pill}</td></tr>"
        )

    platform_table = f"""
    <table>
      <thead><tr><th>AI Platform</th><th>Mentions</th><th>Citing Sources</th><th>Status</th></tr></thead>
      <tbody>{platform_rows or '<tr><td colspan=4 class=na>No AI mention data</td></tr>'}</tbody>
    </table>
    """

    # ---- top citing sources in AI answers ----
    top_sources_rows = ""
    # Merge across platforms, summing mentions per domain
    merged_sources = {}
    for res in ai_mentions.values():
        for s in res.get("top_citing_sources", []):
            dom = s.get("domain", "")
            merged_sources[dom] = merged_sources.get(dom, 0) + s.get("mentions", 0)
    sorted_sources = sorted(merged_sources.items(), key=lambda x: x[1], reverse=True)
    for dom, m in sorted_sources[:25]:
        is_brand = dom and bd and dom.lower().rstrip(".") == bd.lower().rstrip(".")
        brand_tag = ' <span class="pill pill-info">brand</span>' if is_brand else ""
        top_sources_rows += (
            f"<tr><td>{html_escape(dom)}{brand_tag}</td><td>{m}</td></tr>"
        )
    top_sources_table = f"""
    <table>
      <thead><tr><th>Cited Domain</th><th>Mentions</th></tr></thead>
      <tbody>{top_sources_rows or '<tr><td colspan=2 class=na>No citing sources found</td></tr>'}</tbody>
    </table>
    """

    # ---- web mentions table (expandable) ----
    web_rows = ""
    sentiments = {"positive": 0, "neutral": 0, "negative": 0}
    for m in web_mentions.get("mentions", []):
        snippet = m.get("snippet", "") or ""
        sent = classify_sentiment(snippet)
        sentiments[sent] += 1
        title = m.get("title", "") or "(untitled)"
        date = m.get("date", "") or "—"
        web_rows += (
            f"<tr><td>{html_escape(m.get('source', ''))}</td>"
            f"<td><a href='{html_escape(m.get('url', ''))}' rel='nofollow'>{html_escape(m.get('url', ''))}</a></td>"
            f"<td>{html_escape(title)}</td>"
            f"<td class='small'>{html_escape(snippet[:220])}</td>"
            f"<td>{html_escape(date)}</td>"
            f"<td>{_sentiment_pill(sent)}</td></tr>"
        )
    web_table = f"""
    <details open>
      <summary>Web mentions ({total_web_mentions} found) — click to collapse</summary>
      <div class="body">
      <table>
        <thead><tr><th>Source</th><th>URL</th><th>Title</th><th>Snippet</th><th>Date</th><th>Sentiment</th></tr></thead>
        <tbody>{web_rows or '<tr><td colspan=6 class=na>No web mentions found</td></tr>'}</tbody>
      </table>
      </div>
    </details>
    <p class='small'>Sentiment breakdown: 
      <span class='sent-pos'>positive {sentiments['positive']}</span> · 
      <span class='sent-neu'>neutral {sentiments['neutral']}</span> · 
      <span class='sent-neg'>negative {sentiments['negative']}</span>
    </p>
    """

    # ---- recommendations ----
    recs = _build_recommendations(
        bd, brand_name, ai_mentions, web_mentions, sorted_sources, trend
    )
    recs_html = ""
    for r in recs:
        recs_html += (
            f"<div class='rec'>"
            f"<div class='title'>{html_escape(r['title'])}</div>"
            f"<div class='small'>{html_escape(r['detail'])}</div>"
            f"</div>"
        )
    if not recs_html:
        recs_html = "<p class='small'>No recommendations at this time.</p>"

    # ---- assemble HTML ----
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Brand Mention Monitor — {html_escape(bd)}</title>
<style>{_WHITE_SAAS_CSS}</style>
</head>
<body>
<div class="container">
  <h1>Brand Mention Monitor</h1>
  <div class="meta">Brand: <strong>{html_escape(brand_name)}</strong> ({html_escape(bd)}) · Generated: {ts}</div>

  <h2>Summary</h2>
  <div class="cards">
    {cards}
  </div>

  <h2>AI Mention Breakdown by Platform</h2>
  <div class="card">
    {platform_table}
  </div>

  <h2>Top Citing Sources in AI Answers</h2>
  <div class="card">
    {top_sources_table}
  </div>

  <h2>Web Mentions</h2>
  <div class="card">
    {web_table}
  </div>

  <h2>Mention Trend</h2>
  {trend_html if trend_html else '<div class="card"><p class="small">No previous snapshot found — this run establishes the baseline. Re-run later to see trend deltas.</p></div>'}

  <h2>Recommendations</h2>
  {recs_html}

  <div class="footer">
    Generated by brand_mention_monitor.py · DataForSEO AI Optimization API + Perplexity Sonar ·
    White SaaS theme · <span class="noindex-meta">noindex,nofollow</span>
  </div>
</div>
</body>
</html>"""

    html_path = os.path.join(output_path, f"{bd}_brand_mentions.html")
    json_path = os.path.join(output_path, f"{bd}_brand_mentions.json")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    snapshot = build_snapshot(ai_mentions, web_mentions)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "brand_domain": bd,
            "brand_name": brand_name,
            "generated_at": ts,
            "ai_mentions": ai_mentions,
            "web_mentions": {
                "brand_name": web_mentions.get("brand_name", brand_name),
                "count": web_mentions.get("count", 0),
                "mentions": web_mentions.get("mentions", []),
                "citations": web_mentions.get("citations", []),
                "status": web_mentions.get("status", ""),
            },
            "summary": {
                "total_ai_mentions": total_ai_mentions,
                "total_ai_citations": total_ai_citations,
                "total_web_mentions": total_web_mentions,
                "ai_citation_rate": round(ai_citation_rate, 1),
                "new_this_period": new_this_period,
            },
            "trend": trend,
            "snapshot": snapshot,
        }, f, indent=2)
    return html_path


def _render_trend_section(trend):
    """Render the mention trend HTML block from a compare_mention_trends result."""
    t = trend.get("trend", "stable")
    wt = trend.get("web_trend", "stable")
    mention_delta = trend.get("mention_delta", 0)
    web_delta = trend.get("web_count_delta", 0)
    new_sources = trend.get("new_sources", [])
    lost_sources = trend.get("lost_sources", [])
    new_rows = "".join(
        f"<tr><td>{html_escape(s)}</td></tr>" for s in new_sources
    ) or "<tr><td class='na'>None</td></tr>"
    lost_rows = "".join(
        f"<tr><td>{html_escape(s)}</td></tr>" for s in lost_sources
    ) or "<tr><td class='na'>None</td></tr>"
    return f"""
    <div class="card">
      <table>
        <tr><td><strong>AI mentions vs previous</strong></td>
            <td>{trend.get('previous_total_mentions', 0)} → {trend.get('current_total_mentions', 0)} 
                ({'+' if mention_delta >= 0 else ''}{mention_delta}) {_trend_pill(t)}</td></tr>
        <tr><td><strong>Web mentions vs previous</strong></td>
            <td>{'+' if web_delta >= 0 else ''}{web_delta} {_trend_pill(wt)}</td></tr>
        <tr><td><strong>New citing sources</strong></td><td>{trend.get('new_source_count', 0)}</td></tr>
        <tr><td><strong>Lost citing sources</strong></td><td>{trend.get('lost_source_count', 0)}</td></tr>
      </table>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:12px;">
        <div>
          <h3>New sources</h3>
          <table><tbody>{new_rows}</tbody></table>
        </div>
        <div>
          <h3>Lost sources</h3>
          <table><tbody>{lost_rows}</tbody></table>
        </div>
      </div>
    </div>
    """


def _build_recommendations(bd, brand_name, ai_mentions, web_mentions,
                           sorted_sources, trend):
    """Generate actionable recommendations grounded in the findings."""
    recs = []
    total_ai = sum(r.get("total_mentions", 0) for r in ai_mentions.values())
    total_web = web_mentions.get("count", 0)

    # AI visibility
    if total_ai == 0:
        recs.append({
            "title": "Build foundational AI visibility",
            "detail": (
                "The brand has no mentions in the AI mention dataset yet. "
                "Create authoritative, citeable content (guides, comparisons, "
                "data pages) so LLMs encounter and reference " + brand_name +
                " when answering relevant queries."
            ),
        })
    else:
        # Recommend targeting the most-cited third-party sources for citations
        top_third_party = [d for d, m in sorted_sources
                           if d and d.lower().rstrip(".") != bd.lower().rstrip(".")][:5]
        if top_third_party:
            recs.append({
                "title": "Earn citations on top-cited third-party sources",
                "detail": (
                    "When AI answers mention the brand, they most often cite: "
                    + ", ".join(top_third_party) +
                    ". Pursue placements, contributed articles, reviews and "
                    "data partnerships on these domains to increase citation "
                    "frequency."
                ),
            })
        else:
            recs.append({
                "title": "Strengthen third-party citation coverage",
                "detail": (
                    "AI answers cite the brand but rely on few external sources. "
                    "Build presence on review sites, industry directories and "
                    "publisher pages so models have more authoritative sources "
                    "to cite alongside " + brand_name + "."
                ),
            })

    # Platform gap
    chat_gpt = ai_mentions.get("chat_gpt", {})
    google = ai_mentions.get("google", {})
    if chat_gpt.get("total_mentions", 0) == 0 and google.get("total_mentions", 0) > 0:
        recs.append({
            "title": "Close the ChatGPT visibility gap",
            "detail": (
                "The brand appears in the Google/Gemini dataset but not in "
                "ChatGPT. ChatGPT's dataset is US/English — ensure content "
                "targets US-English queries and is crawlable by OpenAI's "
                "indexing partners."
            ),
        })
    elif google.get("total_mentions", 0) == 0 and chat_gpt.get("total_mentions", 0) > 0:
        recs.append({
            "title": "Expand Google/Gemini coverage",
            "detail": (
                "The brand is mentioned by ChatGPT but not surfaced in the "
                "Gemini dataset. Strengthen structured data and authoritative "
                "sources that Google's AI Overviews and Gemini tend to cite."
            ),
        })

    # Web mentions
    if total_web == 0:
        recs.append({
            "title": "Increase web mention footprint",
            "detail": (
                "No recent web mentions were found via Perplexity. Invest in "
                "PR, digital outreach, and content marketing to generate "
                "fresh, citeable mentions of " + brand_name + " across "
                "publishers, blogs and forums."
            ),
        })
    elif total_web < 10:
        recs.append({
            "title": "Scale web mention volume",
            "detail": (
                "Only " + str(total_web) + " recent web mentions found. "
                "Broaden outreach — HARO responses, guest posts, podcast "
                "appearances and review seeding — to grow the surface area "
                "AI engines can draw from."
            ),
        })

    # Trend-based
    if trend and trend.get("has_previous"):
        d = trend.get("mention_delta", 0)
        if d < 0:
            recs.append({
                "title": "Reverse the downward mention trend",
                "detail": (
                    "AI mentions dropped by " + str(abs(d)) + " vs the previous "
                    "snapshot. Audit recently lost citing sources (listed in the "
                    "trend section) and re-engage those publishers."
                ),
            })
        elif d > 0:
            recs.append({
                "title": "Maintain positive momentum",
                "detail": (
                    "AI mentions rose by " + str(d) + " since the last run. "
                    "Double down on the content and partnerships driving the gain."
                ),
            })

    # Sentiment-based
    neg = 0
    total_snippets = 0
    for m in web_mentions.get("mentions", []):
        total_snippets += 1
        if classify_sentiment(m.get("snippet", "")) == "negative":
            neg += 1
    if total_snippets and neg / total_snippets > 0.3:
        recs.append({
            "title": "Manage negative sentiment around mentions",
            "detail": (
                str(neg) + " of " + str(total_snippets) + " web mentions read "
                "as negative. Prioritize reputation management — respond to "
                "complaints, publish positive case studies, and counter "
                "negative coverage with authoritative content."
            ),
        })

    return recs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_previous_snapshot(output_path, bd):
    """Load the most recent prior brand_mentions JSON snapshot, if any."""
    if not os.path.isdir(output_path):
        return None
    candidates = []
    for fname in os.listdir(output_path):
        if fname.startswith(bd) and fname.endswith("_brand_mentions.json"):
            fpath = os.path.join(output_path, fname)
            try:
                mtime = os.path.getmtime(fpath)
                candidates.append((mtime, fpath))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    # The freshest is the one we'd OVERWRITE this run — for a true "previous"
    # comparison we want the run before this one. We load it now (before
    # overwrite) so it represents the prior snapshot.
    _, fpath = candidates[0]
    try:
        with open(fpath) as f:
            data = json.load(f)
        snap = data.get("snapshot")
        if snap:
            return snap
    except Exception:
        return None
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Track brand mentions across AI engines and the web."
    )
    parser.add_argument("--domain", required=True,
                        help="Brand domain, e.g. softprowatersystems.com")
    parser.add_argument("--brand", required=True,
                        help="Display brand name, e.g. SoftPro")
    parser.add_argument("--output", default="/tmp/brand_mention_report",
                        help="Output directory for the HTML report")
    parser.add_argument("--platform", default=None,
                        choices=["chat_gpt", "google", "both"],
                        help="AI platform(s) to track (default: both)")
    args = parser.parse_args()

    bd = brand_root(args.domain)
    brand_name = args.brand

    platform_choice = args.platform or "both"

    # ---- Fetch AI brand mentions (both platforms for the breakdown) ----
    ai_mentions = {}
    plat_keys = []
    if platform_choice == "both":
        plat_keys = [p for p, _ in AI_PLATFORMS]
    else:
        plat_keys = [platform_choice]
    for plat_key in plat_keys:
        human = dict(AI_PLATFORMS).get(plat_key, plat_key)
        print(f"[monitor] Fetching AI mention metrics for {bd} ({human}) ...",
              file=sys.stderr)
        res = fetch_ai_brand_mentions(bd, platform=plat_key)
        ai_mentions[plat_key] = res
        print(f"[monitor]   {human}: status={res['status']} "
              f"mentions={res['total_mentions']} citing_sources={res['citation_count']}",
              file=sys.stderr)

    # ---- Fetch web mentions via Perplexity ----
    print(f"[monitor] Fetching web mentions for '{brand_name}' via Perplexity ...",
          file=sys.stderr)
    web_mentions = fetch_web_mentions(brand_name, limit=50)
    print(f"[monitor]   web: status={web_mentions['status']} "
          f"count={web_mentions['count']}"
          + (f" error={web_mentions.get('error')}" if web_mentions.get("error") else ""),
          file=sys.stderr)

    # ---- Trend: load previous snapshot BEFORE we overwrite it ----
    previous = _load_previous_snapshot(args.output, bd)
    current_snapshot = build_snapshot(ai_mentions, web_mentions)
    trend = compare_mention_trends(current_snapshot, previous)
    if trend["has_previous"]:
        print(f"[monitor] Trend vs previous: AI mentions "
              f"{trend['previous_total_mentions']} -> {trend['current_total_mentions']} "
              f"({trend['trend']})", file=sys.stderr)
    else:
        print("[monitor] No previous snapshot — establishing baseline.",
              file=sys.stderr)

    # ---- Generate report ----
    report_path = generate_mention_report(
        bd, brand_name, ai_mentions, web_mentions, args.output, trend=trend
    )
    json_path = os.path.join(args.output, f"{bd}_brand_mentions.json")
    print(f"\nReport written: {report_path}")
    print(f"Snapshot JSON:  {json_path}")
    print(f"Summary: AI mentions={current_snapshot['total_ai_mentions']} "
          f"web mentions={current_snapshot['total_web_mentions']}")


if __name__ == "__main__":
    main()
