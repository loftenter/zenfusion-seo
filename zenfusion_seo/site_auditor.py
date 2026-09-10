#!/usr/bin/env python3
"""
Zenfusion Site Auditor v1.0 — Technical SEO crawler for Shopify stores.
Crawls an entire website and detects technical SEO issues in 13 categories.
Output matches SiteChecker CSV format for SRS integration.

Usage: python3 site_auditor.py <domain> [--limit N] [--output path]
"""

import requests, csv, json, os, sys, re, time
from urllib.parse import urljoin, urlparse, urldefrag, parse_qs
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from collections import defaultdict, Counter

# === CONFIG ===
REQUEST_TIMEOUT = 30
DELAY = 0.5
MAX_PAGES_DEFAULT = 500
USER_AGENT = "Mozilla/5.0 (compatible; ZenfusionSiteAuditor/1.0)"

# === SEVERITY ===
CRITICAL = "Critical"
WARNING = "Warning"
OPPORTUNITY = "Opportunity"
NOTICE = "Notice"

# === ISSUE -> (category, severity) MAP ===
ISSUE_MAP = {
    "Canonical points to a noindex URL": ("Indexability", CRITICAL),
    "Canonical is missing": ("Indexability", WARNING),
    "Canonical != URL": ("Indexability", NOTICE),
    "Meta noindex pages": ("Indexability", NOTICE),
    "Meta nofollow pages": ("Indexability", NOTICE),
    "More than one <body> tag on page": ("Indexability", CRITICAL),
    "More than one <head> tag on page": ("Indexability", CRITICAL),
    "More than one <html> tag on page": ("Indexability", CRITICAL),
    "Multiple viewport <meta> tags": ("Indexability", CRITICAL),
    "HTML lang attribute missing": ("Indexability", WARNING),
    "Description is missing": ("Content relevance", WARNING),
    "Description is empty": ("Content relevance", WARNING),
    "Description is too long": ("Content relevance", NOTICE),
    "Description is too short": ("Content relevance", NOTICE),
    "H1 is missing": ("Content relevance", WARNING),
    "H1 duplicates": ("Content relevance", WARNING),
    "H1 too long": ("Content relevance", NOTICE),
    "H1 too short": ("Content relevance", NOTICE),
    "H1 = Title": ("Content relevance", NOTICE),
    "H2 is missing": ("Content relevance", WARNING),
    "H2 starts with a lowercase letter": ("Content relevance", NOTICE),
    "H2 has other tags inside": ("Content relevance", OPPORTUNITY),
    "Title too long": ("Content relevance", NOTICE),
    "Title too short": ("Content relevance", NOTICE),
    "Text to code ratio < 10%": ("Content relevance", NOTICE),
    "Page has alt tags with one word": ("Content relevance", OPPORTUNITY),
    "Page has no strong importance elements": ("Content relevance", OPPORTUNITY),
    "Page has no list markdown": ("Content relevance", OPPORTUNITY),
    "Paragraphs are missing": ("Content relevance", OPPORTUNITY),
    "Page has identical headings": ("Duplicate content", CRITICAL),
    "Page has identical alt tags": ("Duplicate content", OPPORTUNITY),
    "H1 = Alt": ("Duplicate content", OPPORTUNITY),
    "More than one h1 on page": ("Duplicate content", NOTICE),
    "Page has internal links to 3xx pages": ("Links", WARNING),
    "Page has internal links to 4xx pages": ("Links", WARNING),
    "Broken jump link": ("Links", WARNING),
    "Page has broken links to external websites": ("Links", WARNING),
    "Page has nofollow outgoing internal links": ("Links", NOTICE),
    "Has an internal link with no anchor text": ("Links", OPPORTUNITY),
    "Page has more than 2 links to internal page with same anchor": ("Links", NOTICE),
    "Page has internal backlinks with same anchor": ("Links", OPPORTUNITY),
    "Page has less than 10 internal backlinks": ("Links", OPPORTUNITY),
    "Page has more than 100 internal links": ("Links", NOTICE),
    "Page has outbound internal links with one word anchor": ("Links", OPPORTUNITY),
    "Page has link with URL in onclick": ("Links", NOTICE),
    "Page has anchored image with no alt text": ("Links", WARNING),
    "Page size is over 1.5 MB": ("Page speed", WARNING),
    "Avoid excessive DOM size": ("Page speed", WARNING),
    "Avoid excessive DOM width": ("Page speed", WARNING),
    "Defer offscreen images": ("Page speed", OPPORTUNITY),
    "Add dimensions to images": ("Page speed", OPPORTUNITY),
    "Serve images in next-gen formats": ("Page speed", OPPORTUNITY),
    "Use video formats for animated content": ("Page speed", OPPORTUNITY),
    "Comments in code >1000 symbols": ("Page speed", OPPORTUNITY),
    "Comments in code has more than 1000 symbols": ("Page speed", OPPORTUNITY),
    "Page has HTTP link to www.w3.org": ("Security", NOTICE),
    "Page contains form with GET method": ("Security", NOTICE),
    "4xx client errors": ("Internal", CRITICAL),
    "3xx redirects": ("Internal", WARNING),
    "301 redirects": ("Internal", WARNING),
    "URL contains upper case characters": ("Internal", WARNING),
    "Long URLs": ("Internal", NOTICE),
    "More than three parameters in URL": ("Internal", NOTICE),
    "Whitespace in URL": ("Internal", NOTICE),
    "No Google Tag Manager code": ("Internal", NOTICE),
    "Multiple Google Tag Manager codes": ("Internal", NOTICE),
    "Paginated parameters in URL": ("Internal", NOTICE),
    "Internal URL redirect broken": ("Redirects", CRITICAL),
    "Twitter card incomplete": ("Social media", WARNING),
    "Twitter card missing": ("Social media", WARNING),
    "Open Graph tags incomplete": ("Social media", WARNING),
    "Open Graph tags missing": ("Social media", WARNING),
    "Headings hierarchy is broken": ("Code validation", OPPORTUNITY),
    "Page has identical HTML id attributes": ("Code validation", WARNING),
    "Table has no caption": ("Code validation", OPPORTUNITY),
    "Page has tags with style attributes": ("Code validation", NOTICE),
    "Page has 0 impressions": ("Search traffic", CRITICAL),
    "Page has at least 1 click": ("Search traffic", OPPORTUNITY),
}


