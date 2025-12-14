import os
import csv
import json
import socket
import secrets
import string
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for, flash
from openai import OpenAI
from docx import Document
from PyPDF2 import PdfReader

socket.setdefaulttimeout(5)

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

UPLOAD_FOLDER = BASE_DIR / "uploads"
EMAIL_LIST_FILE = BASE_DIR / "email_list.csv"
UPLOAD_FOLDER.mkdir(exist_ok=True)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

client = OpenAI(timeout=30)  # uses OPENAI_API_KEY from env


# ============================================================
# ✅ NEW SYSTEM PROMPT (matches final spec + your new notes)
# ============================================================
SYSTEM_PROMPT = """
You are CareerCompass — an AI career coach/analyst that produces school-safe, user-friendly career reports.

THIS IS CRITICAL:
Write directly TO the user in second-person ("you", "your").
Do NOT write about them in third-person ("Charlie is...").

TRUST / SAFETY RULES (NO HALLUCINATION):
- Do NOT invent employers, job titles, degrees, grades, dates, companies, salaries, or achievements.
- If dates/grades are unclear: do NOT guess. Say “recent” or omit.
- Never estimate what the user was paid in previous jobs. It's irrelevant and often wrong.
- Only give salary ranges for target roles and progression, not past roles.

DO NOT REHASH THE CV:
- Do not copy the CV back to them or list everything they wrote.
- Briefly acknowledge key signals, then focus on INSIGHT: what it means, what it unlocks, what’s missing, what to do next.

STAGE AWARENESS (choose one primary stage):
A) Pre-16 (GCSE)
B) Post-16 (A-levels/college/apprenticeship decisions)
C) University student
D) Graduate / early-career (0–3 years)
E) Mid-career (3–10 years)
F) Career changer / returner
G) Unknown (not enough info)

You must state the detected stage in Candidate Snapshot.

NARRATIVE / “STORY” RULE:
The report must flow like a journey:
- Past signals (what your background suggests)
- Present reality (where you are now)
- Next 0–3 months (what to do first)
- Next 1–3 years (likely progression)
- Longer-term earning ceiling levers (what increases future salary)

READABILITY / SPACING RULES:
- Keep paragraphs to 1–3 sentences max.
- Use micro-headings within subsections via short <p><strong>Label:</strong> ...</p>
- Use bullets for lists (no long blocks).
- Insert natural spacing by splitting ideas into separate <p> blocks.
- Avoid “wall of text”.

SCORES MUST USE PERCENT FORMAT:
- Any score out of 100 must be written like “72%” (not “72/100”, not “72”).
- In Best-Fit Roles, provide 3–5 roles/pathways with:
  Technical: XX% | Experience: XX% | Communication: XX% | Overall: XX%

EVIDENCE NOTES (light-touch, non-academic):
Only when referencing salary ranges, hiring competitiveness, progression timelines, or skill ROI:
<p><strong>Evidence note:</strong> Based on reputable public sources such as ONS/HESA/Prospects/CIPD and aggregated job market ranges (Indeed/Reed/Glassdoor).</p>
No links. No academic citations. No “Sources & Methodology” section.

OUTPUT FORMAT (STRICT):
Return HTML ONLY.
Use only: <div>, <h2>, <h3>, <p>, <ul>, <li>, <strong>
No <html>, <head>, <body>, CSS, scripts.

PILL NAV (STRICT):
You MUST output exactly three top-level sections, each wrapped in <div class="section"> and titled with <h2>:

1) SECTION A — Candidate Overview
2) SECTION B — Candidate → Hired
3) SECTION C — Job Search Resources

Within each SECTION, use <h3> subsections with these exact titles and emojis:

SECTION A — Candidate Overview
- 👤 Candidate Snapshot
- 🧭 Career Direction
- 📊 Comparison to Others
- 🎯 Best-Fit Roles / Pathways
- 🧠 Skills & Strengths
- 🚧 Gaps Holding You Back

SECTION B — Candidate → Hired
- 💰 Salary & Money Outlook
- 📈 High-ROI Skills (Career + Life)
- 🗓️ 90-Day Plan
- ✅ Recommended Next Decisions
- 📌 Diagnostic Scores
- 🧾 Final Direction
- 🧾 Summary — Your Career Right Now

SECTION C — Job Search Resources
- 🧾 Professional Summary (CV & LinkedIn Ready)
- 🧾 Cover Letter Opening Paragraph
- ✅ Job Search Tips

SUBSECTION INTERNAL FLOW (LOCKED):
Inside EVERY <h3> subsection, use:
1) Detailed explanation (short paragraphs, insight-focused)
2) Breakdown bullets/scores/ranges
3) Evidence note (only when relevant)
4) TL;DR at the END:
<p><strong>TL;DR:</strong> ...</p>

UK FIRST:
Default to UK assumptions unless location clearly not UK.
Use realistic ranges, not best-case.
""".strip()


