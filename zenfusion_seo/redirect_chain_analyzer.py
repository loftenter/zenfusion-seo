#!/usr/bin/env python3
"""
redirect_chain_analyzer.py — Check URLs for redirect chains and report issues.

Standalone script: traces HTTP redirect chains, classifies issues, and
generates an HTML report with summary cards, issue distribution, redirect
table, full chains, and actionable recommendations.

Accepts:
  --sitemap URL   Parse a sitemap XML for URLs.
  --file PATH     Read URLs (one per line) from a file.
  --urls URL,URL  Comma-separated list of individual URLs.
  --output PATH   Output directory for the HTML report (default: /tmp/redirect_report).

Usage:
  python3 redirect_chain_analyzer.py --urls 'https://example.com,https://www.example.com' --output /tmp/test_redirects
  python3 redirect_chain_analyzer.py --sitemap https://example.com/sitemap.xml --output /tmp/redirects
  python3 redirect_chain_analyzer.py --file urls.txt --output /tmp/redirects
"""

import argparse
import os
import sys
import time
import html
import urllib.request
import urllib.parse
import urllib.error
import socket
from typing import List, Dict, Tuple, Optional, Any
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_HOPS = 10
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; RedirectChainAnalyzer/1.0)"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
REDIRECT_NAMES = {
    301: "301 Moved Permanently",
    302: "302 Found",
    303: "303 See Other",
    307: "307 Temporary Redirect",
    308: "308 Permanent Redirect",
}


# ---------------------------------------------------------------------------
# 1. trace_redirects
# ---------------------------------------------------------------------------
def trace_redirects(url: str, max_hops: int = DEFAULT_MAX_HOPS) -> List[Dict[str, Any]]:
    """Follow redirects from a URL and return a list of hop dictionaries.

    Each hop: {
        'url': str, 'status_code': int, 'location': str|None,
        'redirect_type': str|None (e.g. '301'), 'final': bool
    }
    Stops at the final non-redirect URL or max_hops.
    """
    hops: List[Dict[str, Any]] = []
    visited: List[str] = []
    current_url = url
    redirect_count = 0

    while redirect_count <= max_hops:
        # Normalize for comparison
        normalized = current_url.rstrip("/") if current_url else ""
        if normalized in visited and redirect_count > 0:
            # Loop detected — record and stop
            hops.append({
                "url": current_url,
                "status_code": 0,
                "location": None,
                "redirect_type": "LOOP",
                "final": False,
            })
            break

        visited.append(normalized)

        req = urllib.request.Request(current_url, method="GET", headers={
            "User-Agent": DEFAULT_USER_AGENT,
        })
        try:
            # Don't follow redirects automatically
            opener = urllib.request.build_opener(NoRedirectHandler)
            response = opener.open(req, timeout=DEFAULT_TIMEOUT)
            status_code = response.getcode()
            location = response.headers.get("Location")
            response.close()
        except urllib.error.HTTPError as e:
            status_code = e.code
            location = e.headers.get("Location")
        except urllib.error.URLError as e:
            hops.append({
                "url": current_url,
                "status_code": 0,
                "location": None,
                "redirect_type": f"ERROR: {e.reason}",
                "final": True,
            })
            break
        except socket.timeout:
            hops.append({
                "url": current_url,
                "status_code": 0,
                "location": None,
                "redirect_type": "TIMEOUT",
                "final": True,
            })
            break
        except Exception as e:
            hops.append({
                "url": current_url,
                "status_code": 0,
                "location": None,
                "redirect_type": f"ERROR: {e}",
                "final": True,
            })
            break

        is_redirect = status_code in REDIRECT_STATUSES
        redirect_type = str(status_code) if is_redirect else None

        hops.append({
            "url": current_url,
            "status_code": status_code,
            "location": location,
            "redirect_type": redirect_type,
            "final": not is_redirect,
        })

        if not is_redirect:
            break

        if location:
            # Resolve relative redirects
            current_url = urllib.parse.urljoin(current_url, location)
            redirect_count += 1
        else:
            break

    return hops


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that prevents automatic redirect following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Don't follow — let the caller handle it


