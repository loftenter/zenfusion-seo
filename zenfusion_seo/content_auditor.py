#!/usr/bin/env python3
"""
Zenfusion Content Auditor v1.0 — Content SEO analysis (Search Atlas + POP inspired).
Analyzes a single page URL + target keyword and produces a 0-100 content score
across 6 dimensions: Content Hierarchy, Factuality, Information Gain,
Authority/E-E-A-T, Accuracy/Title-to-Query Alignment, Content Format.

Usage: python3 content_auditor.py <url> <keyword> [--output path]
"""
import json, re, os, sys, math, csv
from collections import Counter
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Requires: pip3 install requests beautifulsoup4")
    sys.exit(1)

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

# Target ranges (from Search Atlas)
TARGETS = {
    "words": (900, 2500),
    "h1": (0, 1),
    "h2": (2, 20),
    "paragraphs": (5, 30),
    "internal_links": (10, 100),
    "external_links": (0, 20),
}

# Authority domains
AUTHORITY_TLDS = [".gov", ".edu", ".org"]
TRUST_SIGNALS = ["trusted by", "certified", "licensed", "award", "testimonial", "guaranteed", "verified"]
SOCIAL_DOMAINS = ["facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "tiktok.com", "pinterest.com"]
CITATION_PATTERNS = [r"according to", r"study shows", r"research indicates", r"source:", r"data from", r"reported by", r"published in"]
POSITIVE_WORDS = ["excellent", "best", "top", "premium", "quality", "trusted", "proven", "guaranteed", "expert", "professional"]
NEGATIVE_WORDS = ["worst", "terrible", "avoid", "scam", "fail", "broken", "poor", "cheap"]

# ============================================================================
# PAGE FETCH
# ============================================================================
def fetch_page(url):
    """Fetch page HTML — try requests first, ScrapeOwl as fallback."""
    try:
        resp = requests.get(url, timeout=30, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.text, resp.status_code
    except Exception:
        pass

    # ScrapeOwl fallback
    api_key = ENV.get("SCRAPEOWL_API_KEY", "")
    if api_key:
        try:
            resp = requests.post("https://api.scrapeowl.com/v1/", json={"api_key": api_key, "url": url}, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", ""), 200
        except Exception:
            pass
    return "", 0

# ============================================================================
# SCORING DIMENSIONS
# ============================================================================
def score_content_hierarchy(soup, title, keyword, url=None):
    """Score heading structure (0-100)."""
    score = 100
    issues = []
    h1s = soup.find_all("h1")
    h2s = soup.find_all("h2")
    h3s = soup.find_all("h3")

    if len(h1s) == 0:
        score -= 20; issues.append(("red", "No H1 tag", "Add an H1 tag containing the target keyword"))
    elif len(h1s) > 1:
        score -= 15; issues.append(("yellow", f"{len(h1s)} H1 tags found", "Use exactly 1 H1 per page"))

    if len(h2s) < 2:
        score -= 10; issues.append(("yellow", f"Only {len(h2s)} H2 tags (target: 2-20)", "Add more H2 sections"))
    elif len(h2s) > 20:
        score -= 10; issues.append(("yellow", f"{len(h2s)} H2 tags (target: 2-20)", "Consolidate some H2 sections"))

    # Check hierarchy skips
    prev_level = 0
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        level = int(tag.name[1])
        if prev_level > 0 and level > prev_level + 1:
            score -= 5; issues.append(("yellow", f"H{prev_level} to H{level} skip", "Fix heading hierarchy"))
        prev_level = level

    # H1 = title check
    if h1s and title:
        h1_text = h1s[0].get_text(strip=True).lower()
        if h1_text == title.lower():
            pass  # Good
        elif keyword and keyword.lower() in h1_text:
            score += 0  # OK
        else:
            score -= 5; issues.append(("yellow", "H1 doesn't contain keyword", f"Add '{keyword}' to H1"))

    # H1 starts lowercase
    if h1s:
        h1_text = h1s[0].get_text(strip=True)
        if h1_text and h1_text[0].islower():
            score -= 5; issues.append(("yellow", "H1 starts lowercase", "Capitalize first letter"))

    return max(0, min(100, score)), issues


def score_factuality(soup, text):
    """Score citations, data sources, authority links (0-100)."""
    score = 0
    issues = []
    max_score = 100

    # Citations — check <cite>, <blockquote>, AND hyperlinked external authority links
    cites = soup.find_all("cite")
    blockquotes = soup.find_all("blockquote")
    
    # Also count hyperlinked external links to authority domains (.gov/.edu/.org)
    # AND social media domains (youtube, facebook, instagram, twitter, linkedin)
    external_auth_links = 0
    social_domains = {'youtube.com', 'youtu.be', 'facebook.com', 'instagram.com', 
                      'twitter.com', 'x.com', 'linkedin.com', 'tiktok.com', 'pinterest.com'}
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if href.startswith("http"):
            domain = urlparse(href).netloc.lower().replace("www.", "")
            if any(tld in domain for tld in AUTHORITY_TLDS):
                external_auth_links += 1
            elif any(social in domain for social in social_domains):
                external_auth_links += 1
    
    citation_count = len(cites) + len(blockquotes) + external_auth_links
    if citation_count > 0:
        score += 20
    else:
        issues.append(("yellow", "No citations found", "Add blockquotes or citations to support claims"))

    # Data source patterns
    text_lower = text.lower()
    found_patterns = sum(1 for p in CITATION_PATTERNS if re.search(p, text_lower))
    if found_patterns >= 2:
        score += 25
    elif found_patterns == 1:
        score += 15
    else:
        issues.append(("yellow", "No data source references", "Add phrases like 'according to' or 'study shows'"))

    # Authority outbound links
    links = soup.find_all("a", href=True)
    auth_links = 0
    for link in links:
        href = link.get("href", "")
        domain = urlparse(href).netloc.lower()
        if any(tld in domain for tld in AUTHORITY_TLDS):
            auth_links += 1
    if 1 <= auth_links <= 3:
        score += 20
    elif auth_links > 3:
        score += 15
    else:
        issues.append(("yellow", "No authority links (.gov/.edu/.org)", "Add 1-3 links to authoritative sources"))

    # Statistics/data points
    stats = len(re.findall(r'\$\d|\d+%|\d+\.\d+%|\d+ percent', text))
    if stats >= 3:
        score += 15
    elif stats >= 1:
        score += 8

    # Author attribution — check schema, itemprop, rel=author, and byline patterns
    author_tags = soup.find_all(attrs={"itemprop": "author"}) or soup.find_all("a", rel="author")
    
    # Check Person schema in JSON-LD
    scripts = soup.find_all("script", type="application/ld+json")
    has_person_schema = False
    for s in scripts:
        schema_text = str(s).lower()
        if '"person"' in schema_text or '"author"' in schema_text or '"name"' in schema_text:
            has_person_schema = True
            break
    
    # Broader byline search — check full text, not just first 500 chars
    # Patterns: "By [Name]", "Written by [Name]", "Author: [Name]"
    author_patterns = [
        r'by\s+[A-Z][a-z]+(?:\s+[A-Z\'\w]+)+',  # "By Craig 'The Water Guy' Phillips"
        r'written\s+by\s+[A-Z]',
        r'author:\s*[A-Z]',
        r'founder,?\s+[A-Z]',
        r'contributed\s+by',
        r'reviewed\s+by',
    ]
    has_byline = any(re.search(p, text[:3000], re.I) for p in author_patterns)
    
    if author_tags or has_person_schema or has_byline:
        score += 10
    else:
        issues.append(("yellow", "No author attribution", "Add author name or bio"))

    # Date/schema
    scripts = soup.find_all("script", type="application/ld+json")
    has_date = False
    for s in scripts:
        if "datePublished" in str(s) or "dateModified" in str(s):
            has_date = True
            score += 10
            break
    if not has_date:
        issues.append(("yellow", "No datePublished schema", "Add datePublished to JSON-LD"))

    return min(100, score), issues


def score_information_gain(soup, text, keyword):
    """Score content uniqueness and depth (0-100)."""
    score = 0
    issues = []

    # Strip boilerplate (nav, footer, header, script, style)
    main_soup = BeautifulSoup(str(soup), "html.parser")
    for tag in main_soup.find_all(["nav", "footer", "header", "script", "style", "noscript"]):
        tag.decompose()
    main_text = main_soup.get_text(strip=True)
    main_words = main_text.split()

    total_words = len(text.split())
    if total_words == 0:
        return 0, [("red", "No content found", "Add page content")]

    # Unique content ratio
    boilerplate_ratio = 1 - (len(main_words) / max(1, total_words))
    unique_ratio = 1 - boilerplate_ratio
    if unique_ratio > 0.6:
        score += 30
    elif unique_ratio > 0.4:
        score += 15
    else:
        issues.append(("yellow", f"High boilerplate ratio ({boilerplate_ratio:.0%})", "Add more original content"))

    # Keyword density
    if keyword:
        text_lower = text.lower()
        keyword_lower = keyword.lower()
        # Count occurrences of the full keyword phrase in the text
        keyword_count = text_lower.count(keyword_lower)
        density = (keyword_count / max(1, len(text_lower.split()))) * 100
        if 0.5 <= density <= 3:
            score += 25
        elif density > 3:
            score += 5
            issues.append(("red", f"Keyword density {density:.1f}% (too high)", "Reduce keyword stuffing"))
        elif density > 0:
            score += 15
        else:
            score += 0
            issues.append(("yellow", "Keyword not found in content", f"Add '{keyword}' to the content"))
    else:
        score += 25

    # Paragraph depth
    paragraphs = soup.find_all("p")
    short_paragraphs = sum(1 for p in paragraphs if len(p.get_text().split()) < 3)
    if paragraphs:
        avg_sentences = sum(len(re.split(r'[.!?]+', p.get_text())) for p in paragraphs) / len(paragraphs)
        if 3 <= avg_sentences <= 7:
            score += 25
        elif avg_sentences < 3:
            score += 10
            issues.append(("yellow", "Short paragraphs", "Expand paragraphs to 3-7 sentences"))
        else:
            score += 15
    else:
        score += 0
        issues.append(("yellow", "No paragraph tags", "Use <p> tags for content"))

    # Unique bigrams
    words = text.lower().split()
    bigrams = set()
    for i in range(len(words) - 1):
        bigrams.add((words[i], words[i+1]))
    if len(bigrams) > 100:
        score += 20
    elif len(bigrams) > 50:
        score += 10
    else:
        score += 5

    return min(100, score), issues


def score_authority(soup, url, text):
    """Score E-E-A-T signals (0-100)."""
    score = 0
    issues = []

    # HTTPS
    if url.startswith("https://"):
        score += 10
    else:
        issues.append(("red", "Not HTTPS", "Switch to HTTPS"))

    # Schema types
    scripts = soup.find_all("script", type="application/ld+json")
    schema_types = []
    for s in scripts:
        try:
            data = json.loads(s.string)
            if isinstance(data, list):
                for item in data:
                    t = item.get("@type", "")
                    if isinstance(t, str):
                        schema_types.append(t)
                    elif isinstance(t, list):
                        schema_types.extend(t)
            elif isinstance(data, dict):
                t = data.get("@type", "")
                if isinstance(t, str):
                    schema_types.append(t)
                elif isinstance(t, list):
                    schema_types.extend(t)
        except Exception:
            pass

    critical_schemas = ["Article", "Organization", "Author", "FAQPage", "Review", "BreadcrumbList"]
    found_schemas = [s for s in schema_types if s in critical_schemas]
    if len(found_schemas) >= 3:
        score += 30
    elif len(found_schemas) >= 1:
        score += 15
    else:
        issues.append(("red", "No critical schema types", "Add Article + Organization + Author schema"))

    missing = [s for s in critical_schemas if s not in found_schemas]
    if missing:
        issues.append(("yellow", f"Missing schema: {', '.join(missing[:3])}", "Add missing JSON-LD schema"))

    # Author info
    author = soup.find(attrs={"itemprop": "author"}) or soup.find("a", rel="author")
    if author:
        score += 10
    else:
        issues.append(("yellow", "No author info", "Add author attribution"))

    # About page
    about = soup.find("a", href=re.compile(r"/about", re.I))
    if about:
        score += 10
    else:
        issues.append(("yellow", "No About page link", "Add link to About page"))

    # Contact info
    contact = soup.find("a", href=re.compile(r"/contact", re.I))
    if contact:
        score += 10
    else:
        issues.append(("yellow", "No Contact link", "Add contact information"))

    # Privacy policy
    privacy = soup.find("a", href=re.compile(r"/privacy", re.I))
    if privacy:
        score += 5

    # Social profiles
    links = soup.find_all("a", href=True)
    social_found = 0
    for link in links:
        href = link.get("href", "").lower()
        if any(s in href for s in SOCIAL_DOMAINS):
            social_found += 1
    if social_found >= 2:
        score += 15
    elif social_found >= 1:
        score += 8
    else:
        issues.append(("yellow", "No social media links", "Add social profile links"))

    # Trust signals
    text_lower = text.lower()
    trust_found = sum(1 for s in TRUST_SIGNALS if s in text_lower)
    if trust_found >= 2:
        score += 10
    elif trust_found >= 1:
        score += 5

    return min(100, score), issues, schema_types


def score_accuracy(soup, title, meta_desc, url, text, keyword):
    """Score title-to-query alignment (0-100)."""
    score = 0
    issues = []

    if not keyword:
        return 50, [("yellow", "No keyword provided", "Specify a target keyword")]

    keyword_lower = keyword.lower()

    # Keyword in title
    if title:
        title_lower = title.lower()
        if keyword_lower == title_lower:
            score += 25
        elif keyword_lower in title_lower:
            score += 18
        else:
            # Check partial (all words present)
            kw_words = keyword_lower.split()
            if all(w in title_lower for w in kw_words):
                score += 15
            else:
                issues.append(("red", "Keyword not in title tag", f"Add '{keyword}' to title"))
                score += 0
    else:
        issues.append(("red", "No title tag", "Add a title tag"))
        score += 0

    # Keyword in H1
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(strip=True).lower()
        if keyword_lower in h1_text:
            score += 20
        elif all(w in h1_text for w in keyword_lower.split()):
            score += 12
        else:
            issues.append(("red", "Keyword not in H1", f"Add '{keyword}' to H1"))
    else:
        issues.append(("red", "No H1 tag", "Add H1 with keyword"))
        score += 0

    # Keyword in URL
        if keyword_lower.replace(" ", "-") in url.lower() or keyword_lower.replace(" ", "") in url.lower():
            score += 5
        else:
            # Don't recommend changing URL if the page is already ranking — changing URLs
            # on a ranking page can cause redirects, lose link equity, and drop rankings
            issues.append(("yellow", "Keyword not in URL", f"URL doesn't contain '{keyword}'. NOTE: Do NOT change the URL if this page is already ranking — URL changes risk losing rankings and link equity. Instead, focus on title tag, H1, and content optimization."))

    # Keyword in meta description
    if meta_desc and keyword_lower in meta_desc.lower():
        score += 10
    else:
        issues.append(("yellow", "Keyword not in meta description", f"Add '{keyword}' to meta description"))

    # Keyword in first paragraph — find first CONTENT <p> (skip nav/banner/product cards)
    first_p = None
    for p in soup.find_all("p"):
        p_text = p.get_text(strip=True)
        # Skip very short paragraphs (banners, nav text)
        if len(p_text) < 50:
            continue
        # Skip if parent or any ancestor is <a> (product card text, not article content)
        ancestor = p.parent
        while ancestor:
            if ancestor.name == 'a':
                break
            ancestor = ancestor.parent
        else:
            # Also skip if parent is nav/header/footer
            parent = p.parent
            if parent and parent.name in ['nav', 'header', 'footer']:
                continue
            # Skip review/testimonial blocks (often start with quotes — straight or curly)
            if p_text[0] in '"\u201c\u201d\u2018\u2019\'':
                continue
            first_p = p
            break
    
    if first_p and keyword_lower in first_p.get_text().lower():
        score += 10
    else:
        issues.append(("yellow", "Keyword not in first paragraph", f"Add '{keyword}' early in content"))

    # Title length
    if title:
        title_len = len(title)
        if 30 <= title_len <= 60:
            score += 10
        else:
            issues.append(("yellow", f"Title length {title_len} (target 30-60)", "Adjust title length"))
            score += 5
    else:
        score += 0

    # Meta description length
    if meta_desc:
        desc_len = len(meta_desc)
        if 120 <= desc_len <= 160:
            score += 10
        else:
            issues.append(("yellow", f"Meta description length {desc_len} (target 120-160)", "Adjust meta description"))
            score += 5
    else:
        issues.append(("red", "No meta description", "Add a meta description"))
        score += 0

    return min(100, score), issues


def score_content_format(soup, text, url=None):
    """Score content format metrics (0-100)."""
    score = 100
    issues = []

    words = len(text.split())
    if words < TARGETS["words"][0]:
        score -= 25; issues.append(("red", f"Word count {words} (target {TARGETS['words'][0]}-{TARGETS['words'][1]})", f"Add {TARGETS['words'][0]-words} more words"))
    elif words > TARGETS["words"][1]:
        score -= 10; issues.append(("yellow", f"Word count {words} (target {TARGETS['words'][0]}-{TARGETS['words'][1]})", "Consider trimming content"))
    else:
        pass  # In range

    paragraphs = soup.find_all("p")
    para_count = len(paragraphs)
    if para_count < TARGETS["paragraphs"][0]:
        score -= 10; issues.append(("yellow", f"{para_count} paragraphs (target {TARGETS['paragraphs'][0]}-{TARGETS['paragraphs'][1]})", "Add more content paragraphs"))
    elif para_count > TARGETS["paragraphs"][1]:
        score -= 10; issues.append(("yellow", f"{para_count} paragraphs (target {TARGETS['paragraphs'][0]}-{TARGETS['paragraphs'][1]})", "Merge short paragraphs"))

    links = soup.find_all("a", href=True)
    parsed_url = None
    page_domain = ""
    if url:
        try:
            page_domain = urlparse(url).netloc.lower().replace("www.", "")
        except Exception:
            pass
    internal_count = 0
    external_count = 0
    _seen_hrefs = set()
    for link in links:
        href = link.get("href", "")
        if not href or href in _seen_hrefs:
            continue
        _seen_hrefs.add(href)
        if href.startswith("http"):
            # Check if this is actually an internal link with a full URL
            try:
                href_domain = urlparse(href).netloc.lower().replace("www.", "")
            except Exception:
                href_domain = ""
            if page_domain and href_domain == page_domain:
                internal_count += 1
            else:
                external_count += 1
        elif href.startswith("/"):
            internal_count += 1

    if internal_count < TARGETS["internal_links"][0]:
        score -= 10; issues.append(("yellow", f"Internal links {internal_count} (target {TARGETS['internal_links'][0]}-{TARGETS['internal_links'][1]})", "Add internal links to related pages"))
    elif internal_count > TARGETS["internal_links"][1]:
        score -= 5; issues.append(("yellow", f"Internal links {internal_count} (target {TARGETS['internal_links'][0]}-{TARGETS['internal_links'][1]})", "Trim low-value internal links"))

    if external_count > TARGETS["external_links"][1]:
        score -= 5; issues.append(("yellow", f"External links {external_count} (target {TARGETS['external_links'][0]}-{TARGETS['external_links'][1]})", "Review external links"))

    # Images
    images = soup.find_all("img")
    if len(images) >= 1:
        score += 0  # In range
    else:
        score -= 10; issues.append(("yellow", "No images", "Add at least 1 image"))

    # Lists
    lists = soup.find_all(["ul", "ol"])
    if len(lists) >= 1:
        score += 0  # Good
    else:
        issues.append(("yellow", "No lists", "Consider adding a list for readability"))

    # Tables — check for both HTML tables AND div-based comparison sections
    tables = soup.find_all("table")
    # Also check for div-based comparison sections (common in Shopify themes)
    div_comparisons = soup.find_all("div", class_=re.compile(r"comparison|compare|vs-table|spec-table|product-comparison", re.I))
    div_comparisons += soup.find_all("section", class_=re.compile(r"comparison|compare", re.I))
    if len(tables) >= 1 or len(div_comparisons) >= 1:
        score += 0  # Good
    else:
        issues.append(("yellow", "No tables", "Consider adding a comparison table or spec table for better content structure"))

    # Multimedia
    iframes = soup.find_all("iframe")
    videos = soup.find_all("video")
    if len(iframes) + len(videos) >= 1:
        score += 0  # Bonus
    else:
        issues.append(("yellow", "No multimedia", "Consider adding video content"))

    return max(0, min(100, score)), issues


# ============================================================================
# STRUCTURE METRICS
# ============================================================================
def get_structure_metrics(soup, text, keyword, url=None):
    """Get all structure metrics with target ranges."""
    words = len(text.split())
    h1_count = len(soup.find_all("h1"))
    h2_count = len(soup.find_all("h2"))
    paragraphs = len(soup.find_all("p"))
    links = soup.find_all("a", href=True)
    # Determine the page's own domain so full-URL internal links (e.g.
    # https://www.softprowatersystems.com/...) are NOT counted as external.
    page_domain = ""
    if url:
        try:
            page_domain = urlparse(url).netloc.lower().replace("www.", "")
        except Exception:
            pass
    # Deduplicate by href so the same URL appearing N times only counts once
    _seen_hrefs = set()
    _internal = 0
    _external = 0
    for l in links:
        href = l.get("href", "")
        if not href or href in _seen_hrefs:
            continue
        _seen_hrefs.add(href)
        if href.startswith("/") or href.startswith("#"):
            _internal += 1
        elif href.startswith("http"):
            # Check if this is actually an internal link with a full URL
            try:
                href_domain = urlparse(href).netloc.lower().replace("www.", "")
            except Exception:
                href_domain = ""
            if page_domain and href_domain == page_domain:
                _internal += 1
            else:
                _external += 1
    internal_links = _internal
    external_links = _external
    citations = len(soup.find_all("cite")) + len(soup.find_all("blockquote"))
    entities = len(set(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text[:5000])))  # Simple entity extraction
    lists = len(soup.find_all(["ul", "ol"]))
    tables = len(soup.find_all("table"))

    metrics = [
        ("Words", words, TARGETS["words"]),
        ("Headings 1", h1_count, TARGETS["h1"]),
        ("Headings 2", h2_count, TARGETS["h2"]),
        ("Paragraphs", paragraphs, TARGETS["paragraphs"]),
        ("Internal links", internal_links, TARGETS["internal_links"]),
        ("External links", external_links, TARGETS["external_links"]),
        ("Citations", citations, None),
        ("Entities", entities, None),
        ("Lists", lists, None),
        ("Tables", tables, None),
    ]

    # Add in-range indicators
    result = []
    for name, value, target in metrics:
        if target:
            in_range = target[0] <= value <= target[1]
            result.append({"name": name, "value": value, "target": f"{target[0]}-{target[1]}", "in_range": in_range})
        else:
            result.append({"name": name, "value": value, "target": "No target", "in_range": None})

    return result


# ============================================================================
# TONE ASSESSMENT
# ============================================================================
def assess_tone(text, issues):
    """Simple tone classification."""
    text_lower = text.lower()
    positive = sum(1 for w in POSITIVE_WORDS if w in text_lower)
    negative = sum(1 for w in NEGATIVE_WORDS if w in text_lower)

    if positive > negative:
        sentiment = "Positive"
    elif negative > positive:
        sentiment = "Negative"
    else:
        sentiment = "Neutral"

    # Strong if has citations, authority signals, data
    factual_issues = [i for i in issues if i[0] == "red"]
    if len(factual_issues) < 2:
        tone = "Strong"
    else:
        tone = "Weak"

    return {"sentiment": sentiment, "tone": tone}


# ============================================================================
# KEYWORD TRACKING
# ============================================================================
def track_keyword(text, keyword):
    """Track keyword usage: On Track / To Add / Over-Used."""
    if not keyword:
        return {"on_track": [], "to_add": [keyword or "No keyword"], "over_used": []}

    text_lower = text.lower()
    keyword_lower = keyword.lower()
    words = text_lower.split()
    word_count = len(words)
    keyword_count = text_lower.count(keyword_lower)
    density = (keyword_count / max(1, word_count)) * 100

    # Related terms (simple: variations of keyword words)
    kw_words = keyword_lower.split()
    related = []
    for w in kw_words:
        if w.endswith("s"):
            related.append(w[:-1])
        else:
            related.append(w + "s")

    on_track = []
    to_add = []
    over_used = []

    if keyword_count > 0 and density <= 3:
        on_track.append(keyword)
    elif density > 3:
        over_used.append(f"{keyword} ({density:.1f}% density)")
    else:
        to_add.append(keyword)

    # Check related terms
    for r in related:
        if r in text_lower and r not in keyword_lower:
            on_track.append(r)
        else:
            to_add.append(r)

    return {"on_track": on_track, "to_add": to_add, "over_used": over_used}


# ============================================================================
# SCHEMA ANALYSIS
# ============================================================================
def analyze_schema(soup):
    """Detect and score JSON-LD schema."""
    scripts = soup.find_all("script", type="application/ld+json")
    types_found = []
    for s in scripts:
        try:
            data = json.loads(s.string)
            if isinstance(data, list):
                for item in data:
                    t = item.get("@type", "")
                    if isinstance(t, str):
                        types_found.append(t)
                    elif isinstance(t, list):
                        types_found.extend(t)
            elif isinstance(data, dict):
                t = data.get("@type", "")
                if isinstance(t, str):
                    types_found.append(t)
                elif isinstance(t, list):
                    types_found.extend(t)
        except Exception:
            pass

    critical = ["Article", "Organization", "Author", "FAQPage", "Review", "BreadcrumbList"]
    found = [t for t in types_found if t in critical]
    missing = [t for t in critical if t not in found]
    score = min(100, len(found) * 20)

    return {"types_found": types_found, "critical_found": found, "missing": missing, "score": score}


# ============================================================================
# MAIN ANALYZE
# ============================================================================
def analyze_page(url, keyword):
    """Main analysis function."""
    print(f"Fetching {url}...")
    html, status = fetch_page(url)
    if not html:
        print(f"Failed to fetch {url}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title").get_text(strip=True) if soup.find("title") else ""
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag.get("content", "") if meta_desc_tag else ""
    text = soup.get_text(" ", strip=True)

    print(f"Title: {title}")
    print(f"Word count: {len(text.split())}")
    print(f"Analyzing 6 dimensions...")

    # Score all dimensions
    hierarchy_score, hierarchy_issues = score_content_hierarchy(soup, title, keyword, url)
    factuality_score, factuality_issues = score_factuality(soup, text)
    info_gain_score, info_gain_issues = score_information_gain(soup, text, keyword)
    authority_score, authority_issues, schema_types = score_authority(soup, url, text)
    accuracy_score, accuracy_issues = score_accuracy(soup, title, meta_desc, url, text, keyword)
    format_score, format_issues = score_content_format(soup, text, url)

    # Overall score
    overall = round((hierarchy_score + factuality_score + info_gain_score + authority_score + accuracy_score + format_score) / 6)

    # Structure metrics
    structure = get_structure_metrics(soup, text, keyword, url)

    # Tone
    all_issues = hierarchy_issues + factuality_issues + info_gain_issues + authority_issues + accuracy_issues + format_issues
    tone = assess_tone(text, all_issues)

    # Keyword tracking
    topics = track_keyword(text, keyword)

    # Schema analysis
    schema = analyze_schema(soup)

    # Needs attention (deduplicate issues)
    needs_attention = []
    seen = set()
    for severity, name, fix in all_issues:
        if name not in seen:
            needs_attention.append({"severity": severity, "issue": name, "fix": fix})
            seen.add(name)

    result = {
        "url": url,
        "keyword": keyword,
        "title": title,
        "meta_description": meta_desc,
        "word_count": len(text.split()),
        "overall_score": overall,
        "scores": {
            "content_hierarchy": hierarchy_score,
            "factuality": factuality_score,
            "information_gain": info_gain_score,
            "authority_e_e_a_t": authority_score,
            "accuracy_title_alignment": accuracy_score,
            "content_format": format_score,
        },
        "structure_metrics": structure,
        "tone": tone,
        "topics": topics,
        "schema": schema,
        "needs_attention": needs_attention,
    }

    print(f"\nOverall Content Score: {overall}/100")
    print(f"  Hierarchy: {hierarchy_score} | Factuality: {factuality_score} | Info Gain: {info_gain_score}")
    print(f"  Authority: {authority_score} | Accuracy: {accuracy_score} | Format: {format_score}")
    print(f"  Tone: {tone['tone']} ({tone['sentiment']})")
    print(f"  Issues: {len(needs_attention)}")
    print(f"  Schema types: {schema_types}")

    return result


# ============================================================================
# HTML REPORT
# ============================================================================
def generate_html_report(result):
    """Generate HTML report matching Zenfusion style."""
    score = result["overall_score"]
    scores = result["scores"]
    color = "#27ae60" if score >= 70 else ("#f39c12" if score >= 40 else "#e74c3c")

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta name="robots" content="noindex, nofollow">
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Content Audit — {result['keyword']} — {result['url'][:30]}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f1117; color: #ffffff; padding: 40px 20px; max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin-bottom: 4px; }}
.subtitle {{ color: #ffffff; opacity: 0.85; font-size: 14px; margin-bottom: 30px; }}
.back-link {{ color: #4A90D9; text-decoration: none; font-size: 13px; margin-bottom: 20px; display: inline-block; }}
.section {{ margin-bottom: 30px; }}
.section-title {{ color: #4A90D9; font-size: 18px; margin-bottom: 14px; border-bottom: 1px solid #2a3142; padding-bottom: 8px; }}
.score-gauge {{ text-align: center; margin-bottom: 30px; }}
.score-value {{ font-size: 56px; font-weight: 700; color: {color}; }}
.score-label {{ font-size: 14px; color: #ffffff; opacity: 0.85; }}
.score-breakdown {{ display: grid; gap: 10px; }}
.score-bar {{ display: flex; align-items: center; gap: 10px; }}
.score-bar .name {{ width: 180px; font-size: 13px; color: #ffffff; opacity: 0.85; }}
.score-bar .bar {{ flex: 1; height: 24px; background: #1a1f2e; border-radius: 12px; overflow: hidden; }}
.score-bar .fill {{ height: 100%; border-radius: 12px; transition: width 0.5s; }}
.score-bar .num {{ width: 40px; text-align: right; font-size: 13px; font-weight: 600; }}
.green-bg {{ background: #27ae60; }}
.orange-bg {{ background: #f39c12; }}
.red-bg {{ background: #e74c3c; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ background: #041952; color: white; padding: 8px; text-align: left; font-size: 12px; }}
td {{ padding: 8px; border-bottom: 1px solid #2a3142; }}
.in-range {{ color: #27ae60; }}
.out-of-range {{ color: #f39c12; }}
.issue-card {{ background: #1a1f2e; border: 1px solid #2a3142; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; }}
.issue-card .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }}
.dot-red {{ background: #e74c3c; }}
.dot-yellow {{ background: #f39c12; }}
.dot-green {{ background: #27ae60; }}
.issue-card .name {{ font-weight: 600; color: #ffffff; }}
.issue-card .fix {{ font-size: 12px; color: #ffffff; opacity: 0.7; margin-top: 4px; }}
.topic-badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; margin: 2px; }}
.topic-on-track {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.topic-to-add {{ background: rgba(243,156,18,0.15); color: #f39c12; }}
.topic-over-used {{ background: rgba(231,76,60,0.15); color: #e74c3c; }}
.footer {{ text-align: center; margin-top: 40px; color: #ffffff; opacity: 0.4; font-size: 11px; border-top: 1px solid #2a3142; padding-top: 20px; }}
.recommendation {{ background: rgba(74,144,217,0.1); border-left: 3px solid #4A90D9; padding: 12px 16px; margin: 8px 0; border-radius: 4px; font-size: 13px; color: #ffffff; opacity: 0.95; }}
.recommendation strong {{ color: #4A90D9; }}
</style>
</head>
<body>

<a href="/" class="back-link">&larr; Back</a>
<h1>Content Audit — {result['keyword']}</h1>
<p class="subtitle">{result['url']}</p>

<div class="score-gauge">
  <div class="score-value">{score}</div>
  <div class="score-label">Overall Content Score (0-100)</div>
</div>

<div class="section">
  <div class="section-title">Score Breakdown</div>
  <div class="score-breakdown">
'''

    for dim_name, dim_score in scores.items():
        dim_label = dim_name.replace("_", " ").title()
        color = "green-bg" if dim_score >= 70 else ("orange-bg" if dim_score >= 40 else "red-bg")
        html += f'<div class="score-bar"><div class="name">{dim_label}</div><div class="bar"><div class="fill {color}" style="width: {dim_score}%"></div></div><div class="num">{dim_score}</div></div>\n'

    html += f'''</div>
</div>

<div class="section">
  <div class="section-title">Tone: {result['tone']['tone']} ({result['tone']['sentiment']})</div>
</div>

<div class="section">
  <div class="section-title">Needs Attention ({len(result['needs_attention'])} issues)</div>
'''

    for issue in result['needs_attention']:
        dot_class = f"dot-{issue['severity']}"
        html += f'<div class="issue-card"><span class="dot {dot_class}"></span><span class="name">{issue["issue"]}</span><div class="fix">{issue["fix"]}</div></div>\n'

    html += '''
</div>

<div class="section">
  <div class="section-title">Structure Metrics</div>
  <table>
    <tr><th>Metric</th><th>Value</th><th>Target</th><th>Status</th></tr>
'''

    for m in result['structure_metrics']:
        status = '<span class="in-range">In range</span>' if m['in_range'] else ('<span class="out-of-range">Out of range</span>' if m['in_range'] is False else 'No target')
        html += f'<tr><td>{m["name"]}</td><td>{m["value"]}</td><td>{m["target"]}</td><td>{status}</td></tr>\n'

    html += '''  </table>
</div>

<div class="section">
  <div class="section-title">Topics / Keyword Tracking</div>
'''

    for topic in result['topics']['on_track']:
        html += f'<span class="topic-badge topic-on-track">{topic}</span>'
    for topic in result['topics']['to_add']:
        html += f'<span class="topic-badge topic-to-add">+ {topic}</span>'
    for topic in result['topics']['over_used']:
        html += f'<span class="topic-badge topic-over-used">{topic}</span>'

    html += f'''
</div>

<div class="section">
  <div class="section-title">Schema Analysis (Score: {result['schema']['score']}/100)</div>
  <p style="font-size: 13px; color: #ffffff; opacity: 0.85; margin-bottom: 8px;">Found: {', '.join(result['schema']['types_found']) or 'None'}</p>
  <p style="font-size: 13px; color: #f39c12;">Missing: {', '.join(result['schema']['missing']) or 'None'}</p>
</div>

<div class="section">
  <div class="section-title">Strategic Recommendations</div>
'''

    # Generate recommendations based on scores
    if scores['accuracy_title_alignment'] < 70:
        html += '<div class="recommendation"><strong>Title-to-Query Alignment:</strong> The page title or H1 does not properly align with the target keyword. Update the title tag and H1 to include the exact keyword.</div>\n'
    if scores['authority_e_e_a_t'] < 70:
        html += '<div class="recommendation"><strong>E-E-A-T Signals:</strong> The page lacks sufficient authority signals. Add author schema, about page links, contact info, and structured data (Article + Organization).</div>\n'
    if scores['factuality'] < 70:
        html += '<div class="recommendation"><strong>Factuality:</strong> Add citations, data sources, and authority outbound links (.gov/.edu/.org) to support claims.</div>\n'
    if scores['information_gain'] < 70:
        html += '<div class="recommendation"><strong>Information Gain:</strong> The content has too much boilerplate or too little unique content. Add more original analysis and reduce template content.</div>\n'
    if scores['content_hierarchy'] < 70:
        html += '<div class="recommendation"><strong>Content Hierarchy:</strong> Fix heading structure — ensure exactly 1 H1 with keyword, 2-20 H2s, and no hierarchy skips.</div>\n'
    if scores['content_format'] < 70:
        html += '<div class="recommendation"><strong>Content Format:</strong> Adjust word count, paragraph count, and internal/external link ratios to target ranges.</div>\n'

    html += f'''
<div class="footer">
  Zenfusion Content Auditor v1.0 &middot; Search Atlas + POP inspired &middot; noindex, nofollow
</div>

</body>
</html>'''

    return html


# ============================================================================
# CLI
# ============================================================================
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 content_auditor.py <url> <keyword> [--output path]")
        print("Example: python3 content_auditor.py https://example.com/page 'target keyword'")
        sys.exit(1)

    url = sys.argv[1]
    keyword = sys.argv[2]
    output_path = "."

    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    result = analyze_page(url, keyword)
    if not result:
        print("Analysis failed.")
        sys.exit(1)

    # Save JSON
    json_path = os.path.join(output_path, "content_audit.json")
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nJSON saved: {json_path}")

    # Save HTML
    html = generate_html_report(result)
    html_path = os.path.join(output_path, "content_audit.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"HTML saved: {html_path}")

    # Save CSV
    csv_path = os.path.join(output_path, "content_audit.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "Value"])
        writer.writerow(["URL", result["url"]])
        writer.writerow(["Keyword", result["keyword"]])
        writer.writerow(["Overall Score", result["overall_score"]])
        for dim, score in result["scores"].items():
            writer.writerow([dim, score])
        writer.writerow(["Tone", result["tone"]["tone"]])
        writer.writerow(["Sentiment", result["tone"]["sentiment"]])
        writer.writerow(["Word Count", result["word_count"]])
        for m in result["structure_metrics"]:
            writer.writerow([m["name"], m["value"]])
        writer.writerow(["Schema Score", result["schema"]["score"]])
        writer.writerow(["Schema Types", ", ".join(result["schema"]["types_found"])])
    print(f"CSV saved: {csv_path}")


if __name__ == "__main__":
    main()
