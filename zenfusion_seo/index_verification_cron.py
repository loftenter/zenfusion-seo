#!/usr/bin/env python3
"""
Index Verification + Re-Submission Cron
=======================================
Checks URLs submitted to Google/IndexBolt/Omega 5+ days ago.
If still not indexed, re-submits to the next indexer in the chain.

Logic:
1. Query DB for backlinks where google_indexed = False AND submitted_at >= 5 days ago
2. Re-check Google index status via DataForSEO site: query
3. If still not indexed → submit to next indexer in chain
4. Log results

Chain: Google Indexing API → IndexBolt → Omega → (retry Google after 7 more days)

Usage (cron):
    python3 index_verification_cron.py [--days 5] [--limit 500]
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import json, os, sys, base64, urllib.request, time, sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))

# Load env
env_path = os.path.expanduser('~/.hermes/.env')
login, password, ib_key = '', '', ''
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line.startswith('DATAFORSEO_LOGIN='): login = line.split('=', 1)[1]
        elif line.startswith('DATAFORSEO_PASSWORD='): password = line.split('=', 1)[1]
        elif line.startswith('INDEXBOLT_API_KEY='): ib_key = line.split('=', 1)[1]

creds = base64.b64encode(f"{login}:{password}".encode()).decode()

def dfs_api(endpoint, payload):
    url = f"https://api.dataforseo.com/v3/{endpoint}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return _fix_goto(json.loads(resp.read()), endpoint)

def check_google_indexed(url):
    """Check if a URL is indexed in Google using DataForSEO SERP API."""
    try:
        result = dfs_api("serp/google/organic/live/advanced", [{
            "keyword": f"site:{url}",
            "location_name": "United States",
            "language_code": "en",
            "device": "desktop",
            "use_improved_etv": True,
            "limit": 1
        }])
        if result.get('status_code') == 20000:
            task = result['tasks'][0]
            if task.get('status_code') == 20000:
                items = task['result'][0].get('items', [])
                return len(items) > 0
        return False
    except:
        return False

def submit_to_google_indexing(url):
    """Submit URL to Google Indexing API (free)."""
    try:
        import google_indexing_helper
        result = google_indexing_helper.submit_url(url)
        return result['success']
    except:
        return False

def submit_to_indexbolt(urls, name):
    """Submit URLs to IndexBolt (costs credits)."""
    try:
        headers = {
            "Authorization": f"Bearer {ib_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://www.indexbolt.com",
            "Referer": "https://www.indexbolt.com/",
        }
        data = json.dumps({"urls": urls, "indexingType": "normal", "submissionName": name}).encode()
        req = urllib.request.Request("https://www.indexbolt.com/api/v1/submit",
            data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result['data']['totalUrls']
    except:
        return 0

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=5, help='Days since last submission to check')
    ap.add_argument('--limit', type=int, default=500, help='Max URLs to check per run')
    ap.add_argument('--submit-method', default='auto', choices=['auto', 'google', 'indexbolt'],
                    help='Which indexer to use for re-submission')
    args = ap.parse_args()

    db_path = os.path.expanduser('~/.hermes/zenfusion.db')
    db = sqlite3.connect(db_path)
    cursor = db.cursor()

    # Ensure submitted_at column exists
    try:
        cursor.execute('ALTER TABLE backlinks ADD COLUMN submitted_at TEXT')
    except:
        pass
    try:
        cursor.execute('ALTER TABLE backlinks ADD COLUMN submitted_via TEXT')
    except:
        pass
    try:
        cursor.execute('ALTER TABLE backlinks ADD COLUMN verification_count INTEGER DEFAULT 0')
    except:
        pass

    db.commit()

    # Find URLs that were submitted 5+ days ago and are still not indexed
    cutoff = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute("""
        SELECT id, url, source_domain, submitted_via, submitted_at, verification_count
        FROM backlinks 
        WHERE (google_indexed = 0 OR google_indexed IS NULL)
        AND submitted_at IS NOT NULL 
        AND submitted_at <= ?
        AND (verification_count < 3)
        ORDER BY submitted_at ASC
        LIMIT ?
    """, (cutoff, args.limit))

    urls_to_check = cursor.fetchall()
    
    if not urls_to_check:
        print(f"[{datetime.now()}] No URLs due for verification (checked backlinks submitted before {cutoff})")
        db.close()
        return

    print(f"\n=== Index Verification Cron ===")
    print(f"URLs to check: {len(urls_to_check)}")
    print(f"Days since submission: {args.days}+")
    print(f"Date: {datetime.now()}")
    print()

    still_not_indexed = []
    now_indexed = 0
    re_submitted_google = 0
    re_submitted_indexbolt = 0
    errors = 0

    for i, (bl_id, url, domain, submitted_via, submitted_at, ver_count) in enumerate(urls_to_check):
        # Check if now indexed
        is_indexed = check_google_indexed(url)

        if is_indexed:
            cursor.execute('UPDATE backlinks SET google_indexed = 1 WHERE id = ?', (bl_id,))
            now_indexed += 1
            print(f"  [{i+1}/{len(urls_to_check)}] ✅ NOW INDEXED: {url[:60]}")
        else:
            still_not_indexed.append((bl_id, url, submitted_via or 'unknown', ver_count or 0))
            print(f"  [{i+1}/{len(urls_to_check)}] ❌ Still not indexed: {url[:60]}")

        # Update verification count
        cursor.execute('UPDATE backlinks SET verification_count = COALESCE(verification_count, 0) + 1 WHERE id = ?', (bl_id,))
        
        db.commit()

        if (i+1) % 50 == 0:
            print(f"\n  Progress: {now_indexed} indexed, {len(still_not_indexed)} still not indexed\n")

        time.sleep(0.3)  # Rate limit DataForSEO

    print(f"\n=== VERIFICATION RESULTS ===")
    print(f"  Now indexed: {now_indexed}")
    print(f"  Still not indexed: {len(still_not_indexed)}")

    # Re-submit still-not-indexed URLs to next indexer in chain
    if still_not_indexed:
        print(f"\n=== RE-SUBMITTING {len(still_not_indexed)} URLs ===")

        # Try Google Indexing API first (free) for ALL unindexed URLs
        # If that fails (Python/LibreSSL incompatibility), fall back to IndexBolt
        urls_to_resubmit = [(bl_id, url) for bl_id, url, via, vc in still_not_indexed]

        # 1. Try Google Indexing API (free)
        google_success_ids = []
        if urls_to_resubmit:
            print(f"\n  Google Indexing API: {len(urls_to_resubmit)} URLs...")
            for bid, url in urls_to_resubmit:
                success = submit_to_google_indexing(url)
                if success:
                    re_submitted_google += 1
                    google_success_ids.append(bid)
                    cursor.execute('UPDATE backlinks SET submitted_via = ?, submitted_at = ? WHERE id = ?',
                        ('Google Indexing API (retry)', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), bid))
                time.sleep(0.3)
            print(f"    Submitted: {re_submitted_google}/{len(urls_to_resubmit)}")

        # 2. Fallback: re-submit to IndexBolt any URLs that Google API couldn't handle
        failed_google_ids = [bid for bid, url in urls_to_resubmit if bid not in google_success_ids]
        if failed_google_ids:
            # Look up URLs for failed IDs
            failed_urls = [(bid, url) for bid, url in urls_to_resubmit if bid not in google_success_ids]
            print(f"\n  IndexBolt (fallback): {len(failed_urls)} URLs (Google API failed)...")
            url_list = [url for bid, url in failed_urls]
            for batch_start in range(0, len(url_list), 50):
                batch = url_list[batch_start:batch_start+50]
                batch_ids = [bid for bid, url in failed_urls[batch_start:batch_start+50]]
                submitted = submit_to_indexbolt(batch, f"Verification retry {batch_start//50+1}")
                re_submitted_indexbolt += submitted
                if submitted > 0:
                    for bid in batch_ids:
                        cursor.execute('UPDATE backlinks SET submitted_via = ?, submitted_at = ? WHERE id = ?',
                            ('IndexBolt (retry)', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), bid))
                time.sleep(2)  # Avoid IndexBolt rate limit
            print(f"    Submitted: {re_submitted_indexbolt}/{len(failed_urls)}")

        db.commit()

    print(f"\n=== CRON COMPLETE ===")
    print(f"  Checked: {len(urls_to_check)}")
    print(f"  Now indexed: {now_indexed}")
    print(f"  Still not indexed: {len(still_not_indexed)}")
    print(f"  Re-submitted to Google: {re_submitted_google}")
    print(f"  Re-submitted to IndexBolt: {re_submitted_indexbolt}")
    print(f"  Date: {datetime.now()}")

    db.close()


if __name__ == '__main__':
    main()
