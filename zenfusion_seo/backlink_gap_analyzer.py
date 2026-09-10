#!/usr/bin/env python3
"""
Zenfusion Backlink Gap Analyzer

Compares a brand's backlink profile against a competitor's backlink profile,
identifies referring domains that link to the competitor but NOT to the brand,
scores each opportunity, and generates an HTML report + JSON results.

Now enhanced with DataForSEO's domain_intersection, referring_networks,
and bulk_ranks endpoints, plus LLM citation domain cross-referencing.

Usage:
  python3 backlink_gap_analyzer.py <brand_domain> <competitor_domain> [--output path]

Example:
  python3 backlink_gap_analyzer.py softprowatersystems.com aquasana.com

DataForSEO API credentials in ~/.hermes/.env:
  DATAFORSEO_LOGIN
  DATAFORSEO_PASSWORD

Majestic Million CSV at ~/Downloads/majestic_million.csv
LLM citation domains at ~/.hermes/scripts/llm_citation_domains.json
"""

import sys, os, json, base64, urllib.request, csv, time, argparse, re
from datetime import datetime
from urllib.parse import urlparse

MAJESTIC_CSV = os.path.expanduser('~/Downloads/majestic_million.csv')
ENV_PATH = os.path.expanduser('~/.hermes/.env')
LLM_CITATION_PATH = os.path.expanduser('~/.hermes/scripts/llm_citation_domains.json')
DEFAULT_OUTPUT_DIR = os.path.expanduser('~/.hermes/audits/backlink-gap')


# ---------------------------------------------------------------------------
# Credentials & API helpers
# ---------------------------------------------------------------------------

def get_creds():
    """Load DataForSEO credentials from environment or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATAFORSEO_LOGIN='):
                        login = line.split('=', 1)[1]
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1]
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found")
        print("Set them in ~/.hermes/.env or as environment variables")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """POST to a DataForSEO v3 live endpoint. Payload is a single task dict;
    it will be wrapped in an array per DataForSEO convention."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'https://api.dataforseo.com/v3/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    body = json.dumps([payload]).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            err = data.get('status_message', 'Unknown error')
            print(f"  API error ({data.get('status_code')}): {err}")
            return None
        return data
    except Exception as e:
        print(f"  Request error: {e}")
        return None


# ---------------------------------------------------------------------------
# DataForSEO backlink fetchers (existing)
# ---------------------------------------------------------------------------

def fetch_backlinks_list(domain, limit=1000):
    """Fetch individual backlinks via /v3/backlinks/backlinks/live.

    Returns list of dicts with: source_url, target_url, anchor, domain_from,
    domain_to, links_type, first_seen, last_seen, domain_strength.
    """
    payload = {
        'target': domain,
        'mode': 'as_is',
        'limit': limit,
        'include_subdomains': True,
    }
    data = dfs_api_call('backlinks/backlinks/live', payload)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get('result', [{}])[0]
    items = result.get('items', [])

    backlinks = []
    for item in items:
        # Determine link type
        links_type = item.get('type', '')
        # DataForSEO uses "dofollow" / "nofollow" / "ugc" / "sponsored"
        is_dofollow = links_type == 'dofollow'

        backlinks.append({
            'source_url': item.get('url_from', ''),
            'target_url': item.get('url_to', ''),
            'anchor': item.get('anchor', ''),
            'domain_from': item.get('domain_from', '').lower(),
            'domain_to': item.get('domain_to', '').lower(),
            'links_type': links_type,
            'is_dofollow': is_dofollow,
            'first_seen': item.get('first_seen', '')[:10],
            'last_seen': item.get('last_seen', '')[:10],
            'domain_strength': item.get('domain_from_rank', 0) or 0,
        })
    return backlinks


def fetch_backlinks_summary(domain):
    """Fetch backlink profile summary via /v3/backlinks/summary/live.

    Returns dict with backlinks count, referring_domains count, etc.
    """
    payload = {
        'target': domain,
        'include_subdomains': True,
    }
    data = dfs_api_call('backlinks/summary/live', payload)
    if not data or not data.get('tasks'):
        return {}

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Summary task error: {task.get('status_message', 'Unknown')}")
        return {}

    result = task.get('result', [{}])[0]
    return {
        'backlinks': result.get('backlinks', 0),
        'referring_domains': result.get('referring_domains', 0),
        'referring_main_domains': result.get('referring_main_domains', 0),
        'backlinks_dofollow': result.get('backlinks_dofollow', 0),
        'backlinks_nofollow': result.get('backlinks_nofollow', 0),
        'lost_backlinks': result.get('lost_backlinks', 0),
        'new_backlinks': result.get('new_backlinks', 0),
    }


