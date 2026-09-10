#!/usr/bin/env python3
"""
ai_visibility_helper.py — Test brand visibility across multiple AI engines
(ChatGPT, Gemini, Claude, Perplexity) using DataForSEO's AI Optimization endpoints.

Replaces the Perplexity Sonar single-LLM AEO citation testing with multi-LLM
coverage via DataForSEO.

Usage:
  python3 ai_visibility_helper.py --brand <domain> --keyword '<keyword>' [--output <dir>]
  python3 ai_visibility_helper.py --brand <domain> --keyword '<kw1>' --keyword '<kw2>' --output <dir>
  python3 ai_visibility_helper.py --metrics --brand <domain> [--platform chat_gpt|google]

Functions (importable into page_auditor.py):
  test_brand_visibility(keyword, brand_domain, llm_types=[...]) -> dict
  get_mention_metrics(brand_domain, platform='chat_gpt') -> dict
  generate_visibility_report(brand_domain, keywords, output_path) -> str

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD  (from ~/.hermes/.env or env vars)
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
from html import escape as html_escape

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = "https://api.dataforseo.com/v3"

# Map our friendly llm_type -> DataForSEO path segment
LLM_PATH_MAP = {
    "chat_gpt": "chat_gpt",
    "google": "gemini",     # 'google' alias -> gemini endpoint
    "gemini": "gemini",
    "claude": "claude",
    "perplexity": "perplexity",
}

DEFAULT_LLM_TYPES = ["chat_gpt", "google", "perplexity", "claude"]

# Sensible default model per llm_type (overridden by /models lookup when possible)
DEFAULT_MODELS = {
    "chat_gpt": "gpt-4o-mini",
    "gemini": "gemini-1.5-flash",
    "google": "gemini-1.5-flash",
    "claude": "claude-3-5-sonnet-20241022",
    "perplexity": "sonar",
}

HUMAN_LABELS = {
    "chat_gpt": "ChatGPT",
    "google": "Gemini",
    "gemini": "Gemini",
    "claude": "Claude",
    "perplexity": "Perplexity",
}


# ---------------------------------------------------------------------------
# Credentials & low-level API call
# ---------------------------------------------------------------------------

def get_creds():
    """Load DataForSEO credentials from env or ~/.hermes/.env."""
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATAFORSEO_LOGIN="):
                        login = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("DATAFORSEO_PASSWORD="):
                        password = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.",
              file=sys.stderr)
        print("Set them in ~/.hermes/.env or as environment variables.",
              file=sys.stderr)
        sys.exit(1)
    return login, password


def api_call(path, payload=None, method="POST", timeout=60):
    """Make a DataForSEO v3 API call via urllib. Returns parsed JSON or None.

    path: path segment under /v3/ (e.g. 'ai_optimization/chat_gpt/llm_responses/live')
    payload: dict for POST body (wrapped into [payload] list per DataForSEO convention)
    """
    login, password = get_creds()
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
        # 404 / 400 / 401 etc. — read body for context, return None gracefully
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if e.code == 401:
            print(f"ERROR 401 Unauthorized for {url}. Check DataForSEO credentials.",
                  file=sys.stderr)
        elif e.code == 404:
            # Some LLM types may not be available — handle silently
            print(f"WARNING 404 Not Found for {url}. Endpoint may be unavailable for this LLM.",
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


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

def get_models(llm_type):
    """Fetch available models for an llm_type. Returns list of dicts or []."""
    path_seg = LLM_PATH_MAP.get(llm_type, llm_type)
    path = f"ai_optimization/{path_seg}/llm_responses/models"
    resp = api_call(path, method="GET")
    if not resp:
        return []
    # Standard envelope: tasks[0].result[]  (V1-style; works for direct REST too)
    tasks = resp.get("tasks", [])
    if not tasks:
        return []
    result = tasks[0].get("result", [])
    # result may itself be a list of model dicts, or a list containing one dict
    if isinstance(result, list) and result and isinstance(result[0], list):
        result = result[0]
    if isinstance(result, list):
        return result
    return []


def pick_model(llm_type):
    """Pick the best model for an llm_type. Returns model_name string."""
    models = get_models(llm_type)
    if not models:
        return DEFAULT_MODELS.get(llm_type, "gpt-4o-mini")

    # Prefer web_search_supported models
    def tier_key(m):
        name = (m.get("model_name") or "").lower()
        # top tier keywords rank higher
        top = any(t in name for t in ("pro", "opus", "ultra", "sonar-pro", "sonar pro"))
        low = any(t in name for t in ("flash", "mini", "nano", "lite", "haiku"))
        return 0 if top else (1 if not low else 2)

    web_models = [m for m in models if m.get("web_search_supported")]
    pool = web_models if web_models else models
    pool_sorted = sorted(
        pool,
        key=lambda m: (
            tier_key(m),
            1 if m.get("reasoning") else 0,    # reasoning true preferred
            m.get("model_name") or "",
        ),
    )
    # tier_key ascending puts top-tier (0) first; reasoning True (1) should come first
    # so invert reasoning sort: use -reasoning
    pool_sorted = sorted(
        pool,
        key=lambda m: (
            tier_key(m),
            0 if m.get("reasoning") else 1,
            m.get("model_name") or "",
        ),
    )
    return pool_sorted[0].get("model_name") if pool_sorted else DEFAULT_MODELS.get(
        llm_type, "gpt-4o-mini"
    )


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------

def _result_items(resp):
    """Extract the result array from a DataForSEO response envelope."""
    if not resp:
        return []
    tasks = resp.get("tasks", [])
    if not tasks:
        return []
    result = tasks[0].get("result", [])
    # result can be [ {...} ] or [ [ {...} ] ]
    if isinstance(result, list) and result and isinstance(result[0], list):
        result = result[0]
    if isinstance(result, list):
        return result
    return []


def extract_answer_text(item):
    """Extract the assistant answer text from a live LLM response item."""
    items = item.get("items", []) if isinstance(item, dict) else []
    text_parts = []
    for el in items:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "message":
            for section in el.get("sections", []):
                if section.get("type") == "text" and section.get("text"):
                    text_parts.append(section["text"])
            # also some payloads put text directly
            if el.get("text") and not text_parts:
                text_parts.append(el["text"])
    return "\n".join(text_parts)


def extract_cited_urls(item):
    """Extract cited source URLs from a live LLM response item."""
    items = item.get("items", []) if isinstance(item, dict) else []
    urls = []
    for el in items:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "message":
            for ann in el.get("annotations", []):
                u = ann.get("url") if isinstance(ann, dict) else None
                if u:
                    urls.append(u)
        # some endpoints put links at top-level
        for link in el.get("links", []) if isinstance(el, dict) else []:
            if isinstance(link, dict) and link.get("url"):
                urls.append(link["url"])
    return urls


def domain_from_url(url):
    """Extract registrable domain from a URL, stripping www. and subdomains."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        # fallback: crude
        m = re.match(r"https?://([^/]+)", url)
        host = (m.group(1) if m else url).lower()
    if not host:
        return ""
    host = host.removeprefix("www.")
    # crude registrable: last two labels
    parts = host.split(".")
    if len(parts) > 2 and parts[-1] in ("com", "org", "net", "io", "co", "ai", "dev"):
        # drop subdomain
        return ".".join(parts[-2:])
    return host


