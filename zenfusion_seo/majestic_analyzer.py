#!/usr/bin/env python3
"""
Zenfusion Majestic Million Analyzer

Analyzes backlink profiles against the Majestic Million list to identify:
1. Which referring domains are in the Majestic Million (strong links)
2. Which Majestic Million domains are NOT yet linking to you (opportunities)
3. Tier 1/2/3 link building priorities

Usage:
  python3 majestic_analyzer.py <domain> [--competitors domain1,domain2]
  python3 majestic_analyzer.py --check-links <csv_file>  (check new links against Majestic Million)

Data files:
  - ~/.hermes/audits/factor-engine-data/majestic_million_index.json (domain -> rank lookup)
  - DataForSEO API for backlink data (credentials in ~/.hermes/.env)
"""

import sys, os, json, base64, urllib.request, csv, time
from urllib.parse import urlparse

MM_CSV = os.path.expanduser('~/Downloads/majestic_million.csv')
MM_INDEX = os.path.expanduser('~/Downloads/majestic_million.csv')

def load_majestic():
    """Load the Majestic Million CSV into a dict."""
    # Try the quick JSON index first
    json_path = os.path.expanduser('~/.hermes/audits/factor-engine-data/majestic_million_index.json')
    if os.path.exists(json_path):
        with open(json_path) as f:
            return json.load(f)
    
    # Fall back to CSV
    if not os.path.exists(MM_CSV):
        print("ERROR: Majestic Million CSV not found at", MM_CSV)
        sys.exit(1)
    
    db = {}
    with open(MM_CSV, 'r', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row.get('Domain', '').lower().strip()
            if domain:
                try:
                    db[domain] = int(row.get('GlobalRank', 0))
                except:
                    pass
    
    # Save quick index
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, 'w') as f:
        json.dump(db, f)
    
    return db

def get_creds():
    """Load DataForSEO credentials from .env."""
    env_path = os.path.expanduser('~/.hermes/.env')
    login, password = '', ''
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('DATAFORSEO_LOGIN='): login = line.split('=', 1)[1]
            elif line.startswith('DATAFORSEO_PASSWORD='): password = line.split('=', 1)[1]
    return login, password

def fetch_backlinks(domain):
    """Fetch backlink referring domains via DataForSEO."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    
    # Use the backlinks summary endpoint first for totals
    url = 'https://api.dataforseo.com/v3/backlinks/summary/live'
    payload = json.dumps([{
        'target': domain,
        'main_domain': domain,
        'search_mode': 'as_is'
    }]).encode()
    
    req = urllib.request.Request(url, data=payload, headers={
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get('tasks') and data['tasks'][0].get('result'):
            return data['tasks'][0]['result'][0]
    except Exception as e:
        print(f"Error fetching backlinks for {domain}: {e}")
    return None

def fetch_referring_domains(domain, limit=1000):
    """Fetch actual referring domain list via DataForSEO."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    
    url = 'https://api.dataforseo.com/v3/backlinks/referring_domains/live'
    payload = json.dumps([{
        'target': domain,
        'main_domain': domain,
        'search_mode': 'as_is',
        'limit': limit,
    }]).encode()
    
    req = urllib.request.Request(url, data=payload, headers={
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json'
    })
    
    referring_domains = set()
    backlinks_list = []
    
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        if data.get('tasks') and data['tasks'][0].get('result'):
            result = data['tasks'][0]['result'][0]
            items = result.get('items', [])
            for item in items:
                ref_domain = item.get('domain', '').lower()
                if ref_domain:
                    referring_domains.add(ref_domain)
                    backlinks_list.append({
                        'url_from': item.get('url', ''),
                        'domain_from': ref_domain,
                        'title': item.get('title', '')[:60],
                        'backlinks': item.get('backlinks', 0),
                        'first_seen': item.get('first_seen', '')[:10],
                        'last_seen': item.get('last_seen', '')[:10],
                    })
    except Exception as e:
        print(f"Error fetching referring domains: {e}")
    
    return referring_domains, backlinks_list

