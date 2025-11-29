import os
import uuid
import csv
import re
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
    abort,
)
from flask_mail import Mail, Message

from openai import OpenAI
from docx import Document
from PyPDF2 import PdfReader
from fpdf import FPDF  # simple text-based PDF

# ---------- Paths & Flask setup ----------

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = "change-me-in-production"

mail = Mail()

# Folders for uploads and generated PDFs
UPLOAD_FOLDER = BASE_DIR / "uploads"
REPORTS_FOLDER = BASE_DIR / "reports"
EMAIL_LIST_FILE = BASE_DIR / "email_list.csv"  # simple CSV mailing list

UPLOAD_FOLDER.mkdir(exist_ok=True)
REPORTS_FOLDER.mkdir(exist_ok=True)

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["REPORTS_FOLDER"] = str(REPORTS_FOLDER)

# ---------- Email (Flask-Mail) config ----------

app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True

# Railway: you set these as environment variables
app.config["MAIL_USERNAME"] = os.environ.get(
    "MAIL_USERNAME", "the.career.compass.cc@gmail.com"
)
app.config["MAIL_PASSWORD"] = os.environ.get(
    "MAIL_PASSWORD", "YOUR_GMAIL_APP_PASSWORD_HERE"
)
app.config["MAIL_DEFAULT_SENDER"] = (
    "CareerCompass",
    os.environ.get("MAIL_DEFAULT_SENDER", "the.career.compass.cc@gmail.com"),
)

mail.init_app(app)

# ---------- OpenAI client ----------

client = OpenAI()  # uses OPENAI_API_KEY from env

# ---------- CareerCompass System Prompt (aligned with your idea card) ----------

SYSTEM_PROMPT = """
You are CareerCompass, an AI career analyst.

Your job is to analyse a candidate’s CV and generate a structured, realistic, and practical career report that:

- gives soon-to-be and recent graduates a clear view of the roles they are genuinely competitive for,
- opens their eyes to realistic alternatives to traditional graduate schemes,
- and offers step-by-step actions that they can follow in the next 0–6 months.

Assume the typical user is:
- a final-year student or fresh graduate (0–2 years out),
- often from a non-elite / non-target university,
- OR someone early in their career who wants to change direction without starting again from zero.

The report should:
- Reduce anxiety by showing that there are many good paths beyond “get a grad scheme at a big name”.
- Highlight overlooked industries, functions, and “bridge roles” that are easier to break into.
- Give them new hope, but always rooted in realistic chances and timelines.

Tone & Purpose:
- Clear, honest, supportive, non-patronising.
- Avoid hype, overpromising, or “you can do anything” clichés.
- Be specific about what is likely vs unlikely for their profile.
- Show them what *is* possible rather than dwelling on what isn’t.

Graduate Scheme Reality & USP:
- Assume most users either won’t get, or don’t need, traditional grad schemes.
- Explicitly normalise this: many great careers start outside formal programmes.
- Focus on:
  - realistic entry/next-step roles,
  - “bridge roles” that help them move into better positions later,
  - non-obvious paths (e.g. operations, customer success, internal support roles, niche industries, agencies, local firms, startups, not just big corporates).
- Where relevant, gently contrast “grad scheme path” vs “alternative path” and highlight benefits of the alternative (faster responsibility, broader exposure, less competition, etc.).

Evidence-Based Guidance:
Use labour-market patterns from publicly available, reputable sources such as:
- UK ONS salary distributions
- US BLS occupational data
- Glassdoor / Indeed / Salary.com aggregated salary ranges
- LinkedIn Talent Insights hiring patterns
- Typical graduate outcomes for similar degrees

When you reference salary or demand:
- Keep numbers approximate and clearly indicative, not precise statistics.
- Explicitly mention the type of source (e.g. “based on Glassdoor ranges for similar roles” or “ONS data for early-career roles”) instead of fake citations.
- Never claim to access live job ads or private datasets.

Insider / Networking-Style Insight (part of the USP):
In each relevant section, include brief “insider” guidance that the candidate would normally only hear from people already in the industry. For example:
- What hiring managers quietly prioritise beyond the job description.
- Common mistakes early-career candidates make that hurt their chances.
- Strong positive signals (projects, behaviours, portfolio pieces) that make people stand out.
- Smart questions to ask in informational interviews or networking chats.
- How someone without a perfect background can still get into the field via side doors or stepping-stone roles.

You MUST:
- Give realistic salary ranges based on level and (when possible) region.
- If location is unknown, provide ranges for 2–3 major regions (e.g. UK/EU/US).
- Maintain consistent structure and clear, digestible writing.
- Ground advice in what is typical for the role and level, not “dream” outcomes.
- Explicitly mention non-traditional, overlooked, or “hidden” routes where possible (e.g. smaller firms, agencies, regional employers, startups, internal operations/support teams).

Formatting Rules:
- Output ONLY HTML.
- Use: <div>, <h2>, <h3>, <p>, <ul>, <li>, <strong>.
- Wrap every main section inside <div class="section">.
- Include the three group headings exactly as written below.
- Do NOT include <html>, <head>, or <body>.

Required Structure:

SECTION A — Candidate Overview
1. Candidate Snapshot
2. Suitable Roles
3. Strengths
4. Skill Gaps & What to Learn

SECTION B — Candidate → Hired
5. Salary Expectations
6. Companies Hiring / Employer Types
7. 90-Day Action Plan

SECTION C — Job Search Resources
8. Professional Summary (CV & LinkedIn Ready)
9. Cover Letter Opening Paragraph
10. Job Search Tips
"""

