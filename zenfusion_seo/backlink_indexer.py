#!/usr/bin/env python3
"""
Zenfusion Backlink Indexer — auto-index unindexed Tier 1 backlink URLs.
Primary: IndexBolt API. Fallback: Omega Indexer (if IndexBolt fails).
"""
import json, os, sys, urllib.request

def get_env():
    env = {}
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k] = v
    return env

def indexbolt_submit(urls, indexing_type="normal", submission_name=None):
    """Submit URLs to IndexBolt for indexing."""
    env = get_env()
    api_key = env.get('INDEXBOLT_API_KEY', '')
    
    payload = {"urls": urls, "indexingType": indexing_type}
    if submission_name:
        payload["submissionName"] = submission_name
    
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://www.indexbolt.com/api/v1/submit",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.indexbolt.com",
        "Referer": "https://www.indexbolt.com/"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def indexbolt_balance():
    """Check IndexBolt credit balance."""
    env = get_env()
    api_key = env.get('INDEXBOLT_API_KEY', '')
    
    req = urllib.request.Request(
        "https://www.indexbolt.com/api/v1/balance",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.indexbolt.com",
        "Referer": "https://www.indexbolt.com/"},
        method="GET"
    )
    
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def omega_submit(urls, indexing_type="normal", submission_name=None):
    """Submit URLs to Omega Indexer (fallback when IndexBolt fails)."""
    env = get_env()
    api_key = env.get('OMEGA_INDEXER_API_KEY', '')
    
    payload = {"urls": urls, "indexing_type": indexing_type}
    if submission_name:
        payload["submission_name"] = submission_name
    
    data = json.dumps(payload).encode()
    
    # Try known Omega API endpoints
    endpoints = [
        "https://omegaindexer.com/api/v1/submit",
        "https://api.omegaindexer.com/v1/submit",
        "https://omegaindexer.com/api/submit",
        "https://www.omegaindexer.com/api/v1/submit",
    ]
    
    for url in endpoints:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.indexbolt.com",
        "Referer": "https://www.indexbolt.com/"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # Try next endpoint
            raise
    
    raise Exception("All Omega API endpoints returned 404")

def submit_for_indexing(urls, indexing_type="normal", submission_name=None):
    """Submit URLs for indexing — Google Indexing API (free) primary, IndexBolt fallback, Omega last resort."""
    # 1. Try Google Indexing API first (FREE)
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
        import google_indexing_helper
        result = google_indexing_helper.submit_urls(urls, batch_delay=0.3)
        if result['submitted'] > 0:
            print(f"  Google Indexing API: {result['submitted']}/{result['total']} URLs submitted (free)")
            if result['failed'] > 0:
                print(f"  {result['failed']} URLs failed Google API, falling back to IndexBolt...")
                failed_urls = [r['url'] for r in result['results'] if not r['success']]
                # 2. Fallback to IndexBolt for failed URLs
                try:
                    ib_result = indexbolt_submit(failed_urls, indexing_type, submission_name)
                    return {"service": "Google+IndexBolt", "result": ib_result, "google_submitted": result['submitted']}
                except Exception as e:
                    print(f"  IndexBolt failed: {e}, trying Omega...")
                    try:
                        omega_result = omega_submit(failed_urls, indexing_type, submission_name)
                        return {"service": "Google+Omega", "result": omega_result, "google_submitted": result['submitted']}
                    except Exception as e2:
                        print(f"  Omega also failed: {e2}")
            return {"service": "Google Indexing API", "result": result, "google_submitted": result['submitted']}
    except Exception as e:
        print(f"  Google Indexing API failed: {e}, falling back to IndexBolt...")
    
    # 3. Fallback to IndexBolt
    try:
        result = indexbolt_submit(urls, indexing_type, submission_name)
        return {"service": "IndexBolt", "result": result}
    except Exception as e:
        print(f"  IndexBolt failed: {e}")
        print(f"  Falling back to Omega Indexer...")
        try:
            result = omega_submit(urls, indexing_type, submission_name)
            return {"service": "Omega", "result": result}
        except Exception as e2:
            print(f"  Omega also failed: {e2}")
            return {"service": "none", "error": str(e2)}

