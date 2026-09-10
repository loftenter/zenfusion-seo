"""
Google PageSpeed Insights (PSI) API Helper
===========================================
Provides free Lighthouse performance/SEO audits + CrUX real user data.
Replaces DataForSEO's Lighthouse endpoint (pay-as-you-go) with free Google API.

Usage:
    from psi_helper import get_psi_report, get_core_web_vitals
    
    # Full Lighthouse + CrUX report
    report = get_psi_report('https://www.softprowatersystems.com/', strategy='mobile')
    
    # Just Core Web Vitals from CrUX
    cwv = get_core_web_vitals('https://www.softprowatersystems.com/')
"""

import json, os, urllib.request, urllib.parse

_cache = {}

def _get_api_key():
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('GOOGLE_API_KEY='):
                return line.split('=', 1)[1]
    return ''

def get_psi_report(url, strategy='mobile', categories=None):
    """
    Get full PageSpeed Insights report (Lighthouse + CrUX).
    
    Args:
        url: URL to test
        strategy: 'mobile' or 'desktop'
        categories: List of categories ('performance', 'seo', 'accessibility', 'best-practices')
                    Default: ['performance', 'seo']
    
    Returns:
        {
            'url': url,
            'strategy': strategy,
            'scores': {'performance': N, 'seo': N, ...},
            'metrics': {'fcp': ..., 'lcp': ..., 'tbt': ..., 'cls': ..., 'si': ...},
            'opportunities': [...],
            'crux_page': {...},
            'crux_origin': {...},
            'audits': {...},
            'full_report': {...}  # Complete Lighthouse response
        }
    """
    if not categories:
        categories = ['performance', 'seo']
    
    cache_key = f"{url}:{strategy}:{','.join(sorted(categories))}"
    if cache_key in _cache:
        return _cache[cache_key]
    
    api_key = _get_api_key()
    
    params = {
        'url': url,
        'strategy': strategy,
        'key': api_key,
    }
    # Add categories
    for cat in categories:
        params.setdefault('category', [])
    
    # Build URL with multiple category params
    query_parts = [f'url={urllib.parse.quote(url)}', f'strategy={strategy}', f'key={api_key}']
    for cat in categories:
        query_parts.append(f'category={cat}')
    
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{'&'.join(query_parts)}"
    
    req = urllib.request.Request(api_url)
    with urllib.request.urlopen(req, timeout=90) as resp:
        result = json.loads(resp.read())
    
    lighthouse = result.get('lighthouseResult', {})
    audits = lighthouse.get('audits', {})
    
    # Extract scores
    scores = {}
    for cat in categories:
        cat_data = lighthouse.get('categories', {}).get(cat if cat != 'best-practices' else 'best-practices', {})
        if cat_data:
            scores[cat] = round(cat_data.get('score', 0) * 100)
    
    # Extract key metrics
    metrics = {}
    metric_map = {
        'fcp': 'first-contentful-paint',
        'lcp': 'largest-contentful-paint',
        'tbt': 'total-blocking-time',
        'cls': 'cumulative-layout-shift',
        'si': 'speed-index',
        'tti': 'interactive',
    }
    for short, full in metric_map.items():
        audit = audits.get(full, {})
        metrics[short] = {
            'display': audit.get('displayValue', 'N/A'),
            'score': audit.get('score', 0),
            'numeric': audit.get('numericValue', 0),
            'rating': 'good' if audit.get('score', 0) >= 0.9 else ('needs_work' if audit.get('score', 0) >= 0.5 else 'poor'),
        }
    
    # Extract opportunities (audits with low scores)
    opportunities = []
    for key, audit in audits.items():
        score = audit.get('score')
        if score is not None and score < 0.9 and audit.get('numericValue', 0) > 0:
            if audit.get('scoreDisplayMode') != 'notApplicable':
                opportunities.append({
                    'id': key,
                    'title': audit.get('title', key),
                    'display': audit.get('displayValue', ''),
                    'score': score,
                    'numeric': audit.get('numericValue', 0),
                })
    opportunities.sort(key=lambda x: -x['numeric'])
    
    # CrUX data
    crux_page = _parse_crux(result.get('loadingExperience', {}))
    crux_origin = _parse_crux(result.get('originLoadingExperience', {}))
    
    report = {
        'url': url,
        'strategy': strategy,
        'scores': scores,
        'metrics': metrics,
        'opportunities': opportunities[:10],
        'crux_page': crux_page,
        'crux_origin': crux_origin,
        'final_url': lighthouse.get('finalUrl', url),
        'total_size': audits.get('total-byte-weight', {}).get('numericValue', 0),
    }
    
    _cache[cache_key] = report
    return report


def _parse_crux(crux_data):
    """Parse CrUX loading experience data."""
    if not crux_data or not crux_data.get('metrics'):
        return None
    
    result = {
        'category': crux_data.get('overall_category', 'N/A'),
        'metrics': {},
    }
    
    for metric, data in crux_data.get('metrics', {}).items():
        result['metrics'][metric] = {
            'percentile': data.get('percentile', 0),
            'category': data.get('category', 'N/A'),
        }
    
    return result


def get_core_web_vitals(url):
    """
    Get just the CrUX Core Web Vitals data (real Chrome user data).
    No Lighthouse run needed — faster, uses field data.
    
    Returns: {'lcp': N, 'cls': N, 'inp': N, 'fcp': N, 'ttfb': N, 'category': 'FAST'|'AVERAGE'|'SLOW'}
    """
    report = get_psi_report(url, strategy='mobile', categories=['performance'])
    
    crux = report.get('crux_page') or report.get('crux_origin')
    if not crux:
        return None
    
    metrics = crux.get('metrics', {})
    
    return {
        'lcp': metrics.get('LARGEST_CONTENTFUL_PAINT_MS', {}).get('percentile', 0),
        'cls': metrics.get('CUMULATIVE_LAYOUT_SHIFT_SCORE', {}).get('percentile', 0),
        'inp': metrics.get('INTERACTION_TO_NEXT_PAINT', {}).get('percentile', 0),
        'fcp': metrics.get('FIRST_CONTENTFUL_PAINT_MS', {}).get('percentile', 0),
        'ttfb': metrics.get('EXPERIMENTAL_TIME_TO_FIRST_BYTE', {}).get('percentile', 0),
        'category': crux.get('category', 'N/A'),
    }


def get_lighthouse_score(url, strategy='mobile'):
    """Quick helper: just get the performance score."""
    report = get_psi_report(url, strategy=strategy, categories=['performance'])
    return report['scores'].get('performance', 0)
