#!/usr/bin/env python3
"""
Zenfusion GSC (Google Search Console) Connector

Allows clients to connect their GSC by adding success@zenfusion.com as a user,
then authenticating via OAuth from the portal.

Flow:
1. Client adds success@zenfusion.com to their GSC property
2. Client clicks "Connect GSC" on the portal dashboard
3. OAuth flow runs → saves token to ~/.hermes/gsc_tokens/{brand_slug}.json
4. GSC data available for audits and reports

Usage:
  python3 gsc_connector.py --auth              # Start OAuth flow (returns URL)
  python3 gsc_connector.py --callback CODE     # Handle OAuth callback
  python3 gsc_connector.py --list              # List connected properties
  python3 gsc_connector.py --pull BRAND_SLUG   # Pull search analytics
"""

import os, sys, json, argparse
from datetime import datetime, timedelta

# Google OAuth + Search Console API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# ── Config ──────────────────────────────────────────────────────────────────

ENV_PATH = os.path.expanduser("~/.hermes/.env")
TOKEN_DIR = os.path.expanduser("~/.hermes/gsc_tokens")
DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")

# OAuth config — these need to be created in Google Cloud Console
# https://console.cloud.google.com/apis/credentials
# Create OAuth 2.0 Client ID (Web application)
# Add redirect URI: https://loftenter.github.io/zenfusion-audits/gsc-callback.html
# Then set the client_id and client_secret in ~/.hermes/.env as:
#   GSC_CLIENT_ID=xxxxx.apps.googleusercontent.com
#   GSC_CLIENT_SECRET=xxxxx

SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']

def get_oauth_config():
    """Load OAuth client config from .env."""
    client_id = os.environ.get("GSC_CLIENT_ID", "")
    client_secret = os.environ.get("GSC_CLIENT_SECRET", "")
    
    if not client_id or not client_secret:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GSC_CLIENT_ID="):
                        client_id = line.split("=", 1)[1]
                    elif line.startswith("GSC_CLIENT_SECRET="):
                        client_secret = line.split("=", 1)[1]
    
    return client_id, client_secret


# ── OAuth Flow ──────────────────────────────────────────────────────────────

def start_oauth():
    """Start OAuth flow — returns URL for user to visit."""
    client_id, client_secret = get_oauth_config()
    
    if not client_id or not client_secret:
        print("ERROR: GSC_CLIENT_ID and GSC_CLIENT_SECRET not found in .env")
        print("\nTo set up:")
        print("1. Go to https://console.cloud.google.com/apis/credentials")
        print("2. Create OAuth 2.0 Client ID (Web application)")
        print("3. Add redirect URI: urn:ietf:wg:oauth:2.0:oob")
        print("4. Add to ~/.hermes/.env:")
        print("   GSC_CLIENT_ID=your_client_id.apps.googleusercontent.com")
        print("   GSC_CLIENT_SECRET=your_client_secret")
        return None
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
            }
        },
        scopes=SCOPES,
    )
    
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    
    print(f"\n{'='*60}")
    print(f"  GSC OAuth Flow")
    print(f"{'='*60}")
    print(f"\n1. Visit this URL:")
    print(f"\n{auth_url}\n")
    print(f"2. Authorize with admin@zenfusion.com")
    print(f"3. Copy the authorization code")
    print(f"4. Run: python3 gsc_connector.py --callback CODE")
    
    return auth_url


def handle_callback(code):
    """Handle OAuth callback — exchange code for token."""
    client_id, client_secret = get_oauth_config()
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
            }
        },
        scopes=SCOPES,
    )
    
    flow.fetch_token(code=code)
    creds = flow.credentials
    
    # Save token
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_path = os.path.join(TOKEN_DIR, "global.json")
    
    token_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': list(creds.scopes),
    }
    
    with open(token_path, 'w') as f:
        json.dump(token_data, f, indent=2)
    
    print(f"\n✅ Token saved to {token_path}")
    
    # List available properties
    list_properties(creds)
    
    return creds


def load_credentials():
    """Load saved OAuth credentials."""
    token_path = os.path.join(TOKEN_DIR, "global.json")
    
    if not os.path.exists(token_path):
        return None
    
    with open(token_path) as f:
        token_data = json.load(f)
    
    creds = Credentials(
        token=token_data['token'],
        refresh_token=token_data.get('refresh_token'),
        token_uri=token_data['token_uri'],
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=token_data.get('scopes', SCOPES),
    )
    
    # Refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Save refreshed token
        token_data['token'] = creds.token
        with open(token_path, 'w') as f:
            json.dump(token_data, f, indent=2)
    
    return creds


# ── GSC API calls ───────────────────────────────────────────────────────────

