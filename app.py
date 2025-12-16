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

# Folders for uploads and simple email list
UPLOAD_FOLDER = BASE_DIR / "uploads"
EMAIL_LIST_FILE = BASE_DIR / "email_list.csv"

UPLOAD_FOLDER.mkdir(exist_ok=True)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

# ---------- OpenAI client ----------
client = OpenAI(timeout=30)  # uses OPENAI_API_KEY from env


# ============================================================
# ✅ CareerCompass Prompt (Final: 13-section report)
# ============================================================
SYSTEM_PROMPT = """
You are CareerCompass, an AI career analyst.

Write directly TO the user in second-person (“you”, “your”).
Do NOT write about them in third-person.

DO NOT REHASH THEIR CV:
- Do not list their education/work history back to them like a CV rewrite.
- Briefly acknowledge key signals, then give insights: what it means, what it unlocks, what’s missing, what to do next.

NO HALLUCINATIONS / TRUST RULES:
- Do NOT invent employers, job titles, degrees, grades, dates, companies, or achievements.
- If a date/grade is unclear, do NOT guess. Use “recent” or omit.
- NEVER estimate what they were paid in previous jobs (irrelevant and often wrong).
- Salary ranges are only for future target roles and progression.

STAGE AWARENESS (choose one, state it in Candidate Snapshot):
Pre-16 (GCSE) / Post-16 / University student / Graduate-early career / Mid-career / Career changer / Unknown.

OUTPUT FORMAT (STRICT):
Return HTML ONLY.
Use only: <div>, <h2>, <h3>, <p>, <ul>, <li>, <strong>
Do NOT include <html>, <head>, <body>, CSS, or scripts.

STRUCTURE (LOCKED):
Output exactly these sections IN THIS ORDER.
Each section must be wrapped as:
<div class="section"><h2>...</h2>...</div>

1. <h2>👤 Candidate Snapshot</h2>
2. <h2>🧭 Career Direction</h2>
3. <h2>📊 Comparison to Others</h2>
4. <h2>🎯 Best-Fit Roles / Pathways</h2>
5. <h2>🧠 Skills & Strengths</h2>
6. <h2>🚧 Gaps Holding You Back</h2>
7. <h2>💰 Salary & Money Outlook</h2>
8. <h2>📈 High-ROI Skills (Career + Life)</h2>
9. <h2>🗓️ 90-Day Plan</h2>
10. <h2>✅ Recommended Next Decisions</h2>
11. <h2>📌 Diagnostic Scores</h2>
12. <h2>🧾 Final Direction</h2>
13. <h2>🧾 Summary — Your Career Right Now</h2>

SECTION INTERNAL FLOW (VERY IMPORTANT):
Inside every section:
1) Detailed explanation (short paragraphs)
2) Breakdown bullets/scores/ranges
3) Evidence note ONLY where relevant (salary, demand, progression, ROI):
<p><strong>Evidence note:</strong> Based on reputable public sources such as ONS/HESA/Prospects/CIPD and aggregated job market ranges (Indeed/Reed/Glassdoor).</p>
4) TL;DR at the end:
<p><strong>TL;DR:</strong> ...</p>

SCORING RULES:
- Any score must be shown as a percentage like “72%” (not “72/100”, not “72”).
- In Best-Fit Roles / Pathways, provide 3–5 specific job titles and:
  Technical: XX% | Experience: XX% | Communication: XX% | Overall: XX%

READABILITY RULES:
- 1–3 sentences per paragraph.
- Prefer multiple short <p> blocks over long text.
- Use bullets heavily.
- Use <p><strong>Label:</strong> ...</p> lines to create whitespace and scanning cues.

UK FIRST unless location clearly indicates otherwise.
Be realistic, practical, and school-safe.
""".strip()


