#!/usr/bin/env python3
"""
Zenfusion Report QA Validator
=============================
Validates HTML reports before publishing to GitHub Pages.
Fails (exit 1) if any section has missing data, zeros where real data expected,
or empty sections. Reports every issue found.

Usage:
    python3 report_qa.py <report.html>
    python3 report_qa.py /tmp/zenfusion-audits/softpro/seo-visibility-report.html

Exit codes:
    0 = PASS (all checks passed)
    1 = FAIL (issues found — do NOT publish)
"""

import sys
import re
import os
from html import unescape


# ============================================================
# QA CHECKS
# ============================================================

def check_no_zeros_in_metrics(html, report_type):
    """Check that metric cards/summary section don't show all zeros."""
    issues = []
    
    # Find all metric value patterns: >0< or >0.0<
    # But only in summary/executive sections, not in data tables where 0 is valid
    summary_section = ""
    for marker in ['Executive Summary', 'Summary', 'Overview', 'summary-cards', 'metric-card']:
        pos = html.find(marker)
        if pos > 0:
            # Get 2000 chars after the marker
            summary_section = html[pos:pos+2000]
            break
    
    if summary_section:
        # Check for patterns like >0< or >0.0< that indicate missing data
        zero_matches = re.findall(r'>\s*(0|0\.0|0\.00)\s*<', summary_section)
        if len(zero_matches) >= 3:
            issues.append(f"SUMMARY ZEROS: {len(zero_matches)} metrics show 0 in summary section — likely missing data")
    
    return issues


def check_no_empty_sections(html):
    """Check that no section has a heading but no content beneath it."""
    issues = []
    
    # Remove HTML comments first
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    
    # Find all h2 headings (main sections — h3s are subsections)
    headings = re.finditer(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL)
    
    for match in headings:
        heading_text = unescape(re.sub(r'<[^>]+>', '', match.group(1))).strip()
        if not heading_text:
            continue
        
        # Get content after this heading until the next h2
        start = match.end()
        next_heading = re.search(r'<h2[^>]*>', html[start:])
        if next_heading:
            end = start + next_heading.start()
        else:
            end = start + 5000  # Check more chars after last heading
        
        section_content = html[start:end]
        # Strip HTML tags to get text
        text_content = re.sub(r'<[^>]+>', '', section_content).strip()
        
        # Check for empty (less than 20 chars of text means no real content)
        if len(text_content) < 20 and not any(
            skip in heading_text.lower() 
            for skip in ['footer', 'generated', 'copyright']
        ):
            issues.append(f"EMPTY SECTION: '{heading_text}' has no content")
    
    return issues


def check_no_placeholder_text(html):
    """Check for placeholder/TODO text that shouldn't be in published reports."""
    issues = []
    
    placeholders = [
        'TODO', 'FIXME', 'PLACEHOLDER', 'lorem ipsum', 'test data',
        'No data available', 'No results found',
        'Coming soon', 'TBD ',
    ]
    
    for p in placeholders:
        if p.lower() in html.lower():
            # Find context
            pos = html.lower().find(p.lower())
            context = re.sub(r'<[^>]+>', '', html[max(0, pos-50):pos+50]).strip()
            issues.append(f"PLACEHOLDER: Found '{p}' near: {context[:80]}")
    
    return issues


