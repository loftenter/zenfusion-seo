#!/usr/bin/env python3
"""
Video SEO Tracker
================
Standalone script that tracks video SEO presence in Google SERP results.

Fetches live SERP data from DataForSEO's Google Organic Live Advanced endpoint
for N keywords and produces a comprehensive HTML report focused on video SEO:

  - Summary cards: total keywords, keywords with video carousel, total videos
    found, target channel presence, video opportunity count
  - Video carousel frequency chart (SVG): % of keywords where video carousel appears
  - Top ranking videos table (expandable): keyword, video title, channel, URL,
    position, duration
  - Channel analysis: which channels appear most often across keywords
  - Video opportunities table: keywords where video carousel exists but target
    doesn't rank — with search volume and priority
  - Content recommendations: what video topics to create, based on what's ranking
  - YouTube optimization tips: title/description/tag suggestions based on ranking
    video patterns

Functions
---------
  fetch_serp_videos(keyword, location_code=2840)
  analyze_video_competition(video_results, target_domain)
  identify_video_opportunities(keywords, video_results, target_youtube_channel)
  generate_video_report(keywords, video_results, analysis, opportunities, output_path)

Usage
-----
  python3 video_seo_tracker.py \\
      --keywords 'best water softener,best water softener system,...' \\
      --domain softprowatersystems.com \\
      --output /tmp/test_video_seo

Environment
-----------
  DATAFORSEO_LOGIN    - DataForSEO API login (from ~/.hermes/.env)
  DATAFORSEO_PASSWORD - DataForSEO API password (from ~/.hermes/.env)
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import os
import sys
import json
import base64
import argparse
import re
import html as html_mod
from datetime import datetime
from collections import Counter, defaultdict
from urllib.parse import urlparse, parse_qs

import urllib.request
import urllib.error

# === CONFIG ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 90


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
            data = _fix_goto(json.loads(resp.read()), endpoint)
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


def parse_channel_from_source(source):
    """
    Parse channel name from DataForSEO 'source'/'website_name' field.

    Examples:
      'YouTube · Drew the Water Guru' -> 'Drew the Water Guru'
      'YouTube  ·  This Old House'     -> 'This Old House'
      'Diamond Crystal Salt · Diamond Crystal Home' -> 'Diamond Crystal Home'
    Returns the channel/platform name or the original source if no separator.
    """
    if not source:
        return ''
    # Split on · (middle dot) which DataForSEO uses to separate platform · channel
    parts = re.split(r'\s*·\s*', source)
    if len(parts) >= 2:
        # Last meaningful part is the channel
        return parts[-1].strip()
    return source.strip()


def parse_platform_from_source(source):
    """Extract the platform (YouTube, TikTok, etc.) from the source field."""
    if not source:
        return ''
    parts = re.split(r'\s*·\s*', source)
    if len(parts) >= 1:
        return parts[0].strip()
    return ''


def extract_youtube_id(url):
    """Extract a YouTube video ID from a URL if present."""
    if not url:
        return ''
    parsed = urlparse(url)
    if 'youtube.com' in (parsed.netloc or ''):
        qs = parse_qs(parsed.query or '')
        v = qs.get('v', [''])
        return v[0] if v else ''
    elif 'youtu.be' in (parsed.netloc or ''):
        vid = parsed.path.strip('/').split('/')[0] if parsed.path else ''
        return vid
    return ''


def _esc(text):
    """HTML-escape text."""
    if text is None:
        return ''
    return html_mod.escape(str(text))


# ============================================================
# 1. FETCH SERP VIDEOS
# ============================================================
def fetch_serp_videos(keyword, location_code=2840):
    """
    Call DataForSEO serp/google/organic/live/advanced and extract ALL video
    results from the SERP.

    Video results come from two sources in the SERP:
      1. 'video' type carousel items — an items[] array of video_element entries
         (the prominent "Video" carousel box at top of results).
      2. 'organic' items with is_video=true — individual YouTube/video URLs that
         rank as organic results.

    For each video found, return: video URL, title, channel/domain, position in
    SERP, thumbnail (if available), duration (if available).

    Args:
        keyword:        search query string
        location_code:  DataForSEO location code (default 2840 = United States)

    Returns:
        dict with:
            keyword, location_code, result_count,
            has_video_carousel (bool),
            carousel_position (int or None),
            videos (list of dicts):
                {url, title, channel, platform, domain, position, rank_type,
                 video_id, thumbnail, duration, source_raw, views_breadcrumb,
                 timestamp, source_type: 'carousel'|'organic'}
            fetched_at
        Returns None on API failure.
    """
    print(f"  → Fetching SERP videos for '{keyword}' (location {location_code})...")

    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
        'device': 'desktop',
        'os': 'windows',
        'depth': 100,
        'use_improved_etv': True,
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

    videos = []
    has_video_carousel = False
    carousel_position = None

    for item in items:
        item_type = item.get('type', 'unknown')
        rank_absolute = item.get('rank_absolute', 0)
        rank_group = item.get('rank_group', 0)

        # --- Video carousel (type='video' or 'videos') ---
        if item_type in ('video', 'videos', 'featured_video'):
            has_video_carousel = True
            carousel_position = rank_absolute
            video_items = item.get('items') or []
            for vi in video_items:
                if vi.get('type') and vi['type'] not in ('video_element', 'video'):
                    # skip non-video sub-items
                    continue
                source_raw = vi.get('source', '') or vi.get('channel', '') or ''
                channel = parse_channel_from_source(source_raw)
                platform = parse_platform_from_source(source_raw) or 'YouTube'
                url = vi.get('url', '')
                domain = normalize_domain(url) if url else ''
                # If no domain from URL, derive from source (e.g., Diamond Crystal)
                if not domain and source_raw:
                    platform_first = parse_platform_from_source(source_raw)
                    if platform_first and 'youtube' not in platform_first.lower():
                        domain = ''
                videos.append({
                    'url': url,
                    'title': vi.get('title', ''),
                    'channel': channel,
                    'platform': platform,
                    'domain': domain,
                    'position': rank_absolute,
                    'rank_type': 'carousel',
                    'video_id': vi.get('video_id', '') or extract_youtube_id(url),
                    'thumbnail': vi.get('thumbnail_url', '') or vi.get('preview_url', ''),
                    'duration': vi.get('duration', '') or vi.get('video_duration', ''),
                    'source_raw': source_raw,
                    'views_breadcrumb': '',
                    'timestamp': vi.get('timestamp', '') or vi.get('date', ''),
                    'source_type': 'carousel',
                })

        # --- Organic items flagged as video (is_video=true) ---
        elif item_type == 'organic' and item.get('is_video'):
            source_raw = item.get('website_name', '') or item.get('source', '') or ''
            channel = parse_channel_from_source(source_raw)
            platform = parse_platform_from_source(source_raw) or 'YouTube'
            url = item.get('url', '')
            domain = item.get('domain', '') or normalize_domain(url)
            videos.append({
                'url': url,
                'title': item.get('title', ''),
                'channel': channel,
                'platform': platform,
                'domain': domain,
                'position': rank_absolute,
                'rank_type': 'organic',
                'video_id': extract_youtube_id(url),
                'thumbnail': '',
                'duration': '',
                'source_raw': source_raw,
                'views_breadcrumb': item.get('breadcrumb', '') or '',
                'timestamp': item.get('timestamp', '') or '',
                'source_type': 'organic',
            })

    print(f"  ✓ {keyword}: {len(videos)} videos found"
          f"{' (carousel at #' + str(carousel_position) + ')' if has_video_carousel else ''}")

    return {
        'keyword': keyword,
        'location_code': location_code,
        'result_count': result_count,
        'has_video_carousel': has_video_carousel,
        'carousel_position': carousel_position,
        'videos': videos,
        'fetched_at': datetime.now().isoformat(),
    }


# ============================================================
# SEARCH VOLUME (helper for opportunity scoring)
# ============================================================
def fetch_search_volumes(keywords, location_code=2840):
    """
    Fetch search volumes for a list of keywords via DataForSEO Google Ads
    Search Volume (Live). Returns dict {keyword: search_volume}.
    """
    if not keywords:
        return {}
    payload = [{
        'keywords': list(keywords),
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
    }]
    data = dfs_api_call('keywords_data/google_ads/search_volume/live', payload, 'POST')
    volumes = {}
    if not data or not data.get('tasks'):
        return volumes
    for task in data['tasks']:
        for r in task.get('result', []):
            kw = r.get('keyword', '')
            vol = r.get('search_volume', 0) or 0
            if kw:
                volumes[kw] = vol
    return volumes


# ============================================================
# 2. ANALYZE VIDEO COMPETITION
# ============================================================
def analyze_video_competition(video_results, target_domain):
    """
    For each keyword checked, determine:
      - Does a video carousel appear?
      - Which channels/domains own the videos?
      - Is the target domain's YouTube channel represented?
      - What topics do the ranking videos cover?

    Args:
        video_results: list of dicts from fetch_serp_videos (one per keyword)
        target_domain: domain string (e.g., 'softprowatersystems.com')

    Returns:
        dict with:
            total_keywords, keywords_with_videos, keywords_with_carousel,
            total_videos, unique_channels, channel_frequency (Counter),
            domain_frequency (Counter), platform_frequency (Counter),
            target_channel_present (bool), target_channel_keywords (list),
            topics_covered (list of common words/phrases in titles),
            per_keyword (list of per-keyword analysis dicts)
    """
    target_domain_norm = normalize_domain(target_domain)
    target_keywords = []

    channel_counter = Counter()   # channel name -> count across all videos
    domain_counter = Counter()     # domain -> count
    platform_counter = Counter()   # YouTube, TikTok, etc.
    topic_words = Counter()        # words in video titles -> topic detection
    per_keyword = []
    total_videos = 0
    keywords_with_videos = 0
    keywords_with_carousel = 0

    # Stopwords for topic extraction
    stopwords = {
        'the', 'a', 'an', 'to', 'how', 'of', 'for', 'and', 'in', 'is', 'your',
        'you', 'with', 'on', 'best', 'what', 'why', 'when', 'are', 'this',
        'it', 'my', 'me', 'we', 'our', 'be', 'do', 'does', 'can', 'will',
        'from', 'or', 'at', 'by', 'as', 'so', 'if', 'but', 'than', 'then',
    }

    for vr in video_results:
        if not vr:
            continue
        kw = vr.get('keyword', '')
        videos = vr.get('videos', [])
        has_carousel = vr.get('has_video_carousel', False)

        if videos:
            keywords_with_videos += 1
        if has_carousel:
            keywords_with_carousel += 1
        total_videos += len(videos)

        # Channels/domains/platforms for this keyword
        kw_channels = Counter()
        kw_domains = Counter()
        kw_platforms = Counter()
        target_present = False

        for v in videos:
            channel = v.get('channel', '')
            platform = v.get('platform', '')
            domain = v.get('domain', '')
            if channel:
                channel_counter[channel] += 1
                kw_channels[channel] += 1
            if domain:
                domain_counter[domain] += 1
                kw_domains[domain] += 1
            if platform:
                platform_counter[platform] += 1
                kw_platforms[platform] += 1

            # Check if target domain's channel is represented
            source_raw = (v.get('source_raw', '') or '').lower()
            url = (v.get('url', '') or '').lower()
            if (target_domain_norm and (
                target_domain_norm in source_raw
                or target_domain_norm in url
                or target_domain_norm in (channel or '').lower()
            )):
                target_present = True

            # Extract topic words from title
            title = v.get('title', '')
            if title:
                words = re.findall(r'[a-zA-Z]{3,}', title.lower())
                for w in words:
                    if w not in stopwords:
                        topic_words[w] += 1

        if target_present:
            target_keywords.append(kw)

        # Top channels for this keyword
        top_channels = kw_channels.most_common(5)
        top_domains = kw_domains.most_common(5)

        per_keyword.append({
            'keyword': kw,
            'has_carousel': has_carousel,
            'carousel_position': vr.get('carousel_position'),
            'video_count': len(videos),
            'top_channels': top_channels,
            'top_domains': top_domains,
            'platforms': dict(kw_platforms),
            'target_present': target_present,
            'video_titles': [v.get('title', '') for v in videos[:10]],
        })

    # Common topics (top 20)
    topics_covered = topic_words.most_common(20)

    return {
        'total_keywords': len([vr for vr in video_results if vr]),
        'keywords_with_videos': keywords_with_videos,
        'keywords_with_carousel': keywords_with_carousel,
        'total_videos': total_videos,
        'unique_channels': len(channel_counter),
        'channel_frequency': channel_counter,
        'domain_frequency': domain_counter,
        'platform_frequency': platform_counter,
        'target_channel_present': len(target_keywords) > 0,
        'target_channel_keywords': target_keywords,
        'topics_covered': topics_covered,
        'per_keyword': per_keyword,
        'target_domain': target_domain_norm,
    }


# ============================================================
# 3. IDENTIFY VIDEO OPPORTUNITIES
# ============================================================
def identify_video_opportunities(keywords, video_results, target_youtube_channel,
                                  search_volumes=None):
    """
    Find keywords where video carousels appear but the target channel doesn't
    have a video ranking. Score by search volume and video carousel presence.

    Args:
        keywords:              list of keyword strings
        video_results:         list of dicts from fetch_serp_videos
        target_youtube_channel: target YouTube channel name (string)
        search_volumes:        optional dict {keyword: volume}

    Returns:
        dict with:
            opportunities (list of {keyword, carousel_present, video_count,
                            search_volume, priority, reason}),
            opportunity_count,
            priority_breakdown {high, medium, low}
    """
    if search_volumes is None:
        search_volumes = {}
    target_lower = (target_youtube_channel or '').lower()

    opportunities = []
    vr_by_kw = {}
    for vr in video_results:
        if vr:
            vr_by_kw[vr.get('keyword', '')] = vr

    for kw in keywords:
        vr = vr_by_kw.get(kw)
        if not vr:
            continue

        videos = vr.get('videos', [])
        has_carousel = vr.get('has_video_carousel', False)
        video_count = len(videos)

        # Skip if no videos at all
        if video_count == 0:
            continue

        # Check if target channel is already ranking
        target_ranking = False
        for v in videos:
            channel = (v.get('channel', '') or '').lower()
            source_raw = (v.get('source_raw', '') or '').lower()
            if target_lower and target_lower in channel:
                target_ranking = True
                break
            if target_lower and target_lower in source_raw:
                target_ranking = True
                break

        if target_ranking:
            continue  # Already ranking — not an opportunity

        # Score: carousel presence + search volume
        vol = search_volumes.get(kw, 0) or 0
        score = 0
        if has_carousel:
            score += 50  # carousel presence is a strong signal
        score += min(50, vol / 100.0)  # volume capped at 50 pts

        # Priority
        if score >= 60:
            priority = 'High'
        elif score >= 30:
            priority = 'Medium'
        else:
            priority = 'Low'

        reason_parts = []
        if has_carousel:
            reason_parts.append(f"Video carousel present (position #{vr.get('carousel_position','?')})")
        else:
            reason_parts.append(f"{video_count} organic video results present")
        if vol:
            reason_parts.append(f"{vol:,} monthly searches")
        reason_parts.append(f"Target channel '{target_youtube_channel or 'N/A'}' not ranking")

        opportunities.append({
            'keyword': kw,
            'carousel_present': has_carousel,
            'carousel_position': vr.get('carousel_position'),
            'video_count': video_count,
            'search_volume': vol,
            'score': round(score, 1),
            'priority': priority,
            'reason': '; '.join(reason_parts),
        })

    # Sort by score descending
    opportunities.sort(key=lambda x: -x['score'])

    priority_breakdown = {
        'High': sum(1 for o in opportunities if o['priority'] == 'High'),
        'Medium': sum(1 for o in opportunities if o['priority'] == 'Medium'),
        'Low': sum(1 for o in opportunities if o['priority'] == 'Low'),
    }

    return {
        'opportunities': opportunities,
        'opportunity_count': len(opportunities),
        'priority_breakdown': priority_breakdown,
    }


# ============================================================
# 4. GENERATE VIDEO REPORT
# ============================================================
def _generate_carousel_frequency_svg(keywords_with_carousel, total_keywords):
    """Generate an SVG donut/gauge showing % of keywords with video carousel."""
    if total_keywords == 0:
        return '<p>No keyword data available.</p>'

    pct = round(keywords_with_carousel / total_keywords * 100, 1)
    without = total_keywords - keywords_with_carousel

    # Donut chart
    cx, cy, r = 100, 100, 70
    circumference = 2 * 3.14159 * r
    # Arc length for the carousel portion
    arc = circumference * (pct / 100.0)

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200" '
        f'style="width:100%;max-width:320px;height:auto;">',
        # Background circle (gray)
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e0e0e0" stroke-width="24"/>',
        # Foreground arc (blue) — using stroke-dasharray
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#0066cc" '
        f'stroke-width="24" stroke-dasharray="{arc:.1f},{circumference:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>',
        # Center text
        f'<text x="{cx}" y="{cy - 5}" text-anchor="middle" font-size="32" '
        f'font-weight="700" fill="#0066cc" font-family="sans-serif">{pct}%</text>',
        f'<text x="{cx}" y="{cy + 20}" text-anchor="middle" font-size="13" '
        f'fill="#666" font-family="sans-serif">Video Carousel</text>',
        # Legend
        f'<rect x="210" y="70" width="14" height="14" fill="#0066cc" rx="2"/>',
        f'<text x="232" y="82" font-size="14" fill="#1a1a1a" font-family="sans-serif">'
        f'Carousel: {keywords_with_carousel}</text>',
        f'<rect x="210" y="100" width="14" height="14" fill="#e0e0e0" rx="2"/>',
        f'<text x="232" y="112" font-size="14" fill="#1a1a1a" font-family="sans-serif">'
        f'No carousel: {without}</text>',
        f'</svg>',
    ]
    return '\n'.join(svg_parts)


def _generate_channel_bar_svg(channel_counter, top_n=10):
    """Generate an SVG horizontal bar chart of top channels by appearance count."""
    top_channels = channel_counter.most_common(top_n)
    if not top_channels:
        return '<p>No channel data available.</p>'

    chart_width = 700
    bar_height = 26
    bar_gap = 8
    label_width = 180
    value_width = 50
    chart_area_width = chart_width - label_width - value_width - 40
    max_count = max(c for _, c in top_channels) if top_channels else 1
    chart_height = len(top_channels) * (bar_height + bar_gap) + 30

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {chart_width} {chart_height}" '
        f'style="width:100%;max-width:{chart_width}px;height:auto;">',
        f'<rect width="{chart_width}" height="{chart_height}" fill="#ffffff"/>',
    ]

    y = 15
    for channel, count in top_channels:
        bar_w = int(chart_area_width * count / max_count) if max_count else 0
        label = channel if len(channel) <= 24 else channel[:22] + '…'
        color = '#0066cc' if count >= 5 else ('#4d94db' if count >= 2 else '#80b3ff')
        svg_parts.append(
            f'<rect x="{label_width + 20}" y="{y}" width="{chart_area_width}" '
            f'height="{bar_height}" fill="#f0f0f0" rx="3"/>'
        )
        svg_parts.append(
            f'<rect x="{label_width + 20}" y="{y}" width="{bar_w}" '
            f'height="{bar_height}" fill="{color}" rx="3"/>'
        )
        svg_parts.append(
            f'<text x="{label_width + 10}" y="{y + bar_height - 7}" '
            f'text-anchor="end" font-size="12" fill="#1a1a1a" font-family="sans-serif">'
            f'{_esc(label)}</text>'
        )
        svg_parts.append(
            f'<text x="{label_width + 20 + chart_area_width + 8}" y="{y + bar_height - 7}" '
            f'font-size="12" fill="#1a1a1a" font-family="sans-serif">{count}</text>'
        )
        y += bar_height + bar_gap

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def generate_video_report(keywords, video_results, analysis, opportunities,
                           output_path):
    """
    Generate an HTML report showing:
      - Summary cards
      - Video carousel frequency chart (SVG)
      - Top ranking videos table (expandable)
      - Channel analysis
      - Video opportunities table
      - Content recommendations
      - YouTube optimization tips

    Args:
        keywords:      list of keyword strings
        video_results: list of dicts from fetch_serp_videos
        analysis:      dict from analyze_video_competition
        opportunities: dict from identify_video_opportunities
        output_path:   directory path (report written as
                       <output_path>/video_seo_report.html)

    Returns:
        str: absolute path to the generated HTML report
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    total_keywords = analysis.get('total_keywords', len(keywords))
    keywords_with_carousel = analysis.get('keywords_with_carousel', 0)
    total_videos = analysis.get('total_videos', 0)
    target_present = analysis.get('target_channel_present', False)
    target_domain = analysis.get('target_domain', '')
    opp_count = opportunities.get('opportunity_count', 0)
    opp_list = opportunities.get('opportunities', [])

    # --- Summary cards ---
    carousel_pct = round(keywords_with_carousel / total_keywords * 100, 1) if total_keywords else 0
    target_present_display = 'Yes' if target_present else 'No'
    target_color = '#27ae60' if target_present else '#e74c3c'

    # --- SVG charts ---
    carousel_svg = _generate_carousel_frequency_svg(keywords_with_carousel, total_keywords)
    channel_svg = _generate_channel_bar_svg(analysis.get('channel_frequency', Counter()))

    # --- Top ranking videos table (all videos across keywords) ---
    all_videos = []
    for vr in video_results:
        if not vr:
            continue
        kw = vr.get('keyword', '')
        for v in vr.get('videos', []):
            all_videos.append({
                'keyword': kw,
                'title': v.get('title', ''),
                'channel': v.get('channel', ''),
                'platform': v.get('platform', ''),
                'url': v.get('url', ''),
                'position': v.get('position', ''),
                'rank_type': v.get('rank_type', ''),
                'duration': v.get('duration', '') or v.get('views_breadcrumb', ''),
                'source_type': v.get('source_type', ''),
            })
    # Sort by keyword then position
    all_videos.sort(key=lambda x: (x['keyword'], x['position'] if x['position'] else 999))

    video_rows = []
    for v in all_videos[:100]:  # cap at 100 rows for performance
        rank_badge = ('<span class="badge badge-carousel">Carousel</span>'
                      if v['rank_type'] == 'carousel'
                      else '<span class="badge badge-organic">Organic</span>')
        duration = v['duration'] or '—'
        video_rows.append(
            f"""<tr>
                <td><span class="kw-cell">{_esc(v['keyword'])}</span></td>
                <td>{_esc(v['title'])[:80]}</td>
                <td>{_esc(v['channel'])}</td>
                <td><a href="{_esc(v['url'])}" target="_blank" rel="nofollow noopener">{_esc(v['url'])[:60]}</a></td>
                <td class="center">#{v['position']}</td>
                <td class="center">{rank_badge}</td>
                <td class="center">{_esc(duration)}</td>
            </tr>"""
        )
    video_table_html = (
        '<table class="data-table"><thead><tr>'
        '<th>Keyword</th><th>Video Title</th><th>Channel</th>'
        '<th>URL</th><th>Position</th><th>Type</th><th>Duration/Views</th>'
        '</tr></thead><tbody>'
        + '\n'.join(video_rows) +
        '</tbody></table>'
    ) if video_rows else '<p>No video results found in SERPs.</p>'

    # --- Channel analysis ---
    channel_freq = analysis.get('channel_frequency', Counter())
    channel_rows = []
    for channel, count in channel_freq.most_common(20):
        channel_rows.append(
            f'<tr><td><strong>{_esc(channel)}</strong></td>'
            f'<td class="center">{count}</td></tr>'
        )
    channel_table_html = (
        '<table class="data-table"><thead><tr>'
        '<th>Channel</th><th>Appearances</th></tr></thead><tbody>'
        + '\n'.join(channel_rows) +
        '</tbody></table>'
    ) if channel_rows else '<p>No channel data available.</p>'

    # --- Video opportunities table ---
    opp_rows = []
    for o in opp_list:
        priority_class = 'prio-high' if o['priority'] == 'High' else (
            'prio-medium' if o['priority'] == 'Medium' else 'prio-low')
        carousel_display = '✓' if o['carousel_present'] else '—'
        vol_display = f"{o['search_volume']:,}" if o['search_volume'] else 'N/A'
        opp_rows.append(
            f"""<tr>
                <td><strong>{_esc(o['keyword'])}</strong></td>
                <td class="center">{carousel_display}</td>
                <td class="center">{o['video_count']}</td>
                <td class="center">{vol_display}</td>
                <td class="center"><span class="badge {priority_class}">{o['priority']}</span></td>
                <td>{_esc(o['reason'])}</td>
            </tr>"""
        )
    opp_table_html = (
        '<table class="data-table"><thead><tr>'
        '<th>Keyword</th><th>Carousel</th><th>Videos</th>'
        '<th>Search Volume</th><th>Priority</th><th>Reason</th>'
        '</tr></thead><tbody>'
        + '\n'.join(opp_rows) +
        '</tbody></table>'
    ) if opp_rows else '<p>No video opportunities identified.</p>'

    # --- Content recommendations ---
    topics = analysis.get('topics_covered', [])
    topic_chips = ' '.join(
        f'<span class="topic-chip">{_esc(w)}</span>' for w, _ in topics[:15]
    ) if topics else '<p>No topic data available.</p>'

    # Build content recommendations from opportunities + topics
    recs = []
    if opp_list:
        top_opp = opp_list[0]
        recs.append(
            f"<strong>Create a YouTube video targeting '{_esc(top_opp['keyword'])}'</strong> — "
            f"this keyword has {top_opp['video_count']} ranking video(s) and "
            f"{top_opp['search_volume']:,} monthly searches but your channel doesn't rank. "
            f"{'A video carousel appears at position #' + str(top_opp.get('carousel_position','?')) + ', making this a high-impact target.' if top_opp['carousel_present'] else 'Organic video results are present.'}"
        )
    if len(opp_list) > 1:
        second = opp_list[1]
        recs.append(
            f"<strong>Produce a video for '{_esc(second['keyword'])}'</strong> — "
            f"second priority opportunity with {second['video_count']} video(s) and "
            f"{second['search_volume']:,} monthly searches."
        )
    if topics:
        top_topic_words = ', '.join(w for w, _ in topics[:8])
        recs.append(
            f"<strong>Topic clusters to cover</strong> — ranking video titles frequently "
            f"feature: {top_topic_words}. Create videos addressing these themes to "
            f"capture related search demand."
        )
    recs.append(
        "<strong>Embed videos on your target pages</strong> — embed your YouTube videos "
        "on the corresponding landing pages with VideoObject schema markup to increase "
        "the chance of appearing in Google's video results."
    )
    if keywords_with_carousel > 0:
        recs.append(
            f"<strong>Prioritize carousel keywords</strong> — {keywords_with_carousel} of "
            f"{total_keywords} keywords show a video carousel. These are the highest-visibility "
            f"video placements in Google SERP."
        )
    rec_html = ''.join(f'<div class="rec-card">{r}</div>' for r in recs)

    # --- YouTube optimization tips ---
    # Analyze ranking video title patterns
    title_lengths = [len(v['title']) for v in all_videos if v['title']]
    avg_title_len = round(sum(title_lengths) / len(title_lengths), 0) if title_lengths else 0
    # Common title patterns
    how_to_count = sum(1 for v in all_videos if v['title'].lower().startswith('how '))
    best_count = sum(1 for v in all_videos if 'best' in v['title'].lower())
    diy_count = sum(1 for v in all_videos if 'diy' in v['title'].lower() or 'install' in v['title'].lower())

    tips = []
    tips.append(
        f"<strong>Title length</strong> — Ranking video titles average "
        f"{int(avg_title_len)} characters. Aim for 50-60 characters with the keyword "
        f"in the first half of the title."
    )
    if how_to_count > 0:
        tips.append(
            f"<strong>How-to format works</strong> — {how_to_count} ranking videos use "
            f"'How to...' titles. This format matches informational search intent."
        )
    if best_count > 0:
        tips.append(
            f"<strong>Best-of format works</strong> — {best_count} ranking videos use "
            f"'best' in the title, capturing commercial-intent searches."
        )
    if diy_count > 0:
        tips.append(
            f"<strong>DIY/Installation content ranks</strong> — {diy_count} videos cover "
            f"installation/DIY topics. Consider tutorial-style content."
        )
    tips.append(
        "<strong>Description optimization</strong> — Write a 200+ word description with "
        "the keyword in the first 2 sentences, include timestamps/chapters, and add "
        "3-5 relevant hashtags."
    )
    tips.append(
        "<strong>Tags</strong> — Use the target keyword as the first tag, then add "
        "variations and related terms from the topic clusters above."
    )
    tips.append(
        "<strong>Schema & embed</strong> — Add VideoObject schema on pages where you "
        "embed the video, with name, description, uploadDate, and thumbnailUrl fields."
    )
    tips_html = ''.join(f'<div class="tip-card">{t}</div>' for t in tips)

    # --- Per-keyword breakdown ---
    per_kw_rows = []
    for pk in analysis.get('per_keyword', []):
        carousel_badge = ('<span class="badge badge-carousel">Carousel</span>'
                         if pk['has_carousel'] else '—')
        channels_display = ', '.join(
            f"{_esc(ch)} ({cnt})" for ch, cnt in pk['top_channels'][:3]
        ) or '—'
        target_badge = ('<span class="badge badge-target">Present</span>'
                        if pk['target_present']
                        else '<span class="badge badge-missing">Missing</span>')
        per_kw_rows.append(
            f"""<tr>
                <td><strong>{_esc(pk['keyword'])}</strong></td>
                <td class="center">{carousel_badge}</td>
                <td class="center">{pk['video_count']}</td>
                <td>{channels_display}</td>
                <td class="center">{target_badge}</td>
            </tr>"""
        )
    per_kw_table = (
        '<table class="data-table"><thead><tr>'
        '<th>Keyword</th><th>Carousel</th><th>Videos</th>'
        '<th>Top Channels</th><th>Target Channel</th>'
        '</tr></thead><tbody>'
        + '\n'.join(per_kw_rows) +
        '</tbody></table>'
    ) if per_kw_rows else '<p>No per-keyword data available.</p>'

    # --- Build full HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Video SEO Tracker Report — {now}</title>
