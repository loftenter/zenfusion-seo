#!/usr/bin/env python3
"""
Keyword Cannibalization Detector — standalone Python script.

Detects true keyword cannibalization using DataForSEO live + historical SERP data.
For each keyword, fetches the live Google SERP (depth 100) and historical SERP snapshots,
unions all URLs from the target domain across snapshots, and classifies whether
multiple same-domain pages are genuinely competing (same intent) or just co-listed
(different page types — e.g., homepage + product page).

Decision logic ported from the keyword-cannibalization-detector skill's engine
(scripts/cannibalization.py): over-time union, page-type classification, rotation
detection, CTR-based value-at-risk, and verdict matrix.

Usage:
  python3 cannibalization_detector.py --domain softprowatersystems.com \
      --keywords 'best water softener,best water softener system' \
      --output /tmp/cannibalization_test

Outputs:
  - <output>/cannibalization_report.html  (white SaaS theme HTML report)
  - <output>/cannibalization_worklist.csv  (actionable CSV worklist)
  - console summary

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD — in ~/.hermes/.env or env vars
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import os
import sys
import json
import csv
import base64
import fnmatch
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from urllib.parse import urlsplit

# ── CTR curve (aggregate desktop organic) ──────────────────────────────────────
CTR_TABLE = {
    1: 0.281, 2: 0.152, 3: 0.099, 4: 0.070, 5: 0.053,
    6: 0.041, 7: 0.033, 8: 0.028, 9: 0.024, 10: 0.021,
    11: 0.019, 12: 0.017, 13: 0.015, 14: 0.014, 15: 0.013,
    16: 0.012, 17: 0.011, 18: 0.0105, 19: 0.010, 20: 0.0095,
}

# ── Thresholds ──────────────────────────────────────────────────────────────────
DEEP_POS = 40
SOFT_INTENT = 0.80
BAND = {"COMMERCIAL": 30, "INFORMATIONAL": 20}

COMMERCIAL_SLUGS = (
    "/product", "/products", "/p/", "/pd/", "/item", "/shop", "/store", "/category",
    "/categories", "/collection", "/collections", "/c/", "/service", "/services",
    "/solutions", "/pricing", "/plans", "/buy", "/order", "/apis", "/api",
)
INFO_SLUGS = (
    "/blog", "/news", "/article", "/articles", "/post", "/posts", "/guide", "/guides",
    "/how-to", "/howto", "/learn", "/resources", "/tips", "/faq", "/help", "/docs",
    "/glossary", "/wiki", "/magazine", "/stories",
)

SEVERITY = {"strong candidate": 1.0, "investigate": 0.55, "harmless overlap": 0.10}


# ── Credential management ──────────────────────────────────────────────────────

def get_creds():
    """Load DataForSEO credentials from env vars or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        env_path = os.path.expanduser('~/.hermes/.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATAFORSEO_LOGIN='):
                        login = line.split('=', 1)[1].strip()
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1].strip()
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.")
        print("Set them in ~/.hermes/.env or as environment variables.")
        sys.exit(1)
    return login, password