def load_env():
    env_path = os.path.expanduser("~/.hermes/.env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


ENV = load_env()
SCRAPEOWL_KEY = ENV.get("SCRAPEOWL_API_KEY", "")


def fetch_page(url, timeout=REQUEST_TIMEOUT):
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) > 500:
            return resp.status_code, resp.text, resp.url, dict(resp.headers)
    except Exception:
        pass
    if SCRAPEOWL_KEY:
        try:
            api_url = "https://api.scrapeowl.com/v1/"
            params = {"api_key": SCRAPEOWL_KEY, "url": url, "render_js": "1"}
            resp = requests.get(api_url, params=params, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return 200, data.get("data", ""), url, {}
        except Exception:
            pass
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
        return resp.status_code, resp.text, resp.url, dict(resp.headers)
    except Exception as e:
        return 0, "", url, {"error": str(e)}


def get_domain_root(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def is_same_domain(url, root_domain):
    parsed = urlparse(url)
    root_parsed = urlparse(root_domain)
    root_netloc = root_parsed.netloc.replace("www.", "")
    url_netloc = parsed.netloc.replace("www.", "")
    return url_netloc == root_netloc or url_netloc.endswith("." + root_netloc)


def normalize_url(url):
    url, _ = urldefrag(url)
    return url.rstrip("/") if url != "/" else url


def check_robots(url):
    root = get_domain_root(url)
    rp = RobotFileParser()
    try:
        rp.set_url(f"{root}/robots.txt")
        rp.read()
    except Exception:
        pass
    return rp


def get_sitemaps(root_url):
    sitemaps = []
    try:
        resp = requests.get(f"{root_url}/sitemap.xml", headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "xml")
            for sm in soup.find_all("sitemap"):
                loc = sm.find("loc")
                if loc:
                    sitemaps.append({"url": loc.text, "type": "sitemap"})
            for u in soup.find_all("url"):
                loc = u.find("loc")
                if loc:
                    sitemaps.append({"url": loc.text, "type": "url"})
    except Exception:
        pass
    return sitemaps


def analyze_page(url, html, status_code, all_pages_status, internal_links_map, title_map, desc_map, headings_map, alt_map=None):
    issues = {CRITICAL: [], WARNING: [], OPPORTUNITY: [], NOTICE: []}
    if not html or status_code == 0:
        if 400 <= status_code < 500:
            issues[CRITICAL].append("4xx client errors")
        return issues

    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag.get("content", "").strip() if desc_tag else ""
    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""
    robots_meta = soup.find("meta", attrs={"name": "robots"})
    robots_content = robots_meta.get("content", "").lower() if robots_meta else ""

    # Indexability
    if "noindex" in robots_content:
        issues[NOTICE].append("Meta noindex pages")
    if "nofollow" in robots_content:
        issues[NOTICE].append("Meta nofollow pages")
    if not canonical and status_code == 200:
        issues[WARNING].append("Canonical is missing")
    if canonical and canonical.rstrip("/") != url.rstrip("/"):
        issues[NOTICE].append("Canonical != URL")
    if "noindex" in robots_content and canonical:
        issues[CRITICAL].append("Canonical points to a noindex URL")
    for tag in ["body", "head", "html"]:
        if len(soup.find_all(tag)) > 1:
            issues[CRITICAL].append(f"More than one <{tag}> tag on page")
    viewports = soup.find_all("meta", attrs={"name": "viewport"})
    if len(viewports) > 1:
        issues[CRITICAL].append("Multiple viewport <meta> tags")
    html_tag = soup.find("html")
    if html_tag and not html_tag.get("lang"):
        issues[WARNING].append("HTML lang attribute missing")

    # Content relevance
    if not desc_tag:
        issues[WARNING].append("Description is missing")
    elif not desc:
        issues[WARNING].append("Description is empty")
    elif len(desc) > 160:
        issues[NOTICE].append("Description is too long")
    elif len(desc) < 70:
        issues[NOTICE].append("Description is too short")

    h1_tags = soup.find_all("h1")
    h2_tags = soup.find_all("h2")
    if not h1_tags:
        issues[WARNING].append("H1 is missing")
    elif len(h1_tags) > 1:
        issues[NOTICE].append("More than one h1 on page")
    else:
        h1 = h1_tags[0].get_text(strip=True)
        if len(h1) > 60:
            issues[NOTICE].append("H1 too long")
        elif len(h1) < 10:
            issues[NOTICE].append("H1 too short")
        if h1 and h1[0].islower():
            issues[NOTICE].append("H1 = Title" if title and h1.lower() == title.lower() else "")
    if title and h1_tags and h1_tags[0].get_text(strip=True).lower() == title.lower():
        issues[NOTICE].append("H1 = Title")
    if not h2_tags:
        issues[WARNING].append("H2 is missing")
    else:
        for h2 in h2_tags:
            h2_text = h2.get_text(strip=True)
            if h2_text and h2_text[0].islower():
                issues[NOTICE].append("H2 starts with a lowercase letter")
            if h2.find("a") or h2.find("span") or h2.find("div"):
                issues[OPPORTUNITY].append("H2 has other tags inside")
    if title:
        if len(title) > 60:
            issues[NOTICE].append("Title too long")
        elif len(title) < 30:
            issues[NOTICE].append("Title too short")
    text = soup.get_text(strip=True)
    if len(html) > 0 and len(text) / len(html) < 0.10:
        issues[NOTICE].append("Text to code ratio < 10%")

    # Links
    links = soup.find_all("a", href=True)
    internal_links = []
    nofollow_internal = 0
    no_anchor = 0
    onclick_count = 0
    anchor_map = defaultdict(list)
    for a in links:
        href = a.get("href", "").strip()
        text_link = a.get_text(strip=True)
        if href == "#":
            continue
        if "javascript:" in href.lower():
            continue
        absolute = urljoin(url, href)
        if is_same_domain(absolute, url):
            internal_links.append(absolute)
            rel = a.get("rel", [])
            if isinstance(rel, list) and "nofollow" in [r.lower() for r in rel]:
                nofollow_internal += 1
            # Check for anchor text: link text OR image alt text inside the link
            if not text_link:
                # Check if link contains an image with alt text
                img = a.find("img")
                if img and img.get("alt", "").strip():
                    text_link = img["alt"].strip()
                else:
                    no_anchor += 1
            anchor_map[normalize_url(absolute)].append(text_link)
        if a.get("onclick"):
            onclick_count += 1

    link_3xx = link_4xx = 0
    for link_url in set(internal_links):
        sc = all_pages_status.get(normalize_url(link_url), 0)
        if 300 <= sc < 400:
            link_3xx += 1
        elif 400 <= sc < 500:
            link_4xx += 1
    if link_3xx:
        issues[WARNING].append("Page has internal links to 3xx pages")
    if link_4xx:
        issues[WARNING].append("Page has internal links to 4xx pages")
    if nofollow_internal:
        issues[NOTICE].append("Page has nofollow outgoing internal links")
    if no_anchor:
        issues[OPPORTUNITY].append("Has an internal link with no anchor text")
    if onclick_count:
        issues[NOTICE].append("Page has link with URL in onclick")
    for u, anchors in anchor_map.items():
        for a_text, cnt in Counter(anchors).items():
            if cnt > 2:
                issues[NOTICE].append("Page has more than 2 links to internal page with same anchor")
                break
    backlinks = internal_links_map.get(normalize_url(url), [])
    if len(backlinks) < 10:
        issues[OPPORTUNITY].append("Page has less than 10 internal backlinks")
    if len(set(backlinks)) < len(backlinks):
        issues[OPPORTUNITY].append("Page has internal backlinks with same anchor")
    if len(internal_links) > 100:
        issues[NOTICE].append("Page has more than 100 internal links")

    # Page speed
    page_size = len(html.encode("utf-8"))
    if page_size > 1500000:
        issues[WARNING].append("Page size is over 1.5 MB")
    all_tags = soup.find_all(True)
    if len(all_tags) > 1500:
        issues[WARNING].append("Avoid excessive DOM size")
    # DOM width = max number of children at any level (siblings)
    max_width = 0
    for tag in all_tags:
        children = tag.find_all(recursive=False)
        if len(children) > max_width:
            max_width = len(children)
    if max_width > 100:  # SiteChecker threshold for excessive DOM width
        issues[WARNING].append("Avoid excessive DOM width")
    images = soup.find_all("img")
    if sum(1 for img in images if not img.get("width") or not img.get("height")) > 0:
        issues[OPPORTUNITY].append("Add dimensions to images")
    if sum(1 for img in images if not img.get("loading") == "lazy" and not img.get("data-src")) > 5:
        issues[OPPORTUNITY].append("Defer offscreen images")
    if images and not any("webp" in str(img.get("src", "")) or "avif" in str(img.get("src", "")) for img in images):
        issues[OPPORTUNITY].append("Serve images in next-gen formats")
    if soup.find_all("img", src=re.compile(r"\.gif$", re.I)):
        issues[OPPORTUNITY].append("Use video formats for animated content")
    # Comments >1000 symbols — use regex on raw HTML (BeautifulSoup Comment type is unreliable)
    long_comments = re.findall(r"<!--(.{1000,}?)-->", html, re.DOTALL)
    if long_comments:
        issues[OPPORTUNITY].append("Comments in code has more than 1000 symbols")

    # Security
    if soup.find_all("a", href=re.compile(r"http://www\.w3\.org")):
        issues[NOTICE].append("Page has HTTP link to www.w3.org")
    if soup.find_all("form", method=re.compile(r"^get$", re.I)):
        issues[NOTICE].append("Page contains form with GET method")

    # Internal URL
    parsed = urlparse(url)
    if any(c.isupper() for c in parsed.path):
        issues[WARNING].append("URL contains upper case characters")
    if len(url) > 100:
        issues[NOTICE].append("Long URLs")
    params = parse_qs(parsed.query)
    if len(params) > 3:
        issues[NOTICE].append("More than three parameters in URL")
    if " " in url:
        issues[NOTICE].append("Whitespace in URL")
    if "page" in params or "p" in params:
        issues[NOTICE].append("Paginated parameters in URL")
    gtm = len(re.findall(r"gtag/js|googletagmanager\.com/gtm\.js", html))
    if gtm == 0:
        issues[NOTICE].append("No Google Tag Manager code")
    elif gtm > 1:
        issues[NOTICE].append("Multiple Google Tag Manager codes")

    # Redirects
    if 300 <= status_code < 400:
        issues[WARNING].append("3xx redirects")
        if status_code == 301:
            issues[WARNING].append("301 redirects")
    if status_code >= 400:
        issues[CRITICAL].append("4xx client errors")

    # Social media
    og_fields = ["og:title", "og:description", "og:image", "og:url", "og:type"]
    og_found = sum(1 for og in og_fields if soup.find("meta", attrs={"property": og}))
    if og_found < 3:
        issues[WARNING].append("Open Graph tags incomplete" if og_found else "Open Graph tags missing")
    # Twitter card: SiteChecker checks card, title, description, image, site, creator
    tw_fields = ["twitter:card", "twitter:title", "twitter:description", "twitter:image"]
    tw_found = sum(1 for tw in tw_fields if soup.find("meta", attrs={"name": tw}) or soup.find("meta", attrs={"property": tw}))
    if tw_found < 4:
        issues[WARNING].append("Twitter card incomplete" if tw_found else "Twitter card missing")

    # Code validation
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    prev = 0
    for h in headings:
        level = int(h.name[1])
        if level > prev + 1 and prev > 0:
            issues[OPPORTUNITY].append("Headings hierarchy is broken")
            break
        prev = level
    id_tags = [t.get("id") for t in soup.find_all(id=True)]
    if len(id_tags) != len(set(id_tags)):
        issues[WARNING].append("Page has identical HTML id attributes")
    if soup.find_all("table") and any(not t.find("caption") for t in soup.find_all("table")):
        issues[OPPORTUNITY].append("Table has no caption")
    if soup.find_all(attrs={"style": True}):
        issues[NOTICE].append("Page has tags with style attributes")

    # Store for cross-page analysis
    title_map[url] = title
    desc_map[url] = desc
    headings_map[url] = [h.get_text(strip=True) for h in headings]

    # Alt text checks
    alt_texts = [img.get("alt", "") for img in images]
    # Store alt texts for cross-page dedup
    if alt_map is not None:
        alt_map[url] = [a for a in alt_texts if a]
    if sum(1 for a in alt_texts if a and len(a.split()) == 1) > 0:
        issues[OPPORTUNITY].append("Page has alt tags with one word")
    if h1_tags and images:
        h1_text = h1_tags[0].get_text(strip=True)
        if any(img.get("alt", "") == h1_text for img in images):
            issues[OPPORTUNITY].append("H1 = Alt")
    if not soup.find_all(["strong", "b"]):
        issues[OPPORTUNITY].append("Page has no strong importance elements")
    if not soup.find_all(["ul", "ol"]):
        issues[OPPORTUNITY].append("Page has no list markdown")
    if not soup.find_all("p"):
        issues[OPPORTUNITY].append("Paragraphs are missing")

    # Clean empty entries
    for k in issues:
        issues[k] = [i for i in issues[k] if i]
    return issues


def compute_weight(url, internal_links_map):
    parsed = urlparse(url)
    if parsed.path in ["", "/"]:
        return 100.0
    backlinks = internal_links_map.get(normalize_url(url), [])
    max_backlinks = max((len(v) for v in internal_links_map.values()), default=1)
    if max_backlinks == 0:
        return 0.0
    return round((len(backlinks) / max_backlinks) * 100, 3)


def compute_site_health(all_page_issues, total_pages):
    if total_pages == 0:
        return 0
    c = sum(len(p[CRITICAL]) for p in all_page_issues.values())
    w = sum(len(p[WARNING]) for p in all_page_issues.values())
    o = sum(len(p[OPPORTUNITY]) for p in all_page_issues.values())
    n = sum(len(p[NOTICE]) for p in all_page_issues.values())
    if c + w + o + n == 0:
        return 100
    penalty = (c * 3 + w * 1.5 + o * 0.5 + n * 0.1) / total_pages
    return round(max(0, 100 - penalty), 1)


def extract_keyword_opportunities(title_map, h1_map):
    """Extract title tags and H1s, run through Keywords Everywhere for full keyword data.
    Filters out boilerplate/legal pages and strips full product titles to actual keywords.
    Returns sorted list of {url, keyword, source, volume, cpc, competition} dicts (highest volume first)."""
    
    # Boilerplate/legal terms to NEVER include in keyword opportunity reports
    EXCLUDE_TERMS = {
        "about us", "privacy policy", "terms of service", "track your order",
        "terms and conditions", "choose water type", "contact us", "contact",
        "faq", "search", "cart", "checkout", "account", "login", "sign in",
        "404 not found", "page not found", "menu", "navigation", "footer",
        "sitemap", "robots.txt", "accessibility", "cookie policy",
        "returns", "shipping policy", "warranty", "support", "help",
    }
    
    # Patterns that indicate a full product title, not a keyword
    # Strip brand names, emojis, marketing fluff to extract the actual keyword
    import re
    
    def clean_title_to_keyword(title):
        """Strip brand names, emojis, and marketing fluff from a title to get the actual keyword."""
        if not title:
            return ""
        # Remove emojis
        cleaned = re.sub(r'[🇺🇸🚚✅⭐🆕🔥💡]', '', title)
        # Remove brand prefixes like "SoftPro™ Elite Smart HE"
        cleaned = re.sub(r'^[A-Z][a-zA-Z]+(?:\s*(?:™|®|©))?\s+(?:Elite|ECO|Smart|Pro|Plus|Basic|Standard|Home|HE)\s+(?:Smart|HE|Pro)?', '', cleaned)
        # Remove " - Best Water Softener Systems for..." suffixes
        cleaned = re.sub(r'\s*[-–—]\s*Best\s+.*$', '', cleaned)
        # Remove "Sale!" and similar
        cleaned = re.sub(r'\s*(Sale!|Sale|New!|New)\s*', '', cleaned, flags=re.I)
        # Remove trailing brand name
        cleaned = re.sub(r'\s*(SoftPro|Soft Pro)\s*$', '', cleaned, flags=re.I)
        # Remove calculator/utility prefixes
        cleaned = re.sub(r'^Choose\s+.*?\s*[-–—]\s*', '', cleaned)
        # Clean up whitespace
        cleaned = cleaned.strip()
        return cleaned
    
    def is_excluded(keyword):
        """Check if a keyword should be excluded from the opportunity report."""
        kw_lower = keyword.strip().lower()
        # Check exact match
        if kw_lower in EXCLUDE_TERMS:
            return True
        # Check if any excluded term is in the keyword
        for term in EXCLUDE_TERMS:
            if term in kw_lower:
                return True
        # Skip very short keywords
        if len(kw_lower) < 4:
            return True
        return False
    
    # Collect unique keywords from titles and H1s
    keywords = set()
    title_keyword_map = {}  # maps cleaned keyword -> original url
    
    for url, title in title_map.items():
        if not title or len(title.strip()) <= 5:
            continue
        cleaned = clean_title_to_keyword(title)
        if cleaned and not is_excluded(cleaned) and len(cleaned) > 5:
            keywords.add(cleaned)
            title_keyword_map[cleaned] = url
    
    for url, h1 in h1_map.items():
        if not h1 or len(h1.strip()) <= 5:
            continue
        cleaned = clean_title_to_keyword(h1)
        if cleaned and not is_excluded(cleaned) and len(cleaned) > 5:
            keywords.add(cleaned)
            if cleaned not in title_keyword_map:
                title_keyword_map[cleaned] = url

    if not keywords:
        return []

    # Run through KE (max 100 per call) — get full data (vol + CPC + competition)
    ke_data = {}
    kw_list = list(keywords)
    try:
        _scripts_dir = os.path.join(os.path.expanduser("~/.hermes"), "scripts")
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from ke_helper import get_keyword_data
        for i in range(0, len(kw_list), 100):
            batch = kw_list[i:i+100]
            batch_data = get_keyword_data(batch, "us")
            for kw, d in batch_data.items():
                if kw.startswith('_') or not isinstance(d, dict):
                    continue
                ke_data[kw.lower()] = {
                    'vol': d.get('vol', 0),
                    'cpc': d.get('cpc', 0),
                    'competition': d.get('competition', 0),
                    'currency': d.get('currency', '$'),
                }
    except Exception as e:
        print(f"  KE API error: {e}")

    # Build opportunities list
    opportunities = []
    for url, title in title_map.items():
        if not title or len(title.strip()) <= 5:
            continue
        cleaned = clean_title_to_keyword(title)
        if not cleaned or is_excluded(cleaned) or len(cleaned) <= 5:
            continue
        kw_lower = cleaned.lower()
        ke = ke_data.get(kw_lower, {})
        opportunities.append({
            "url": url, "keyword": cleaned, "source": "title",
            "volume": ke.get('vol', 0),
            "cpc": ke.get('cpc', 0),
            "competition": ke.get('competition', 0),
            "currency": ke.get('currency', '$'),
        })
    for url, h1 in h1_map.items():
        if not h1 or len(h1.strip()) <= 5:
            continue
        cleaned = clean_title_to_keyword(h1)
        if not cleaned or is_excluded(cleaned) or len(cleaned) <= 5:
            continue
        kw_lower = cleaned.lower()
        ke = ke_data.get(kw_lower, {})
        opportunities.append({
            "url": url, "keyword": cleaned, "source": "h1",
            "volume": ke.get('vol', 0),
            "cpc": ke.get('cpc', 0),
            "competition": ke.get('competition', 0),
            "currency": ke.get('currency', '$'),
        })

    # Sort by volume descending — filter out zero-volume keywords
    opportunities = [o for o in opportunities if o.get("volume", 0) > 0]
    opportunities.sort(key=lambda x: -x["volume"])
    return opportunities


def crawl_site(start_url, max_pages=500):
    root = get_domain_root(start_url)
    rp = check_robots(start_url)
    visited = set()
    queue = [start_url]
    all_pages = {}
    all_pages_status = {}
    internal_links_map = defaultdict(list)
    title_map, desc_map, headings_map = {}, {}, {}
    h1_map = {}  # url -> H1 text (for keyword opportunities)
    alt_map = {}  # url -> [alt texts]

    print(f"\n{'='*70}")
    print(f"  ZENFUSION SITE AUDITOR v1.0")
    print(f"{'='*70}")
    print(f"  Domain: {root}")
    print(f"  Max pages: {max_pages}")
    print(f"  ScrapeOwl: {'ON' if SCRAPEOWL_KEY else 'OFF'}")
    print(f"{'='*70}\n")

    sitemaps = get_sitemaps(root)
    print(f"  Sitemaps: {len(sitemaps)} entries found")
    for sm in sitemaps:
        if sm["type"] == "url" and is_same_domain(sm["url"], start_url):
            queue.append(sm["url"])

    page_count = 0
    while queue and page_count < max_pages:
        url = queue.pop(0)
        norm = normalize_url(url)
        if norm in visited:
            continue
        visited.add(norm)
        if rp and not rp.can_fetch(USER_AGENT, url):
            continue
        if any(url.lower().endswith(ext) for ext in [".jpg", ".png", ".gif", ".css", ".js", ".svg", ".ico"]):
            continue
        time.sleep(DELAY)
        page_count += 1
        if page_count % 10 == 0:
            print(f"  Crawled {page_count} pages...")
        status, html, final_url, headers = fetch_page(url)
        all_pages_status[norm] = status
        all_pages[url] = {"status": status, "html": html, "headers": headers, "final_url": final_url}
        if status == 200 and html:
            soup = BeautifulSoup(html, "html.parser")
            # Collect H1 for keyword opportunity analysis
            _h1 = soup.find("h1")
            if _h1:
                h1_map[url] = _h1.get_text(strip=True)
            for a in soup.find_all("a", href=True):
                href = a.get("href", "").strip()
                if href and href != "#" and not href.startswith("javascript:"):
                    absolute = urljoin(url, href)
                    if is_same_domain(absolute, start_url):
                        target_norm = normalize_url(absolute)
                        internal_links_map[target_norm].append(a.get_text(strip=True))
                        if target_norm not in visited and absolute not in queue:
                            if len(queue) < max_pages * 2:
                                queue.append(absolute)

    print(f"\n  Crawl complete: {page_count} pages fetched")

    all_page_issues = {}
    for url, pd in all_pages.items():
        all_page_issues[url] = analyze_page(
            url, pd["html"], pd["status"], all_pages_status, internal_links_map, title_map, desc_map, headings_map, alt_map
        )

    # Cross-page duplicate checks
    title_counts = Counter(t for t in title_map.values() if t)
    desc_counts = Counter(d for d in desc_map.values() if d)
    # Build alt text -> set of URLs mapping for identical alt tag detection
    alt_text_urls = defaultdict(set)
    for url, alts in alt_map.items():
        for a in alts:
            alt_text_urls[a].add(url)

    for url in all_page_issues:
        title = title_map.get(url, "")
        desc = desc_map.get(url, "")
        if title and title_counts[title] > 1:
            all_page_issues[url][CRITICAL].append("Page has identical headings")
        if desc and desc_counts[desc] > 1:
            all_page_issues[url][WARNING].append("Description duplicates")
        # Check if this page has alt tags that appear on other pages
        page_alts = alt_map.get(url, [])
        if any(len(alt_text_urls.get(a, set())) > 1 for a in page_alts):
            all_page_issues[url][OPPORTUNITY].append("Page has identical alt tags")

    all_page_data = []
    for url, issues in all_page_issues.items():
        weight = compute_weight(url, internal_links_map)
        title = title_map.get(url, "")
        desc = desc_map.get(url, "")
        status = all_pages_status.get(normalize_url(url), 0)
        crits = sorted(set(issues[CRITICAL]))
        warns = sorted(set(issues[WARNING]))
        opps = sorted(set(issues[OPPORTUNITY]))
        nots = sorted(set(issues[NOTICE]))
        all_page_data.append({
            "url": url, "status_code": status, "indexation": "Indexable",
            "title": title, "description": desc, "weight": weight,
            "criticals_count": len(crits), "warnings_count": len(warns),
            "opportunities_count": len(opps), "notices_count": len(nots),
            "criticals": ", ".join(crits), "warnings": ", ".join(warns),
            "opportunities": ", ".join(opps), "notices": ", ".join(nots),
        })

    health = compute_site_health(all_page_issues, len(all_page_issues))

    cat_summary = defaultdict(lambda: {"criticals": 0, "warnings": 0, "opportunities": 0, "notices": 0, "pages": set()})
    sev_key_map = {CRITICAL: "criticals", WARNING: "warnings", OPPORTUNITY: "opportunities", NOTICE: "notices"}
    for url, issues in all_page_issues.items():
        for sev, items in issues.items():
            for item in items:
                cat = ISSUE_MAP.get(item, ("Other", sev))[0]
                cat_summary[cat][sev_key_map[sev]] += 1
                cat_summary[cat]["pages"].add(url)

    # Extract keyword opportunities from titles and H1s via KE
    keyword_opportunities = extract_keyword_opportunities(title_map, h1_map)

    return all_page_data, health, cat_summary, sitemaps, keyword_opportunities


def save_csv(page_data, output_path):
    fields = ["Url", "Status Code", "Indexation", "Title", "Description", "Weight",
              "# Criticals", "# Warnings", "# Opportunities", "# Notices",
              "Criticals", "Warnings", "Opportunities", "Notices"]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in page_data:
            w.writerow({"Url": p["url"], "Status Code": p["status_code"], "Indexation": p["indexation"],
                         "Title": p["title"], "Description": p["description"], "Weight": p["weight"],
                         "# Criticals": p["criticals_count"], "# Warnings": p["warnings_count"],
                         "# Opportunities": p["opportunities_count"], "# Notices": p["notices_count"],
                         "Criticals": p["criticals"], "Warnings": p["warnings"],
                         "Opportunities": p["opportunities"], "Notices": p["notices"]})


def save_json(page_data, health, cat_summary, sitemaps, keyword_opportunities, output_path):
    result = {
        "site_health_score": health,
        "total_pages": len(page_data),
        "total_criticals": sum(p["criticals_count"] for p in page_data),
        "total_warnings": sum(p["warnings_count"] for p in page_data),
        "total_opportunities": sum(p["opportunities_count"] for p in page_data),
        "total_notices": sum(p["notices_count"] for p in page_data),
        "category_summary": {cat: {"criticals": v["criticals"], "warnings": v["warnings"],
                                    "opportunities": v["opportunities"], "notices": v["notices"],
                                    "pages_affected": len(v["pages"])}
                              for cat, v in cat_summary.items()},
        "sitemaps": len(sitemaps),
        "pages": page_data,
        "keyword_opportunities": keyword_opportunities,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def print_summary(health, cat_summary, page_data):
    c = sum(p["criticals_count"] for p in page_data)
    w = sum(p["warnings_count"] for p in page_data)
    o = sum(p["opportunities_count"] for p in page_data)
    n = sum(p["notices_count"] for p in page_data)
    print(f"\n{'='*70}")
    print(f"  SITE AUDIT COMPLETE")
    print(f"{'='*70}")
    print(f"  Website Score: {health}/100")
    print(f"  Total Pages: {len(page_data)}")
    print(f"  Criticals: {c} | Warnings: {w} | Opportunities: {o} | Notices: {n}")
    print(f"\n{'='*70}")
    print(f"  CATEGORY BREAKDOWN")
    print(f"{'='*70}")
    for cat, v in sorted(cat_summary.items(), key=lambda x: -(x[1]["criticals"] + x[1]["warnings"])):
        total = v["criticals"] + v["warnings"] + v["opportunities"] + v["notices"]
        if total == 0:
            continue
        print(f"  {cat}: {total} issues ({v['criticals']}C/{v['warnings']}W/{v['opportunities']}O/{v['notices']}N) - {len(v['pages'])} pages")
    print(f"\n  Report generated by Zenfusion Site Auditor v1.0")


def print_keyword_opportunities(keyword_opportunities):
    if not keyword_opportunities:
        print(f"\n{'='*70}")
        print(f"  KEYWORD OPPORTUNITIES (KE Volume Data)")
        print(f"{'='*70}")
        print("  No title/H1 keywords found for volume lookup.")
        return

    high_vol = [k for k in keyword_opportunities if k["volume"] >= 1000]
    med_vol = [k for k in keyword_opportunities if 100 <= k["volume"] < 1000]
    low_vol = [k for k in keyword_opportunities if 0 < k["volume"] < 100]
    no_vol = [k for k in keyword_opportunities if k["volume"] == 0]

    print(f"\n{'='*70}")
    print(f"  KEYWORD OPPORTUNITIES (KE Volume Data)")
    print(f"{'='*70}")
    print(f"  Total keywords analyzed: {len(keyword_opportunities)}")
    print(f"  High volume (1,000+): {len(high_vol)} | Medium (100-999): {len(med_vol)} | Low (1-99): {len(low_vol)} | No data: {len(no_vol)}")
    print()

    if high_vol:
        print(f"  --- HIGH VOLUME (1,000+/mo) ---")
        for k in high_vol[:15]:
            print(f"  {k['volume']:>8,}  [{k['source']}]  {k['keyword'][:70]}")
            print(f"           URL: {k['url'][:80]}")

    if med_vol:
        print(f"\n  --- MEDIUM VOLUME (100-999/mo) ---")
        for k in med_vol[:10]:
            print(f"  {k['volume']:>8,}  [{k['source']}]  {k['keyword'][:70]}")

    if low_vol:
        print(f"\n  --- LOW VOLUME (1-99/mo) ---")
        for k in low_vol[:10]:
            print(f"  {k['volume']:>8,}  [{k['source']}]  {k['keyword'][:70]}")

    if no_vol:
        print(f"\n  --- NO VOLUME DATA ({len(no_vol)} keywords) ---")
        for k in no_vol[:5]:
            print(f"         [{k['source']}]  {k['keyword'][:70]}")
        if len(no_vol) > 5:
            print(f"  ... and {len(no_vol) - 5} more")


def generate_keyword_dashboard_html(keyword_opportunities, page_data, health, domain, output_path):
    """Generate a white SaaS theme HTML dashboard with KE keyword opportunity data."""
    total = len(keyword_opportunities)
    high_vol = [k for k in keyword_opportunities if k.get("volume", 0) >= 1000]
    med_vol = [k for k in keyword_opportunities if 100 <= k.get("volume", 0) < 1000]
    low_vol = [k for k in keyword_opportunities if 0 < k.get("volume", 0) < 100]
    no_vol = [k for k in keyword_opportunities if k.get("volume", 0) == 0]
    
    total_volume = sum(k.get("volume", 0) for k in keyword_opportunities)
    
    # Build table rows
    rows_html = ""
    for k in keyword_opportunities[:200]:  # Top 200
        vol = k.get("volume", 0)
        cpc = k.get("cpc", 0)
        comp = k.get("competition", 0)
        currency = k.get("currency", "$")
        vol_class = "vol-high" if vol >= 1000 else ("vol-med" if vol >= 100 else "vol-low")
        comp_label = "Very High" if comp >= 0.8 else ("High" if comp >= 0.6 else ("Medium" if comp >= 0.3 else "Low"))
        comp_color = "#cc0000" if comp >= 0.8 else ("#e67e22" if comp >= 0.6 else ("#f39c12" if comp >= 0.3 else "#27ae60"))
        
        # Escape keyword for HTML
        kw_safe = k["keyword"].replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
        url_short = k["url"].replace("https://", "").replace("http://", "")[:60]
        
        rows_html += f"""      <tr>
        <td class="{vol_class}">{vol:,}</td>
        <td>{currency}{cpc}</td>
        <td><span style="color:{comp_color};font-weight:600">{comp_label}</span> <span style="color:#999;font-size:13px">({comp:.2f})</span></td>
        <td><span class="source-badge source-{k['source']}">{k['source']}</span></td>
        <td><a href="{k['url']}" target="_blank" style="color:#0066cc;text-decoration:none">{url_short}</a></td>
        <td><strong>{kw_safe}</strong></td>
      </tr>
"""
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Keyword Opportunity Dashboard — {domain}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #ffffff; color: #1a1a1a; font-size: 16px; margin: 0; padding: 20px; }}
  h1 {{ font-size: 28px; color: #1a1a1a; margin-bottom: 4px; }}
  h2 {{ font-size: 20px; color: #1a1a1a; margin-top: 30px; margin-bottom: 10px; }}
  .subtitle {{ font-size: 16px; color: #666; margin-bottom: 20px; }}
  .url-headline {{ font-size: 20px; font-weight: 700; color: #0066cc; margin-bottom: 20px; }}
  .url-headline a {{ color: #0066cc; text-decoration: none; }}
  .summary-cards {{ display: grid; grid-template-columns: repeat(5,1fr); gap: 12px; margin-bottom: 24px; }}
  .summary-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 16px; text-align: center; }}
  .summary-card .num {{ font-size: 28px; font-weight: 700; color: #0066cc; }}
  .summary-card .label {{ font-size: 14px; color: #666; text-transform: uppercase; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #ffffff; }}
  th {{ background: #f0f0f0; color: #1a1a1a; font-weight: 700; font-size: 15px; padding: 12px 14px; text-align: left; border-bottom: 2px solid #e0e0e0; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #e0e0e0; font-size: 15px; color: #1a1a1a; }}
  tr:hover {{ background: #f8f9fa; }}
  .vol-high {{ color: #27ae60; font-weight: 700; }}
  .vol-med {{ color: #0066cc; font-weight: 600; }}
  .vol-low {{ color: #999; }}
  .source-badge {{ padding: 3px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; text-transform: uppercase; }}
  .source-title {{ background: #e7f3ff; color: #0066cc; }}
  .source-h1 {{ background: #e8f5e9; color: #27ae60; }}
  .section {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
  .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e0e0e0; font-size: 13px; color: #999; }}
  .expandable {{ cursor: pointer; user-select: none; }}
  .detail {{ display: none; }}
  .detail.show {{ display: block; }}
</style>
</head>
<body>
  <h1>Keyword Opportunity Dashboard</h1>
  <div class="url-headline"><a href="https://{domain}" target="_blank">{domain}</a></div>
  <div class="subtitle">Keywords Everywhere data for all page titles and H1s found during site crawl. Data includes search volume, CPC, and competition.</div>
  
  <div class="summary-cards">
    <div class="summary-card"><div class="num">{total:,}</div><div class="label">Total Keywords</div></div>
    <div class="summary-card"><div class="num">{len(high_vol):,}</div><div class="label">High Vol (1K+)</div></div>
    <div class="summary-card"><div class="num">{len(med_vol):,}</div><div class="label">Med Vol (100-999)</div></div>
    <div class="summary-card"><div class="num">{total_volume:,}</div><div class="label">Total Monthly Vol</div></div>
    <div class="summary-card"><div class="num">{health if isinstance(health, (int, float)) else 0:.0f}</div><div class="label">Site Health Score</div></div>
  </div>
  
  <div class="section">
    <h2>Keyword Opportunities (Top 200 — sorted by volume)</h2>
    <table>
      <thead>
        <tr>
          <th>Monthly Volume</th>
          <th>CPC</th>
          <th>Competition</th>
          <th>Source</th>
          <th>Page URL</th>
          <th>Keyword (Title/H1)</th>
        </tr>
      </thead>
      <tbody>
{rows_html}      </tbody>
    </table>
  </div>
  
  <div class="footer">
    Zenfusion Site Auditor — Keyword Opportunity Dashboard · Powered by Keywords Everywhere (prepaid credits) · noindex, nofollow
  </div>

<script>
(function() {{
  document.querySelectorAll('.expandable').forEach(function(el) {{
    var detail = el.nextElementSibling;
    if (detail && detail.classList.contains('detail')) {{
      el.addEventListener('click', function() {{
        detail.classList.toggle('show');
      }});
    }}
  }});
}})();
</script>
</body>
</html>'''
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 site_auditor.py <domain> [--limit N] [--output path]")
        sys.exit(1)
    domain = sys.argv[1]
    if not domain.startswith("http"):
        domain = "https://" + domain
    max_pages = MAX_PAGES_DEFAULT
    output_dir = os.path.expanduser("~/.hermes/audits/site-auditor")
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--limit" and i + 1 < len(sys.argv):
            max_pages = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--output" and i + 1 < len(sys.argv):
            output_dir = sys.argv[i + 1]
            i += 2
        else:
            i += 1
    os.makedirs(output_dir, exist_ok=True)
    page_data, health, cat_summary, sitemaps, keyword_opportunities = crawl_site(domain, max_pages)
    domain_name = urlparse(domain).netloc.replace("www.", "").replace(".", "_")
    csv_path = os.path.join(output_dir, f"{domain_name}_site_audit.csv")
    json_path = os.path.join(output_dir, f"{domain_name}_site_audit.json")
    save_csv(page_data, csv_path)
    save_json(page_data, health, cat_summary, sitemaps, keyword_opportunities, json_path)
    print_summary(health, cat_summary, page_data)
    print_keyword_opportunities(keyword_opportunities)
    print(f"\n  CSV: {csv_path}")
    print(f"  JSON: {json_path}")
    
    # Generate HTML keyword dashboard (title/H1 based — for reference)
    # Note: The full gap-analysis dashboard is generated separately by build_keyword_dashboard.py
    if keyword_opportunities:
        html_path = os.path.join(output_dir, f"{domain_name}_title_h1_keywords.html")
        generate_keyword_dashboard_html(keyword_opportunities, page_data, health, urlparse(domain).netloc, html_path)
        print(f"  HTML Keywords: {html_path}")


if __name__ == "__main__":
    main()
