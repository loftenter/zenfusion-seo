"""
Google Indexing API Helper
===========================
Submits URLs directly to Google for indexing — FREE, no credits needed.
Replaces IndexBolt as the primary indexing method (IndexBolt becomes fallback).

Usage:
    from google_indexing_helper import submit_url, submit_urls, get_url_status
    
    # Submit single URL
    result = submit_url('https://www.softprowatersystems.com/')
    
    # Submit batch (100 per day limit per Google's docs, but we batch in groups)
    results = submit_urls(['https://url1.com', 'https://url2.com'])
    
    # Check if URL has been submitted before
    status = get_url_status('https://www.softprowatersystems.com/')
"""

import json, os, time
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

_token_cache = None

def _load_credentials():
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
        token_data['token'] = creds.token
        with open(token_path, 'w') as f:
            json.dump(token_data, f, indent=2)
    
    _token_cache = creds
    return creds


def _get_indexing_service():
    creds = _load_credentials()
    return build('indexing', 'v3', credentials=creds)


def submit_url(url, notification_type='URL_UPDATED'):
    """
    Submit a single URL to Google for indexing.
    
    Args:
        url: The URL to submit
        notification_type: 'URL_UPDATED' (default) or 'URL_DELETED'
    
    Returns:
        {'success': True, 'url': url} or {'success': False, 'error': str}
    """
    try:
        indexing = _get_indexing_service()
        result = indexing.urlNotifications().publish(
            body={'url': url, 'type': notification_type}
        ).execute()
        return {'success': True, 'url': url, 'response': result}
    except Exception as e:
        return {'success': False, 'url': url, 'error': str(e)}


def submit_urls(urls, batch_delay=0.5):
    """
    Submit multiple URLs to Google for indexing.
    
    Args:
        urls: List of URL strings
        batch_delay: Seconds between submissions (avoid rate limits)
    
    Returns:
        {'submitted': N, 'failed': N, 'results': [...]}
    """
    results = []
    submitted = 0
    failed = 0
    
    for url in urls:
        result = submit_url(url)
        results.append(result)
        if result['success']:
            submitted += 1
        else:
            failed += 1
        time.sleep(batch_delay)
    
    return {
        'submitted': submitted,
        'failed': failed,
        'total': len(urls),
        'results': results,
        'service': 'Google Indexing API (free)'
    }


def get_url_status(url):
    """
    Get the notification metadata for a URL.
    
    Returns:
        {'last_notified': '...', 'type': '...'} or None if not submitted
    """
    try:
        indexing = _get_indexing_service()
        result = indexing.urlNotifications().getMetadata(url=url).execute()
        latest = result.get('latestUpdate', {})
        return {
            'url': url,
            'last_notified': latest.get('notifyTime', ''),
            'type': latest.get('type', ''),
        }
    except Exception:
        return None