def api_call(endpoint, payload):
    """POST to a DataForSEO v3 endpoint. Returns parsed JSON or None."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'https://api.dataforseo.com/v3/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = _fix_goto(json.loads(resp.read()), endpoint)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ''
        print(f"  API HTTP {e.code} on {endpoint}: {err_body[:200]}")
        return None
    except Exception as e:
        print(f"  API request error on {endpoint}: {e}")
        return None
    if body.get('status_code') != 20000:
        print(f"  API error {body.get('status_code')}: {body.get('status_message', '')}")
        # Check tasks for per-task errors
        for task in body.get('tasks', []):
            if task.get('status_code') not in (20000, None):
                print(f"    task: {task.get('status_code')} - {task.get('status_message', '')}")
        return None
    return body


# ── URL / domain helpers ───────────────────────────────────────────────────────

def normalize_host(host):
    host = (host or "").lower().strip()
    return host[4:] if host.startswith("www.") else host


def host_of(url):
    try:
        return normalize_host(urlsplit(url if "//" in url else "http://" + url).netloc)
    except Exception:
        return ""


def url_key(url):
    try:
        parts = urlsplit(url if "//" in url else "http://" + url)
        host = normalize_host(parts.netloc)
        path = (parts.path or "/").rstrip("/") or "/"
        return f"{host}{path}"
    except Exception:
        return (url or "").lower().rstrip("/")


def domain_matches(url, domain, include_subdomains=False):
    host = normalize_host(host_of(url))
    dom = normalize_host(domain)
    return host == dom or (include_subdomains and host.endswith("." + dom))


# ── Page-type classification ───────────────────────────────────────────────────

def classify_page_type(url, url_map=None, signals=None):
    """Return 'commercial', 'informational', or 'ambiguous'."""
    path = "/" + (urlsplit(url if "//" in url else "http://" + url).path or "").lstrip("/")
    path_l = path.lower()
    if url_map:
        for pattern, ptype in url_map.items():
            pat = pattern.lower()
            if fnmatch.fnmatch(path_l, pat) or fnmatch.fnmatch(url.lower(), pat) or path_l.startswith(pat.rstrip("*")):
                return ptype
    signals = signals or {}
    if signals.get("price") or signals.get("rating") or signals.get("is_shop"):
        return "commercial"
    crumb = signals.get("breadcrumb")
    crumb = " ".join(map(str, crumb)).lower() if isinstance(crumb, list) else str(crumb or "").lower()
    if crumb:
        if any(s in crumb for s in ("shop", "products", "product", "category", "collections", "pricing")):
            return "commercial"
        if any(s in crumb for s in ("blog", "news", "guide", "articles", "resources")):
            return "informational"
    if path_l in ("/", ""):
        return "commercial"
    comm = any(s in path_l for s in COMMERCIAL_SLUGS)
    info = any(s in path_l for s in INFO_SLUGS)
    if comm and not info:
        return "commercial"
    if info and not comm:
        return "informational"
    return "ambiguous"


# ── SERP extraction ───────────────────────────────────────────────────────────

def extract_domain_urls(serp_items, domain, include_subdomains=False, url_map=None):
    """Extract the domain's organic results from a SERP items list.
    Returns deduped [{url, position, type, signals}] sorted by position."""
    best = {}
    for it in serp_items or []:
        if it.get("type") not in ("organic", "featured_snippet"):
            continue
        url = it.get("url") or ""
        if not url or not domain_matches(url, domain, include_subdomains):
            continue
        pos = it.get("rank_group")
        if pos is None:
            pos = it.get("rank_absolute")
        if pos is None:
            continue
        k = url_key(url)
        signals = {
            "price": it.get("price"),
            "rating": it.get("rating"),
            "breadcrumb": it.get("breadcrumb"),
            "is_shop": it.get("is_shop") or it.get("shop"),
        }
        rec = {"url": url, "position": int(pos), "signals": signals}
        if k not in best or rec["position"] < best[k]["position"]:
            if k in best and not any(signals.values()) and any(best[k]["signals"].values()):
                rec["signals"] = best[k]["signals"]
            best[k] = rec
    urls = sorted(best.values(), key=lambda r: r["position"])
    for r in urls:
        r["type"] = classify_page_type(r["url"], url_map=url_map, signals=r["signals"])
    return urls


# ── CTR ───────────────────────────────────────────────────────────────────────

def ctr(pos):
    if pos is None or pos < 1:
        return 0.0
    pos = int(pos)
    if pos in CTR_TABLE:
        return CTR_TABLE[pos]
    if pos <= 30:
        return round(0.0095 - (pos - 20) * 0.00033, 5)
    if pos <= 50:
        return round(0.0062 - (pos - 30) * 0.00013, 5)
    if pos <= 100:
        return round(max(0.0035 - (pos - 50) * 0.00005, 0.0010), 5)
    return 0.0008


# ── Intent helpers ────────────────────────────────────────────────────────────

def bucket_intent(label):
    return "COMMERCIAL" if (label or "").lower() in ("commercial", "transactional") else "INFORMATIONAL"


def effective_intent(label, probability, page_types):
    base = bucket_intent(label)
    if base == "COMMERCIAL":
        return base, False
    if any(t == "commercial" for t in page_types) and (probability is None or probability < SOFT_INTENT):
        return "COMMERCIAL", True
    return base, False


# ── Core API calls ────────────────────────────────────────────────────────────

def fetch_live_serp(keyword, location_code=2840, language_code='en'):
    """Fetch live organic SERP (depth 100) for a keyword."""
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'device': 'desktop',
        'os': 'windows',
        'depth': 100,
        'use_improved_etv': True,
    }]
    data = api_call('serp/google/organic/live/advanced', payload)
    if not data or not data.get('tasks'):
        return None
    task = data['tasks'][0]
    if task.get('status_code') not in (20000, None):
        print(f"  SERP task error for '{keyword}': {task.get('status_code')} - {task.get('status_message', '')}")
        return None
    result = task.get('result', [{}])[0] if task.get('result') else {}
    items = result.get('items', [])
    # Extract search volume / cpc if present in keyword_info
    kw_info = result.get('keyword_info', {})
    return {
        'items': items,
        'search_volume': kw_info.get('search_volume'),
        'cpc': kw_info.get('cpc'),
    }


def fetch_historical_serps(keyword, location_code=2840, language_code='en', months=12):
    """Fetch historical SERP snapshots for a keyword."""
    date_to = datetime.now().strftime('%Y-%m-%d')
    date_from = (datetime.now() - timedelta(days=months * 30)).strftime('%Y-%m-%d')
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'date_from': date_from,
        'date_to': date_to,
    }]
    data = api_call('dataforseo_labs/google/historical_serps/live', payload)
    if not data or not data.get('tasks'):
        return []
    task = data['tasks'][0]
    if task.get('status_code') not in (20000, None):
        print(f"  Historical SERPs error for '{keyword}': {task.get('status_code')} - {task.get('status_message', '')}")
        return []
    result = task.get('result', [{}])[0] if task.get('result') else {}
    snapshots = result.get('items', [])
    observations = []
    for snap in snapshots:
        date = snap.get('datetime', '')
        snap_items = snap.get('items', [])
        observations.append({'date': date, 'items': snap_items})
    return observations


def fetch_search_volume(keywords, location_code=2840, language_code='en'):
    """Batch fetch search volume + CPC for a list of keywords."""
    payload = [{
        'keywords': keywords,
        'location_code': location_code,
        'language_code': language_code,
    }]
    data = api_call('keywords_data/google_ads/search_volume/live', payload)
    if not data or not data.get('tasks'):
        return {}
    task = data['tasks'][0]
    if task.get('status_code') not in (20000, None):
        print(f"  Search volume error: {task.get('status_code')} - {task.get('status_message', '')}")
        return {}
    result = task.get('result', [])
    vol_map = {}
    for item in result:
        kw = item.get('keyword', '')
        vol_map[kw] = {
            'search_volume': item.get('search_volume'),
            'cpc': item.get('cpc'),
        }
    return vol_map


def fetch_search_intent(keywords, language_code='en'):
    """Batch fetch search intent labels for a list of keywords."""
    payload = [{
        'keywords': keywords,
        'language_code': language_code,
    }]
    data = api_call('dataforseo_labs/google/search_intent/live', payload)
    if not data or not data.get('tasks'):
        return {}
    task = data['tasks'][0]
    if task.get('status_code') not in (20000, None):
        print(f"  Search intent error: {task.get('status_code')} - {task.get('status_message', '')}")
        return {}
    result = task.get('result', [])
    intent_map = {}
    for item in result:
        kw = item.get('keyword', '')
        ki = item.get('keyword_intent', {})
        intent_map[kw] = {
            'label': ki.get('label'),
            'probability': ki.get('probability'),
        }
    return intent_map


# ── GSC integration ───────────────────────────────────────────────────────────

def load_gsc_data(gsc_path='/tmp/softpro_gsc_all.json'):
    """Load Google Search Console data if available.
    Returns dict: keyword -> {page: {impressions, clicks, position, ctr}}"""
    if not gsc_path or not os.path.exists(gsc_path):
        return {}
    try:
        with open(gsc_path) as f:
            raw = json.load(f)
    except Exception as e:
        print(f"  GSC data load error: {e}")
        return {}
    gsc_map = {}
    # Handle various GSC JSON shapes
    rows = raw if isinstance(raw, list) else raw.get('rows', raw.get('data', []))
    for row in rows:
        if isinstance(row, dict):
            keys = row.get('keys', [])
            if len(keys) >= 2:
                query, page = keys[0], keys[1]
            else:
                query = row.get('query', row.get('query_', ''))
                page = row.get('page', row.get('url', ''))
            if not query:
                continue
            key = query.lower().strip()
            if key not in gsc_map:
                gsc_map[key] = {}
            gsc_map[key][page] = {
                'impressions': row.get('impressions', 0),
                'clicks': row.get('clicks', 0),
                'position': row.get('position', 0),
                'ctr': row.get('ctr', 0),
            }
    return gsc_map


def cross_reference_gsc(keyword, gsc_data):
    """Return GSC metrics for a keyword if available."""
    if not gsc_data:
        return None
    return gsc_data.get(keyword.lower().strip())


# ── Function 1: detect_cannibalization ────────────────────────────────────────

def detect_cannibalization(domain, keywords, location_code=2840, include_subdomains=False,
                           language_code='en', url_map=None, gsc_path=None):
    """
    For each keyword, fetch live SERP + historical SERPs, extract domain URLs,
    and return per-keyword observations + conflicts.

    Returns:
      {
        'domain': domain,
        'keywords_checked': N,
        'keywords_ranking': N,
        'conflicts': [ {keyword, urls_ranking, positions, observations, ...} ],
        'clean': [ ... ],
        'not_ranking': [ ... ],
      }
    """
    domain = normalize_host(domain)
    # Dedupe keywords
    seen = set()
    unique_kw = []
    for kw in keywords:
        k = kw.strip().lower()
        if k and k not in seen:
            seen.add(k)
            unique_kw.append(kw.strip())
    print(f"\n{'='*60}")
    print(f"CANNIBALIZATION DETECTION — {domain}")
    print(f"Keywords: {len(unique_kw)} (after dedupe) | Location: {location_code} | Lang: {language_code}")
    print(f"{'='*60}\n")

    # Load GSC data
    gsc_data = load_gsc_data(gsc_path) if gsc_path else load_gsc_data()

    # Batch enrich: search volume + intent
    print("Fetching search volume + CPC (batched)...")
    vol_map = fetch_search_volume(unique_kw, location_code, language_code)
    print("Fetching search intent (batched)...")
    intent_map = fetch_search_intent(unique_kw, language_code)

    conflicts = []
    clean = []
    not_ranking = []

    for i, kw in enumerate(unique_kw, 1):
        print(f"\n[{i}/{len(unique_kw)}] '{kw}'")

        # Live SERP
        live_data = fetch_live_serp(kw, location_code, language_code)
        if live_data is None:
            print(f"  Live SERP failed — skipping.")
            not_ranking.append({'keyword': kw, 'reason': 'SERP fetch failed'})
            continue

        live_urls = extract_domain_urls(live_data['items'], domain, include_subdomains, url_map)
        if not live_urls:
            print(f"  Domain not in live top-100 — not ranking.")
            not_ranking.append({'keyword': kw, 'reason': 'not in live top-100'})
            continue

        print(f"  Live: {len(live_urls)} domain URL(s) — best #{live_urls[0]['position']}")

        # Historical SERPs
        historical = fetch_historical_serps(kw, location_code, language_code)
        hist_observations = []
        for snap in historical:
            snap_urls = extract_domain_urls(snap['items'], domain, include_subdomains, url_map)
            if snap_urls:
                hist_observations.append({'date': snap['date'], 'urls': snap_urls})

        if hist_observations:
            print(f"  Historical: {len(hist_observations)} snapshot(s) with domain URLs")

        # Build observations list
        observations = [{'date': 'live', 'urls': live_urls}] + hist_observations

        # Enrichment
        vol_info = vol_map.get(kw, {})
        # Prefer SERP-embedded volume if volume API didn't return it
        search_volume = vol_info.get('search_volume') or live_data.get('search_volume')
        cpc = vol_info.get('cpc') or live_data.get('cpc')
        intent_info = intent_map.get(kw, {})
        intent_label = intent_info.get('label', 'informational')
        intent_probability = intent_info.get('probability')

        # GSC cross-reference
        gsc_metrics = cross_reference_gsc(kw, gsc_data)

        # Classify
        result = classify_conflict(
            kw, live_urls, observations,
            intent_label, intent_probability,
            search_volume, cpc, url_map, gsc_metrics
        )

        if result['n_pages'] >= 2:
            result['value_at_risk'] = calculate_value_at_risk(
                kw, [u['position'] for u in result['urls_ranking']], search_volume, cpc
            )
            conflicts.append(result)
            print(f"  CONFLICT: {result['n_pages']} pages, verdict={result['verdict']}, "
                  f"rotating={result['rotating']}, val_at_risk=${result['value_at_risk']:.2f}/mo")
        else:
            clean.append(result)
            print(f"  Clean — single page ranking.")

    # Sort conflicts by value at risk (descending)
    conflicts.sort(key=lambda c: c.get('value_at_risk', 0), reverse=True)
    for idx, c in enumerate(conflicts, 1):
        c['priority_rank'] = idx

    result = {
        'domain': domain,
        'keywords_checked': len(unique_kw),
        'keywords_ranking': len(conflicts) + len(clean),
        'conflicts': conflicts,
        'clean': clean,
        'not_ranking': not_ranking,
        'gsc_available': bool(gsc_data),
    }
    return result


# ── Function 2: classify_conflict ────────────────────────────────────────────

def classify_conflict(keyword, urls_ranking, observations=None,
                      intent_label='informational', intent_probability=None,
                      search_volume=None, cpc=None, url_map=None, gsc_metrics=None):
    """
    Determine if the domain has true cannibalization (same-intent pages competing)
    or normal co-listing (different page types).

    Uses the over-time union + rotation model from the skill's decision matrix:
    - Union all URLs across snapshots
    - rotation_count = distinct URLs that held the top domain slot across dates
    - Verdict based on n_pages, best_pos, rotation, page types, intent
    """
    observations = observations or [{'date': 'live', 'urls': urls_ranking}]

    # Union all pages across observations
    pages = {}
    top_by_date = {}
    for obs in observations:
        date = obs.get('date', 'live')
        urls = sorted(obs.get('urls', []), key=lambda u: u['position'])
        if not urls:
            continue
        top_by_date[date] = url_key(urls[0]['url'])
        for u in urls:
            k = url_key(u['url'])
            p = pages.get(k)
            if p is None:
                pages[k] = {
                    'url': u['url'],
                    'type': u.get('type') or classify_page_type(u['url'], url_map=url_map, signals=u.get('signals')),
                    'best_pos': u['position'],
                    'positions': [(date, u['position'])],
                    'dates': {date},
                }
            else:
                p['best_pos'] = min(p['best_pos'], u['position'])
                p['positions'].append((date, u['position']))
                p['dates'].add(date)
                if not p['type'] and u.get('type'):
                    p['type'] = u['type']

    n_dates = len({o.get('date', 'live') for o in observations})
    n_pages = len(pages)

    if n_pages < 2:
        return {
            'keyword': keyword,
            'n_pages': n_pages,
            'n_snapshots': n_dates,
            'urls_ranking': [{'url': p['url'], 'position': p['best_pos'], 'type': p['type'],
                              'dates': sorted(p['dates'])} for p in pages.values()],
            'verdict': 'clean',
            'rotating': False,
            'rotation_count': 0,
            'best_pos': min((p['best_pos'] for p in pages.values()), default=None),
            'reason': 'Only one page ranks — no competition.',
            'recommendation': 'No action needed.',
            'intent_label': intent_label,
            'intent_probability': intent_probability,
            'search_volume': search_volume,
            'cpc': cpc,
            'gsc_metrics': gsc_metrics,
        }

    ordered = sorted(pages.values(), key=lambda p: p['best_pos'])
    primary, secondary = ordered[0], ordered[1]
    best_pos = primary['best_pos']
    rotation_count = len(set(top_by_date.values()))
    page_types = [primary.get('type'), secondary.get('type')]

    eint, overridden = effective_intent(intent_label, intent_probability, page_types)
    verdict = _verdict(eint, best_pos, n_pages, rotation_count, page_types)

    result = {
        'keyword': keyword,
        'n_pages': n_pages,
        'n_snapshots': n_dates,
        'rotation_count': rotation_count,
        'rotating': rotation_count >= 2,
        'best_pos': best_pos,
        'urls_ranking': [
            {'url': p['url'], 'position': p['best_pos'], 'type': p['type'],
             'dates': sorted(p['dates']) if isinstance(p['dates'], set) else p['dates']}
            for p in ordered
        ],
        'pair': {
            'stronger': {'url': primary['url'], 'position': primary['best_pos'], 'type': primary['type']},
            'weaker': {'url': secondary['url'], 'position': secondary['best_pos'], 'type': secondary['type']},
        },
        'verdict': verdict,
        'intent_label': intent_label,
        'intent_probability': intent_probability,
        'effective_intent': eint.lower(),
        'intent_overridden': overridden,
        'search_volume': search_volume,
        'cpc': cpc,
        'gsc_metrics': gsc_metrics,
    }
    result['reason'] = _reason(result)
    result['recommendation'] = _recommendation(result)
    return result


def _verdict(eint, best_pos, n_pages, rotation_count, page_types):
    if n_pages < 2:
        return "harmless overlap"
    if best_pos > DEEP_POS:
        return "harmless overlap"
    rotating = rotation_count >= 2
    band = BAND[eint]
    if best_pos <= band:
        v = "strong candidate" if rotating else "investigate"
    else:
        v = "investigate" if (rotating or eint == "COMMERCIAL") else "harmless overlap"
    if v == "strong candidate" and eint == "INFORMATIONAL" and _mixed_types(page_types):
        v = "investigate"
    return v


def _mixed_types(types):
    return ("commercial" in types) and ("informational" in types)


def _reason(r):
    n, best = r['n_pages'], r['best_pos']
    val = f"${r.get('value_at_risk', 0):.2f}/mo" if r.get('value_at_risk') else None
    car = r.get('clicks_at_risk')
    ovr = " (re-classified commercial from ranking page type)" if r.get('intent_overridden') else ""
    if r['verdict'] == 'harmless overlap':
        if best and best > DEEP_POS:
            return (f"The domain's best page only reaches #{best} — too deep to earn "
                    f"meaningful clicks, so competing pages aren't costing anything.")
        return (f"The domain fields {n} pages here but Google keeps one at #{best} "
                f"without swapping — authority isn't being split.")
    rot = (f"Google rotates between {r['rotation_count']} of them across {r['n_snapshots']} snapshots"
           if r['rotating'] else f"they co-rank across {r['n_snapshots']} snapshots")
    valpart = f" ~{car} clicks/mo" + (f" (~{val})" if val else "") + " at risk." if car else "."
    return (f"{n} of the domain's own pages compete for this {r['effective_intent']} query{ovr}; "
            f"{rot}; best position #{best}.{valpart}")


def _recommendation(r):
    if r['verdict'] == 'harmless overlap':
        return f"No action — one page holds #{r['best_pos']} without rotation, or all pages sit too deep."
    s, w = r['pair']['stronger'], r['pair']['weaker']
    ts, tw = s.get('type'), w.get('type')
    su = _short_url(s['url'])
    wu = _short_url(w['url'])
    if ts == 'commercial' and tw == 'commercial':
        return (f"Consolidate: merge the weaker page {wu} (#{w['position']}) into the stronger "
                f"{su} (#{s['position']}) and 301-redirect it — one authoritative page outranks rotating ones.")
    if ts == 'informational' and tw == 'informational':
        return (f"Differentiate the two articles so each owns a distinct query, or consolidate "
                f"into one guide and 301 the weaker {wu}.")
    if _mixed_types([ts, tw]):
        comm = su if ts == 'commercial' else wu
        info = wu if ts == 'commercial' else su
        return (f"Different intents — keep both: canonicalize / de-optimize the informational page {info} "
                f"for this term and point internal links + canonical at the commercial page {comm}. "
                f"Do NOT merge them.")
    return (f"Clarify page roles: decide which of {su} (#{s['position']}) and {wu} (#{w['position']}) "
            f"should own this term, then differentiate or consolidate accordingly.")


def _short_url(url):
    p = urlsplit(url if "//" in url else "http://" + url)
    return (host_of(url) + (p.path or "/")).rstrip("/") or host_of(url)


# ── Function 3: calculate_value_at_risk ────────────────────────────────────────

def calculate_value_at_risk(keyword, positions, search_volume, cpc):
    """
    Estimate clicks and value lost due to cannibalization.

    value_at_risk = clicks_at_risk × CPC
    clicks_at_risk = CTR(best_pos) × volume × fragmentation
    fragmentation = (n_pages - 1) / n_pages
    """
    if not positions or not search_volume:
        return 0.0
    n_pages = len(positions)
    if n_pages < 2:
        return 0.0
    best_pos = min(positions)
    frag = (n_pages - 1) / n_pages
    clicks_at_risk = round(ctr(best_pos) * search_volume * frag)
    if not cpc:
        return 0.0
    value = round(clicks_at_risk * cpc, 2)
    return value


# ── Function 4: generate_cannibalization_report ────────────────────────────────

def generate_cannibalization_report(domain, conflicts, output_path):
    """
    Generate an HTML report with a conflict table, verdicts, and fix recommendations.
    White SaaS theme. Also writes a CSV worklist.
    """
    os.makedirs(output_path, exist_ok=True)
    domain_clean = normalize_host(domain)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Write CSV worklist
    csv_path = os.path.join(output_path, 'cannibalization_worklist.csv')
    _write_csv(conflicts, csv_path)

    # Build HTML
    html = _build_html_report(domain_clean, conflicts, timestamp)
    html_path = os.path.join(output_path, 'cannibalization_report.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return html_path, csv_path


def _write_csv(conflicts, path):
    header = [
        'priority', 'keyword', 'verdict', 'n_pages', 'rotating', 'rotation_count',
        'best_pos', 'search_volume', 'cpc', 'clicks_at_risk', 'value_at_risk',
        'effective_intent', 'intent_label', 'page_a_url', 'page_a_position', 'page_a_type',
        'page_b_url', 'page_b_position', 'page_b_type',
        'all_urls', 'reason', 'recommendation',
    ]
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        for c in conflicts:
            s = c.get('pair', {}).get('stronger', {})
            wk = c.get('pair', {}).get('weaker', {})
            all_urls = ' | '.join(f"{u['url']}@{u['position']}" for u in c.get('urls_ranking', []))
            w.writerow([
                c.get('priority_rank', ''),
                c['keyword'],
                c['verdict'],
                c['n_pages'],
                c.get('rotating', False),
                c.get('rotation_count', 0),
                c.get('best_pos', ''),
                c.get('search_volume', ''),
                c.get('cpc', ''),
                c.get('clicks_at_risk', ''),
                c.get('value_at_risk', ''),
                c.get('effective_intent', ''),
                c.get('intent_label', ''),
                s.get('url', ''), s.get('position', ''), s.get('type', ''),
                wk.get('url', ''), wk.get('position', ''), wk.get('type', ''),
                all_urls,
                c.get('reason', ''),
                c.get('recommendation', ''),
            ])


def _verdict_badge(verdict):
    colors = {
        'strong candidate': '#ef4444',
        'investigate': '#f59e0b',
        'harmless overlap': '#10b981',
        'clean': '#10b981',
    }
    color = colors.get(verdict, '#6b7280')
    return f'<span class="badge" style="background:{color}">{verdict}</span>'


def _build_html_report(domain, conflicts, timestamp):
    strong_count = sum(1 for c in conflicts if c['verdict'] == 'strong candidate')
    investigate_count = sum(1 for c in conflicts if c['verdict'] == 'investigate')
    harmless_count = sum(1 for c in conflicts if c['verdict'] == 'harmless overlap')

    # Conflict table rows
    rows_html = []
    for c in conflicts:
        s = c.get('pair', {}).get('stronger', {})
        wk = c.get('pair', {}).get('weaker', {})
        val = c.get('value_at_risk', 0)
        val_str = f"${val:,.2f}/mo" if val else "—"
        gsc = c.get('gsc_metrics')
        gsc_html = ""
        if gsc:
            total_imp = sum(v.get('impressions', 0) for v in gsc.values())
            total_clk = sum(v.get('clicks', 0) for v in gsc.values())
            gsc_html = f'<td>{total_imp:,} imp / {total_clk:,} clicks</td>'
        else:
            gsc_html = '<td class="muted">N/A</td>'

        urls_cell = ""
        for u in c.get('urls_ranking', []):
            u_type = u.get('type', 'ambiguous')
            urls_cell += (f'<div class="url-row"><span class="pos">#{u["position"]}</span> '
                          f'<span class="ptype ptype-{u_type}">{u_type}</span> '
                          f'<a href="{u["url"]}" target="_blank">{_escape(u["url"])}</a></div>')

        rows_html.append(f"""
        <tr>
          <td class="priority">{c.get('priority_rank', '')}</td>
          <td class="keyword">{_escape(c['keyword'])}</td>
          <td>{_verdict_badge(c['verdict'])}</td>
          <td>{c['n_pages']}</td>
          <td>{"🔄 Yes" if c.get('rotating') else "No"}</td>
          <td>#{c.get('best_pos', '?')}</td>
          <td>{c.get('search_volume', '—'):,}</td>
          <td>{val_str}</td>
          {gsc_html}
          <td class="urls-cell">{urls_cell}</td>
          <td class="reason-cell">{_escape(c.get('reason', ''))}<br><span class="fix">{_escape(c.get('recommendation', ''))}</span></td>
        </tr>""")

    rows_html_str = '\n'.join(rows_html) if rows_html else '<tr><td colspan="10" class="empty">No conflicts detected — all keywords field a single page.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cannibalization Report — {domain}</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #3b82f6;
    --green: #10b981; --amber: #f59e0b; --red: #ef4444;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    padding: 24px; max-width: 1400px; margin: 0 auto;
  }}
  h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.875rem; margin-bottom: 24px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
  .stat-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; text-align: center;
  }}
  .stat-card .num {{ font-size: 2rem; font-weight: 700; }}
  .stat-card .label {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }}
  .stat-card.strong .num {{ color: var(--red); }}
  .stat-card.investigate .num {{ color: var(--amber); }}
  .stat-card.harmless .num {{ color: var(--green); }}
  table {{
    width: 100%; border-collapse: collapse; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    font-size: 0.875rem;
  }}
  th {{
    background: #f1f5f9; padding: 12px 14px; text-align: left; font-weight: 600;
    font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap;
  }}
  td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: #f8fafc; }}
  .badge {{
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 0.75rem; font-weight: 600; color: #fff; white-space: nowrap;
  }}
  .keyword {{ font-weight: 600; white-space: nowrap; }}
  .priority {{ text-align: center; font-weight: 700; color: var(--accent); }}
  .muted {{ color: var(--muted); }}
  .empty {{ text-align: center; padding: 40px; color: var(--muted); }}
  .url-row {{ margin-bottom: 6px; font-size: 0.8rem; }}
  .url-row:last-child {{ margin-bottom: 0; }}
  .pos {{
    display: inline-block; min-width: 28px; text-align: center;
    background: #f1f5f9; border-radius: 4px; padding: 1px 5px; font-weight: 600;
    font-size: 0.75rem; margin-right: 4px;
  }}
  .ptype {{
    display: inline-block; padding: 1px 6px; border-radius: 4px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase; margin-right: 4px;
  }}
  .ptype-commercial {{ background: #dbeafe; color: #1e40af; }}
  .ptype-informational {{ background: #d1fae5; color: #065f46; }}
  .ptype-ambiguous {{ background: #f3f4f6; color: #6b7280; }}
  .reason-cell {{ max-width: 320px; font-size: 0.8rem; }}
  .reason-cell .fix {{
    display: block; margin-top: 6px; padding-top: 6px;
    border-top: 1px dashed var(--border); color: var(--accent); font-weight: 500;
  }}
  .urls-cell {{ max-width: 280px; }}
  .urls-cell a {{ color: var(--accent); text-decoration: none; }}
  .urls-cell a:hover {{ text-decoration: underline; }}
  .footer {{
    margin-top: 24px; text-align: center; font-size: 0.75rem; color: var(--muted);
  }}
  .section-title {{
    font-size: 1.125rem; font-weight: 700; margin-bottom: 12px;
  }}
  .legend {{
    display: flex; gap: 16px; margin-bottom: 16px; font-size: 0.8rem; color: var(--muted);
  }}
</style>
</head>
<body>
  <h1>🔑 Keyword Cannibalization Report</h1>
  <p class="subtitle">{domain} · Generated {timestamp} · Data: DataForSEO SERP + Historical</p>

  <div class="stats-grid">
    <div class="stat-card"><div class="num">{len(conflicts)}</div><div class="label">Total Conflicts</div></div>
    <div class="stat-card strong"><div class="num">{strong_count}</div><div class="label">Strong Candidates</div></div>
    <div class="stat-card investigate"><div class="num">{investigate_count}</div><div class="label">Investigate</div></div>
    <div class="stat-card harmless"><div class="num">{harmless_count}</div><div class="label">Harmless Overlap</div></div>
  </div>

  <div class="section-title">Conflict Priority Table (by value at risk)</div>
  <div class="legend">
    <span><span class="badge" style="background:#ef4444">strong candidate</span> Google rotates competing pages at a high position — active authority split</span>
    <span><span class="badge" style="background:#f59e0b">investigate</span> Multiple pages competing but rotation unclear or positions deeper</span>
    <span><span class="badge" style="background:#10b981">harmless overlap</span> Co-listed but not splitting authority</span>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>Keyword</th><th>Verdict</th><th>Pages</th><th>Rotating</th>
        <th>Best Pos</th><th>Volume</th><th>Value/mo</th><th>GSC Data</th>
        <th>Ranking URLs</th><th>Reason & Recommendation</th>
      </tr>
    </thead>
    <tbody>
      {rows_html_str}
    </tbody>
  </table>

  <div class="footer">
    Cannibalization detection: live SERP (depth 100) + historical SERP snapshots unioned over time.
    Verdicts use the over-time rotation model: strong = rotating at high position, investigate = competing but unclear, harmless = not splitting authority.
    Data: DataForSEO · Generated by cannibalization_detector.py
  </div>
</body>
</html>"""


