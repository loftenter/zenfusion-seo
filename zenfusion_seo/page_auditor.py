#!/usr/bin/env python3
"""
Zenfusion Unified Page Auditor v1.0
Integrates ALL audit tools into one comprehensive report:
  - site_auditor.py  (13-category technical SEO)
  - factor_engine.py (173+ ranking factors)
  - content_auditor.py (6-dimension content quality)
  - DataForSEO SERP / Lighthouse / AI Overview
  - InfraNodus entity extraction
  - SRS composite scoring (Structure + Relevance + Signal)
  - Dual fix lists (Developer + SEO Team)

Usage: python3 page_auditor.py <url> <keyword> [--output path]
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import sys, os, json, re, base64, urllib.request, urllib.parse, ssl, time
from urllib.parse import urlparse
from collections import defaultdict

# Add scripts dir to import path
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Requires: pip3 install requests beautifulsoup4")
    sys.exit(1)

# === Import existing audit modules ===
import site_auditor
import factor_engine
import content_auditor

# ============================================================================
# CONFIG
# ============================================================================
def load_env():
    env_path = os.path.expanduser("~/.hermes/.env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
    return env

ENV = load_env()

# ============================================================================
# FETCH PAGE
# ============================================================================
def fetch_page(url):
    """Fetch page HTML — requests first, ScrapeOwl fallback (with JS rendering).
    Returns (html, status, response).
    """
    try:
        resp = requests.get(url, timeout=30, verify=False,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        if resp.status_code == 200 and len(resp.text) > 1000:
            # Check if the page has meaningful content (title + body)
            if '<title' in resp.text.lower() and ('<body' in resp.text.lower() or '<h1' in resp.text.lower()):
                return resp.text, resp.status_code, resp
    except Exception:
        pass
    # ScrapeOwl fallback — use JS rendering for Shopify/Magento/JS-heavy sites
    api_key = ENV.get("SCRAPEOWL_API_KEY", "")
    if api_key:
        try:
            # First try with JS rendering (more accurate for Shopify, Magento, React, etc.)
            resp = requests.post("https://api.scrapeowl.com/v1/scrape",
                                json={"api_key": api_key, "url": url, "render_js": True}, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                html = data.get("html", data.get("body", data.get("content", data.get("data", ""))))
                if html and len(html) > 1000:
                    return html, 200, None
        except Exception:
            pass
        # If JS rendering fails, try without
        try:
            resp = requests.post("https://api.scrapeowl.com/v1/scrape",
                                json={"api_key": api_key, "url": url, "render_js": False}, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                html = data.get("html", data.get("body", data.get("content", data.get("data", ""))))
                if html and len(html) > 100:
                    return html, 200, None
        except Exception:
            pass
    return "", 0, None

# ============================================================================
# SCREAMING FROG GAP METRICS (15 new metrics from SF analysis)
# ============================================================================
def analyze_readability(text):
    """Analyze readability using textstat."""
    try:
        import textstat
        flesch = textstat.flesch_reading_ease(text)
        sentences = textstat.sentence_count(text)
        words = len(text.split())
        avg_words_per_sentence = round(words / max(1, sentences), 1)
        text_ratio = round(len(text) / max(1, len(text) + 10000) * 100, 1)  # approximate
        
        if flesch >= 80: level = "Easy"
        elif flesch >= 60: level = "Standard"
        elif flesch >= 40: level = "Medium"
        elif flesch >= 20: level = "Difficult"
        else: level = "Very Difficult"
        
        return {
            "flesch_reading_ease": round(flesch, 1),
            "sentence_count": sentences,
            "avg_words_per_sentence": avg_words_per_sentence,
            "readability_level": level,
            "text_ratio": text_ratio,
        }
    except ImportError:
        # Manual calculation without textstat
        import re
        sentences = len(re.findall(r'[.!?]+', text))
        words = len(text.split())
        avg_words = round(words / max(1, sentences), 1)
        # Simple Flesch approximation
        syllables = sum(len(re.findall(r'[aeiouAEIOU]+', w)) for w in text.split())
        if words > 0 and sentences > 0:
            flesch = 206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)
        else:
            flesch = 0
        if flesch >= 80: level = "Easy"
        elif flesch >= 60: level = "Standard"
        elif flesch >= 40: level = "Medium"
        elif flesch >= 20: level = "Difficult"
        else: level = "Very Difficult"
        return {
            "flesch_reading_ease": round(flesch, 1),
            "sentence_count": sentences,
            "avg_words_per_sentence": avg_words,
            "readability_level": level,
            "text_ratio": round(len(text) / max(1, len(text.encode('utf-8'))) * 100, 1),
        }

def check_spelling(text):
    """Check for spelling errors using pyspellchecker."""
    try:
        from spellchecker import SpellChecker
        spell = SpellChecker()
        # Only check significant words (len > 3)
        words = [w.strip('.,!?";:()[]{}').lower() for w in text.split() if len(w) > 3]
        misspelled = spell.unknown(words[:500])  # Check first 500 words for speed
        # Filter out proper nouns and brand terms
        common_brands = ['softpro', 'springwell', 'pelican', 'nuvoh2o', 'aquasana', 'aquasure',
                        'culligan', 'fleck', 'ispring', 'saltless', 'saltfree', 'watersoftener']
        real_misspelled = [w for w in misspelled if w not in common_brands]
        return {
            "spelling_errors": len(real_misspelled),
            "misspelled_words": list(real_misspelled)[:10],
        }
    except ImportError:
        return {"spelling_errors": 0, "misspelled_words": []}

def analyze_technical_headers(resp, html, url):
    """Analyze HTTP headers and page weight (SF gap metrics)."""
    result = {
        "response_time_ms": 0,
        "page_size_bytes": len(html) if html else 0,
        "transferred_bytes": 0,
        "http_version": "unknown",
        "last_modified": "",
        "cookies_count": 0,
        "language": "",
        "content_type": "text/html",
        "x_robots_tag": "",
        "meta_refresh": "",
        "rel_next": "",
        "rel_prev": "",
        "amphtml": "",
        "meta_keywords": "",
        "redirect_url": "",
        "redirect_type": "",
        "mobile_alternate": "",
    }
    
    if resp:
        # Response time (TTFB)
        if hasattr(resp, 'elapsed'):
            result["response_time_ms"] = round(resp.elapsed.total_seconds() * 1000)
        
        # HTTP headers
        headers = dict(resp.headers) if hasattr(resp, 'headers') else {}
        result["transferred_bytes"] = int(headers.get('Content-Length', len(html) if html else 0) or 0)
        result["http_version"] = resp.raw.version if hasattr(resp, 'raw') and hasattr(resp.raw, 'version') else "unknown"
        result["last_modified"] = headers.get('Last-Modified', '')
        result["cookies_count"] = len(resp.cookies) if hasattr(resp, 'cookies') else 0
        result["content_type"] = headers.get('Content-Type', 'text/html')
        result["x_robots_tag"] = headers.get('X-Robots-Tag', '')
        
        # Redirect info
        if hasattr(resp, 'is_redirect') and resp.is_redirect:
            result["redirect_url"] = headers.get('Location', '')
            result["redirect_type"] = str(resp.status_code)
    
    # Parse HTML for remaining metrics
    if html:
        soup = BeautifulSoup(html, "html.parser")
        
        # Meta keywords
        mk = soup.find("meta", attrs={"name": "keywords"})
        if mk:
            result["meta_keywords"] = mk.get("content", "")[:200]
        
        # Meta refresh
        mr = soup.find("meta", attrs={"http-equiv": "refresh"})
        if mr:
            result["meta_refresh"] = mr.get("content", "")
        
        # rel=next/prev
        next_link = soup.find("link", rel="next")
        if next_link:
            result["rel_next"] = next_link.get("href", "")
        prev_link = soup.find("link", rel="prev")
        if prev_link:
            result["rel_prev"] = prev_link.get("href", "")
        
        # AMP
        amp = soup.find("link", rel="amphtml")
        if amp:
            result["amphtml"] = amp.get("href", "")
        
        # Mobile alternate
        ma = soup.find("link", rel="alternate", media=True)
        if ma:
            result["mobile_alternate"] = ma.get("href", "")
        
        # Language (from html lang attribute)
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            result["language"] = html_tag.get("lang")
    
    return result

def analyze_information_gain(keyword, serp, target_url, env):
    """
    Information Gain Analysis — finds consensus vs unique claims across SERP.
    Fetches top ranking pages, extracts content, identifies what most pages say
    (consensus = coverage floor) vs what only one page says (unique = IG opportunities).
    """
    import re
    try:
        import trafilatura
    except ImportError:
        return {"error": "trafilatura not installed", "consensus": [], "unique": [], "missing": []}

    TOP_N = 8
    readable_pages = []
    
    # Fetch content from top ranking pages (skip the target URL itself)
    for result in serp[:TOP_N]:
        url = result.get("url", "")
        if not url or url == target_url:
            continue
        domain = result.get("domain", "")
        position = result.get("position", 0)
        
        try:
            resp = requests.get(url, timeout=30, verify=False,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
            if resp.status_code != 200:
                continue
            html = resp.text
            text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
            if len(text) < 400:
                continue
            
            # Clean boilerplate
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            boilerplate = re.compile(r"(cookie|subscribe|newsletter|sign up|all rights reserved|privacy policy)", re.I)
            lines = [ln for ln in lines if not boilerplate.search(ln)]
            text = "\n".join(lines)[:15000]
            
            readable_pages.append({
                "position": position,
                "domain": domain,
                "url": url,
                "content": text
            })
        except:
            continue
    
    if len(readable_pages) < 3:
        return {"error": f"Only {len(readable_pages)} pages readable (need 3+)", 
                "consensus": [], "unique": [], "missing": [], "pages_analyzed": len(readable_pages)}
    
    # Extract claims from each page using keyword-based heuristics
    # (Since we don't have Anthropic API, we extract key claims via pattern matching)
    all_claims = []
    for page in readable_pages:
        claims = extract_claims_heuristic(page["content"], keyword)
        for claim in claims:
            all_claims.append({
                "text": claim,
                "position": page["position"],
                "domain": page["domain"]
            })
    
    # Cluster claims - find consensus (shared by 2+ pages) vs unique (1 page)
    from collections import defaultdict
    claim_groups = defaultdict(list)
    
    for claim in all_claims:
        # Normalize for clustering
        normalized = claim["text"].lower().strip()
        # Group by first 5 significant words
        words = [w for w in normalized.split() if len(w) > 3][:5]
        key = " ".join(words) if words else normalized[:30]
        claim_groups[key].append(claim)
    
    consensus = []
    unique = []
    
    for key, claims in claim_groups.items():
        positions = list(set(c["position"] for c in claims))
        canonical = claims[0]["text"]
        
        if len(positions) >= 2:
            consensus.append({
                "text": canonical,
                "pages": len(positions),
                "positions": positions
            })
        elif len(claims) == 1:
            unique.append({
                "text": canonical,
                "position": claims[0]["position"],
                "domain": claims[0]["domain"]
            })
    
    # Sort consensus by number of pages (most agreed first)
    consensus.sort(key=lambda x: -x["pages"])
    # Sort unique by position (higher ranking first)
    unique.sort(key=lambda x: x["position"])
    
    # Identify missing topics (white space)
    missing = identify_missing_topics(keyword, consensus, unique)
    
    return {
        "pages_analyzed": len(readable_pages),
        "total_claims": len(all_claims),
        "consensus": consensus[:15],
        "unique": unique[:25],
        "missing": missing,
        "repetition_rate": round(len(consensus) / max(1, len(consensus) + len(unique)) * 100, 1)
    }


def extract_claims_heuristic(text, keyword):
    """Extract key claims from text using heuristic patterns."""
    import re
    claims = []
    lines = text.split('\n')
    
    # Patterns that indicate factual claims
    claim_patterns = [
        r'(\d+[%\s]*(?:percent|%)?\s*(?:of|less|more|reduction|increase|savings))',
        r'(\$\d[\d,]*(?:\.\d+)?(?:\s*(?:to|–|-)\s*\$?\d[\d,]*)?)',
        r'(\d[\d,]*\s*(?:grains?|gpg|ppm|gallons?|years?|months?|hours?))',
        r'(should\s+(?:be|use|have|include|consider))',
        r'(recommend\w*\s+\w+)',
        r'(important\s+(?:to|that|because))',
        r'(because\s+)',
        r'(causes?\s+)',
        r'(prevents?\s+)',
        r'(reduces?\s+)',
        r'(increases?\s+)',
        r'(requires?\s+)',
        r'(warranty\s+)',
        r'(certified\s+)',
        r'(efficient\s+)',
    ]
    
    seen = set()
    for line in lines:
        line = line.strip()
        if len(line) < 20 or len(line) > 200:
            continue
        # Skip navigation, marketing
        if re.search(r'(cookie|subscribe|newsletter|buy now|click here|sign up|cart|menu)', line, re.I):
            continue
        
        # Check if line matches claim patterns
        for pattern in claim_patterns:
            if re.search(pattern, line, re.I):
                # Clean and normalize
                claim = re.sub(r'\s+', ' ', line).strip()
                claim = re.sub(r'^(?:-|\*|\d+\.?\s*)', '', claim).strip()
                if len(claim) > 15 and len(claim) < 150 and claim.lower() not in seen:
                    seen.add(claim.lower())
                    claims.append(claim)
                    break
        
        if len(claims) >= 25:
            break
    
    return claims


def identify_missing_topics(keyword, consensus, unique):
    """Identify topics that no ranking page covers."""
    # Common topics that should be covered but often aren't
    potential_topics = [
        "ROI calculation and cost savings analysis",
        "Environmental impact of salt discharge",
        "Effect on septic systems",
        "Water pH impact",
        "Installation time estimates",
        "Warranty claim rates and reliability data",
        "Customer service comparison",
        "Long-term ownership costs beyond initial purchase",
        "Impact on drinking water taste",
        "Comparison of salt consumption efficiency across models",
        "Effect on water pressure",
        "Maintenance frequency and cost over time",
        "Compatibility with different water sources (well vs city)",
        "Impact on home resale value",
        "Energy consumption comparison",
    ]
    
    # Check which topics are already covered
    all_text = " ".join(c["text"].lower() for c in consensus + unique)
    
    missing = []
    for topic in potential_topics:
        topic_words = topic.lower().split()
        # Check if key words from the topic appear in any claim
        covered = any(word in all_text for word in topic_words if len(word) > 4)
        if not covered:
            missing.append(topic)
    
    return missing[:10]


# ============================================================================
# DATAFORSEO API
# ============================================================================

# Keywords Everywhere helper (prepaid credits — use INSTEAD of DataForSEO for keyword volume/CPC/competition/trend)
try:
    import sys as _sys
    _sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
    import ke_helper
    HAS_KE = True
except:
    HAS_KE = False

def get_ke_keyword_data(keyword, country="us"):
    """Get keyword volume/CPC/competition/trend from Keywords Everywhere (prepaid)."""
    if not HAS_KE:
        return None
    try:
        return ke_helper.get_keyword_data([keyword], country)
    except:
        return None

def deep_scan_top100(keyword, env):
    """
    Deep SERP scan: fetch top 100 organic results, scrape each page,
    extract claims AND problems/issues. Also captures PAA questions
    and related searches. Only runs ONCE per keyword (cached to disk).
    """
    import hashlib, re
    from urllib.parse import urlparse
    
    # Cache check — only run once per keyword
    cache_key = hashlib.sha256(keyword.encode()).hexdigest()[:20]
    cache_dir = os.path.expanduser('~/.hermes/audits/deep-scan')
    cache_file = os.path.join(cache_dir, f"{cache_key}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            print(f"  ✓ Deep scan: loaded from cache ({cached['stats']['unique_claims']} claims, {cached['stats']['unique_problems']} problems)")
            return cached
        except:
            pass  # Cache corrupt, re-run
    
    os.makedirs(cache_dir, exist_ok=True)
    
    # Claim + problem patterns
    claim_patterns = [
        r'(\d+[%\s]*(?:percent|%)?\s*(?:of|less|more|reduction|increase|savings|efficient))',
        r'(\$[\d,]+(?:\.\d+)?(?:\s*(?:to|\u2013|-)\s*\$?[\d,]+)?)',
        r'(\d[\d,]*\s*(?:grains?|gpg|ppm|gallons?|years?|months?|hours?|psi|microns?))',
        r'(should\s+(?:be|use|have|include|consider))',
        r'(recommend\w*\s+\w+)',
        r'(warranty\s+\d+\s*year)',
        r'(certified\s+(?:by\s+)?(?:NSF|WQA|ANSI))',
        r'(flow\s*rate\s*of\s*[\d.]+\s*(?:gpm|gallons))',
        r'(capacity\s*of\s*[\d,]+\s*grains?)',
        r'(removes?\s+up\s+to\s*[\d]+%)',
        r'(reduces?\s+.*?\d+%)',
        r'(lifetime\s+warranty)',
        r'(free\s+(?:shipping|installation|consultation))',
        r'(patented?\s+\w+)',
        r'(tested\s+(?:and\s+)?certified)',
        r'(independent\s+lab)',
        r'(third-party\s+(?:tested|certified))',
        r'(industry\s+(?:leading|standard|best))',
        r'(automatic\s+(?:regeneration|backwash))',
        r'(digital\s+(?:metered|control|display))',
        r'(bypass\s+valve)',
        r'(problem\w*\s+with)',
        r'(issue\w*\s+with)',
        r'(common\s+complaint)',
        r'(recall\s+on)',
        r'(class\s+action)',
        r'(leak\w*\s+from)',
        r'(breakdown)',
        r'(failure\s+rate)',
        r'(resin\s+(?:fouling|damage|replacement))',
        r'(salt\s+bridg)',
        r'(hard\s+water\s+(?:damage|problem|issue))',
        r'(scale\s+buildup)',
        r'(dry\s+skin)',
        r'(hair\s+damage)',
        r'(appliance\s+damage)',
        r'(stain\w*\s+on)',
        r'(spotting\s+on)',
        r'(corrosion)',
        r'(low\s+water\s+pressure)',
        r'(expensive\s+to\s+(?:maintain|operate|run))',
        r'(wast\w*\s+(?:water|salt|energy))',
        r'(environmental\s+(?:concern|impact|issue))',
        r'(not\s+worth\s+it)',
        r'(regret\s+buying)',
        r'(disappointed)',
    ]
    
    problem_keywords = [
        'problem', 'issue', 'complaint', 'recall', 'class action',
        'leak', 'breakdown', 'failure', 'damage', 'bridging',
        'mush', 'corrosion', 'low pressure', 'expensive',
        'waste', 'environmental concern', 'not worth',
        'regret', 'disappointed', 'would not recommend',
        'dry skin', 'hair damage', 'stain', 'spotting',
        'film', 'scale buildup', 'hard water damage'
    ]
    
    # Step 1: Fetch top 100 SERP
    try:
        serp_data = dfs_api("serp/google/organic/live/advanced", [{
            "keyword": keyword,
            "location_code": 2840,
            "language_code": "en",
            "device": "desktop",
            "depth": 100
        }])
    except:
        return {"error": "SERP API failed", "claims": [], "problems": [], "paa_questions": [], "related_searches": []}
    
    serp_results = []
    paa_questions = []
    related_searches = []
    
    if serp_data.get('status_code') == 20000:
        task = serp_data['tasks'][0]
        if task.get('status_code') == 20000:
            items = task['result'][0].get('items', [])
            
            for item in items:
                if item.get('type') == 'organic':
                    serp_results.append({
                        'position': item.get('rank_group', 0),
                        'url': item.get('url', ''),
                        'domain': item.get('domain', ''),
                        'title': item.get('title', '')
                    })
                elif item.get('type') == 'people_also_ask':
                    if 'items' in item and item['items']:
                        for q in item['items'][:5]:
                            if isinstance(q, dict) and q.get('title'):
                                paa_questions.append(q['title'])
                elif item.get('type') == 'related_searches':
                    if 'items' in item and item['items']:
                        for s in item['items'][:10]:
                            if isinstance(s, dict) and s.get('title'):
                                related_searches.append(s['title'])
    
    print(f"  Deep scan: {len(serp_results)} organic results, {len(paa_questions)} PAA, {len(related_searches)} related")
    
    # Step 2: Fetch and extract from each page
    all_claims = []
    all_problems = []
    
    for i, res in enumerate(serp_results):
        url = res.get('url', '')
        if not url:
            continue
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                page_html = resp.read().decode('utf-8', errors='replace')
            
            text = re.sub(r'<script[^>]*>.*?</script>', '', page_html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            
            if len(text) < 400:
                continue
            
            lines = text.split('.')
            seen = set()
            
            for line in lines:
                line = line.strip()
                if len(line) < 15 or len(line) > 200:
                    continue
                if re.search(r'(cookie|subscribe|newsletter|buy now|click here|sign up|cart|menu|shop|login|privacy)', line, re.I):
                    continue
                
                for pattern in claim_patterns:
                    if re.search(pattern, line, re.I):
                        claim = re.sub(r'\s+', ' ', line).strip()
                        claim_key = claim.lower()[:80]
                        if len(claim) > 15 and len(claim) < 150 and claim_key not in seen:
                            seen.add(claim_key)
                            claim_lower = claim.lower()
                            is_problem = any(w in claim_lower for w in problem_keywords)
                            
                            entry = {
                                'claim': claim,
                                'domain': res['domain'],
                                'position': res['position'],
                                'url': url
                            }
                            
                            if is_problem:
                                all_problems.append(entry)
                            else:
                                all_claims.append(entry)
                        break
        except:
            pass
        
        if (i+1) % 20 == 0:
            print(f"    [{i+1}/{len(serp_results)}] claims={len(all_claims)} problems={len(all_problems)}")
        time.sleep(0.3)
    
    # Deduplicate
    seen = set()
    unique_claims = []
    for c in all_claims:
        key = c['claim'].lower()[:80]
        if key not in seen:
            seen.add(key)
            unique_claims.append(c)
    
    seen = set()
    unique_problems = []
    for p in all_problems:
        key = p['claim'].lower()[:80]
        if key not in seen:
            seen.add(key)
            unique_problems.append(p)
    
    result = {
        'keyword': keyword,
        'serp_results': serp_results,
        'claims': unique_claims,
        'problems': unique_problems,
        'paa_questions': paa_questions,
        'related_searches': related_searches,
        'stats': {
            'pages_fetched': len(serp_results),
            'unique_claims': len(unique_claims),
            'unique_problems': len(unique_problems)
        }
    }
    
    # Cache to disk
    with open(cache_file, 'w') as f:
        json.dump(result, f, indent=2)
    
    print(f"  ✓ Deep scan complete: {len(unique_claims)} claims, {len(unique_problems)} problems, cached")
    
    return result


# ============================================================================
# DATAFORSEO API (original)
# ============================================================================

def generate_faqs_from_ig(keyword, ig_result, target_url):
    """Generate FAQs from Information Gain analysis results.
    Closes white space (missing topics) and consolidates unique claims
    that are relevant to the brand and product line."""
    
    faqs = []
    consensus_texts = " ".join(c["text"].lower() for c in ig_result.get("consensus", []))
    unique_texts = " ".join(u["text"].lower() for u in ig_result.get("unique", []))
    missing = ig_result.get("missing", [])

    # Detect domain/brand from URL for brand-specific references
    from urllib.parse import urlparse
    domain = urlparse(target_url).netloc.replace("www.", "")
    brand = domain.split(".")[0].capitalize()
    if brand.lower() == "softpro":
        brand = "SoftPro"

    # Strip leading modifiers ("best", "top", "cheap", etc.) so FAQ questions
    # read naturally: "How much does a water softener cost?" instead of
    # "How much does a best water softener cost?".
    _modifiers = {"best", "top", "cheap", "cheapest", "good", "affordable",
                  "quality", "premium", "professional", "certified", "great",
                  "ultimate", "ideal", "perfect"}
    kw_words = keyword.split()
    if len(kw_words) > 1 and kw_words[0].lower() in _modifiers:
        natural_kw = " ".join(kw_words[1:])
    else:
        natural_kw = keyword
    # Use the natural form for question/answer phrasing throughout
    keyword = natural_kw
    
    # FAQ templates based on missing topics and unique claims
    # Each FAQ closes a white space or consolidates a relevant unique claim
    
    # 1. Cost FAQ (from missing + unique claims about pricing)
    if "cost" in " ".join(missing).lower() or "$" in unique_texts:
        faqs.append({
            "q": f"How much does a {keyword} cost?",
            "a": f"A quality {keyword} typically ranges from $300 to $3,000 depending on type and capacity. "
                 f"High-efficiency systems with advanced metered valves typically run $1,200-$2,000. "
                 f"Installation adds $200-$600 if hiring a plumber, though many modern systems support DIY installation. "
                 f"Annual maintenance costs (salt or alternative) run $100-$300 depending on water hardness and household size."
        })
    
    # 2. ROI FAQ (from missing)
    if any("roi" in m.lower() or "cost savings" in m.lower() for m in missing):
        faqs.append({
            "q": f"What is the ROI of a {keyword}?",
            "a": f"A {keyword} typically pays for itself within 2-4 years through reduced appliance replacement costs. "
                 f"Hard water causes scale buildup that shortens appliance lifespans by 30-50%. "
                 f"Softened water also reduces soap and detergent usage by up to 50%, saving $200-$600 per year. "
                 f"Combined with extended appliance lifespans, a $1,500 system can return its investment in under 3 years."
        })
    
    # 3. Septic FAQ (from missing)
    if any("septic" in m.lower() for m in missing):
        faqs.append({
            "q": f"Do {keyword}s affect septic systems?",
            "a": f"Modern {keyword}s are safe for septic systems when properly configured. The salt brine discharge "
                 f"is diluted enough that it doesn't harm the bacterial balance. High-efficiency systems produce "
                 f"less discharge than older models, further reducing any septic impact. If your septic system "
                 f"is undersized or failing, consult a septic professional before installation."
        })
    
    # 4. Installation FAQ (from missing + consensus)
    if any("installation" in m.lower() or "install" in m.lower() for m in missing):
        faqs.append({
            "q": f"Can I install a {keyword} myself or do I need a plumber?",
            "a": f"Many modern {keyword}s are designed for DIY installation with push-to-connect fittings or "
                 f"bypass valves — typically a 2-4 hour job. You'll need access to the main water line, a drain "
                 f"for regeneration discharge, and a nearby electrical outlet. For homes with older plumbing "
                 f"or complex configurations, a licensed plumber is recommended. Improper installation can "
                 f"void your warranty."
        })
    
    # 5. Hardness threshold FAQ (from missing + consensus)
    if any("hardness" in m.lower() or "threshold" in m.lower() for m in missing) or "hard water" in consensus_texts:
        faqs.append({
            "q": f"What water hardness level requires a {keyword}?",
            "a": f"Water hardness above 7 grains per gallon (GPG) or 120 parts per million (PPM) generally "
                 f"warrants a {keyword}. At this level, you'll notice scale buildup on fixtures, soap scum "
                 f"in showers, and reduced appliance efficiency. An estimated 85% of the US has hard water. "
                 f"Test your water with a simple kit or contact your municipal water supplier."
        })
    
    # 6. Pressure FAQ (from missing)
    if any("pressure" in m.lower() for m in missing):
        faqs.append({
            "q": f"Do {keyword}s affect water pressure?",
            "a": f"A properly sized {keyword} should not noticeably reduce water pressure. Systems with 1-inch "
                 f"valves maintain pressure even during peak usage. If you notice reduced pressure after "
                 f"installation, it may indicate the system needs regeneration, the resin bed needs cleaning, "
                 f"or the softener is undersized for your household's peak flow rate."
        })
    
    # 7. Salt usage FAQ (from unique claims about salt efficiency)
    if "salt" in unique_texts or "salt" in consensus_texts:
        faqs.append({
            "q": f"How much salt does a {keyword} use per month?",
            "a": f"Most {keyword}s use 20-40 lbs of salt per month for a family of four with moderate water "
                 f"hardness (10-15 GPG). High-efficiency systems can use up to 46% less salt, bringing monthly "
                 f"consumption to 10-20 lbs. Salt refills are typically needed every 2-3 months. "
                 f"Demand-initiated regeneration systems only regenerate when needed, reducing salt usage further."
        })
    
    # 8. Potassium chloride FAQ (from unique claims)
    if "potassium" in unique_texts:
        faqs.append({
            "q": f"Is potassium chloride a good alternative to sodium salt?",
            "a": f"Potassium chloride works in most {keyword}s as a sodium-free alternative, ideal for "
                 f"low-sodium diets. It costs 2-3 times more than sodium salt ($40-$60 vs $15-$20 per bag) "
                 f"and is gentler on septic systems and local waterways. Most modern systems are compatible "
                 f"with both sodium and potassium chloride."
        })
    
    # 9. Lifespan FAQ (from unique claims about warranty)
    if "warranty" in unique_texts or "years" in unique_texts:
        faqs.append({
            "q": f"How long do {keyword}s last?",
            "a": f"A quality {keyword} lasts 10-15 years with proper maintenance. The resin bed typically "
                 f"lasts 10-12 years before needing replacement. Control valves can last 15+ years. "
                 f"To maximize lifespan: refill salt before the brine tank empties, clean the brine tank "
                 f"annually, and use a pre-filter if your water contains sediment or iron. Iron can damage "
                 f"resin and reduce system lifespan by up to 50%."
        })
    
    # 10. NSF certification FAQ (from unique claims)
    if "nsf" in unique_texts or "certified" in unique_texts:
        faqs.append({
            "q": f"What does NSF certification mean for a {keyword}?",
            "a": f"NSF International certification means the system's components have been independently tested "
                 f"to meet strict public health and safety standards. NSF/ANSI Standard 44 certifies that the "
                 f"system effectively reduces water hardness and that materials don't leach harmful substances "
                 f"into your water. This is particularly important for drinking water applications."
        })
    
    # 11. Pre-filter FAQ (from unique claims about iron)
    if "iron" in unique_texts or "pre-filter" in unique_texts or "sediment" in unique_texts:
        faqs.append({
            "q": f"Do I need a pre-filter with my {keyword}?",
            "a": f"If your water contains sediment, iron, or manganese, a pre-filter is strongly recommended. "
                 f"Iron levels above 0.3 ppm can cause resin fouling that reduces effectiveness and lifespan "
                 f"by up to 50%. A simple sediment filter ($20-$50) installed before the {keyword} protects "
                 f"your investment. For high iron levels, consider a dedicated iron filter as pre-treatment."
        })
    
    # 12. pH/taste FAQ (from missing)
    if any("ph" in m.lower() or "taste" in m.lower() for m in missing):
        faqs.append({
            "q": f"How does a {keyword} affect drinking water taste?",
            "a": f"Softened water has a slightly different taste due to the ion exchange process. Most people "
                 f"don't notice the change. The sodium added is minimal: roughly 7.5 mg per 8 oz glass for "
                 f"moderately hard water (10 GPG) — less sodium than a slice of bread. For those sensitive to "
                 f"taste, a bypass valve for the kitchen tap or a reverse osmosis system can provide "
                 f"untreated drinking water."
        })
    
    # 13. Single vs twin tank (from unique claims)
    if "twin" in unique_texts or "single" in unique_texts or "tank" in unique_texts:
        faqs.append({
            "q": f"What's the difference between a single-tank and twin-tank {keyword}?",
            "a": f"Single-tank systems can't soften water during regeneration (typically a 2-hour process). "
                 f"Twin-tank systems alternate between two tanks for 24/7 soft water. Twin-tank systems cost "
                 f"more upfront ($300-$600 more) but are ideal for high water usage or households needing "
                 f"soft water at all hours. For most families, a single-tank system with demand-initiated "
                 f"regeneration is sufficient."
        })
    
    # 14. Outdoor installation (from unique claims)
    if any("outdoor" in m.lower() or "outside" in m.lower() for m in missing) or "outdoor" in unique_texts:
        faqs.append({
            "q": f"Can a {keyword} be installed outside?",
            "a": f"Yes, {keyword}s can be installed outdoors in mild climates with proper protection from "
                 f"direct sunlight, freezing temperatures, and heavy rain. A weather-resistant enclosure is "
                 f"recommended. In areas where temperatures drop below freezing, indoor installation in a "
                 f"garage, basement, or utility closet is preferred."
        })
    
    # 15. Energy consumption (from missing)
    if any("energy" in m.lower() for m in missing):
        faqs.append({
            "q": f"How much electricity does a {keyword} use?",
            "a": f"A {keyword} uses very little electricity — typically $5-$10 per year. The control valve "
                 f"uses a small amount of power for the timer and metering system. Regeneration cycles "
                 f"consume the most power but only run every few days. Demand-initiated systems are more "
                 f"energy-efficient than timer-based systems since they regenerate only when needed."
        })
    
    return faqs[:15]  # Cap at 15 FAQs


def generate_ai_examples(keyword, url, infranodus=None, content_result=None, factors=None, serp=None):
    """Generate 3 click-worthy AI-written examples each for Meta Title,
    Meta Description, H1, H2, H3, and Image ALT text. Uses the core keyword
    plus entities/topics extracted from the page to produce relevant copy.
    Returns an HTML section (expandable sub-sections, collapsed by default)."""

    # --- Build a natural keyword form (strip "best"/"top" etc.) ---
    _mods = {"best", "top", "cheap", "cheapest", "good", "affordable",
             "quality", "premium", "professional", "certified", "great",
             "ultimate", "ideal", "perfect"}
    kw_words = keyword.split()
    natural_kw = " ".join(kw_words[1:]) if (len(kw_words) > 1 and kw_words[0].lower() in _mods) else keyword
    kw_title = natural_kw.title()
    kw_lower = natural_kw.lower()

    # --- Gather entities / secondary topics for richer examples ---
    entities = []
    if infranodus and infranodus.get("top_entities"):
        entities = [e for e in infranodus["top_entities"] if e.lower() != kw_lower][:8]
    # Pull secondary topics from content_result if available
    if content_result and content_result.get("topics", {}).get("to_add"):
        for t in content_result["topics"]["to_add"][:5]:
            if t.lower() not in [e.lower() for e in entities]:
                entities.append(t)
    # SERP competitor titles as inspiration for angles
    competitor_angles = []
    if serp:
        for r in serp[:5]:
            t = r.get("title", "")
            if t:
                competitor_angles.append(t)

    # Helper: pick a secondary entity / topic, fallback to a sensible default
    def secondary(default):
        return entities[0] if entities else default

    sec_kw = secondary("salt-free water conditioner")
    sec_kw2 = entities[1] if len(entities) > 1 else "whole house filtration"
    entity3 = entities[2] if len(entities) > 2 else "city water"

    # --- Build example sets ---
    meta_titles = [
        f"Best {natural_kw} for {entity3.title()} (2024 Reviews)",
        f"{kw_title}: Top-Rated Systems for Clean Water",
        f"Best {natural_kw} Systems — Expert Buying Guide",
    ][:3]
    # Ensure each title is under 60 chars
    meta_titles = [t[:60].rsplit(" ", 1)[0] if len(t) > 60 else t for t in meta_titles]

    meta_descriptions = [
        f"Looking for the best {natural_kw}? Compare top-rated systems for {entity3}, see real cost data, and find the right fit for your home. Expert reviews & buying guide.",
        f"Discover the best {natural_kw} for clean, soft water. We compare features, prices, and certifications to help you choose. Read our 2024 expert guide now.",
        f"Find your ideal {natural_kw} — reviews, installation tips, and cost breakdowns. See which systems rank best for {entity3} and {sec_kw}.",
    ][:3]
    # Clamp to 120-160 chars
    meta_descriptions = [d if 120 <= len(d) <= 160 else d[:157] for d in meta_descriptions]

    h1s = [
        f"Best {natural_kw} for {entity3.title()} — Expert Reviews & Recommendations",
        f"{kw_title} Systems: Complete Buyer's Guide for Cleaner Water",
        f"Top {natural_kw} Picks — Reviews, Costs & Expert Advice",
    ][:3]

    h2s = [
        f"Best {natural_kw} Systems for {entity3.title()}",
        f"Cost & ROI of a {natural_kw}",
        f"{sec_kw.title()}: What You Need to Know",
    ][:3]

    h3s = [
        f"Top-Rated {natural_kw} Brands",
        f"How {kw_title} Works",
        f"Installing a {natural_kw} in {entity3.title()}",
    ][:3]

    alt_texts = [
        f"{kw_title} system installed in a home utility room",
        f"Cross-section diagram of a {natural_kw} showing resin tank and control valve",
        f"Comparison chart of the best {natural_kw} models for {entity3}",
    ][:3]

    # --- Build HTML ---
    def example_block(title, examples, hint=""):
        html = f'      <div class="cat-group">\n        <div class="cat-group-title">{title}</div>\n'
        if hint:
            html += f'        <p style="font-size:15px;color:#444;margin-bottom:6px">{hint}</p>\n'
        for ex in examples:
            ex_attr = ex.replace('"', '&quot;')
            html += f'        <div style="background:#f8f9fa;border-left:3px solid #27ae60;padding:8px 12px;margin-bottom:4px;border-radius:0 6px 6px 0;font-size:15px">{ex} <span style="color:#0066cc;font-weight:600">({len(ex)} chars)</span> <button onclick="copyText(this)" data-text="{ex_attr}" style="background:#0066cc;color:white;border:none;padding:4px 12px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:8px">📋 Copy</button></div>\n'
        html += '      </div>\n'
        return html

    html = '''
  <!-- AI-Written Examples -->
  <div class="section">
    <div class="section-title expandable"><strong>AI-Written Examples (Click to Expand)</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Three click-worthy, keyword-optimized examples each for meta title, meta description,
        H1, H2, H3, and image ALT text. Swap in your brand and refine for tone.
      </p>
'''
    html += example_block("Meta Title (under 60 chars, include core keyword)", meta_titles,
                          "Click-worthy, includes core keyword, stays under 60 characters.")
    html += example_block("Meta Description (120-160 chars, compelling)", meta_descriptions,
                          "Includes keyword, compelling, 120-160 characters.")
    html += example_block("H1 (include keyword, compelling)", h1s)
    html += example_block("H2 (include keyword or secondary keyword)", h2s)
    html += example_block("H3 (include keyword or entity)", h3s)
    html += example_block("Image ALT text (include keyword, descriptive)", alt_texts)
    html += '''    </div>
  </div>

'''
    return html


# ============================================================================
# DATAFORSEO API (original)
# ============================================================================
def dfs_api(endpoint, payload):
    """Call DataForSEO API."""
    login = ENV.get("DATAFORSEO_LOGIN", "")
    password = ENV.get("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        return None
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"https://api.dataforseo.com/v3/{endpoint}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Basic {creds}",
                                          "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return _fix_goto(json.loads(resp.read()), endpoint)

def fetch_serp(keyword):
    """Fetch SERP organic results + AI Overview from DataForSEO."""
    try:
        data = dfs_api("serp/google/organic/live/advanced", [{
            "keyword": keyword,
            "location_code": 2840,
            "language_code": "en",
            "device": "desktop",
            "depth": 100,
            "parse_aiserp": True
        }])
        if not data:
            return [], {}

        items = data["tasks"][0]["result"][0].get("items", [])
        organic = []
        ai_overview = {}

        for item in items:
            itype = item.get("type", "")
            # DataForSEO checks array (replaces deprecated booleans as of Jan 20, 2027)
            item_checks = item.get("checks") or []
            is_featured_snippet = "is_featured_snippet" in item_checks
            is_highly_cited = "is_highly_cited" in item_checks
            
            if itype == "organic":
                organic.append({
                    "rank": item.get("rank_group", 0),
                    "domain": item.get("domain", ""),
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "is_highly_cited": is_highly_cited,
                })
            elif itype == "featured_snippet":
                organic.append({
                    "rank": item.get("rank_group", 0),
                    "domain": item.get("domain", ""),
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                })
            elif itype == "ai_overview":
                markdown = item.get("markdown", "")
                # DataForSEO provides structured citation references. The
                # `references` array holds the real source names in the
                # `source` field; the `domain` field is always google.com
                # (Google redirect URLs), so we use `source` instead.
                references = item.get("references", []) or []
                cited_urls = []
                cited_domains = []
                cited_sources = []
                for ref in references:
                    if not isinstance(ref, dict):
                        continue
                    u = ref.get("url", "") or ""
                    src = ref.get("source", "") or ""
                    if u:
                        cited_urls.append(u)
                    if src and src.lower() not in ("google", "www.google.com", "google.com"):
                        if src not in cited_sources:
                            cited_sources.append(src)
                    # Also try to extract a real domain from the URL if it's
                    # not a google redirect
                    if u and "google.com/goto" not in u and "google.com/search" not in u:
                        try:
                            d = urlparse(u).netloc.lower().replace("www.", "")
                            if d and d not in ("google.com",) and d not in cited_domains:
                                cited_domains.append(d)
                        except Exception:
                            pass
                # Use source names as the primary cited_domains list (these are
                # the actual publishers YouTube, Mid Atlantic Water, etc.)
                if cited_sources:
                    cited_domains = cited_sources
                # Fallback: extract from markdown if no references found
                if not cited_urls:
                    # Format 1: [[1]](url) — double-bracket numbered citations
                    citations = re.findall(r'\[\[(\d+)\]\]\(([^)]+)\)', markdown)
                    if citations:
                        cited_urls = [u for _, u in citations]
                    # Format 2: [1](url) — single-bracket numbered citations
                    if not cited_urls:
                        citations = re.findall(r'\[(\d+)\]\(([^)]+)\)', markdown)
                        if citations:
                            cited_urls = [u for _, u in citations]
                    # Format 3: [text](url) — standard markdown links
                    if not cited_urls:
                        md_links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', markdown)
                        cited_urls = [u for _, u in md_links if u.startswith("http")]
                # Deduplicate URLs while preserving order
                seen = set()
                cited_urls = [u for u in cited_urls if not (u in seen or seen.add(u))]
                products = re.findall(r'\*\*([^*]+)\*\*', markdown)
                text = re.sub(r'\[.*?\]\(.*?\)', '', markdown)
                text = re.sub(r'[*#\[\]]', '', text)
                ai_overview = {
                    "has_ai_overview": True,
                    "markdown": markdown[:10000],
                    "text": text[:5000],
                    "cited_domains": cited_domains,
                    "cited_urls": cited_urls,
                    "citation_count": len(cited_urls) if cited_urls else len(references),
                    "products_mentioned": [p.strip() for p in products[:20] if p.strip()],
                    "source_diversity": len(cited_domains),
                }
            elif itype == "people_also_ask":
                ai_overview.setdefault("paa", []).extend(
                    [q.get("title", "") for q in item.get("items", [])[:8]]
                )
            elif itype in ("reddit", "video", "youtube"):
                ai_overview.setdefault("special", []).append({
                    "type": itype,
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                })

        return organic, ai_overview
    except Exception:
        return [], {}

def fetch_lighthouse(url):
    """Fetch Lighthouse / Core Web Vitals from DataForSEO."""
    try:
        data = dfs_api("on_page/lighthouse/live/json", [{"url": url}])
        if not data:
            return {}

        result = data["tasks"][0]["result"][0]
        # DataForSEO returns Lighthouse data at the TOP LEVEL of result (not nested)
        audits = result.get("audits", {})
        categories = result.get("categories", {})

        # Extract key metrics
        def get_audit(audit_id):
            a = audits.get(audit_id, {})
            return {"score": a.get("score", 0), "displayValue": a.get("displayValue", ""),
                    "title": a.get("title", ""), "description": a.get("description", "")}

        cat_scores = {}
        for cat_id in ["performance", "accessibility", "best-practices", "seo"]:
            cat = categories.get(cat_id, {})
            score = cat.get("score")
            cat_scores[cat_id] = round(score * 100) if score is not None else 0

        metrics = {
            "scores": cat_scores,
            "fcp": get_audit("first-contentful-paint"),
            "lcp": get_audit("largest-contentful-paint"),
            "cls": get_audit("cumulative-layout-shift"),
            "tbt": get_audit("total-blocking-time"),
            "speed_index": get_audit("speed-index"),
            "tti": get_audit("interactive"),
        }
        return metrics
    except Exception:
        return {}

def check_google_index(url):
    """Check Google index status via site: query with fallback to URL search."""
    try:
        clean_url = url.split('?')[0].rstrip('/')
        # Try site: query first
        data = dfs_api("serp/google/organic/live/advanced", [{
            "keyword": f"site:{clean_url}",
            "location_code": 2840,
            "language_code": "en",
            "device": "desktop",
            "use_improved_etv": True,
        }])
        if not data:
            return {"indexed": False, "count": 0}
        items = data["tasks"][0]["result"][0].get("items", [])
        if len(items) > 0:
            return {"indexed": True, "count": len(items)}
        # Fallback: try searching for the URL directly (without site: prefix)
        data2 = dfs_api("serp/google/organic/live/advanced", [{
            "keyword": clean_url,
            "location_code": 2840,
            "language_code": "en",
            "device": "desktop",
            "use_improved_etv": True,
            "limit": 5,
        }])
        if data2:
            items2 = data2["tasks"][0]["result"][0].get("items", [])
            for item in items2:
                if clean_url in item.get("url", ""):
                    return {"indexed": True, "count": 1}
        return {"indexed": False, "count": 0}
    except Exception:
        return {"indexed": False, "count": 0, "error": "API error"}

# ============================================================================
# INFRANODUS
# ============================================================================
def infranodus_analyze(text):
    """Send text to InfraNodus for entity extraction."""
    api_key = ENV.get("INFRANODUS_API_KEY", "")
    if not api_key:
        return {"entity_count": 0, "top_entities": [], "cluster_names": [],
                "content_gap_count": 0, "diversity_score": 0, "error": "No API key"}
    try:
        url = "https://infranodus.com/api/v1/graphAndStatements"
        payload = json.dumps({"text": text[:5000]}).encode()
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())

        graph_data = data.get("entriesAndGraphOfContext", {}).get("graph", {}).get("graphologyGraph", {})
        attrs = graph_data.get("attributes", {})
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])
        clusters = attrs.get("allClusters", [])
        gaps = attrs.get("gaps", [])
        diversity = attrs.get("diversity_stats", {})

        entity_labels = [n.get("key", "") for n in nodes]
        return {
            "entity_count": len(nodes),
            "edge_count": len(edges),
            "cluster_count": len(clusters),
            "content_gap_count": len(gaps),
            "diversity_score": 1.0 if diversity.get("diversity_score") == "diverse" else 0.5 if diversity.get("diversity_score") == "normal" else 0.0,
            "top_entities": entity_labels[:20],
            "cluster_names": [c.get("aiName", "") for c in clusters[:8]],
        }
    except Exception as e:
        return {"entity_count": 0, "top_entities": [], "cluster_names": [],
                "content_gap_count": 0, "diversity_score": 0, "error": str(e)}

# ============================================================================
# SRS COMPOSITE SCORING
# ============================================================================
def compute_srs(technical_issues, content_result, factors, lighthouse):
    """Compute SRS composite score: Structure + Relevance + Signal."""

    # --- STRUCTURE (0-100) ---
    # Based on technical SEO health, heading structure, schema
    structure = 100
    c = len(technical_issues.get("Critical", []))
    w = len(technical_issues.get("Warning", []))
    o = len(technical_issues.get("Opportunity", []))
    n = len(technical_issues.get("Notice", []))
    structure -= c * 8
    structure -= w * 4
    structure -= o * 1.5
    structure -= n * 0.5

    # Heading structure quality
    h1 = factors.get("h1_count", 0)
    if h1 != 1:
        structure -= 10
    h2 = factors.get("h2_count", 0)
    if h2 < 2:
        structure -= 5

    # Schema presence
    schema_count = factors.get("schema_type_count", 0)
    structure += min(10, schema_count * 2)

    structure = max(0, min(100, structure))

    # --- RELEVANCE (0-100) ---
    # Based on keyword presence, LSI coverage, content alignment
    relevance = 0
    if factors.get("title_keyword_present", 0):
        relevance += 15
    if factors.get("h1_keyword_present", 0):
        relevance += 15
    if factors.get("keyword_in_first_100", 0):
        relevance += 10
    if factors.get("h2_keyword_present", 0):
        relevance += 10
    relevance += min(20, factors.get("lsi_coverage_pct", 0) * 0.2)
    relevance += min(10, factors.get("keyword_density", 0) * 3)
    if factors.get("keyword_in_bold", 0):
        relevance += 5
    if factors.get("keyword_in_lists", 0):
        relevance += 5
    if factors.get("keyword_in_tables", 0):
        relevance += 5
    if factors.get("image_alt_keyword", 0):
        relevance += 5

    # Content score from content_auditor
    if content_result:
        relevance += content_result.get("overall_score", 0) * 0.10

    relevance = max(0, min(100, round(relevance)))

    # --- SIGNAL (0-100) ---
    # Based on authority, E-E-A-T, backlinks, social, AI Overview
    signal = 0
    if content_result:
        signal += content_result.get("scores", {}).get("authority_e_e_a_t", 0) * 0.3
        signal += content_result.get("scores", {}).get("factuality", 0) * 0.2
        signal += content_result.get("scores", {}).get("information_gain", 0) * 0.2

    # Social signals
    if factors.get("og_title", 0):
        signal += 3
    if factors.get("og_image", 0):
        signal += 3
    if factors.get("twitter_card", 0):
        signal += 3
    if factors.get("hreflang_present", 0):
        signal += 3

    # Lighthouse performance signal
    if lighthouse and lighthouse.get("scores"):
        perf = lighthouse["scores"].get("performance", 0)
        signal += perf * 0.11

    # HTTPS
    if factors.get("url_https", 0):
        signal += 3

    signal = max(0, min(100, round(signal)))

    # Composite
    composite = round(structure * 0.33 + relevance * 0.33 + signal * 0.34)

    return {
        "structure": round(structure),
        "relevance": round(relevance),
        "signal": round(signal),
        "composite": composite,
    }

# ============================================================================
# FIX LIST GENERATION
# ============================================================================
def analyze_citation_readiness(page_text, soup, factors, content_result, serp_data):
    """Analyze page against the 7 markers of AI citation from the Cited Page Swipe File."""
    import re as _re
    markers = {}
    text_lower = page_text.lower() if page_text else ""
    words = text_lower.split()[:100] if text_lower else []
    first_100 = ' '.join(words)
    
    # Marker 1: Direct answer up top
    answer_patterns = [r'\$[\d,]+', r'\d+%', r'\d+\s*(years?|months?|days?|hours?)', r'\b(yes|no)\b', r'\b\d{4}\b', r'\d+\s*(ppm|gpg|grains?)', r'\d+\s*(sq\s*ft|square\s*feet)', r'\d+\s*(gallons?|gpm)']
    has_answer = any(_re.search(p, first_100, _re.I) for p in answer_patterns)
    if has_answer:
        markers['direct_answer'] = {'score': 8, 'status': 'pass', 'finding': 'First 100 words contain a concrete answer.', 'recommendation': 'Keep the direct answer in the first paragraph.'}
    else:
        markers['direct_answer'] = {'score': 3, 'status': 'fail', 'finding': 'No direct answer in first 100 words.', 'recommendation': 'Add a concrete answer (price, timeframe, verdict) in the first paragraph.'}
    
    # Marker 2: Question-shaped headings
    headings = []
    if soup:
        for tag in soup.find_all(['h2', 'h3']):
            h_text = tag.get_text(strip=True)
            if h_text:
                headings.append(h_text)
    question_headings = [h for h in headings if '?' in h or _re.match(r'^(what|why|how|when|where|which|best|can|do|should|is|are)\b', h, _re.I)]
    q_ratio = len(question_headings) / max(len(headings), 1)
    if q_ratio >= 0.3:
        markers['question_headings'] = {'score': 8, 'status': 'pass', 'finding': f'{len(question_headings)}/{len(headings)} headings are question-shaped ({q_ratio:.0%}).', 'recommendation': 'Keep question-shaped headings.'}
    elif q_ratio > 0:
        markers['question_headings'] = {'score': 5, 'status': 'partial', 'finding': f'{len(question_headings)}/{len(headings)} question-shaped ({q_ratio:.0%}).', 'recommendation': 'Rewrite more H2s as buyer questions.'}
    else:
        markers['question_headings'] = {'score': 2, 'status': 'fail', 'finding': 'No question-shaped headings.', 'recommendation': 'Rewrite H2s as questions buyers ask.'}
    
    # Marker 3: Quotable blocks
    paragraphs = []
    if soup:
        for p in soup.find_all('p'):
            p_text = p.get_text(strip=True)
            if len(p_text) > 20:
                paragraphs.append(p_text)
    quotable = 0
    transitions = ['however', 'additionally', 'moreover', 'furthermore', 'consequently', 'nevertheless', 'thus', 'therefore', 'also', 'but', 'while', 'although']
    for p in paragraphs:
        sentences = _re.split(r'[.!?]+', p)
        sentence_count = len([s for s in sentences if len(s.strip()) > 10])
        first_word = p.split()[0].lower() if p.split() else ''
        if 1 <= sentence_count <= 3 and first_word not in transitions:
            quotable += 1
    q_block_ratio = quotable / max(len(paragraphs), 1)
    if q_block_ratio >= 0.5:
        markers['quotable_blocks'] = {'score': 8, 'status': 'pass', 'finding': f'{quotable}/{len(paragraphs)} paragraphs are quotable ({q_block_ratio:.0%}).', 'recommendation': 'Maintain self-contained paragraphs.'}
    elif q_block_ratio >= 0.25:
        markers['quotable_blocks'] = {'score': 5, 'status': 'partial', 'finding': f'{quotable}/{len(paragraphs)} quotable ({q_block_ratio:.0%}).', 'recommendation': 'Rewrite paragraphs to be self-contained. Remove transition openers.'}
    else:
        markers['quotable_blocks'] = {'score': 3, 'status': 'fail', 'finding': f'Only {quotable}/{len(paragraphs)} quotable.', 'recommendation': 'Rewrite as 1-3 sentence self-contained claims.'}
    
    # Marker 4: First-party evidence
    evidence_patterns = [r'\$[\d,]+', r'\d+%', r'\d+\s*(years?|customers?|tested|measured)', r'\b(our results|our testing|we tested|we measured|in our lab)\b', r'\b(since\s+\d{4}|established\s+\d{4})\b', r'\b(NSF|ANSI|certified|certification)\b', r'\b(warranty|guarantee)\b']
    evidence_count = sum(1 for p in evidence_patterns if _re.search(p, text_lower, _re.I))
    if evidence_count >= 3:
        markers['first_party_evidence'] = {'score': 9, 'status': 'pass', 'finding': f'Strong evidence: {evidence_count} types found.', 'recommendation': 'Keep evidence specific and verifiable.'}
    elif evidence_count >= 1:
        markers['first_party_evidence'] = {'score': 5, 'status': 'partial', 'finding': f'Some evidence ({evidence_count} types).', 'recommendation': 'Add more original numbers and certifications.'}
    else:
        markers['first_party_evidence'] = {'score': 2, 'status': 'fail', 'finding': 'No first-party evidence.', 'recommendation': 'Add original data: test results, prices, certifications.'}
    
    # Marker 5: Visible author identity
    author_found = False
    author_patterns = [r'written by', r'author:', r'about the author', r'by\s+[A-Z][a-z]+\s+[A-Z]', r'reviewed by', r'expert:', r'contributor:']
    for p in author_patterns:
        if _re.search(p, text_lower, _re.I):
            author_found = True
            break
    schema_types = content_result.get('schema', {}).get('found', []) if content_result else []
    has_person_schema = any('Person' in s or 'Author' in s for s in schema_types)
    if author_found and has_person_schema:
        markers['author_identity'] = {'score': 9, 'status': 'pass', 'finding': 'Author on page + Person schema.', 'recommendation': 'Keep author visible with credentials.'}
    elif author_found:
        markers['author_identity'] = {'score': 6, 'status': 'partial', 'finding': 'Author on page but no Person schema.', 'recommendation': 'Add Person schema with name and credentials.'}
    elif has_person_schema:
        markers['author_identity'] = {'score': 5, 'status': 'partial', 'finding': 'Person schema but no visible byline.', 'recommendation': 'Add visible author byline on page.'}
    else:
        markers['author_identity'] = {'score': 2, 'status': 'fail', 'finding': 'No author identity.', 'recommendation': 'Add named author with credentials + Person schema.'}
    
    # Marker 6: Schema as served
    if content_result and content_result.get('schema', {}).get('found'):
        found_types = content_result['schema']['found']
        missing_types = content_result['schema'].get('missing', [])
        has_article = 'Article' in found_types
        has_faq = 'FAQPage' in found_types
        has_product = 'Product' in found_types
        if has_article and has_faq:
            markers['schema_served'] = {'score': 9, 'status': 'pass', 'finding': f'Strong schema: {", ".join(found_types)}.', 'recommendation': 'Keep Article + FAQPage served.'}
        elif has_article or has_faq or has_product:
            markers['schema_served'] = {'score': 7, 'status': 'pass', 'finding': f'Good schema: {", ".join(found_types)}.', 'recommendation': f'Add: {", ".join(missing_types[:3])}.'}
        else:
            markers['schema_served'] = {'score': 4, 'status': 'partial', 'finding': f'Basic schema: {", ".join(found_types)}.', 'recommendation': 'Add Article + FAQPage schema.'}
    else:
        # Page-type-aware schema recommendations
        _is_product_page = '/products/' in url.lower() if url else False
        if _is_product_page:
            markers['schema_served'] = {'score': 2, 'status': 'fail', 'finding': 'No schema detected.', 'recommendation': 'Add Product + Offer + AggregateRating JSON-LD. Also add Person schema for author (Craig Phillips) to strengthen E-E-A-T.'}
        else:
            markers['schema_served'] = {'score': 2, 'status': 'fail', 'finding': 'No schema detected.', 'recommendation': 'Add Article, Person, FAQPage JSON-LD.'}
    
    # Marker 7: Cluster membership
    internal_links = factors.get('internal_links', 0) if factors else 0
    nav_links = factors.get('nav_links', 0) if factors else 0
    content_links = internal_links - nav_links
    if content_links >= 10:
        markers['cluster_membership'] = {'score': 8, 'status': 'pass', 'finding': f'{content_links} internal content links.', 'recommendation': 'Maintain cluster depth.'}
    elif content_links >= 5:
        markers['cluster_membership'] = {'score': 5, 'status': 'partial', 'finding': f'{content_links} content links.', 'recommendation': 'Add more internal links. Target 10+.'}
    else:
        markers['cluster_membership'] = {'score': 3, 'status': 'fail', 'finding': f'Only {content_links} content links.', 'recommendation': 'Add internal links from related pages.'}
    
    total_score = sum(m['score'] for m in markers.values())
    checklist = [
        {'q': 'Is the question answered in the first screen?', 'pass': markers['direct_answer']['status'] == 'pass'},
        {'q': 'Could a stranger quote 3 consecutive sentences and have them make sense alone?', 'pass': markers['quotable_blocks']['status'] == 'pass'},
        {'q': 'Is there first-party evidence only you could produce?', 'pass': markers['first_party_evidence']['status'] == 'pass'},
        {'q': 'Are headings questions a buyer would type?', 'pass': markers['question_headings']['status'] == 'pass'},
        {'q': 'Is a named human with credentials on the page?', 'pass': markers['author_identity']['status'] == 'pass'},
        {'q': 'Does the schema pass the served-check?', 'pass': markers['schema_served']['status'] == 'pass'},
        {'q': 'Do at least 2 sibling pages link here?', 'pass': markers['cluster_membership']['status'] == 'pass'},
    ]
    pass_count = sum(1 for c in checklist if c['pass'])
    return {'markers': markers, 'total_score': total_score, 'max_score': 70, 'checklist': checklist, 'pass_count': pass_count, 'publish_ready': pass_count >= 5}


def generate_fix_lists(technical_issues, content_result, factors, srs, lighthouse):
    """Generate dual fix lists: Developer + SEO Team, grouped by P0/P1/P2."""

    dev_p0, dev_p1, dev_p2 = [], [], []
    seo_p0, seo_p1, seo_p2 = [], [], []

    # --- From technical issues ---
    for issue in technical_issues.get("Critical", []):
        dev_p0.append(issue)
    for issue in technical_issues.get("Warning", []):
        # Categorize dev vs seo
        if any(kw in issue.lower() for kw in ["canonical", "meta", "html", "tag", "dom", "size", "comment", "gtm", "viewport", "lang"]):
            dev_p1.append(issue)
        elif any(kw in issue.lower() for kw in ["title", "description", "h1", "h2", "h3", "heading"]):
            seo_p1.append(issue)
        else:
            dev_p1.append(issue)
    for issue in technical_issues.get("Opportunity", []):
        if any(kw in issue.lower() for kw in ["image", "alt", "dimension", "webp", "video", "gif", "comment"]):
            dev_p2.append(issue)
        else:
            seo_p2.append(issue)
    for issue in technical_issues.get("Notice", []):
        if any(kw in issue.lower() for kw in ["title", "description", "h1", "h2", "text to code"]):
            seo_p2.append(issue)
        else:
            dev_p2.append(issue)

    # --- From content auditor ---
    if content_result:
        for item in content_result.get("needs_attention", []):
            severity = item.get("severity", "yellow")
            fix = f"{item['issue']}: {item['fix']}"
            if severity == "red":
                seo_p0.append(fix)
            elif severity == "yellow":
                seo_p1.append(fix)

        # Schema missing
        for schema in content_result.get("schema", {}).get("missing", []):
            dev_p1.append(f"Add {schema} schema markup")

    # --- From factors ---
    if factors.get("h1_count", 0) != 1:
        seo_p0.append("Fix H1 count: exactly 1 H1 required")
    if factors.get("canonical_present", 0) == 0:
        dev_p0.append("Add canonical link tag")
    if factors.get("title_keyword_present", 0) == 0:
        seo_p0.append("Keyword missing from <title> tag (keyword is in H1 but NOT in title tag — check Shopify theme title template)")
    if factors.get("h1_keyword_present", 0) == 0:
        seo_p0.append("Add target keyword to H1")
    if factors.get("og_title", 0) == 0:
        dev_p1.append("Add og:title meta tag")
    if factors.get("og_image", 0) == 0:
        dev_p1.append("Add og:image meta tag")
    if factors.get("twitter_card", 0) == 0:
        dev_p1.append("Add twitter:card meta tag")
    if factors.get("images_without_alt", 0) > 0:
        seo_p1.append(f"Add alt text to {factors['images_without_alt']} images without alt")
    if factors.get("lsi_coverage_pct", 0) < 50:
        seo_p2.append(f"Increase LSI term coverage (currently {factors.get('lsi_coverage_pct', 0):.0f}%)")
    if factors.get("has_faq_section", 0) == 0:
        seo_p2.append("Add FAQ section for passage optimization")
    if factors.get("json_ld_count", 0) == 0:
        dev_p1.append("Add JSON-LD structured data")
    if factors.get("hreflang_present", 0) == 0:
        dev_p2.append("Add hreflang tags for international targeting")

    # --- From Lighthouse ---
    if lighthouse and lighthouse.get("scores"):
        perf = lighthouse["scores"].get("performance", 0)
        if perf < 50:
            dev_p0.append("Critical: Lighthouse performance score below 50")
        elif perf < 90:
            dev_p1.append(f"Improve Lighthouse performance (currently {perf})")

        for cat in ["accessibility", "best-practices", "seo"]:
            cat_score = lighthouse["scores"].get(cat, None)
            if cat_score is not None and cat_score < 80:
                dev_p1.append(f"Improve Lighthouse {cat} score (currently {cat_score})")

    # --- From SRS ---
    if srs["structure"] < 50:
        dev_p0.append("Structure score critical — fix technical SEO fundamentals")
    if srs["relevance"] < 50:
        seo_p0.append("Relevance score critical — optimize keyword targeting")
    if srs["signal"] < 50:
        seo_p1.append("Signal score low — improve E-E-A-T and authority signals")

    # Dedupe
    def dedupe(lst):
        seen = set()
        result = []
        for item in lst:
            if item not in seen and item:
                seen.add(item)
                result.append(item)
        return result

    return {
        "developer": {"P0": dedupe(dev_p0), "P1": dedupe(dev_p1), "P2": dedupe(dev_p2)},
        "seo_team": {"P0": dedupe(seo_p0), "P1": dedupe(seo_p1), "P2": dedupe(seo_p2)},
    }

# ============================================================================
# STRATEGIC RECOMMENDATIONS
# ============================================================================
def generate_recommendations(srs, content_result, factors, lighthouse, serp, ai_overview, infranodus):
    """Generate strategic recommendations."""
    recs = []

    if srs["composite"] < 50:
        recs.append(("<strong>Critical Priority:</strong>", "Overall SRS score is below 50. Focus on fixing all P0 items before anything else."))
    elif srs["composite"] < 70:
        recs.append(("<strong>Improvement Needed:</strong>", "SRS score is moderate. Address P0 and P1 items to reach competitive levels."))

    if srs["structure"] < 60:
        recs.append(("<strong>Structure:</strong>", "Fix technical SEO foundations: canonical tags, heading hierarchy, schema markup, and HTML validation."))
    if srs["relevance"] < 60:
        # Build relevance recommendation based on actual factor values
        rel_issues = []
        if factors.get("title_keyword_present", 0) == 0:
            rel_issues.append("title tag")
        if factors.get("h1_keyword_present", 0) == 0:
            rel_issues.append("H1")
        if factors.get("keyword_in_first_100", 0) == 0:
            rel_issues.append("first 100 words")
        if factors.get("lsi_coverage_pct", 0) < 50:
            rel_issues.append(f"LSI term coverage (currently {factors.get('lsi_coverage_pct', 0):.0f}%)")
        
        if rel_issues:
            recs.append(("<strong>Relevance:</strong>", f"Improve keyword targeting: add keyword to {', '.join(rel_issues)}."))
        else:
            recs.append(("<strong>Relevance:</strong>", "Keyword targeting is good - keyword found in H1 and first 100 words of content."))
    if srs["signal"] < 60:
        recs.append(("<strong>Signal:</strong>", "Boost authority signals: add citations, author schema, trust badges, and improve page speed."))

    if content_result:
        scores = content_result.get("scores", {})
        if scores.get("accuracy_title_alignment", 100) < 70:
            recs.append(("<strong>Title-to-Query Alignment:</strong>", "Update title tag and H1 to include the exact target keyword phrase."))
        if scores.get("authority_e_e_a_t", 100) < 70:
            recs.append(("<strong>E-E-A-T:</strong>", "Add author schema, Organization schema, contact info, and trust signals (certifications, guarantees)."))
        if scores.get("factuality", 100) < 70:
            recs.append(("<strong>Factuality:</strong>", "Add citations, data sources, and authority outbound links (.gov/.edu/.org)."))
        if scores.get("information_gain", 100) < 70:
            recs.append(("<strong>Information Gain:</strong>", "Add original analysis, unique data, and reduce template/boilerplate content."))
        if scores.get("content_hierarchy", 100) < 70:
            recs.append(("<strong>Content Hierarchy:</strong>", "Fix heading structure — exactly 1 H1 with keyword, 2-20 H2s, no hierarchy skips."))
        if scores.get("content_format", 100) < 70:
            recs.append(("<strong>Content Format:</strong>", "Adjust word count, paragraph count, and link ratios to target ranges."))

    if factors.get("lsi_coverage_pct", 0) < 50:
        recs.append(("<strong>LSI Coverage:</strong>", f"Only {factors.get('lsi_coverage_pct', 0):.0f}% of LSI terms found. Add related terms to strengthen topical relevance."))

    if factors.get("has_faq_section", 0) == 0:
        recs.append(("<strong>FAQ Section:</strong>", "Add an FAQ section to capture passage-based rankings and featured snippets."))

    if ai_overview and ai_overview.get("has_ai_overview"):
        recs.append(("<strong>AI Overview:</strong>", f"Google AI Overview exists for this keyword with {ai_overview.get('citation_count', 0)} citations. Optimize content to get cited."))
    else:
        recs.append(("<strong>AI Overview:</strong>", "No AI Overview detected yet. Structure content with clear Q&A format to increase chances of citation."))

    if infranodus and infranodus.get("content_gap_count", 0) > 3:
        recs.append(("<strong>Content Gaps:</strong>", f"InfraNodus found {infranodus['content_gap_count']} content gaps. Add missing entities to improve topical coverage."))

    if serp:
        target_domain = ""
        recs.append(("<strong>SERP Competition:</strong>", f"Analyze top {min(10, len(serp))} competitors for content length, structure, and schema patterns to match."))

    if lighthouse and lighthouse.get("scores"):
        perf = lighthouse["scores"].get("performance", 0)
        if perf < 90:
            recs.append(("<strong>Page Speed:</strong>", f"Lighthouse performance is {perf}/100. Optimize images, reduce JS, and enable lazy loading."))

    # === LLM Citation Targeting Recommendations ===
    _scripts_dir = os.path.join(os.path.expanduser("~/.hermes"), "scripts")
    _llm_domains = {}
    _llm_playbook = {}
    try:
        with open(os.path.join(_scripts_dir, "llm_citation_domains.json")) as _f:
            _llm_domains = json.load(_f)
    except Exception:
        pass
    try:
        with open(os.path.join(_scripts_dir, "llm_playbook_data.json")) as _f:
            _llm_playbook = json.load(_f)
    except Exception:
        pass

    if _llm_domains:
        _risk_emoji = {"S": "\U0001f7e2", "A": "\U0001f7e1", "B": "\U0001f7e0", "C": "\U0001f534"}
        _pb_domains = _llm_playbook.get("domains", {})
        _councils = _llm_playbook.get("councils", [])

        # Check if page has LLM-favorite backlinks (via mm_ factors)
        _mm_count = factors.get("mm_match_count", 0)
        _mm_strong = factors.get("mm_strong_links", 0)
        if _mm_count > 0:
            recs.append(("<strong>\U0001f916 LLM Citation Backlinks:</strong>",
                f"Page has {_mm_count} Majestic Million backlinks ({_mm_strong} strong). "
                f"Targeting LLM-favorite domains can dramatically increase AI Overview and LLM citation rates."))
        else:
            recs.append(("<strong>\U0001f916 LLM Citation Opportunity:</strong>",
                "No LLM-favorite domain backlinks detected. Targeting LLM citation sources is the "
                "highest-leverage off-page strategy for AI search visibility."))

        # Top 5 S-tier platform recommendations with risk levels
        _s_tier = [(d, info) for d, info in _pb_domains.items() if info.get("tier") == "S"][:5]
        if _s_tier:
            _platform_lines = []
            for _dom, _info in _s_tier:
                _risk = _risk_emoji.get(_info.get("tier", ""), "\U0001f7e1")
                _platform_lines.append(f"{_risk} <strong>{_dom}</strong>: {_info.get('strategy', '')}")
            recs.append(("<strong>\U0001f916 Top LLM Target Platforms (S-Tier):</strong>",
                "<br>".join(_platform_lines)))

        # Council pricing recommendations
        _affordable = [c for c in _councils
                       if "$" in c.get("cost", "") and "Undisclosed" not in c.get("cost", "")][:5]
        if _affordable:
            _council_lines = []
            for c in _affordable:
                _council_lines.append(f"&bull; {c['council']} ({c['host']}): {c['cost']}")
            recs.append(("<strong>\U0001f916 LLM Council Programs (Paid Paths):</strong>",
                "<br>".join(_council_lines)
                + "<br><em>Note: Council links are typically nofollow but drive LLM citation via brand visibility.</em>"))

        # Reddit-specific recommendation
        if "reddit.com" in _pb_domains:
            _rd = _pb_domains["reddit.com"]
            recs.append(("<strong>\U0001f916 Reddit Strategy:</strong>",
                f"Target Reddit with niche subreddit engagement. "
                f"Strategy: {_rd.get('strategy', 'Ads + niche subreddit comments + AMAs')}. "
                "Reddit is an S-tier LLM citation source \u2014 actively cited by ChatGPT, Claude, and Perplexity."))

        # Forbes Council recommendation with pricing
        _forbes_councils = [c for c in _councils
                            if "forbes.com" in c.get("host", "")
                            and "standard" in c.get("council", "").lower()]
        if _forbes_councils:
            _fc = _forbes_councils[0]
            recs.append(("<strong>\U0001f916 Forbes Council:</strong>",
                f"Pursue Forbes Council at {_fc.get('cost', '$3,300')}/yr for LLM citation. "
                "S-tier domain heavily cited by LLMs. All Forbes Council links are nofollow, "
                "but the brand visibility drives organic citations from LLMs."))

    return recs

# ============================================================================
# GROUP FACTORS
# ============================================================================
FACTOR_GROUPS = {
    "On-Page": ["title_length", "title_keyword_present", "title_keyword_position", "title_keyword_at_start",
                "title_keyword_density", "h1_count", "h1_length", "h1_keyword_present", "h1_keyword_position",
                "h2_count", "h2_keyword_present", "h2_keyword_in_first", "h3_count", "h4_count", "h5_count", "h6_count",
                "word_count", "char_count", "keyword_count", "keyword_density", "keyword_in_first_100",
                "keyword_in_last_100", "paragraph_count", "avg_paragraph_length", "paragraph_length_variance",
                "sentence_count", "avg_sentence_length", "bold_count", "keyword_in_bold",
                "ul_count", "ol_count", "li_count", "keyword_in_lists", "table_count", "table_rows", "keyword_in_tables"],
    "Links": ["internal_links", "external_links", "total_links", "nofollow_links", "exact_match_anchors",
              "partial_match_anchors", "branded_anchors", "external_unique_domains", "link_density",
              "nav_links", "footer_links", "body_links", "anchor_diversity_ratio", "broken_links",
              "internal_nofollow", "external_nofollow"],
    "Technical": ["url_length", "url_depth", "url_has_keyword", "url_hyphens", "url_underscores",
                  "url_https", "url_www", "canonical_present", "canonical_self_referencing",
                  "meta_robots_present", "meta_robots_noindex", "meta_robots_nofollow",
                  "page_size_kb", "html_size_kb", "css_count", "js_count", "font_count",
                  "dom_total_elements", "dom_tag_diversity", "dom_max_depth",
                  "og_title", "og_description", "og_image", "og_type", "twitter_card",
                  "hreflang_present", "hreflang_count"],
    "Schema": ["json_ld_count", "has_product_schema", "has_review_schema", "has_aggregaterating_schema",
               "has_faqpage_schema", "has_organization_schema", "has_breadcrumblist_schema",
               "has_article_schema", "has_videoobject_schema", "has_itempage_schema", "has_webpage_schema",
               "schema_type_count", "schema_property_count", "sameas_count", "has_microdata", "has_rdfa"],
    "Semantic": ["lsi_term_count", "lsi_coverage_pct", "has_faq_section", "faq_question_count",
                 "has_comparison", "has_spec_table", "has_howto", "has_reviews",
                 "has_intro_body_conclusion", "passage_qa_count", "has_eav_table",
                 "infr_entity_count", "infr_edge_count", "infr_cluster_count",
                 "infr_content_gap_count", "infr_diversity_score"],
    "Media": ["image_count", "images_with_alt", "images_without_alt", "image_alt_keyword",
              "image_to_text_ratio", "webp_images", "jpg_images", "png_images", "svg_images",
              "lazy_loading", "video_count", "audio_count"],
    "AI Overview": ["ai_target_mentioned", "ai_target_cited", "ai_citation_count", "ai_source_diversity",
                    "ai_overview_length", "ai_product_count", "ai_competitor_citations",
                    "ai_entity_overlap_pct", "ai_has_overview", "ai_markdown_length"],
    "Shopify": ["shopify_cdn", "shopify_theme", "shopify_section", "cart_link", "checkout_link",
                "is_product_page", "is_collection_page", "is_blog_page", "is_page", "shopify_app",
                "liquid_indicator", "variant_url", "shopify_metafield", "shopify_money_format",
                "shopify_analytics", "shopify_storefront", "shopify_currency", "shopify_locale",
                "has_buy_button", "has_shopify_reviews"],
    "Backlinks": ["mm_match_count", "mm_match_rate", "mm_top_rank", "mm_avg_rank", "mm_median_rank",
                  "mm_ref_domains_checked", "mm_tier_targets", "mm_tier_priority_score",
                  "mm_opportunity_score", "mm_in_majestic", "mm_majestic_rank",
                  "mm_strong_links", "mm_medium_links", "mm_weak_links"],
}

# ============================================================================
# ISSUE FIX MAP — descriptions and fix recommendations for each technical SEO issue
# ============================================================================

ISSUE_FIX_MAP = {
    "4xx client errors": {
        "desc": "The page returned a 4xx HTTP status code (403, 404, 410, etc.), meaning the page is inaccessible or does not exist.",
        "fix": "Check the URL for typos. If the page was deleted, restore it or add a 301 redirect to a relevant replacement page. Check server access permissions."
    },
    "3xx redirects": {
        "desc": "The page redirects to another URL using a 3xx status code. While redirects are sometimes necessary, chains or unnecessary redirects slow down page loading.",
        "fix": "Update internal links to point directly to the final destination URL. Avoid redirect chains (A→B→C). Use 301 for permanent redirects, not 302."
    },
    "301 redirects": {
        "desc": "The page uses a 301 permanent redirect. This is correct for permanently moved pages but should be eliminated by updating internal links.",
        "fix": "Update all internal links pointing to the old URL to point directly to the new URL. This eliminates unnecessary redirect overhead."
    },
    "Meta noindex pages": {
        "desc": "The page has a noindex meta tag, which tells search engines not to index this page in search results.",
        "fix": "If this page should be indexed, remove the noindex meta tag. If it should not be indexed (e.g., thank-you pages), this is correct — no action needed."
    },
    "Meta nofollow pages": {
        "desc": "The page has a nofollow meta tag, which tells search engines not to follow links on this page.",
        "fix": "If you want search engines to follow links on this page, remove the nofollow meta tag. Keep it for pages like login or admin areas."
    },
    "Canonical is missing": {
        "desc": "The page does not have a canonical link tag. Without it, search engines may index duplicate versions of the same page.",
        "fix": "Add a <link rel=\"canonical\"> tag in the <head> pointing to the preferred version of this page URL."
    },
    "Canonical != URL": {
        "desc": "The canonical URL does not match the current page URL. This may be intentional (e.g., canonical to a category page) but can also be a mistake.",
        "fix": "Verify the canonical URL is correct. If this page should be canonical to itself, update the canonical tag to match the current URL."
    },
    "Canonical points to a noindex URL": {
        "desc": "The canonical URL points to a page that has a noindex tag. This effectively tells search engines not to index either page.",
        "fix": "Remove the noindex tag from the canonical target page, or change the canonical to point to an indexable URL."
    },
    "More than one <body> tag on page": {
        "desc": "The page contains multiple <body> tags, which is invalid HTML and can cause rendering issues.",
        "fix": "Remove the extra <body> tags. There should be exactly one <body> tag per HTML document."
    },
    "More than one <head> tag on page": {
        "desc": "The page contains multiple <head> tags, which is invalid HTML.",
        "fix": "Merge the contents of all <head> tags into a single <head> element."
    },
    "More than one <html> tag on page": {
        "desc": "The page contains multiple <html> tags, which is invalid HTML.",
        "fix": "Remove the extra <html> tags. There should be exactly one <html> tag wrapping the entire document."
    },
    "Multiple viewport <meta> tags": {
        "desc": "The page has more than one viewport meta tag, which can cause mobile rendering conflicts.",
        "fix": "Remove duplicate viewport meta tags. Keep only one: <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    },
    "HTML lang attribute missing": {
        "desc": "The <html> tag does not have a lang attribute, which helps search engines and screen readers identify the page language.",
        "fix": "Add lang attribute to the <html> tag: <html lang=\"en\">"
    },
    "Description is missing": {
        "desc": "The page has no meta description tag. Search engines will auto-generate a snippet, which may not be compelling.",
        "fix": "Add a meta description tag (120-160 characters) that includes the target keyword and a clear value proposition to drive clicks."
    },
    "Description is empty": {
        "desc": "The meta description tag exists but is empty.",
        "fix": "Write a compelling meta description (120-160 characters) that includes the target keyword and encourages clicks."
    },
    "Description is too long": {
        "desc": "The meta description exceeds 160 characters, so search engines will truncate it in search results.",
        "fix": "Shorten the meta description to 120-160 characters. Keep the most important text and keyword at the beginning."
    },
    "Description is too short": {
        "desc": "The meta description is under 120 characters, which may not fully utilize the available snippet space.",
        "fix": "Expand the meta description to 120-160 characters to maximize snippet real estate in search results."
    },
    "H1 is missing": {
        "desc": "The page has no H1 heading. The H1 is the most important on-page heading and helps search engines understand the page topic.",
        "fix": "Add a single H1 heading that includes the target keyword and clearly describes the page content."
    },
    "H1 duplicates": {
        "desc": "Multiple pages on the site have the same H1 heading, which can dilute topical relevance.",
        "fix": "Make each page's H1 unique and specific to that page's content and target keyword."
    },
    "H1 too long": {
        "desc": "The H1 heading exceeds 60 characters, which may get truncated in some search results.",
        "fix": "Shorten the H1 to under 60 characters while keeping the target keyword near the beginning."
    },
    "H1 too short": {
        "desc": "The H1 heading is very short, which may not provide enough context for search engines or users.",
        "fix": "Expand the H1 to include more descriptive text while keeping the target keyword."
    },
    "H1 = Title": {
        "desc": "The H1 heading is identical to the page title tag. While not harmful, having different text allows targeting variations.",
        "fix": "Consider making the H1 slightly different from the title tag to target keyword variations and improve semantic relevance."
    },
    "H2 is missing": {
        "desc": "The page has no H2 subheadings. H2s help structure content and target secondary keywords.",
        "fix": "Add 2-5 H2 subheadings that break up content into logical sections and include secondary/related keywords."
    },
    "H2 starts with a lowercase letter": {
        "desc": "An H2 heading starts with a lowercase letter, which looks unprofessional.",
        "fix": "Capitalize the first letter of each H2 heading."
    },
    "H2 has other tags inside": {
        "desc": "An H2 heading contains other HTML tags (like links or spans), which can dilute heading relevance.",
        "fix": "Remove nested tags from H2 headings. Keep H2 text clean and focused on the heading text."
    },
    "Title too long": {
        "desc": "The title tag exceeds 60 characters, so it will be truncated in search results.",
        "fix": "Shorten the title to 50-60 characters. Keep the target keyword near the beginning."
    },
    "Title too short": {
        "desc": "The title tag is under 30 characters, which wastes valuable search result real estate.",
        "fix": "Expand the title to 50-60 characters to include the target keyword and a compelling value proposition."
    },
    "Text to code ratio < 10%": {
        "desc": "The ratio of visible text to HTML code is below 10%, indicating heavy code overhead relative to content.",
        "fix": "Reduce HTML bloat by removing inline styles, minimizing JavaScript, and moving CSS to external files. Add more text content."
    },
    "Page has alt tags with one word": {
        "desc": "Some images have alt text that is only one word, which does not provide enough context for accessibility or SEO.",
        "fix": "Expand alt text to descriptive phrases (3-7 words) that include relevant keywords and accurately describe the image."
    },
    "Page has no strong importance elements": {
        "desc": "The page does not use <strong> or <b> tags to emphasize important text.",
        "fix": "Add <strong> tags around important keywords and phrases to signal their importance to search engines."
    },
    "Page has no list markdown": {
        "desc": "The page does not use any <ul> or <ol> lists. Lists improve readability and can win featured snippets.",
        "fix": "Add bullet or numbered lists for steps, features, comparisons, or summaries to improve readability and snippet eligibility."
    },
    "Paragraphs are missing": {
        "desc": "The page has very few or no paragraph elements, which may indicate thin content or poor HTML structure.",
        "fix": "Add well-structured paragraphs (3-7 sentences each) with relevant content to improve readability and content depth."
    },
    "Page has identical headings": {
        "desc": "Multiple pages on the site have identical heading structures, which can signal duplicate content.",
        "fix": "Make heading structures unique across pages. Each page should have distinct H1, H2, and H3 content."
    },
    "Page has identical alt tags": {
        "desc": "Multiple images on the page have the same alt text, which provides no differentiation value.",
        "fix": "Write unique alt text for each image that accurately describes that specific image."
    },
    "H1 = Alt": {
        "desc": "The H1 heading text is the same as an image's alt text, which is redundant.",
        "fix": "Differentiate the H1 text from image alt text to avoid redundancy."
    },
    "More than one h1 on page": {
        "desc": "The page has multiple H1 tags. Best practice is to have a single H1 per page.",
        "fix": "Keep only one H1 tag (the most important heading). Change additional H1s to H2 or H3."
    },
    "Page has internal links to 3xx pages": {
        "desc": "The page contains internal links that redirect (3xx status), adding unnecessary redirect overhead.",
        "fix": "Update internal links to point directly to the final destination URL, eliminating redirect hops."
    },
    "Page has internal links to 4xx pages": {
        "desc": "The page contains internal links to pages that return 4xx errors (broken links).",
        "fix": "Fix or remove broken internal links. Either restore the target page or redirect to a working page."
    },
    "Broken jump link": {
        "desc": "The page has an anchor link (e.g., #section) that points to a non-existent element on the page.",
        "fix": "Fix or remove the broken anchor link. Ensure the target element exists with a matching id attribute."
    },
    "Page has broken links to external websites": {
        "desc": "The page links to external websites that return 4xx or 5xx errors.",
        "fix": "Remove or update broken external links. Consider linking to a working alternative resource."
    },
    "Page has nofollow outgoing internal links": {
        "desc": "Internal links on the page use rel=\"nofollow\", which prevents search engines from following them.",
        "fix": "Remove rel=\"nofollow\" from internal links. Nofollow should only be used for untrusted external links."
    },
    "Has an internal link with no anchor text": {
        "desc": "An internal link on the page has no visible anchor text (empty or image-only link without alt text).",
        "fix": "Add descriptive anchor text to all internal links. If using an image link, add alt text to the image."
    },
    "Page has more than 2 links to internal page with same anchor": {
        "desc": "The page links to the same internal URL more than twice with identical anchor text, which can look spammy.",
        "fix": "Consolidate duplicate links or vary the anchor text for each link to the same page."
    },
    "Page has internal backlinks with same anchor": {
        "desc": "Multiple internal pages link back to this page using the exact same anchor text.",
        "fix": "Vary the anchor text used in internal links pointing to this page to create a natural link profile."
    },
    "Page has less than 10 internal backlinks": {
        "desc": "Fewer than 10 OTHER pages on the site link TO this page (inbound internal links, NOT outbound links from this page). This is different from the 'internal_links' factor which counts outbound links FROM this page.",
        "fix": "Add internal links from related blog posts, collection pages, and other content pages pointing TO this page. Use varied anchor text with the target keyword. Target 10+ inbound links from other pages."
    },
    "Page has more than 100 internal links": {
        "desc": "The page has over 100 internal links, which dilutes link equity and can look spammy to search engines.",
        "fix": "Reduce the number of internal links to under 100. Remove unnecessary links, especially from navigation and footer areas."
    },
    "Page has outbound internal links with one word anchor": {
        "desc": "Internal links on the page use one-word anchor text, which provides little SEO value.",
        "fix": "Expand anchor text to 2-5 word phrases that include relevant keywords."
    },
    "Page has link with URL in onclick": {
        "desc": "A link on the page uses JavaScript onclick instead of a proper href attribute, which search engines may not follow.",
        "fix": "Replace onclick-based links with standard <a href> links. If JavaScript navigation is needed, add a noscript fallback."
    },
    "Page has anchored image with no alt text": {
        "desc": "An image that is wrapped in a link has no alt text, which means neither the image nor the link destination is described.",
        "fix": "Add descriptive alt text to the image that describes both the image and the link destination."
    },
    "Page size is over 1.5 MB": {
        "desc": "The total page size exceeds 1.5 MB, which significantly impacts load time especially on mobile.",
        "fix": "Compress images (use WebP), minify CSS/JS, enable gzip compression, and remove unused assets to reduce page size."
    },
    "Avoid excessive DOM size": {
        "desc": "The page has a large DOM (many elements), which slows down rendering and JavaScript execution.",
        "fix": "Reduce DOM elements by simplifying the page structure, removing unnecessary wrappers, and using CSS instead of nested divs."
    },
    "Avoid excessive DOM width": {
        "desc": "The DOM tree is very wide (many siblings at one level), which can slow rendering.",
        "fix": "Restructure the page to reduce the number of sibling elements at each level. Group related elements in containers."
    },
    "Defer offscreen images": {
        "desc": "Images below the fold are loaded immediately instead of being lazy-loaded, wasting bandwidth and slowing initial render.",
        "fix": "Add loading=\"lazy\" attribute to below-the-fold images to defer their loading until the user scrolls to them."
    },
    "Add dimensions to images": {
        "desc": "Some images do not have width and height attributes, which can cause layout shifts as images load.",
        "fix": "Add width and height attributes to all <img> tags to prevent Cumulative Layout Shift (CLS)."
    },
    "Serve images in next-gen formats": {
        "desc": "Images are served in older formats (JPEG, PNG) instead of modern formats (WebP, AVIF) that offer better compression.",
        "fix": "Convert images to WebP or AVIF format. Use <picture> tags with fallbacks for older browsers."
    },
    "Use video formats for animated content": {
        "desc": "Animated content uses GIF format instead of more efficient video formats (MP4, WebM).",
        "fix": "Replace large GIFs with MP4 or WebM video files, which are much smaller and load faster."
    },
    "Comments in code >1000 symbols": {
        "desc": "The HTML contains large code comments (>1000 characters), adding unnecessary page weight.",
        "fix": "Remove or minify HTML comments, especially in production. Keep only essential comments."
    },
    "Comments in code has more than 1000 symbols": {
        "desc": "The HTML contains large code comments (>1000 characters), adding unnecessary page weight.",
        "fix": "Remove or minify HTML comments, especially in production. Keep only essential comments."
    },
    "Page has HTTP link to www.w3.org": {
        "desc": "The page contains an HTTP (non-HTTPS) link to www.w3.org, which is a minor security concern.",
        "fix": "Change the link to use HTTPS: https://www.w3.org/ instead of http://www.w3.org/"
    },
    "Page contains form with GET method": {
        "desc": "A form on the page uses the GET method, which exposes form data in the URL and can be cached by search engines.",
        "fix": "Change the form method to POST for forms that submit sensitive data. GET is fine for search forms."
    },
    "URL contains upper case characters": {
        "desc": "The URL contains uppercase letters, which can cause duplicate content issues (search engines treat URLs as case-sensitive).",
        "fix": "Use lowercase URLs exclusively. Set up 301 redirects from uppercase to lowercase versions."
    },
    "Long URLs": {
        "desc": "The URL is very long (>100 characters), which can be truncated in search results and is harder to share.",
        "fix": "Shorten the URL by removing unnecessary words, parameters, or subdirectories. Keep it descriptive but concise."
    },
    "More than three parameters in URL": {
        "desc": "The URL has more than 3 query parameters, which can create crawl inefficiency and duplicate content.",
        "fix": "Reduce URL parameters. Use URL rewriting to create clean URLs (e.g., /category/product instead of ?cat=1&prod=2&...)."
    },
    "Whitespace in URL": {
        "desc": "The URL contains whitespace characters, which can cause encoding issues and broken links.",
        "fix": "Remove spaces from URLs. Use hyphens to separate words: /best-water-softener instead of /best water softener."
    },
    "No Google Tag Manager code": {
        "desc": "Google Tag Manager is not installed on the page, limiting your ability to manage analytics and tracking tags.",
        "fix": "Install Google Tag Manager. Add the GTM container snippet in the <head> and right after the opening <body> tag."
    },
    "Multiple Google Tag Manager codes": {
        "desc": "Multiple GTM container codes are installed, which can cause duplicate tracking and data issues.",
        "fix": "Remove duplicate GTM containers. Keep only one GTM container per page."
    },
    "Paginated parameters in URL": {
        "desc": "The URL uses pagination parameters (e.g., ?page=2), which can create crawl issues if not handled properly.",
        "fix": "Use rel=\"prev\" and rel=\"next\" tags for pagination, or use a canonical tag to consolidate paginated pages."
    },
    "Internal URL redirect broken": {
        "desc": "An internal redirect points to a URL that no longer exists, creating a broken redirect chain.",
        "fix": "Fix the redirect to point to a valid URL, or remove the redirect entirely if the source URL is no longer needed."
    },
    "Twitter card incomplete": {
        "desc": "The Twitter Card meta tags are incomplete, which may result in poor link previews on Twitter/X.",
        "fix": "Complete the Twitter Card tags: add twitter:card, twitter:title, twitter:description, and twitter:image."
    },
    "Twitter card missing": {
        "desc": "The page has no Twitter Card meta tags, so links will not generate rich previews on Twitter/X.",
        "fix": "Add Twitter Card meta tags: twitter:card, twitter:title, twitter:description, and twitter:image."
    },
    "Open Graph tags incomplete": {
        "desc": "Open Graph tags are incomplete, resulting in poor link previews on Facebook, LinkedIn, and other platforms.",
        "fix": "Complete the Open Graph tags: og:title, og:description, og:image, og:url, and og:type."
    },
    "Open Graph tags missing": {
        "desc": "The page has no Open Graph tags, so links will not generate rich previews on social media platforms.",
        "fix": "Add Open Graph meta tags: og:title, og:description, og:image, og:url, and og:type."
    },
    "Headings hierarchy is broken": {
        "desc": "The heading structure skips levels (e.g., H1 → H3 without H2), which confuses search engines about content hierarchy.",
        "fix": "Fix heading order: H1 → H2 → H3 → H4. Never skip heading levels. Use CSS for styling, not heading tags."
    },
    "Page has identical HTML id attributes": {
        "desc": "Multiple elements on the page share the same id attribute, which is invalid HTML and breaks anchor links.",
        "fix": "Make all id attributes unique on the page. Use classes for shared styling instead of duplicate ids."
    },
    "Table has no caption": {
        "desc": "A table on the page does not have a <caption> element, which helps screen readers and SEO understand table content.",
        "fix": "Add a <caption> element to each table describing its contents."
    },
    "Page has tags with style attributes": {
        "desc": "Elements on the page use inline style attributes instead of external CSS, which increases page size and reduces maintainability.",
        "fix": "Move inline styles to external CSS classes. This reduces HTML size and improves caching."
    },
    "Page has 0 impressions": {
        "desc": "This page has zero impressions in Google Search Console over the analyzed period, meaning it is not ranking for any queries.",
        "fix": "Improve on-page SEO, build internal links to this page, submit the URL in Google Search Console, and ensure it is crawlable."
    },
    "Page has at least 1 click": {
        "desc": "This page has received at least 1 click from search results, indicating some search visibility.",
        "fix": "This is a positive signal. Continue optimizing the page to increase impressions and clicks."
    },
    "No Google Analytics code": {
        "desc": "Google Analytics is not installed on the page, so you cannot track user behavior and traffic.",
        "fix": "Install Google Analytics (GA4) or Google Tag Manager with GA4 tag."
    },
    "Page is not mobile friendly": {
        "desc": "The page does not meet Google's mobile-friendly criteria, which can hurt mobile rankings.",
        "fix": "Add a responsive viewport meta tag, use responsive CSS, ensure tap targets are at least 48px, and avoid horizontal scrolling."
    },
    "No XML sitemap found": {
        "desc": "No XML sitemap was found, which helps search engines discover and crawl your pages.",
        "fix": "Create an XML sitemap (e.g., at /sitemap.xml) and submit it in Google Search Console."
    },
    "Robots.txt is missing": {
        "desc": "No robots.txt file was found, which guides search engine crawlers on what to crawl.",
        "fix": "Create a robots.txt file in the root directory specifying which pages search engines should and should not crawl."
    },
}

def get_issue_fix(issue_name):
    """Get description and fix recommendation for a technical SEO issue.
    Uses exact match first, then partial match on key terms."""
    # Exact match
    entry = ISSUE_FIX_MAP.get(issue_name, {})
    if entry:
        return entry.get("desc", ""), entry.get("fix", "")
    
    # Try stripping prefixes like "Keyword not in title tag: " 
    if ":" in issue_name:
        parts = issue_name.split(":", 1)
        # Try the part after the colon
        entry = ISSUE_FIX_MAP.get(parts[1].strip(), {})
        if entry:
            return entry.get("desc", ""), entry.get("fix", "")
        # Try the part before the colon
        entry = ISSUE_FIX_MAP.get(parts[0].strip(), {})
        if entry:
            return entry.get("desc", ""), entry.get("fix", "")
    
    # Partial match on keywords
    issue_lower = issue_name.lower()
    fix_map_lower = {k.lower(): v for k, v in ISSUE_FIX_MAP.items()}
    for key, val in fix_map_lower.items():
        if key in issue_lower or issue_lower in key:
            return val.get("desc", ""), val.get("fix", "")
    
    # Keyword-specific fixes for content auditor items
    if "keyword not in title" in issue_lower or "add target keyword to title" in issue_lower or "keyword missing from" in issue_lower:
        return ("The page title tag does not contain the exact target keyword. The H1 has the keyword but the <title> tag may use a Shopify theme template that doesn't include it. Note: the title tag may render differently in the browser vs what crawlers see.",
                "Update the Shopify theme's title template for this page to include the target keyword. In Shopify admin: Edit the page's meta title, or update the theme's title.liquid template. Example: 'Best Water Softener for City Water | SoftPro'")
    if "h1 to h3 skip" in issue_lower or "h3 to h5 skip" in issue_lower or "heading hierarchy" in issue_lower:
        return ("The heading structure skips levels (e.g., H1 → H3 without H2), which confuses search engines about content hierarchy.",
                "Fix heading order: H1 → H2 → H3 → H4. Never skip heading levels. Add intermediate H2 or H3 headings as needed.")
    if "no citations" in issue_lower:
        return ("The page does not cite any external sources to support its claims. Search engines favor pages with authoritative citations.",
                "Add blockquotes, citations, or links to authoritative sources (.gov, .edu, .org) to support factual claims. Example: 'According to the EPA...'")
    if "no author" in issue_lower or "author attribution" in issue_lower:
        return ("The page does not have an author byline or bio, which hurts E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness).",
                "Add an author name and short bio at the top or bottom of the page. Link to the author's profile page. Example: 'Written by [Name], Water Treatment Specialist with 15+ years experience'")
    if "no schema" in issue_lower or "add schema" in issue_lower or "missing schema" in issue_lower:
        return ("The page is missing structured data (schema markup) that helps search engines understand the content.",
                "Add JSON-LD structured data for the page type. For articles: Article schema. For products: Product + Offer schema. For FAQs: FAQPage schema.")
    if "short paragraph" in issue_lower or "merge short" in issue_lower:
        return ("The page has many very short paragraphs, which can indicate thin content or poor structure.",
                "Merge short paragraphs into 3-7 sentence blocks with clear topic sentences. Remove one-sentence paragraphs.")
    if "word count" in issue_lower or "trimming content" in issue_lower:
        return ("The word count is outside the optimal range for this keyword. Too much content can dilute focus; too little can seem thin.",
                "Adjust content length to match the top-ranking pages (typically 900-2,500 words). Trim redundant sections or add depth where thin.")
    if "keyword not in first paragraph" in issue_lower:
        return ("The target keyword does not appear in the first paragraph, which is a missed relevance signal.",
                "Add the target keyword to the first 100 words of the page. Example: 'When looking for the best water softener for city water...'")
    if "meta description length" in issue_lower:
        return ("The meta description length is outside the optimal range (120-160 characters).",
                "Adjust the meta description to 120-160 characters. Include the target keyword near the beginning.")
    if "lighthouse performance" in issue_lower or "performance score" in issue_lower:
        return ("The Lighthouse performance score is low, indicating slow page loading that hurts rankings and user experience.",
                "Optimize: compress images to WebP, defer offscreen images, minify CSS/JS, reduce DOM size, enable lazy loading, use a CDN.")
    if "relevance score" in issue_lower:
        return ("The content relevance score is low, meaning the page content doesn't closely match the target keyword.",
                "Add the target keyword to: title, H1, first paragraph, meta description. Increase keyword density to 0.5-1.5%. Add LSI keywords.")
    if "signal score" in issue_lower:
        return ("The authority signal score is low, indicating weak E-E-A-T signals.",
                "Add author bio, citations, outbound links to authority sites, schema markup, and original data/research to boost E-E-A-T.")
    if "lsi" in issue_lower or "term coverage" in issue_lower:
        return ("LSI (Latent Semantic Indexing) term coverage is low, meaning the page doesn't use enough related/semantic keywords.",
                "Add related keywords and entities found in top-ranking pages. Use InfraNodus entities to guide content expansion.")
    if "faq" in issue_lower and "add" in issue_lower:
        return ("The page does not have a FAQ section, which can win featured snippets and passage rankings.",
                "Add a FAQ section with 5-10 questions and answers related to the target keyword. Add FAQPage JSON-LD schema.")
    if "hreflang" in issue_lower:
        return ("The page does not have hreflang tags, which help search engines serve the correct language version to users.",
                "Add hreflang tags in the <head> for each language/region version of the page.")
    if "og:" in issue_lower or "open graph" in issue_lower:
        return ("Open Graph tags are missing, resulting in poor social media link previews.",
                "Add og:title, og:description, og:image, og:url, and og:type meta tags in the <head>.")
    if "twitter" in issue_lower and "card" in issue_lower:
        return ("Twitter Card meta tags are missing or incomplete, resulting in poor link previews on Twitter/X.",
                "Add twitter:card, twitter:title, twitter:description, and twitter:image meta tags.")
    
    return ("Issue detected during audit.", "Review the issue and apply standard SEO best practices to fix it.")


# ============================================================================
# HTML REPORT GENERATION
# ============================================================================
def generate_html_report(url, keyword, srs, content_result, technical_issues, factors,
                         serp, ai_overview, lighthouse, google_index, infranodus, fix_lists, recommendations, ig_result=None, deep_scan=None, gsc_data=None, perplexity_data=None, citation_readiness=None):
    """Generate comprehensive HTML report."""

    def score_color(score):
        if score >= 70: return "#27ae60"
        if score >= 40: return "#0066cc"
        return "#e74c3c"

    def score_class(score):
        if score >= 70: return "green-bg"
        if score >= 40: return "orange-bg"
        return "red-bg"

    srs_color = score_color(srs["composite"])
    struct_color = score_color(srs["structure"])
    rel_color = score_color(srs["relevance"])
    sig_color = score_color(srs["signal"])

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta name="robots" content="noindex, nofollow">
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zenfusion Page Auditor Report — {keyword} — {url[:40]}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #ffffff; color: #1a1a1a; padding: 40px 20px; max-width: 1000px; margin: 0 auto; font-size: 16px; }}
h1 {{ font-size: 28px; margin-bottom: 4px; }}
.url-headline {{ font-size: 20px; color: #0066cc; font-weight: 600; margin-bottom: 10px; word-break: break-all; }}
.keyword-tag {{ display: inline-block; background: #e7f3ff; border: 1px solid #0066cc; color: #0066cc; padding: 6px 16px; border-radius: 12px; font-size: 20px; font-weight: 700; margin-bottom: 20px; }}
.subtitle {{ color: #444; font-size: 16px; margin-bottom: 30px; }}
.section {{ margin-bottom: 30px; }}
.section-title {{ color: #0066cc; font-size: 20px; margin-bottom: 14px; border-bottom: 1px solid #e0e0e0; padding-bottom: 8px; cursor: pointer; }}
.expandable {{ cursor: pointer; }}
.expandable::after {{ content: " ▼"; font-size: 14px; color: #0066cc; }}
.expandable.expanded::after {{ content: " ▲"; }}
.detail {{ display: none; }}
.detail.show {{ display: block; }}
.detail p {{ font-size: 16px; line-height: 1.6; margin-bottom: 10px; }}
.score-gauge {{ text-align: center; margin-bottom: 30px; padding: 30px; background: #f8f9fa; border-radius: 12px; border: 1px solid #e0e0e0; }}
.score-value {{ font-size: 64px; font-weight: 700; color: {srs_color}; }}
.score-label {{ font-size: 16px; color: #444; margin-top: 4px; }}
.srs-cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 30px; }}
.srs-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; text-align: center; }}
.srs-card .card-title {{ font-size: 16px; color: #333; margin-bottom: 8px; }}
.srs-card .card-score {{ font-size: 36px; font-weight: 700; }}
.score-breakdown {{ display: grid; gap: 10px; }}
.score-bar {{ display: flex; align-items: center; gap: 10px; }}
.score-bar .name {{ width: 200px; font-size: 18px; color: #1a1a1a; }}
.score-bar .bar {{ flex: 1; height: 24px; background: #f8f9fa; border-radius: 12px; overflow: hidden; border: 1px solid #e0e0e0; }}
.score-bar .fill {{ height: 100%; border-radius: 12px; transition: width 0.5s; }}
.score-bar .num {{ width: 40px; text-align: right; font-size: 16px; font-weight: 600; color: #1a1a1a; }}
.green-bg {{ background: #27ae60; }}
.orange-bg {{ background: #f39c12; }}
.red-bg {{ background: #e74c3c; }}
table {{ width: 100%; border-collapse: collapse; font-size: 16px; }}
th {{ background: #0066cc; color: #ffffff; padding: 10px; text-align: left; font-size: 15px; }}
td {{ padding: 10px; border-bottom: 1px solid #e0e0e0; color: #1a1a1a; font-size: 15px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.wrap {{ white-space: normal; max-width: 400px; }}
tr:hover {{ background: #f8f9fa; }}
.in-range {{ color: #27ae60; font-weight: 600; }}
.out-of-range {{ color: #e74c3c; font-weight: 600; }}
.issue-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; }}
.issue-card .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }}
.dot-red {{ background: #e74c3c; }}
.dot-yellow {{ background: #f39c12; }}
.dot-green {{ background: #27ae60; }}
.issue-card .name {{ font-weight: 600; color: #1a1a1a; font-size: 16px; }}
.issue-card .fix {{ font-size: 15px; color: #444; margin-top: 4px; }}
.topic-badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 15px; margin: 2px; }}
.topic-on-track {{ background: #e8f5e9; color: #1b7a3d; }}
.topic-to-add {{ background: #fff3e0; color: #b35400; }}
.topic-over-used {{ background: #fdecea; color: #c0392b; }}
.factor-group {{ margin-bottom: 12px; }}
.factor-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-size: 16px; }}
.factor-name {{ color: #333; }}
.factor-value {{ color: #0066cc; font-weight: 700; }}
.cat-group {{ margin-bottom: 16px; }}
.cat-group-title {{ font-size: 18px; font-weight: 600; color: #0066cc; margin-bottom: 8px; padding-bottom: 4px; border-bottom: 1px solid #e0e0e0; }}
.recommendation {{ background: #f0f7ff; border-left: 3px solid #0066cc; padding: 14px 18px; margin: 10px 0; border-radius: 6px; font-size: 16px; color: #1a1a1a; line-height: 1.6; }}
.recommendation strong {{ color: #0066cc; }}
a {{ color: #0066cc; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.fix-item {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px; padding: 10px 14px; margin-bottom: 6px; font-size: 16px; color: #1a1a1a; }}
.fix-p0 {{ border-left: 3px solid #e74c3c; }}
.fix-p1 {{ border-left: 3px solid #f39c12; }}
.fix-p2 {{ border-left: 3px solid #27ae60; }}
.fix-col-title {{ font-size: 20px; font-weight: 600; color: #0066cc; margin-bottom: 10px; }}
.fix-priority {{ font-size: 16px; font-weight: 600; margin: 12px 0 6px; color: #444; }}
.lighthouse-scores {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
.lh-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; text-align: center; }}
.lh-card .lh-title {{ font-size: 15px; color: #333; margin-bottom: 6px; }}
.lh-card .lh-score {{ font-size: 28px; font-weight: 700; }}
.cwv-metric {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #e0e0e0; font-size: 16px; }}
.ai-status {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin-bottom: 12px; }}
.footer {{ text-align: center; margin-top: 40px; color: #666; font-size: 15px; border-top: 1px solid #e0e0e0; padding-top: 20px; }}
.grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
.entity-badge {{ display: inline-block; background: #e7f3ff; border: 1px solid #0066cc; color: #0066cc; padding: 4px 12px; border-radius: 12px; font-size: 15px; margin: 2px; }}
.cluster-badge {{ display: inline-block; background: #e8f5e9; border: 1px solid #27ae60; color: #1b7a3d; padding: 4px 12px; border-radius: 12px; font-size: 15px; margin: 2px; }}
.gap-badge {{ display: inline-block; background: #fdecea; border: 1px solid #e74c3c; color: #c0392b; padding: 4px 12px; border-radius: 12px; font-size: 15px; margin: 2px; }}
.char-count {{ color: #0066cc; font-weight: 600; font-size: 14px; }}
</style>
</head>
<body>

<h1>Zenfusion Page Auditor Report</h1>
<div class="url-headline"><a href="{url}" target="_blank">{url}</a></div>
<div class="keyword-tag">Keyword: {keyword}</div>

<!-- 1. SRS Score Gauge + 3 SRS Cards -->
<div class="section">
  <div class="score-gauge">
    <div class="score-value">{srs["composite"]}</div>
    <div class="score-label">SRS Composite Score (0-100)</div>
  </div>
  <div class="srs-cards">
    <div class="srs-card">
      <div class="card-title">Structure</div>
      <div class="card-score" style="color: {struct_color}">{srs["structure"]}</div>
    </div>
    <div class="srs-card">
      <div class="card-title">Relevance</div>
      <div class="card-score" style="color: {rel_color}">{srs["relevance"]}</div>
    </div>
    <div class="srs-card">
      <div class="card-title">Signal</div>
      <div class="card-score" style="color: {sig_color}">{srs["signal"]}</div>
    </div>
  </div>
</div>
'''

    # 2. Content Quality (6 dimension score bars)
    if content_result:
        scores = content_result["scores"]
        html += '''
<div class="section">
  <div class="section-title expandable"><strong>Content Quality (6 Dimensions)</strong></div>
  <div class="detail show">
    <div class="score-breakdown">
'''
        for dim_name, dim_score in scores.items():
            dim_label = dim_name.replace("_", " ").title()
            color_class = score_class(dim_score)
            html += f'      <div class="score-bar"><div class="name" style="font-weight:700;font-size:18px;color:#1a1a1a">{dim_label}</div><div class="bar"><div class="fill {color_class}" style="width: {dim_score}%"></div></div><div class="num" style="font-weight:700">{dim_score}</div></div>\n'
        
        # Show issues found by content auditor
        issues_found = content_result.get("needs_attention", [])
        if issues_found:
            html += f'    <div style="margin-top:16px"><div style="font-size:18px;font-weight:700;color:#1a1a1a;margin-bottom:10px">Issues Found ({len(issues_found)})</div>\n'
            for item in issues_found:
                issue_text = item.get("issue", "")
                fix_text = item.get("fix", "")
                severity = item.get("severity", "yellow")
                sev_color = "#e74c3c" if severity == "red" else "#f39c12"
                html += f'      <div class="issue-card expandable" style="cursor:pointer"><span class="dot" style="background:{sev_color}"></span><span class="name">{issue_text}</span> <span style="font-size:14px;color:#999">▼</span>\n        <div class="detail" style="margin-top:8px;padding:10px;background:#ffffff;border:1px solid #e0e0e0;border-radius:6px">\n          <div style="font-size:15px;color:#333;margin-bottom:6px"><strong>Issue:</strong> {issue_text}</div>\n          <div style="font-size:15px;color:#0066cc;margin-bottom:6px"><strong>How to fix:</strong> {fix_text}</div>\n        </div>\n      </div>\n'
            html += '    </div>\n'
        
        html += f'''    </div>
    <p style="font-size: 15px; color: #1a1a1a; margin-top: 12px;">Overall Content Score: <strong style="color: {score_color(content_result["overall_score"])}">{content_result["overall_score"]}/100</strong> · Tone: {content_result["tone"]["tone"]} ({content_result["tone"]["sentiment"]})</p>
  </div>
</div>
'''

    # 3. Technical SEO Issues (13 categories)
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>Technical SEO Issues (13 Categories)</strong></div>
  <div class="detail show">
'''
    # Group issues by category
    cat_issues = defaultdict(list)
    for severity in ["Critical", "Warning", "Opportunity", "Notice"]:
        for issue in technical_issues.get(severity, []):
            cat, sev = site_auditor.ISSUE_MAP.get(issue, ("Other", severity))
            cat_issues[cat].append((sev, issue))

    # Category order
    cat_order = ["Indexability", "Content relevance", "Duplicate content", "Links", "Page speed",
                 "Security", "Internal", "Redirects", "Social media", "Code validation",
                 "Search traffic", "Mobile friendly", "XML sitemaps", "Other"]

    for cat in cat_order:
        if cat not in cat_issues:
            continue
        issues = cat_issues[cat]
        html += f'    <div class="cat-group">\n      <div class="cat-group-title expandable">{cat} ({len(issues)} issues)</div>\n      <div class="detail show">\n'
        for sev, issue in issues:
            dot = "dot-red" if sev == "Critical" else ("dot-yellow" if sev == "Warning" else "dot-green")
            issue_desc, issue_fix = get_issue_fix(issue)
            html += f'      <div class="issue-card expandable" style="cursor:pointer"><span class="dot {dot}"></span><span class="name">{issue}</span> <span style="font-size:14px;color:#999">▼</span>\n        <div class="detail" style="margin-top:8px;padding:10px;background:#ffffff;border:1px solid #e0e0e0;border-radius:6px">\n          <div style="font-size:15px;color:#333;margin-bottom:6px"><strong>Severity:</strong> <span style="color:{dot};font-weight:600">{sev}</span></div>\n          <div style="font-size:15px;color:#333;margin-bottom:6px"><strong>Issue:</strong> {issue_desc}</div>\n          <div style="font-size:15px;color:#0066cc;margin-bottom:6px"><strong>How to fix:</strong> {issue_fix}</div>\n        </div>\n      </div>\n'
        html += '    </div>\n    </div>\n'

    # Summary counts
    total_c = len(technical_issues.get("Critical", []))
    total_w = len(technical_issues.get("Warning", []))
    total_o = len(technical_issues.get("Opportunity", []))
    total_n = len(technical_issues.get("Notice", []))
    html += f'''    <p style="font-size: 15px; color: #1a1a1a; margin-top: 10px;">Summary: {total_c} Critical · {total_w} Warnings · {total_o} Opportunities · {total_n} Notices</p>
  </div>
</div>
'''

    # 4. Ranking Factors (173+)
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>Ranking Factors (173+ Factors)</strong></div>
  <div class="detail">
'''
    for group_name, factor_names in FACTOR_GROUPS.items():
        group_factors = [(f, factors.get(f, "N/A")) for f in factor_names if f in factors]
        if not group_factors:
            continue
        html += f'    <div class="cat-group">\n      <div class="cat-group-title expandable">{group_name} ({len(group_factors)} factors)</div>\n      <div class="detail">\n'
        for fname, fval in group_factors:
            if isinstance(fval, float):
                fval = round(fval, 2)
            html += f'      <div class="factor-row"><span class="factor-name">{fname}</span><span class="factor-value">{fval}</span></div>\n'
        html += '      </div>\n    </div>\n'
    html += f'''    <p style="font-size: 15px; color: #1a1a1a; margin-top: 10px;">Total factors extracted: {len(factors)}</p>
  </div>
</div>
'''

    # 5. SERP Competitors + AI Overview
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>SERP Competitors & AI Overview</strong></div>
  <div class="detail show">
'''
    if serp:
        # Google-style SERP preview — looks like a real search results page
        # Show top 10 results (page 1 of Google)
        target_domain = url.split('/')[2].replace('www.', '') if '://' in url else ''
        
        serp_html = '''
    <style>
      .serp-result { padding: 12px 0; border-bottom: 1px solid #e0e0e0; }
      .serp-result:last-child { border-bottom: none; }
      .serp-favicon { display: inline-block; width: 22px; height: 22px; vertical-align: middle; margin-right: 6px; border-radius: 4px; background: #f0f0f0; text-align: center; font-size: 12px; line-height: 22px; color: #666; }
      .serp-url-line { font-size: 13px; color: #5f6368; margin-bottom: 2px; }
      .serp-url-line a { color: #5f6368; text-decoration: none; }
      .serp-url-line a:hover { text-decoration: underline; }
      .serp-title { font-size: 18px; margin: 0 0 2px 0; }
      .serp-title a { color: #1a0dab; text-decoration: none; }
      .serp-title a:hover { text-decoration: underline; }
      .serp-desc { font-size: 14px; color: #4d5156; line-height: 1.4; }
      .serp-badge { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }
      .serp-badge-ours { background: #27ae60; color: white; font-weight: 600; }
      .serp-badge-ai { background: #0066cc; color: white; font-weight: 600; }
      .serp-badge-cited { background: #e67e22; color: white; }
      .serp-ai-box { background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }
      .serp-ai-box .serp-ai-label { font-size: 14px; color: #0066cc; font-weight: 600; margin-bottom: 8px; }
      .serp-ai-content { font-size: 14px; color: #1a1a1a; line-height: 1.6; max-height: 300px; overflow-y: auto; white-space: pre-wrap; }
      .serp-paa { background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }
      .serp-paa-q { font-size: 15px; color: #1a0dab; font-weight: 500; cursor: pointer; }
      .serp-search-bar { background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 24px; padding: 10px 20px; margin-bottom: 16px; display: flex; align-items: center; }
      .serp-search-bar .serp-logo { font-size: 20px; margin-right: 12px; }
      .serp-search-bar .serp-query { font-size: 16px; color: #1a1a1a; flex: 1; }
      .serp-meta { font-size: 13px; color: #70757a; margin-bottom: 12px; }
    </style>
    <div class="serp-search-bar">
      <span class="serp-logo">🔍</span>
      <span class="serp-query">''' + keyword + '''</span>
    </div>
    <div class="serp-meta">Page 1 of Google · ''' + str(len(serp)) + ''' results total · DataForSEO live SERP</div>
'''
        
        # AI Overview box (if present)
        if ai_overview and ai_overview.get("has_ai_overview"):
            ai_text = ai_overview.get("markdown", "") or ai_overview.get("text", "")
            ai_citations = ai_overview.get("cited_domains", [])
            serp_html += '<div class="serp-ai-box">\n'
            serp_html += '  <div class="serp-ai-label">⚡ AI Overview <span class="serp-badge serp-badge-ai">AI</span></div>\n'
            if ai_text:
                serp_html += f'  <div class="serp-ai-content">{ai_text[:2000]}{"..." if len(ai_text) > 2000 else ""}</div>\n'
            if ai_citations:
                serp_html += f'  <div style="margin-top:8px;font-size:13px;color:#5f6368">Cited sources: {", ".join(ai_citations[:10])}</div>\n'
            serp_html += '</div>\n'
        
        # Organic results — Google-style
        for r in serp[:10]:
            r_domain = r.get("domain", "")
            r_url = r.get("url", "")
            r_title = r.get("title", "")
            r_desc = r.get("description", "")
            r_rank = r.get("rank", 0)
            r_cited = r.get("is_highly_cited", False)
            
            is_ours = target_domain and target_domain in r_domain
            
            # Favicon (first letter of domain)
            favicon_letter = r_domain[0].upper() if r_domain else "?"
            
            # Build URL display line (like Google: domain > path)
            display_url = r_domain
            if r_url and '://' in r_url:
                path_part = r_url.split(r_domain)[-1] if r_domain in r_url else ''
                if path_part and path_part != '/':
                    display_url = f'{r_domain} › {path_part.strip("/").split("/")[0]}' if '/' in path_part else r_domain
            
            # Badges
            badges = ''
            if is_ours:
                badges += '<span class="serp-badge serp-badge-ours">YOUR PAGE</span>'
            if r_cited:
                badges += '<span class="serp-badge serp-badge-cited">⚡ Highly Cited</span>'
            
            # Highlight our domain in the title
            title_display = r_title
            if is_ours:
                title_display = f'<strong>{r_title}</strong>'
            
            serp_html += f'<div class="serp-result">\n'
            serp_html += f'  <div class="serp-url-line"><span class="serp-favicon">{favicon_letter}</span><a href="{r_url}" target="_blank">{display_url}</a>{badges}</div>\n'
            serp_html += f'  <div class="serp-title"><a href="{r_url}" target="_blank">{title_display}</a></div>\n'
            serp_html += f'  <div class="serp-desc">{r_desc[:200]}{"..." if len(r_desc) > 200 else ""}</div>\n'
            serp_html += '</div>\n'
        
        html += serp_html
    else:
        html += '    <p style="color: #0066cc;">No SERP data available.</p>\n'

    # AI Overview status
    if ai_overview and ai_overview.get("has_ai_overview"):
        html += f'''    <div class="ai-status" style="margin-top: 16px;">
      <strong style="color: #27ae60;">AI Overview Found</strong><br>
      <span style="font-size: 15px; color: #1a1a1a;">
        Citations: {ai_overview.get("citation_count", 0)} · Source Diversity: {ai_overview.get("source_diversity", 0)} · Products: {len(ai_overview.get("products_mentioned", []))}
      </span><br>
      <span style="font-size: 14px; color: #1a1a1a; margin-top: 4px; display: block;">
        Cited domains: {", ".join(ai_overview.get("cited_domains", [])[:10]) or "None"}
      </span>
'''
        if ai_overview.get("paa"):
            html += f'      <div style="margin-top: 8px;"><strong style="font-size: 14px; color: #0066cc;">People Also Ask:</strong><br>'
            for q in ai_overview["paa"][:5]:
                html += f'        <span style="font-size: 14px; color: #1a1a1a;">• {q}</span><br>\n'
            html += '      </div>\n'
        if ai_overview.get("special"):
            html += '      <div style="margin-top: 8px;"><strong style="font-size: 14px; color: #0066cc;">Special Results:</strong><br>'
            for s in ai_overview["special"][:5]:
                html += f'        <span style="font-size: 14px; color: #1a1a1a;">• {s["type"].title()}: {s["title"][:50]}</span><br>\n'
            html += '      </div>\n'
        html += '    </div>\n'
    else:
        html += '    <div class="ai-status"><strong style="color: #0066cc;">No AI Overview detected</strong> for this keyword.</div>\n'

    html += '''  </div>
</div>
'''

    # 6. Core Web Vitals (Lighthouse)
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>Core Web Vitals (Lighthouse)</strong></div>
  <div class="detail show">
'''
    if lighthouse and lighthouse.get("scores"):
        html += '    <div class="lighthouse-scores">\n'
        for cat_id, cat_label in [("performance", "Performance"), ("accessibility", "Accessibility"),
                                   ("best-practices", "Best Practices"), ("seo", "SEO")]:
            score = lighthouse["scores"].get(cat_id, 0)
            color = score_color(score)
            html += f'      <div class="lh-card"><div class="lh-title">{cat_label}</div><div class="lh-score" style="color: {color}">{score}</div></div>\n'
        html += '    </div>\n'

        # Core Web Vitals metrics
        html += '    <div style="margin-top: 12px;">\n'
        for metric_id, metric_label in [("fcp", "First Contentful Paint"), ("lcp", "Largest Contentful Paint"),
                                         ("cls", "Cumulative Layout Shift"), ("tbt", "Total Blocking Time"),
                                         ("speed_index", "Speed Index"), ("tti", "Time to Interactive")]:
            m = lighthouse.get(metric_id, {})
            if m and m.get("displayValue"):
                score = m.get("score", 0)
                color = "#27ae60" if score and score >= 0.9 else ("#f39c12" if score and score >= 0.5 else "#e74c3c")
                html += f'      <div class="cwv-metric"><span>{metric_label}</span><span style="color: {color}; font-weight: 600;">{m["displayValue"]}</span></div>\n'
        html += '    </div>\n'
    else:
        html += '    <p style="color: #0066cc;">Lighthouse data not available.</p>\n'

    html += '''  </div>
</div>
'''

    # 7. Structure Metrics
    if content_result:
        html += '''
<div class="section">
  <div class="section-title expandable"><strong>Structure Metrics (with Target Ranges)</strong></div>
  <div class="detail show">
    <table>
      <tr><th>Metric</th><th>Value</th><th>Target</th><th>Status</th></tr>
'''
        for m in content_result["structure_metrics"]:
            if m["in_range"] is True:
                status = '<span class="in-range">✓ In range</span>'
            elif m["in_range"] is False:
                status = '<span class="out-of-range">✗ Out of range</span>'
            else:
                status = '—'
            html += f'      <tr><td>{m["name"]}</td><td>{m["value"]}</td><td>{m["target"]}</td><td>{status}</td></tr>\n'
        html += '    </table>\n  </div>\n</div>\n'

    # 8. Schema Analysis (Ranking Factor — structured data is a confirmed Google ranking signal)
    if content_result:
        schema = content_result["schema"]
        schema_score = schema.get("score", 0)
        schema_types = schema.get("types_found", [])
        schema_missing = schema.get("missing", [])
        schema_critical = schema.get("critical_found", [])
        
        # Determine page type for schema recommendations
        page_url_lower = url.lower() if url else ""
        is_product = "/products/" in page_url_lower or "product" in page_url_lower
        is_blog = "/blogs/" in page_url_lower or "/pages/" in page_url_lower or "blog" in page_url_lower
        _domain = url.split("/")[2].replace("www.", "") if "://" in url else ""
        is_home = page_url_lower.rstrip("/").endswith(_domain) if _domain else False
        
        # Schema type requirements by page type
        schema_requirements = {
            "Product": {"required": ["name", "image", "description", "offers", "brand", "aggregateRating"], "priority": "Critical", "impact": "Product schema enables rich results with price, availability, and ratings in SERPs"},
            "Offer": {"required": ["price", "priceCurrency", "availability"], "priority": "Critical", "impact": "Offer schema shows pricing and availability in search results"},
            "AggregateRating": {"required": ["ratingValue", "reviewCount", "bestRating"], "priority": "High", "impact": "Star ratings in SERPs increase CTR by 15-35%"},
            "Review": {"required": ["reviewRating", "author", "datePublished"], "priority": "High", "impact": "Individual reviews add credibility and can appear in rich results"},
            "Article": {"required": ["headline", "author", "datePublished", "image"], "priority": "High", "impact": "Article schema enables Google News inclusion and rich article snippets"},
            "FAQPage": {"required": ["mainEntity"], "priority": "High", "impact": "FAQ rich results expand your SERP footprint and can appear in AI Overviews"},
            "Person": {"required": ["name", "jobTitle", "affiliation"], "priority": "Medium", "impact": "Person schema strengthens E-E-A-T signals and author authority"},
            "Organization": {"required": ["name", "url", "logo", "sameAs"], "priority": "High", "impact": "Organization schema enables knowledge panel and brand entity recognition"},
            "BreadcrumbList": {"required": ["itemListElement"], "priority": "High", "impact": "Breadcrumbs replace URLs in SERPs and improve click-through"},
            "WebSite": {"required": ["name", "url", "potentialAction"], "priority": "Medium", "impact": "WebSite schema enables sitelinks search box in SERPs"},
        }
        
        # Build schema type table
        schema_rows = ""
        for st in sorted(set(schema_types + schema_missing)):
            found = st in schema_types
            req = schema_requirements.get(st, {})
            priority = req.get("priority", "Medium")
            impact = req.get("impact", "Improves search engine understanding of page content")
            required_props = req.get("required", [])
            
            if found:
                status = f'<span style="color:#27ae60;font-weight:600">✓ Present</span>'
                action = "Maintain — schema is properly implemented"
                row_class = "schema-found"
            else:
                status = f'<span style="color:#cc0000;font-weight:600">✗ Missing</span>'
                action = f"Add {st} JSON-LD schema — required properties: {', '.join(required_props)}"
                row_class = "schema-missing"
            
            priority_color = {"Critical": "#cc0000", "High": "#e67e22", "Medium": "#0066cc"}.get(priority, "#666")
            schema_rows += f'      <tr class="{row_class}"><td><strong>{st}</strong></td><td>{status}</td><td style="color:{priority_color};font-weight:600">{priority}</td><td style="font-size:14px">{impact}</td><td style="font-size:14px">{action}</td></tr>\n'
        
        # Build missing schema recommendations
        missing_recs = ""
        if schema_missing:
            missing_recs = '<div style="margin-top:12px;padding:12px;background:#fff3cd;border:1px solid #ffeaa7;border-radius:8px">\n'
            missing_recs += f'  <strong style="font-size:15px;color:#856404">⚠ Missing Schema ({len(schema_missing)} types):</strong>\n  <ul style="margin:8px 0 0 0;font-size:15px;color:#1a1a1a">\n'
            for ms in schema_missing:
                req = schema_requirements.get(ms, {})
                props = ', '.join(req.get("required", ["(check schema.org)"]))
                missing_recs += f'    <li><strong>{ms}</strong> — Add JSON-LD with: {props}</li>\n'
            missing_recs += '  </ul>\n</div>\n'
        
        # Schema ranking factor score
        if schema_score >= 80:
            score_color = "#27ae60"
            score_label = "Excellent"
        elif schema_score >= 60:
            score_color = "#e67e22"
            score_label = "Good"
        elif schema_score >= 40:
            score_color = "#e67e22"
            score_label = "Needs Work"
        else:
            score_color = "#cc0000"
            score_label = "Poor"
        
        # JSON-LD count
        json_ld_count = factors.get("json_ld_count", 0) if factors else 0
        has_microdata = factors.get("has_microdata", False) if factors else False
        has_rdfa = factors.get("has_rdfa", False) if factors else False
        schema_prop_count = factors.get("schema_property_count", 0) if factors else 0
        
        # Schema as ranking factor explanation
        schema_factor_note = '<div style="margin-top:10px;padding:10px;background:#e8f4fd;border:1px solid #b3d9f2;border-radius:8px;font-size:14px;color:#1a1a1a"><strong>Why schema matters:</strong> Google uses structured data to understand page content, enable rich results (star ratings, FAQ accordions, product pricing), and feed AI Overviews. Pages with proper schema see 15-35% higher CTR. Missing Product/Review schema on product pages is a confirmed negative ranking signal.</div>'
        
        html += f'''
<div class="section">
  <div class="section-title expandable"><strong>Schema &amp; Structured Data Analysis (Score: <span style="color:{score_color}">{schema_score}/100 — {score_label}</span>)</strong></div>
  <div class="detail show">
    <p style="font-size: 15px; color: #1a1a1a; margin-bottom: 8px;">JSON-LD blocks: <strong>{json_ld_count}</strong> · Schema types found: <strong>{len(schema_types)}</strong> · Properties: <strong>{schema_prop_count}</strong> · Microdata: {"✓" if has_microdata else "✗"} · RDFa: {"✓" if has_rdfa else "✗"}</p>
    
    <table style="width:100%;border-collapse:collapse;margin-top:10px">
      <thead><tr style="background:#f0f0f0"><th style="padding:8px;text-align:left;font-size:14px">Schema Type</th><th style="padding:8px;text-align:left;font-size:14px">Status</th><th style="padding:8px;text-align:left;font-size:14px">Priority</th><th style="padding:8px;text-align:left;font-size:14px">SEO Impact</th><th style="padding:8px;text-align:left;font-size:14px">Action</th></tr></thead>
      <tbody>
{schema_rows}      </tbody>
    </table>
    
{missing_recs}
    {schema_factor_note}
  </div>
</div>
'''

    # 9. Entity & Topical Analysis (InfraNodus)
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>Entity & Topical Analysis (InfraNodus)</strong></div>
  <div class="detail show">
'''
    if infranodus and infranodus.get("entity_count", 0) > 0:
        html += f'    <p style="font-size: 15px; color: #1a1a1a; margin-bottom: 10px;">Entities: {infranodus["entity_count"]} · Edges: {infranodus.get("edge_count", 0)} · Clusters: {infranodus.get("cluster_count", 0)} · Content Gaps: {infranodus.get("content_gap_count", 0)} · Diversity: {infranodus.get("diversity_score", 0)}</p>\n'

        if infranodus.get("top_entities"):
            html += '    <div style="margin-bottom: 12px;"><strong style="font-size: 15px; color: #0066cc;">Top Entities:</strong> <button onclick="copyEntities()" style="background:#0066cc;color:white;border:none;padding:3px 10px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:8px">📋 Click to Copy All Entities</button><br>\n'
            for e in infranodus["top_entities"]:
                html += f'      <span class="entity-badge">{e}</span>\n'
            html += '    </div>\n'

        if infranodus.get("cluster_names"):
            html += '    <div style="margin-bottom: 12px;"><strong style="font-size: 15px; color: #27ae60;">Clusters:</strong><br>\n'
            for c in infranodus["cluster_names"]:
                if c:
                    html += f'      <span class="cluster-badge">{c}</span>\n'
            html += '    </div>\n'

        if infranodus.get("content_gap_count", 0) > 0:
            html += f'    <div><strong style="font-size: 15px; color: #0066cc;">Content Gaps: {infranodus["content_gap_count"]} gaps detected</strong> — add missing entities to improve topical coverage.</div>\n'
    else:
        err = infranodus.get("error", "") if infranodus else "No data"
        html += f'    <p style="color: #0066cc;">InfraNodus analysis unavailable. {err}</p>\n'

    html += '''  </div>
</div>
'''

    # 10. Topics / Keyword Tracking
    if content_result:
        html += '''
<div class="section">
  <div class="section-title expandable"><strong>Topics / Keyword Tracking</strong></div>
  <div class="detail show">
'''
        topics = content_result["topics"]
        if topics.get("on_track"):
            html += '    <div style="margin-bottom: 8px;"><strong style="font-size: 15px; color: #27ae60;">On Track:</strong><br>\n'
            for t in topics["on_track"]:
                html += f'      <span class="topic-badge topic-on-track">{t}</span>\n'
            html += '    </div>\n'
        if topics.get("to_add"):
            html += '    <div style="margin-bottom: 8px;"><strong style="font-size: 15px; color: #0066cc;">To Add:</strong><br>\n'
            for t in topics["to_add"]:
                html += f'      <span class="topic-badge topic-to-add">+ {t}</span>\n'
            html += '    </div>\n'
        if topics.get("over_used"):
            html += '    <div><strong style="font-size: 15px; color: #e74c3c;">Over-Used:</strong><br>\n'
            for t in topics["over_used"]:
                html += f'      <span class="topic-badge topic-over-used">{t}</span>\n'
            html += '    </div>\n'
        html += '''  </div>
</div>
'''

    # 10b. Information Gain Analysis (Invisible Authority)
    if ig_result and ig_result.get("pages_analyzed", 0) > 0:
        ig_pages = ig_result.get("pages_analyzed", 0)
        ig_claims = ig_result.get("total_claims", 0)
        ig_consensus = ig_result.get("consensus", [])
        ig_unique = ig_result.get("unique", [])
        ig_missing = ig_result.get("missing", [])
        ig_rep = ig_result.get("repetition_rate", 0)
        
        html += f'''
  <!-- Information Gain Analysis -->
  <div class="section">
    <div class="section-title expandable"><strong>Information Gain Analysis (Invisible Authority)</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Analyzes top {ig_pages} ranking pages for "{keyword}" to find <strong>consensus</strong> (what most pages say = your coverage floor) 
        and <strong>unique claims</strong> (what only one page says = your information gain opportunity). 
        Repetition rate: <strong>{ig_rep}%</strong> — lower means more unique content to consolidate.
      </p>
      
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{ig_pages}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Pages Analyzed</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{ig_claims}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Total Claims</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#27ae60">{len(ig_consensus)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Consensus Claims</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color: #0066cc">{len(ig_unique)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Unique Claims</div>
        </div>
      </div>
      
      <h3 style="color:#27ae60;font-size:16px;margin:20px 0 10px">CONSENSUS (Coverage Floor — must cover these)</h3>
      <p style="font-size:15px;color:#444;margin-bottom:10px">Claims shared by 2+ ranking pages. Cover these to be a credible answer.</p>
'''
        for c in ig_consensus[:15]:
            html += f'      <div style="background:#f8f9fa;border-left:3px solid #27ae60;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{c["text"]} <span style="color:#27ae60;font-size:13px">({c["pages"]} pages)</span></div>\n'
        
        html += f'''
      <h3 style="color: #0066cc;font-size:16px;margin:20px 0 10px">UNIQUE CLAIMS (Information Gain Opportunities)</h3>
      <p style="font-size:15px;color:#444;margin-bottom:10px">Claims made by only one page. Consolidate these to carry more info than any single competitor.</p>
'''
        for u in ig_unique[:25]:
            html += f'      <div style="background:#f8f9fa;border-left:3px solid #f39c12;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{u["text"]} <span style="color: #0066cc;font-size:13px">— {u["domain"]} (pos {u["position"]})</span></div>\n'
        
        if ig_missing:
            html += f'''
      <h3 style="color:#cc0000;font-size:16px;margin:20px 0 10px">MISSING (White Space — nobody covers these)</h3>
      <p style="font-size:15px;color:#444;margin-bottom:10px">Topics no ranking page covers. Own this space with original first-hand content.</p>
'''
            for m in ig_missing[:10]:
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #cc0000;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{m}</div>\n'
        
        html += '''
      <h3 style="color:#0066cc;font-size:16px;margin:20px 0 10px">Content Brief</h3>
      <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:16px;font-size:15px;line-height:1.6">
        <p><strong>1. Cover the consensus</strong> — ensure your page addresses all shared claims above. This is your entry ticket.</p>
        <p style="margin-top:8px"><strong>2. Consolidate unique claims</strong> — pull together the scattered unique claims from competitors. Your page will carry more information than any single competitor.</p>
        <p style="margin-top:8px"><strong>3. Fill white space</strong> — add first-hand material on the missing topics. This is where true information gain lives — content that appears nowhere on page 1.</p>
      </div>
    </div>
  </div>

'''
    elif ig_result and ig_result.get("error"):
        html += f'''
  <div class="section">
    <div class="section-title">Information Gain Analysis</div>
    <div class="detail show">
      <p style="font-size:14px;color:#444">{ig_result.get("error", "Analysis unavailable")}</p>
    </div>
  </div>

'''

    # 10c. AI-Generated FAQs (from Information Gain analysis)
    if ig_result and ig_result.get("pages_analyzed", 0) > 0:
        faqs = generate_faqs_from_ig(keyword, ig_result, url)
        
        if faqs:
            html += f'''
  <!-- AI-Generated FAQs -->
  <div class="section">
    <div class="section-title expandable"><strong>AI-Generated FAQs (From Information Gain Analysis)</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        {len(faqs)} FAQs generated to close white space and consolidate unique claims from competitors. 
        Each FAQ is factual, brand-relevant, and designed for FAQ schema markup and AI Overview citation.
        Add these to the page with FAQPage schema for passage optimization.
      </p>
'''
            for i, faq in enumerate(faqs, 1):
                html += f'''      <div class="issue-card" style="border-left:3px solid #0066cc;margin-bottom:12px">
        <span class="name" style="font-size:14px;color:#0066cc;font-weight:600">Q{i}: {faq["q"]}</span>
        <div class="detail show" style="font-size:14px;color:#444;margin-top:10px;padding-top:8px;border-top:1px solid #e0e0e0">
          {faq["a"]}
        </div>
      </div>
'''
            
            # Build proper FAQPage JSON-LD schema (valid JSON) + Copy Schema button
            faq_schema = json.dumps({
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": f["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": f["a"]}
                    }
                    for f in faqs
                ]
            }, indent=2, ensure_ascii=False)
            # Escape for visible <code> display (HTML-escape <, >, &)
            faq_schema_disp = faq_schema.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # Escape for use inside an HTML data-* attribute (browser decodes entities on getAttribute)
            faq_schema_attr = faq_schema.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(chr(34), "&quot;").replace(chr(39), "&#39;")
            html += f'''
      <div style="margin-top:20px;padding:16px;background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px">
        <p style="color:#0066cc;font-size:15px;font-weight:600;margin-bottom:8px">FAQPage Schema (JSON-LD) — paste into page &lt;head&gt;:</p>
        <button onclick="copySchema(this)" data-schema="{faq_schema_attr}" style="background:#0066cc;color:white;border:none;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer;margin-bottom:10px">📋 Copy Schema</button>
        <code style="font-size:13px;color:#1b7a3d;display:block;white-space:pre-wrap;overflow-x:auto;background:#ffffff;padding:10px;border:1px solid #e0e0e0;border-radius:6px">&lt;script type="application/ld+json"&gt;
{faq_schema_disp}
&lt;/script&gt;</code>
      </div>
    </div>
  </div>

'''

    # 10d. AI-Written Examples (Meta Title, Meta Description, H1, H2, H3, ALT)
    html += generate_ai_examples(keyword, url, infranodus, content_result, factors, serp)

    # 10e. Keywords Everywhere Keyword Data (prepaid credits, replaces DataForSEO for volume/CPC/competition)
    if HAS_KE:
        ke_data = get_ke_keyword_data(keyword)
        if ke_data:
            kw_data = ke_data.get(keyword.lower(), {})
            if kw_data:
                ke_vol = kw_data.get("vol", 0)
                ke_cpc = kw_data.get("cpc", 0)
                ke_currency = kw_data.get("currency", "$")
                ke_comp = kw_data.get("competition", 0)
                ke_trend = kw_data.get("trend", [])
                ke_credits = ke_data.get("_credits_remaining", 0)
                
                # Get seasonality insight
                ke_season = ke_helper.get_seasonality_insight(keyword) if ke_trend else {}
                ke_season_score = ke_season.get("seasonality_score", 0)
                ke_peak = f"{ke_season.get('peak_month', '')} ({ke_season.get('peak_volume', 0):,})"
                ke_low = f"{ke_season.get('low_month', '')} ({ke_season.get('low_volume', 0):,})"
                ke_avg = ke_season.get("avg_monthly_volume", 0)
                
                # Build trend mini-chart data
                trend_labels = [m.get("month", "")[:3] for m in ke_trend]
                trend_values = [m.get("value", 0) for m in ke_trend]
                
                # Competition label
                comp_label = "Very High" if ke_comp >= 0.8 else ("High" if ke_comp >= 0.6 else ("Medium" if ke_comp >= 0.3 else "Low"))
                comp_color = "#cc0000" if ke_comp >= 0.8 else ("#e67e22" if ke_comp >= 0.6 else ("#f39c12" if ke_comp >= 0.3 else "#27ae60"))
                
                html += f'''
  <!-- Keywords Everywhere Data -->
  <div class="section">
    <div class="section-title expandable"><strong>Keyword Data (Keywords Everywhere — Prepaid)</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Search volume, CPC, competition, and 12-month trend for "{keyword}". 
        Data from Keywords Everywhere (prepaid credits — saves DataForSEO API costs).
        KE credits remaining: <strong>{ke_credits:,}</strong>
      </p>
      
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px">
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{ke_vol:,}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Monthly Volume</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#27ae60">{ke_currency}{ke_cpc}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">CPC</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:{comp_color}">{comp_label}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Competition ({ke_comp:.2f})</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color: #0066cc">{ke_season_score}%</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Seasonality</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{ke_avg:,}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Avg Monthly</div>
        </div>
      </div>
      
      <h3 style="color:#0066cc;font-size:16px;margin:20px 0 10px">12-Month Search Trend</h3>
'''
                # Simple text-based trend chart
                if trend_values:
                    max_val = max(trend_values) if trend_values else 1
                    html += '      <div style="display:flex;align-items:flex-end;gap:4px;height:80px;margin:10px 0;padding:10px;background:#f8f9fa;border-radius:8px">\n'
                    for i, (label, val) in enumerate(zip(trend_labels, trend_values)):
                        height_pct = round(val / max_val * 100) if max_val > 0 else 0
                        bar_color = "#0066cc" if val >= ke_avg else "#e0e0e0"
                        html += f'        <div style="flex:1;display:flex;flex-direction:column;align-items:center"><div style="font-size:9px;color:#aaa;margin-bottom:2px">{val:,}</div><div style="width:100%;height:{height_pct}%;min-height:2px;background:{bar_color};border-radius:2px"></div><div style="font-size:9px;color:#aaa;margin-top:4px">{label}</div></div>\n'
                    html += '      </div>\n'
                
                html += f'''
      <div style="margin-top:16px">
        <h3 style="color: #0066cc;font-size:14px;margin-bottom:8px">Seasonality Insight</h3>
        <div style="background:#f8f9fa;padding:12px;border-radius:8px;font-size:15px;color:#444">
          <strong>Peak:</strong> {ke_peak} | <strong>Low:</strong> {ke_low} | <strong>Seasonality:</strong> {ke_season_score}% variance
          <br><em style="font-size:14px">Content should be updated/refreshed before peak season. {"High seasonality — plan content updates 1-2 months before peak." if ke_season_score > 30 else "Low seasonality — consistent year-round demand."}</em>
        </div>
      </div>
    </div>
  </div>

'''

    # 10e. Deep SERP Scan (Top 100 — Claims + Problems + PAA)
    if deep_scan and deep_scan.get("stats", {}).get("unique_claims", 0) > 0:
        ds_claims = deep_scan.get("claims", [])
        ds_problems = deep_scan.get("problems", [])
        ds_paa = deep_scan.get("paa_questions", [])
        ds_related = deep_scan.get("related_searches", [])
        ds_stats = deep_scan.get("stats", {})
        
        html += f'''
  <!-- Deep SERP Scan -->
  <div class="section">
    <div class="section-title expandable"><strong>Deep SERP Scan — Top 100 Results (Claims, Problems & PAA)</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Scanned top {ds_stats.get('pages_fetched', 0)} organic results for "{keyword}". Found
        <strong>{ds_stats.get('unique_claims', 0)} unique claims</strong>,
        <strong>{ds_stats.get('unique_problems', 0)} industry problems/issues</strong>,
        <strong>{len(ds_paa)} People Also Ask questions</strong>, and
        <strong>{len(ds_related)} related searches</strong>.
      </p>
      
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{ds_stats.get('unique_claims', 0)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Claims</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#cc0000">{ds_stats.get('unique_problems', 0)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Problems/Issues</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color: #0066cc">{len(ds_paa)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">People Also Ask</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#27ae60">{len(ds_related)}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Related Searches</div>
        </div>
      </div>
      
'''
        # Industry problems section
        if ds_problems:
            html += '      <h3 style="color:#cc0000;font-size:16px;margin:20px 0 10px">INDUSTRY PROBLEMS & ISSUES</h3>\n'
            html += '      <p style="font-size:15px;color:#444;margin-bottom:10px">Common complaints, failures, and concerns from across the top 100 results. Address these to build trust.</p>\n'
            for p in ds_problems[:25]:
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #cc0000;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{p["claim"][:150]} <span style="color:#cc0000;font-size:13px">— {p["domain"]} (pos {p["position"]})</span></div>\n'
        
        # PAA questions
        if ds_paa:
            html += '      <h3 style="color: #0066cc;font-size:16px;margin:20px 0 10px">PEOPLE ALSO ASK</h3>\n'
            html += '      <p style="font-size:15px;color:#444;margin-bottom:10px">Real questions searchers are asking. Answer these on your page for passage ranking.</p>\n'
            for q in ds_paa[:20]:
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #f39c12;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{q}</div>\n'
        
        # Related searches
        if ds_related:
            # Get KE volume data for related searches
            ke_related_data = {}
            if HAS_KE:
                try:
                    ke_related_data = ke_helper.get_batch_volumes(ds_related[:50])
                except:
                    pass
            
            html += '      <h3 style="color:#27ae60;font-size:16px;margin:20px 0 10px">RELATED SEARCHES</h3>\n'
            html += '      <p style="font-size:15px;color:#444;margin-bottom:10px">Adjacent search queries with search volume (Keywords Everywhere data). Target high-volume queries with related content sections.</p>\n'
            for r in ds_related[:20]:
                vol = ke_related_data.get(r.lower(), 0)
                vol_str = f' <span style="color:#27ae60;font-size:13px;font-weight:600">{vol:,}/mo</span>' if vol else ''
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #27ae60;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{r}{vol_str}</div>\n'
        
        # Additional claims (top 20 from deep scan that weren't in IG top-8)
        if ds_claims:
            html += '      <h3 style="color:#0066cc;font-size:16px;margin:20px 0 10px">ADDITIONAL CLAIMS (from positions 11-100)</h3>\n'
            html += '      <p style="font-size:15px;color:#444;margin-bottom:10px">Claims found beyond the top 10 results that may not be covered by any page 1 competitor.</p>\n'
            for c in ds_claims[:25]:
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #0066cc;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{c["claim"][:150]} <span style="color:#0066cc;font-size:13px">— {c["domain"]} (pos {c["position"]})</span></div>\n'
        
        html += '''
    </div>
  </div>

'''

    # 10f. GSC Real Search Data (Google Search Console — free, real impressions/clicks)
    if gsc_data:
        kw = gsc_data.get('keyword_perf')
        pg = gsc_data.get('page_perf')
        
        html += '''
  <!-- GSC Real Search Data -->
  <div class="section">
    <div class="section-title expandable"><strong>Google Search Console — Real Performance Data</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Real Google search data (impressions, clicks, CTR, position) from Search Console. Free API — not estimated.
      </p>
'''
        
        if kw:
            html += f'''      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#0066cc">{kw['impressions']:,}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Impressions (keyword)</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:#27ae60">{kw['clicks']}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Clicks</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color: #0066cc">{kw['ctr']:.2%}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">CTR</div>
        </div>
        <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:{'#27ae60' if kw['position'] < 10 else '#e74c3c'}">{kw['position']:.1f}</div>
          <div style="font-size:13px;color:#444;text-transform:uppercase;margin-top:4px">Avg Position</div>
        </div>
      </div>
'''
        
        if pg:
            html += f'''      <h3 style="color:#0066cc;font-size:16px;margin:20px 0 10px">Page Performance (last 28 days)</h3>
      <div style="background:#f8f9fa;padding:12px;border-radius:8px;font-size:15px;color:#444;margin-bottom:16px">
        <b>Page impressions:</b> {pg['impressions']:,} | <b>Clicks:</b> {pg['clicks']} | <b>CTR:</b> {pg['ctr']:.2%} | <b>Position:</b> {pg['position']:.1f}
      </div>
'''
            if pg.get('top_queries'):
                html += '      <h3 style="color:#0066cc;font-size:14px;margin:16px 0 8px">Top Queries for This Page</h3>\n'
                for q in pg['top_queries'][:10]:
                    html += f'      <div style="background:#f8f9fa;border-left:3px solid #0066cc;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px">{q["query"][:40]} <span style="color:#0066cc;font-size:13px">imp={q["impressions"]:,} clicks={q["clicks"]} pos={q["position"]:.1f}</span></div>\n'
        
        html += '''    </div>
  </div>

'''

    # 10g. Perplexity AEO Citation Test
    if perplexity_data:
        cited = perplexity_data.get('cited', False)
        mentioned = perplexity_data.get('mentioned', False)
        cit_urls = perplexity_data.get('citation_urls', [])
        all_cits = perplexity_data.get('all_citations', [])
        answer = perplexity_data.get('answer_preview', '')
        cost = perplexity_data.get('cost', 0)
        
        status_badge = '<span style="background:#27ae60;color:white;padding:2px 8px;border-radius:4px;font-size:14px;font-weight:600">✅ CITED</span>' if cited else \
                       ('<span style="background:#f39c12;color:white;padding:2px 8px;border-radius:4px;font-size:14px;font-weight:600">🤖 MENTIONED</span>' if mentioned else \
                        '<span style="background:#e74c3c;color:white;padding:2px 8px;border-radius:4px;font-size:14px;font-weight:600">❌ NOT CITED</span>')
        
        html += f'''
  <!-- Perplexity AEO Citation Test -->
  <div class="section">
    <div class="section-title expandable"><strong>AEO Citation Test — Perplexity Sonar AI</strong></div>
    <div class="detail show">
      <p style="font-size:15px;color:#444;margin-bottom:16px">
        Tested whether this page/brand is cited in Perplexity's AI answer for "{keyword}". {status_badge}
        Cost: ${cost:.4f} | Total citations in answer: {len(all_cits)}
      </p>
'''
        
        if cit_urls:
            html += '      <h3 style="color:#27ae60;font-size:14px;margin:16px 0 8px">YOUR CITATION URL(s)</h3>\n'
            for cu in cit_urls:
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #27ae60;padding:8px 12px;margin-bottom:6px;border-radius:0 6px 6px 0;font-size:15px"><a href="{cu}" target="_blank" style="color:#27ae60">{cu}</a></div>\n'
        
        if all_cits:
            html += f'      <h3 style="color:#0066cc;font-size:14px;margin:16px 0 8px">All {len(all_cits)} Citations in AI Answer</h3>\n'
            for c in all_cits[:15]:
                domain = c.split('/')[2] if len(c.split('/')) > 2 else c
                html += f'      <div style="background:#f8f9fa;border-left:3px solid #e0e0e0;padding:6px 12px;margin-bottom:4px;border-radius:0 6px 6px 0;font-size:14px">{domain} — <a href="{c}" target="_blank" style="color:#0066cc">{c[:60]}</a></div>\n'
        
        if answer:
            html += f'      <h3 style="color:#0066cc;font-size:14px;margin:16px 0 8px">AI Answer Preview</h3>\n'
            html += f'      <div style="background:#f8f9fa;padding:12px;border-radius:8px;font-size:15px;color:#444;line-height:1.5">{answer}...</div>\n'
        
        html += '''    </div>
  </div>

'''

    # 11. Fix Lists (Developer + SEO Team)
    html += '''
<div class="section">
  <div class="section-title expandable"><strong>Fix Lists (Developer + SEO Team)</strong></div>
  <div class="detail show">
    <div>
      <div class="fix-col-title">Developer Fix List</div>
'''
    for priority, label, cls in [("P0", "P0 — Critical", "fix-p0"), ("P1", "P1 — Important", "fix-p1"), ("P2", "P2 — Nice to Have", "fix-p2")]:
        items = fix_lists["developer"].get(priority, [])
        html += f'        <div class="fix-priority">{label} ({len(items)})</div>\n'
        for item in items:
            # Try to get fix detail from the item text
            issue_name = item.split(":")[0] if ":" in item else item
            fix_desc, fix_rec = get_issue_fix(issue_name.strip())
            html += f'        <div class="fix-item {cls} expandable" style="cursor:pointer">{item} <span style="font-size:14px;color:#999">▼</span>\n          <div class="detail" style="margin-top:8px;padding:10px;background:#ffffff;border:1px solid #e0e0e0;border-radius:6px">\n            <div style="font-size:15px;color:#333;margin-bottom:4px"><strong>How to fix:</strong> {fix_rec}</div>\n          </div>\n        </div>\n'

    html += '''      </div>
      <div style="margin-top:20px">
        <div class="fix-col-title">SEO Team Fix List</div>
'''
    for priority, label, cls in [("P0", "P0 — Critical", "fix-p0"), ("P1", "P1 — Important", "fix-p1"), ("P2", "P2 — Nice to Have", "fix-p2")]:
        items = fix_lists["seo_team"].get(priority, [])
        html += f'        <div class="fix-priority">{label} ({len(items)})</div>\n'
        for item in items:
            issue_name = item.split(":")[0] if ":" in item else item
            fix_desc, fix_rec = get_issue_fix(issue_name.strip())
            html += f'        <div class="fix-item {cls} expandable" style="cursor:pointer">{item} <span style="font-size:14px;color:#999">▼</span>\n          <div class="detail" style="margin-top:8px;padding:10px;background:#ffffff;border:1px solid #e0e0e0;border-radius:6px">\n            <div style="font-size:15px;color:#333;margin-bottom:4px"><strong>How to fix:</strong> {fix_rec}</div>\n          </div>\n        </div>\n'

    html += '''      </div>
    </div>
  </div>
</div>
'''

    # 12. Citation Readiness (7 Markers)
    if citation_readiness:
        cr = citation_readiness
        cr_score = cr['total_score']
        cr_max = cr['max_score']
        cr_pct = (cr_score / cr_max) * 100
        cr_color = '#27ae60' if cr_pct >= 70 else ('#e67e22' if cr_pct >= 50 else '#cc0000')
        cr_ready = '✅ Ready to publish' if cr['publish_ready'] else '⚠️ Fix before publishing'
        
        marker_cards = ""
        marker_names = {
            'direct_answer': '1. Direct Answer Up Top',
            'question_headings': '2. Question-Shaped Headings',
            'quotable_blocks': '3. Quotable Blocks',
            'first_party_evidence': '4. First-Party Evidence',
            'author_identity': '5. Visible Author Identity',
            'schema_served': '6. Schema as Served',
            'cluster_membership': '7. Cluster Membership',
        }
        for key, name in marker_names.items():
            m = cr['markers'][key]
            status_color = '#27ae60' if m['status'] == 'pass' else ('#e67e22' if m['status'] == 'partial' else '#cc0000')
            status_icon = '✅' if m['status'] == 'pass' else ('⚠️' if m['status'] == 'partial' else '❌')
            marker_cards += f"""      <div style="background:#ffffff;border:1px solid #e0e0e0;border-radius:8px;padding:14px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <strong style="font-size:16px;color:#1a1a1a">{name}</strong>
          <span style="font-size:15px;font-weight:700;color:{status_color}">{status_icon} {m['score']}/10</span>
        </div>
        <div style="font-size:15px;color:#333;margin-bottom:4px">{m['finding']}</div>
        <div style="font-size:15px;color:#0066cc"><strong>Fix:</strong> {m['recommendation']}</div>
      </div>