def analyze_domain(domain, majestic_db):
    """Analyze a domain's backlinks against Majestic Million."""
    print(f"\n{'='*70}")
    print(f"  MAJESTIC MILLION ANALYSIS: {domain}")
    print(f"{'='*70}")
    
    # Check if the domain itself is in Majestic Million
    domain_rank = majestic_db.get(domain.lower())
    if domain_rank:
        print(f"\n  {domain} is IN the Majestic Million: Rank #{domain_rank}")
    else:
        print(f"\n  {domain} is NOT in the Majestic Million")
    
    # Fetch backlink summary
    print(f"\n  Fetching backlink data via DataForSEO...")
    summary = fetch_backlinks(domain)
    if summary:
        print(f"  Total backlinks: {summary.get('backlinks', 0):,}")
        print(f"  Referring domains: {summary.get('referring_domains', 0):,}")
    
    # Fetch referring domains
    print(f"  Fetching referring domains (top 1000 backlinks)...")
    ref_domains, backlinks = fetch_referring_domains(domain)
    print(f"  Found {len(ref_domains):,} unique referring domains")
    
    # Match against Majestic Million
    majestic_matches = []
    for rd in ref_domains:
        rank = majestic_db.get(rd)
        if rank:
            majestic_matches.append({'domain': rd, 'majestic_rank': rank})
    
    majestic_matches.sort(key=lambda x: x['majestic_rank'])
    
    print(f"\n  MATCHED IN MAJESTIC MILLION: {len(majestic_matches)}")
    print(f"  ({len(majestic_matches) / max(len(ref_domains), 1) * 100:.1f}% of referring domains)")
    
    if majestic_matches:
        print(f"\n  Top 20 Majestic Million referring domains:")
        print(f"  {'Majestic Rank':>15}  {'Domain':<40}")
        print(f"  {'-'*15}  {'-'*40}")
        for m in majestic_matches[:20]:
            print(f"  #{m['majestic_rank']:>13,}  {m['domain']:<40}")
    
    return {
        'domain': domain,
        'in_majestic': domain_rank is not None,
        'majestic_rank': domain_rank,
        'total_ref_domains': len(ref_domains),
        'majestic_matches': len(majestic_matches),
        'top_majestic_referrers': majestic_matches[:50],
    }

def check_links_csv(csv_path, majestic_db):
    """Check a list of new links against the Majestic Million."""
    print(f"\n{'='*70}")
    print(f"  CHECKING NEW LINKS AGAINST MAJESTIC MILLION")
    print(f"{'='*70}")
    
    # Read CSV — try to find a domain/URL column
    domains_to_check = []
    with open(csv_path, 'r', errors='replace') as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        
        # Find the domain column
        domain_col = None
        url_col = None
        for i, h in enumerate(headers):
            if h.lower().strip() in ['domain', 'referring_domain', 'source_domain']:
                domain_col = i
            elif h.lower().strip() in ['url', 'source_url', 'backlink_url', 'link']:
                url_col = i
        
        for row in reader:
            if domain_col is not None and domain_col < len(row):
                domains_to_check.append(row[domain_col].lower().strip())
            elif url_col is not None and url_col < len(row):
                parsed = urlparse(row[url_col])
                if parsed.netloc:
                    domains_to_check.append(parsed.netloc.replace('www.', '').lower())
    
    print(f"  Found {len(domains_to_check)} links to check\n")
    
    matched = []
    unmatched = []
    for d in domains_to_check:
        rank = majestic_db.get(d)
        if rank:
            matched.append({'domain': d, 'majestic_rank': rank})
        else:
            unmatched.append(d)
    
    matched.sort(key=lambda x: x['majestic_rank'])
    
    print(f"  MATCHED IN MAJESTIC MILLION: {len(matched)} / {len(domains_to_check)}")
    print(f"  ({len(matched) / max(len(domains_to_check), 1) * 100:.1f}%)")
    
    if matched:
        print(f"\n  These links ARE on the Majestic Million — build Tier 1/2/3 to them:")
        print(f"  {'Majestic Rank':>15}  {'Domain':<40}")
        print(f"  {'-'*15}  {'-'*40}")
        for m in matched:
            print(f"  #{m['majestic_rank']:>13,}  {m['domain']:<40}")
    
    if unmatched:
        print(f"\n  NOT in Majestic Million ({len(unmatched)} domains):")
        for d in unmatched[:20]:
            print(f"    {d}")
        if len(unmatched) > 20:
            print(f"    ... and {len(unmatched) - 20} more")
    
    # Output for tier building
    if matched:
        print(f"\n  === TIER BUILDING PRIORITY LIST ===")
        print(f"  Priority: Build links TO these URLs (they're on strong domains)")
        print(f"  These are your Tier 1 money site links — build Tier 2/3 to boost them")
        for m in matched:
            print(f"    Tier 1: {m['domain']} (Majestic #{m['majestic_rank']:,})")

