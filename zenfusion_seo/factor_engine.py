#!/usr/bin/env python3
"""
Zenfusion Factor Analysis Engine v2.0 — Cora SEO replacement for Shopify stores.
Analyzes top-ranking pages for a keyword and tells you exactly what your page needs.

v2.0 upgrades:
- 200+ ranking factors (up from 80)
- InfraNodus entity analysis integration
- Google AI Overview citation analysis (DataForSEO parse_aiserp)
- Majestic Million backlink quality scoring
- Pearson + Kendall correlations alongside Spearman
- Effect size calculation (Cohen's d)
- Factor importance ranking

Usage:
  python3 factor_engine.py <serp_json> <target_url> [--limit N] [--no-infranodus] [--no-ai-overview] [--no-majestic]

Output:
  - Top correlating factors (Spearman + Pearson + Kendall)
  - Factor road map (200+ factors, your page vs top 10 medians)
  - AI Overview analysis (cited domains, product mentions, entity overlap)
  - InfraNodus entity analysis (entities, clusters, content gaps)
  - Majestic Million analysis (match rate, tier targets, opportunities)
  - Developer fix list (theme/code changes)
  - SEO team fix list (Shopify admin accessible changes)
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import sys, json, os, re, time, urllib.request, urllib.parse, ssl, base64
from collections import Counter, defaultdict
from bs4 import BeautifulSoup
from statistics import median, mean, stdev
from math import sqrt

# === CORRELATION FUNCTIONS ===
def spearman_corr(x, y):
    try:
        from scipy.stats import spearmanr
        r, p = spearmanr(x, y)
        if hasattr(r, 'nan'): r = float(r)
        if hasattr(p, 'nan'): p = float(p)
        return float(r), float(p)
    except Exception:
        return _spearman_manual(x, y)

def _spearman_manual(x, y):
    n = len(x)
    if n < 3: return 0.0, 1.0
    def ranks(vals):
        indexed = sorted(enumerate(vals), key=lambda t: t[1])
        r = [0]*n
        i = 0
        while i < n:
            j = i
            while j+1 < n and indexed[j+1][1] == indexed[i][1]: j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j+1): r[indexed[k][0]] = avg_rank
            i = j + 1
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i]-mx)*(ry[i]-my) for i in range(n))
    den = sqrt(sum((rx[i]-mx)**2 for i in range(n)) * sum((ry[i]-my)**2 for i in range(n)))
    r = num / den if den != 0 else 0.0
    t_stat = r * sqrt((n-2)/(1-r*r)) if abs(r) < 1 else 0
    p = max(0.0, min(1.0, 2 * (1 - _t_cdf(abs(t_stat), n-2)))) if n > 2 else 1.0
    return r, p

def _t_cdf(t, df):
    """Approximate CDF of t-distribution using normal approximation for large df."""
    if df > 30:
        from math import erf
        return 0.5 * (1 + erf(t / sqrt(2)))
    # Simple approximation for small df
    z = t / sqrt(1 + t*t/df)
    z = max(-1.0, min(1.0, z))  # Clamp to valid asin domain
    from math import pi, asin
    return 0.5 + asin(z) / pi

def pearson_corr(x, y):
    """Pearson correlation coefficient."""
    n = len(x)
    if n < 3: return 0.0, 1.0
    mx, my = mean(x), mean(y)
    num = sum((x[i]-mx)*(y[i]-my) for i in range(n))
    den = sqrt(sum((x[i]-mx)**2 for i in range(n)) * sum((y[i]-my)**2 for i in range(n)))
    r = num / den if den != 0 else 0.0
    t_stat = r * sqrt((n-2)/(1-r*r)) if abs(r) < 1 else 0
    p = max(0.0, min(1.0, 2 * (1 - _t_cdf(abs(t_stat), n-2)))) if n > 2 else 1.0
    return r, p

def kendall_tau(x, y):
    """Kendall's tau rank correlation."""
    n = len(x)
    if n < 3: return 0.0, 1.0
    concordant, discordant = 0, 0
    for i in range(n):
        for j in range(i+1, n):
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            if dx * dy > 0: concordant += 1
            elif dx * dy < 0: discordant += 1
    tau = (concordant - discordant) / (n * (n-1) / 2) if n > 1 else 0
    # Approximate p-value
    var = 2 * (2*n + 5) / (9 * n * (n-1)) if n > 1 else 1
    z = tau / sqrt(var) if var > 0 else 0
    from math import erf
    p = 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2))))
    return tau, p

def cohens_d(x, y):
    """Effect size: Cohen's d between two groups."""
    try:
        nx, ny = len(x), len(y)
        if nx < 2 or ny < 2: return 0.0
        mx, my = mean(x), mean(y)
        sx, sy = stdev(x), stdev(y)
        pooled_sd = sqrt(((nx-1)*sx**2 + (ny-1)*sy**2) / (nx+ny-2))
        return (mx - my) / pooled_sd if pooled_sd > 0 else 0.0
    except:
        return 0.0

# === PAGE FETCHING ===
def fetch_page(url, timeout=30):
    """Fetch a page using ScrapeOwl API (with JS rendering) or fall back to urllib."""
    api_key = os.environ.get('SCRAPEOWL_API_KEY', '')
    if not api_key:
        env_path = os.path.expanduser('~/.hermes/.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('SCRAPEOWL_API_KEY='):
                        api_key = line.split('=', 1)[1]
                        break

    if api_key:
        try:
            import urllib.parse as up
            params = up.urlencode({'api_key': api_key, 'url': url, 'render_js': 'true'})
            scrape_url = f"https://api.scrapeowl.com/v1/scrape?{params}"
            start = time.time()
            req = urllib.request.Request(scrape_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='replace'))
            if data.get('status') == 200 and 'html' in data:
                html = data['html']
                load_time = time.time() - start
                return html, load_time
        except Exception:
            pass

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'})
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        html = resp.read().decode('utf-8', errors='replace')
    load_time = time.time() - start
    return html, load_time