<style>
    body {{
        background: #fff;
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
        min-width: 160px;
        flex: 1;
        text-align: center;
    }}
    .card-value {{
        font-size: 28px;
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
    .badge {{
        display: inline-block;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
    }}
    .badge-carousel {{ background: #e8f0fe; color: #0066cc; }}
    .badge-organic {{ background: #f0f0f0; color: #666; }}
    .badge-target {{ background: #e8f8ee; color: #27ae60; }}
    .badge-missing {{ background: #fdeaea; color: #e74c3c; }}
    .prio-high {{ background: #fdeaea; color: #e74c3c; }}
    .prio-medium {{ background: #fef5e7; color: #f39c12; }}
    .prio-low {{ background: #e8f8ee; color: #27ae60; }}
    .topic-chip {{
        display: inline-block;
        background: #e8f0fe;
        color: #0066cc;
        padding: 4px 12px;
        border-radius: 16px;
        font-size: 13px;
        font-weight: 600;
        margin: 3px;
    }}
    .rec-card, .tip-card {{
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-left: 4px solid #0066cc;
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 10px;
        font-size: 15px;
        line-height: 1.6;
    }}
    .kw-cell {{
        font-size: 14px;
        font-weight: 500;
    }}
    .section {{
        margin-bottom: 32px;
    }}
    .two-col {{
        display: flex;
        gap: 24px;
        flex-wrap: wrap;
    }}
    .two-col > div {{
        flex: 1;
        min-width: 300px;
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
    <h1>Video SEO Tracker Report</h1>
    <p class="subtitle">
        Target domain: <strong>{_esc(target_domain) if target_domain else 'N/A'}</strong>
        | Keywords analyzed: <strong>{total_keywords}</strong>
        | Generated: {now}
    </p>

    <div class="section">
        <h2>Summary</h2>
        <div class="metric-row">
            <div class="metric-card"><div class="card-value">{total_keywords}</div><div class="card-label">Keywords Analyzed</div></div>
            <div class="metric-card"><div class="card-value">{keywords_with_carousel}</div><div class="card-label">Keywords with Video Carousel</div></div>
            <div class="metric-card"><div class="card-value">{total_videos}</div><div class="card-label">Total Videos Found</div></div>
            <div class="metric-card"><div class="card-value" style="color:{target_color};">{target_present_display}</div><div class="card-label">Target Channel Present</div></div>
            <div class="metric-card"><div class="card-value">{opp_count}</div><div class="card-label">Video Opportunities</div></div>
        </div>
    </div>

    <div class="section">
        <h2>Video Carousel Frequency</h2>
        <p class="subtitle">{carousel_pct}% of analyzed keywords show a video carousel in Google SERP.</p>
        <div class="chart-container">
            {carousel_svg}
        </div>
    </div>

    <div class="section">
        <h2>Top Ranking Videos</h2>
        <p class="subtitle">Videos found across all keywords, sorted by keyword and SERP position.</p>
        {video_table_html}
    </div>

    <div class="section">
        <h2>Channel Analysis</h2>
        <p class="subtitle">Which channels appear most often across all analyzed keywords.</p>
        <div class="chart-container">
            {channel_svg}
        </div>
        {channel_table_html}
    </div>

    <div class="section">
        <h2>Per-Keyword Breakdown</h2>
        <p class="subtitle">Video presence and target channel representation per keyword.</p>
        {per_kw_table}
    </div>

    <div class="section">
        <h2>Video Opportunities</h2>
        <p class="subtitle">Keywords where videos appear in SERP but the target channel doesn't rank.</p>
        {opp_table_html}
    </div>

    <div class="section">
        <h2>Content Recommendations</h2>
        <h3>Topic Clusters from Ranking Videos</h3>
        <div style="margin-bottom:16px;">{topic_chips}</div>
        {rec_html}
    </div>

    <div class="section">
        <h2>YouTube Optimization Tips</h2>
        <p class="subtitle">Title, description, tag, and schema recommendations based on ranking video patterns.</p>
        {tips_html}
    </div>

    <footer>
        <p>Video SEO Tracker Report | Generated by Hermes Agent | DataForSEO SERP API</p>
        <p>This report is marked <code>noindex,nofollow</code> and is not indexed by search engines.</p>
    </footer>
</body>
</html>"""

    # Write report
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, 'video_seo_report.html')
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  ✓ Report saved to: {report_file}")
    return os.path.abspath(report_file)


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Track video SEO presence in Google SERP results.'
    )
    parser.add_argument(
        '--keywords', required=True,
        help='Comma-separated list of keywords to analyze'
    )
    parser.add_argument(
        '--domain', required=True,
        help='Target domain (e.g., softprowatersystems.com)'
    )
    parser.add_argument(
        '--channel', default='',
        help='Target YouTube channel name (default: derived from domain)'
    )
    parser.add_argument(
        '--location', type=int, default=DEFAULT_LOCATION_CODE,
        help=f'DataForSEO location code (default {DEFAULT_LOCATION_CODE} = US)'
    )
    parser.add_argument(
        '--output', required=True,
        help='Output directory for the HTML report'
    )
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(',') if k.strip()]
    if not keywords:
        print("ERROR: No keywords provided.")
        sys.exit(1)

    target_domain = args.domain.strip()
    # Derive channel name from domain if not provided
    target_channel = args.channel.strip()
    if not target_channel:
        target_channel = target_domain.split('.')[0] if '.' in target_domain else target_domain

    print(f"\n{'='*60}")
    print(f"Video SEO Tracker")
    print(f"{'='*60}")
    print(f"Keywords: {len(keywords)}")
    print(f"Target domain: {target_domain}")
    print(f"Target channel: {target_channel}")
    print(f"Output: {args.output}")
    print()

    # Step 1: Fetch SERP videos for each keyword
    print("--- Step 1: Fetching SERP video data ---")
    video_results = []
    for kw in keywords:
        vr = fetch_serp_videos(kw, args.location)
        video_results.append(vr)

    # Fetch search volumes for opportunity scoring
    print("\n--- Fetching search volumes ---")
    search_volumes = fetch_search_volumes(keywords, args.location)
    if search_volumes:
        print(f"  ✓ Got volumes for {len(search_volumes)} keywords")
    else:
        print("  ⚠ No search volume data (continuing without)")

    # Step 2: Analyze video competition
    print("\n--- Step 2: Analyzing video competition ---")
    analysis = analyze_video_competition(video_results, target_domain)
    print(f"  Keywords with videos: {analysis['keywords_with_videos']}")
    print(f"  Keywords with carousel: {analysis['keywords_with_carousel']}")
    print(f"  Total videos found: {analysis['total_videos']}")
    print(f"  Unique channels: {analysis['unique_channels']}")
    print(f"  Target channel present: {analysis['target_channel_present']}")

    # Step 3: Identify video opportunities
    print("\n--- Step 3: Identifying video opportunities ---")
    opportunities = identify_video_opportunities(
        keywords, video_results, target_channel, search_volumes
    )
    print(f"  Opportunities found: {opportunities['opportunity_count']}")
    pb = opportunities['priority_breakdown']
    print(f"    High: {pb['High']}, Medium: {pb['Medium']}, Low: {pb['Low']}")

    # Step 4: Generate report
    print("\n--- Step 4: Generating report ---")
    report_path = generate_video_report(
        keywords, video_results, analysis, opportunities, args.output
    )

    print(f"\n{'='*60}")
    print(f"DONE — Report: {report_path}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
