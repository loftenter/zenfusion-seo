#!/usr/bin/env python3
"""
Fetch all T1 backlinks for a brand from DataForSEO and store in the Zenfusion DB.
Also serves as the monthly update mechanism — marks stale links, adds new ones.

Usage:
  python3 sync_dataforseo_backlinks.py --brand-id 1
  python3 sync_dataforseo_backlinks.py --brand softpro
"""

import os, sys, json, base64, urllib.request, sqlite3, time
from datetime import datetime
from urllib.parse import urlparse

DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")
ENV_PATH = os.path.expanduser("~/.hermes/.env")

# ── Credentials ──────────────────────────────────────────────────────────────

def get_creds():
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATAFORSEO_LOGIN="):
                        login = line.split("=", 1)[1]
                    elif line.startswith("DATAFORSEO_PASSWORD="):
                        password = line.split("=", 1)[1]
    if not login or not password:
        print("ERROR: DataForSEO credentials not found")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
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
            print(f"  API error: {data.get('status_message', 'Unknown')}")
            return None
        return data
    except Exception as e:
        print(f"  Request error: {e}")
        return None


# ── Fetching ─────────────────────────────────────────────────────────────────

def normalize_domain(d):
    if not d:
        return ''
    d = d.lower().strip()
    if d.startswith('http://') or d.startswith('https://'):
        d = urlparse(d).netloc
    if d.startswith('www.'):
        d = d[4:]
    return d


def fetch_all_backlinks(domain, limit_per_call=1000, max_total=50000):
    """Fetch backlinks in batches using offset pagination.
    DataForSEO backlinks/live supports offset for pagination.
    """
    all_backlinks = []
    offset = 0
    
    while offset < max_total:
        payload = {
            'target': domain,
            'mode': 'as_is',
            'limit': limit_per_call,
            'offset': offset,
            'include_subdomains': True,
        }
        
        print(f"  Fetching batch: offset={offset}, limit={limit_per_call}...")
        data = dfs_api_call('backlinks/backlinks/live', payload)
        
        if not data or not data.get('tasks'):
            break
        
        task = data['tasks'][0]
        if task.get('status_code') != 20000:
            print(f"  Task error: {task.get('status_message', 'Unknown')}")
            break
        
        result = task.get('result', [{}])[0]
        items = result.get('items', [])
        
        if not items:
            print(f"  No more items returned.")
            break
        
        for item in items:
            links_type = item.get('type', '')
            all_backlinks.append({
                'url': item.get('url_from', ''),
                'source_domain': normalize_domain(item.get('domain_from', '')),
                'target_url': item.get('url_to', ''),
                'anchor_text': item.get('anchor', '') or '',
                'link_type': links_type,
                'first_seen': (item.get('first_seen', '') or '')[:10],
                'last_seen': (item.get('last_seen', '') or '')[:10],
                'domain_power': item.get('domain_from_rank', 0) or 0,
                'response_code': item.get('status_code', 200) or 200,
            })
        
        total_available = result.get('total_count', len(all_backlinks))
        print(f"  Got {len(items)} items (total so far: {len(all_backlinks)} / {total_available:,})")
        
        offset += limit_per_call
        
        # Rate limit
        time.sleep(0.3)
        
        if len(items) < limit_per_call:
            break
    
    return all_backlinks


# ── DB Storage ───────────────────────────────────────────────────────────────

def get_brand_domain(conn, brand_id):
    c = conn.cursor()
    c.execute("SELECT domain FROM brands WHERE id=?", (brand_id,))
    row = c.fetchone()
    return row[0] if row else None


