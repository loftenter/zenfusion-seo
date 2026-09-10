#!/usr/bin/env python3
"""
SERP Feature Landscape
======================
Standalone script that maps SERP feature dominance across a market.

Fetches live SERP data from DataForSEO's Google Organic Live Advanced
endpoint for N keywords and produces a comprehensive HTML report with:

  - Market overview (keyword count, total SERP features found)
  - Feature frequency table (appearance %, avg position impact, top owner)
  - Feature dominance chart (SVG bar chart)
  - AI Overview analysis (frequency, cited domains, content patterns)
  - PAA analysis (common questions extracted across keywords)
  - Featured snippet analysis (who owns them, content type)
  - Opportunities (features the target domain could win)
  - Recommendations for each feature type

Usage:
  python3 serp_feature_landscape.py \\
      --seed 'water softener' \\
      --keywords 'best water softener,best water softener system,...' \\
      --domain softprowatersystems.com \\
      --output /tmp/test_serp_landscape

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
from collections import Counter, defaultdict
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

# Canonical feature names — normalize variants (video/videos, image/images) to one key.
CANONICAL_FEATURE = {
    'videos': 'video',
    'images': 'image',
}

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


# ============================================================
# URL / DOMAIN HELPERS
# ============================================================
def normalize_domain(url):
    """Extract the root domain (without www) from a URL or bare domain string."""
    if not url:
        return ''
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host


def domain_matches(target_domain, candidate_url):
    """Check if a candidate URL/domain shares the same root domain as target_domain."""
    if not target_domain or not candidate_url:
        return False
    return normalize_domain(candidate_url) == target_domain


def canonical_feature(ftype):
    """Normalize variant feature types (e.g., 'videos' → 'video')."""
    return CANONICAL_FEATURE.get(ftype, ftype)


def feature_label(ftype):
    """Get human-readable label for a feature type (canonicalized)."""
    canon = canonical_feature(ftype) or 'unknown'
    return SERP_FEATURE_TYPES.get(canon, canon.replace('_', ' ').title())


# ============================================================
# 1. FETCH SERP FOR KEYWORD
# ============================================================
def fetch_serp_for_keyword(keyword, location_code=2840):
    """
    Call DataForSEO SERP organic live advanced and return all SERP features present.

    Returns a dict with:
        keyword, result_count, items_count, organic_results,
        features (list of {type, label, position, domain, url, is_target}),
        ai_overview, people_also_ask, featured_snippet, local_pack,
        video_carousel, shopping, knowledge_graph, things_to_know, perspectives,
        se_types_count, fetched_at
    Returns None on API failure.
    """
    print(f"  → Fetching SERP for keyword '{keyword}' (location {location_code})...")

    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
        'device': 'desktop',
        'os': 'windows',
        'depth': 100,
        'use_improved_etv': True,
        'parse_aiserp': True,
    }]

    data = dfs_api_call('serp/google/organic/live/advanced', payload, 'POST')
    if not data or not data.get('tasks'):
        print(f"  ✗ No SERP data returned for '{keyword}'")
        return None

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'unknown')}")
        return None

    results = task.get('result', [])
    if not results:
        print(f"  ✗ No result object for '{keyword}'")
        return None
    result = results[0] if isinstance(results, list) else results

    items = result.get('items') or []
    result_count = result.get('result_count', 0)

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
            markdown = item.get('markdown', '') or ''
            citations = re.findall(r'\[\[(\d+)\]\]\(([^)]+)\)', markdown)
            cited_urls = [u for _, u in citations]
            cited_domains = list(set(
                normalize_domain(u) for u in cited_urls if u
            ))
            # Also check items list inside ai_overview for organic citations
            ai_items = item.get('items') or []
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
            }

        # --- People Also Ask ---
        elif item_type == 'people_also_ask':
            paa_items = item.get('items') or []
            for paa in paa_items:
                people_also_ask.append({
                    'question': paa.get('question', paa.get('title', '')),
                    'answer': (paa.get('answer', paa.get('description', '')) or '')[:500],
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
                'position': rank_absolute,
            }
            features.append({
                'type': canonical_feature('featured_snippet'),
                'label': feature_label('featured_snippet'),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
            })

        # --- Local Pack ---
        elif item_type in ('local_pack', 'map'):
            local_items = item.get('items') or []
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
            }
            features.append({
                'type': canonical_feature(item_type),
                'label': feature_label(item_type),
                'position': rank_absolute,
                'domain': '',
                'url': '',
            })

        # --- Video Carousel ---
        elif item_type in ('video', 'videos'):
            video_items = item.get('items') or []
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
            }
            features.append({
                'type': canonical_feature(item_type),
                'label': feature_label(item_type),
                'position': rank_absolute,
                'domain': '',
                'url': '',
            })

        # --- Shopping ---
        elif item_type == 'shopping':
            shop_items = item.get('items') or []
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
            }
            features.append({
                'type': 'shopping',
                'label': feature_label('shopping'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
            })

        # --- Knowledge Graph ---
        elif item_type == 'knowledge_graph':
            knowledge_graph = {
                'position': rank_absolute,
                'title': item.get('title', ''),
                'description': (item.get('description', '') or '')[:1000],
                'url': item.get('url', ''),
                'domain': item.get('domain', ''),
            }
            features.append({
                'type': 'knowledge_graph',
                'label': feature_label('knowledge_graph'),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
            })

        # --- Things to Know ---
        elif item_type == 'things_to_know':
            ttk_items = item.get('items') or []
            things_to_know = {
                'position': rank_absolute,
                'items': [
                    {
                        'title': ti.get('title', ''),
                        'description': (ti.get('description', '') or '')[:300],
                    }
                    for ti in ttk_items
                ],
            }
            features.append({
                'type': 'things_to_know',
                'label': feature_label('things_to_know'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
            })

        # --- Perspectives ---
        elif item_type == 'perspectives':
            persp_items = item.get('items') or []
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
                'label': feature_label('perspectives'),
                'position': rank_absolute,
                'domain': '',
                'url': '',
            })

        # --- Other feature types ---
        elif item_type in SERP_FEATURE_TYPES and item_type != 'organic':
            features.append({
                'type': canonical_feature(item_type),
                'label': feature_label(item_type),
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
            })

    # If AI Overview was found, add it to features list
    if ai_overview:
        features.insert(0, {
            'type': 'ai_overview',
            'label': 'AI Overview',
            'position': ai_overview['position'],
            'domain': '',
            'url': '',
        })

    # Add PAA to features
    if people_also_ask:
        features.append({
            'type': 'people_also_ask',
            'label': 'People Also Ask',
            'position': 0,
            'domain': '',
            'url': '',
        })

    # Deduplicate features by type (keep first occurrence)
    seen_types = set()
    unique_features = []
    for f in features:
        if f['type'] not in seen_types:
            seen_types.add(f['type'])
            unique_features.append(f)

    serp_data = {
        'keyword': keyword,
        'location_code': location_code,
        'result_count': result_count,
        'items_count': len(items),
        'organic_results': organic_results[:30],
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
        'se_domain': result.get('se_domain', ''),
        'fetched_at': datetime.now().isoformat(),
    }

    feat_names = ', '.join(f['label'] for f in unique_features) or 'none'
    print(f"  ✓ SERP: {result_count:,} results | {len(organic_results)} organic | "
          f"Features: {feat_names}")

    return serp_data


# ============================================================
# 2. ANALYZE FEATURE FREQUENCY
# ============================================================
def analyze_feature_frequency(serp_results):
    """
    Across N keywords, compute how often each SERP feature appears as a percentage.

    Args:
        serp_results: list of serp_data dicts (from fetch_serp_for_keyword)

    Returns:
        dict with:
            total_keywords: int
            feature_frequency: {feature_type: {label, count, percentage, avg_position}}
            all_features_found: sorted list of (feature_type, count)
            total_features_found: int (unique feature types across all keywords)
    """
    total_keywords = len(serp_results)
    if total_keywords == 0:
        return {
            'total_keywords': 0,
            'feature_frequency': {},
            'all_features_found': [],
            'total_features_found': 0,
        }

    feature_counts = Counter()       # feature_type → # keywords where it appears
    feature_positions = defaultdict(list)  # feature_type → list of positions
    feature_labels = {}

    for serp in serp_results:
        if not serp:
            continue
        for f in serp.get('features', []):
            ftype = f['type']
            feature_counts[ftype] += 1
            feature_labels[ftype] = f['label']
            pos = f.get('position', 0)
            if pos and pos > 0:
                feature_positions[ftype].append(pos)

    feature_frequency = {}
    for ftype, count in feature_counts.most_common():
        positions = feature_positions.get(ftype, [])
        avg_pos = round(sum(positions) / len(positions), 1) if positions else 0
        feature_frequency[ftype] = {
            'label': feature_labels.get(ftype, feature_label(ftype)),
            'count': count,
            'percentage': round(count / total_keywords * 100, 1),
            'avg_position': avg_pos,
        }

    all_features_found = sorted(feature_counts.items(), key=lambda x: -x[1])

    return {
        'total_keywords': total_keywords,
        'feature_frequency': feature_frequency,
        'all_features_found': all_features_found,
        'total_features_found': len(feature_counts),
    }


# ============================================================
# 3. IDENTIFY FEATURE OWNERS
# ============================================================
def identify_feature_owners(serp_results, target_domain):
    """
    For each feature, determine which domains own it — i.e., which domains
    appear most frequently in featured snippets, AI Overviews, etc.

    Args:
        serp_results: list of serp_data dicts
        target_domain: domain string (e.g., 'softprowatersystems.com')

    Returns:
        dict: {feature_type: {
            'label', 'owners': [(domain, count)], 'target_owns': bool,
            'target_count': int, 'top_owner': str
        }}
    """
    target_domain = normalize_domain(target_domain)
    feature_owners = defaultdict(Counter)  # feature_type → Counter(domain → count)
    feature_labels = {}

    for serp in serp_results:
        if not serp:
            continue

        # AI Overview owners
        if serp.get('ai_overview'):
            ftype = 'ai_overview'
            feature_labels[ftype] = 'AI Overview'
            for d in serp['ai_overview'].get('cited_domains', []):
                if d:
                    feature_owners[ftype][d] += 1

        # PAA owners
        if serp.get('people_also_ask'):
            ftype = 'people_also_ask'
            feature_labels[ftype] = 'People Also Ask'
            for paa in serp['people_also_ask']:
                d = paa.get('domain', '')
                if d:
                    feature_owners[ftype][normalize_domain(d)] += 1

        # Featured snippet owner
        if serp.get('featured_snippet'):
            ftype = 'featured_snippet'
            feature_labels[ftype] = 'Featured Snippet'
            d = serp['featured_snippet'].get('domain', '')
            if d:
                feature_owners[ftype][normalize_domain(d)] += 1

        # Local pack owners
        if serp.get('local_pack'):
            ftype = canonical_feature('local_pack')
            feature_labels[ftype] = feature_label('local_pack')
            for li in serp['local_pack'].get('items') or []:
                d = li.get('domain', '')
                if d:
                    feature_owners[ftype][normalize_domain(d)] += 1

        # Video carousel owners
        if serp.get('video_carousel'):
            ftype = canonical_feature('video')
            feature_labels[ftype] = feature_label('video')
            for vi in serp['video_carousel'].get('items') or []:
                d = vi.get('source', '')
                if d:
                    feature_owners[ftype][normalize_domain(d)] += 1

        # Shopping owners
        if serp.get('shopping'):
            ftype = 'shopping'
            feature_labels[ftype] = feature_label('shopping')
            for si in serp['shopping'].get('items') or []:
                d = si.get('source', '')
                if d:
                    feature_owners[ftype][normalize_domain(d)] += 1

        # Knowledge Graph owner
        if serp.get('knowledge_graph'):
            ftype = 'knowledge_graph'
            feature_labels[ftype] = feature_label('knowledge_graph')
            d = serp['knowledge_graph'].get('domain', '')
            if d:
                feature_owners[ftype][normalize_domain(d)] += 1

        # Perspectives owners
        if serp.get('perspectives'):
            ftype = 'perspectives'
            feature_labels[ftype] = feature_label('perspectives')
            for pi in serp['perspectives'].get('items') or []:
                d = pi.get('source', '')
                if d:
                    feature_owners[ftype][normalize_domain(d)] += 1

        # Things to Know — no domain owners per se, skip

        # Generic feature scan: for any other feature with a domain field
        for f in serp.get('features', []):
            ftype = f['type']
            if ftype in feature_labels:
                continue  # Already handled above
            feature_labels[ftype] = f.get('label', feature_label(ftype))
            d = f.get('domain', '')
            if d:
                feature_owners[ftype][normalize_domain(d)] += 1

    # Build the result
    result = {}
    for ftype, counter in feature_owners.items():
        owners_sorted = counter.most_common(20)
        target_count = counter.get(target_domain, 0)
        result[ftype] = {
            'label': feature_labels.get(ftype, feature_label(ftype)),
            'owners': owners_sorted,
            'target_owns': target_count > 0,
            'target_count': target_count,
            'top_owner': owners_sorted[0][0] if owners_sorted else '',
        }

    return result


# ============================================================
# 4. GENERATE LANDSCAPE REPORT
# ============================================================
def _esc(text):
    """HTML-escape text."""
    if text is None:
        return ''
    return html_mod.escape(str(text))


def _detect_snippet_type(description):
    """Detect if a featured snippet is paragraph, list, or table based on its content."""
    if not description:
        return 'paragraph'
    desc = description.strip()
    # Check for table-like patterns
    if '|' in desc or '\t' in desc:
        return 'table'
    # Check for list-like patterns
    list_patterns = [
        r'^\d+[\.\)]\s',      # 1. or 1) at start
        r'^[-•*]\s',          # bullet points
        r'^\d+\.\s.*\n\d+\.\s',  # numbered list multi-line
    ]
    for pattern in list_patterns:
        if re.search(pattern, desc, re.MULTILINE):
            return 'list'
    # Default
    return 'paragraph'


def _generate_svg_bar_chart(feature_frequency, max_features=15):
    """Generate an SVG bar chart showing feature appearance percentages."""
    sorted_features = sorted(
        feature_frequency.items(),
        key=lambda x: -x[1]['percentage']
    )[:max_features]

    if not sorted_features:
        return '<p>No feature data available for chart.</p>'

    chart_width = 700
    bar_height = 28
    bar_gap = 8
    label_width = 160
    value_width = 60
    chart_area_width = chart_width - label_width - value_width - 40
    max_bars = len(sorted_features)
    chart_height = max_bars * (bar_height + bar_gap) + 40

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {chart_width} {chart_height}" '
        f'style="width:100%;max-width:{chart_width}px;height:auto;">',
        f'<rect width="{chart_width}" height="{chart_height}" fill="#ffffff"/>',
    ]

    y = 20
    for ftype, info in sorted_features:
        pct = info['percentage']
        bar_w = int(chart_area_width * pct / 100)
        label = info['label']
        # Truncate long labels
        if len(label) > 22:
            label = label[:20] + '…'

        # Bar background (light gray)
        svg_parts.append(
            f'<rect x="{label_width + 20}" y="{y}" width="{chart_area_width}" '
            f'height="{bar_height}" fill="#f0f0f0" rx="3"/>'
        )
        # Actual bar (blue)
        color = '#0066cc'
        if pct >= 75:
            color = '#004c99'
        elif pct >= 50:
            color = '#0066cc'
        elif pct >= 25:
            color = '#4d94db'
        else:
            color = '#80b3ff'
        svg_parts.append(
            f'<rect x="{label_width + 20}" y="{y}" width="{bar_w}" '
            f'height="{bar_height}" fill="{color}" rx="3"/>'
        )
        # Label
        svg_parts.append(
            f'<text x="{label_width + 10}" y="{y + bar_height - 8}" '
            f'text-anchor="end" font-size="13" fill="#1a1a1a" font-family="sans-serif">'
            f'{_esc(label)}</text>'
        )
        # Percentage text
        svg_parts.append(
            f'<text x="{label_width + 20 + chart_area_width + 8}" y="{y + bar_height - 8}" '
            f'font-size="13" fill="#1a1a1a" font-family="sans-serif">{pct}%</text>'
        )
        y += bar_height + bar_gap

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def generate_landscape_report(seed_keyword, serp_results, analysis, output_path,
                               target_domain=None):
    """
    Generate an HTML report showing SERP feature dominance across a market.

    Writes the report to `<output_path>.html` (or `<output_path>/report.html`
    if output_path is a directory) and returns the file path.
    """
    if output_path.endswith('.html'):
        report_file = output_path
    elif os.path.isdir(output_path) or output_path.endswith('/'):
        os.makedirs(output_path, exist_ok=True)
        report_file = os.path.join(output_path, 'serp_feature_landscape.html')
    else:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        report_file = output_path + '.html'

    target_domain_norm = normalize_domain(target_domain) if target_domain else ''
    feature_owners = identify_feature_owners(serp_results, target_domain or '')
    feature_freq = analysis.get('feature_frequency', {})
    total_keywords = analysis.get('total_keywords', len(serp_results))
    total_features = analysis.get('total_features_found', 0)

    # --- Feature frequency table ---
    sorted_freq = sorted(feature_freq.items(), key=lambda x: -x[1]['percentage'])
    freq_rows = []
    for ftype, info in sorted_freq:
        label = info['label']
        pct = info['percentage']
        count = info['count']
        avg_pos = info['avg_position']
        owner_info = feature_owners.get(ftype, {})
        top_owner = owner_info.get('top_owner', '')
        target_owns = owner_info.get('target_owns', False)
        owner_display = top_owner if top_owner else '—'
        if target_owns:
            owner_display = f'<strong style="color:#27ae60;">{target_domain_norm}</strong>'
        elif top_owner:
            owner_display = f'<span style="color:#666;">{_esc(top_owner)}</span>'
        freq_rows.append(
            f"""<tr>
                <td><strong>{_esc(label)}</strong></td>
                <td class="center">{pct}%</td>
                <td class="center">{count}/{total_keywords}</td>
                <td class="center">{avg_pos if avg_pos else '—'}</td>
                <td>{owner_display}</td>
            </tr>"""
        )
    freq_table_html = '\n'.join(freq_rows) if freq_rows else '<tr><td colspan="5">No feature data.</td></tr>'

    # --- SVG Bar Chart ---
    svg_chart = _generate_svg_bar_chart(feature_freq)

    # --- AI Overview Analysis ---
    ai_count = sum(1 for s in serp_results if s and s.get('ai_overview'))
    ai_pct = round(ai_count / total_keywords * 100, 1) if total_keywords else 0
    ai_cited_domains = Counter()
    ai_content_samples = []
    for s in serp_results:
        if not s or not s.get('ai_overview'):
            continue
        for d in s['ai_overview'].get('cited_domains', []):
            ai_cited_domains[d] += 1
        text = s['ai_overview'].get('text', '')
        if text:
            ai_content_samples.append({
                'keyword': s['keyword'],
                'text': text[:600],
                'citations': s['ai_overview'].get('cited_domains', []),
                'citation_count': s['ai_overview'].get('citation_count', 0),
            })

    ai_top_domains_html = ''
    if ai_cited_domains:
        ai_top_items = []
        for d, c in ai_cited_domains.most_common(15):
            is_target = (d == target_domain_norm)
            style = ' style="color:#27ae60;font-weight:bold;"' if is_target else ''
            ai_top_items.append(
                f'<tr><td{_style_or_empty(is_target)}>{_esc(d)}</td>'
                f'<td class="center">{c}</td></tr>'
            )
        ai_top_domains_html = f"""
        <h3>Cited Domains in AI Overview</h3>
        <table class="data-table">
            <thead><tr><th>Domain</th><th>Citations</th></tr></thead>
            <tbody>{''.join(ai_top_items)}</tbody>
        </table>"""
    else:
        ai_top_domains_html = '<p>No AI Overview citations found across analyzed keywords.</p>'

    ai_samples_html = ''
    if ai_content_samples:
        sample_cards = []
        for sample in ai_content_samples[:5]:
            kw = _esc(sample['keyword'])
            text = _esc(sample['text'])
            cited = ', '.join(_esc(d) for d in sample['citations'][:8]) or 'None'
            cc = sample['citation_count']
            sample_cards.append(
                f"""<div class="ai-sample-card">
                    <div class="ai-sample-kw">Keyword: <strong>{kw}</strong> ({cc} citations)</div>
                    <div class="ai-sample-text">{text}</div>
                    <div class="ai-sample-cited"><strong>Cited:</strong> {cited}</div>
                </div>"""
            )
        ai_samples_html = f'<h3>AI Overview Content Samples</h3>{"".join(sample_cards)}'
    else:
        ai_samples_html = '<p>No AI Overview content was captured.</p>'

    target_in_ai = ai_cited_domains.get(target_domain_norm, 0)
    ai_analysis_html = f"""
    <div class="metric-row">
        <div class="metric-card"><div class="card-value">{ai_count}/{total_keywords}</div><div class="card-label">AI Overview Appears</div></div>
        <div class="metric-card"><div class="card-value">{ai_pct}%</div><div class="card-label">Of Keywords</div></div>
        <div class="metric-card"><div class="card-value">{len(ai_cited_domains)}</div><div class="card-label">Unique Cited Domains</div></div>
        <div class="metric-card"><div class="card-value">{target_in_ai}</div><div class="card-label">Target Cited</div></div>
    </div>
    {ai_top_domains_html}
    {ai_samples_html}
    """

    # --- PAA Analysis ---
    all_paa_questions = []
    paa_domain_counter = Counter()
    for s in serp_results:
        if not s:
            continue
        for paa in s.get('people_also_ask', []):
            q = paa.get('question', '') or paa.get('title', '')
            if q:
                all_paa_questions.append(q)
            d = paa.get('domain', '')
            if d:
                paa_domain_counter[normalize_domain(d)] += 1

    paa_count = len(all_paa_questions)
    paa_keywords_with_paa = sum(1 for s in serp_results if s and s.get('people_also_ask'))
    paa_pct = round(paa_keywords_with_paa / total_keywords * 100, 1) if total_keywords else 0

    # Deduplicate and find common questions
    paa_question_counter = Counter(all_paa_questions)
    common_questions = paa_question_counter.most_common(20)

    common_questions_html = ''
    if common_questions:
        q_items = []
        for q, c in common_questions:
            q_items.append(
                f'<li><span class="q-count">{c}x</span> {_esc(q)}</li>'
            )
        common_questions_html = f'<ol class="paa-list">{"".join(q_items)}</ol>'
    else:
        common_questions_html = '<p>No People Also Ask questions found.</p>'

    paa_top_owners_html = ''
    if paa_domain_counter:
        paa_owner_items = []
        for d, c in paa_domain_counter.most_common(10):
            is_target = (d == target_domain_norm)
            paa_owner_items.append(
                f'<tr><td>{_esc(d)}</td><td class="center">{c}</td></tr>'
            )
        paa_top_owners_html = f"""
        <h3>Top PAA Answer Sources</h3>
        <table class="data-table">
            <thead><tr><th>Domain</th><th>Answers Provided</th></tr></thead>
            <tbody>{''.join(paa_owner_items)}</tbody>
        </table>"""

    paa_analysis_html = f"""
    <div class="metric-row">
        <div class="metric-card"><div class="card-value">{paa_keywords_with_paa}/{total_keywords}</div><div class="card-label">Keywords with PAA</div></div>
        <div class="metric-card"><div class="card-value">{paa_pct}%</div><div class="card-label">PAA Presence</div></div>
        <div class="metric-card"><div class="card-value">{paa_count}</div><div class="card-label">Total Questions</div></div>
        <div class="metric-card"><div class="card-value">{len(paa_question_counter)}</div><div class="card-label">Unique Questions</div></div>
    </div>
    <h3>Common Questions Across Keywords</h3>
    {common_questions_html}
    {paa_top_owners_html}
    """

    # --- Featured Snippet Analysis ---
    fs_count = 0
    fs_owners = Counter()
    fs_types = Counter()
    fs_samples = []
    for s in serp_results:
        if not s:
            continue
        fs = s.get('featured_snippet')
        if fs:
            fs_count += 1
            d = fs.get('domain', '')
            if d:
                fs_owners[normalize_domain(d)] += 1
            snippet_type = _detect_snippet_type(fs.get('description', ''))
            fs_types[snippet_type] += 1
            fs_samples.append({
                'keyword': s['keyword'],
                'domain': d,
                'url': fs.get('url', ''),
                'title': fs.get('title', ''),
                'description': fs.get('description', '')[:400],
                'snippet_type': snippet_type,
            })

    fs_pct = round(fs_count / total_keywords * 100, 1) if total_keywords else 0
    target_fs = fs_owners.get(target_domain_norm, 0)

    fs_owners_html = ''
    if fs_owners:
        fs_owner_items = []
        for d, c in fs_owners.most_common(10):
            is_target = (d == target_domain_norm)
            style = ' style="color:#27ae60;font-weight:bold;"' if is_target else ''
            fs_owner_items.append(
                f'<tr><td{style}>{_esc(d)}</td><td class="center">{c}</td></tr>'
            )
        fs_owners_html = f"""
        <h3>Featured Snippet Owners</h3>
        <table class="data-table">
            <thead><tr><th>Domain</th><th>Snippets Won</th></tr></thead>
            <tbody>{''.join(fs_owner_items)}</tbody>
        </table>"""

    fs_types_html = ''
    if fs_types:
        type_items = []
        for t, c in fs_types.most_common():
            type_items.append(f'<span class="badge badge-type">{_esc(t)}: {c}</span>')
        fs_types_html = f'<div class="badge-row">{"".join(type_items)}</div>'

    fs_samples_html = ''
    if fs_samples:
        sample_cards = []
        for sample in fs_samples[:5]:
            kw = _esc(sample['keyword'])
            domain = _esc(sample['domain'])
            title = _esc(sample['title'])
            desc = _esc(sample['description'])
            url = _esc(sample['url'])
            stype = _esc(sample['snippet_type'])
            sample_cards.append(
                f"""<div class="fs-sample-card">
                    <div class="fs-sample-kw">{kw} <span class="badge badge-type">{stype}</span></div>
                    <div class="fs-sample-domain"><a href="{url}" target="_blank" rel="noopener nofollow">{domain}</a> — {title}</div>
                    <div class="fs-sample-desc">{desc}</div>
                </div>"""
            )
        fs_samples_html = f'<h3>Featured Snippet Samples</h3>{"".join(sample_cards)}'
    else:
        fs_samples_html = '<p>No featured snippets found across analyzed keywords.</p>'

    fs_analysis_html = f"""
    <div class="metric-row">
        <div class="metric-card"><div class="card-value">{fs_count}/{total_keywords}</div><div class="card-label">Keywords with FS</div></div>
        <div class="metric-card"><div class="card-value">{fs_pct}%</div><div class="card-label">FS Presence</div></div>
        <div class="metric-card"><div class="card-value">{target_fs}</div><div class="card-label">Target Owns</div></div>
        <div class="metric-card"><div class="card-value">{len(fs_owners)}</div><div class="card-label">Unique Owners</div></div>
    </div>
    {fs_types_html}
    {fs_owners_html}
    {fs_samples_html}
    """

    # --- Opportunities ---
    opportunities = []
    for ftype, info in sorted_freq:
        owner_info = feature_owners.get(ftype, {})
        target_owns = owner_info.get('target_owns', False)
        if not target_owns and info['percentage'] >= 10:
            rec = FEATURE_RECOMMENDATIONS.get(ftype, DEFAULT_RECOMMENDATION)
            top_owner = owner_info.get('top_owner', '—')
            opportunities.append({
                'feature': info['label'],
                'pct': info['percentage'],
                'top_owner': top_owner,
                'recommendation': rec,
            })

    opp_html = ''
    if opportunities:
        opp_items = []
        for opp in opportunities:
            opp_items.append(
                f"""<div class="opp-card">
                    <div class="opp-header">
                        <strong>{_esc(opp['feature'])}</strong>
                        <span class="badge badge-present">{opp['pct']}% present</span>
                    </div>
                    <div class="opp-owner">Currently owned by: {_esc(opp['top_owner'])}</div>
                    <div class="opp-rec">{_esc(opp['recommendation'])}</div>
                </div>"""
            )
        opp_html = ''.join(opp_items)
    else:
        opp_html = '<p>No gap opportunities identified — the target domain already owns all significant features, or features are too rare.</p>'

    # --- Recommendations ---
    rec_items = []
    for ftype, info in sorted_freq:
        rec = FEATURE_RECOMMENDATIONS.get(ftype, DEFAULT_RECOMMENDATION)
        rec_items.append(
            f"""<div class="rec-card">
                <div class="rec-feature">{_esc(info['label'])} <span class="badge badge-present">{info['percentage']}%</span></div>
                <div class="rec-text">{_esc(rec)}</div>
            </div>"""
        )
    recommendations_html = ''.join(rec_items) if rec_items else '<p>No recommendations.</p>'

    # --- Build full HTML ---
    now = datetime.now().strftime('%Y-%m-%d %H:%M CST')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="robots" content="noindex,nofollow">
<title>SERP Feature Landscape — {_esc(seed_keyword)}</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        background: #ffffff;
        color: #1a1a1a;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
        font-size: 16px;
        line-height: 1.6;
        padding: 20px;
        max-width: 1200px;
        margin: 0 auto;
    }}
    h1 {{
        color: #1a1a1a;
        font-size: 28px;
        margin-bottom: 8px;
    }}
    h2 {{
        color: #1a1a1a;
        font-size: 24px;
        margin-top: 36px;
        margin-bottom: 16px;
        padding-bottom: 8px;
        border-bottom: 2px solid #e0e0e0;
    }}
    h3 {{
        color: #1a1a1a;
        font-size: 20px;
        margin-top: 24px;
        margin-bottom: 12px;
    }}
    a {{
        color: #0066cc;
        text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    .subtitle {{
        color: #666;
        font-size: 15px;
        margin-bottom: 24px;
    }}
    .metric-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 16px;
        margin-bottom: 24px;
    }}
    .metric-card {{
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 16px 24px;
        min-width: 140px;
        flex: 1;
        text-align: center;
    }}
    .card-value {{
        font-size: 24px;
        font-weight: 700;
        color: #0066cc;
    }}
    .card-label {{
        font-size: 13px;
        color: #666;
        margin-top: 4px;
    }}
    table.data-table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 20px;
        font-size: 15px;
    }}
    table.data-table th {{
        background: #f8f9fa;
        color: #1a1a1a;
        font-weight: 600;
        text-align: left;
        padding: 10px 12px;
        border-bottom: 2px solid #e0e0e0;
    }}
    table.data-table td {{
        padding: 10px 12px;
        border-bottom: 1px solid #eee;
        vertical-align: top;
    }}
    table.data-table td.center {{
        text-align: center;
    }}
    table.data-table tr:hover {{
        background: #f8f9fa;
    }}
    .chart-container {{
        background: #fff;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 24px;
        overflow-x: auto;
    }}
    .ai-sample-card, .fs-sample-card {{
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-radius: 6px;
        padding: 16px;
        margin-bottom: 12px;
    }}
    .ai-sample-kw, .fs-sample-kw {{
        font-weight: 600;
        margin-bottom: 8px;
    }}
    .ai-sample-text, .fs-sample-desc {{
        font-size: 14px;
        color: #444;
        max-height: 200px;
        overflow-y: auto;
        margin-bottom: 8px;
        line-height: 1.5;
    }}
    .ai-sample-cited, .fs-sample-domain {{
        font-size: 13px;
        color: #666;
    }}
    .paa-list {{
        list-style: none;
        counter-reset: paa-counter;
    }}
    .paa-list li {{
        padding: 8px 0;
        border-bottom: 1px solid #eee;
        counter-increment: paa-counter;
    }}
    .paa-list li::before {{
        content: counter(paa-counter) ".";
        font-weight: 700;
        color: #0066cc;
        margin-right: 8px;
    }}
    .q-count {{
        background: #e8f0fe;
        color: #0066cc;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 600;
        margin-right: 6px;
    }}
    .badge {{
        display: inline-block;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
    }}
    .badge-present {{ background: #f39c12; color: #fff; }}
    .badge-type {{ background: #e8f0fe; color: #0066cc; margin-right: 6px; }}
    .badge-row {{ margin: 12px 0; }}
    .opp-card, .rec-card {{
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-left: 4px solid #0066cc;
        border-radius: 6px;
        padding: 16px;
        margin-bottom: 12px;
    }}
    .opp-header, .rec-feature {{
        font-size: 17px;
        margin-bottom: 8px;
    }}
    .opp-owner {{
        font-size: 14px;
        color: #666;
        margin-bottom: 8px;
    }}
    .opp-rec, .rec-text {{
        font-size: 14px;
        color: #444;
    }}
    .section {{
        margin-bottom: 32px;
    }}
    footer {{
        margin-top: 40px;
        padding-top: 20px;
        border-top: 1px solid #e0e0e0;
        font-size: 13px;
        color: #999;
    }}
</style>
</head>
<body>
    <h1>SERP Feature Landscape Report</h1>
    <p class="subtitle">Seed keyword: <strong>{_esc(seed_keyword)}</strong>
    {'| Target domain: <strong>' + _esc(target_domain_norm) + '</strong>' if target_domain_norm else ''}
    | Generated: {now}</p>

    <div class="section">
        <h2>Market Overview</h2>
        <div class="metric-row">
            <div class="metric-card"><div class="card-value">{total_keywords}</div><div class="card-label">Keywords Analyzed</div></div>
            <div class="metric-card"><div class="card-value">{total_features}</div><div class="card-label">Unique SERP Features</div></div>
            <div class="metric-card"><div class="card-value">{ai_count}</div><div class="card-label">AI Overview Present</div></div>
            <div class="metric-card"><div class="card-value">{fs_count}</div><div class="card-label">Featured Snippets</div></div>
            <div class="metric-card"><div class="card-value">{paa_count}</div><div class="card-label">PAA Questions</div></div>
        </div>
    </div>

    <div class="section">
        <h2>Feature Frequency Table</h2>
        <table class="data-table">
            <thead>
                <tr>
                    <th>SERP Feature</th>
                    <th>Appearance %</th>
                    <th>Keywords</th>
                    <th>Avg Position</th>
                    <th>Top Owner</th>
                </tr>
            </thead>
            <tbody>
                {freq_table_html}
            </tbody>
        </table>
    </div>

    <div class="section">
        <h2>Feature Dominance Chart</h2>
        <div class="chart-container">
            {svg_chart}
        </div>
    </div>

    <div class="section">
        <h2>AI Overview Analysis</h2>
        {ai_analysis_html}
    </div>

    <div class="section">
        <h2>People Also Ask Analysis</h2>
        {paa_analysis_html}
    </div>

    <div class="section">
        <h2>Featured Snippet Analysis</h2>
        {fs_analysis_html}
    </div>

    <div class="section">
        <h2>Opportunities for {_esc(target_domain_norm or 'Target Domain')}</h2>
        {opp_html}
    </div>

    <div class="section">
        <h2>Feature-Specific Recommendations</h2>
        {recommendations_html}
    </div>

    <footer>
        <p>Report generated by serp_feature_landscape.py | DataForSEO SERP API | {now}</p>
        <p>noindex,nofollow — this report is for internal use only</p>
    </footer>
</body>
</html>"""

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n  ✓ Report written to: {report_file}")
    return report_file


