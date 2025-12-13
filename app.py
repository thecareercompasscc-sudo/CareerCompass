import os
import csv
import json
import socket
import secrets
import string
from datetime import datetime
from pathlib import Path

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from openai import OpenAI
from docx import Document
from PyPDF2 import PdfReader

# ---- Global network timeout baseline ----
socket.setdefaulttimeout(5)

# ---------- Flask setup ----------
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

UPLOAD_FOLDER = BASE_DIR / "uploads"
EMAIL_LIST_FILE = BASE_DIR / "email_list.csv"

UPLOAD_FOLDER.mkdir(exist_ok=True)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

# ---------- OpenAI client (with timeout) ----------
client = OpenAI(timeout=30)  # uses OPENAI_API_KEY from env

# ---------- CareerCompass System Prompt (NEW SPEC) ----------
SYSTEM_PROMPT = """
You are CareerCompass, an AI career analyst. Produce a structured, realistic, highly readable career report based on the user’s input (CV text or education/work summary).

Your outputs must be:
- grounded and realistic (no hype, no guarantees)
- UK-first by default unless another location is clearly stated
- student-friendly where appropriate, but still valuable for mid-career users
- easy to scan on mobile (short paragraphs, bullets, clear headings)

CRITICAL: Stage awareness
Infer ONE primary stage from the input and adapt tone + advice:
A) Pre-16 (Year 9–11 / GCSE stage)
B) Post-16 (Year 12–13 / sixth form / college / apprenticeship decisions)
C) University student
D) Graduate / early-career (0–3 years)
E) Mid-career (3–10 years)
F) Career changer / returner
G) Unknown (insufficient info; make conservative assumptions)

Evidence notes (light touch, non-academic)
Where you make claims about salary ranges, demand, typical requirements, or progression timelines, include a short line:
<strong>Evidence note:</strong> Based on reputable public sources such as ONS/HESA/Prospects/CIPD and aggregated job market ranges (Indeed/Reed/Glassdoor).
Do NOT include links. Do NOT add a “sources & methodology” section.

Formatting (STRICT)
Output ONLY HTML.
Use only: <div>, <h2>, <h3>, <p>, <ul>, <li>, <strong>.
Wrap each group in <div class="section"> ... </div>.
Inside each group, use <h3> subsections.

Pill navigation dependency (IMPORTANT)
Your HTML must include these THREE group headings exactly, each inside its own <div class="section">:
- SECTION A — Candidate Overview
- SECTION B — Candidate → Hired
- SECTION C — Job Search Resources

Within each subsection, use:
1) detailed explanation
2) bullets/scores/ranges (where relevant)
3) a TL;DR line at the end: <p><strong>TL;DR:</strong> ...</p>

Do not overwhelm: prefer fewer, higher-impact recommendations.
"""

# ---------- Helper: build user prompt ----------
def build_user_prompt(cv_text: str) -> str:
    trimmed = (cv_text or "")[:8000]
    return f"""
Analyse the following input and generate a CareerCompass report in HTML.

The report must be:
- stage-aware (A–G)
- highly readable and scannable
- financially grounded (salary ranges, progression)
- includes light “Evidence note” lines only where relevant
- uses Detail → Evidence → TL;DR inside each subsection
- contains the three group headings SECTION A/B/C exactly as instructed

INPUT:
{trimmed}
"""

# ---------- Helper: extract text from uploaded file ----------
def extract_text_from_upload(file_storage) -> str:
    if not file_storage or file_storage.filename == "":
        return ""

    filename = file_storage.filename
    ext = Path(filename).suffix.lower()

    # Avoid collisions
    temp_path = UPLOAD_FOLDER / f"{secrets.token_hex(8)}_{Path(filename).name}"
    file_storage.save(temp_path)

    text = ""
    try:
        if ext == ".txt":
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext == ".docx":
            doc = Document(str(temp_path))
            text = "\n".join(p.text for p in doc.paragraphs)
        elif ext == ".pdf":
            with open(temp_path, "rb") as f:
                reader = PdfReader(f)
                chunks = []
                for page in reader.pages:
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        chunks.append(page_text)
                text = "\n".join(chunks)
        else:
            text = ""
    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass

    return text.strip()