def build_user_prompt(cv_text: str) -> str:
    trimmed = (cv_text or "")[:9000]
    return f"""
Generate the CareerCompass report using the exact HTML structure and order described in the system prompt.

Important:
- Second-person voice only (“you”).
- Do not rehash the CV; focus on insights and actions.
- Do not guess dates/grades or estimate past pay.
- Scores must be percentages.

CV TEXT:
\"\"\"
{trimmed}
\"\"\"
""".strip()


# ============================================================
# Extract text from uploaded file
# ============================================================
def extract_text_from_upload(file_storage) -> str:
    if not file_storage or file_storage.filename == "":
        return ""

    filename = file_storage.filename
    ext = Path(filename).suffix.lower()

    # Avoid collisions in uploads
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
# Report validation + repair
# ============================================================
REQUIRED_H2 = [
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
]


def report_has_required_structure(html: str) -> bool:
    if not html:
        return False

    # Must contain all required h2 headings
    for h2 in REQUIRED_H2:
        if h2 not in html:
            return False

    # Must contain TL;DR marker
    if "<strong>TL;DR:</strong>" not in html:
        return False

    # Must use percentages somewhere (scores)
    if "%" not in html:
        return False

    # Must have section wrappers
    if "class=\"section\"" not in html and "class='section'" not in html:
        return False

    return True


def repair_report_html(cv_text: str, bad_html: str) -> str:
    """
    Repair pass: rewrite output into the exact 13-section structure
    without adding new facts. Removes guessed dates/claims.
    """
    prompt = f"""
Your previous output did not meet the required format.

Fix it:
- Output exactly 13 sections in the correct order, each wrapped in <div class="section">.
- Use the exact <h2> headings as specified.
- Second-person voice only ("you").
- Remove any guessed dates/grades or invented details.
- Never estimate past pay.
- Ensure scores are formatted as percentages like "72%".
- Ensure every section ends with TL;DR.
- Improve spacing: short paragraphs, labels, bullets.
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
        max_tokens=4200,
    )
    return (response.choices[0].message.content or "").strip()


def generate_report_html(cv_text: str) -> str:
    if not cv_text or not cv_text.strip():
        return "<div class='section'><h2>Error</h2><p>No CV text provided.</p></div>"

    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        app.logger.info("Calling OpenAI for report generation...")

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(cv_text)},
            ],
            temperature=0.25,
            max_tokens=4200,
        )

        html = (response.choices[0].message.content or "").strip()

        # If the model drifts, repair automatically
        if not report_has_required_structure(html):
            app.logger.warning("Report structure invalid — running repair pass.")
            fixed = repair_report_html(cv_text, html)
            return fixed or html

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
# Referral code
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


# ============================================================
# Save email locally
# ============================================================
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


# ============================================================
# Sync email to Google Sheets (primary)
# ============================================================
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
    for row in sheet.get_all_records():
        existing_email = (row.get("email") or "").strip().lower()
        if existing_email:
            existing.add(existing_email)

    if email not in existing:
        sheet.append_row([email, datetime.utcnow().isoformat()])
        app.logger.info(f"Added {email} to primary Google Sheet.")
    else:
        app.logger.info(f"Email {email} already in primary Google Sheet; skipping.")


# ============================================================
# Sync email to feedback sheet
# ============================================================
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
    for row in sheet.get_all_records():
        existing_email = (row.get("email") or "").strip().lower()
        if existing_email:
            existing.add(existing_email)

    if email not in existing:
        sheet.append_row([email, datetime.utcnow().isoformat()])
        app.logger.info(f"Added {email} to feedback Google Sheet.")
    else:
        app.logger.info(f"Email {email} already in feedback Google Sheet; skipping.")


# ============================================================
# Send report email via Resend
# ============================================================
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
                f"Resend API error for {recipient_email}: status={resp.status_code}, body={resp.text}"
            )
    except requests.RequestException as e:
        app.logger.error(f"Network error sending email via Resend to {recipient_email}: {e}")


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

    # best-effort email capture + sync
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

    # send email best effort
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