# ---------- Helper: build user prompt ----------

def build_user_prompt(cv_text: str) -> str:
    return f"""
You are generating a CareerCompass report primarily for soon-to-be or recent graduates
and early-career professionals who may NOT have elite backgrounds or traditional grad schemes.

Analyse the following CV and produce a structured HTML career report.

Important rules:
- Assume the user wants realistic, high-quality options beyond just “apply to grad schemes”.
- Highlight non-obvious but realistic paths, bridge roles, and overlooked industries.
- Follow the exact structure and section titles from the system prompt.
- Use the three group headings (SECTION A/B/C).
- Output ONLY HTML.
- Make the writing realistic, concise, practical, and hopeful but not hypey.
- Base all analysis only on the CV text and reasonable inferences.

Here is the candidate’s CV:

{cv_text}
"""

# ---------- PDF generation (simple HTML → text) ----------

class SimplePDF(FPDF):
    """Very simple PDF generator for text-based reports."""
    pass


def create_pdf_from_html(html_content: str, pdf_path: Path) -> None:
    pdf = SimplePDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=11)

    text = html_content

    # Turn </li> into newlines, <li> into bullets
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li>", "• ", text, flags=re.IGNORECASE)

    # Replace <br> with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Write line by line
    for line in text.splitlines():
        line = line.strip()
        if not line:
            pdf.ln(4)
        else:
            pdf.multi_cell(0, 6, line)

    pdf.output(str(pdf_path))

# ---------- Helper: extract text from uploaded file ----------

def extract_text_from_upload(file_storage) -> str:
    """
    Extract text from supported uploads:
    - .txt  : read as text
    - .docx : python-docx
    - .pdf  : PyPDF2
    """
    if not file_storage or file_storage.filename == "":
        return ""

    filename = file_storage.filename
    ext = Path(filename).suffix.lower()

    temp_path = UPLOAD_FOLDER / filename
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

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(cv_text)},
        ],
        temperature=0.3,
        max_tokens=4000,
    )

    return response.choices[0].message.content

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

# ---------- Helper: sync a single email to Google Sheets ----------

def sync_email_to_sheet(email: str) -> None:
    """
    Sends ONE email record to your Google Sheet with no duplicates.
    Uses the service account in cc_service.json if present.
    """
    email = (email or "").strip().lower()
    if not email:
        return

    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        app.logger.warning("gspread/oauth2client not installed; skipping sheet sync.")
        return

    key_path = BASE_DIR / "cc_service.json"
    if not key_path.exists():
        app.logger.info("cc_service.json not found; skipping Google Sheet sync.")
        return

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        str(key_path), scope
    )
    client_gs = gspread.authorize(creds)

    SHEET_NAME = "CareerCompass Emails"
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

# ---------- Helper: send report PDF to user ----------

def send_report_email(recipient_email: str, pdf_path: Path) -> None:
    recipient_email = (recipient_email or "").strip()
    if not recipient_email:
        return

    if not pdf_path.exists():
        app.logger.error(f"PDF path does not exist: {pdf_path}")
        return

    subject = "Your CareerCompass Report"
    body_text = (
        "Hi,\n\n"
        "Thanks for using CareerCompass.\n\n"
        "Attached is your personalised career report as a PDF.\n\n"
        "If you have any feedback or want help interpreting it, just reply to this email.\n\n"
        "Best,\n"
        "The CareerCompass Team"
    )

    msg = Message(subject=subject, recipients=[recipient_email])
    msg.body = body_text

    with pdf_path.open("rb") as f:
        pdf_data = f.read()
    filename = pdf_path.name
    msg.attach(filename, "application/pdf", pdf_data)

    mail.send(msg)

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

    # 1) Get HTML report
    report_html = generate_report_html(combined_cv)

    # 2) Build HTML used for PDF (same content you show on-screen)
    full_html_for_pdf = render_template(
        "report_pdf.html",
        email=email,
        report_html=report_html,
    )

    # 3) PDF path
    report_id = str(uuid.uuid4())
    pdf_path = REPORTS_FOLDER / f"{report_id}.pdf"

    # 4) Generate PDF
    try:
        create_pdf_from_html(full_html_for_pdf, pdf_path)
    except Exception as e:
        app.logger.error(f"Error generating PDF: {e}")

    # 5) Save email + sync + send
    try:
        save_email_to_list(email)
    except Exception as e:
        app.logger.error(f"Failed to save email to list: {e}")

    try:
        sync_email_to_sheet(email)
    except Exception as e:
        app.logger.error(f"Failed to sync email to Google Sheet: {e}")

    try:
        send_report_email(email, pdf_path)
    except Exception as e:
        app.logger.error(f"Failed to send report email: {e}")

    # 6) Download link + render report page
    download_url = url_for("download_report", report_id=report_id)

    return render_template(
        "report.html",
        email=email,
        report_html=report_html,
        download_url=download_url,
    )

@app.route("/download/<report_id>", methods=["GET"])
def download_report(report_id):
    pdf_filename = f"{report_id}.pdf"
    pdf_path = REPORTS_FOLDER / pdf_filename

    if not pdf_path.exists():
        abort(404)

    return send_from_directory(
        app.config["REPORTS_FOLDER"],
        pdf_filename,
        as_attachment=True,
        download_name="CareerCompass_Report.pdf",
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # Railway will set this
    app.run(host="0.0.0.0", port=port)
