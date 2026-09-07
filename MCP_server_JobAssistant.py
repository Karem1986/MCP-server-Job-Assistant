# MCP server for job automation with Gmail and Google Drive
from fastmcp import FastMCP
import io
import os
from googleapiclient.http import MediaIoBaseDownload
import base64
from datetime import date
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

# Import the get_credentials function from google we have created
from Google_auth_test import get_credentials
from googleapiclient.discovery import build
from pypdf import PdfReader

mcp = FastMCP("Job Assistant MCP server")

# Which Drive folder holds the CVs. Set per-user in the MCP registration
# (claude mcp add ... -e CV_FOLDER_ID=...), so no account-specific id lives in the code.
CV_FOLDER_ID = os.environ.get("CV_FOLDER_ID", "")

PREFERENCES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "job_preferences.md"
)

DIGEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digests")

# Written by this server, never by the host, so it cannot be paraphrased away or
# forgotten. See "Design decision: matching and drafting happen in the host".
MATCH_WARNING_MESSAGE = (
    "WARNING: Matched on job title, company and location only - the full job description was "
    "never read. Open the link and read the real posting before sending anything."
)

# Tools

@mcp.tool
def get_job_preferences() -> str:
    """Read the user's job-search preferences: target roles, locations, deal-breakers."""
    if not os.path.exists(PREFERENCES_PATH):
        return "No preferences file found. Create job_preferences.md in the server folder."
    with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
        return f.read()

@mcp.tool
def cv_list() -> list[dict]:
    """List CV files found in the JobSearch--> CVs folder of the user's Google Drive"""
    if not CV_FOLDER_ID:
        raise RuntimeError(
            "CV_FOLDER_ID is not set. Re-register the server with the Drive folder id, e.g. "
            "claude mcp add -s user Job_Assistant -e CV_FOLDER_ID=<folder id> -- <python> <script>"
        )

    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)

    results = drive_service.files().list(
        q=f"'{CV_FOLDER_ID}' in parents and trashed = false",
        fields="files(id, name, mimeType)",
    ).execute()

    return results.get("files", [])

@mcp.tool
def get_cv_content(file_id: str) -> str:
    """Download a CV PDF from Drive and extract its text content."""
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)

    # If the CV has been deleted, then do not download it
    metadata = drive_service.files().get(fileId=file_id, fields="trashed").execute()
    if metadata.get("trashed"):
        return "This file has been deleted (moved to trash) and is no longer available."

    request = drive_service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    reader = PdfReader(file_buffer)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return text


# it serves both tools to read email jobs and to get all jobs from a LinkedIn email alert, for html related bugs or gmail changes the text structure fixex wil be executed here
def _get_email_html(gmail_service, message_id: str) -> str:
    """Fetch a Gmail message and return its decoded HTML body (empty string if none)."""
    msg = gmail_service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    def find_html_part(part):
        if part.get("mimeType") == "text/html":
            return part["body"].get("data")
        for subpart in part.get("parts", []):
            result = find_html_part(subpart)
            if result:
                return result
        return None

    html_data = find_html_part(msg.get("payload", {}))
    if not html_data:
        return ""

    html_bytes = base64.urlsafe_b64decode(html_data)
    return html_bytes.decode("utf-8", errors="ignore")

@mcp.tool
def get_raw_email_html(message_id: str) -> str:
    """TEMPORARY: dump the raw HTML of an email so we can inspect its structure."""
    creds = get_credentials()
    gmail_service = build("gmail", "v1", credentials=creds)
    html = _get_email_html(gmail_service, message_id)
    return html or "(no HTML part found)"

