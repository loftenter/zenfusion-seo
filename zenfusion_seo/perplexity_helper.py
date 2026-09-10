"""
Perplexity Sonar API Helper — AEO Citation Testing
====================================================
Tests whether a brand/URL is cited in AI-generated answers.
Uses Perplexity's Sonar model which provides web-sourced answers with citations.

Cost: ~$0.005 per query (pennies)

Usage:
    from perplexity_helper import test_aeo_citation, test_keyword_citations
    
    # Test if a URL is cited for a keyword
    result = test_aeo_citation('best water softener', 'softprowatersystems.com')
    # → {'cited': True, 'citation_url': 'https://www.softprowatersystems.com/...', 'answer': '...', 'all_citations': [...]}
    
    # Test multiple keywords for a brand
    results = test_keyword_citations('softprowatersystems.com', 
        ['best water softener', 'water softener reviews', 'salt-free water softener'])
"""

import json, os, urllib.request, re

_cache = {}

def _get_api_key():
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('PERPLEXITY_API_KEY='):
                return line.split('=', 1)[1]
    return ''

def _query_perplexity(query, system_prompt=None, model='sonar', max_tokens=2000):
    """Query Perplexity Sonar API and return answer + citations."""
    api_key = _get_api_key()
    if not api_key:
        return {'error': 'No PERPLEXITY_API_KEY in .env'}
    
    if not system_prompt:
        system_prompt = 'You are a helpful assistant. Provide detailed answers with sources. Include specific product recommendations and brand names.'
    
    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': query}
        ],
        'temperature': 0.2,
        'max_tokens': max_tokens,
    }).encode()
    
    req = urllib.request.Request(
        'https://api.perplexity.ai/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    
    return result


def test_aeo_citation(keyword, brand_domain, brand_name=None, query_override=None):
    """
    Test if a brand is cited in Perplexity's answer for a keyword.
    
    Args:
        keyword: The search query (e.g., 'best water softener')
        brand_domain: The domain to check (e.g., 'softprowatersystems.com')
        brand_name: Optional brand name to check in answer text (e.g., 'SoftPro')
        query_override: Custom query instead of keyword
    
    Returns:
        {
            'cited': bool,           — domain appears in citations
            'mentioned': bool,       — brand name appears in answer text
            'citation_urls': [...],  — matching citation URLs
            'answer': str,           — full answer text
            'all_citations': [...],  — all citation URLs
            'cost': float,           — cost in dollars
        }
    """
    cache_key = f"{keyword}:{brand_domain}"
    if cache_key in _cache:
        return _cache[cache_key]
    
    query = query_override or keyword
    result = _query_perplexity(query)
    
    if 'error' in result:
        return result
    
    answer = result.get('choices', [{}])[0].get('message', {}).get('content', '')
    citations = result.get('citations', [])
    cost = result.get('usage', {}).get('cost', {}).get('total_cost', 0)
    
    # Check if brand domain is in citations
    brand_domain_lower = brand_domain.lower().replace('www.', '')
    citation_urls = [c for c in citations if brand_domain_lower in c.lower()]
    
    # Check if brand name is mentioned in answer
    mentioned = False
    if brand_name:
        mentioned = brand_name.lower() in answer.lower()
    
    # Also check if domain appears in answer text
    if not mentioned and brand_domain_lower in answer.lower():
        mentioned = True
    
    response = {
        'cited': len(citation_urls) > 0,
        'mentioned': mentioned,
        'citation_urls': citation_urls,
        'answer': answer,
        'all_citations': citations,
        'cost': cost,
        'keyword': keyword,
        'brand_domain': brand_domain,
    }
    
    _cache[cache_key] = response
    return response


def test_keyword_citations(brand_domain, keywords, brand_name=None):
    """
    Test multiple keywords for AEO citation.
    
    Args:
        brand_domain: Domain to check
        keywords: List of keywords to test
        brand_name: Optional brand name
    
    Returns:
        {
            'brand': brand_domain,
            'keywords_tested': N,
            'cited_count': N,
            'mentioned_count': N,
            'total_cost': float,
            'results': [...],
        }
    """
    results = []
    total_cost = 0
    cited_count = 0
    mentioned_count = 0
    
    for kw in keywords:
        result = test_aeo_citation(kw, brand_domain, brand_name)
        results.append({
            'keyword': kw,
            'cited': result.get('cited', False),
            'mentioned': result.get('mentioned', False),
            'citation_urls': result.get('citation_urls', []),
            'cost': result.get('cost', 0),
        })
        total_cost += result.get('cost', 0)
        if result.get('cited'):
            cited_count += 1
        if result.get('mentioned'):
            mentioned_count += 1
    
    return {
        'brand': brand_domain,
        'keywords_tested': len(keywords),
        'cited_count': cited_count,
        'mentioned_count': mentioned_count,
        'total_cost': round(total_cost, 4),
        'results': results,
    }


def get_competitor_citations(keyword, competitor_domains):
    """
    For a keyword, find which competitors are cited by Perplexity.
    Useful for competitive AEO analysis.
    
    Args:
        keyword: Search query
        competitor_domains: List of domains to check
    
    Returns:
        {'keyword': str, 'competitors_cited': [...], 'all_citations': [...], 'answer': str}
    """
    result = _query_perplexity(keyword)
    
    if 'error' in result:
        return result
    
    answer = result.get('choices', [{}])[0].get('message', {}).get('content', '')
    citations = result.get('citations', [])
    
    competitors_cited = []
    for domain in competitor_domains:
        domain_lower = domain.lower().replace('www.', '')
        matching = [c for c in citations if domain_lower in c.lower()]
        if matching:
            competitors_cited.append({
                'domain': domain,
                'citation_urls': matching,
                'mentioned_in_text': domain_lower in answer.lower(),
            })
    
    return {
        'keyword': keyword,
        'competitors_cited': competitors_cited,
        'all_citations': citations,
        'answer': answer[:500],
        'cost': result.get('usage', {}).get('cost', {}).get('total_cost', 0),
    }


def get_ai_overview_answer(keyword):
    """
    Get the full AI-generated answer for a keyword (no brand checking).
    Useful for content gap analysis — see what AI says about a topic.
    
    Returns: {'answer': str, 'citations': [...], 'cost': float}
    """
    result = _query_perplexity(keyword)
    
    if 'error' in result:
        return result
    
    return {
        'answer': result.get('choices', [{}])[0].get('message', {}).get('content', ''),
        'citations': result.get('citations', []),
        'cost': result.get('usage', {}).get('cost', {}).get('total_cost', 0),
    }