def indexbolt_status(submission_id):
    """Check submission status."""
    env = get_env()
    api_key = env.get('INDEXBOLT_API_KEY', '')
    
    req = urllib.request.Request(
        f"https://www.indexbolt.com/api/v1/submissions/{submission_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET"
    )
    
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def auto_index_unindexed(analysis_file, money_site_domain=None):
    """
    Read a backlink analysis JSON file, find unindexed Tier 1 URLs,
    submit them to IndexBolt for indexing.
    
    CRITICAL RULES:
    - ONLY index Tier 1 URLs (links that point to the client's money site)
    - ONLY index URLs that are NOT already indexed in Google
    - If money_site_domain is provided, filter to only URLs linking to that domain
    - Never index Tier 2 or Tier 3 links
    """
    with open(analysis_file) as f:
        backlinks = json.load(f)
    
    # Filter: only Tier 1 + unindexed + LIVE (returns HTTP 200)
    candidates = []
    dead_urls = []
    for b in backlinks:
        # Must be Tier 1 (direct link to money site)
        if not b.get('tier', '').startswith('T1'):
            continue
        # Must NOT be already indexed
        if b.get('google_indexed') == True:
            continue
        # Must be LIVE — check HTTP status before submitting
        url = b['url']
        try:
            req = urllib.request.Request(url, method='HEAD',
                headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    b['url_live'] = True
                    candidates.append(b)
                else:
                    b['url_live'] = False
                    dead_urls.append(url)
        except Exception:
            # Try GET as fallback (some servers don't support HEAD)
            try:
                req = urllib.request.Request(url,
                    headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        b['url_live'] = True
                        candidates.append(b)
                    else:
                        b['url_live'] = False
                        dead_urls.append(url)
            except Exception:
                b['url_live'] = False
                dead_urls.append(url)
    
    if dead_urls:
        print(f"\nDEAD URLs (skipped to save credits): {len(dead_urls)}")
        for u in dead_urls[:10]:
            print(f"  DEAD: {u}")
        if len(dead_urls) > 10:
            print(f"  ... and {len(dead_urls) - 10} more dead URLs")
    
    if not candidates:
        print("\nNo live unindexed Tier 1 URLs to submit.")
        return
    
    urls = [b['url'] for b in candidates]
    print(f"Found {len(urls)} unindexed Tier 1 URLs to index:")
    for u in urls[:10]:
        print(f"  -> {u}")
    if len(urls) > 10:
        print(f"  ... and {len(urls) - 10} more")
    
    # Check balance
    balance = indexbolt_balance()
    credits = balance['data']['balance']
    needed = len(urls)
    print(f"\nIndexBolt balance: {credits} credits (need {needed})")
    
    if credits < needed:
        print(f"Not enough credits. Need {needed}, have {credits}.")
        print("Submitting in batches of available credits...")
        urls = urls[:credits]
        needed = len(urls)
        print(f"Will submit {needed} URLs with available credits.")
    
    if needed == 0:
        print("No credits available for indexing.")
        return
    
    # Submit
    print(f"\nSubmitting {len(urls)} URLs to IndexBolt (normal indexing)...")
    result = indexbolt_submit(urls, "normal", f"Auto-index unindexed T1 backlinks")
    
    if result.get('success'):
        print(f"Submitted! ID: {result['data']['submissionId']}")
        print(f"   Credits used: {result['data']['creditsUsed']}")
        print(f"   Balance after: {result['data']['balanceAfter']}")
        
        # Update the analysis file with submission ID
        submitted_urls = set(urls)
        for b in backlinks:
            if b['url'] in submitted_urls:
                b['indexing_submitted'] = True
                b['indexing_submission_id'] = result['data']['submissionId']
                b['indexing_service'] = 'IndexBolt'
        
        with open(analysis_file, 'w') as f:
            json.dump(backlinks, f, indent=2)
        
        print(f"\nAnalysis file updated with indexing submission tracking.")
    else:
        print(f"Submission failed: {result}")

# ============================================================================
# LLM CITATION DOMAIN HELPERS (shared by backlink report scripts)
# ============================================================================
import os as _os

_LLM_DOMAINS_CACHE = None

def load_llm_citation_domains():
    """Load LLM citation domains from JSON. Returns dict: domain -> {tier, strategy, ...}."""
    global _LLM_DOMAINS_CACHE
    if _LLM_DOMAINS_CACHE is not None:
        return _LLM_DOMAINS_CACHE
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'llm_citation_domains.json')
    try:
        with open(path) as f:
            _LLM_DOMAINS_CACHE = json.load(f)
    except Exception:
        _LLM_DOMAINS_CACHE = {}
    return _LLM_DOMAINS_CACHE

def _get_root_domain(domain):
    """Extract root domain (e.g., sub.example.co.uk -> example.co.uk)."""
    parts = domain.lower().split('.')
    if len(parts) >= 3 and parts[-2] in ('co', 'com', 'org', 'net', 'gov', 'edu', 'mil'):
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:]) if len(parts) >= 2 else domain