# ============================================================
# HELPERS used in template above
# ============================================================
def _style_or_empty(is_target):
    """Return style attribute for target domain rows, empty otherwise."""
    return ' style="color:#27ae60;font-weight:bold;"' if is_target else ''


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Map SERP feature dominance across a market.'
    )
    parser.add_argument('--seed', required=True,
                        help='Seed keyword (market label, e.g., "water softener")')
    parser.add_argument('--keywords', required=True,
                        help='Comma-separated list of keywords to analyze')
    parser.add_argument('--domain', default='',
                        help='Target domain for opportunity analysis (e.g., softprowatersystems.com)')
    parser.add_argument('--output', required=True,
                        help='Output path (file or directory)')
    parser.add_argument('--location', type=int, default=DEFAULT_LOCATION_CODE,
                        help=f'Location code (default {DEFAULT_LOCATION_CODE} = US)')
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(',') if k.strip()]
    if not keywords:
        print("ERROR: No keywords provided.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"SERP Feature Landscape Analysis")
    print(f"{'='*60}")
    print(f"Seed keyword: {args.seed}")
    print(f"Keywords to analyze: {len(keywords)}")
    print(f"Target domain: {args.domain or '(none)'}")
    print(f"Output: {args.output}")
    print(f"{'='*60}\n")

    # Step 1: Fetch SERP for each keyword
    print("Step 1: Fetching SERP data for all keywords...\n")
    serp_results = []
    for i, kw in enumerate(keywords, 1):
        print(f"[{i}/{len(keywords)}] {kw}")
        serp = fetch_serp_for_keyword(kw, location_code=args.location)
        if serp:
            serp_results.append(serp)
        else:
            print(f"  ✗ Skipping '{kw}' — no data\n")
        print()

    if not serp_results:
        print("ERROR: No SERP data collected. Exiting.")
        sys.exit(1)

    print(f"\nSuccessfully fetched SERP data for {len(serp_results)}/{len(keywords)} keywords.\n")

    # Step 2: Analyze feature frequency
    print("Step 2: Analyzing feature frequency...")
    analysis = analyze_feature_frequency(serp_results)
    print(f"  Found {analysis['total_features_found']} unique SERP features across {analysis['total_keywords']} keywords.")
    for ftype, info in sorted(analysis['feature_frequency'].items(), key=lambda x: -x[1]['percentage']):
        print(f"    {info['label']}: {info['percentage']}% ({info['count']}/{analysis['total_keywords']})")
    print()

    # Step 3: Identify feature owners
    print("Step 3: Identifying feature owners...")
    owners = identify_feature_owners(serp_results, args.domain)
    for ftype, info in owners.items():
        top = info['top_owner'] or '—'
        print(f"    {info['label']}: top owner = {top} (target: {info['target_count']})")
    print()

    # Step 4: Generate report
    print("Step 4: Generating HTML report...")
    report_path = generate_landscape_report(
        args.seed, serp_results, analysis, args.output, target_domain=args.domain
    )

    print(f"\n{'='*60}")
    print(f"COMPLETE — Report: {report_path}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