def brand_root(brand_domain):
    """Normalize a brand domain for matching (strip www/https/trailing slash)."""
    d = (brand_domain or "").lower().strip()
    d = d.removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    return d


def brand_name_from_domain(brand_domain):
    """Derive a display brand name from the domain (e.g. softprowatersystems.com -> softpro)."""
    d = brand_root(brand_domain)
    parts = d.split(".")
    if len(parts) > 1:
        full = parts[-2].replace("-", " ").replace("_", " ")
        # Try to extract the brand name from compound domains
        # e.g. softprowatersystems -> softpro, watergeneratorpros -> watergenpros
        for sep in ['water', 'home', 'garden', 'regalo', 'supply', 'generator', 'filter']:
            if sep in full:
                idx = full.find(sep)
                brand_part = full[:idx]
                if len(brand_part) >= 3:
                    return brand_part
        return full
    return d


# ---------------------------------------------------------------------------
# Core function 1: test_brand_visibility
# ---------------------------------------------------------------------------

def test_brand_visibility(keyword, brand_domain, llm_types=None):
    """Send a prompt to each LLM via DataForSEO and check if the brand is
    mentioned or cited.

    Returns:
      {
        "keyword": str,
        "brand_domain": str,
        "results": {
            "<llm_type>": {
                "model_name": str,
                "mentioned": bool,
                "cited": bool,
                "answer_excerpt": str,
                "cited_sources": [str, ...],
                "error": str|None,
                "status": "ok"|"not_measured"|"unavailable"
            }, ...
        },
        "summary": {"llms_tested": N, "mentions": N, "citations": N}
      }
    """
    if llm_types is None:
        llm_types = DEFAULT_LLM_TYPES

    bd = brand_root(brand_domain)
    bname = brand_name_from_domain(brand_domain).lower()
    prompt = f"What is the best {keyword}?"

    results = {}
    for llm in llm_types:
        path_seg = LLM_PATH_MAP.get(llm, llm)
        model_name = pick_model(llm)
        entry = {
            "model_name": model_name,
            "mentioned": False,
            "cited": False,
            "answer_excerpt": "",
            "cited_sources": [],
            "error": None,
            "status": "ok",
        }

        payload = {
            "model_name": model_name,
            "user_prompt": prompt,
            "web_search": True,
            "use_improved_etv": True,
        }
        path = f"ai_optimization/{path_seg}/llm_responses/live"
        resp = api_call(path, payload=payload, method="POST")

        if resp is None:
            entry["status"] = "unavailable"
            entry["error"] = "Endpoint returned no response (404/HTTP error)"
            results[llm] = entry
            continue

        # check API-level status
        status_code = resp.get("status_code")
        if status_code and status_code != 20000:
            entry["status"] = "not_measured"
            entry["error"] = f"API status_code={status_code}: {resp.get('status_message','')}"
            results[llm] = entry
            continue

        items = _result_items(resp)
        if not items:
            entry["status"] = "not_measured"
            entry["error"] = "No result items returned"
            results[llm] = entry
            continue

        item = items[0] if isinstance(items[0], dict) else {}
        text = extract_answer_text(item)
        cited_urls = extract_cited_urls(item)
        cited_domains = list({domain_from_url(u) for u in cited_urls if u})

        # mention: brand name or domain appears in answer text (word boundary, ci)
        text_lower = text.lower()
        mentioned = False
        if bd and bd in text_lower:
            mentioned = True
        if bname and re.search(r"\b" + re.escape(bname) + r"\b", text_lower):
            mentioned = True
        # also match domain without TLD components in text
        root_label = bd.split(".")[0] if bd else ""
        if root_label and len(root_label) > 3 and re.search(
            r"\b" + re.escape(root_label) + r"\b", text_lower
        ):
            mentioned = True

        # citation: brand domain appears among cited source domains
        cited = bd in cited_domains if bd else False

        entry["mentioned"] = bool(mentioned)
        entry["cited"] = bool(cited)
        entry["answer_excerpt"] = text  # Store FULL response text, not truncated
        entry["cited_sources"] = sorted(cited_domains)
        results[llm] = entry

    mentions = sum(1 for r in results.values() if r["mentioned"])
    citations = sum(1 for r in results.values() if r["cited"])
    return {
        "keyword": keyword,
        "brand_domain": bd,
        "results": results,
        "summary": {
            "llms_tested": len(results),
            "mentions": mentions,
            "citations": citations,
        },
    }