# === CREDENTIAL LOADING ===
def load_env():
    """Load all credentials from ~/.hermes/.env into a dict."""
    creds = {}
    env_path = os.path.expanduser('~/.hermes/.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k] = v
    return creds

# === INFRANODUS INTEGRATION ===
def infranodus_analyze(text, api_key):
    """Send text to InfraNodus and return entity graph metrics."""
    try:
        url = "https://infranodus.com/api/v1/graphAndStatements"
        payload = json.dumps({"text": text[:5000]}).encode()  # Limit to 5K chars
        req = urllib.request.Request(url, data=payload, method='POST', headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())

        graph_data = data.get('entriesAndGraphOfContext', {}).get('graph', {}).get('graphologyGraph', {})
        attrs = graph_data.get('attributes', {})
        nodes = graph_data.get('nodes', [])
        edges = graph_data.get('edges', [])
        clusters = attrs.get('allClusters', [])
        gaps = attrs.get('gaps', [])
        diversity = attrs.get('diversity_stats', {})
        top_nodes = attrs.get('top_influential_nodes', [])

        # Extract entity labels
        entity_labels = [n.get('key', '') for n in nodes]

        return {
            'entity_count': len(nodes),
            'edge_count': len(edges),
            'cluster_count': len(clusters),
            'content_gap_count': len(gaps),
            'diversity_score': 1.0 if diversity.get('diversity_score') == 'diverse' else 0.5 if diversity.get('diversity_score') == 'normal' else 0.0,
            'top_entities': entity_labels[:20],
            'cluster_names': [c.get('aiName', '') for c in clusters[:8]],
        }
    except Exception as e:
        return {'entity_count': 0, 'edge_count': 0, 'cluster_count': 0, 'content_gap_count': 0,
                'diversity_score': 0, 'top_entities': [], 'cluster_names': [], 'error': str(e)}

# === AI OVERVIEW FETCHING ===
def fetch_ai_overview(keyword, creds):
    """Fetch Google AI Overview for a keyword via DataForSEO."""
    try:
        login = creds.get('DATAFORSEO_LOGIN', '')
        password = creds.get('DATAFORSEO_PASSWORD', '')
        auth = base64.b64encode(f'{login}:{password}'.encode()).decode()

        url = 'https://api.dataforseo.com/v3/serp/google/organic/live/advanced'
        payload = json.dumps([{
            'keyword': keyword,
            'location_code': 2840,
            'language_code': 'en',
            'device': 'desktop',
            'use_improved_etv': True,
            'depth': 100,
            'parse_aiserp': True
        }]).encode()

        req = urllib.request.Request(url, data=payload, headers={
            'Authorization': f'Basic {auth}',
            'Content-Type': 'application/json'
        })

        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _fix_goto(json.loads(resp.read()), 'serp/')

        items = data['tasks'][0]['result'][0].get('items', [])

        for item in items:
            # DataForSEO checks array (replaces deprecated is_image, is_video, etc.)
            # checks returns null when no special properties, or a list of property strings
            item_checks = item.get('checks') or []
            is_featured_snippet = 'is_featured_snippet' in item_checks
            is_image = 'is_image' in item_checks
            is_video = 'is_video' in item_checks
            is_highly_cited = 'is_highly_cited' in item_checks
            
            if item.get('type') == 'ai_overview':
                markdown = item.get('markdown', '')
                # Parse cited sources [[N]](url)
                citations = re.findall(r'\[\[(\d+)\]\]\(([^)]+)\)', markdown)
                cited_urls = [u for _, u in citations]
                cited_domains = list(set(u.split('/')[2] if '/' in u else u for u in cited_urls))

                # Parse product mentions
                products = re.findall(r'\*\*([^*]+)\*\*', markdown)

                # Clean text
                text = re.sub(r'\[.*?\]\(.*?\)', '', markdown)
                text = re.sub(r'[*#\[\]]', '', text)

                return {
                    'has_ai_overview': True,
                    'markdown': markdown[:10000],
                    'text': text[:5000],
                    'cited_domains': cited_domains,
                    'cited_urls': cited_urls,
                    'citation_count': len(cited_urls),
                    'products_mentioned': [p.strip() for p in products[:20] if p.strip()],
                    'source_diversity': len(cited_domains),
                }

        return {'has_ai_overview': False, 'cited_domains': [], 'citation_count': 0}
    except Exception as e:
        return {'has_ai_overview': False, 'cited_domains': [], 'citation_count': 0, 'error': str(e)}

# === MAJESTIC MILLION INTEGRATION ===
def load_majestic_index():
    """Load the Majestic Million index."""
    index_path = os.path.expanduser('~/.hermes/audits/factor-engine-data/majestic_million_index.json')
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                return json.load(f)
        except:
            pass
    return None

def fetch_referring_domains(domain, creds, limit=500):
    """Fetch referring domains via DataForSEO."""
    try:
        login = creds.get('DATAFORSEO_LOGIN', '')
        password = creds.get('DATAFORSEO_PASSWORD', '')
        auth = base64.b64encode(f'{login}:{password}'.encode()).decode()

        url = 'https://api.dataforseo.com/v3/backlinks/referring_domains/live'
        payload = json.dumps([{
            'target': domain, 'main_domain': domain,
            'search_mode': 'as_is', 'limit': limit,
        }]).encode()

        req = urllib.request.Request(url, data=payload, headers={
            'Authorization': f'Basic {auth}',
            'Content-Type': 'application/json'
        })

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        items = data['tasks'][0]['result'][0].get('items', [])
        return [item.get('domain', '').lower() for item in items if item.get('domain')]
    except:
        return []

# === LSI TERM LISTS ===
LSI_TERMS = {
    'water softener': ['salt', 'resin', 'regeneration', 'ion exchange', 'hardness', 'grain',
        'capacity', 'brine', 'filter', 'well water', 'iron', 'magnesium', 'calcium',
        'softening', 'conditioner', 'valve', 'timer', 'meter', 'bypass', 'nsf',
        'certified', 'installation', 'plumbing', 'gpm', 'flow rate', 'pressure',
        'warranty', 'efficiency', 'demand', 'upflow', 'downflow'],
    'iron filter': ['iron', 'ferrous', 'ferric', 'bacteria', 'manganese', 'hydrogen sulfide',
        'oxidation', 'aeration', 'greensand', 'birm', 'air injection', 'well water',
        'pH', 'ppm', 'mg/L', 'staining', 'odor', 'rotten egg', 'sulfur'],
    'carbon filter': ['activated carbon', 'coconut shell', 'carbon block', 'granular',
        'chlorine', 'VOC', 'chloramine', 'taste', 'odor', 'PFAS', 'lead',
        'mercury', 'NSF', 'micron', 'flow rate', 'capacity', 'replacement'],
    'fluoride filter': ['fluoride', 'fluorosis', 'bone char', 'activated alumina',
        'reverse osmosis', 'deionization', 'distillation', 'berkey', 'NSF',
        'ppm', 'EPA', 'dental', 'skeletal'],
}

def get_lsi_terms(keyword):
    """Get LSI terms for a keyword."""
    kw_lower = keyword.lower()
    for key, terms in LSI_TERMS.items():
        if key in kw_lower:
            return terms
    # Default water treatment terms
    return ['water', 'filter', 'softener', 'system', 'treatment', 'filtration',
            'hardness', 'installation', 'maintenance', 'warranty', 'certified',
            'capacity', 'flow rate', 'pressure', 'NSF', 'quality']

# === FACTOR EXTRACTION (200+ factors) ===
def extract_factors(html, url, keyword, creds=None, majestic_db=None, ai_overview=None):
    """Extract 200+ ranking factors from a page."""
    factors = {}
    kw_lower = keyword.lower()
    kw_words = [w for w in kw_lower.split() if len(w) > 2]

    soup = BeautifulSoup(html, 'html.parser')

    # Extract JSON-LD blocks BEFORE removing scripts (they get decomposed below)
    json_ld_blocks = soup.find_all('script', type='application/ld+json')
    json_ld_text = ' '.join(b.get_text() for b in json_ld_blocks)

    # Remove scripts and styles for text analysis
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    
    # Create a CONTENT-ONLY soup for content-level factors (links, headings, anchors, paragraphs)
    # This strips navigation, header, footer, sidebar, modals — everything that isn't main content
    soup_content = BeautifulSoup(str(soup), 'html.parser')
    # Remove nav, header, footer, aside (sidebars), and modal/dialog elements
    for tag in soup_content.find_all(['nav', 'header', 'footer', 'aside']):
        tag.decompose()
    # Remove common Magento/Shopify nav containers
    for selector in ['.modals-wrapper', '.modal-slid', '.popup', '.sidebar', '.header', '.footer',
                     '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]']:
        for el in soup_content.select(selector):
            el.decompose()
    
    text_content = soup.get_text(separator=' ', strip=True)
    words = text_content.split()
    word_count = len(words)
    text_lower = text_content.lower()
    content_text = soup_content.get_text(separator=' ', strip=True)
    content_lower = content_text.lower()
    content_words = content_text.split()

    # --- ON-PAGE FACTORS (40) ---
    # Title
    title_tag = soup.find('title')
    title_text = title_tag.get_text() if title_tag else ''
    title_lower = title_text.lower()
    factors['title_length'] = len(title_text)
    factors['title_keyword_present'] = 1 if kw_lower in title_lower else 0
    if kw_lower in title_lower:
        factors['title_keyword_position'] = title_lower.index(kw_lower) / max(len(title_lower), 1)
    else:
        factors['title_keyword_position'] = -1
    factors['title_keyword_at_start'] = 1 if title_lower.strip().startswith(kw_lower) else 0
    kw_in_title = sum(1 for w in kw_words if w in title_lower)
    factors['title_keyword_density'] = kw_in_title / max(len(title_text.split()), 1)

    # H1 — use content-only soup to exclude modal/header H1s
    h1_tags = soup_content.find_all('h1')
    h1_text = ' '.join(h.get_text() for h in h1_tags)
    h1_lower = h1_text.lower()
    factors['h1_count'] = len(h1_tags)
    factors['h1_length'] = len(h1_text)
    factors['h1_keyword_present'] = 1 if kw_lower in h1_lower else 0
    factors['h1_keyword_position'] = h1_lower.index(kw_lower) / max(len(h1_lower), 1) if kw_lower in h1_lower else -1

    # H2-H6 — use content-only soup to exclude nav/sidebar headings
    for i in range(2, 7):
        tags = soup_content.find_all(f'h{i}')
        text = ' '.join(t.get_text() for t in tags)
        factors[f'h{i}_count'] = len(tags)
        factors[f'h{i}_total_length'] = len(text)
        factors[f'h{i}_keyword_present'] = 1 if kw_lower in text.lower() else 0

    h2_tags = soup_content.find_all('h2')
    if h2_tags:
        first_h2 = h2_tags[0].get_text().lower()
        factors['h2_keyword_in_first'] = 1 if kw_lower in first_h2 else 0
    else:
        factors['h2_keyword_in_first'] = 0

    # Body content
    factors['word_count'] = word_count
    factors['char_count'] = len(text_content)
    factors['keyword_count'] = text_lower.count(kw_lower)
    factors['keyword_density'] = factors['keyword_count'] / max(word_count, 1) * 100

    # Keyword position — use content-only text (without nav/header/footer) for accurate "first 100" check
    first_100 = ' '.join(content_words[:100]).lower()
    last_100 = ' '.join(content_words[-100:]).lower()
    factors['keyword_in_first_100'] = 1 if kw_lower in first_100 else 0
    factors['keyword_in_last_100'] = 1 if kw_lower in last_100 else 0

    # Paragraphs — use content-only soup
    para_tags = soup_content.find_all('p')
    para_lengths = [len(p.get_text().split()) for p in para_tags]
    factors['paragraph_count'] = len(para_tags)
    factors['avg_paragraph_length'] = mean(para_lengths) if para_lengths else 0
    factors['paragraph_length_variance'] = stdev(para_lengths) if len(para_lengths) > 1 else 0

    # Sentences
    sentences = re.split(r'[.!?]+', text_content)
    sentences = [s.strip() for s in sentences if s.strip()]
    sent_lengths = [len(s.split()) for s in sentences]
    factors['sentence_count'] = len(sentences)
    factors['avg_sentence_length'] = mean(sent_lengths) if sent_lengths else 0

    # Bold/strong
    bold_tags = soup.find_all(['b', 'strong'])
    bold_text = ' '.join(b.get_text() for b in bold_tags).lower()
    factors['bold_count'] = len(bold_tags)
    factors['keyword_in_bold'] = 1 if kw_lower in bold_text else 0

    # Lists
    ul_tags = soup.find_all('ul')
    ol_tags = soup.find_all('ol')
    li_tags = soup.find_all('li')
    li_text = ' '.join(li.get_text() for li in li_tags).lower()
    factors['ul_count'] = len(ul_tags)
    factors['ol_count'] = len(ol_tags)
    factors['li_count'] = len(li_tags)
    factors['keyword_in_lists'] = 1 if kw_lower in li_text else 0

    # Tables
    # Tables — use content-only soup
    table_tags = soup_content.find_all('table')
    table_text = ' '.join(t.get_text() for t in table_tags).lower()
    factors['table_count'] = len(table_tags)
    factors['table_rows'] = sum(len(t.find_all('tr')) for t in table_tags)
    factors['keyword_in_tables'] = 1 if kw_lower in table_text else 0

    # --- LINK FACTORS (25) ---
    # Use CONTENT-ONLY soup for link analysis — counts content links, not nav/menu/footer links
    all_links = soup_content.find_all('a', href=True)
    internal_links = []
    external_links = []
    external_link_urls = set()  # Deduplicate by URL
    nofollow_links = 0
    exact_match = 0
    partial_match = 0
    branded_match = 0
    broken_links = 0
    nav_links = 0
    footer_links = 0
    body_links = 0
    external_domains = set()

    # Internal vs external — use proper URL parsing
    from urllib.parse import urlparse as _urlparse
    try:
        page_domain = _urlparse(url).netloc.lower().replace('www.', '')
    except:
        page_domain = ''

    for a in all_links:
        href = a.get('href', '')
        anchor = a.get_text().strip().lower()
        rel = a.get('rel', [])
        if isinstance(rel, list):
            rel = ' '.join(rel).lower()
        else:
            rel = str(rel).lower()

        # Internal vs external — check domain match properly
        if href.startswith('/') and not href.startswith('//'):
            internal_links.append(a)
        elif href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:') or href.startswith('tel:'):
            pass  # skip non-URL links
        elif href.startswith('http'):
            try:
                href_domain = _urlparse(href).netloc.lower().replace('www.', '')
                if href_domain == page_domain or page_domain in href_domain or href_domain in page_domain:
                    internal_links.append(a)
                else:
                    # Deduplicate external links by URL
                    href_lower = href.lower().rstrip('/')
                    if href_lower not in external_link_urls:
                        external_links.append(a)
                        external_link_urls.add(href_lower)
                    external_domains.add(href_domain)
            except:
                external_links.append(a)
        elif href.startswith('//'):
            # Protocol-relative — same domain
            internal_links.append(a)

        if 'nofollow' in rel:
            nofollow_links += 1

        # Anchor text analysis
        if anchor:
            if anchor == kw_lower:
                exact_match += 1
            elif any(w in anchor for w in kw_words):
                partial_match += 1
            else:
                branded_match += 1

        # Broken links
        if href in ['#', '', 'javascript:void(0)']:
            broken_links += 1

        # Link position
        parent = a.parent
        if parent and parent.name in ['nav', 'header']:
            nav_links += 1
        elif parent and parent.name == 'footer':
            footer_links += 1
        else:
            body_links += 1

    factors['internal_links'] = len(internal_links)
    factors['external_links'] = len(external_links)
    factors['total_links'] = len(all_links)
    factors['nofollow_links'] = nofollow_links
    factors['exact_match_anchors'] = exact_match
    factors['partial_match_anchors'] = partial_match
    factors['branded_anchors'] = branded_match
    factors['external_unique_domains'] = len(external_domains)
    factors['link_density'] = len(all_links) / max(word_count, 1) * 100
    factors['nav_links'] = nav_links
    factors['footer_links'] = footer_links
    factors['body_links'] = body_links
    total_anchors = exact_match + partial_match + branded_match
    factors['anchor_diversity_ratio'] = (partial_match + branded_match) / max(total_anchors, 1)
    factors['broken_links'] = broken_links
    factors['internal_nofollow'] = sum(1 for a in internal_links if 'nofollow' in str(a.get('rel', '')).lower())
    factors['external_nofollow'] = sum(1 for a in external_links if 'nofollow' in str(a.get('rel', '')).lower())

    # --- TECHNICAL FACTORS (30) ---
    parsed = urllib.parse.urlparse(url)
    factors['url_length'] = len(url)
    factors['url_depth'] = len([p for p in parsed.path.split('/') if p])
    factors['url_has_keyword'] = 1 if kw_lower.replace(' ', '-') in url.lower() or kw_lower.replace(' ', '_') in url.lower() else 0
    factors['url_hyphens'] = url.count('-')
    factors['url_underscores'] = url.count('_')
    factors['url_https'] = 1 if url.startswith('https') else 0
    factors['url_www'] = 1 if 'www.' in url else 0

    # Canonical
    canon = soup.find('link', rel='canonical')
    factors['canonical_present'] = 1 if canon else 0
    factors['canonical_self_referencing'] = 1 if canon and canon.get('href', '').rstrip('/') in url.rstrip('/') else 0

    # Meta robots
    meta_robots = soup.find('meta', attrs={'name': 'robots'})
    robots_content = meta_robots.get('content', '').lower() if meta_robots else ''
    factors['meta_robots_present'] = 1 if meta_robots else 0
    factors['meta_robots_noindex'] = 1 if 'noindex' in robots_content else 0
    factors['meta_robots_nofollow'] = 1 if 'nofollow' in robots_content else 0

    # Page size
    html_size = len(html)
    factors['page_size_kb'] = round(html_size / 1024, 1)
    factors['html_size_kb'] = round(len(html) / 1024, 1)

    # CSS/JS
    factors['css_count'] = len(soup.find_all('link', rel='stylesheet'))
    factors['js_count'] = len(soup.find_all('script', src=True))
    factors['font_count'] = len(soup.find_all('link', rel='preload', href=True))

    # DOM
    all_tags = soup.find_all(True)
    factors['dom_total_elements'] = len(all_tags)
    factors['dom_tag_diversity'] = len(set(t.name for t in all_tags))

    # Max DOM depth
    def max_depth(tag, d=0):
        children = tag.find_all(True, recursive=False)
        if not children: return d
        return max(max_depth(c, d+1) for c in children)
    factors['dom_max_depth'] = max_depth(soup) if all_tags else 0

    # Performance
    factors['load_time'] = 0  # Set by caller

    # Open Graph
    og_title = soup.find('meta', property='og:title')
    og_desc = soup.find('meta', property='og:description')
    og_image = soup.find('meta', property='og:image')
    og_type = soup.find('meta', property='og:type')
    factors['og_title'] = 1 if og_title else 0
    factors['og_description'] = 1 if og_desc else 0
    factors['og_image'] = 1 if og_image else 0
    factors['og_type'] = 1 if og_type else 0

    # Twitter Card
    tw_card = soup.find('meta', attrs={'name': 'twitter:card'})
    factors['twitter_card'] = 1 if tw_card else 0

    # Hreflang
    hreflang_tags = soup.find_all('link', rel='alternate', hreflang=True)
    factors['hreflang_present'] = 1 if hreflang_tags else 0
    factors['hreflang_count'] = len(hreflang_tags)

    # --- SCHEMA FACTORS (20) ---
    # Use pre-extracted JSON-LD blocks (scripts were decomposed above)
    factors['json_ld_count'] = len(json_ld_blocks)

    # Parse schema types from pre-extracted text
    schema_text = json_ld_text.lower()
    schema_types = ['product', 'review', 'aggregaterating', 'faqpage', 'organization',
                    'breadcrumblist', 'article', 'videoobject', 'itempage', 'webpage']
    for st in schema_types:
        factors[f'has_{st}_schema'] = 1 if f'"{st}"' in schema_text or f"'{st}'" in schema_text else 0

    factors['schema_type_count'] = sum(1 for st in schema_types if factors.get(f'has_{st}_schema'))
    factors['schema_property_count'] = schema_text.count('"@type"') + schema_text.count("'@type'")
    factors['sameas_count'] = schema_text.count('sameas')

    # Microdata/RDFa
    factors['has_microdata'] = 1 if soup.find(attrs={'itemtype': True}) else 0
    factors['has_rdfa'] = 1 if soup.find(attrs={'property': True, 'vocab': True}) else 0

    # --- SHOPIFY FACTORS (20) ---
    html_lower = html.lower()
    factors['shopify_cdn'] = 1 if 'cdn.shopify.com' in html_lower else 0
    factors['shopify_theme'] = 1 if 'shopify.theme' in html_lower or 'theme-store' in html_lower else 0
    factors['shopify_section'] = 1 if 'data-section-id' in html_lower or 'shopify-section' in html_lower else 0
    factors['cart_link'] = 1 if '/cart' in html_lower or 'cart-link' in html_lower else 0
    factors['checkout_link'] = 1 if '/checkout' in html_lower else 0
    factors['is_product_page'] = 1 if '/products/' in url.lower() else 0
    factors['is_collection_page'] = 1 if '/collections/' in url.lower() else 0
    factors['is_blog_page'] = 1 if '/blogs/' in url.lower() or '/blog/' in url.lower() else 0
    factors['is_page'] = 1 if '/pages/' in url.lower() else 0
    factors['shopify_app'] = 1 if 'shopify.app' in html_lower or 'apps.shopify.com' in html_lower else 0
    factors['liquid_indicator'] = 1 if '{{' in html and '}}' in html else 0
    factors['variant_url'] = 1 if '?variant=' in url.lower() else 0
    factors['shopify_metafield'] = 1 if 'metafield' in html_lower else 0
    factors['shopify_money_format'] = 1 if 'money_format' in html_lower or 'shopify.money' in html_lower else 0
    factors['shopify_analytics'] = 1 if 'shopify-analytics' in html_lower or 'gtag' in html_lower else 0
    factors['shopify_storefront'] = 1 if 'storefront' in html_lower else 0
    factors['shopify_currency'] = 1 if 'currency' in html_lower and 'shopify' in html_lower else 0
    factors['shopify_locale'] = 1 if 'shopify.locale' in html_lower else 0
    factors['has_buy_button'] = 1 if 'add to cart' in html_lower or 'buy now' in html_lower or 'add-to-cart' in html_lower else 0
    factors['has_shopify_reviews'] = 1 if 'judgeme' in html_lower or 'yotpo' in html_lower or 'stamped' in html_lower or 'loox' in html_lower or 'okendo' in html_lower else 0

    # --- SEMANTIC FACTORS (25) ---
    lsi_terms = get_lsi_terms(keyword)
    lsi_found = [t for t in lsi_terms if t in text_lower]
    factors['lsi_term_count'] = len(lsi_found)
    factors['lsi_coverage_pct'] = len(lsi_found) / max(len(lsi_terms), 1) * 100

    # FAQ detection
    faq_keywords = ['faq', 'frequently asked', 'questions', 'q:', 'a:']
    factors['has_faq_section'] = 1 if any(kw in text_lower for kw in faq_keywords) else 0
    factors['faq_question_count'] = len(re.findall(r'\b(?:what|how|why|when|where|do|does|can|is|are|should|will)\b\s', text_lower[:5000]))

    # Content structure
    factors['has_comparison'] = 1 if any(kw in text_lower for kw in ['vs', 'versus', 'compare', 'comparison']) else 0
    factors['has_spec_table'] = 1 if any(kw in text_lower for kw in ['specification', 'specs', 'dimensions', 'weight', 'capacity']) and table_tags else 0
    factors['has_howto'] = 1 if any(kw in text_lower for kw in ['how to', 'step by step', 'guide', 'tutorial', 'instructions']) else 0
    factors['has_reviews'] = 1 if any(kw in text_lower for kw in ['review', 'rating', 'stars', 'customer', 'testimonial']) else 0
    factors['has_intro_body_conclusion'] = 1 if len(para_tags) > 5 and word_count > 500 else 0

    # Passage optimization (question-answer pairs)
    headings = soup.find_all(['h2', 'h3'])
    qa_count = sum(1 for h in headings if any(h.get_text().strip().lower().startswith(q) for q in ['what', 'how', 'why', 'when', 'where', 'do', 'does', 'can', 'is', 'are']))
    factors['passage_qa_count'] = qa_count

    # EAV patterns
    factors['has_eav_table'] = 1 if table_tags and any(kw in table_text for kw in ['specification', 'property', 'attribute', 'feature']) else 0

    # InfraNodus integration (optional)
    if creds and creds.get('INFRANODUS_API_KEY'):
        infra = infranodus_analyze(text_content[:5000], creds['INFRANODUS_API_KEY'])
        factors['infr_entity_count'] = infra.get('entity_count', 0)
        factors['infr_edge_count'] = infra.get('edge_count', 0)
        factors['infr_cluster_count'] = infra.get('cluster_count', 0)
        factors['infr_content_gap_count'] = infra.get('content_gap_count', 0)
        factors['infr_diversity_score'] = infra.get('diversity_score', 0)
    else:
        factors['infr_entity_count'] = 0
        factors['infr_edge_count'] = 0
        factors['infr_cluster_count'] = 0
        factors['infr_content_gap_count'] = 0
        factors['infr_diversity_score'] = 0

    # --- MEDIA FACTORS (15) ---
    # Use content-only soup to exclude nav/sidebar images
    all_img_tags = soup_content.find_all('img')
    # Filter out navigation icons, category thumbnails, tracking pixels
    content_images = []
    for img in all_img_tags:
        src = (img.get('src', '') or img.get('data-src', '') or '').lower()
        w = img.get('width', '')
        h = img.get('height', '')
        # Skip if width/height attributes indicate tiny image
        try:
            if w and int(str(w).replace('px','')) <= 50: continue
            if h and int(str(h).replace('px','')) <= 50: continue
        except (ValueError, TypeError):
            pass
        # Skip tracking pixels, sprites, blank images
        if any(skip in src for skip in ['1x1', 'pixel', 'tracking', 'blank.gif', 'spacer', 'placeholder']):
            continue
        # Skip SVG icons
        if src.endswith('.svg') and 'icon' in src:
            continue
        # Skip category/navigation thumbnails (Magento/Shopify nav images)
        if '/category/' in src and '/product/' not in src:
            continue
        # Skip Magento menu/wysiwyg images (navigation, not content)
        if '/wysiwyg/' in src or '/menu/' in src:
            continue
        # Skip logo/favicon
        if any(skip in src for skip in ['logo', 'favicon', 'sprite', 'icon-', '-icon']):
            continue
        content_images.append(img)
    
    img_tags = content_images
    img_alts = [img.get('alt', '') for img in img_tags]
    img_with_alt = sum(1 for a in img_alts if a.strip())
    img_without_alt = len(img_alts) - img_with_alt
    img_alt_text = ' '.join(img_alts).lower()

    factors['image_count'] = len(img_tags)
    factors['total_img_tags'] = len(all_img_tags)  # raw count for reference
    factors['images_with_alt'] = img_with_alt
    factors['images_without_alt'] = img_without_alt
    factors['image_alt_keyword'] = 1 if kw_lower in img_alt_text else 0
    factors['image_to_text_ratio'] = len(img_tags) / max(word_count, 1) * 100

    # Image formats
    img_srcs = [img.get('src', '') + img.get('data-src', '') for img in img_tags]
    factors['webp_images'] = sum(1 for s in img_srcs if '.webp' in s.lower())
    factors['jpg_images'] = sum(1 for s in img_srcs if '.jpg' in s.lower() or '.jpeg' in s.lower())
    factors['png_images'] = sum(1 for s in img_srcs if '.png' in s.lower())
    factors['svg_images'] = sum(1 for s in img_srcs if '.svg' in s.lower())

    # Lazy loading
    factors['lazy_loading'] = sum(1 for img in img_tags if 'lazy' in str(img.get('loading', '')).lower() or 'data-src' in img.attrs)

    # Video/audio
    factors['video_count'] = len(soup.find_all('video')) + len(soup.find_all('iframe', src=re.compile(r'youtube|vimeo')))
    factors['audio_count'] = len(soup.find_all('audio'))

    # --- AI OVERVIEW FACTORS (10) ---
    if ai_overview and ai_overview.get('has_ai_overview'):
        target_domain = url.split('/')[2].replace('www.', '') if '/' in url else ''
        ai_cited = ai_overview.get('cited_domains', [])
        ai_text = ai_overview.get('text', '').lower()
        ai_products = ai_overview.get('products_mentioned', [])

        factors['ai_target_mentioned'] = 1 if target_domain in ai_text or any(p.lower() in ai_text for p in ai_products if target_domain.split('.')[0] in p.lower()) else 0
        factors['ai_target_cited'] = 1 if target_domain in [d.replace('www.', '') for d in ai_cited] else 0
        factors['ai_citation_count'] = sum(1 for d in ai_cited if target_domain in d)
        factors['ai_source_diversity'] = ai_overview.get('source_diversity', 0)
        factors['ai_overview_length'] = len(ai_text)
        factors['ai_product_count'] = len(ai_products)
        factors['ai_competitor_citations'] = len([d for d in ai_cited if target_domain not in d])

        # Entity overlap (how many of page's top entities appear in AI Overview)
        page_entities = factors.get('infr_entity_count', 0)
        ai_entity_overlap = 0
        if page_entities > 0 and ai_text:
            # Simple word overlap check
            page_words = set(text_lower.split())
            ai_words = set(ai_text.split())
            ai_entity_overlap = len(page_words & ai_words) / max(len(ai_words), 1) * 100
        factors['ai_entity_overlap_pct'] = round(ai_entity_overlap, 1)
        factors['ai_has_overview'] = 1
        factors['ai_markdown_length'] = len(ai_overview.get('markdown', ''))
    else:
        for k in ['ai_target_mentioned', 'ai_target_cited', 'ai_citation_count', 'ai_source_diversity',
                   'ai_overview_length', 'ai_product_count', 'ai_competitor_citations',
                   'ai_entity_overlap_pct', 'ai_has_overview', 'ai_markdown_length']:
            factors[k] = 0

    # --- MAJESTIC MILLION FACTORS (15) ---
    if majestic_db and creds:
        domain = url.split('/')[2].replace('www.', '') if '/' in url else ''
        ref_domains = fetch_referring_domains(domain, creds, limit=500)
        mm_matches = [d for d in ref_domains if d in majestic_db]
        mm_ranks = [majestic_db[d] for d in mm_matches]

        factors['mm_match_count'] = len(mm_matches)
        factors['mm_match_rate'] = len(mm_matches) / max(len(ref_domains), 1) * 100
        factors['mm_top_rank'] = min(mm_ranks) if mm_ranks else 0
        factors['mm_avg_rank'] = int(mean(mm_ranks)) if mm_ranks else 0
        factors['mm_median_rank'] = int(median(mm_ranks)) if mm_ranks else 0
        factors['mm_ref_domains_checked'] = len(ref_domains)

        # Tier building targets (top 10 Majestic Million referrers)
        factors['mm_tier_targets'] = len([r for r in mm_ranks if r < 10000])
        factors['mm_tier_priority_score'] = sum(1 / max(r, 1) * 1000 for r in mm_ranks if r < 10000)

        # Link opportunity score (inverse of match rate — lower match = more opportunity)
        factors['mm_opportunity_score'] = 100 - factors['mm_match_rate']
        factors['mm_in_majestic'] = 1 if domain in majestic_db else 0
        factors['mm_majestic_rank'] = majestic_db.get(domain, 0)

        # Strength categories
        factors['mm_strong_links'] = len([r for r in mm_ranks if r < 1000])
        factors['mm_medium_links'] = len([r for r in mm_ranks if 1000 <= r < 100000])
        factors['mm_weak_links'] = len([r for r in mm_ranks if r >= 100000])
    else:
        for k in ['mm_match_count', 'mm_match_rate', 'mm_top_rank', 'mm_avg_rank', 'mm_median_rank',
                   'mm_ref_domains_checked', 'mm_tier_targets', 'mm_tier_priority_score',
                   'mm_opportunity_score', 'mm_in_majestic', 'mm_majestic_rank',
                   'mm_strong_links', 'mm_medium_links', 'mm_weak_links']:
            factors[k] = 0

    return factors

# === MAIN ===
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 factor_engine.py <serp_json> <target_url> [--limit N] [--no-infranodus] [--no-ai-overview] [--no-majestic]")
        sys.exit(1)

    serp_path = sys.argv[1]
    target_url = sys.argv[2]
    limit = 30
    use_infranodus = True
    use_ai_overview = True
    use_majestic = True

    for i, arg in enumerate(sys.argv[3:], 3):
        if arg == '--limit' and i+1 < len(sys.argv):
            limit = int(sys.argv[i+1])
        elif arg == '--no-infranodus':
            use_infranodus = False
        elif arg == '--no-ai-overview':
            use_ai_overview = False
        elif arg == '--no-majestic':
            use_majestic = False

    # Load credentials
    creds = load_env()
    if not use_infranodus:
        creds.pop('INFRANODUS_API_KEY', None)

    # Load SERP data
    with open(serp_path) as f:
        serp = json.load(f)

    print(f"{'='*70}")
    print(f"  ZENFUSION FACTOR ANALYSIS ENGINE v2.0")
    print(f"{'='*70}")
    print(f"  SERP results: {len(serp)}")
    print(f"  Target URL: {target_url}")
    print(f"  Limit: {limit} pages")
    print(f"  InfraNodus: {'ON' if use_infranodus else 'OFF'}")
    print(f"  AI Overview: {'ON' if use_ai_overview else 'OFF'}")
    print(f"  Majestic Million: {'ON' if use_majestic else 'OFF'}")

    # Determine keyword from SERP file name
    serp_name = os.path.basename(serp_path).replace('serp_', '').replace('.json', '').replace('_', ' ')
    keyword = serp_name
    print(f"  Keyword: {keyword}")
    # Fetch AI Overview (once for all pages)
    ai_overview = None
    if use_ai_overview and creds.get('DATAFORSEO_LOGIN'):
        print(f"\n  Fetching Google AI Overview...")
        ai_overview = fetch_ai_overview(keyword, creds)
        if ai_overview.get('has_ai_overview'):
            print(f"  AI Overview found! {ai_overview['citation_count']} citations, {len(ai_overview.get('products_mentioned',[]))} products")
        else:
            print(f"  No AI Overview found for this keyword")

    # Load Majestic Million index
    majestic_db = None
    if use_majestic:
        print(f"  Loading Majestic Million index...")
        majestic_db = load_majestic_index()
        if majestic_db:
            print(f"  Majestic Million loaded: {len(majestic_db):,} domains")
        else:
            print(f"  Majestic Million index not found — skipping Majestic factors")

    # Find the target's actual rank in the SERP
    target_domain = target_url.split('/')[2].replace('www.', '').lower() if '/' in target_url else ''
    target_rank = 0
    target_clean = target_url.split('?')[0].lower()
    for r in serp:
        serp_url = r.get('url', '').split('?')[0].lower()
        serp_domain = r.get('domain', '').lower().replace('www.', '')
        if target_clean == serp_url or target_domain in serp_domain:
            target_rank = r.get('rank_group', 0)
            break

    print(f"  Target URL: {target_url}")
    if target_rank:
        print(f"  Target rank in SERP: #{target_rank}")
    else:
        print(f"  Target rank: not found in top SERP")

    # Fetch and analyze pages
    print(f"\n  Fetching {limit+1} pages...")
    all_factors = []
    target_factors = None

    urls_to_fetch = [(r.get('rank_group', i+1), r.get('url', ''), False) for i, r in enumerate(serp[:limit])]
    # Use the target's actual SERP rank if found, otherwise 0
    urls_to_fetch.append((target_rank if target_rank else 0, target_url, True))  # Target page last

    for i, (rank, url, is_target) in enumerate(urls_to_fetch):
        label = f"TARGET" if is_target else f"#{rank}"
        short_url = url[:60] + '...' if len(url) > 60 else url
        print(f"  [{i+1}/{len(urls_to_fetch)}] {label} {short_url}...", end='', flush=True)

        try:
            html, load_time = fetch_page(url, timeout=30)
            factors = extract_factors(html, url, keyword, creds if use_infranodus or use_majestic else None,
                                      majestic_db if use_majestic else None, ai_overview)
            factors['load_time'] = round(load_time, 2)
            factors['rank'] = rank
            factors['url'] = url

            if is_target:
                target_factors = factors
                print(f" OK ({load_time:.1f}s, {factors['word_count']} words, {factors['infr_entity_count']} entities)")
            else:
                all_factors.append(factors)
                print(f" OK ({load_time:.1f}s, {factors['word_count']} words)")

        except Exception as e:
            print(f" FAILED ({str(e)[:50]})")

    if not target_factors:
        print("\n  Could not fetch target page. Aborting.")
        sys.exit(1)

    if len(all_factors) < 3:
        print(f"\n  Only {len(all_factors)} competitor pages fetched. Need at least 3 for analysis.")

    # Compute correlations
    factor_names = [k for k in target_factors.keys() if k not in ['rank', 'url', 'load_time']]
    ranks = [f['rank'] for f in all_factors]

    print(f"\n  Computing correlations for {len(factor_names)} factors across {len(all_factors)} competitors...")

    correlations = []
    for fname in factor_names:
        values = [f.get(fname, 0) for f in all_factors]
        # Skip factors with zero variance
        if len(set(values)) < 2:
            continue

        r_sp, p_sp = spearman_corr(values, ranks)
        r_pe, p_pe = pearson_corr(values, ranks)
        r_ke, p_ke = kendall_tau(values, ranks)

        # Effect size: difference between top 10 and bottom 10
        top10_vals = [f.get(fname, 0) for f in all_factors if f['rank'] <= 10]
        bottom_vals = [f.get(fname, 0) for f in all_factors if f['rank'] > 10]
        d = cohens_d(top10_vals, bottom_vals) if len(top10_vals) >= 2 and len(bottom_vals) >= 2 else 0

        target_val = target_factors.get(fname, 0)
        top10_median = median(top10_vals) if top10_vals else 0

        correlations.append({
            'factor': fname,
            'spearman': r_sp, 'spearman_p': p_sp,
            'pearson': r_pe, 'pearson_p': p_pe,
            'kendall': r_ke, 'kendall_p': p_ke,
            'effect_size': d,
            'top10_median': top10_median,
            'target_value': target_val,
            'gap': target_val - top10_median,
        })

    # Sort by absolute Spearman correlation
    correlations.sort(key=lambda x: abs(x['spearman']), reverse=True)

    # Significant correlations (|r| > 0.3, p < 0.1)
    significant = [c for c in correlations if abs(c['spearman']) > 0.3 and c['spearman_p'] < 0.1]

    # Generate report
    print(f"\n\n{'='*70}")
    print(f"  ZENFUSION FACTOR ANALYSIS REPORT v2.0")
    print(f"{'='*70}")
    print(f"  Keyword: {keyword}")
    print(f"  Target: {target_url}")
    print(f"  Current rank: #{target_factors.get('rank', '?')}")
    print(f"  Competitors analyzed: {len(all_factors)}")
    print(f"  Factors measured: {len(factor_names)}")
    print(f"  Significant correlations: {len(significant)}")

    # AI Overview section
    if ai_overview and ai_overview.get('has_ai_overview'):
        print(f"\n{'='*70}")
        print(f"  GOOGLE AI OVERVIEW ANALYSIS")
        print(f"{'='*70}")
        target_domain = target_url.split('/')[2].replace('www.', '') if '/' in target_url else ''
        cited_domains = ai_overview.get('cited_domains', [])
        products = ai_overview.get('products_mentioned', [])

        print(f"  AI Overview present: YES")
        print(f"  Citations: {ai_overview.get('citation_count', 0)}")
        print(f"  Cited domains: {len(cited_domains)}")
        print(f"  Products mentioned: {len(products)}")
        print(f"  Target domain cited: {'YES' if target_domain in [d.replace('www.','') for d in cited_domains] else 'NO'}")

        if products:
            print(f"\n  Products in AI Overview:")
            for p in products[:10]:
                print(f"    - {p[:60]}")

        if cited_domains:
            print(f"\n  Cited domains:")
            for d in sorted(set(cited_domains)):
                count = cited_domains.count(d)
                is_target = '*** YOUR DOMAIN ***' if target_domain in d else ''
                print(f"    {d} ({count}x) {is_target}")

    # InfraNodus section
    if use_infranodus and target_factors.get('infr_entity_count', 0) > 0:
        print(f"\n{'='*70}")
        print(f"  INFRANODUS ENTITY ANALYSIS (Target Page)")
        print(f"{'='*70}")
        print(f"  Entities: {target_factors.get('infr_entity_count', 0)}")
        print(f"  Relationships: {target_factors.get('infr_edge_count', 0)}")
        print(f"  Topical clusters: {target_factors.get('infr_cluster_count', 0)}")
        print(f"  Content gaps: {target_factors.get('infr_content_gap_count', 0)}")
        print(f"  Diversity score: {target_factors.get('infr_diversity_score', 0)}")

    # Majestic Million section
    if use_majestic and majestic_db and target_factors.get('mm_ref_domains_checked', 0) > 0:
        print(f"\n{'='*70}")
        print(f"  MAJESTIC MILLION ANALYSIS (Target Page)")
        print(f"{'='*70}")
        print(f"  Referring domains checked: {target_factors.get('mm_ref_domains_checked', 0)}")
        print(f"  In Majestic Million: {target_factors.get('mm_match_count', 0)} ({target_factors.get('mm_match_rate', 0):.1f}%)")
        print(f"  Top Majestic rank: #{target_factors.get('mm_top_rank', 0):,}")
        print(f"  Strong links (top 1000): {target_factors.get('mm_strong_links', 0)}")
        print(f"  Tier building targets: {target_factors.get('mm_tier_targets', 0)}")

    # Top correlations
    print(f"\n{'='*70}")
    print(f"  TOP CORRELATING FACTORS ({len(significant)} significant, |r| > 0.3)")
    print(f"{'='*70}")
    print(f"  {'Factor':<35} {'Spearman':>10} {'Pearson':>10} {'Kendall':>10} {'Effect':>8} {'Top10':>8} {'Yours':>8}")
    print(f"  {'-'*35} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*8} {'-'*8}")
    for c in correlations[:25]:
        sig = '*' if abs(c['spearman']) > 0.3 and c['spearman_p'] < 0.1 else ''
        print(f"  {c['factor']:<35} {c['spearman']:+.3f}{sig} {c['pearson']:+.3f}{'':>3} {c['kendall']:+.3f}{'':>3} {c['effect_size']:+.2f}{'':>4} {c['top10_median']:>8.1f} {c['target_value']:>8.1f}")

    # Factor road map
    print(f"\n{'='*70}")
    print(f"  FACTOR ROAD MAP (all factors, sorted by Spearman correlation)")
    print(f"{'='*70}")
    print(f"  {'Factor':<35} {'Corr':>8} {'Top10':>8} {'Yours':>8} {'Action':>30}")
    print(f"  {'-'*35} {'-'*8} {'-'*8} {'-'*8} {'-'*30}")
    for c in correlations[:40]:
        gap = c['gap']
        if abs(gap) < 0.5:
            action = '-'
        elif gap > 0 and c['spearman'] < 0:
            action = 'DECREASE'
        elif gap < 0 and c['spearman'] < 0:
            action = 'INCREASE'
        elif gap > 0 and c['spearman'] > 0:
            action = 'INCREASE'
        else:
            action = 'DECREASE'
        print(f"  {c['factor']:<35} {c['spearman']:+.3f} {c['top10_median']:>8.1f} {c['target_value']:>8.1f} {action:>30}")

    # Developer fix list
    print(f"\n{'='*70}")
    print(f"  DEVELOPER FIX LIST (theme/code changes)")
    print(f"{'='*70}\n")

    dev_fixes = []
    if target_factors.get('images_without_alt', 0) > 0:
        dev_fixes.append(f"  1. [MEDIUM] Add alt text to {target_factors['images_without_alt']} images missing alt attributes\n     Where: theme template image tags\n     Why: Competitors have 0 missing alt text")
    if target_factors.get('page_size_kb', 0) > 826:
        dev_fixes.append(f"  2. [HIGH] Reduce page size from {target_factors['page_size_kb']}KB to under 826KB\n     Where: Theme assets / Shopify CDN image compression\n     Why: Page is heavier than top 10 median")
    if target_factors.get('has_product_schema', 0) == 0 and target_factors.get('is_product_page', 0):
        dev_fixes.append(f"  3. [HIGH] Add Product schema (JSON-LD)\n     Where: theme.liquid product template\n     Why: Zero competitors have Product schema — opportunity for rich snippets")
    if target_factors.get('has_faqpage_schema', 0) == 0 and target_factors.get('has_faq_section', 0):
        dev_fixes.append(f"  4. [MEDIUM] Add FAQPage schema to FAQ sections\n     Where: theme.liquid FAQ section\n     Why: Enables FAQ rich results in SERPs")
    if target_factors.get('has_organization_schema', 0) == 0:
        dev_fixes.append(f"  5. [LOW] Add Organization schema\n     Where: theme.liquid head\n     Why: Builds entity trust with Google")

    if dev_fixes:
        for f in dev_fixes:
            print(f)
            print()
    else:
        print("  No developer fixes identified.\n")

    # SEO team fix list
    print(f"{'='*70}")
    print(f"  SEO TEAM FIX LIST (Shopify admin accessible)")
    print(f"{'='*70}\n")

    seo_fixes = []
    top10_internal = median([f.get('internal_links', 0) for f in all_factors if f['rank'] <= 10]) if all_factors else 0
    if target_factors.get('internal_links', 0) < top10_internal * 0.7:
        seo_fixes.append(f"  1. [HIGH] Add internal links (current: {target_factors.get('internal_links',0)}, top 10 median: {top10_internal:.0f})\n     Where: Shopify admin -> Content editor (link to collections/products)\n     Why: Internal links distribute page authority")

    top10_words = median([f.get('word_count', 0) for f in all_factors if f['rank'] <= 10]) if all_factors else 0
    if target_factors.get('word_count', 0) < top10_words * 0.8:
        seo_fixes.append(f"  2. [HIGH] Increase content length (current: {target_factors.get('word_count',0)}, top 10 median: {top10_words:.0f})\n     Where: Shopify admin -> Page description editor\n     Why: Top ranking pages have more content")

    if target_factors.get('exact_match_anchors', 0) > 3:
        seo_fixes.append(f"  3. [MEDIUM] Diversify {target_factors.get('exact_match_anchors',0)} exact-match anchor text links\n     Where: Content editor -> Edit internal link anchor text\n     Why: Over-optimization signal — competitors have 0-1 exact match")

    if target_factors.get('table_count', 0) == 0 and any(f.get('table_count', 0) > 0 for f in all_factors[:10]):
        seo_fixes.append(f"  4. [MEDIUM] Add comparison/spec tables\n     Where: Content editor (HTML mode)\n     Why: Top ranking pages have tables; SoftPro has 0")

    if target_factors.get('lsi_term_count', 0) < 15:
        seo_fixes.append(f"  5. [LOW] Add more LSI/related terms (current: {target_factors.get('lsi_term_count',0)})\n     Where: Content editor\n     Why: Improves topical relevance")

    if target_factors.get('infr_content_gap_count', 0) > 0 and use_infranodus:
        seo_fixes.append(f"  6. [HIGH] Bridge {target_factors.get('infr_content_gap_count',0)} InfraNodus content gaps\n     Where: Content editor -> Write bridging content between disconnected topics\n     Why: Structural holes in entity graph indicate missing topical coverage")

    if ai_overview and ai_overview.get('has_ai_overview'):
        target_domain = target_url.split('/')[2].replace('www.', '') if '/' in target_url else ''
        if target_domain not in [d.replace('www.','') for d in ai_overview.get('cited_domains', [])]:
            seo_fixes.append(f"  7. [HIGH] Publish citable content for Google AI Overview\n     Where: Blog posts / Pages\n     Why: Target domain is NOT cited as a source in AI Overview")

    if seo_fixes:
        for f in seo_fixes:
            print(f)
            print()
    else:
        print("  No SEO team fixes identified.\n")

    # Summary
    print(f"{'='*70}")
    print(f"  Report generated by Zenfusion Factor Engine v2.0")
    print(f"  Factors analyzed: {len(factor_names)}")
    print(f"  Significant correlations: {len(significant)}")
    print(f"  Developer fixes: {len(dev_fixes)}")
    print(f"  SEO team fixes: {len(seo_fixes)}")
    if ai_overview and ai_overview.get('has_ai_overview'):
        print(f"  AI Overview: {ai_overview.get('citation_count',0)} citations, {len(ai_overview.get('products_mentioned',[]))} products")
    if use_infranodus:
        print(f"  InfraNodus: {target_factors.get('infr_entity_count',0)} entities, {target_factors.get('infr_cluster_count',0)} clusters, {target_factors.get('infr_content_gap_count',0)} gaps")
    if use_majestic and majestic_db:
        print(f"  Majestic Million: {target_factors.get('mm_match_count',0)} matches ({target_factors.get('mm_match_rate',0):.1f}%)")
    print(f"{'='*70}\n")

if __name__ == '__main__':
    main()