def _escape(text):
    if not text:
        return ""
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


# ── Console summary ───────────────────────────────────────────────────────────

def print_summary(result):
    domain = result['domain']
    checked = result['keywords_checked']
    ranking = result['keywords_ranking']
    conflicts = result['conflicts']
    clean = result['clean']

    strong = sum(1 for c in conflicts if c['verdict'] == 'strong candidate')
    investigate = sum(1 for c in conflicts if c['verdict'] == 'investigate')
    harmless = sum(1 for c in conflicts if c['verdict'] == 'harmless overlap')

    print(f"\n{'='*60}")
    print(f"KEYWORD CANNIBALIZATION — {domain}")
    print(f"{'='*60}")
    print(f"CHECKED: {checked} keywords · {ranking} ranking · {len(conflicts)} with ≥2 competing pages")
    print(f"\nVERDICTS")
    print(f"  strong candidate: {strong}")
    print(f"  investigate:      {investigate}")
    print(f"  harmless overlap: {harmless}")
    print(f"  clean (1 page):   {len(clean)}")

    if conflicts:
        print(f"\nTOP CONFLICTS (by value at risk)")
        print(f"  {'#':<4} {'Keyword':<35} {'Verdict':<20} {'Best':<6} {'Pages':<7} {'Val/mo':<12}")
        print(f"  {'-'*90}")
        for c in conflicts[:10]:
            val = f"${c.get('value_at_risk', 0):.2f}" if c.get('value_at_risk') else "—"
            print(f"  {c.get('priority_rank', ''):<4} {c['keyword'][:33]:<35} "
                  f"{c['verdict'][:18]:<20} #{c.get('best_pos', '?'):<5} "
                  f"{c['n_pages']:<7} {val:<12}")

    print(f"\n{'='*60}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Detect keyword cannibalization using DataForSEO SERP data.')
    parser.add_argument('--domain', required=True, help='Target domain (e.g. softprowatersystems.com)')
    parser.add_argument('--keywords', required=True,
                        help='Comma-separated keywords, or path to a .txt/.csv file (one per line)')
    parser.add_argument('--output', required=True, help='Output directory for report + CSV')
    parser.add_argument('--location-code', type=int, default=2840, help='DataForSEO location code (default 2840=US)')
    parser.add_argument('--language', default='en', help='Language code (default en)')
    parser.add_argument('--include-subdomains', action='store_true', help='Include subdomains as same site')
    parser.add_argument('--gsc-file', default=None, help='Path to GSC JSON data file')
    parser.add_argument('--url-map', default=None, help='JSON file with path→type map (e.g. {"/blog/*":"informational"})')
    args = parser.parse_args()

    # Parse keywords
    if os.path.exists(args.keywords):
        with open(args.keywords) as f:
            keywords = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    else:
        keywords = [k.strip() for k in args.keywords.split(',') if k.strip()]

    if not keywords:
        print("ERROR: No keywords provided.")
        sys.exit(1)

    # Load URL map
    url_map = None
    if args.url_map and os.path.exists(args.url_map):
        with open(args.url_map) as f:
            url_map = json.load(f)

    # Run detection
    result = detect_cannibalization(
        domain=args.domain,
        keywords=keywords,
        location_code=args.location_code,
        include_subdomains=args.include_subdomains,
        language_code=args.language,
        url_map=url_map,
        gsc_path=args.gsc_file,
    )

    # Generate report
    all_conflicts = result['conflicts']
    # Include harmless conflicts in the report too (they have n_pages >= 2)
    html_path, csv_path = generate_cannibalization_report(
        args.domain, all_conflicts, args.output
    )

    # Also write raw JSON
    json_path = os.path.join(args.output, 'cannibalization_raw.json')
    os.makedirs(args.output, exist_ok=True)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

    # Summary
    print_summary(result)
    print(f"\nReport: {html_path}")
    print(f"CSV:    {csv_path}")
    print(f"JSON:   {json_path}")
    print(f"\nData: DataForSEO")


if __name__ == '__main__':
    main()