# ---------------------------------------------------------------------------
# Core function 2: get_mention_metrics
# ---------------------------------------------------------------------------

def get_mention_metrics(brand_domain, platform="chat_gpt"):
    """Fetch pre-aggregated mention/citation data for a brand from the
    ai_optimization/llm_mentions/target_metrics/live endpoint.

    Returns:
      {
        "brand_domain": str,
        "platform": str,
        "aggregated_metrics": {...}|None,
        "sources_domain": [...],
        "raw": {...}|None,
        "status": "ok"|"empty"|"error"
      }
    """
    bd = brand_root(brand_domain)
    payload = {
        "target": [{"domain": bd}],
        "platform": platform,
        "use_improved_etv": True,
    }
    path = "ai_optimization/llm_mentions/target_metrics/live"
    resp = api_call(path, payload=payload, method="POST")

    if resp is None:
        return {
            "brand_domain": bd,
            "platform": platform,
            "aggregated_metrics": None,
            "sources_domain": [],
            "raw": None,
            "status": "error",
        }

    items = _result_items(resp)
    if not items or not isinstance(items[0], dict):
        return {
            "brand_domain": bd,
            "platform": platform,
            "aggregated_metrics": None,
            "sources_domain": [],
            "raw": resp,
            "status": "empty",
        }

    item = items[0]
    agg = item.get("aggregated_metrics", {})
    total = agg.get("total", {}) if isinstance(agg, dict) else {}
    sources = agg.get("sources_domain", []) if isinstance(agg, dict) else []

    return {
        "brand_domain": bd,
        "platform": platform,
        "aggregated_metrics": total,
        "sources_domain": sources,
        "raw": item,
        "status": "ok" if total else "empty",
    }