# ---------------------------------------------------------------------------
# NEW: DataForSEO domain_intersection, referring_networks, bulk_ranks
# ---------------------------------------------------------------------------

def fetch_domain_intersection(competitor_domain, client_domain, limit=1000):
    """Fetch domains linking to competitor but NOT to client via
    /v3/backlinks/domain_intersection/live.

    Uses exclude_targets to find the actual gap in a single call.

    Returns list of dicts with: domain, rank, dofollow_links, nofollow_links,
    first_seen, referring_pages, backlinks, referring_domains, intersections_count.
    """
    payload = {
        'targets': {'1': competitor_domain},
        'exclude_targets': [client_domain],
        'limit': limit,
        'include_subdomains': True,
        'exclude_internal_backlinks': True,
    }
    data = dfs_api_call('backlinks/domain_intersection/live', payload)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Domain intersection task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get('result', [{}])[0]
    items = result.get('items', [])

    gap_domains = []
    for item in items:
        # Each item has domain_intersection dict with per-target data
        di = item.get('domain_intersection', {})
        # Target "1" corresponds to our competitor (first key in targets object)
        target_data = di.get('1', {})
        summary = item.get('summary', {})

        domain = (target_data.get('domain_from', '') or
                   target_data.get('domain', '') or
                   item.get('domain', '')).lower()

        if not domain:
            continue

        gap_domains.append({
            'domain': domain,
            'rank': target_data.get('rank', 0) or 0,
            'dofollow_links': target_data.get('backlinks_dofollow', 0) or 0,
            'nofollow_links': target_data.get('backlinks_nofollow', 0) or 0,
            'backlinks': target_data.get('backlinks', 0) or 0,
            'referring_pages': target_data.get('referring_pages', 0) or 0,
            'referring_domains': target_data.get('referring_domains', 0) or 0,
            'first_seen': (target_data.get('first_seen', '') or '')[:10],
            'intersections_count': summary.get('intersections_count', 0) or 0,
        })

    return gap_domains


def fetch_referring_networks(domain, limit=100):
    """Fetch referring networks analysis via /v3/backlinks/referring_networks/live.

    Shows which networks/platforms (by IP or subnet) link to the domain.

    Returns list of dicts with: network_address, rank, backlinks,
    referring_pages, referring_domains, referring_ips, first_seen.
    """
    payload = {
        'target': domain,
        'network_address_type': 'subnet',
        'limit': limit,
        'include_subdomains': True,
        'order_by': ['backlinks,desc'],
    }
    data = dfs_api_call('backlinks/referring_networks/live', payload)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Referring networks task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get('result', [{}])[0]
    items = result.get('items', [])

    networks = []
    for item in items:
        networks.append({
            'network_address': item.get('network_address', ''),
            'rank': item.get('rank', 0) or 0,
            'backlinks': item.get('backlinks', 0) or 0,
            'referring_pages': item.get('referring_pages', 0) or 0,
            'referring_domains': item.get('referring_domains', 0) or 0,
            'referring_ips': item.get('referring_ips', 0) or 0,
            'first_seen': (item.get('first_seen', '') or '')[:10],
        })

    return networks


def fetch_bulk_ranks(domains, rank_scale='one_hundred'):
    """Fetch domain ranks in bulk via /v3/backlinks/bulk_ranks/live.

    Batches up to 1000 domains per call. Returns dict {domain: rank}.
    """
    if not domains:
        return {}

    # Remove duplicates while preserving order
    seen = set()
    unique_domains = []
    for d in domains:
        d = d.lower().strip()
        if d and d not in seen:
            seen.add(d)
            unique_domains.append(d)

    ranks = {}
    batch_size = 1000
    for i in range(0, len(unique_domains), batch_size):
        batch = unique_domains[i:i + batch_size]
        payload = {
            'targets': batch,
            'rank_scale': rank_scale,
        }
        data = dfs_api_call('backlinks/bulk_ranks/live', payload)
        if not data or not data.get('tasks'):
            continue

        task = data['tasks'][0]
        if task.get('status_code') != 20000:
            print(f"  Bulk ranks task error: {task.get('status_message', 'Unknown')}")
            continue

        result = task.get('result', [{}])[0]
        items = result.get('items', [])
        for item in items:
            target = (item.get('target', '') or '').lower()
            rank = item.get('rank', 0) or 0
            if target:
                ranks[target] = rank

    return ranks


# ---------------------------------------------------------------------------
# LLM citation domains
# ---------------------------------------------------------------------------