"""
        
        checklist_html = ""
        for c in cr['checklist']:
            icon = '✅' if c['pass'] else '❌'
            checklist_html += f"        <div style='padding:6px 0;font-size:15px;color:#1a1a1a'>{icon} {c['q']}</div>\n"
        
        html += f'''
<div class="section">
  <div class="section-title expandable"><strong>Citation Readiness (7 Markers from Cited Page Swipe File)</strong></div>
  <div class="detail show">
    <p style="font-size:16px;color:#333;margin-bottom:16px">Based on 18 real pages cited by Claude + Perplexity (May-July 2026). Publish when 5 of 7 markers pass; fix before publishing if under 4.</p>
    
    <div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;padding:16px;margin-bottom:16px;text-align:center">
      <div style="font-size:32px;font-weight:700;color:{cr_color}">{cr_score}/{cr_max}</div>
      <div style="font-size:16px;color:#666;margin-top:4px">Citation Readiness Score ({cr_pct:.0f}%)</div>
      <div style="font-size:16px;font-weight:600;color:{cr_color};margin-top:8px">{cr_ready}</div>
    </div>
    
{marker_cards}
    
    <h3 style="font-size:18px;font-weight:700;color:#1a1a1a;margin-top:20px;margin-bottom:10px">Pre-Publish Checklist</h3>
{checklist_html}
  </div>
</div>

'''

    # 13. Strategic Recommendations
    html += '''
<div class="section">
  <div class="section-title">Strategic Recommendations</div>
'''
    for title, text in recommendations:
        html += f'  <div class="recommendation">{title} {text}</div>\n'

    # Google index status
    html += f'''
<div style="margin-top: 16px; background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 16px; font-size: 15px;">
  <strong style="color: #0066cc;">Google Index Status:</strong>
  <span style="color: {"#27ae60" if google_index.get("indexed") else "#e74c3c"};">
    {"Indexed" if google_index.get("indexed") else "Not indexed"} ({google_index.get("count", 0)} pages found)
  </span>
</div>
'''

    html += '''
<div class="footer">
  Zenfusion Unified Page Auditor v1.0 &middot; Integrates site_auditor + factor_engine + content_auditor + DataForSEO + InfraNodus &middot; noindex, nofollow
</div>

<script>
// Expand/collapse — script is at end of <body> so the DOM is fully parsed;
// no DOMContentLoaded wrapper needed.
(function() {
  document.querySelectorAll('.expandable').forEach(function(el) {
    // Check nextElementSibling first (section-title pattern)
    var detail = el.nextElementSibling;
    if (!detail || !detail.classList.contains('detail')) {
      // Check first child with .detail class (issue-card pattern)
      detail = el.querySelector(':scope > .detail');
    }
    if (detail && detail.classList.contains('detail')) {
      // Sync the ▲/▼ indicator with the current open/closed state
      if (detail.classList.contains('show')) {
        el.classList.add('expanded');
      }
      el.addEventListener('click', function(e) {
        // Don't toggle if clicking a link or button inside
        if (e.target.tagName === 'A' || e.target.tagName === 'BUTTON') return;
        detail.classList.toggle('show');
        el.classList.toggle('expanded');
      });
    }
  });
})();
function copySchema(btn) {
  var text = btn.getAttribute('data-schema');
  // getAttribute auto-decodes HTML entities, so we get the raw JSON back.
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() {
      btn.textContent = '✅ Copied!';
      setTimeout(function() { btn.textContent = '📋 Copy Schema'; }, 2000);
    }).catch(function() {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      btn.textContent = '✅ Copied!';
      setTimeout(function() { btn.textContent = '📋 Copy Schema'; }, 2000);
    });
  } else {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '✅ Copied!';
    setTimeout(function() { btn.textContent = '📋 Copy Schema'; }, 2000);
  }
}
function copyEntities() {
  var entities = document.querySelectorAll('.entity-badge');
  var text = Array.from(entities).map(function(e) { return e.textContent; }).join(', ');
  navigator.clipboard.writeText(text).then(function() {
    alert('Entities copied to clipboard!');
  }).catch(function() {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    alert('Entities copied to clipboard!');
  });
}
function copyText(btn) {
  var text = btn.getAttribute('data-text');
  navigator.clipboard.writeText(text).then(function() {
    btn.textContent = '✅ Copied!';
    setTimeout(function() { btn.textContent = '📋 Copy'; }, 2000);
  }).catch(function() {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '✅ Copied!';
    setTimeout(function() { btn.textContent = '📋 Copy'; }, 2000);
  });
}
</script>

</body>
</html>'''

    return html

# ============================================================================
# MAIN
# ============================================================================
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 page_auditor.py <url> <keyword> [--output path]")
        print("Example: python3 page_auditor.py https://example.com/page 'target keyword'")
        sys.exit(1)

    url = sys.argv[1]
    keyword = sys.argv[2]
    output_path = None

    for i, arg in enumerate(sys.argv[3:], 3):
        if arg == "--output" and i + 1 < len(sys.argv):
            output_path = sys.argv[i + 1]

    print(f"\n{'='*70}")
    print(f"  ZENFUSION UNIFIED PAGE AUDITOR v1.0")
    print(f"{'='*70}")
    print(f"  URL: {url}")
    print(f"  Keyword: {keyword}")
    print(f"{'='*70}\n")

    # 1. Fetch the page HTML
    print("  [1/12] Fetching page HTML...")
    html, status_code, http_resp = fetch_page(url)
    if not html:
        print(f"  FAILED to fetch {url}")
        sys.exit(1)
    print(f"  ✓ Fetched {len(html)} bytes, status {status_code}")

    # 2. Run site_auditor.py's analyze_page()
    print("  [2/11] Running technical SEO audit (13 categories)...")
    all_pages_status = {}
    internal_links_map = defaultdict(list)
    title_map, desc_map, headings_map = {}, {}, {}
    technical_issues = site_auditor.analyze_page(
        url, html, status_code, all_pages_status,
        internal_links_map, title_map, desc_map, headings_map
    )
    c = len(technical_issues.get("Critical", []))
    w = len(technical_issues.get("Warning", []))
    o = len(technical_issues.get("Opportunity", []))
    n = len(technical_issues.get("Notice", []))
    print(f"  ✓ {c} Critical, {w} Warnings, {o} Opportunities, {n} Notices")

    # 3. Run factor_engine.py's extract_factors()
    print("  [3/11] Extracting 173+ ranking factors...")
    creds = ENV
    # 5. Fetch DataForSEO SERP + AI Overview (single API call, reused in step 5)
    print("  [3a/11] Fetching SERP + AI Overview from DataForSEO...")
    serp, ai_overview_data = fetch_serp(keyword)
    if ai_overview_data.get("has_ai_overview"):
        print(f"  ✓ AI Overview found: {ai_overview_data['citation_count']} citations, {ai_overview_data.get('source_diversity', 0)} sources")
    else:
        print(f"  - No AI Overview found")
    print(f"  ✓ {len(serp)} SERP results found")

    factors = factor_engine.extract_factors(
        html, url, keyword, creds=creds, majestic_db=None, ai_overview=ai_overview_data
    )
    print(f"  ✓ Extracted {len(factors)} factors")

    # 4. Run content_auditor.py's analyze_page()
    print("  [4/11] Running content quality audit (6 dimensions)...")
    content_result = content_auditor.analyze_page(url, keyword)
    if content_result:
        print(f"  ✓ Content score: {content_result['overall_score']}/100")
    else:
        print(f"  - Content audit failed, continuing with partial data")
        content_result = None

    # 6. Fetch PageSpeed Insights (free Google API, replaces DataForSEO Lighthouse)
    print("  [6/14] Fetching PageSpeed Insights (Google PSI — free)...")
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
        import psi_helper
        lighthouse = psi_helper.get_psi_report(url, strategy='mobile', categories=['performance', 'seo'])
        if lighthouse and lighthouse.get('scores'):
            # Adapt to the format the rest of the code expects
            lighthouse['scores'] = {
                'performance': lighthouse['scores'].get('performance', 0),
                'seo': lighthouse['scores'].get('seo', 0),
            }
            # Add metric display values
            for short, full in [('first-contentful-paint', 'fcp'), ('largest-contentful-paint', 'lcp'), 
                               ('total-blocking-time', 'tbt'), ('cumulative-layout-shift', 'cls'),
                               ('speed-index', 'si')]:
                m = lighthouse.get('metrics', {}).get(full, {})
                if m:
                    lighthouse['scores'][full] = m.get('display', '')
            print(f"  ✓ PSI: perf={lighthouse['scores'].get('performance', 0)}, seo={lighthouse['scores'].get('seo', 0)} (free)")
            # Add CrUX data
            if lighthouse.get('crux_page'):
                print(f"  ✓ CrUX: {lighthouse['crux_page'].get('category', 'N/A')} (real user data)")
        else:
            print(f"  - PSI data unavailable, falling back to DataForSEO")
            lighthouse = fetch_lighthouse(url)
    except Exception as e:
        print(f"  - PSI failed ({e}), falling back to DataForSEO")
        lighthouse = fetch_lighthouse(url)

    # 7. Check Google index status
    print("  [7/11] Checking Google index status...")
    google_index = check_google_index(url)
    print(f"  ✓ Indexed: {google_index.get('indexed', False)} ({google_index.get('count', 0)} pages)")

    # 8. Run InfraNodus entity extraction
    print("  [8/11] Running InfraNodus entity extraction...")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text_content = soup.get_text(separator=" ", strip=True)
    infranodus = infranodus_analyze(text_content)
    if infranodus.get("entity_count", 0) > 0:
        print(f"  ✓ {infranodus['entity_count']} entities, {infranodus.get('cluster_count', 0)} clusters, {infranodus.get('content_gap_count', 0)} gaps")
    else:
        print(f"  - InfraNodus: {infranodus.get('error', 'No entities')}")

    # 8b. GSC: Real Google Search Console data (free, real impressions/clicks/position)
    print("  [8b/14] Fetching Google Search Console data (real search performance)...")
    gsc_data = None
    try:
        import gsc_helper
        # Try to find the matching GSC property
        gsc_properties = gsc_helper.list_properties()
        target_domain = urlparse(url).netloc
        matching_prop = None
        for prop in gsc_properties:
            prop_domain = prop.replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')
            if target_domain.replace('www.', '') in prop_domain or prop_domain in target_domain.replace('www.', ''):
                matching_prop = prop
                break
        
        if matching_prop:
            # Get keyword performance
            kw_perf = gsc_helper.get_keyword_performance(matching_prop, keyword)
            # Get page performance
            page_path = urlparse(url).path
            page_perf = gsc_helper.get_page_performance(matching_prop, page_path)
            
            gsc_data = {
                'property': matching_prop,
                'keyword_perf': kw_perf,
                'page_perf': page_perf,
            }
            
            if kw_perf:
                print(f"  ✓ GSC keyword '{keyword}': {kw_perf['impressions']:,} imp, {kw_perf['clicks']} clicks, pos {kw_perf['position']:.1f}")
            if page_perf:
                print(f"  ✓ GSC page: {page_perf['impressions']:,} imp, {page_perf['clicks']} clicks, pos {page_perf['position']:.1f}")
                if page_perf.get('top_queries'):
                    print(f"  ✓ Top query: '{page_perf['top_queries'][0]['query']}' ({page_perf['top_queries'][0]['impressions']:,} imp)")
        else:
            print(f"  - No GSC property found for {target_domain}")
    except Exception as e:
        print(f"  - GSC: {e}")

    # 8c. Perplexity AEO Citation Test (pennies per query)
    print("  [8c/14] Testing AEO citation via Perplexity Sonar API...")
    perplexity_data = None
    try:
        import perplexity_helper
        target_domain = urlparse(url).netloc.replace('www.', '')
        brand_name = 'SoftPro' if 'softpro' in target_domain.lower() else None
        
        result = perplexity_helper.test_aeo_citation(keyword, target_domain, brand_name)
        
        perplexity_data = {
            'cited': result.get('cited', False),
            'mentioned': result.get('mentioned', False),
            'citation_urls': result.get('citation_urls', []),
            'all_citations': result.get('all_citations', []),
            'answer_preview': result.get('answer', '')[:500],
            'cost': result.get('cost', 0),
        }
        
        if perplexity_data['cited']:
            print(f"  ✓ Perplexity CITED: {len(perplexity_data['citation_urls'])} citation(s) (${perplexity_data['cost']:.4f})")
        elif perplexity_data['mentioned']:
            print(f"  ~ Perplexity MENTIONED but not cited (${perplexity_data['cost']:.4f})")
        else:
            print(f"  ✗ Perplexity: not cited or mentioned (${perplexity_data['cost']:.4f})")
        print(f"  Total citations in answer: {len(perplexity_data['all_citations'])}")
    except Exception as e:
        print(f"  - Perplexity: {e}")

    # 9. Compute SRS composite score
    print("  [9/11] Computing SRS composite score...")
    srs = compute_srs(technical_issues, content_result, factors, lighthouse)
    print(f"  ✓ Structure: {srs['structure']} | Relevance: {srs['relevance']} | Signal: {srs['signal']} | Composite: {srs['composite']}")

    # 10. Generate dual fix lists
    print("  [10/11] Generating fix lists...")
    fix_lists = generate_fix_lists(technical_issues, content_result, factors, srs, lighthouse)
    dev_total = sum(len(fix_lists["developer"][p]) for p in ["P0", "P1", "P2"])
    seo_total = sum(len(fix_lists["seo_team"][p]) for p in ["P0", "P1", "P2"])
    print(f"  ✓ Developer: {dev_total} items | SEO Team: {seo_total} items")

    # Generate strategic recommendations
    recommendations = generate_recommendations(srs, content_result, factors, lighthouse, serp, ai_overview_data, infranodus)

    # 13. Run Information Gain analysis (Invisible Authority technique)
    print("  [13/14] Running Information Gain analysis (consensus vs unique claims)...")
    ig_result = analyze_information_gain(keyword, serp, url, ENV)
    if ig_result.get("pages_analyzed", 0) > 0:
        print(f"  ✓ IG: {ig_result['pages_analyzed']} pages, {ig_result['total_claims']} claims, {len(ig_result['consensus'])} consensus, {len(ig_result['unique'])} unique, {ig_result.get('repetition_rate', 0)}% repetition")
    else:
        print(f"  - IG: {ig_result.get('error', 'No pages readable')}")

    # 14. Deep SERP scan — top 100 results (cached, first audit only)
    print("  [14/14] Running deep SERP scan (top 100 results, cached)...")
    deep_scan = deep_scan_top100(keyword, ENV)
    if deep_scan.get("stats", {}).get("unique_claims", 0) > 0:
        print(f"  ✓ Deep scan: {deep_scan['stats']['unique_claims']} claims, {deep_scan['stats']['unique_problems']} problems, {len(deep_scan.get('paa_questions', []))} PAA, {len(deep_scan.get('related_searches', []))} related searches")
    else:
        print(f"  - Deep scan: {deep_scan.get('error', 'No results')}")

    # 12. Run SF gap metrics (readability, spelling, technical headers)
    print("  [12/12] Running SF gap metrics (readability, spelling, technical)...")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text_for_analysis = soup.get_text(separator=" ", strip=True)
    
    readability = analyze_readability(text_for_analysis)
    spelling = check_spelling(text_for_analysis)
    tech_headers = analyze_technical_headers(http_resp, html, url)
    
    print(f"  ✓ Readability: {readability['readability_level']} (Flesch: {readability['flesch_reading_ease']})")
    print(f"  ✓ Spelling errors: {spelling['spelling_errors']}")
    print(f"  ✓ Response time: {tech_headers['response_time_ms']}ms, Page size: {tech_headers['page_size_bytes']:,} bytes")
    
    # Add SF metrics to factors dict
    factors['sf_flesch_reading_ease'] = readability['flesch_reading_ease']
    factors['sf_sentence_count'] = readability['sentence_count']
    factors['sf_avg_words_per_sentence'] = readability['avg_words_per_sentence']
    factors['sf_readability_level'] = readability['readability_level']
    factors['sf_text_ratio'] = readability['text_ratio']
    factors['sf_spelling_errors'] = spelling['spelling_errors']
    factors['sf_misspelled_words'] = spelling['misspelled_words']
    factors['sf_response_time_ms'] = tech_headers['response_time_ms']
    factors['sf_page_size_bytes'] = tech_headers['page_size_bytes']
    factors['sf_transferred_bytes'] = tech_headers['transferred_bytes']
    factors['sf_http_version'] = tech_headers['http_version']
    factors['sf_last_modified'] = tech_headers['last_modified']
    factors['sf_cookies_count'] = tech_headers['cookies_count']
    factors['sf_language'] = tech_headers['language']
    factors['sf_x_robots_tag'] = tech_headers['x_robots_tag']
    factors['sf_meta_refresh'] = tech_headers['meta_refresh']
    factors['sf_rel_next'] = tech_headers['rel_next']
    factors['sf_rel_prev'] = tech_headers['rel_prev']
    factors['sf_amphtml'] = tech_headers['amphtml']
    factors['sf_meta_keywords'] = tech_headers['meta_keywords']
    factors['sf_redirect_url'] = tech_headers['redirect_url']
    factors['sf_mobile_alternate'] = tech_headers['mobile_alternate']

    # 15. Citation Readiness Analysis (7 Markers from Cited Page Swipe File)
    print("  [15/15] Running Citation Readiness analysis (7 markers)...")
    try:
        citation_readiness = analyze_citation_readiness(text_content, soup, factors, content_result, serp)
        print(f"  ✓ Citation Readiness: {citation_readiness['total_score']}/{citation_readiness['max_score']} ({citation_readiness['pass_count']}/7 markers passed)")
    except Exception as e:
        print(f"  - Citation Readiness: error ({e})")
        citation_readiness = None

    # 11. Generate comprehensive HTML report
    print("  [11/11] Generating HTML report...")
    report_html = generate_html_report(
        url, keyword, srs, content_result, technical_issues, factors,
        serp, ai_overview_data, lighthouse, google_index, infranodus,
        fix_lists, recommendations, ig_result, deep_scan, gsc_data, perplexity_data,
        citation_readiness
    )

    # Write report
    if not output_path:
        output_dir = os.path.expanduser("~/.hermes/audits/page-auditor")
        os.makedirs(output_dir, exist_ok=True)
        domain = url.split("/")[2].replace("www.", "").replace(".", "_") if "/" in url else "page"
        safe_kw = re.sub(r'[^a-z0-9]+', '_', keyword.lower())[:30]
        output_path = os.path.join(output_dir, f"{domain}_{safe_kw}_unified_audit.html")

    with open(output_path, "w") as f:
        f.write(report_html)

    print(f"\n{'='*70}")
    print(f"  ✓ Report saved to: {output_path}")
    print(f"  ✓ SRS Composite: {srs['composite']}/100")
    print(f"  ✓ Technical Issues: {c}C / {w}W / {o}O / {n}N")
    print(f"  ✓ Factors: {len(factors)}")
    if content_result:
        print(f"  ✓ Content Score: {content_result['overall_score']}/100")
    print(f"  ✓ Fix Items: {dev_total} dev / {seo_total} seo")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()