# ---------------------------------------------------------------------------
# 2. classify_redirect_issues
# ---------------------------------------------------------------------------
def classify_redirect_issues(chain: List[Dict[str, Any]]) -> List[str]:
    """Classify redirect issues for a given chain.

    Detects:
      - redirect_loop: same URL appears twice
      - long_chain: 3+ redirect hops
      - http_to_https: redirect from http:// to https://
      - domain_change: redirect to a different domain
      - unnecessary_redirect: 301 to same URL with only a trailing slash difference
      - multi_subdomain_chain: redirects through multiple subdomains
    """
    issues: List[str] = []

    if not chain:
        return issues

    # Extract redirect hops (status code in REDIRECT_STATUSES)
    redirect_hops = [h for h in chain if h.get("status_code") in REDIRECT_STATUSES]

    # --- redirect_loop: same URL appears twice ---
    seen_urls: List[str] = []
    for h in chain:
        u = (h.get("url") or "").rstrip("/")
        if u in seen_urls:
            issues.append("redirect_loop")
            break
        seen_urls.append(u)

    # --- long_chain: 3+ redirect hops ---
    if len(redirect_hops) >= 3:
        issues.append("long_chain")

    # --- http_to_https ---
    for h in redirect_hops:
        src = h.get("url", "")
        loc = h.get("location") or ""
        if src.startswith("http://") and loc.startswith("https://"):
            issues.append("http_to_https")
            break

    # --- domain_change ---
    domains_seen: set = set()
    for h in chain:
        u = h.get("url", "")
        parsed = urllib.parse.urlparse(u)
        if parsed.netloc:
            # Strip leading www. for domain comparison
            base_domain = parsed.netloc.replace("www.", "", 1) if parsed.netloc.startswith("www.") else parsed.netloc
            domains_seen.add(base_domain)
    if len(domains_seen) > 1:
        issues.append("domain_change")

    # --- unnecessary_redirect: 301 to same URL with only trailing slash difference ---
    for h in redirect_hops:
        if str(h.get("status_code")) == "301":
            src = h.get("url", "")
            loc = h.get("location", "") or ""
            if loc:
                resolved = urllib.parse.urljoin(src, loc)
                # Same path, only trailing slash differs
                if src.rstrip("/") == resolved.rstrip("/") and src != resolved:
                    issues.append("unnecessary_redirect")
                    break

    # --- multi_subdomain_chain: redirects through multiple subdomains ---
    subdomains: set = set()
    for h in chain:
        u = h.get("url", "")
        parsed = urllib.parse.urlparse(u)
        if parsed.netloc:
            parts = parsed.netloc.split(".")
            if len(parts) >= 3:
                subdomains.add(parsed.netloc)
    if len(subdomains) >= 2:
        issues.append("multi_subdomain_chain")

    return issues


