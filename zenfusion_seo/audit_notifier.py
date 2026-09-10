#!/usr/bin/env python3
"""
Zenfusion Audit Notification System

Sends email notifications when audits are started and completed.
Usage:
  python3 audit_notifier.py --type started --audit-type "Page Audit" --brand "SoftPro" --keyword "salt free water softener" --url "https://www.softprowatersystems.com/page"
  python3 audit_notifier.py --type completed --audit-type "Page Audit" --brand "SoftPro" --keyword "salt free water softener" --report-url "https://loftenter.github.io/zenfusion-audits/softpro/page-audit.html"

Credentials in ~/.hermes/.env:
  EMAIL_ADDRESS (sender)
  EMAIL_PASSWORD (Gmail App Password)
"""

import smtplib
import argparse
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

RECIPIENTS = [
    "justin@justinlofton.com",
    "admin@zenfusion.com",
    "topshopteam8020@gmail.com",
]

SENDER_NAME = "Zenfusion Audit Bot"

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_creds():
    """Load email credentials from ~/.hermes/.env."""
    email_addr = os.environ.get("EMAIL_ADDRESS")
    email_pass = os.environ.get("EMAIL_PASSWORD")
    if not email_addr or not email_pass:
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("EMAIL_ADDRESS=") and not line.startswith("#"):
                        email_addr = line.split("=", 1)[1]
                    elif line.startswith("EMAIL_PASSWORD=") and not line.startswith("#"):
                        email_pass = line.split("=", 1)[1]
    if not email_addr or not email_pass:
        print("ERROR: EMAIL_ADDRESS and EMAIL_PASSWORD not found in env or ~/.hermes/.env")
        sys.exit(1)
    return email_addr, email_pass


def send_email(subject, body, recipients=None):
    """Send an HTML email to all recipients."""
    sender_email, sender_pass = get_creds()
    recipients = recipients or RECIPIENTS

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{sender_email}>"
    msg["To"] = ", ".join(recipients)

    # Plain text fallback (strip HTML tags)
    import re
    text_body = re.sub(r'<[^[:>]+>', '', body)
    msg.attach(MIMEText(text_body, "plain"))

    # HTML version (this is what email clients render)
    msg.attach(MIMEText(body, "html"))

    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        s.starttls()
        s.login(sender_email, sender_pass)
        s.sendmail(sender_email, recipients, msg.as_string())
        s.quit()
        print(f"Email sent to {len(recipients)} recipients: {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


# ── Notification templates ──────────────────────────────────────────────────

def notify_started(audit_type, brand, keyword=None, page_url=None):
    """Send 'Audit Started' notification."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    subject = f"🔄 Audit Started: {audit_type} for {brand}"

    body = f"""<html><body style="font-family: -apple-system, sans-serif; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
<h2 style="color: #4A90D9;">🔄 Audit Started</h2>
<table style="border-collapse: collapse; width: 100%;">
<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Audit Type</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{audit_type}</td></tr>
<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Brand</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{brand}</td></tr>
"""

    if keyword:
        body += f'<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Keyword</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{keyword}</td></tr>\n'
    if page_url:
        body += f'<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Page URL</td><td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="{page_url}">{page_url}</a></td></tr>\n'

    body += f"""<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Started</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{now}</td></tr>
</table>
<p style="margin-top: 20px; color: #666; font-size: 13px;">You'll receive another email when the audit is complete with a link to the full report.</p>
<p style="color: #999; font-size: 12px;">— Zenfusion Audit Bot</p>
</body></html>"""

    send_email(subject, body)


def notify_completed(audit_type, brand, keyword=None, report_url=None, summary=None):
    """Send 'Audit Complete' notification with report link."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    subject = f"✅ Audit Complete: {audit_type} for {brand}"

    body = f"""<html><body style="font-family: -apple-system, sans-serif; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
<h2 style="color: #27ae60;">✅ Audit Complete</h2>
<table style="border-collapse: collapse; width: 100%;">
<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Audit Type</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{audit_type}</td></tr>
<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Brand</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{brand}</td></tr>
"""

    if keyword:
        body += f'<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Keyword</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{keyword}</td></tr>\n'
    if report_url:
        body += f'<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Report</td><td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="{report_url}" style="background: #4A90D9; color: white; padding: 8px 20px; border-radius: 6px; text-decoration: none; display: inline-block; margin-top: 4px;">📄 View Full Report</a></td></tr>\n'
    if summary:
        body += f'<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Summary</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{summary}</td></tr>\n'

    body += f"""<tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Completed</td><td style="padding: 8px; border-bottom: 1px solid #eee;">{now}</td></tr>
</table>
<p style="margin-top: 20px; color: #666; font-size: 13px;">The full interactive report is now live on GitHub Pages. Click above to view.</p>
<p style="color: #999; font-size: 12px;">— Zenfusion Audit Bot</p>
</body></html>"""

    send_email(subject, body)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zenfusion Audit Notification System")
    parser.add_argument("--type", required=True, choices=["started", "completed"],
                        help="Notification type")
    parser.add_argument("--audit-type", required=True, help="Type of audit (e.g. 'Page Audit')")
    parser.add_argument("--brand", required=True, help="Brand name (e.g. 'SoftPro')")
    parser.add_argument("--keyword", default=None, help="Target keyword")
    parser.add_argument("--page-url", default=None, help="Page URL being audited")
    parser.add_argument("--report-url", default=None, help="URL to the completed report")
    parser.add_argument("--summary", default=None, help="Brief summary of results")
    args = parser.parse_args()

    if args.type == "started":
        notify_started(args.audit_type, args.brand, args.keyword, args.page_url)
    else:
        notify_completed(args.audit_type, args.brand, args.keyword, args.report_url, args.summary)