# ---------------------------------------------------------------------------
# Core function 3: generate_visibility_report
# ---------------------------------------------------------------------------

_WHITE_SAAS_CSS = """
  body { background:#fff; color:#1a202c; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; margin:0; padding:40px 20px; }
  .container { max-width:960px; margin:0 auto; }
  h1 { font-size:28px; font-weight:700; margin:0 0 4px; color:#0f2a43; }
  h2 { font-size:20px; font-weight:600; margin:32px 0 12px; color:#0f2a43; border-bottom:1px solid #e2e8f0; padding-bottom:6px; }
  h3 { font-size:16px; font-weight:600; margin:20px 0 8px; color:#2d3748; }
  .meta { color:#718096; font-size:13px; margin-bottom:24px; }
  a { color:#2563eb; text-decoration:none; }
  a:hover { text-decoration:underline; }
  table { border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }
  th, td { border:1px solid #e2e8f0; padding:8px 10px; text-align:left; }
  th { background:#f7fafc; font-weight:600; color:#2d3748; }
  td.yes { color:#16a34a; font-weight:600; }
  td.no { color:#dc2626; }
  td.na { color:#9ca3af; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:16px 20px; margin:12px 0; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .pill-ok { background:#dcfce7; color:#166534; }
  .pill-warn { background:#fef9c3; color:#854d0e; }
  .pill-err { background:#fee2e2; color:#991b1b; }
  .excerpt { background:#f9fafb; border-left:3px solid #2563eb; padding:12px 14px; font-size:15px; color:#374151; margin:6px 0; white-space:pre-wrap; max-height:400px; overflow-y:auto; overflow-x:hidden; word-wrap:break-word; }
  .small { font-size:14px; color:#6b7280; }
  .footer { margin-top:40px; padding-top:16px; border-top:1px solid #e2e8f0; font-size:12px; color:#9ca3af; }
"""


def _pill(status):
    cls = {"ok": "pill-ok", "not_measured": "pill-warn", "unavailable": "pill-err"}.get(
        status, "pill-err"
    )
    return f'<span class="pill {cls}">{html_escape(status)}</span>'


