#!/usr/bin/env python3
"""
SERP Feature Check
====================
Standalone script that analyzes what SERP features appear for a page's
target keyword and how the target URL competes in each feature.

Fetches live SERP data from DataForSEO's Google Organic Live Advanced
endpoint and produces a comprehensive HTML report with:

  - SERP overview (keyword, total results, features present)
  - SERP feature cards (present/absent, target URL featured status)
  - Organic results table (position, URL, title, domain; target highlighted)
  - AI Overview content (scrollable box if present)
  - People Also Ask questions list
  - Featured snippet details (owner + content)
  - Competition analysis (who appears in features we don't)
  - Recommendations for winning each SERP feature

Usage:
  python3 serp_feature_check.py \\
      --keyword 'best water softener' \\
      --url https://www.example.com/page \\
      --output /tmp/serp_features

Environment:
  DATAFORSEO_LOGIN    - DataForSEO API login (from ~/.hermes/.env)
  DATAFORSEO_PASSWORD - DataForSEO API password (from ~/.hermes/.env)
"""

import os
import sys
import json
import base64
import argparse
import re
import html as html_mod
from datetime import datetime
from urllib.parse import urlparse

import urllib.request
import urllib.error

# === CONFIG ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 90

# All SERP feature types we recognize for feature detection.
# DataForSEO returns these as item.type values (and via the `checks` array).
SERP_FEATURE_TYPES = {
    'featured_snippet': 'Featured Snippet',
    'people_also_ask': 'People Also Ask',
    'people_also_search': 'People Also Search',
    'ai_overview': 'AI Overview',
    'local_pack': 'Local Pack',
    'map': 'Local Map',
    'video': 'Video Carousel',
    'videos': 'Video Carousel',
    'image': 'Image Results',
    'images': 'Image Results',
    'news': 'News',
    'top_stories': 'Top Stories',
    'shopping': 'Shopping',
    'twitter': 'Twitter / X',
    'questions': 'Questions',
    'knowledge_graph': 'Knowledge Graph',
    'carousel': 'Carousel',
    'recipe': 'Recipes',
    'jobs': 'Jobs',
    'flights': 'Flights',
    'hotels': 'Hotels',
    'pack': 'Pack',
    'events': 'Events',
    'discussions': 'Discussions',
    'recipes': 'Recipes',
    'podcasts': 'Podcasts',
    'scholarly_articles': 'Scholarly Articles',
    'study_listings': 'Study Listings',
    'top_sights': 'Top Sights',
    'things_to_know': 'Things to Know',
    'refine_reviews': 'Refine Reviews',
    'perspectives': 'Perspectives',
    'short_videos': 'Short Videos',
}

# Recommendations keyed by feature type. Each is a short actionable string.
FEATURE_RECOMMENDATIONS = {
    'ai_overview': (
        "Structure content for AI extraction: concise summaries at the top of the page, "
        "clear factual statements with numbers, FAQ schema, and authoritative citations. "
        "Aim to be cited as a source by providing unique data, comparisons, and expert quotes."
    ),
    'featured_snippet': (
        "Target the featured snippet by answering the query directly in a 40-60 word paragraph "
        "near the top of the page, or with a well-structured table/list. Use the keyword in a "
        "prominent H2 and provide a clear, factual answer. Add FAQ or HowTo schema."
    ),
    'people_also_ask': (
        "Win PAA by adding an FAQ section answering the exact PAA questions shown. Use FAQPage "
        "schema markup. Each answer should be 40-50 words and directly address the question."
    ),
    'local_pack': (
        "Win the Local Pack by claiming/optimizing your Google Business Profile, building local "
        "citations (NAP consistency), getting reviews with keywords, and creating location-specific "
        "landing pages if applicable."
    ),
    'video': (
        "Win the video carousel by publishing a relevant YouTube video with the keyword in the "
        "title/description, adding timestamps, and embedding it on the target page with VideoObject schema."
    ),
    'videos': (
        "Win the video carousel by publishing a relevant YouTube video with the keyword in the "
        "title/description, adding timestamps, and embedding it on the target page with VideoObject schema."
    ),
    'shopping': (
        "Win Shopping results by submitting a Google Merchant Center feed with the keyword in product "
        "titles, competitive pricing, high-quality images, and Product schema with offers on the page."
    ),
    'knowledge_graph': (
        "Win the Knowledge Graph by building entity authority: Wikipedia/Wikidata entries, consistent "
        "NAP, Organization schema, and authoritative mentions from trusted sources."
    ),
    'top_stories': (
        "Win Top Stories by publishing timely news content with the keyword, using NewsArticle schema, "
        "and getting indexed in Google News."
    ),
    'twitter': (
        "Win the Twitter/X module by posting relevant, high-engagement tweets with the keyword and "
        "building author authority on the platform."
    ),
    'image': (
        "Win image results by adding descriptive alt text with the keyword, high-quality images, "
        "descriptive filenames, and ImageObject schema."
    ),
    'images': (
        "Win image results by adding descriptive alt text with the keyword, high-quality images, "
        "descriptive filenames, and ImageObject schema."
    ),
    'carousel': (
        "Win the carousel by creating listicle-style content (e.g., 'Top 10...') with clear items, "
        "ItemList schema, and high-quality images for each entry."
    ),
    'things_to_know': (
        "Win Things to Know by structuring content around the sub-topics Google identifies, using "
        "clear H2/H3 headings that match those concepts, and providing comprehensive coverage."
    ),
    'perspectives': (
        "Win Perspectives by publishing first-person, opinion, or experiential content with clear "
        "authorship, author bios, and E-E-A-T signals."
    ),
    'short_videos': (
        "Win short video results by publishing TikTok/Reels/Shorts with the keyword in captions and "
        "on-screen text."
    ),
}