@mcp.tool
def list_job_alert_emails(max_results: int = 10) -> list[dict]:
    """List LinkedIn job-digest emails only (excludes generic LinkedIn nudges)."""
    creds = get_credentials()
    gmail_service = build("gmail", "v1", credentials=creds)

    results = gmail_service.users().messages().list(
        userId="me",
        q='from:jobs-noreply@linkedin.com subject:"Nieuwe vacatures"',
        maxResults=max_results,
    ).execute()
    message_refs = results.get("messages", [])

    alerts = []
    for ref in message_refs:
        msg = gmail_service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["Subject", "Date"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
        date = next((h["value"] for h in headers if h["name"] == "Date"), "")
        alerts.append({"id": ref["id"], "subject": subject, "date": date})

    return alerts

# What get_recent_vacancies last handed to the host. save_digest compares the digest
# against this, so vacancies the host quietly drops show up in the file instead of
# vanishing. Module-level, so it empties when the server process restarts.
_last_retrieval: list[dict] = []


def _vacancy_key(title: str, company_location: str) -> tuple[str, str]:
    """Identity of one vacancy, used both for dedup and for the digest reconciliation."""
    return (title.strip().lower(), company_location.strip().lower())


# Same reasoning as _get_email_html: LinkedIn template changes get fixed in one place
def _parse_vacancies(html: str) -> list[dict]:
    """Parse the job cards out of one LinkedIn alert email's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    job_cards = soup.find_all("td", attrs={"data-test-id": "job-card"})

    jobs = []
    for card in job_cards:
        title_tag = card.find("a", class_="text-md")
        if not title_tag:
            continue

        company_location_tag = card.find("p", class_="text-system-gray-100")

        jobs.append({
            "title": title_tag.get_text(strip=True),
            "company_location": company_location_tag.get_text(strip=True) if company_location_tag else "",
            "url": title_tag.get("href", ""),
        })

    return jobs

@mcp.tool
def get_all_vacancies_from_email(message_id: str) -> list[dict]:
    """Extract individual vacancies from a LinkedIn alert email wit its message_id"""
    creds = get_credentials()
    gmail_service = build("gmail", "v1", credentials=creds)

    html = _get_email_html(gmail_service, message_id)
    if not html:
        return []

    return _parse_vacancies(html)

@mcp.tool
def get_recent_vacancies(days: int = 1, max_emails: int = 10) -> list[dict]:
    """All vacancies from the last N days of LinkedIn job alerts, deduplicated.

    One call instead of list_job_alert_emails + a get_all_vacancies_from_email per
    email. LinkedIn repeats the same vacancy across consecutive alerts, so duplicates
    are dropped on title + company/location.
    """
    creds = get_credentials()
    gmail_service = build("gmail", "v1", credentials=creds)

    results = gmail_service.users().messages().list(
        userId="me",
        q=f'from:jobs-noreply@linkedin.com subject:"Nieuwe vacatures" newer_than:{days}d',
        maxResults=max_emails,
    ).execute()

    jobs = []
    seen = set()
    for ref in results.get("messages", []):
        html = _get_email_html(gmail_service, ref["id"])
        if not html:
            continue
        for job in _parse_vacancies(html):
            key = _vacancy_key(job["title"], job["company_location"])
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)

    global _last_retrieval
    _last_retrieval = [
        {"title": j["title"], "company_location": j["company_location"]} for j in jobs
    ]

    return jobs


class DigestEntry(BaseModel):
    """One reviewed vacancy: the job, the CV picked for it, and the draft letter."""
    title: str
    company_location: str
    url: str
    matched_cv: str = Field(description="File name of the CV from cv_list() that fits best")
    why_this_cv: str = Field(description="One or two sentences on why this CV over the others")
    draft_letter: str = Field(description="Full draft motivation letter, ready to edit")

def _dropped_vacancies(entries: list["DigestEntry"]) -> list[dict]:
    """Vacancies handed to the host by get_recent_vacancies that never reached the digest."""
    if not _last_retrieval:
        return []

    written = {_vacancy_key(e.title, e.company_location) for e in entries}
    return [
        v for v in _last_retrieval
        if _vacancy_key(v["title"], v["company_location"]) not in written
    ]

@mcp.tool
def save_digest(entries: list[DigestEntry]) -> str:
    """Write today's job digest to a markdown file for human review.

    Call this after matching CVs and drafting letters. Do not write the verification
    warning yourself - this tool adds it to the digest. Any vacancy from
    get_recent_vacancies that you leave out is recorded in the digest as dropped.
    """
    global _last_retrieval

    if not entries:
        return "Nothing to write: no entries were passed."

    dropped = _dropped_vacancies(entries)
    retrieved_count = len(_last_retrieval)

    os.makedirs(DIGEST_DIR, exist_ok=True)
    today = date.today().isoformat()
    path = os.path.join(DIGEST_DIR, f"digest_{today}.md")

    lines = [
        f"# Job digest - {today}",
        "",
        f"> **{MATCH_WARNING_MESSAGE}**",
        "",
        f"{len(entries)} {'vacancy' if len(entries) == 1 else 'vacancies'}. "
        "Nothing here has been sent. Every application is still yours to send by hand.",
        "",
    ]

    if dropped:
        lines += [
            f"> **{retrieved_count} vacancies were retrieved, {len(entries)} written up. "
            f"The assistant left out the {len(dropped)} below. Duplicates were already removed "
            "before it saw them, so these are distinct openings:**",
            ">",
        ]
        lines += [f"> - **{v['title']}** ({v['company_location']})" for v in dropped]
        lines.append("")

    for i, entry in enumerate(entries, start=1):
        lines += [
            "---",
            "",
            f"## {i}. {entry.title}",
            "",
            f"- **Company / location:** {entry.company_location}",
            f"- **Vacancy:** {entry.url}",
            f"- **Matched CV:** {entry.matched_cv}",
            f"- **Why this CV:** {entry.why_this_cv}",
            "",
            f"> {MATCH_WARNING_MESSAGE}",
            "",
            "### Draft letter",
            "",
            entry.draft_letter.strip(),
            "",
        ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Consumed: a later save_digest built from other tools must not be judged against
    # this retrieval, or it would report every vacancy as dropped.
    _last_retrieval = []

    dropped_note = (
        f"\n\n{len(dropped)} retrieved {'vacancy was' if len(dropped) == 1 else 'vacancies were'} "
        f"left out and recorded as dropped in the digest: "
        + "; ".join(f"{v['title']} ({v['company_location']})" for v in dropped)
        if dropped else ""
    )

    return (
        f"Digest with {len(entries)} {'vacancy' if len(entries) == 1 else 'vacancies'} written to {path}"
        f"{dropped_note}\n\n"
        f"Tell the user: {MATCH_WARNING_MESSAGE}"
    )

if __name__ == "__main__":
    mcp.run()