# ---------- Helper: call OpenAI and get report HTML ----------
def generate_report_html(cv_text: str) -> str:
    if not cv_text or not cv_text.strip():
        return "<div class='section'><h2>Error</h2><p>No CV text provided.</p></div>"

    try:
        app.logger.info("Calling OpenAI for report generation...")

        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(cv_text)},
            ],
            temperature=0.3,
            max_tokens=3200,
        )

        app.logger.info("OpenAI call succeeded.")
        html = response.choices[0].message.content or ""

        # Safety: if model returns non-HTML, wrap it minimally
        if "<div" not in html or "SECTION A" not in html:
            return (
                "<div class='section'>"
                "<h2>SECTION A — Candidate Overview</h2>"
                "<p>We generated your report, but formatting was unexpected. Here is the content:</p>"
                f"<p>{html}</p>"
                "</div>"
            )

        return html

    except Exception as e:
        app.logger.error(f"OpenAI API error: {e}")
        return """
        <div class='section'>
          <h2>Temporary issue generating your report</h2>
          <p>
            We ran into a problem while generating your CareerCompass report.
            This is usually due to a temporary issue talking to the AI model.
          </p>
          <p>
            Please wait a moment and try again. If this keeps happening,
            reply to any CareerCompass email with a screenshot of this page.
          </p>
        </div>
        """

# ---------- Helper: generate referral code ----------
def generate_referral_code(email: str) -> str:
    email = (email or "").strip()
    if not email:
        prefix = "CC"
    else:
        local_part = email.split("@")[0]
        letters = [ch for ch in local_part if ch.isalpha()]
        prefix = "".join(letters[:2]).upper() or "CC"

    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return prefix + digits