def find_opportunities(domain, competitors, majestic_db):
    """Find Majestic Million domains linking to competitors but NOT to you."""
    print(f"\n{'='*70}")
    print(f"  LINK OPPORTUNITY ANALYSIS")
    print(f"  Target: {domain}")
    print(f"  Competitors: {', '.join(competitors)}")
    print(f"{'='*70}")
    
    # Get your referring domains
    your_refs, _ = fetch_referring_domains(domain)
    print(f"\n  Your referring domains: {len(your_refs):,}")
    
    # Get competitor referring domains
    comp_refs = {}
    for comp in competitors:
        print(f"  Fetching {comp} referring domains...", end='', flush=True)
        refs, _ = fetch_referring_domains(comp)
        comp_refs[comp] = refs
        print(f" {len(refs):,}")
    
    # Find domains linking to competitors but NOT to you
    opportunities = set()
    for comp, refs in comp_refs.items():
        for ref in refs:
            if ref not in your_refs:
                rank = majestic_db.get(ref)
                if rank:
                    opportunities.add((ref, rank, comp))
    
    # Sort by Majestic rank
    opp_list = sorted(opportunities, key=lambda x: x[1])
    
    print(f"\n  MAJESTIC MILLION DOMAINS LINKING TO COMPETITORS BUT NOT YOU: {len(opp_list)}")
    
    if opp_list:
        print(f"\n  {'Majestic Rank':>15}  {'Domain':<35}  {'Links To':<20}")
        print(f"  {'-'*15}  {'-'*35}  {'-'*20}")
        for domain_name, rank, comp in opp_list[:30]:
            print(f"  #{rank:>13,}  {domain_name:<35}  {comp:<20}")
    
    return opp_list

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 majestic_analyzer.py <domain> [--competitors comp1,comp2]")
        print("  python3 majestic_analyzer.py --check-links <csv_file>")
        print("  python3 majestic_analyzer.py --opportunities <domain> --competitors comp1,comp2")
        sys.exit(1)
    
    print("Loading Majestic Million database...")
    start = time.time()
    majestic_db = load_majestic()
    print(f"Loaded {len(majestic_db):,} domains in {time.time() - start:.1f}s")
    
    if sys.argv[1] == '--check-links':
        if len(sys.argv) < 3:
            print("ERROR: --check-links requires a CSV file path")
            sys.exit(1)
        check_links_csv(sys.argv[2], majestic_db)
    
    elif sys.argv[1] == '--opportunities':
        if len(sys.argv) < 3:
            print("ERROR: --opportunities requires a domain")
            sys.exit(1)
        domain = sys.argv[2]
        competitors = []
        if '--competitors' in sys.argv:
            idx = sys.argv.index('--competitors')
            if idx + 1 < len(sys.argv):
                competitors = sys.argv[idx + 1].split(',')
        find_opportunities(domain, competitors, majestic_db)
    
    else:
        domain = sys.argv[1]
        competitors = []
        if '--competitors' in sys.argv:
            idx = sys.argv.index('--competitors')
            if idx + 1 < len(sys.argv):
                competitors = sys.argv[idx + 1].split(',')
        
        result = analyze_domain(domain, majestic_db)
        
        if competitors:
            find_opportunities(domain, competitors, majestic_db)

if __name__ == '__main__':
    main()
