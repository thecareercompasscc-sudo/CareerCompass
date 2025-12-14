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

# ---------- OpenAI client ----------
client = OpenAI(timeout=30)  # uses OPENAI_API_KEY from env


# ============================================================
# ✅ NEW PROMPT (Matches the "final spec" from this chat)
# ============================================================
SYSTEM_PROMPT = """
You are CareerCompass — a career analyst that turns a user’s CV into a practical, trustworthy career report.

You are NOT a motivational speaker.
You must be realistic, specific, and useful.

CRITICAL TRUST RULES (NO HALLUCINATION):
- Do NOT invent employers, job titles, degrees, grades, dates, companies, or achievements.
- If a date is unclear, do NOT guess it. Use “recent” or omit dates entirely.
- If a detail is missing, say so briefly and continue with grounded assumptions.

STAGE AWARENESS (choose one):
A) Pre-16 (GCSE)
B) Post-16 (A-levels/college/apprenticeship decisions)
C) University student
D) Graduate / early-career (0–3 years)
E) Mid-career (3–10 years)
F) Career changer / returner
G) Unknown

You must state the detected stage in Section 1.

EVIDENCE NOTES (light-touch, non-academic):
Only include “Evidence note” lines where you mention:
- salary ranges
- competitiveness/hiring reality
- progression timelines
- skill ROI
Format exactly like:
<p><strong>Evidence note:</strong> ...</p>
No links. No academic citations. No “Sources & Methodology” section.

READABILITY RULE (locked):
Inside EVERY subsection:
1) Detailed explanation (short paragraphs)
2) Breakdown / bullets / scores / ranges
3) TL;DR at the END:
<p><strong>TL;DR:</strong> ...</p>

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
- <h3>👤 Candidate Snapshot</h3>
- <h3>🧭 Career Direction</h3>
- <h3>📊 Comparison to Others</h3>
- <h3>🎯 Best-Fit Roles / Pathways</h3>
- <h3>🧠 Skills & Strengths</h3>
- <h3>🚧 Gaps Holding You Back</h3>

SECTION B — Candidate → Hired
- <h3>💰 Salary & Money Outlook</h3>
- <h3>📈 High-ROI Skills (Career + Life)</h3>
- <h3>🗓️ 90-Day Plan</h3>
- <h3>✅ Recommended Next Decisions</h3>
- <h3>📌 Diagnostic Scores</h3>
- <h3>🧾 Final Direction</h3>
- <h3>🧾 Summary — Your Career Right Now</h3>

SECTION C — Job Search Resources
- <h3>🧾 Professional Summary (CV & LinkedIn Ready)</h3>
- <h3>🧾 Cover Letter Opening Paragraph</h3>
- <h3>✅ Job Search Tips</h3>

SCORING:
In “Best-Fit Roles / Pathways”, include 3–5 roles/pathways with simple competitiveness scores:
- Technical (0–100)
- Experience (0–100)
- Communication (0–100)
- Overall (0–100)

In “Diagnostic Scores”, score 0–100:
- Clarity
- Skills readiness
- Evidence/credibility
- Market fit
- Path readiness
Then 1 sentence explaining biggest lever.

UK FIRST:
Default to UK salary ranges unless location clearly not UK.
Use realistic ranges, not best-case.

Remember: this should feel more valuable than “ask ChatGPT”, because it is structured, stage-aware, quantified, and decision-focused.
""".strip()


def build_user_prompt(cv_text: str) -> str:
    trimmed = (cv_text or "")[:8500]
    return f"""
You will be given CV text. Extract what is there, do not guess dates or fabricate details.

CV TEXT:
\"\"\"
{trimmed}
\"\"\"

Now produce the report in the EXACT HTML structure required in the system prompt.

Important:
- Use the exact SECTION A/B/C headings.
- Use the exact <h3> subsection headings (with emojis).
- In every subsection: detail first, then bullets/scores/ranges, then TL;DR last.
- Include Evidence note lines only when you mention salary, demand, progression timelines, or ROI.
- If dates/grades are unclear: do NOT guess them.
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
# ✅ Structure validation + repair pass
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

    # ensure at least some TL;DR markers
    if "<strong>TL;DR:</strong>" not in html:
        return False

    return True


def repair_report_html(cv_text: str, bad_html: str) -> str:
    """
    Second pass: strictly reformat into required structure, without adding new facts.
    """
    prompt = f"""
You produced HTML but the structure was wrong.

Task:
- Rewrite into the EXACT required structure and headings.
- Do NOT add new facts. Only use what is already in the CV text and the provided draft.
- Remove any guessed dates/grades/claims that are not clearly supported.

CV TEXT:
\"\"\"
{(cv_text or "")[:8500]}
\"\"\"

DRAFT OUTPUT (may be wrong):
\"\"\"
{(bad_html or "")[:8500]}
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
        max_tokens=3600,
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
            max_tokens=3600,
        )

        html = (response.choices[0].message.content or "").strip()

        # ✅ If formatting drifted, repair it automatically
        if not report_has_required_structure(html):
            app.logger.warning("Report structure invalid — running repair pass.")
            html2 = repair_report_html(cv_text, html)
            if report_has_required_structure(html2):
                return html2
            # fallback: return repaired anyway, better than nothing
            return html2 or html

        return html

    except Exception as e:
        app.logger.error(f"OpenAI API error: {e}")
        return """
        <div class='section'>
          <h2>Temporary issue generating your report</h2>
          <p>We ran into a problem while generating your CareerCompass report.</p>
          <p>Please try again in a moment.</p>
        </div>
        """


# ============================================================
# Referral + email list + sheets + resend (unchanged)
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

    text_body = (
        "Hi,\n\n"
        "Thanks for trying CareerCompass.\n\n"
        "Your report is included below.\n\n"
        "Best,\nCareerCompass"
    )

    html_parts = []
    html_parts.append("<div style='font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto;'>")

    html_parts.append(
        """
        <p style="font-size:14px; line-height:1.6;">
          Hi,<br><br>
          Thanks for trying the CareerCompass beta. 🙌<br>
          You’ll find your full CareerCompass report at the bottom of this email 👇
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
        "text": text_body,
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

    # best-effort persistence + email
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