def build_user_prompt(cv_text: str) -> str:
    trimmed = (cv_text or "")[:9000]
    return f"""
Analyse the following CV text and produce the report in the exact structure required.

Important:
- Second-person voice only ("you").
- Do not repeat the CV back. Extract signals and give insights.
- Do not guess dates/grades/salaries. Never estimate past pay.
- Scores must be shown as percentages (e.g., 72%).
- Maintain strong readability: short paragraphs, clear labels, bullets.

CV TEXT:
\"\"\"
{trimmed}
\"\"\"
""".strip()


# ============================================================
# Upload parsing
# ============================================================
def extract_text_from_upload(file_storage) -> str:
    if not file_storage or file_storage.filename == "":
        return ""

    filename = file_storage.filename
    ext = Path(filename).suffix.lower()

    temp_path = UPLOAD_FOLDER / f"{secrets.token_hex(8)}_{Path(filename).name}"
    file_storage.save(temp_path)

    text = ""
    try:
        if ext == ".txt":
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext == ".docx":
            doc = Document(str(temp_path))
            text = "\n".join(p.text for p in doc.paragraphs if p.text)
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

    return (text or "").strip()


# ============================================================
# Structure validation + repair pass
# ============================================================
REQUIRED_H2 = [
    "SECTION A — Candidate Overview",
    "SECTION B — Candidate → Hired",
    "SECTION C — Job Search Resources",
]

REQUIRED_H3 = [
    "👤 Candidate Snapshot",
    "🧭 Career Direction",
    "📊 Comparison to Others",
    "🎯 Best-Fit Roles / Pathways",
    "🧠 Skills & Strengths",
    "🚧 Gaps Holding You Back",
    "💰 Salary & Money Outlook",
    "📈 High-ROI Skills (Career + Life)",
    "🗓️ 90-Day Plan",
    "✅ Recommended Next Decisions",
    "📌 Diagnostic Scores",
    "🧾 Final Direction",
    "🧾 Summary — Your Career Right Now",
    "🧾 Professional Summary (CV & LinkedIn Ready)",
    "🧾 Cover Letter Opening Paragraph",
    "✅ Job Search Tips",
]


def report_has_required_structure(html: str) -> bool:
    if not html:
        return False

    upper = html.upper()
    for h2 in REQUIRED_H2:
        if h2.upper() not in upper:
            return False

    for h3 in REQUIRED_H3:
        if h3 not in html:
            return False

    # enforce TL;DR marker and percent sign usage somewhere
    if "<strong>TL;DR:</strong>" not in html:
        return False
    if "%" not in html:
        return False

    return True