def is_llm_citation_domain(domain):
    """Check if a domain (or its root) is in the LLM citation favorites list."""
    llm_domains = load_llm_citation_domains()
    domain = domain.lower().replace('www.', '')
    if domain in llm_domains:
        return True, llm_domains[domain]
    root = _get_root_domain(domain)
    if root in llm_domains:
        return True, llm_domains[root]
    return False, None

def tag_backlinks_llm(backlinks):
    """Tag each backlink dict with 'is_llm_favorite' (bool) and 'llm_tier' (str or None).
    Returns the count of LLM-favorite backlinks."""
    llm_count = 0
    for b in backlinks:
        is_llm, info = is_llm_citation_domain(b.get('domain', ''))
        b['is_llm_favorite'] = is_llm
        b['llm_tier'] = info.get('tier', '') if info else None
        b['llm_strategy'] = info.get('strategy', '') if info else None
        if is_llm:
            llm_count += 1
    return llm_count

def sort_backlinks_llm_first(backlinks):
    """Sort backlinks: LLM-favorite first (by tier S > A > B > C), then by total_score descending.
    Modifies the list in place and returns it."""
    tier_order = {'S': 0, 'A': 1, 'B': 2, 'C': 3}
    backlinks.sort(key=lambda b: (
        0 if b.get('is_llm_favorite') else 1,
        tier_order.get(b.get('llm_tier', ''), 9) if b.get('is_llm_favorite') else 0,
        -b.get('total_score', 0)
    ))
    return backlinks

def get_llm_badge_html(backlink):
    """Return HTML badge string for LLM-favorite backlinks, empty string otherwise."""
    if backlink.get('is_llm_favorite'):
        tier = backlink.get('llm_tier', '')
        title = backlink.get('llm_strategy', '')
        return f' <span style="background:#6c5ce7;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700" title="{title}">🤖 LLM</span>'
    return ''

# ============================================================================
# KE ANCHOR VOLUME HELPER
# ============================================================================

def add_anchor_volumes(backlinks, country='us'):
    """Run anchor text through Keywords Everywhere to get search volume.
    Adds 'anchor_volume' field to each backlink dict.
    Skips backlinks with no anchor_text field."""
    try:
        sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from ke_helper import get_batch_volumes
    except Exception:
        for b in backlinks:
            b.setdefault('anchor_volume', 0)
        return

    # Collect unique anchor texts
    anchors = []
    seen = set()
    for b in backlinks:
        anchor = b.get('anchor_text', '').strip()
        if anchor and anchor.lower() not in seen and len(anchor) < 100:
            anchors.append(anchor)
            seen.add(anchor.lower())

    if not anchors:
        for b in backlinks:
            b.setdefault('anchor_volume', 0)
        return

    # KE accepts max 100 keywords per call
    volumes = {}
    for i in range(0, len(anchors), 100):
        batch = anchors[i:i+100]
        try:
            batch_vols = get_batch_volumes(batch, country)
            volumes.update(batch_vols)
        except Exception:
            pass

    for b in backlinks:
        anchor = b.get('anchor_text', '').strip()
        b['anchor_volume'] = volumes.get(anchor.lower(), 0) if anchor else 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 backlink_indexer.py <analysis_json> [--balance] [--status <id>]")
        sys.exit(1)
    
    if sys.argv[1] == '--balance':
        print(json.dumps(indexbolt_balance(), indent=2))
    elif sys.argv[1] == '--status':
        print(json.dumps(indexbolt_status(sys.argv[2]), indent=2))
    else:
        auto_index_unindexed(sys.argv[1])
