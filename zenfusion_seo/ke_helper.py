"""
Keywords Everywhere (KE) API Helper
====================================
Shared module used by page_auditor.py, site_auditor.py, backlink analysis, 
and keyword research scripts.

PRIORITY: Use KE for keyword volume/CPC/competition/trend data (prepaid credits).
Fall back to DataForSEO only for data KE doesn't provide.

API: POST https://api.keywordseverywhere.com/v1/get_keyword_data
Auth: Authorization: Bearer <api_key>
Payload: {"kw": ["keyword1", "keyword2"], "country": "us"}
Returns: vol, cpc {currency, value}, competition (0-1), trend [12 months]
"""

import json
import os
import urllib.request
from typing import List, Dict, Optional

_cache = {}

def _get_api_key() -> str:
    """Load KE API key from .env file."""
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('KEYWORDS_EVERYWHERE_API_KEY='):
                return line.split('=', 1)[1]
    return ''

def get_keyword_data(keywords: List[str], country: str = "us") -> Dict[str, Dict]:
    """
    Fetch keyword data from Keywords Everywhere API.
    
    Args:
        keywords: List of keywords (max 100 per call)
        country: Country code (us, gb, ca, etc.)
    
    Returns:
        Dict mapping keyword -> {vol, cpc, competition, trend, currency}
    
    Example:
        >>> data = get_keyword_data(["best water softener", "water filter"])
        >>> data["best water softener"]["vol"]
        8100
        >>> data["best water softener"]["cpc"]
        1.93
    """
    if not keywords:
        return {}
    
    api_key = _get_api_key()
    if not api_key:
        return {}
    
    # Deduplicate and limit to 100 per call
    keywords = list(set(keywords))[:100]
    
    # Check cache
    cache_key = f"{country}:{'|'.join(sorted(keywords))}"
    if cache_key in _cache:
        return _cache[cache_key]
    
    # Make API call
    payload = json.dumps({"kw": keywords, "country": country}).encode()
    req = urllib.request.Request(
        "https://api.keywordseverywhere.com/v1/get_keyword_data",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {}
    
    # Parse results
    data = {}
    for item in result.get("data", []):
        kw = item.get("keyword", "")
        if kw:
            data[kw.lower()] = {
                "vol": item.get("vol", 0),
                "cpc": float(item.get("cpc", {}).get("value", 0)),
                "currency": item.get("cpc", {}).get("currency", "$"),
                "competition": item.get("competition", 0),
                "trend": item.get("trend", []),
            }
    
    # Cache credits info
    data["_credits_remaining"] = result.get("credits", 0)
    data["_credits_consumed"] = result.get("credits_consumed", 0)
    
    _cache[cache_key] = data
    return data


def get_keyword_volume(keyword: str, country: str = "us") -> int:
    """Quick helper: just get search volume for one keyword."""
    data = get_keyword_data([keyword], country)
    return data.get(keyword.lower(), {}).get("vol", 0)


def get_keyword_cpc(keyword: str, country: str = "us") -> float:
    """Quick helper: just get CPC for one keyword."""
    data = get_keyword_data([keyword], country)
    return data.get(keyword.lower(), {}).get("cpc", 0.0)


def get_keyword_competition(keyword: str, country: str = "us") -> float:
    """Quick helper: just get competition score (0-1) for one keyword."""
    data = get_keyword_data([keyword], country)
    return data.get(keyword.lower(), {}).get("competition", 0.0)


def get_keyword_trend(keyword: str, country: str = "us") -> list:
    """Quick helper: get 12-month trend data for a keyword."""
    data = get_keyword_data([keyword], country)
    return data.get(keyword.lower(), {}).get("trend", [])


def get_batch_volumes(keywords: List[str], country: str = "us") -> Dict[str, int]:
    """
    Batch helper: get volumes for many keywords at once.
    Returns dict of keyword -> volume.
    """
    data = get_keyword_data(keywords, country)
    return {kw: d["vol"] for kw, d in data.items() if not kw.startswith("_")}


def get_keyword_priority_score(keyword: str, country: str = "us") -> float:
    """
    Calculate a priority score for a keyword based on volume, CPC, and competition.
    Higher = better opportunity.
    
    Formula: vol * cpc * (1 - competition)
    High volume + high CPC + low competition = best opportunity
    """
    data = get_keyword_data([keyword], country)
    kw_data = data.get(keyword.lower())
    if not kw_data:
        return 0.0
    
    vol = kw_data.get("vol", 0)
    cpc = kw_data.get("cpc", 0)
    competition = kw_data.get("competition", 1)
    
    # Normalize: vol in thousands, cpc in dollars
    vol_score = min(vol / 10000, 10)  # Cap at 10
    cpc_score = min(cpc, 10)  # Cap at $10
    comp_score = 1 - competition  # Lower competition = higher score
    
    return round(vol_score * cpc_score * comp_score, 2)


def get_seasonality_insight(keyword: str, country: str = "us") -> Dict:
    """
    Analyze 12-month trend to find seasonality patterns.
    Returns peak month, low month, and seasonality score.
    """
    trend = get_keyword_trend(keyword, country)
    if not trend:
        return {"has_seasonality": False}
    
    values = [m.get("value", 0) for m in trend]
    if not values or max(values) == 0:
        return {"has_seasonality": False}
    
    peak_idx = values.index(max(values))
    low_idx = values.index(min(values))
    peak_month = trend[peak_idx].get("month", "")
    low_month = trend[low_idx].get("month", "")
    
    # Seasonality = how much the keyword fluctuates
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    seasonality_score = round((variance ** 0.5) / avg * 100, 1) if avg > 0 else 0
    
    return {
        "has_seasonality": seasonality_score > 20,
        "seasonality_score": seasonality_score,
        "peak_month": f"{peak_month} {trend[peak_idx].get('year', '')}",
        "peak_volume": max(values),
        "low_month": f"{low_month} {trend[low_idx].get('year', '')}",
        "low_volume": min(values),
        "avg_monthly_volume": round(avg),
        "trend": trend
    }


def get_credits_remaining() -> int:
    """Check remaining KE credits."""
    data = get_keyword_data(["test"], "us")
    return data.get("_credits_remaining", 0)