# ---------- Helper: store email in CSV mailing list ----------
def save_email_to_list(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    file_exists = EMAIL_LIST_FILE.exists()
    with EMAIL_LIST_FILE.open(mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["email", "timestamp_utc"])
        writer.writerow([email, datetime.utcnow().isoformat()])

# ---------- Helper: sync a single email to primary Google Sheet ----------
def sync_email_to_sheet(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    creds_json = os.environ.get("GOOGLE_SERVICE_JSON")
    if not creds_json:
        app.logger.warning("GOOGLE_SERVICE_JSON not set; skipping sheet sync.")
        return

    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        app.logger.error("gspread/oauth2client not installed; skipping sheet sync.")
        return

    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError:
        app.logger.error("GOOGLE_SERVICE_JSON is not valid JSON; skipping sheet sync.")
        return

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    client_gs = gspread.authorize(creds)

    SHEET_NAME = "EMAIL LISTS"
    sheet = client_gs.open(SHEET_NAME).sheet1

    existing = set()
    records = sheet.get_all_records()
    for row in records:
        existing_email = (row.get("email") or "").strip().lower()
        if existing_email:
            existing.add(existing_email)

    if email not in existing:
        timestamp = datetime.utcnow().isoformat()
        sheet.append_row([email, timestamp])
        app.logger.info(f"Added {email} to primary Google Sheet.")
    else:
        app.logger.info(f"Email {email} already in primary Google Sheet; skipping.")

# ---------- Helper: sync a single email to V1 Feedback Results / User List ----------
def sync_email_to_feedback_sheet(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    creds_json = os.environ.get("GOOGLE_SERVICE_JSON")
    if not creds_json:
        app.logger.warning("GOOGLE_SERVICE_JSON not set; skipping feedback sheet sync.")
        return

    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        app.logger.error("gspread/oauth2client not installed; skipping feedback sheet sync.")
        return

    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError:
        app.logger.error("GOOGLE_SERVICE_JSON is not valid JSON; skipping feedback sheet sync.")
        return

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    client_gs = gspread.authorize(creds)

    SHEET_NAME = "V1 Feedback Results"
    WORKSHEET_NAME = "User List"
    sheet = client_gs.open(SHEET_NAME).worksheet(WORKSHEET_NAME)

    existing = set()
    records = sheet.get_all_records()
    for row in records:
        existing_email = (row.get("email") or "").strip().lower()
        if existing_email:
            existing.add(existing_email)

    if email not in existing:
        timestamp = datetime.utcnow().isoformat()
        sheet.append_row([email, timestamp])
        app.logger.info(f"Added {email} to feedback Google Sheet.")
    else:
        app.logger.info(f"Email {email} already in feedback Google Sheet; skipping.")

# ---------- Helper: send report email via Resend API ----------
def send_report_email(
    recipient_email: str,
    html_report: str,
    referral_code: str,
    feedback_form_url: str,
) -> None:
    recipient_email = (recipient_email or "").strip()
    if not recipient_email:
        app.logger.info("No recipient email provided – skipping email send.")
        return

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        app.logger.error("RESEND_API_KEY not set; cannot send email.")
        return

    from_email = os.environ.get(
        "RESEND_FROM_EMAIL",
        "CareerCompass <report@career-compass.uk>",
    )

    subject = "Your CareerCompass report + Lifetime Membership draw"
    feedback_form_url = (feedback_form_url or "").strip()
    share_url = "https://career-compass.uk"

    text_body = (
        "Hi,\n\n"
        "Thanks for trying the CareerCompass beta.\n\n"
        "You’ll find your full CareerCompass report at the bottom of this email.\n"
        "If you can spare 30 seconds to share feedback, you can enter this month’s\n"
        "draw for CareerCompass Lifetime Membership.\n\n"
        "Best,\nCareerCompass"
    )

    html_parts = []
    html_parts.append(
        "<div style='font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto;'>"
    )

    html_parts.append(
        """
        <p style="font-size:14px; line-height:1.6;">
          Hi,<br><br>
          Thanks for trying the CareerCompass beta. 🙌<br>
          You’ll find your full CareerCompass report at the bottom of this email 👇
        </p>
        <p style="font-size:14px; line-height:1.6;">
          If you want to help keep CareerCompass free for students and early-career people,
          the biggest thing you can do is share <strong>30 seconds of feedback</strong>.
          As a thank you, you’ll be entered into this month’s draw for
          <strong>CareerCompass Lifetime Membership</strong>.
        </p>
        """
    )

    html_parts.append(
        """
        <h3 style="font-size:16px; margin:16px 0 6px;">🎁 Monthly prizes</h3>
        <p style="font-size:14px; line-height:1.6; margin:0 0 4px;">
          Each month we award <strong>6 Lifetime Memberships</strong>:
        </p>
        <ul style="font-size:14px; line-height:1.6; padding-left:20px; margin-top:4px;">
          <li><strong>3 winners</strong> chosen at random from all feedback submissions</li>
          <li><strong>3 winners</strong> from the top referrers</li>
        </ul>
        """
    )

    if feedback_form_url:
        html_parts.append(
            f"""
            <p style="font-size:14px; margin:12px 0;">
              👉 <strong>Complete the feedback form and enter the draw:</strong><br>
              <a href="{feedback_form_url}" style="color:#0957D0;">{feedback_form_url}</a>
            </p>
            """
        )

    html_parts.append("<hr style='margin:20px 0; border:none; border-top:1px solid #dddddd;'>")

    html_parts.append(
        f"""
        <h3 style="font-size:16px; margin:0 0 8px;">Your Lifetime Membership & referral code</h3>
        <p style="font-size:14px; line-height:1.6;">
          CareerCompass is free while we’re in beta, but this won’t always be the case.
          A <strong>Lifetime Membership</strong> means you’ll never pay for any of our future
          premium tools — including upcoming products focused on career progression,
          earning more, and building extra income streams.
        </p>
        <p style="font-size:14px; line-height:1.6;">
          Your personal referral code: <strong>{referral_code or "N/A"}</strong>
        </p>
        <p style="font-size:14px; line-height:1.6;">
          You can boost your chances of winning:
        </p>
        <ul style="font-size:14px; line-height:1.6; padding-left:20px;">
          <li>Entering someone else’s referral code in the feedback form → <strong>+1 point</strong></li>
          <li>Each person who enters your code in the form → <strong>+1 point</strong></li>
        </ul>
        <p style="font-size:14px; line-height:1.6;">
          More points = a higher chance of winning Lifetime Membership.
        </p>
        """
    )

    html_parts.append(
        f"""
        <p style="font-size:14px; line-height:1.6;">
          If you want to share CareerCompass directly, you can send this link to a friend 👇<br>
          <a href="{share_url}" style="color:#0957D0;">{share_url}</a>
        </p>
        <hr style="margin:20px 0; border:none; border-top:1px solid #dddddd;">
        """
    )

    html_parts.append(
        """
        <h3 style="font-size:16px; margin:0 0 8px;">✨ Bonus: Interview simulator prompt</h3>
        <p style="font-size:13px; line-height:1.6; margin:0 0 8px;">
          You can paste your CareerCompass report into ChatGPT (or any AI tool) and use this prompt:
        </p>
        <p style="font-size:13px; line-height:1.6; margin:0 0 8px;">
          <code style="font-family:Menlo,Consolas,monospace;background:#f5f5f5;padding:4px 6px;border-radius:3px;display:block;">
Here is my personalised career report from CareerCompass. Act as an interviewer for one of the roles you recommended. Ask me realistic interview questions one at a time. After each answer, give me honest but encouraging feedback and a stronger example answer based on my background.
          </code>
        </p>
        <hr style="margin:24px 0; border:none; border-top:1px solid #dddddd;">
        <h2 style="font-size:18px; margin-bottom:12px;">Your full CareerCompass report</h2>
        """
    )

    html_parts.append(html_report)

    html_parts.append(
        """
        <p style="font-size:14px; line-height:1.6; margin-top:24px;">
          Thanks for being part of the CareerCompass beta,<br>
          <strong>The CareerCompass Team</strong>
        </p>
        </div>
        """
    )

    html_body = "".join(html_parts)

    data = {
        "from": from_email,
        "to": [recipient_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=data,
            timeout=10,
        )
        if 200 <= resp.status_code < 300:
            app.logger.info(f"Email sent to {recipient_email} via Resend.")
        else:
            app.logger.error(
                f"Resend API error for {recipient_email}: "
                f"status={resp.status_code}, body={resp.text}"
            )
    except requests.RequestException as e:
        app.logger.error(f"Network error sending email via Resend to {recipient_email}: {e}")

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate_report():
    email = request.form.get("email", "").strip()
    text_box = request.form.get("cv_text", "").strip()
    file = request.files.get("cv_file")

    file_text = extract_text_from_upload(file)
    combined_cv = "\n\n".join(part for part in [text_box, file_text] if part).strip()

    if not combined_cv:
        flash("Please paste your CV or upload a valid file.", "error")
        return redirect(url_for("index"))

    report_html = generate_report_html(combined_cv)

    referral_code = generate_referral_code(email) if email else ""
    feedback_form_url = os.environ.get("FEEDBACK_FORM_URL", "")

    try:
        save_email_to_list(email)
    except Exception as e:
        app.logger.error(f"Failed to save email to local CSV list: {e}")

    try:
        sync_email_to_sheet(email)
    except Exception as e:
        app.logger.error(f"Failed to sync email to primary Google Sheet: {e}")

    try:
        sync_email_to_feedback_sheet(email)
    except Exception as e:
        app.logger.error(f"Failed to sync email to feedback Google Sheet: {e}")

    try:
        send_report_email(email, report_html, referral_code, feedback_form_url)
    except Exception as e:
        app.logger.error(f"Failed to send report email: {e}")

    return render_template(
        "report.html",
        email=email,
        report_html=report_html,
        download_url=None,
        referral_code=referral_code,
        feedback_form_url=feedback_form_url,
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