def check_table_data(html):
    """Check that data tables have actual data rows (not just headers)."""
    issues = []
    
    # Find all tables
    tables = re.finditer(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    
    for i, match in enumerate(tables, 1):
        table_content = match.group(1)
        
        # Count rows
        header_rows = len(re.findall(r'<tr[^>]*>.*?<th', table_content, re.DOTALL))
        data_rows = len(re.findall(r'<tr[^>]*>.*?<td', table_content, re.DOTALL))
        
        if data_rows == 0:
            issues.append(f"EMPTY TABLE #{i}: Has headers but no data rows")
    
    return issues


def check_no_none_in_html(html):
    """Check that 'None' doesn't appear as a data value (Python str(None) leak)."""
    issues = []
    
    # Look for None as a cell value or card value
    none_matches = re.findall(r'>\s*None\s*<', html)
    if none_matches:
        issues.append(f"NONE VALUES: {len(none_matches)} 'None' values found in HTML (Python None leaked to output)")
    
    return issues


def check_no_question_marks(html):
    """Check that '?' doesn't appear as a data value (incomplete API data)."""
    issues = []
    
    # Look for ? as a cell value (not in URLs or text)
    q_matches = re.findall(r'<td[^>]*>\s*\?\s*</td>', html)
    if q_matches:
        issues.append(f"QUESTION MARKS: {len(q_matches)} '?' values in table cells (incomplete data)")
    
    return issues


def check_metric_cards_have_values(html):
    """Check that metric cards in the top section have non-zero values.
    Only flags summary/overview cards — issue count cards with 0 are valid (no issues = good)."""
    issues = []
    
    # Issue-count labels where 0 is a valid value (no issues found = good)
    issue_labels = [
        'missing', 'broken', 'duplicate', 'critical', 'warning',
        'non-https', 'spam', 'error', 'noindex', 'blocked',
        'title too', 'multiple h1', 'too short', 'too long',
        'non-https', 'pages with', 'no canonical', 'redirect',
    ]
    
    # Find all stat/metric card patterns
    card_patterns = [
        r'<div class="[^"]*(?:stat-val|metric-value|num|stat-num|summary-num)[^"]*"[^>]*>\s*(\d+\.?\d*)\s*</div>\s*<div class="[^"]*(?:label|stat-label)[^"]*"[^>]*>\s*(.*?)\s*</div>',
    ]
    
    zero_cards = 0
    total_cards = 0
    for pattern in card_patterns:
        matches = re.findall(pattern, html, re.DOTALL)
        for val, label in matches:
            label_lower = label.lower().strip()
            # Skip issue-count cards where 0 is valid
            if any(il in label_lower for il in issue_labels):
                continue
            total_cards += 1
            if float(val) == 0:
                zero_cards += 1
    
    if total_cards >= 3 and zero_cards == total_cards:
        issues.append(f"ALL METRIC CARDS ZERO: {zero_cards}/{total_cards} cards show 0 — top section has no data")
    elif zero_cards > total_cards * 0.7 and total_cards >= 4:
        issues.append(f"MOSTLY ZEROS: {zero_cards}/{total_cards} metric cards show 0 — likely missing data")
    
    return issues


def check_links_are_valid(html):
    """Check that internal report links are not broken (basic href check)."""
    issues = []
    
    # Find all href links
    hrefs = re.findall(r'href="([^"]+)"', html)
    for href in hrefs:
        if href.startswith('#') or href.startswith('mailto:') or href.startswith('tel:'):
            continue
        if href.startswith('http') and 'github.io' not in href:
            continue  # External links are fine
        if href.startswith('//'):
            continue  # Protocol-relative
    
    return issues


def run_qa(html_path):
    """Run all QA checks on an HTML report file."""
    if not os.path.exists(html_path):
        print(f"✗ FILE NOT FOUND: {html_path}")
        return False
    
    with open(html_path) as f:
        html = f.read()
    
    if len(html) < 500:
        print(f"✗ FILE TOO SMALL: {len(html)} bytes — report likely incomplete")
        return False
    
    all_issues = []
    
    # Run all checks
    all_issues.extend(check_no_zeros_in_metrics(html, ''))
    all_issues.extend(check_no_empty_sections(html))
    all_issues.extend(check_no_placeholder_text(html))
    all_issues.extend(check_table_data(html))
    all_issues.extend(check_no_none_in_html(html))
    all_issues.extend(check_no_question_marks(html))
    all_issues.extend(check_metric_cards_have_values(html))
    
    # Report results
    print(f"\n{'='*60}")
    print(f"  QA VALIDATION REPORT")
    print(f"  File: {os.path.basename(html_path)}")
    print(f"  Size: {len(html):,} bytes")
    print(f"{'='*60}\n")
    
    if not all_issues:
        print("  ✅ PASS — All checks passed. Safe to publish.\n")
        return True
    else:
        print(f"  ❌ FAIL — {len(all_issues)} issue(s) found:\n")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i}. {issue}")
        print(f"\n  DO NOT PUBLISH until issues are resolved.\n")
        return False


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 report_qa.py <report.html>")
        sys.exit(1)
    
    success = run_qa(sys.argv[1])
    sys.exit(0 if success else 1)