DEFAULT_RECOMMENDATION = (
    "Optimize content to target this SERP feature: ensure relevant schema markup, "
    "high-quality content matching search intent, and strong on-page SEO signals."
)


# ============================================================
# CREDENTIALS
# ============================================================
def get_creds():
    """Load DataForSEO credentials from environment or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        env_path = os.path.expanduser('~/.hermes/.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATAFORSEO_LOGIN='):
                        login = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.")
        print("Set them in ~/.hermes/.env or as environment variables.")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload=None, method='POST'):
    """Make a DataForSEO API call. Payload must be a list (array of task objects)."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'{API_BASE}/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    if method == 'POST' and payload is not None:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers
        )
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            print(f"  API Error: {data.get('status_code')} - {data.get('status_message')}")
            return None
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f"  HTTP Error {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  Request error: {e}")
        return None


def _extract_first_result(data):
    """Extract the first result object from a DataForSEO response."""
    if not data or not data.get('tasks'):
        return None
    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'unknown')}")
    results = task.get('result', [])
    if not results:
        return None
    return results[0] if isinstance(results, list) else results


# ============================================================
# URL / DOMAIN HELPERS
# ============================================================
def normalize_url(url):
    """Normalize a URL for comparison (strip trailing slash, lowercase host)."""
    if not url:
        return ''
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    path = parsed.path.rstrip('/')
    return f'{host}{path}'


def normalize_domain(url):
    """Extract the root domain (without www) from a URL."""
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host


def url_matches(target_url, candidate_url):
    """Check if two URLs refer to the same page (normalized comparison)."""
    if not target_url or not candidate_url:
        return False
    return normalize_url(target_url) == normalize_url(candidate_url)


def domain_matches(target_url, candidate_url):
    """Check if two URLs share the same root domain."""
    if not target_url or not candidate_url:
        return False
    return normalize_domain(target_url) == normalize_domain(candidate_url)