def sync_to_db(conn, brand_id, brand_domain, backlinks):
    """Insert/update backlinks in DB. Mark existing as updated, insert new ones."""
    c = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    
    # Get existing URLs in DB for this brand (for dedup)
    c.execute("SELECT url FROM backlinks WHERE brand_id=? AND url IS NOT NULL AND url != ''", (brand_id,))
    existing_urls = set(r[0] for r in c.fetchall())
    
    # Also get existing by source_domain + target_url combo
    c.execute("SELECT source_domain, url FROM backlinks WHERE brand_id=?", (brand_id,))
    existing_pairs = set((r[0], r[1]) for r in c.fetchall() if r[0] and r[1])
    
    new_count = 0
    updated_count = 0
    skipped_count = 0
    
    for bl in backlinks:
        url = bl['url']
        source_domain = bl['source_domain']
        
        if not url or not source_domain:
            skipped_count += 1
            continue
        
        # Check if this URL already exists
        if url in existing_urls or (source_domain, url) in existing_pairs:
            # Update the existing record with fresh data
            c.execute("""UPDATE backlinks SET 
                anchor_text=?, domain_power=?, link_type=?, 
                first_seen=?, last_seen=?, response_code=?,
                tool_source='DataForSEO', tool_date=?
                WHERE brand_id=? AND url=?""",
                (bl['anchor_text'], bl['domain_power'], bl['link_type'],
                 bl['first_seen'], bl['last_seen'], bl['response_code'],
                 now, brand_id, url))
            updated_count += 1
        else:
            # Insert new backlink
            c.execute("""INSERT INTO backlinks 
                (brand_id, url, source_domain, anchor_text, link_type, 
                 first_seen, last_seen, domain_power, response_code, 
                 tier, tool_source, tool_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'DataForSEO', ?, ?)""",
                (brand_id, url, source_domain, bl['anchor_text'], bl['link_type'],
                 bl['first_seen'], bl['last_seen'], bl['domain_power'], bl['response_code'],
                 now, now))
            new_count += 1
            existing_urls.add(url)
    
    conn.commit()
    
    print(f"\n  DB Sync Results:")
    print(f"    New backlinks inserted: {new_count:,}")
    print(f"    Existing backlinks updated: {updated_count:,}")
    print(f"    Skipped (no URL/domain): {skipped_count}")
    
    return new_count, updated_count, skipped_count


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sync DataForSEO backlinks to Zenfusion DB")
    parser.add_argument("--brand-id", type=int, default=1, help="Brand ID")
    parser.add_argument("--max", type=int, default=50000, help="Max backlinks to fetch")
    args = parser.parse_args()
    
    print(f"\n{'='*70}")
    print(f"  DATAFORSEO BACKLINK SYNC")
    print(f"{'='*70}")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Get brand domain
    brand_domain = get_brand_domain(conn, args.brand_id)
    if not brand_domain:
        print(f"ERROR: Brand not found with id={args.brand_id}")
        sys.exit(1)
    
    print(f"\n  Brand: {brand_domain} (ID: {args.brand_id})")
    
    # Current DB state
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM backlinks WHERE brand_id=?", (args.brand_id,))
    current_count = c.fetchone()[0]
    print(f"  Current backlinks in DB: {current_count:,}")
    
    # Fetch from DataForSEO
    print(f"\n  Fetching backlinks from DataForSEO (max {args.max:,})...")
    backlinks = fetch_all_backlinks(brand_domain, max_total=args.max)
    print(f"\n  Total fetched: {len(backlinks):,}")
    
    if not backlinks:
        print("  No backlinks fetched. Exiting.")
        conn.close()
        return
    
    # Sync to DB
    print(f"\n  Syncing to database...")
    new_count, updated_count, skipped = sync_to_db(conn, args.brand_id, brand_domain, backlinks)
    
    # Final count
    c.execute("SELECT COUNT(*) FROM backlinks WHERE brand_id=?", (args.brand_id,))
    final_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(DISTINCT source_domain) FROM backlinks WHERE brand_id=?", (args.brand_id,))
    unique_domains = c.fetchone()[0]
    
    print(f"\n  Final DB state:")
    print(f"    Total backlinks: {final_count:,}")
    print(f"    Unique source domains: {unique_domains:,}")
    print(f"    DataForSEO tool_source entries: {final_count - current_count + updated_count:,}")
    
    conn.close()
    
    print(f"\n{'='*70}")
    print(f"  SYNC COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