def load_llm_citation_domains():
    """Load LLM citation domains JSON and return a set of normalized domain names."""
    if not os.path.exists(LLM_CITATION_PATH):
        print(f"  WARNING: LLM citation domains file not found at {LLM_CITATION_PATH}")
        print("  Opportunities will be scored without LLM citation cross-referencing.")
        return set(), {}

    try:
        with open(LLM_CITATION_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  WARNING: Could not load LLM citation domains: {e}")
        return set(), {}

    domain_set = set()
    domain_info = {}
    for key, val in data.items():
        domain = val.get('domain', key).lower().strip()
        domain_set.add(domain)
        domain_info[domain] = {
            'tier': val.get('tier', ''),
            'strategy': val.get('strategy', ''),
        }

    return domain_set, domain_info


def is_llm_citation_domain(domain, llm_domain_set):
    """Check if a domain is in the LLM citation domains list."""
    return domain.lower().strip() in llm_domain_set


# ---------------------------------------------------------------------------
# Majestic Million
# ---------------------------------------------------------------------------

def load_majestic():
    """Load Majestic Million CSV into a domain->rank dict."""
    if not os.path.exists(MAJESTIC_CSV):
        print(f"  WARNING: Majestic Million CSV not found at {MAJESTIC_CSV}")
        print("  Opportunities will be scored without Majestic data.")
        return {}

    db = {}
    with open(MAJESTIC_CSV, 'r', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row.get('Domain', '').lower().strip()
            if domain:
                try:
                    db[domain] = int(row.get('GlobalRank', 0))
                except ValueError:
                    pass
    return db


def majestic_rank(domain, majestic_db):
    """Return Majestic Million global rank for domain, or None."""
    return majestic_db.get(domain.lower().strip())


# ---------------------------------------------------------------------------
# Gap analysis & scoring
# ---------------------------------------------------------------------------

def normalize_domain(d):
    """Strip www. prefix and protocol."""
    if not d:
        return ''
    d = d.lower().strip()
    if d.startswith('http://') or d.startswith('https://'):
        d = urlparse(d).netloc
    if d.startswith('www.'):
        d = d[4:]
    return d


def compute_opportunity_score(domain_strength, maj_rank, is_dofollow,
                                competitor_overlap, is_llm_citation,
                                bulk_rank, brand_domain, comp_domain):
    """Score an opportunity 0-100.

    Components (enhanced with new data sources):
      - Domain strength (DataForSEO domain_from_rank)     up to 30 pts
      - Bulk rank (DataForSEO bulk_ranks 0-100 scale)     up to 20 pts
      - Majestic Million rank                            up to 15 pts
      - DoFollow link bonus                              up to 5 pts
      - Competitor overlap count bonus                   up to 15 pts
      - LLM citation domain bonus                        up to 10 pts
      - Relevance (domain name overlap heuristic)        up to 5 pts
    """
    score = 0

    # Domain strength (DataForSEO rank ~0-1000 range, higher = stronger)
    if domain_strength:
        if domain_strength >= 800:
            score += 30
        elif domain_strength >= 600:
            score += 26
        elif domain_strength >= 400:
            score += 21
        elif domain_strength >= 200:
            score += 17
        elif domain_strength >= 80:
            score += 12
        elif domain_strength >= 20:
            score += 8
        else:
            score += 4

    # Bulk rank (0-100 scale, from bulk_ranks endpoint)
    if bulk_rank is not None and bulk_rank > 0:
        # bulk_rank is 0-100, so we can use it more directly
        if bulk_rank >= 80:
            score += 20
        elif bulk_rank >= 60:
            score += 16
        elif bulk_rank >= 40:
            score += 12
        elif bulk_rank >= 20:
            score += 8
        else:
            score += 4

    # Majestic Million rank
    if maj_rank is not None:
        if maj_rank <= 1_000:
            score += 15
        elif maj_rank <= 10_000:
            score += 12
        elif maj_rank <= 50_000:
            score += 10
        elif maj_rank <= 100_000:
            score += 7
        elif maj_rank <= 500_000:
            score += 5
        else:
            score += 3

    # DoFollow bonus
    if is_dofollow:
        score += 5
    else:
        score += 2

    # Competitor overlap count bonus (how many competitors this domain links to)
    if competitor_overlap:
        if competitor_overlap >= 5:
            score += 15
        elif competitor_overlap >= 3:
            score += 12
        elif competitor_overlap >= 2:
            score += 8
        else:
            score += 4

    # LLM citation domain bonus (double-value: backlink + AI citation)
    if is_llm_citation:
        score += 10

    # Relevance heuristic — base placeholder
    score += 5

    return min(score, 100)


def analyze_gap(brand_domain, comp_domain, majestic_db,
                llm_domain_set=None, llm_domain_info=None):
    """Main analysis: fetch both profiles, find gap, score opportunities.

    Enhanced with domain_intersection, referring_networks, and bulk_ranks endpoints.
    Falls back to the legacy backlinks-list approach if domain_intersection fails.
    """
    brand_domain = normalize_domain(brand_domain)
    comp_domain = normalize_domain(comp_domain)

    if llm_domain_set is None:
        llm_domain_set = set()
    if llm_domain_info is None:
        llm_domain_info = {}

    # ---- Fetch summaries ----
    print(f"\n  Fetching backlink summary for {brand_domain}...")
    brand_summary = fetch_backlinks_summary(brand_domain)
    print(f"    Backlinks: {brand_summary.get('backlinks', 0):,}  "
          f"Referring domains: {brand_summary.get('referring_domains', 0):,}")

    print(f"  Fetching backlink summary for {comp_domain}...")
    comp_summary = fetch_backlinks_summary(comp_domain)
    print(f"    Backlinks: {comp_summary.get('backlinks', 0):,}  "
          f"Referring domains: {comp_summary.get('referring_domains', 0):,}")

    # ---- Fetch backlink lists (kept for anchor/source URL data) ----
    print(f"\n  Fetching backlinks list for {brand_domain} (limit 1000)...")
    brand_links = fetch_backlinks_list(brand_domain, limit=1000)
    print(f"    Got {len(brand_links)} backlinks")

    print(f"  Fetching backlinks list for {comp_domain} (limit 1000)...")
    comp_links = fetch_backlinks_list(comp_domain, limit=1000)
    print(f"    Got {len(comp_links)} backlinks")

    # ---- Build referring domain sets from backlinks ----
    brand_ref_domains = set()
    for bl in brand_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d:
            brand_ref_domains.add(d)

    comp_ref_domains = set()
    for bl in comp_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d:
            comp_ref_domains.add(d)

    print(f"\n  Brand referring domains (unique): {len(brand_ref_domains)}")
    print(f"  Competitor referring domains (unique): {len(comp_ref_domains)}")

    # ---- NEW: Fetch domain intersection (the real gap) ----
    print(f"\n  Fetching domain intersection: {comp_domain} (excluding {brand_domain})...")
    intersection_gap = fetch_domain_intersection(comp_domain, brand_domain, limit=1000)
    print(f"    Domain intersection returned {len(intersection_gap)} gap domains")

    # Build a lookup from intersection data for enriched scoring
    intersection_lookup = {}
    for ig in intersection_gap:
        d = normalize_domain(ig.get('domain', ''))
        if d:
            intersection_lookup[d] = ig

    # ---- Find gap: domains linking to competitor but NOT to brand ----
    # Use both methods: legacy (set difference) + domain_intersection
    missing_domains_legacy = comp_ref_domains - brand_ref_domains

    # Merge: union of both methods for maximum coverage
    missing_domains = set(missing_domains_legacy)
    for ig in intersection_gap:
        d = normalize_domain(ig.get('domain', ''))
        if d:
            missing_domains.add(d)

    print(f"  Missing domains (legacy set diff): {len(missing_domains_legacy)}")
    print(f"  Missing domains (domain_intersection): {len(intersection_gap)}")
    print(f"  Merged missing domains: {len(missing_domains)}")

    # ---- Build opportunities list from missing backlinks + intersection data ----
    # Group comp_links by domain_from so we pick the best representative link
    comp_by_domain = {}
    for bl in comp_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d not in missing_domains:
            continue  # not in the gap, skip
        # Keep the strongest link per domain (highest domain_strength, prefer dofollow)
        existing = comp_by_domain.get(d)
        if existing is None:
            comp_by_domain[d] = bl
        else:
            # Prefer dofollow, then higher domain_strength
            if (bl.get('is_dofollow') and not existing.get('is_dofollow')) or \
               (bl.get('is_dofollow') == existing.get('is_dofollow') and
                bl.get('domain_strength', 0) > existing.get('domain_strength', 0)):
                comp_by_domain[d] = bl

    # Add intersection-only domains (not in comp_links but found by domain_intersection)
    for ig in intersection_gap:
        d = normalize_domain(ig.get('domain', ''))
        if d and d not in comp_by_domain:
            comp_by_domain[d] = {
                'source_url': '',
                'target_url': '',
                'anchor': '',
                'domain_from': d,
                'links_type': '',
                'is_dofollow': ig.get('dofollow_links', 0) > 0,
                'first_seen': ig.get('first_seen', ''),
                'last_seen': '',
                'domain_strength': ig.get('rank', 0) or 0,
            }

    # ---- NEW: Fetch bulk ranks for all prospect domains ----
    all_prospect_domains = list(comp_by_domain.keys())
    print(f"\n  Fetching bulk ranks for {len(all_prospect_domains)} prospect domains...")
    bulk_ranks = fetch_bulk_ranks(all_prospect_domains, rank_scale='one_hundred')
    print(f"    Got bulk ranks for {len(bulk_ranks)} domains")

    # ---- NEW: Fetch referring networks for competitor ----
    print(f"\n  Fetching referring networks for {comp_domain}...")
    comp_networks = fetch_referring_networks(comp_domain, limit=100)
    print(f"    Got {len(comp_networks)} referring networks")

    print(f"  Fetching referring networks for {brand_domain}...")
    brand_networks = fetch_referring_networks(brand_domain, limit=100)
    print(f"    Got {len(brand_networks)} referring networks")

    # ---- Build opportunities with enhanced scoring ----
    opportunities = []
    for d, bl in comp_by_domain.items():
        maj_rank_val = majestic_rank(d, majestic_db)
        ds = bl.get('domain_strength', 0) or 0
        is_df = bl.get('is_dofollow', False)

        # Get intersection data for this domain
        ig_data = intersection_lookup.get(d, {})
        competitor_overlap = ig_data.get('intersections_count', 1)

        # Get bulk rank
        bulk_rank = bulk_ranks.get(d)

        # Check LLM citation status
        is_llm = is_llm_citation_domain(d, llm_domain_set)
        llm_info = llm_domain_info.get(d, {})

        score = compute_opportunity_score(
            ds, maj_rank_val, is_df, competitor_overlap, is_llm,
            bulk_rank, brand_domain, comp_domain
        )

        opportunities.append({
            'source_domain': d,
            'source_url': bl.get('source_url', ''),
            'anchor_text': bl.get('anchor', ''),
            'target_url': bl.get('target_url', ''),
            'links_type': bl.get('links_type', ''),
            'is_dofollow': is_df,
            'first_seen': bl.get('first_seen', ''),
            'last_seen': bl.get('last_seen', ''),
            'domain_strength': ds,
            'majestic_rank': maj_rank_val,
            'in_majestic': maj_rank_val is not None,
            'bulk_rank': bulk_rank,
            'competitor_overlap': competitor_overlap,
            'is_llm_citation': is_llm,
            'llm_citation_tier': llm_info.get('tier', ''),
            'llm_citation_strategy': llm_info.get('strategy', ''),
            'opportunity_score': round(score, 1),
        })

    # Sort by score descending
    opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)

    print(f"\n  Scored {len(opportunities)} missing link opportunities")
    if opportunities:
        top = opportunities[0]
        print(f"  Top opportunity: {top['source_domain']} "
              f"(score {top['opportunity_score']}, "
              f"DS {top['domain_strength']}, "
              f"Bulk {top.get('bulk_rank', 'N/A')}, "
              f"Majestic {'#'+str(top['majestic_rank']) if top['majestic_rank'] else 'N/A'}, "
              f"LLM {'YES' if top['is_llm_citation'] else 'no'})")

    return {
        'brand_domain': brand_domain,
        'competitor_domain': comp_domain,
        'brand_summary': brand_summary,
        'competitor_summary': comp_summary,
        'brand_ref_domains_count': len(brand_ref_domains),
        'competitor_ref_domains_count': len(comp_ref_domains),
        'missing_domains_count': len(missing_domains),
        'missing_domains_legacy_count': len(missing_domains_legacy),
        'intersection_gap_count': len(intersection_gap),
        'opportunities': opportunities,
        'competitor_referring_networks': comp_networks,
        'brand_referring_networks': brand_networks,
        'bulk_ranks_count': len(bulk_ranks),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }


# ---------------------------------------------------------------------------
# HTML report generation (White SaaS theme)
# ---------------------------------------------------------------------------

def generate_html_report(results):
    """Generate a white-themed SaaS-style HTML report."""
    brand = results['brand_domain']
    comp = results['competitor_domain']
    opps = results['opportunities']
    bs = results['brand_summary']
    cs = results['competitor_summary']
    gen_date = results['generated_at']
    comp_networks = results.get('competitor_referring_networks', [])
    brand_networks = results.get('brand_referring_networks', [])

    top_10 = opps[:10]

    # Pre-compute values
    top_score = f"{opps[0]['opportunity_score']:.1f}" if opps else "0.0"
    top_domain = opps[0]['source_domain'] if opps else "N/A"
    in_majestic_count = sum(1 for o in opps if o['in_majestic'])
    dofollow_count = sum(1 for o in opps if o['is_dofollow'])
    llm_citation_count = sum(1 for o in opps if o['is_llm_citation'])
    bulk_rank_count = sum(1 for o in opps if o.get('bulk_rank') is not None)
    intersection_count = results.get('intersection_gap_count', 0)

    # Build table rows
    table_rows = []
    for i, o in enumerate(opps, 1):
        maj_rank_display = f"#{o['majestic_rank']:,}" if o['majestic_rank'] else "—"
        bulk_rank_display = str(o['bulk_rank']) if o.get('bulk_rank') is not None else "—"
        overlap_display = str(o.get('competitor_overlap', 1))
        df_badge = '<span class="badge badge-df">DoFollow</span>' if o['is_dofollow'] else \
                   '<span class="badge badge-nf">NoFollow</span>'
        llm_badge = '<span class="badge badge-llm">★ LLM Citation</span>' if o['is_llm_citation'] else ''

        # Score color class
        sc = o['opportunity_score']
        if sc >= 70:
            score_class = "score-high"
        elif sc >= 40:
            score_class = "score-mid"
        else:
            score_class = "score-low"

        table_rows.append(f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="domain"><a href="https://{o['source_domain']}" target="_blank" rel="noopener noindex nofollow">{o['source_domain']}</a></td>
          <td class="anchor">{(o['anchor_text'] or '')[:60]}</td>
          <td class="num">{o['domain_strength']}</td>
          <td class="num">{bulk_rank_display}</td>
          <td class="num">{maj_rank_display}</td>
          <td class="num">{overlap_display}</td>
          <td>{df_badge}{llm_badge}</td>
          <td class="num">{o['first_seen'] or '—'}</td>
          <td class="score {score_class}">{o['opportunity_score']:.1f}</td>
        </tr>""")

    # Build recommendation cards
    rec_cards = []
    for i, o in enumerate(top_10, 1):
        maj_info = f"Majestic #{o['majestic_rank']:,}" if o['majestic_rank'] else "Not in Majestic Million"
        bulk_info = f"Bulk Rank {o['bulk_rank']}" if o.get('bulk_rank') is not None else "Bulk Rank N/A"
        llm_info = ""
        if o['is_llm_citation']:
            llm_info = ' · <span class="llm-flag">★ LLM Citation (' + str(o.get('llm_citation_tier', '?')) + ')</span>'
        llm_strategy_html = ""
        if o['is_llm_citation'] and o.get('llm_citation_strategy'):
            llm_strategy_html = '<div class="llm-strategy">LLM Strategy: ' + o['llm_citation_strategy'] + '</div>'
        rec_cards.append(f"""
        <div class="rec-card">
          <div class="rec-rank">#{i}</div>
          <div class="rec-body">
            <div class="rec-domain"><a href="https://{o['source_domain']}" target="_blank" rel="noopener noindex nofollow">{o['source_domain']}</a></div>
            <div class="rec-meta">
              Score: <strong>{o['opportunity_score']:.1f}</strong> ·
              Domain Strength: <strong>{o['domain_strength']}</strong> ·
              {bulk_info} ·
              {maj_info} ·
              {'DoFollow' if o['is_dofollow'] else 'NoFollow'} ·
              Overlap: {o.get('competitor_overlap', 1)}
              {llm_info}
            </div>
            <div class="rec-anchor">Anchor: &quot;{(o['anchor_text'] or 'N/A')[:80]}&quot;</div>
            <div class="rec-url"><a href="{o['source_url']}" target="_blank" rel="noopener noindex nofollow">{o['source_url'][:120]}</a></div>
            {llm_strategy_html}
          </div>
        </div>""")

    # Build referring networks table rows (top 10 for competitor)
    network_rows = []
    for n in comp_networks[:10]:
        network_rows.append(f"""
        <tr>
          <td class="num">{n['network_address']}</td>
          <td class="num">{n['rank']}</td>
          <td class="num">{n['backlinks']:,}</td>
          <td class="num">{n['referring_domains']:,}</td>
          <td class="num">{n['referring_ips']:,}</td>
          <td class="num">{n['first_seen'] or '—'}</td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Backlink Gap Analysis: {brand} vs {comp}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #f8f9fb;
    color: #1a1d28;
    line-height: 1.6;
    padding: 20px 40px;
  }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h1 {{
    font-size: 28px;
    margin-bottom: 4px;
    font-weight: 700;
    color: #0f172a;
  }}
  .subtitle {{
    color: #64748b;
    font-size: 14px;
    margin-bottom: 30px;
  }}
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
    color: #0f172a;
  }}
  .card-sub {{
    font-size: 13px;
    color: #94a3b8;
    margin-top: 4px;
  }}

  /* Section headers */
  h2 {{
    font-size: 22px;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid #e2e8f0;
    color: #0f172a;
  }}

  /* Table */
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
    background: #f1f5f9;
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
  tr:hover td {{ background: #f8fafc; }}
  td.rank {{ color: #94a3b8; font-weight: 700; text-align: center; width: 36px; }}
  td.domain {{ font-weight: 600; color: #0f172a; }}
  td.anchor {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #64748b; }}
  td.url {{ max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.score {{ text-align: right; font-weight: 700; font-size: 15px; }}
  .score-high {{ color: #16a34a; }}
  .score-mid {{ color: #d97706; }}
  .score-low {{ color: #94a3b8; }}

  /* Badges */
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 4px;
  }}
  .badge-df {{ background: #dcfce7; color: #16a34a; }}
  .badge-nf {{ background: #fef3c7; color: #d97706; }}
  .badge-llm {{ background: #ede9fe; color: #7c3aed; }}

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
    font-size: 28px;
    font-weight: 700;
    color: #2563eb;
    min-width: 50px;
    text-align: center;
    padding-top: 2px;
  }}
  .rec-domain {{ font-size: 17px; font-weight: 600; margin-bottom: 4px; color: #0f172a; }}
  .rec-meta {{ font-size: 13px; color: #64748b; margin-bottom: 6px; }}
  .rec-anchor {{ font-size: 13px; color: #94a3b8; margin-bottom: 4px; font-style: italic; }}
  .rec-url {{ font-size: 12px; }}
  .llm-flag {{ color: #7c3aed; font-weight: 600; }}
  .llm-strategy {{ font-size: 12px; color: #7c3aed; margin-top: 4px; background: #f5f3ff; padding: 4px 8px; border-radius: 4px; }}

  .footer {{
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid #e2e8f0;
    color: #94a3b8;
    font-size: 12px;
    text-align: center;
  }}
</style>
</head>
<body>

<h1>Backlink Gap Analysis</h1>
<p class="subtitle">
  <strong style="color:#0f172a">{brand}</strong> vs <strong style="color:#0f172a">{comp}</strong>
  &nbsp;·&nbsp; <span class="generated">Generated {gen_date}</span>
</p>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="card-label">Brand Backlinks</div>
    <div class="card-value">{bs.get('backlinks', 0):,}</div>
    <div class="card-sub">{brand} · {bs.get('referring_domains', 0):,} referring domains</div>
  </div>
  <div class="card">
    <div class="card-label">Competitor Backlinks</div>
    <div class="card-value">{cs.get('backlinks', 0):,}</div>
    <div class="card-sub">{comp} · {cs.get('referring_domains', 0):,} referring domains</div>
  </div>
  <div class="card">
    <div class="card-label">Missing Links</div>
    <div class="card-value" style="color:#d97706">{len(opps)}</div>
    <div class="card-sub">Domains linking to competitor, not to brand</div>
  </div>
  <div class="card">
    <div class="card-label">Domain Intersection Gap</div>
    <div class="card-value" style="color:#2563eb">{intersection_count}</div>
    <div class="card-sub">Found via domain_intersection API</div>
  </div>
  <div class="card">
    <div class="card-label">Top Opportunity Score</div>
    <div class="card-value" style="color:#16a34a">{top_score}</div>
    <div class="card-sub">{top_domain}</div>
  </div>
  <div class="card">
    <div class="card-label">LLM Citation Prospects</div>
    <div class="card-value" style="color:#7c3aed">{llm_citation_count}</div>
    <div class="card-sub">Double-value: backlink + AI citation</div>
  </div>
  <div class="card">
    <div class="card-label">In Majestic Million</div>
    <div class="card-value">{in_majestic_count}</div>
    <div class="card-sub">of {len(opps)} missing link domains</div>
  </div>
  <div class="card">
    <div class="card-label">Bulk Ranks Retrieved</div>
    <div class="card-value">{bulk_rank_count}</div>
    <div class="card-sub">Domain strength checked in bulk</div>
  </div>
</div>

<!-- Missing Links Table -->
<h2>Missing Link Opportunities ({len(opps)})</h2>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Source Domain</th>
        <th>Anchor Text</th>
        <th>Domain Strength</th>
        <th>Bulk Rank</th>
        <th>Majestic Rank</th>
        <th>Overlap</th>
        <th>Link Type</th>
        <th>First Seen</th>
        <th>Score</th>
      </tr>
    </thead>
    <tbody>
      {''.join(table_rows) if table_rows else '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:30px">No missing link opportunities found.</td></tr>'}
    </tbody>
  </table>
</div>

<!-- Top 10 Recommendations -->
<h2>Top 10 Links to Pursue</h2>
<div class="recs">
  {''.join(rec_cards) if rec_cards else '<p style="color:#94a3b8">No recommendations available.</p>'}
</div>

<!-- Referring Networks -->
<h2>Competitor Referring Networks (Top 10)</h2>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Network Address</th>
        <th>Rank</th>
        <th>Backlinks</th>
        <th>Referring Domains</th>
        <th>Referring IPs</th>
        <th>First Seen</th>
      </tr>
    </thead>
    <tbody>
      {''.join(network_rows) if network_rows else '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:30px">No referring networks data available.</td></tr>'}
    </tbody>
  </table>
</div>

<div class="footer">
  Backlink Gap Analysis · Zenfusion SEO · DataForSEO API · Majestic Million<br>
  Endpoints: backlinks/live · summary/live · domain_intersection/live · referring_networks/live · bulk_ranks/live<br>
  Generated {gen_date} · <code style="color:#cbd5e1">{brand} vs {comp}</code>
</div>

</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Backlink Gap Analyzer — find links your competitor has that you don\'t'
    )
    parser.add_argument('--client', dest='brand_domain',
                        help='Your domain (e.g., softprowatersystems.com)')
    parser.add_argument('--competitor', dest='competitor_domain',
                        help='Competitor domain (e.g., aquasana.com)')
    # Keep backward compat: positional args also work
    parser.add_argument('positional_args', nargs='*', help='brand_domain competitor_domain (positional)')
    parser.add_argument('--output', '-o', help='Output directory for reports (default: ~/.hermes/audits/backlink-gap)')
    args = parser.parse_args()

    # Resolve brand/competitor from --client/--competitor flags or positional args
    brand_input = args.brand_domain
    comp_input = args.competitor_domain
    if not brand_input and len(args.positional_args) >= 1:
        brand_input = args.positional_args[0]
    if not comp_input and len(args.positional_args) >= 2:
        comp_input = args.positional_args[1]

    if not brand_input or not comp_input:
        parser.error("Both --client and --competitor are required (or provide as positional args)")

    output_dir = args.output or DEFAULT_OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    brand = normalize_domain(brand_input)
    comp = normalize_domain(comp_input)

    print(f"\n{'='*70}")
    print(f"  BACKLINK GAP ANALYSIS")
    print(f"  Brand:      {brand}")
    print(f"  Competitor: {comp}")
    print(f"{'='*70}")

    # Load Majestic Million
    print("\n  Loading Majestic Million database...", end='', flush=True)
    t0 = time.time()
    majestic_db = load_majestic()
    print(f" {len(majestic_db):,} domains ({time.time()-t0:.1f}s)")

    # Load LLM citation domains
    print("  Loading LLM citation domains...", end='', flush=True)
    t0 = time.time()
    llm_domain_set, llm_domain_info = load_llm_citation_domains()
    print(f" {len(llm_domain_set)} domains ({time.time()-t0:.1f}s)")

    # Run analysis
    results = analyze_gap(brand, comp, majestic_db, llm_domain_set, llm_domain_info)

    # Generate outputs
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name = f"backlink-gap_{brand}_vs_{comp}_{timestamp}"

    json_path = os.path.join(output_dir, f"{base_name}.json")
    html_path = os.path.join(output_dir, f"{base_name}.html")

    with open(json_path, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n  JSON saved: {json_path}")

    html = generate_html_report(results)
    with open(html_path, 'w') as f:
        f.write(html)
    print(f"  HTML saved: {html_path}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  ANALYSIS COMPLETE")
    print(f"  Brand backlinks:      {results['brand_summary'].get('backlinks', 0):,}")
    print(f"  Competitor backlinks: {results['competitor_summary'].get('backlinks', 0):,}")
    print(f"  Missing opportunities: {len(results['opportunities'])}")
    print(f"  Domain intersection:  {results.get('intersection_gap_count', 0)}")
    print(f"  Bulk ranks retrieved: {results.get('bulk_ranks_count', 0)}")
    print(f"  Referring networks:   {len(results.get('competitor_referring_networks', []))}")
    print(f"  LLM citation prospects: {sum(1 for o in results['opportunities'] if o['is_llm_citation'])}")
    print(f"  In Majestic Million:  {sum(1 for o in results['opportunities'] if o['in_majestic'])}")
    print(f"  Reports: {html_path}")
    print(f"{'='*70}\n")


if __name__ == '__main__':
    main()