# ============================================================
# 1. FETCH SERP FEATURES
# ============================================================
def fetch_serp_features(keyword, url, location_code=2840, language_code='en'):
    """
    Call DataForSEO SERP organic live advanced and return all SERP features present.

    Returns a dict with:
        keyword, target_url, target_domain, result_count,
        items (raw SERP items), organic_results, features (list of dicts),
        ai_overview, people_also_ask, featured_snippet, local_pack, video_carousel,
        shopping, knowledge_graph
    """
    print(f"  → Fetching SERP for keyword '{keyword}' (location {location_code})...")

    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'device': 'desktop',
        'os': 'windows',
        'depth': 100,
        'use_improved_etv': True,
        'parse_aiserp': True,
    }]

    data = dfs_api_call('serp/google/organic/live/advanced', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print(f"  ✗ No SERP data returned for '{keyword}'")
        return None

    items = result.get('items', [])
    result_count = result.get('result_count', 0)
    target_domain = normalize_domain(url)

    organic_results = []
    features = []
    ai_overview = None
    people_also_ask = []
    featured_snippet = None
    local_pack = None
    video_carousel = None
    shopping = None
    knowledge_graph = None
    things_to_know = None
    perspectives = None

    for item in items:
        item_type = item.get('type', 'unknown')
        rank_absolute = item.get('rank_absolute', 0)
        rank_group = item.get('rank_group', 0)
        item_checks = item.get('checks') or []

        # --- Organic results ---
        if item_type == 'organic':
            organic_results.append({
                'position': rank_absolute,
                'rank_group': rank_group,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'title': item.get('title', ''),
                'description': item.get('description', ''),
                'is_featured_snippet': 'is_featured_snippet' in item_checks,
            })

        # --- AI Overview ---
        elif item_type == 'ai_overview':
            markdown = item.get('markdown', '')
            citations = re.findall(r'\[\[(\d+)\]\]\(([^)]+)\)', markdown)
            cited_urls = [u for _, u in citations]
            cited_domains = list(set(
                normalize_domain(u) for u in cited_urls if u
            ))
            # Also check items list inside ai_overview for organic citations
            ai_items = item.get('items', [])
            ai_cited_urls_from_items = []
            for ai_it in ai_items:
                if ai_it.get('type') == 'ai_overview_organic':
                    u = ai_it.get('url', '')
                    if u:
                        ai_cited_urls_from_items.append(u)
            all_cited_urls = cited_urls + ai_cited_urls_from_items
            all_cited_domains = list(set(
                normalize_domain(u) for u in all_cited_urls if u
            ))
            clean_text = re.sub(r'\[.*?\]\(.*?\)', '', markdown)
            clean_text = re.sub(r'[*#\[\]]', '', clean_text)
            ai_overview = {
                'markdown': markdown[:20000],
                'text': clean_text[:8000],
                'cited_urls': all_cited_urls,
                'cited_domains': all_cited_domains,
                'citation_count': len(all_cited_urls),
                'position': rank_absolute,
                'target_in_ai_overview': target_domain in all_cited_domains,
            }

        # --- People Also Ask ---
        elif item_type == 'people_also_ask':
            paa_items = item.get('items', [])
            for paa in paa_items:
                people_also_ask.append({
                    'question': paa.get('question', paa.get('title', '')),
                    'answer': paa.get('answer', paa.get('description', ''))[:500],
                    'url': paa.get('url', ''),
                    'domain': paa.get('domain', ''),
                    'title': paa.get('title', ''),
                })

        # --- Featured Snippet ---
        elif item_type == 'featured_snippet':
            featured_snippet = {
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'title': item.get('title', ''),
                'description': item.get('description', ''),
                'snippet_type': item.get('type', 'featured_snippet'),
                'position': rank_absolute,
                'is_target': domain_matches(url, item.get('url', '')),
            }
            features.append({
                'type': 'featured_snippet',
                'label': SERP_FEATURE_TYPES.get('featured_snippet', 'Featured Snippet'),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'is_target': domain_matches(url, item.get('url', '')),
            })

        # --- Local Pack ---
        elif item_type in ('local_pack', 'map'):
            local_items = item.get('items', [])
            local_pack = {
                'position': rank_absolute,
                'count': len(local_items),
                'items': [
                    {
                        'title': li.get('title', ''),
                        'domain': li.get('domain', ''),
                        'url': li.get('url', ''),
                        'rating': li.get('rating', ''),
                        'address': li.get('address', ''),
                    }
                    for li in local_items
                ],
                'target_in_local': any(
                    domain_matches(url, li.get('url', '') or li.get('domain', ''))
                    for li in local_items
                ),
            }
            features.append({
                'type': item_type,
                'label': SERP_FEATURE_TYPES.get(item_type, item_type),
                'position': rank_absolute,
                'domain': '',
                'url': '',
                'is_target': local_pack['target_in_local'],
            })

        # --- Video Carousel ---
        elif item_type in ('video', 'videos'):
            video_items = item.get('items', [])
            video_carousel = {
                'position': rank_absolute,
                'count': len(video_items),
                'items': [
                    {
                        'title': vi.get('title', ''),
                        'url': vi.get('url', ''),
                        'source': vi.get('source', vi.get('domain', '')),
                        'video_id': vi.get('video_id', ''),
                    }
                    for vi in video_items
                ],
                'target_in_video': any(
                    domain_matches(url, vi.get('url', '') or vi.get('source', ''))
                    for vi in video_items
                ),
            }
            features.append({
                'type': item_type,
                'label': SERP_FEATURE_TYPES.get(item_type, 'Video Carousel'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
                'is_target': video_carousel['target_in_video'],
            })

        # --- Shopping ---
        elif item_type == 'shopping':
            shop_items = item.get('items', [])
            shopping = {
                'position': rank_absolute,
                'count': len(shop_items),
                'items': [
                    {
                        'title': si.get('title', ''),
                        'url': si.get('url', ''),
                        'source': si.get('source', si.get('domain', '')),
                        'price': si.get('price', ''),
                    }
                    for si in shop_items
                ],
                'target_in_shopping': any(
                    domain_matches(url, si.get('url', '') or si.get('source', ''))
                    for si in shop_items
                ),
            }
            features.append({
                'type': 'shopping',
                'label': SERP_FEATURE_TYPES.get('shopping', 'Shopping'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
                'is_target': shopping['target_in_shopping'],
            })

        # --- Knowledge Graph ---
        elif item_type == 'knowledge_graph':
            knowledge_graph = {
                'position': rank_absolute,
                'title': item.get('title', ''),
                'description': item.get('description', '')[:1000],
                'url': item.get('url', ''),
                'domain': item.get('domain', ''),
            }
            features.append({
                'type': 'knowledge_graph',
                'label': SERP_FEATURE_TYPES.get('knowledge_graph', 'Knowledge Graph'),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'is_target': domain_matches(url, item.get('url', '')),
            })

        # --- Things to Know ---
        elif item_type == 'things_to_know':
            ttk_items = item.get('items', [])
            things_to_know = {
                'position': rank_absolute,
                'items': [
                    {
                        'title': ti.get('title', ''),
                        'description': ti.get('description', '')[:300],
                    }
                    for ti in ttk_items
                ],
            }
            features.append({
                'type': 'things_to_know',
                'label': SERP_FEATURE_TYPES.get('things_to_know', 'Things to Know'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
                'is_target': False,
            })

        # --- Perspectives ---
        elif item_type == 'perspectives':
            persp_items = item.get('items', [])
            perspectives = {
                'position': rank_absolute,
                'items': [
                    {
                        'title': pi.get('title', ''),
                        'source': pi.get('source', ''),
                        'url': pi.get('url', ''),
                        'date': pi.get('date', ''),
                    }
                    for pi in persp_items
                ],
            }
            features.append({
                'type': 'perspectives',
                'label': SERP_FEATURE_TYPES.get('perspectives', 'Perspectives'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
                'is_target': any(
                    domain_matches(url, pi.get('url', '') or pi.get('source', ''))
                    for pi in persp_items
                ),
            })

        # --- Other feature types ---
        elif item_type in SERP_FEATURE_TYPES and item_type != 'organic':
            # Check if the item or its sub-items reference the target URL
            target_in_feature = False
            sub_urls = [item.get('url', '')]
            for sub in item.get('items', []):
                if isinstance(sub, dict):
                    sub_urls.append(sub.get('url', ''))
                    sub_urls.append(sub.get('source', ''))
            for su in sub_urls:
                if su and domain_matches(url, su):
                    target_in_feature = True
                    break
            features.append({
                'type': item_type,
                'label': SERP_FEATURE_TYPES.get(item_type, item_type.replace('_', ' ').title()),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'is_target': target_in_feature,
            })

    # If AI Overview was found, add it to features list
    if ai_overview:
        features.insert(0, {
            'type': 'ai_overview',
            'label': 'AI Overview',
            'position': ai_overview['position'],
            'domain': '',
            'url': '',
            'is_target': ai_overview['target_in_ai_overview'],
            'extra': f"{ai_overview['citation_count']} citations",
        })

    # Add PAA to features
    if people_also_ask:
        features.append({
            'type': 'people_also_ask',
            'label': 'People Also Ask',
            'position': 0,
            'domain': '',
            'url': '',
            'is_target': any(
                domain_matches(url, p.get('url', '') or p.get('domain', ''))
                for p in people_also_ask
            ),
            'extra': f"{len(people_also_ask)} questions",
        })

    # Find target in organic results
    target_organic_position = None
    for org in organic_results:
        if domain_matches(url, org['url']) or url_matches(url, org['url']):
            org['is_target'] = True
            if target_organic_position is None:
                target_organic_position = org['position']
        else:
            org['is_target'] = False

    # Build the full feature list (deduplicated by type)
    seen_types = set()
    unique_features = []
    for f in features:
        if f['type'] not in seen_types:
            seen_types.add(f['type'])
            unique_features.append(f)

    serp_data = {
        'keyword': keyword,
        'target_url': url,
        'target_domain': target_domain,
        'location_code': location_code,
        'result_count': result_count,
        'items_count': len(items),
        'organic_results': organic_results[:30],
        'target_organic_position': target_organic_position,
        'features': unique_features,
        'ai_overview': ai_overview,
        'people_also_ask': people_also_ask,
        'featured_snippet': featured_snippet,
        'local_pack': local_pack,
        'video_carousel': video_carousel,
        'shopping': shopping,
        'knowledge_graph': knowledge_graph,
        'things_to_know': things_to_know,
        'perspectives': perspectives,
        'se_types_count': result.get('se_types_count', {}),
        'spell': result.get('spell', ''),
        'se_domain': result.get('se_domain', ''),
        'fetched_at': datetime.now().isoformat(),
    }

    feat_names = ', '.join(f['label'] for f in unique_features) or 'none'
    print(f"  ✓ SERP: {result_count:,} results | {len(organic_results)} organic | "
          f"Features: {feat_names}")
    if target_organic_position:
        print(f"  ✓ Target found at organic position #{target_organic_position}")
    else:
        print(f"  ✗ Target URL not found in organic results")

    return serp_data


# ============================================================
# 2. ANALYZE FEATURE COMPETITION
# ============================================================
def analyze_feature_competition(serp_data, target_url):
    """
    For each SERP feature, check if the target URL appears in it.

    Returns a dict mapping feature type -> {
        'label', 'present': bool, 'target_featured': bool,
        'competitors': [list of domains in the feature],
        'recommendation': str
    }
    """
    target_domain = normalize_domain(target_url)
    analysis = {}

    features = serp_data.get('features', [])

    # Build a set of all feature types present
    present_types = {f['type'] for f in features}

    # For each known feature type, determine presence and target status
    for feat_type, label in SERP_FEATURE_TYPES.items():
        present = feat_type in present_types
        target_featured = False
        competitors = []
        recommendation = ''

        if present:
            # Find matching feature entries
            matching = [f for f in features if f['type'] == feat_type]
            for m in matching:
                if m.get('is_target'):
                    target_featured = True
                if m.get('domain'):
                    competitors.append(m['domain'])

            # Deep-dive into specific feature data for competitor extraction
            if feat_type == 'ai_overview' and serp_data.get('ai_overview'):
                competitors = serp_data['ai_overview'].get('cited_domains', [])
                target_featured = serp_data['ai_overview'].get(
                    'target_in_ai_overview', False
                )
            elif feat_type == 'people_also_ask' and serp_data.get('people_also_ask'):
                for paa in serp_data['people_also_ask']:
                    d = paa.get('domain', '')
                    if d:
                        competitors.append(d)
                target_featured = any(
                    domain_matches(target_url, paa.get('url', '') or paa.get('domain', ''))
                    for paa in serp_data['people_also_ask']
                )
            elif feat_type == 'featured_snippet' and serp_data.get('featured_snippet'):
                fs = serp_data['featured_snippet']
                competitors = [fs.get('domain', '')] if fs.get('domain') else []
                target_featured = fs.get('is_target', False)
            elif feat_type in ('local_pack', 'map') and serp_data.get('local_pack'):
                lp = serp_data['local_pack']
                competitors = [li.get('domain', '') for li in lp.get('items', []) if li.get('domain')]
                target_featured = lp.get('target_in_local', False)
            elif feat_type in ('video', 'videos') and serp_data.get('video_carousel'):
                vc = serp_data['video_carousel']
                competitors = [vi.get('source', '') for vi in vc.get('items', []) if vi.get('source')]
                target_featured = vc.get('target_in_video', False)
            elif feat_type == 'shopping' and serp_data.get('shopping'):
                sh = serp_data['shopping']
                competitors = [si.get('source', '') for si in sh.get('items', []) if si.get('source')]
                target_featured = sh.get('target_in_shopping', False)
            elif feat_type == 'knowledge_graph' and serp_data.get('knowledge_graph'):
                kg = serp_data['knowledge_graph']
                competitors = [kg.get('domain', '')] if kg.get('domain') else []
                target_featured = domain_matches(target_url, kg.get('url', ''))

            # Remove target domain from competitors list
            competitors = [c for c in competitors if c and normalize_domain(c) != target_domain]
            # Dedupe
            seen = set()
            unique_comps = []
            for c in competitors:
                cd = normalize_domain(c) if c.startswith('http') else c.lower()
                if cd not in seen:
                    seen.add(cd)
                    unique_comps.append(c)
            competitors = unique_comps

        recommendation = FEATURE_RECOMMENDATIONS.get(feat_type, DEFAULT_RECOMMENDATION)
        if not present:
            recommendation = ''

        analysis[feat_type] = {
            'label': label,
            'present': present,
            'target_featured': target_featured,
            'competitors': competitors,
            'recommendation': recommendation,
        }

    # Add Things to Know, Perspectives if present
    for extra_type in ('things_to_know', 'perspectives', 'short_videos'):
        if extra_type in present_types and extra_type not in analysis:
            matching = [f for f in features if f['type'] == extra_type]
            analysis[extra_type] = {
                'label': SERP_FEATURE_TYPES.get(extra_type, extra_type),
                'present': True,
                'target_featured': any(m.get('is_target') for m in matching),
                'competitors': [],
                'recommendation': FEATURE_RECOMMENDATIONS.get(
                    extra_type, DEFAULT_RECOMMENDATION
                ),
            }

    return analysis


# ============================================================
# 3. HTML REPORT GENERATION
# ============================================================
def _esc(text):
    """HTML-escape text."""
    if text is None:
        return ''
    return html_mod.escape(str(text))


def _format_count(n):
    """Format a number with thousands separators."""
    try:
        return f'{int(n):,}'
    except (ValueError, TypeError):
        return str(n)


def _feature_status_badge(analysis_entry):
    """Return HTML for a feature status badge."""
    if not analysis_entry['present']:
        return '<span class="badge badge-absent">Absent</span>'
    if analysis_entry['target_featured']:
        return '<span class="badge badge-won">Won ✓</span>'
    return '<span class="badge badge-present">Present — Not Featured</span>'


def generate_serp_feature_report(keyword, url, serp_data, output_path):
    """
    Generate an HTML report showing SERP features and competition analysis.

    Writes the report to `<output_path>.html` (or `<output_path>/report.html`
    if output_path is a directory) and returns the file path.
    """
    if serp_data is None:
        raise ValueError("serp_data is None — cannot generate report")

    target_domain = normalize_domain(url)
    competition = analyze_feature_competition(serp_data, url)

    # Determine output file path
    if output_path.endswith('.html'):
        report_file = output_path
    elif os.path.isdir(output_path) or output_path.endswith('/'):
        os.makedirs(output_path, exist_ok=True)
        report_file = os.path.join(output_path, 'serp_feature_report.html')
    else:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        report_file = output_path + '.html'

    # --- Build feature cards ---
    feature_cards_html = []
    present_features = []
    absent_features = []

    for feat_type, label in SERP_FEATURE_TYPES.items():
        entry = competition.get(feat_type, {
            'label': label,
            'present': False,
            'target_featured': False,
            'competitors': [],
            'recommendation': '',
        })
        if not entry['present']:
            absent_features.append((feat_type, entry))
            continue
        present_features.append((feat_type, entry))

    # Present features first
    for feat_type, entry in present_features:
        competitors_html = ''
        if entry['competitors']:
            comps = ', '.join(f'<span class="comp-domain">{_esc(d)}</span>' for d in entry['competitors'][:10])
            competitors_html = f'<div class="comp-list"><strong>Competitors in this feature:</strong> {comps}</div>'

        rec_html = ''
        if entry['recommendation']:
            rec_html = f'<div class="recommendation"><strong>How to win:</strong> {_esc(entry["recommendation"])}</div>'

        status = _feature_status_badge(entry)
        card = f"""
        <div class="feature-card {'won' if entry['target_featured'] else ''}">
            <div class="feature-card-header">
                <span class="feature-name">{_esc(entry['label'])}</span>
                {status}
            </div>
            {competitors_html}
            {rec_html}
        </div>"""
        feature_cards_html.append(card)

    # Absent features as a compact summary
    if absent_features:
        absent_names = ', '.join(_esc(e['label']) for _, e in absent_features)
        feature_cards_html.append(
            f'<div class="absent-summary"><strong>Absent features:</strong> {absent_names}</div>'
        )

    # --- Organic results table ---
    organic_rows = []
    for org in serp_data.get('organic_results', []):
        is_target = org.get('is_target', False)
        row_class = 'target-row' if is_target else ''
        title = org.get('title', '')[:120]
        org_url = org.get('url', '')
        domain = org.get('domain', '')
        pos = org.get('position', '')
        snippet_flag = '⭐' if org.get('is_featured_snippet') else ''
        organic_rows.append(
            f"""<tr class="{row_class}">
                <td class="pos">{pos}</td>
                <td><a href="{_esc(org_url)}" target="_blank" rel="noopener nofollow">{_esc(title)}</a>{(' ' + snippet_flag) if snippet_flag else ''}</td>
                <td class="domain">{_esc(domain)}</td>
                <td class="url-cell"><a href="{_esc(org_url)}" target="_blank" rel="noopener nofollow">{_esc(org_url)}</a></td>
            </tr>"""
        )
    organic_table_html = '\n'.join(organic_rows) if organic_rows else '<tr><td colspan="4">No organic results found.</td></tr>'

    # --- AI Overview section ---
    ai_overview_html = ''
    if serp_data.get('ai_overview'):
        ai = serp_data['ai_overview']
        cited_domains_html = ''
        if ai.get('cited_domains'):
            domains_list = ', '.join(f'<span class="cited-domain">{_esc(d)}</span>' for d in ai['cited_domains'][:20])
            cited_domains_html = f'<div class="cited-domains"><strong>Cited domains ({ai["citation_count"]}):</strong> {domains_list}</div>'
        target_badge = ''
        if ai.get('target_in_ai_overview'):
            target_badge = '<span class="badge badge-won">Target Cited ✓</span>'
        else:
            target_badge = '<span class="badge badge-missed">Target Not Cited</span>'
        ai_text = _esc(ai.get('text', '')[:8000])
        ai_overview_html = f"""
        <div class="section">
            <h2>AI Overview {target_badge}</h2>
            <div class="ai-overview-box">{ai_text}</div>
            {cited_domains_html}
            <div class="recommendation"><strong>How to win AI Overview:</strong> {_esc(FEATURE_RECOMMENDATIONS['ai_overview'])}</div>
        </div>"""
    else:
        ai_overview_html = """
        <div class="section">
            <h2>AI Overview</h2>
            <p class="muted">No AI Overview present for this keyword.</p>
        </div>"""

    # --- People Also Ask section ---
    paa_html = ''
    if serp_data.get('people_also_ask'):
        paa_items = serp_data['people_also_ask']
        paa_list = []
        for i, paa in enumerate(paa_items, 1):
            question = _esc(paa.get('question', paa.get('title', '')))
            answer = _esc(paa.get('answer', '')[:500])
            domain = _esc(paa.get('domain', ''))
            url_val = _esc(paa.get('url', ''))
            paa_list.append(f"""
            <div class="paa-item">
                <div class="paa-question"><strong>{i}. {question}</strong></div>
                <div class="paa-answer">{answer}</div>
                <div class="paa-source">Source: <a href="{url_val}" target="_blank" rel="noopener nofollow">{domain or url_val}</a></div>
            </div>""")
        paa_html = f"""
        <div class="section">
            <h2>People Also Ask ({len(paa_items)} questions)</h2>
            {''.join(paa_list)}
            <div class="recommendation"><strong>How to win PAA:</strong> {_esc(FEATURE_RECOMMENDATIONS['people_also_ask'])}</div>
        </div>"""
    else:
        paa_html = """
        <div class="section">
            <h2>People Also Ask</h2>
            <p class="muted">No People Also Ask box present for this keyword.</p>
        </div>"""

    # --- Featured Snippet section ---
    fs_html = ''
    if serp_data.get('featured_snippet'):
        fs = serp_data['featured_snippet']
        owner = _esc(fs.get('domain', ''))
        fs_title = _esc(fs.get('title', ''))
        fs_desc = _esc(fs.get('description', '')[:1000])
        fs_url = _esc(fs.get('url', ''))
        owner_badge = ''
        if fs.get('is_target'):
            owner_badge = '<span class="badge badge-won">We Own This ✓</span>'
        else:
            owner_badge = f'<span class="badge badge-missed">Owned by {_esc(owner)}</span>'
        fs_html = f"""
        <div class="section">
            <h2>Featured Snippet {owner_badge}</h2>
            <div class="featured-snippet-box">
                <div class="fs-title">{fs_title}</div>
                <div class="fs-desc">{fs_desc}</div>
                <div class="fs-source">Source: <a href="{fs_url}" target="_blank" rel="noopener nofollow">{_esc(fs.get('url', ''))}</a></div>
            </div>
            <div class="recommendation"><strong>How to win featured snippet:</strong> {_esc(FEATURE_RECOMMENDATIONS['featured_snippet'])}</div>
        </div>"""
    else:
        fs_html = """
        <div class="section">
            <h2>Featured Snippet</h2>
            <p class="muted">No featured snippet present for this keyword.</p>
        </div>"""

    # --- Competition analysis ---
    comp_rows = []
    for feat_type, entry in present_features:
        label = entry['label']
        status = _feature_status_badge(entry)
        competitors = ', '.join(_esc(d) for d in entry['competitors'][:8]) or '—'
        comp_rows.append(f"""
            <tr>
                <td>{_esc(label)}</td>
                <td>{status}</td>
                <td class="comp-cell">{competitors}</td>
            </tr>""")
    competition_html = f"""
    <div class="section">
        <h2>Competition Analysis</h2>
        <p class="muted">Features where competitors appear but the target URL does not.</p>
        <table class="comp-table">
            <thead>
                <tr><th>SERP Feature</th><th>Status</th><th>Competitors Present</th></tr>
            </thead>
            <tbody>
                {''.join(comp_rows) if comp_rows else '<tr><td colspan="3">No features present.</td></tr>'}
            </tbody>
        </table>
    </div>"""

    # --- Recommendations section ---
    rec_items = []
    for feat_type, entry in present_features:
        if not entry['target_featured'] and entry['recommendation']:
            rec_items.append(f"""
            <div class="rec-item">
                <strong>{_esc(entry['label'])}</strong>
                <p>{_esc(entry['recommendation'])}</p>
            </div>""")
    recommendations_html = f"""
    <div class="section">
        <h2>Recommendations</h2>
        {''.join(rec_items) if rec_items else '<p class="muted">Target URL is featured in all present SERP features. No additional recommendations.</p>'}
    </div>"""

    # --- SERP Overview cards ---
    features_present = len(present_features)
    features_won = sum(1 for _, e in present_features if e['target_featured'])
    features_missed = features_present - features_won
    target_pos = serp_data.get('target_organic_position', 'Not found')

    overview_html = f"""
    <div class="overview-grid">
        <div class="summary-card neutral">
            <div class="card-label">Keyword</div>
            <div class="card-value">{_esc(keyword)}</div>
        </div>
        <div class="summary-card neutral">
            <div class="card-label">Target Domain</div>
            <div class="card-value">{_esc(target_domain)}</div>
        </div>
        <div class="summary-card {'green-bg' if features_won > 0 else 'neutral'}">
            <div class="card-label">Features Won</div>
            <div class="card-value">{features_won}</div>
        </div>
        <div class="summary-card {'orange-bg' if features_missed > 0 else 'neutral'}">
            <div class="card-label">Features Missed</div>
            <div class="card-value">{features_missed}</div>
        </div>
        <div class="summary-card neutral">
            <div class="card-label">Total SERP Features</div>
            <div class="card-value">{features_present}</div>
        </div>
        <div class="summary-card neutral">
            <div class="card-label">Total Results</div>
            <div class="card-value">{_format_count(serp_data.get('result_count', 0))}</div>
        </div>
        <div class="summary-card {'green-bg' if target_pos and target_pos != 'Not found' and target_pos <= 10 else 'neutral'}">
            <div class="card-label">Target Organic Position</div>
            <div class="card-value">{target_pos if target_pos else 'Not found'}</div>
        </div>
        <div class="summary-card neutral">
            <div class="card-label">SE Domain</div>
            <div class="card-value">{_esc(serp_data.get('se_domain', 'google.com'))}</div>
        </div>
    </div>"""

    # --- Full HTML ---
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fetched_at = _esc(serp_data.get('fetched_at', now_str))

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>SERP Feature Analysis — {_esc(keyword)}</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        background: #ffffff;
        color: #1a1a1a;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        font-size: 16px;
        line-height: 1.6;
        padding: 24px;
    }}
    h1 {{
        color: #0066cc;
        font-size: 28px;
        margin-bottom: 4px;
    }}
    h2 {{
        color: #1a1a1a;
        font-size: 22px;
        margin-bottom: 12px;
        padding-bottom: 8px;
        border-bottom: 2px solid #e0e0e0;
    }}
    .subtitle {{
        color: #666;
        font-size: 15px;
        margin-bottom: 24px;
    }}
    .section {{
        background: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        padding: 24px;
        margin-bottom: 20px;
    }}
    .overview-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 16px;
        margin-bottom: 24px;
    }}
    .summary-card {{
        padding: 16px;
        border-radius: 10px;
        border: 1px solid #e0e0e0;
        text-align: center;
    }}
    .summary-card .card-label {{
        font-size: 13px;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 4px;
    }}
    .summary-card .card-value {{
        font-size: 24px;
        font-weight: 700;
        color: #1a1a1a;
        word-break: break-word;
    }}
    .green-bg {{ background: #27ae60; border-color: #27ae60; }}
    .green-bg .card-label, .green-bg .card-value {{ color: #ffffff; }}
    .orange-bg {{ background: #f39c12; border-color: #f39c12; }}
    .orange-bg .card-label, .orange-bg .card-value {{ color: #ffffff; }}
    .neutral {{ background: #f8f9fa; }}
    .feature-card {{
        background: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 12px;
    }}
    .feature-card.won {{
        border-color: #27ae60;
        border-left: 4px solid #27ae60;
    }}
    .feature-card-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;
    }}
    .feature-name {{
        font-weight: 600;
        font-size: 17px;
        color: #1a1a1a;
    }}
    .badge {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 600;
    }}
    .badge-won {{ background: #27ae60; color: #fff; }}
    .badge-present {{ background: #f39c12; color: #fff; }}
    .badge-absent {{ background: #e0e0e0; color: #666; }}
    .badge-missed {{ background: #e74c3c; color: #fff; }}
    .comp-list {{
        font-size: 14px;
        color: #444;
        margin-top: 6px;
    }}
    .comp-domain {{
        display: inline-block;
        background: #f0f0f0;
        padding: 2px 8px;
        border-radius: 4px;
        margin: 2px 2px;
        font-size: 13px;
    }}
    .recommendation {{
        margin-top: 8px;
        padding: 10px 14px;
        background: #f0f7ff;
        border-left: 3px solid #0066cc;
        border-radius: 4px;
        font-size: 15px;
        color: #333;
    }}
    .absent-summary {{
        padding: 12px 16px;
        background: #f8f9fa;
        border-radius: 6px;
        font-size: 14px;
        color: #666;
        margin-top: 8px;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 15px;
    }}
    th, td {{
        text-align: left;
        padding: 10px 12px;
        border-bottom: 1px solid #e0e0e0;
        color: #333;
    }}
    th {{
        background: #f8f9fa;
        font-weight: 600;
        color: #1a1a1a;
    }}
    tr.target-row {{
        background: #e8f5e9;
    }}
    tr.target-row td {{
        font-weight: 600;
    }}
    td.pos {{
        text-align: center;
        font-weight: 700;
        width: 50px;
    }}
    td.domain {{
        color: #666;
        font-size: 14px;
        width: 200px;
    }}
    td.url-cell {{
        max-width: 300px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }}
    td.url-cell a, td a {{
        color: #0066cc;
        text-decoration: none;
    }}
    td.url-cell a:hover, td a:hover {{
        text-decoration: underline;
    }}
    .ai-overview-box {{
        max-height: 400px;
        overflow-y: auto;
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-radius: 6px;
        padding: 16px;
        font-size: 15px;
        line-height: 1.7;
        color: #333;
        white-space: pre-wrap;
        margin-bottom: 12px;
    }}
    .cited-domains {{
        margin-top: 10px;
        font-size: 14px;
        color: #444;
    }}
    .cited-domain {{
        display: inline-block;
        background: #e3f2fd;
        color: #0066cc;
        padding: 2px 8px;
        border-radius: 4px;
        margin: 2px;
        font-size: 13px;
    }}
    .paa-item {{
        padding: 12px 0;
        border-bottom: 1px solid #e0e0e0;
    }}
    .paa-item:last-child {{ border-bottom: none; }}
    .paa-question {{
        font-size: 16px;
        color: #1a1a1a;
        margin-bottom: 4px;
    }}
    .paa-answer {{
        font-size: 15px;
        color: #555;
        margin-bottom: 4px;
    }}
    .paa-source {{
        font-size: 13px;
        color: #888;
    }}
    .paa-source a {{ color: #0066cc; text-decoration: none; }}
    .featured-snippet-box {{
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-radius: 6px;
        padding: 16px;
        margin-bottom: 12px;
    }}
    .fs-title {{
        font-size: 17px;
        font-weight: 600;
        margin-bottom: 6px;
    }}
    .fs-desc {{
        font-size: 15px;
        color: #555;
        margin-bottom: 6px;
    }}
    .fs-source {{ font-size: 13px; color: #888; }}
    .fs-source a {{ color: #0066cc; text-decoration: none; }}
    .comp-table td.comp-cell {{
        font-size: 14px;
        color: #555;
    }}
    .rec-item {{
        padding: 12px 0;
        border-bottom: 1px solid #e0e0e0;
    }}
    .rec-item:last-child {{ border-bottom: none; }}
    .rec-item strong {{
        color: #0066cc;
        font-size: 16px;
    }}
    .rec-item p {{
        color: #444;
        margin-top: 4px;
    }}
    .muted {{ color: #888; font-size: 15px; }}
    .footer {{
        text-align: center;
        padding: 20px;
        color: #aaa;
        font-size: 13px;
    }}
</style>
</head>
<body>
    <h1>SERP Feature Analysis</h1>
    <p class="subtitle">Keyword: <strong>{_esc(keyword)}</strong> &nbsp;|&nbsp; URL: <strong>{_esc(url)}</strong> &nbsp;|&nbsp; Fetched: {fetched_at}</p>

    {overview_html}

    <div class="section">
        <h2>SERP Feature Cards</h2>
        {''.join(feature_cards_html)}
    </div>

    <div class="section">
        <h2>Organic Results</h2>
        <table>
            <thead>
                <tr><th style="width:50px;">Pos</th><th>Title</th><th style="width:200px;">Domain</th><th>URL</th></tr>
            </thead>
            <tbody>
                {organic_table_html}
            </tbody>
        </table>
    </div>

    {ai_overview_html}
    {paa_html}
    {fs_html}
    {competition_html}
    {recommendations_html}

    <div class="footer">
        Generated by serp_feature_check.py &nbsp;|&nbsp; Data via DataForSEO SERP API &nbsp;|&nbsp; {now_str}
    </div>
</body>
</html>"""

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(full_html)

    print(f"  ✓ Report saved: {report_file}")
    return report_file


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Analyze SERP features for a keyword+URL combination.'
    )
    parser.add_argument('--keyword', required=True, help='Target keyword')
    parser.add_argument('--url', required=True, help='Target URL to check')
    parser.add_argument(
        '--location-code', type=int, default=DEFAULT_LOCATION_CODE,
        help='DataForSEO location code (default: 2840 = United States)'
    )
    parser.add_argument(
        '--language-code', default=DEFAULT_LANGUAGE_CODE,
        help='Language code (default: en)'
    )
    parser.add_argument(
        '--output', required=True,
        help='Output path (file or directory). .html extension will be added if missing.'
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  SERP Feature Check")
    print(f"  Keyword: {args.keyword}")
    print(f"  URL: {args.url}")
    print(f"{'='*60}\n")

    # Step 1: Fetch SERP features
    serp_data = fetch_serp_features(
        args.keyword, args.url,
        location_code=args.location_code,
        language_code=args.language_code,
    )

    if not serp_data:
        print("\n✗ Failed to fetch SERP data. Exiting.")
        sys.exit(1)

    # Step 2: Analyze competition
    print("\n  → Analyzing feature competition...")
    competition = analyze_feature_competition(serp_data, args.url)
    present = sum(1 for v in competition.values() if v['present'])
    won = sum(1 for v in competition.values() if v['present'] and v['target_featured'])
    print(f"  ✓ Features present: {present} | Won by target: {won}")

    # Step 3: Generate report
    print("\n  → Generating HTML report...")
    report_path = generate_serp_feature_report(
        args.keyword, args.url, serp_data, args.output
    )

    print(f"\n{'='*60}")
    print(f"  ✓ DONE")
    print(f"  Report: {report_path}")
    print(f"{'='*60}\n")

    return report_path


if __name__ == '__main__':
    main()