def list_properties(creds=None):
    """List all GSC properties available to the authenticated user."""
    if not creds:
        creds = load_credentials()
    if not creds:
        print("No GSC connection. Run: python3 gsc_connector.py --auth")
        return []
    
    service = build('searchconsole', 'v1', credentials=creds)
    sites = service.sites().list().execute()
    
    properties = []
    for site in sites.get('siteEntry', []):
        properties.append({
            'url': site.get('siteUrl', ''),
            'permission_level': site.get('permissionLevel', ''),
        })
    
    print(f"\n{'='*60}")
    print(f"  Connected GSC Properties ({len(properties)})")
    print(f"{'='*60}")
    for p in properties:
        print(f"  {p['url']} ({p['permission_level']})")
    
    return properties


def pull_search_analytics(domain, days=90, brand_id=None):
    """Pull search analytics for a domain from GSC."""
    creds = load_credentials()
    if not creds:
        print("No GSC connection. Run: python3 gsc_connector.py --auth")
        return None
    
    service = build('searchconsole', 'v1', credentials=creds)
    
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # The site URL in GSC can be https://domain.com/ or sc-domain:domain.com
    # Try both formats
    site_url = f"sc-domain:{domain}"
    
    try:
        request = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['query'],
            'rowLimit': 1000,
            'startRow': 0,
        }
        response = service.searchanalytics().query(
            siteUrl=site_url,
            body=request
        ).execute()
        
        if not response.get('rows'):
            # Try https:// format
            site_url = f"https://{domain}/"
            response = service.searchanalytics().query(
                siteUrl=site_url,
                body=request
            ).execute()
        
        rows = response.get('rows', [])
        
        queries = []
        for row in rows:
            queries.append({
                'query': row['keys'][0],
                'clicks': row.get('clicks', 0),
                'impressions': row.get('impressions', 0),
                'ctr': round(row.get('ctr', 0) * 100, 2),
                'position': round(row.get('position', 0), 1),
            })
        
        queries.sort(key=lambda x: x['clicks'], reverse=True)
        
        total_clicks = sum(q['clicks'] for q in queries)
        total_impressions = sum(q['impressions'] for q in queries)
        avg_position = sum(q['position'] * q['impressions'] for q in queries) / max(total_impressions, 1)
        
        print(f"\n{'='*60}")
        print(f"  GSC Search Analytics: {domain}")
        print(f"  Period: {start_date} to {end_date} ({days} days)")
        print(f"{'='*60}")
        print(f"  Total clicks: {total_clicks:,}")
        print(f"  Total impressions: {total_impressions:,}")
        print(f"  Avg position: {avg_position:.1f}")
        print(f"  Queries: {len(queries)}")
        print(f"\n  Top 20 queries:")
        for i, q in enumerate(queries[:20], 1):
            print(f"    {i:2d}. {q['query'][:50]:<50s} | clicks:{q['clicks']:>5} | impr:{q['impressions']:>6} | pos:{q['position']:>5.1f} | CTR:{q['ctr']:>5.1f}%")
        
        return {
            'domain': domain,
            'start_date': start_date,
            'end_date': end_date,
            'total_clicks': total_clicks,
            'total_impressions': total_impressions,
            'avg_position': round(avg_position, 1),
            'queries': queries,
        }
        
    except Exception as e:
        print(f"Error pulling GSC data: {e}")
        return None


def pull_pages_analytics(domain, days=90):
    """Pull page-level analytics from GSC."""
    creds = load_credentials()
    if not creds:
        return None
    
    service = build('searchconsole', 'v1', credentials=creds)
    
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    site_url = f"sc-domain:{domain}"
    
    try:
        request = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['page'],
            'rowLimit': 1000,
        }
        response = service.searchanalytics().query(
            siteUrl=site_url,
            body=request
        ).execute()
        
        rows = response.get('rows', [])
        pages = []
        for row in rows:
            pages.append({
                'url': row['keys'][0],
                'clicks': row.get('clicks', 0),
                'impressions': row.get('impressions', 0),
                'ctr': round(row.get('ctr', 0) * 100, 2),
                'position': round(row.get('position', 0), 1),
            })
        
        pages.sort(key=lambda x: x['clicks'], reverse=True)
        
        print(f"\n  Top 20 pages:")
        for i, p in enumerate(pages[:20], 1):
            print(f"    {i:2d}. {p['url'][:60]:<60s} | clicks:{p['clicks']:>5} | impr:{p['impressions']:>6}")
        
        return pages
        
    except Exception as e:
        print(f"Error pulling page data: {e}")
        return []


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zenfusion GSC Connector")
    parser.add_argument('--auth', action='store_true', help='Start OAuth flow')
    parser.add_argument('--callback', metavar='CODE', help='OAuth callback code')
    parser.add_argument('--list', action='store_true', help='List connected properties')
    parser.add_argument('--pull', metavar='DOMAIN', help='Pull search analytics for domain')
    parser.add_argument('--days', type=int, default=90, help='Number of days to pull (default 90)')
    args = parser.parse_args()
    
    if args.auth:
        start_oauth()
    elif args.callback:
        handle_callback(args.callback)
    elif args.list:
        list_properties()
    elif args.pull:
        result = pull_search_analytics(args.pull, days=args.days)
        if result:
            pull_pages_analytics(args.pull, days=args.days)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
