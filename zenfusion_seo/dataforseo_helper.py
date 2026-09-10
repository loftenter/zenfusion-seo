#!/usr/bin/env python3
"""
DataForSEO API Helper for Zenfusion Hermes Agent
Provides keyword research, SERP analysis, and competitor data.

Usage:
  python3 dataforseo_helper.py <command> [args]

Commands:
  serp <keyword> [location]      - Get live SERP results for a keyword
  keywords <keyword> [location]  - Get keyword ideas with search volume
  backlinks <domain>              - Get backlink profile for a domain
  competitors <domain>            - Get organic competitors for a domain
  content <keyword>               - Get content analysis for a keyword
  status                          - Check API account status

Environment variables:
  DATAFORSEO_LOGIN    - API login email
  DATAFORSEO_PASSWORD - API password
"""

import os, sys, json, base64, urllib.request


def normalize_domain(domain):
    """Normalize a domain: strip protocol, www, trailing slash. Return clean domain."""
    if not domain:
        return domain
    d = domain.lower().strip()
    d = d.replace('https://', '').replace('http://', '')
    if d.startswith('www.'):
        d = d[4:]
    d = d.strip('/')
    return d


def resolve_domain_for_api(domain):
    """Check if domain works. If not, try www.domain.com. Return clean domain for API calls."""
    import urllib.request as _urllib
    clean = normalize_domain(domain)
    for candidate in [clean, 'www.' + clean]:
        try:
            req = _urllib.request.Request('https://' + candidate, headers={'User-Agent': 'Mozilla/5.0'}, method='HEAD')
            _urllib.request.urlopen(req, timeout=5)
            return clean
        except:
            try:
                req = _urllib.request.Request('https://' + candidate, headers={'User-Agent': 'Mozilla/5.0'})
                _urllib.request.urlopen(req, timeout=5)
                return clean
            except:
                continue
    return clean


def get_creds():

    """Load credentials from environment or .env file."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    
    if not login or not password:
        # Try loading from .env
        env_path = os.path.expanduser('~/.hermes/.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
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


def api_call(endpoint, payload=None, method='GET'):
    """Make a DataForSEO API call."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'https://api.dataforseo.com/v3/{endpoint}'
    
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json'
    }
    
    if method == 'POST' and payload is not None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            print(f"API Error: {data.get('status_code')} - {data.get('status_message')}")
            return None
        return data
    except Exception as e:
        print(f"Request error: {e}")
        return None


def cmd_serp(keyword, location_code=2840):
    """Get live SERP results for a keyword."""
    # 2840 = United States
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': 'en',
        'device': 'desktop',
        'os': 'windows',
        'use_improved_etv': True
    }]
    
    data = api_call('serp/google/organic/live/advanced', payload, 'POST')  # use_improved_etv added in payload builder above
    if not data or not data.get('tasks'):
        return
    
    result = data['tasks'][0].get('result', [{}])[0]
    items = result.get('items', [])
    
    print(f"SERP Results for: '{keyword}'")
    print(f"Total results: {result.get('result_count', 'N/A')}")
    print(f"Items returned: {len(items)}")
    print()
    
    for item in items:
        item_type = item.get('type', 'unknown')
        if item_type == 'organic':
            print(f"  #{item.get('rank_group', '?')} [{item_type}] {item.get('domain', '?')}")
            print(f"    Title: {item.get('title', '')[:80]}")
            print(f"    URL: {item.get('url', '')[:100]}")
            print(f"    Description: {item.get('description', '')[:120]}")
            print()
        elif item_type == 'featured_snippet':
            print(f"  [FEATURED SNIPPET] {item.get('domain', '?')}")
            print(f"    Title: {item.get('title', '')[:80]}")
            print()
        elif item_type == 'local_pack':
            print(f"  [LOCAL PACK] {item.get('title', '')[:80]}")
            print()
        elif item_type == 'people_also_ask':
            print(f"  [PEOPLE ALSO ASK] {item.get('title', '')[:80]}")
            print()
        elif item_type in ('video', 'image', 'news'):
            print(f"  [{item_type.upper()}] {item.get('title', '')[:80]}")
            print()