# ---------------------------------------------------------------------------
# 3. generate_redirect_report
# ---------------------------------------------------------------------------
def generate_redirect_report(
    urls: List[str],
    chains: Dict[str, List[Dict[str, Any]]],
    issues: Dict[str, List[str]],
    output_path: str,
) -> str:
    """Generate an HTML report and return the file path."""
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, "redirect_report.html")

    total_urls = len(urls)
    urls_with_redirects = sum(1 for u in urls if len(chains.get(u, [])) > 1)
    urls_with_issues = sum(1 for u in urls if issues.get(u))

    all_chain_lengths = [len(chains.get(u, [])) - 1 for u in urls if chains.get(u)]
    all_chain_lengths = [l for l in all_chain_lengths if l > 0]
    avg_chain_length = sum(all_chain_lengths) / len(all_chain_lengths) if all_chain_lengths else 0
    max_chain_length = max(all_chain_lengths) if all_chain_lengths else 0

    # Issue distribution
    issue_counts: Dict[str, int] = {}
    for u in urls:
        for issue in issues.get(u, []):
            issue_counts[issue] = issue_counts.get(issue, 0) + 1

    # Build HTML
    rows_html = ""
    for i, url in enumerate(urls):
        chain = chains.get(url, [])
        issue_list = issues.get(url, [])
        hop_count = max(0, len(chain) - 1)
        final_url = chain[-1]["url"] if chain else url
        status_codes = " → ".join(str(h["status_code"]) for h in chain) if chain else "N/A"
        issue_flags = ", ".join(issue_list) if issue_list else "None"
        chain_detail_id = f"chain_{i}"

        has_redirect = hop_count > 0
        has_issue = len(issue_list) > 0
        row_class = "row-issue" if has_issue else ("row-warning" if has_redirect else "row-ok")

        chain_detail_html = ""
        if has_redirect and len(chain) > 1:
            chain_steps = ""
            for j, hop in enumerate(chain):
                arrow = "→" if j > 0 else ""
                step_class = "step-redirect" if hop.get("status_code") in REDIRECT_STATUSES else "step-final"
                loc = hop.get("location") or ""
                chain_steps += f"""
                <div class="chain-step {step_class}">
                    <span class="step-arrow">{arrow}</span>
                    <span class="step-url">{html.escape(hop['url'] or '')}</span>
                    <span class="step-status">{html.escape(str(hop.get('status_code', 'N/A')))}</span>
                    <span class="step-loc">{html.escape(loc)}</span>
                    <span class="step-type">{html.escape(str(hop.get('redirect_type', '') or ''))}</span>
                </div>"""
            chain_detail_html = f"""
            <div class="chain-detail" id="{chain_detail_id}" style="display:none;">
                {chain_steps}
            </div>"""

        rows_html += f"""
            <tr class="{row_class}" onclick="document.getElementById('{chain_detail_id}').style.display = (document.getElementById('{chain_detail_id}').style.display === 'none' ? 'block' : 'none');" style="cursor:pointer;">
                <td>{html.escape(url)}</td>
                <td>{html.escape(final_url)}</td>
                <td>{hop_count}</td>
                <td>{html.escape(status_codes)}</td>
                <td>{html.escape(issue_flags)}</td>
            </tr>
            <tr><td colspan="5" style="padding:0;border:none;">
                {chain_detail_html}
            </td></tr>"""

    # Recommendations
    recommendations_html = ""
    fixable = []
    for u in urls:
        issue_list = issues.get(u, [])
        chain = chains.get(u, [])
        if "redirect_loop" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — Redirect loop detected. Fix the redirect rule causing the loop.</li>")
        if "long_chain" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — Long chain ({len(chain)-1} hops). Simplify to a direct 301 to the final URL.</li>")
        if "http_to_https" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — HTTP to HTTPS redirect. Ensure the canonical URL uses HTTPS and update internal links.</li>")
        if "domain_change" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — Redirect to different domain. Verify this is intentional (e.g., www to non-www) and update links to point directly to the final domain.</li>")
        if "unnecessary_redirect" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — Unnecessary trailing-slash redirect. Fix URL normalization to avoid this redirect.</li>")
        if "multi_subdomain_chain" in issue_list:
            fixable.append(f"<li><strong>{html.escape(u)}</strong> — Redirect chain through multiple subdomains. Consolidate to a single redirect.</li>")

    if fixable:
        recommendations_html = "<ul class='rec-list'>" + "".join(fixable) + "</ul>"
    else:
        recommendations_html = "<p>No actionable issues found. All redirects are direct and appropriate.</p>"

    # Issue distribution bars
    issue_dist_html = ""
    issue_labels = {
        "redirect_loop": "Redirect Loop",
        "long_chain": "Long Chain (3+ hops)",
        "http_to_https": "HTTP → HTTPS",
        "domain_change": "Domain Change",
        "unnecessary_redirect": "Unnecessary Redirect",
        "multi_subdomain_chain": "Multi-Subdomain Chain",
    }
    if issue_counts:
        max_count = max(issue_counts.values())
        for key, label in issue_labels.items():
            count = issue_counts.get(key, 0)
            if count > 0:
                pct = (count / max_count * 100) if max_count else 0
                issue_dist_html += f"""
                <div class="issue-bar-row">
                    <span class="issue-bar-label">{label}</span>
                    <div class="issue-bar-track"><div class="issue-bar-fill" style="width:{pct}%"></div></div>
                    <span class="issue-bar-count">{count}</span>
                </div>"""
    else:
        issue_dist_html = "<p>No issues detected across all URLs.</p>"

    # Full chains section
    full_chains_html = ""
    for url in urls:
        chain = chains.get(url, [])
        if len(chain) <= 1:
            continue
        chain_steps = ""
        for j, hop in enumerate(chain):
            arrow = "→" if j > 0 else ""
            step_class = "step-redirect" if hop.get("status_code") in REDIRECT_STATUSES else "step-final"
            loc = hop.get("location") or ""
            chain_steps += f"""
                <div class="chain-step {step_class}">
                    <span class="step-arrow">{arrow}</span>
                    <span class="step-url">{html.escape(hop['url'] or '')}</span>
                    <span class="step-status badge">{html.escape(str(hop.get('status_code', 'N/A')))}</span>
                    <span class="step-loc">{html.escape(loc)}</span>
                    <span class="step-type">{html.escape(str(hop.get('redirect_type', '') or ''))}</span>
                </div>"""
        issue_list = issues.get(url, [])
        issue_tags = " ".join(f'<span class="issue-tag">{html.escape(i)}</span>' for i in issue_list)
        full_chains_html += f"""
        <div class="full-chain-block">
            <h3 class="chain-title">{html.escape(url)}</h3>
            <div class="issue-tags">{issue_tags}</div>
            {chain_steps}
        </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Redirect Chain Analyzer Report</title>
<style>
:root {{
    --primary: #6366f1;
    --primary-light: #818cf8;
    --primary-dark: #4f46e5;
    --bg: #f8fafc;
    --card-bg: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --green: #10b981;
    --green-bg: #d1fae5;
    --amber: #f59e0b;
    --amber-bg: #fef3c7;
    --red: #ef4444;
    --red-bg: #fee2e2;
    --blue: #3b82f6;
    --blue-bg: #dbeafe;
    --radius: 12px;
    --shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:var(--bg); color:var(--text); padding:24px; line-height:1.6; }}
h1 {{ font-size:28px; font-weight:700; margin-bottom:8px; }}
.subtitle {{ color:var(--text-muted); font-size:14px; margin-bottom:32px; }}
.container {{ max-width:1200px; margin:0 auto; }}
.section {{ margin-bottom:40px; }}
.section-title {{ font-size:20px; font-weight:600; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid var(--border); }}

/* Summary cards */
.cards {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:16px; }}
.card {{ background:var(--card-bg); border-radius:var(--radius); padding:24px; box-shadow:var(--shadow); border:1px solid var(--border); }}
.card-value {{ font-size:36px; font-weight:700; color:var(--primary); }}
.card-label {{ font-size:13px; color:var(--text-muted); margin-top:4px; text-transform:uppercase; letter-spacing:0.5px; }}
.card.issue .card-value {{ color:var(--red); }}
.card.redirect .card-value {{ color:var(--amber); }}

/* Issue distribution */
.issue-bar-row {{ display:flex; align-items:center; gap:12px; margin-bottom:12px; }}
.issue-bar-label {{ width:200px; font-size:14px; font-weight:500; }}
.issue-bar-track {{ flex:1; height:24px; background:var(--border); border-radius:6px; overflow:hidden; }}
.issue-bar-fill {{ height:100%; background:var(--primary); border-radius:6px; transition:width 0.3s; }}
.issue-bar-count {{ width:40px; text-align:right; font-weight:600; }}

/* Table */
table {{ width:100%; border-collapse:collapse; background:var(--card-bg); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow); }}
th {{ background:var(--primary); color:white; padding:12px 16px; text-align:left; font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }}
td {{ padding:12px 16px; border-bottom:1px solid var(--border); font-size:14px; }}
tr:last-child td {{ border-bottom:none; }}
.row-ok {{ }}
.row-warning {{ background:var(--amber-bg); }}
.row-issue {{ background:var(--red-bg); }}
tr:hover {{ opacity:0.85; }}

/* Chain steps */
.chain-detail {{ padding:16px; background:#f1f5f9; border-radius:8px; margin:4px 0; }}
.chain-step {{ display:flex; align-items:center; gap:10px; padding:6px 0; font-size:13px; font-family:'SF Mono',Monaco,monospace; }}
.step-arrow {{ color:var(--primary); font-weight:bold; width:20px; text-align:center; }}
.step-url {{ flex:1; word-break:break-all; color:var(--text); }}
.step-status {{ color:var(--text-muted); font-weight:600; min-width:40px; }}
.step-loc {{ color:var(--blue); word-break:break-all; }}
.step-type {{ color:var(--text-muted); min-width:60px; }}
.step-final .step-url {{ color:var(--green); font-weight:600; }}
.step-redirect .step-url {{ color:var(--amber); }}
.badge {{ background:var(--blue-bg); padding:2px 8px; border-radius:4px; font-size:12px; }}

/* Full chains */
.full-chain-block {{ background:var(--card-bg); border-radius:var(--radius); padding:24px; margin-bottom:16px; box-shadow:var(--shadow); border:1px solid var(--border); }}
.chain-title {{ font-size:16px; font-weight:600; margin-bottom:8px; word-break:break-all; }}
.issue-tags {{ margin-bottom:12px; }}
.issue-tag {{ display:inline-block; background:var(--red-bg); color:var(--red); padding:2px 10px; border-radius:4px; font-size:12px; margin-right:4px; margin-bottom:4px; font-weight:500; }}

/* Recommendations */
.rec-list {{ list-style:none; }}
.rec-list li {{ background:var(--card-bg); border-left:4px solid var(--primary); padding:12px 16px; margin-bottom:8px; border-radius:0 8px 8px 0; box-shadow:var(--shadow); font-size:14px; }}
.rec-list strong {{ color:var(--primary-dark); }}

.footer {{ text-align:center; color:var(--text-muted); font-size:12px; margin-top:40px; padding-top:20px; border-top:1px solid var(--border); }}
</style>
</head>
<body>
<div class="container">
    <h1>🔗 Redirect Chain Analyzer Report</h1>
    <p class="subtitle">Generated on {time.strftime('%Y-%m-%d %H:%M:%S')}</p>

    <!-- Summary Cards -->
    <div class="section">
        <h2 class="section-title">Summary</h2>
        <div class="cards">
            <div class="card"><div class="card-value">{total_urls}</div><div class="card-label">Total URLs Checked</div></div>
            <div class="card redirect"><div class="card-value">{urls_with_redirects}</div><div class="card-label">URLs with Redirects</div></div>
            <div class="card issue"><div class="card-value">{urls_with_issues}</div><div class="card-label">URLs with Issues</div></div>
            <div class="card"><div class="card-value">{avg_chain_length:.1f}</div><div class="card-label">Avg Chain Length</div></div>
            <div class="card"><div class="card-value">{max_chain_length}</div><div class="card-label">Max Chain Length</div></div>
        </div>
    </div>

    <!-- Issue Distribution -->
    <div class="section">
        <h2 class="section-title">Issue Distribution</h2>
        {issue_dist_html}
    </div>

    <!-- URL Redirect Table -->
    <div class="section">
        <h2 class="section-title">URL Redirect Summary</h2>
        <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px;">Click a row to expand the full redirect chain.</p>
        <table>
            <thead><tr><th>Original URL</th><th>Final URL</th><th>Hops</th><th>Status Codes</th><th>Issues</th></tr></thead>
            <tbody>{rows_html}
            </tbody>
        </table>
    </div>

    <!-- Full Redirect Chains -->
    <div class="section">
        <h2 class="section-title">Full Redirect Chains</h2>
        {full_chains_html if full_chains_html else '<p>No redirect chains found.</p>'}
    </div>

    <!-- Recommendations -->
    <div class="section">
        <h2 class="section-title">Recommendations</h2>
        {recommendations_html}
    </div>

    <div class="footer">
        Redirect Chain Analyzer — Zenfusion SEO Tools
    </div>
</div>
</body>
</html>"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    return report_file


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def parse_sitemap(sitemap_url: str) -> List[str]:
    """Fetch and parse a sitemap XML, return list of URLs."""
    try:
        req = urllib.request.Request(sitemap_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = resp.read()
    except Exception as e:
        print(f"Error fetching sitemap: {e}", file=sys.stderr)
        return []

    urls: List[str] = []
    try:
        root = ET.fromstring(data)
        # Handle namespaces
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            if loc.text:
                urls.append(loc.text.strip())
        if not urls:
            # Fallback: no namespace
            for loc in root.iter():
                if loc.tag.endswith("loc") and loc.text:
                    urls.append(loc.text.strip())
    except ET.ParseError as e:
        print(f"Error parsing sitemap XML: {e}", file=sys.stderr)
    return urls


def read_urls_from_file(path: str) -> List[str]:
    """Read URLs from a file, one per line."""
    try:
        with open(path, "r") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        print(f"File not found: {path}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Check URLs for redirect chains and report issues.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s --urls 'https://example.com,https://www.example.com' --output /tmp/redirects
  %(prog)s --sitemap https://example.com/sitemap.xml --output /tmp/redirects
  %(prog)s --file urls.txt --output /tmp/redirects
""",
    )
    parser.add_argument("--urls", help="Comma-separated list of URLs to check")
    parser.add_argument("--sitemap", help="Sitemap URL to parse for URLs")
    parser.add_argument("--file", help="Path to file containing URLs (one per line)")
    parser.add_argument("--output", default="/tmp/redirect_report", help="Output directory for HTML report")
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS, help="Maximum redirect hops to follow (default: 10)")

    args = parser.parse_args()

    # Gather URLs
    urls: List[str] = []
    if args.urls:
        urls.extend(u.strip() for u in args.urls.split(",") if u.strip())
    if args.sitemap:
        urls.extend(parse_sitemap(args.sitemap))
    if args.file:
        urls.extend(read_urls_from_file(args.file))

    if not urls:
        parser.print_help()
        sys.exit(1)

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)
    urls = unique_urls

    print(f"Checking {len(urls)} URL(s) for redirect chains...\n")

    chains: Dict[str, List[Dict[str, Any]]] = {}
    issues: Dict[str, List[str]] = {}

    for i, url in enumerate(urls, 1):
        print(f"  [{i}/{len(urls)}] {url}")
        chain = trace_redirects(url, max_hops=args.max_hops)
        chains[url] = chain
        issue_list = classify_redirect_issues(chain)
        issues[url] = issue_list

        hop_count = max(0, len(chain) - 1)
        if hop_count > 0:
            final = chain[-1]["url"] if chain else "?"
            print(f"    → {hop_count} hop(s) → {final}")
        else:
            print(f"    → No redirects (final, {chain[-1]['status_code'] if chain else '?'})")
        if issue_list:
            print(f"    ⚠ Issues: {', '.join(issue_list)}")
        print()

    # Generate report
    report_path = generate_redirect_report(urls, chains, issues, args.output)
    print(f"\n✅ Report generated: {report_path}")
    return


if __name__ == "__main__":
    main()