def generate_visibility_report(brand_domain, keywords, output_path):
    """Run visibility testing across all keywords and LLMs, then write an
    HTML report and a JSON companion. Returns the HTML report path (str)."""
    bd = brand_root(brand_domain)
    os.makedirs(output_path, exist_ok=True)

    all_results = []
    for kw in keywords:
        print(f"[visibility] Testing '{kw}' for {bd} ...", file=sys.stderr)
        res = test_brand_visibility(kw, bd)
        all_results.append(res)

    print(f"[visibility] Fetching mention metrics for {bd} (platform=chat_gpt) ...",
          file=sys.stderr)
    metrics = get_mention_metrics(bd, platform="chat_gpt")

    # ----- build HTML -----
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows_html = []
    for res in all_results:
        kw = res["keyword"]
        for llm, r in res["results"].items():
            label = HUMAN_LABELS.get(llm, llm)
            if r["status"] != "ok":
                rows_html.append(
                    f"<tr><td>{html_escape(kw)}</td><td>{html_escape(label)}</td>"
                    f"<td>{html_escape(r.get('model_name',''))}</td>"
                    f"<td class='na'>{_pill(r['status'])}</td>"
                    f"<td class='na'>—</td><td class='na'>—</td></tr>"
                )
                continue
            m = "✓ Yes" if r["mentioned"] else "✗ No"
            c = "✓ Yes" if r["cited"] else "✗ No"
            mcls = "yes" if r["mentioned"] else "no"
            ccls = "yes" if r["cited"] else "no"
            rows_html.append(
                f"<tr><td>{html_escape(kw)}</td><td>{html_escape(label)}</td>"
                f"<td>{html_escape(r['model_name'])}</td>"
                f"<td>{_pill('ok')}</td>"
                f"<td class='{mcls}'>{m}</td><td class='{ccls}'>{c}</td></tr>"
            )

    # detail excerpts
    details_html = []
    for res in all_results:
        for llm, r in res["results"].items():
            if r["status"] != "ok":
                continue
            label = HUMAN_LABELS.get(llm, llm)
            excerpt = r["answer_excerpt"] or "(no text)"
            sources = ", ".join(r["cited_sources"]) if r["cited_sources"] else "none"
            details_html.append(
                f"<div class='card'>"
                f"<h3>{html_escape(label)} — {html_escape(res['keyword'])}</h3>"
                f"<div class='small'>Model: {html_escape(r['model_name'])} · "
                f"Mentioned: {'yes' if r['mentioned'] else 'no'} · "
                f"Cited: {'yes' if r['cited'] else 'no'}</div>"
                f"<div class='excerpt'>{html_escape(excerpt)}</div>"
                f"<div class='small'>Cited sources: {html_escape(sources)}</div>"
                f"</div>"
            )

    # metrics section
    if metrics["status"] == "ok" and metrics["aggregated_metrics"]:
        am = metrics["aggregated_metrics"]
        mentions = am.get("mentions", "n/a")
        sources_rows = ""
        for s in (metrics["sources_domain"] or [])[:15]:
            if isinstance(s, dict):
                sources_rows += (
                    f"<tr><td>{html_escape(str(s.get('key','')))}</td>"
                    f"<td>{html_escape(str(s.get('mentions','')))}</td></tr>"
                )
        metrics_html = f"""
        <div class='card'>
          <h3>Aggregated Metrics (platform: {html_escape(metrics['platform'])})</h3>
          <p>Total mentions: <strong>{html_escape(str(mentions))}</strong></p>
          <table><thead><tr><th>Cited Domain</th><th>Mentions</th></tr></thead>
          <tbody>{sources_rows or '<tr><td colspan=2 class=na>No source domains</td></tr>'}</tbody>
          </table>
        </div>"""
    else:
        metrics_html = (
            f"<div class='card'><p class='small'>No aggregated mention metrics "
            f"available for {html_escape(bd)} (platform: {html_escape(metrics['platform'])}, "
            f"status: {html_escape(metrics['status'])}).</p></div>"
        )

    # summary stats
    total_cells = sum(r["summary"]["llms_tested"] for r in all_results)
    total_mentions = sum(r["summary"]["mentions"] for r in all_results)
    total_citations = sum(r["summary"]["citations"] for r in all_results)
    mention_rate = (total_mentions / total_cells * 100) if total_cells else 0
    citation_rate = (total_citations / total_cells * 100) if total_cells else 0

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Visibility Report — {html_escape(bd)}</title>
<style>{_WHITE_SAAS_CSS}</style>
</head>
<body>
<div class="container">
  <h1>AI Visibility Report</h1>
  <div class="meta">Brand: <strong>{html_escape(bd)}</strong> · Generated: {ts}</div>

  <h2>Summary</h2>
  <div class="card">
    <table>
      <tr><td><strong>Keywords tested</strong></td><td>{len(keywords)}</td></tr>
      <tr><td><strong>LLM responses</strong></td><td>{total_cells}</td></tr>
      <tr><td><strong>Mentions</strong></td><td>{total_mentions} ({mention_rate:.1f}%)</td></tr>
      <tr><td><strong>Citations</strong></td><td>{total_citations} ({citation_rate:.1f}%)</td></tr>
    </table>
  </div>

  <h2>Visibility Matrix</h2>
  <table>
    <thead><tr><th>Keyword</th><th>LLM</th><th>Model</th><th>Status</th><th>Mentioned</th><th>Cited</th></tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>

  <h2>Mention Metrics (DataForSEO Dataset)</h2>
  {metrics_html}

  <h2>Response Details</h2>
  {''.join(details_html) if details_html else '<p class=small>No successful responses to show.</p>'}

  <div class="footer">
    Generated by ai_visibility_helper.py · DataForSEO AI Optimization API ·
    White SaaS theme
  </div>
