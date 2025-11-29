import os
import csv
import json
import socket
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
app.secret_key = "change-me-in-production"

# Folders for uploads and simple email list
UPLOAD_FOLDER = BASE_DIR / "uploads"
EMAIL_LIST_FILE = BASE_DIR / "email_list.csv"

UPLOAD_FOLDER.mkdir(exist_ok=True)

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

# ---------- OpenAI client (with timeout) ----------

# Uses OPENAI_API_KEY from your environment.
client = OpenAI(timeout=30)

# ---------- CareerCompass System Prompt ----------

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

[... keep the rest of your long spec here ...]
"""

# ---------- Helper: build user prompt ----------

def build_user_prompt(cv_text: str) -> str:
    trimmed = (cv_text or "")[:6000]
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

{trimmed}
"""

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

    try:
        app.logger.info("Calling OpenAI for report generation...")
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(cv_text)},
            ],
            temperature=0.3,
            max_tokens=2500,
        )
        app.logger.info("OpenAI call succeeded.")
        return response.choices[0].message.content
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
    Uses GOOGLE_SERVICE_JSON env var.
    """
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
        app.logger.info(f"Added {email} to Google Sheet.")

# ---------- Helper: send report email via Resend API ----------

def send_report_email(recipient_email: str, html_report: str) -> None:
    """
    Send the generated report to the user via email using Resend API.

    - No PDFs or attachments.
    - Includes an AI Prompt Pack (static) at the top.
    - Appends the full HTML report underneath.
    """
    recipient_email = (recipient_email or "").strip()
    if not recipient_email:
        app.logger.info("No recipient email provided – skipping email send.")
        return

    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "careercompass@example.com")

    if not api_key:
        app.logger.error("RESEND_API_KEY not set; cannot send email.")
        return

    subject = "Your CareerCompass report + AI prompts to go deeper"

    # Plain-text fallback
    text_body = (
        "Hi,\n\n"
        "Thanks for using CareerCompass.\n\n"
        "Your personalised career report is included in this email as HTML.\n"
        "We’ve also added a set of AI prompts you can copy and paste into ChatGPT\n"
        "or any AI tool to get more personalised help from your report.\n\n"
        "Best,\nCareerCompass"
    )

    ai_prompts_html = """
    <div style="font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto;">
      <h1 style="font-size: 22px; margin-bottom: 8px;">Your CareerCompass report</h1>
      <p style="font-size: 14px; line-height: 1.5;">
        Below is your full CareerCompass report. To get even more value from it,
        you can copy your report into an AI tool (like ChatGPT) and use the
        prompts in this email to go deeper.
      </p>

      <h2 style="font-size: 18px; margin-top: 24px;">✨ Bonus: AI Prompt Pack</h2>
      <p style="font-size: 14px; line-height: 1.5;">
        Copy your report, open your favourite AI tool, and paste <strong>your report</strong>
        followed by one of the prompts below.
      </p>

      <div style="margin-top: 16px; font-size: 13px; line-height: 1.6;">
        <h3 style="font-size: 16px; margin-bottom: 4px;">1) Turn my report into a CV rewrite</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Here is my personalised career report from CareerCompass. Rewrite my CV using the strengths, skill gaps, and target roles listed. Make it ATS-friendly, action-driven, and aligned to the roles I’m best suited for.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">2) Weekly job search strategy</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Here is my personalised career report from CareerCompass. Create a realistic weekly job search schedule with daily tasks, tailored to my background and the target roles mentioned.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">3) Likely interview questions + model answers</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Using my CareerCompass report, list 10 likely interview questions for the roles suggested and provide strong, tailored sample answers based on my experience.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">4) Rewrite my LinkedIn profile</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Rewrite my LinkedIn headline and About section using the insights from this CareerCompass report. Make it concise, employer-focused, and clearly aligned with the roles you think I should target.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">5) 30-day learning plan for my skill gaps</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Here is my CareerCompass report. Based on the skill gaps identified, create a focused 30-day learning plan with weekly milestones and a few suggested resources or practice ideas.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">6) Tailor my CV to a job description</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Using the strengths and target roles in this CareerCompass report, help me tailor my CV to the following job description. Rewrite my bullet points and highlight what I should emphasise.

Job description:
[Paste job description here]
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">7) Portfolio or project ideas</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Based on this CareerCompass report, suggest 3–5 practical project or portfolio ideas I can complete in 2–6 weeks to make myself more competitive for the roles mentioned.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">8) Networking message ideas</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Using the details in this CareerCompass report, write 3 short networking messages I can send to people already working in the roles you recommend for me.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">9) Turn skill gaps into weekly actions</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Take the skill gaps listed in this CareerCompass report and break them down into practical weekly actions I can follow over the next 2–3 months.
        </pre>

        <h3 style="font-size: 16px; margin-bottom: 4px;">10) Define my professional positioning</h3>
        <pre style="white-space: pre-wrap; font-family: Menlo, Consolas, monospace; background: #f5f5f5; padding: 8px; border-radius: 4px;">
Using this CareerCompass report, summarise my professional positioning in 3–4 sentences: who I am, what I offer, and the types of problems I can solve for employers.
        </pre>
      </div>

      <hr style="margin: 32px 0; border: none; border-top: 1px solid #dddddd;">

      <h2 style="font-size: 18px; margin-bottom: 12px;">Your full CareerCompass report</h2>
    </div>
    """

    html_body = ai_prompts_html + html_report

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
        if resp.status_code >= 200 and resp.status_code < 300:
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
    combined_cv = "\n\n".join(
        part for part in [text_box, file_text] if part
    ).strip()

    if not combined_cv:
        flash("Please paste your CV or upload a valid file.", "error")
        return redirect(url_for("index"))

    # 1) Get HTML report
    report_html = generate_report_html(combined_cv)

    # 2) Save email + sync + send (best effort, never block user seeing report)
    try:
        save_email_to_list(email)
    except Exception as e:
        app.logger.error(f"Failed to save email to list: {e}")

    try:
        sync_email_to_sheet(email)
    except Exception as e:
        app.logger.error(f"Failed to sync email to Google Sheet: {e}")

    try:
        send_report_email(email, report_html)
    except Exception as e:
        app.logger.error(f"Failed to send report email: {e}")

    # 3) Render on-screen HTML report page
    return render_template(
        "report.html",
        email=email,
        report_html=report_html,
        download_url=None,
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # Railway overrides this
    app.run(host="0.0.0.0", port=port)
