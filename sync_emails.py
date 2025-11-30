import csv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Google Sheets auth
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    "cc_service.json", scope
)
client = gspread.authorize(creds)

# Open your sheet
SHEET_NAME = "EMAIL LISTS"
sheet = client.open(SHEET_NAME).CareerCompass

# Load existing sheet emails
existing = {row["email"].strip().lower() for row in sheet.get_all_records()}

# Read emails from CSV
new_rows = []
with open("email_list.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        email = row["email"].strip().lower()
        timestamp = row["timestamp_utc"]
        if email not in existing:
            new_rows.append([email, timestamp])
            existing.add(email)

# Upload only new ones
if new_rows:
    sheet.append_rows(new_rows)
    print(f"Uploaded {len(new_rows)} new emails.")
else:
    print("No new emails to upload.")