</div>
</body>
</html>"""

    html_path = os.path.join(output_path, f"{bd}_ai_visibility.html")
    json_path = os.path.join(output_path, f"{bd}_ai_visibility.json")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "brand_domain": bd,
                "generated_at": ts,
                "keywords": keywords,
                "results": all_results,
                "metrics": metrics,
                "summary": {
                    "total_cells": total_cells,
                    "total_mentions": total_mentions,
                    "total_citations": total_citations,
                    "mention_rate": round(mention_rate, 1),
                    "citation_rate": round(citation_rate, 1),
                },
            },
            f,
            indent=2,
        )
    return html_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test brand visibility across AI engines via DataForSEO."
    )
    parser.add_argument("--brand", required=True, help="Brand domain, e.g. example.com")
    parser.add_argument(
        "--keyword", action="append", required=True,
        help="Keyword to test (repeatable for multiple keywords)",
    )
    parser.add_argument(
        "--output", default="/tmp/ai_visibility_report",
        help="Output directory for the HTML report",
    )
    parser.add_argument(
        "--metrics", action="store_true",
        help="Only fetch pre-aggregated mention metrics for the brand",
    )
    parser.add_argument(
        "--platform", default="chat_gpt", choices=["chat_gpt", "google"],
        help="Platform for mention metrics (default: chat_gpt)",
    )
    parser.add_argument(
        "--llm", action="append", dest="llm_types",
        help="LLM type to test (repeatable). Default: chat_gpt google perplexity claude",
    )
    args = parser.parse_args()

    if args.metrics:
        m = get_mention_metrics(args.brand, platform=args.platform)
        print(json.dumps(m, indent=2))
        return

    llm_types = args.llm_types or DEFAULT_LLM_TYPES
    if len(args.keyword) == 1 and not args.llm_types:
        # single keyword quick mode
        res = test_brand_visibility(args.keyword[0], args.brand, llm_types=llm_types)
        print(json.dumps(res, indent=2))

    report = generate_visibility_report(
        args.brand, args.keyword, args.output
    )
    print(f"\nReport written: {report}")


if __name__ == "__main__":
    main()
