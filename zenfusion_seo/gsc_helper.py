"""
Google Search Console (GSC) Helper
====================================
Provides real search performance data (impressions, clicks, CTR, position)
from Google Search Console. Free API — replaces DataForSEO approximations.

Usage:
    from gsc_helper import get_search_analytics, get_top_queries, get_page_performance
    
    # Get top queries for a domain
    queries = get_top_queries('https://qualitywatertreatment.com/')
    
    # Get performance for a specific page
    perf = get_page_performance('https://qualitywatertreatment.com/', 
                                 '/pages/best-water-softener')
    
    # Get performance for a specific keyword
    kw_data = get_keyword_performance('https://qualitywatertreatment.com/',
                                       'best water softener')
"""

import json, os, sys
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

_token_cache = None
_gsc_cache = {}

def _load_credentials():
    """Load and refresh Google OAuth credentials."""
    global _token_cache
    
    if _token_cache and _token_cache.valid:
        return _token_cache
    
    token_path = os.path.expanduser('~/.hermes/google_token.json')
    with open(token_path) as f:
        token_data = json.load(f)
    
    creds = Credentials(
        token=token_data['token'],
        refresh_token=token_data['refresh_token'],
        token_uri=token_data['token_uri'],
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=token_data['scopes'],
    )
    
    if creds.expired or not creds.valid:
        creds.refresh(Request())
        # Save refreshed token
        token_data['token'] = creds.token
        with open(token_path, 'w') as f:
            json.dump(token_data, f, indent=2)
    
    _token_cache = creds
    return creds


def _get_gsc_service():
    """Get GSC API service instance."""
    creds = _load_credentials()
    return build('searchconsole', 'v1', credentials=creds)


def list_properties():
    """List all GSC properties available to the authenticated user."""
    gsc = _get_gsc_service()
    sites = gsc.sites().list().execute()
    return [site['siteUrl'] for site in sites.get('siteEntry', [])]


def get_search_analytics(site_url, dimensions=None, start_date=None, end_date=None,
                          filters=None, row_limit=1000):
    """
    Get search analytics data from GSC.
    
    Args:
        site_url: GSC property URL (e.g. 'https://qualitywatertreatment.com/')
        dimensions: List of dimensions ('query', 'page', 'country', 'device', 'date')
        start_date: YYYY-MM-DD (default: 28 days ago)
        end_date: YYYY-MM-DD (default: today)
        filters: List of filter dicts [{'dimension': 'query', 'operator': 'contains', 'expression': 'water softener'}]
        row_limit: Max rows to return (max 1000)
    
    Returns:
        List of dicts with keys, clicks, impressions, ctr, position
    """
    if not dimensions:
        dimensions = ['query']
    
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')
    if not start_date:
        start_date = (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')
    
    cache_key = f"{site_url}:{','.join(dimensions)}:{start_date}:{end_date}:{str(filters)}:{row_limit}"
    if cache_key in _gsc_cache:
        return _gsc_cache[cache_key]
    
    gsc = _get_gsc_service()
    
    request = {
        'startDate': start_date,
        'endDate': end_date,
        'dimensions': dimensions,
        'rowLimit': min(row_limit, 1000),
    }
    
    if filters:
        request['dimensionFilterGroups'] = [{
            'filters': [{
                'dimension': f['dimension'],
                'operator': f.get('operator', 'contains'),
                'expression': f['expression'],
            } for f in filters]
        }]
    
    response = gsc.searchanalytics().query(siteUrl=site_url, body=request).execute()
    rows = response.get('rows', [])
    
    # Parse into structured data
    results = []
    for row in rows:
        entry = {
            'keys': row['keys'],
            'clicks': row['clicks'],
            'impressions': row['impressions'],
            'ctr': row['ctr'],
            'position': row['position'],
        }
        # Add dimension values as named fields
        for i, dim in enumerate(dimensions):
            entry[dim] = row['keys'][i] if i < len(row['keys']) else None
        results.append(entry)
    
    _gsc_cache[cache_key] = results
    return results


def get_top_queries(site_url, days=28, limit=100):
    """Get top queries by impressions for a site."""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['query'], 
                                start_date=start_date, end_date=end_date,
                                row_limit=limit)
    
    # Sort by impressions
    data.sort(key=lambda x: -x['impressions'])
    return data


def get_top_pages(site_url, days=28, limit=100):
    """Get top pages by impressions for a site."""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['page'],
                                start_date=start_date, end_date=end_date,
                                row_limit=limit)
    
    data.sort(key=lambda x: -x['impressions'])
    return data


def get_keyword_performance(site_url, keyword, days=28):
    """
    Get search performance for a specific keyword.
    
    Returns: {'impressions': N, 'clicks': N, 'ctr': float, 'position': float} or None
    """
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['query'],
                                start_date=start_date, end_date=end_date,
                                filters=[{'dimension': 'query', 'operator': 'equals', 'expression': keyword}],
                                row_limit=1)
    
    if data:
        return {
            'impressions': data[0]['impressions'],
            'clicks': data[0]['clicks'],
            'ctr': data[0]['ctr'],
            'position': data[0]['position'],
        }
    return None


def get_page_performance(site_url, page_path, days=28):
    """
    Get search performance for a specific page URL.
    
    Returns: {'impressions': N, 'clicks': N, 'ctr': float, 'position': float, 'top_queries': [...]} 
    or None
    """
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # Page-level stats
    page_data = get_search_analytics(site_url, dimensions=['page'],
                                     start_date=start_date, end_date=end_date,
                                     filters=[{'dimension': 'page', 'operator': 'contains', 'expression': page_path}],
                                     row_limit=1)
    
    if not page_data:
        return None
    
    result = {
        'impressions': page_data[0]['impressions'],
        'clicks': page_data[0]['clicks'],
        'ctr': page_data[0]['ctr'],
        'position': page_data[0]['position'],
    }
    
    # Top queries for this page
    query_data = get_search_analytics(site_url, dimensions=['query', 'page'],
                                      start_date=start_date, end_date=end_date,
                                      filters=[{'dimension': 'page', 'operator': 'contains', 'expression': page_path}],
                                      row_limit=50)
    
    result['top_queries'] = [
        {'query': q['query'], 'impressions': q['impressions'], 'clicks': q['clicks'],
         'ctr': q['ctr'], 'position': q['position']}
        for q in query_data[:20]
    ]
    
    return result


def get_query_page_combos(site_url, days=28, limit=500):
    """
    Get query + page combination data — shows which queries rank which pages.
    Useful for keyword-to-page mapping.
    """
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['query', 'page'],
                                start_date=start_date, end_date=end_date,
                                row_limit=limit)
    return data


def get_device_breakdown(site_url, days=28):
    """Get performance breakdown by device (desktop, mobile, tablet)."""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['device'],
                                start_date=start_date, end_date=end_date,
                                row_limit=10)
    return data


def get_country_breakdown(site_url, days=28, limit=50):
    """Get performance breakdown by country."""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    data = get_search_analytics(site_url, dimensions=['country'],
                                start_date=start_date, end_date=end_date,
                                row_limit=limit)
    return data


def get_index_status(site_url):
    """Get URL inspection/index status for the site."""
    gsc = _get_gsc_service()
    try:
        # Get sitemap status
        sitemaps = gsc.sitemaps().list(siteUrl=site_url).execute()
        return {
            'sitemaps': [s.get('path', '') for s in sitemaps.get('sitemap', [])],
        }
    except:
        return None