def cmd_keywords(keyword, location_code=2840):
    """Get keyword ideas with search volume."""
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': 'en'
    }]
    
    data = api_call('keywords_data/google_ads/search_volume/live', payload, 'POST')
    if not data or not data.get('tasks'):
        return
    
    result = data['tasks'][0].get('result', [{}])
    if not result:
        print("No keyword data returned")
        return
    
    print(f"Keyword Ideas for: '{keyword}'")
    print()
    
    for item in result[:20]:
        kw = item.get('keyword', 'N/A')
        volume = item.get('search_volume', 'N/A')
        cpc = item.get('cpc', 'N/A')
        competition = item.get('competition', 'N/A')
        
        print(f"  {kw:<40} vol={volume:<8} cpc={cpc:<8} comp={competition}")


def cmd_backlinks(domain):
    """Get backlink profile for a domain."""
    payload = [{
        'target': domain,
        'main_domain': domain,
        'search_mode': 'as_is'
    }]
    
    data = api_call('backlinks/summary/live', payload, 'POST')
    if not data or not data.get('tasks'):
        return
    
    result = data['tasks'][0].get('result', [{}])
    if not result:
        print("No backlink data returned")
        return
    
    for item in result:
        print(f"Backlink Profile: {domain}")
        print(f"  Total backlinks: {item.get('backlinks', 'N/A')}")
        print(f"  Referring domains: {item.get('referring_domains', 'N/A')}")
        print(f"  Referring main domains: {item.get('referring_main_domains', 'N/A')}")
        print(f"  Total referring IPs: {item.get('referring_ips', 'N/A')}")
        print(f"  Backlinks (dofollow): {item.get('backlinks_dofollow', 'N/A')}")
        print(f"  Backlinks (nofollow): {item.get('backlinks_nofollow', 'N/A')}")


def cmd_competitors(domain, location_code=2840):
    """Get organic competitors for a domain."""
    payload = [{
        'target': domain,
        'location_code': location_code,
        'language_code': 'en'
    }]
    
    data = api_call('dataforseo_labs/google/competitors/live', payload, 'POST')
    if not data or not data.get('tasks'):
        return
    
    result = data['tasks'][0].get('result', [{}])
    if not result:
        print("No competitor data returned")
        return
    
    for item in result.get('items', [])[:10]:
        comp = item.get('domain', 'N/A')
        avg_pos = item.get('avg_position', 'N/A')
        visibility = item.get('visibility', 'N/A')
        keywords = item.get('se_keywords', 'N/A')
        
        print(f"  {comp:<35} pos={avg_pos:<8} vis={visibility:<8} keywords={keywords}")


def cmd_status():
    """Check API account status."""
    data = api_call('appendix/user_data')
    if not data:
        print("Failed to get account status")
        return
    
    results = data.get('results', [])
    if not results:
        print(f"Status: {data.get('status_message')}")
        return
    
    for item in results:
        print(f"Account: {item.get('email', 'N/A')}")
        for bal in item.get('balances', []):
            print(f"  Balance: {bal.get('value', '?')} {bal.get('currency', '?')}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == 'serp':
        keyword = sys.argv[2] if len(sys.argv) > 2 else 'water softener'
        cmd_serp(keyword)
    elif cmd == 'keywords':
        keyword = sys.argv[2] if len(sys.argv) > 2 else 'water softener'
        cmd_keywords(keyword)
    elif cmd == 'backlinks':
        domain = sys.argv[2] if len(sys.argv) > 2 else 'softprowatersystems.com'
        cmd_backlinks(domain)
    elif cmd == 'competitors':
        domain = sys.argv[2] if len(sys.argv) > 2 else 'softprowatersystems.com'
        cmd_competitors(domain)
    elif cmd == 'status':
        cmd_status()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