def repair_report_html(cv_text: str, bad_html: str) -> str:
    """
    Second pass: rewrite into the exact required structure without adding facts.
    Also fixes third-person voice and % formatting if needed.
    """
    prompt = f"""
Your previous output did not meet the required format.

Fix it:
- Second-person voice only.
- Exact SECTION A/B/C + exact <h3> headings.
- Scores must be percentages (e.g., 72%).
- Remove any guessed dates/grades/salaries or invented details.
- Improve readability: short <p> blocks, labels, bullets.
- Do NOT rehash the CV; focus on insights.

CV TEXT:
\"\"\"
{(cv_text or "")[:9000]}
\"\"\"

DRAFT OUTPUT:
\"\"\"
{(bad_html or "")[:9000]}
\"\"\"

Now output corrected HTML only.
""".strip()

    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=3800,
    )
    return (response.choices[0].message.content or "").strip()


def generate_report_html(cv_text: str) -> str:
    if not cv_text or not cv_text.strip():
        return "<div class='section'><h2>Error</h2><p>No CV text provided.</p></div>"

    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(cv_text)},
            ],
            temperature=0.25,
            max_tokens=3800,
        )

        html = (response.choices[0].message.content or "").strip()

        if not report_has_required_structure(html):
            app.logger.warning("Report structure invalid — running repair pass.")
            html2 = repair_report_html(cv_text, html)
            if report_has_required_structure(html2):
                return html2
            return html2 or html

        return html

    except Exception as e:
        app.logger.error(f"OpenAI API error: {e}")
        return """
        <div class='section'>
          <h2>Temporary issue generating your report</h2>
          <p>We ran into a problem while generating your CareerCompass report. Please try again.</p>
        </div>
        """


# ============================================================
# Referral + email list + sheets + resend (kept as your existing)
# ============================================================
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


def send_report_email(recipient_email: str, html_report: str, referral_code: str, feedback_form_url: str) -> None:
    recipient_email = (recipient_email or "").strip()
    if not recipient_email:
        app.logger.info("No recipient email provided – skipping email send.")
        return

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        app.logger.error("RESEND_API_KEY not set; cannot send email.")
        return

    from_email = os.environ.get("RESEND_FROM_EMAIL", "CareerCompass <report@career-compass.uk>")
    subject = "Your CareerCompass report + Lifetime Membership draw"
    feedback_form_url = (feedback_form_url or "").strip()
    share_url = "https://career-compass.uk"

    html_parts = []
    html_parts.append("<div style='font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto;'>")
    html_parts.append(
        """
        <p style="font-size:14px; line-height:1.6;">
          Hi,<br><br>
          Thanks for trying the CareerCompass beta. 🙌<br>
          Your full CareerCompass report is below 👇
        </p>
        """
    )

    if feedback_form_url:
        html_parts.append(
            f"""
            <p style="font-size:14px; line-height:1.6;">
              🎁 30s feedback = chance to win Lifetime Membership —
              <a href="{feedback_form_url}" style="color:#0957D0;">open feedback form</a>.
            </p>
            <hr style="margin:18px 0; border:none; border-top:1px solid #dddddd;">
            """
        )

    html_parts.append(
        f"""
        <p style="font-size:14px; line-height:1.6;">
          Your referral code: <strong>{referral_code or "N/A"}</strong><br>
          Share CareerCompass: <a href="{share_url}" style="color:#0957D0;">{share_url}</a>
        </p>
        <hr style="margin:18px 0; border:none; border-top:1px solid #dddddd;">
        <h2 style="font-size:18px; margin-bottom:12px;">Your full CareerCompass report</h2>
        """
    )

    html_parts.append(html_report)
    html_parts.append("</div>")

    data = {
        "from": from_email,
        "to": [recipient_email],
        "subject": subject,
        "html": "".join(html_parts),
        "text": "Your CareerCompass report is included in this email.",
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post("https://api.resend.com/emails", headers=headers, json=data, timeout=10)
        if 200 <= resp.status_code < 300:
            app.logger.info(f"Email sent to {recipient_email} via Resend.")
        else:
            app.logger.error(f"Resend API error: status={resp.status_code}, body={resp.text}")
    except requests.RequestException as e:
        app.logger.error(f"Network error sending email via Resend: {e}")


# ============================================================
# Routes
# ============================================================
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

    # best effort storage + email
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
