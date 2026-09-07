# Job Assistant — an MCP server for Gmail and Google Drive

A **Model Context Protocol** server, built in Python with [FastMCP](https://gofastmcp.com), that
gives an AI host read-only access to a personal Google Drive and Gmail account so it can do the
tedious half of a job search — while the decision to actually apply stays with a human.

**In this repository:** the server itself (`MCP_server_JobAssistant.py`), the harness permission
rule discussed below (`.claude/settings.json`), and a [sample digest](docs/sample-digest.md)
showing what the tool actually produces.

Credentials, personal preferences and real digests are excluded by `.gitignore` — the sample is
a redacted stand-in with invented companies, but the structure and the warning text are verbatim.

Registering it with a host, with the Drive folder passed as an environment variable rather than
hardcoded:

```bash
claude mcp add -s user Job_Assistant -e CV_FOLDER_ID=<drive folder id> \
  -- /path/to/venv/python /path/to/MCP_server_JobAssistant.py
```

---

## The problem

Job hunting generates a lot of copy-paste: open the alert email, copy the vacancy into a chat,
paste in a CV, ask for a tailored letter, repeat. The work is mechanical, but the judgement at
the end — *is this role actually worth applying to?* — is not.

So the goal was to automate everything up to that judgement, and nothing past it:

1. Read CVs from Google Drive.
2. Read LinkedIn job-alert emails from Gmail.
3. Match a CV to each vacancy and draft a tailored letter.
4. Write a digest for a human to review.
5. **Stop.** The human reads the real posting and sends the application themselves.

## Architecture

Standard MCP host/client/server separation:

- **Host** — an AI application (this runs on Claude Code). The reasoning happens here.
- **Client** — created by the host when it connects.
- **Server** — this project. It exposes tools; it does not make decisions.

Google authentication sits outside the MCP protocol entirely: a separate OAuth 2.0 module the
server imports. Scopes are read-only by design — `drive.readonly` and `gmail.readonly`. The
server never needs write access to a mailbox, so it never asks for it.

### Matching happens in the host, not in a tool

CV-matching and letter-drafting are deliberately **not** Python functions in this server.

In MCP's model the host is the reasoning engine and tools are its hands and eyes. A hand-rolled
keyword-matching algorithm would be a cruder judgement than letting the model read the CV text
and the vacancy and decide for itself. So the tools return data, and the reasoning stays in the
host where it belongs.

That split is the whole point of the protocol, and it also means the server is host-agnostic:
the same code would work unchanged behind any MCP-compatible application.

---

## The interesting part: guardrails the model can't opt out of

Three times during development, a constraint that existed only as an instruction turned out not
to hold. Each time the fix was the same in shape — move the constraint somewhere the model
doesn't control — and each time it had to move somewhere different.

### 1. A disclosure the model kept dropping

Because vacancy data is limited to title, company and location (see *Limitations*), every match
is a first pass, not a verified fit. The digest therefore has to say so.

Across four test runs, **the host omitted that warning three times.** Prompting harder would
have been guesswork — unverifiable, and silently degrading the moment the phrasing or the model
changed.

Instead the warning became a constant in the server, and the tool that writes the digest injects
it into the file itself: once as a banner, once beneath every single vacancy, and again in the
value returned to the host. The tool's docstring instructs the host not to write its own version.

The model never composes that sentence, so it cannot shorten, soften or forget it. In the
[sample digest](docs/sample-digest.md) it appears four times across three vacancies — once as a
banner, once beneath each entry.

### 2. Vacancies quietly disappearing

On the first full run the host wrote up **6 of 10** vacancies, discarding three distinct
companies as *"lower-differentiation duplicates."* They weren't duplicates — real duplicates are
already removed by the retrieval tool — and nothing in the digest recorded that anything had
been dropped. The output looked complete.

Adding *"don't discard vacancies"* to the preferences file would have been the same category of
instruction that had already failed three times in four.

So the retrieval tool now records what it handed over, and the digest tool diffs the final digest
against it. Anything missing is named in the digest, under the banner. The host can still leave a
vacancy out — it just can't do it invisibly. The [sample digest](docs/sample-digest.md) shows
that block: four vacancies retrieved, three written up, and the fourth named.

Two details that make it trustworthy:

- The record is **consumed** — cleared once the digest tool has used it — so a later digest built
  from different tools isn't judged against a stale retrieval and falsely reported as dropping
  everything.
- It lives in memory, so a server restart between the two calls means the check doesn't run.
  That's the right failure direction: it can fail to warn, but it can never warn falsely.

### 3. A rule the server had no power to enforce

The project deliberately never fetches LinkedIn job pages — that's automated access against their
terms, and the account at risk is a personal one being used to job hunt.

On a later run, the host fetched one anyway, using its own web-fetch tool. 200 OK, 53KB.

The rule had been documented in the README and designed into the server, and neither constrains a
tool the server doesn't own. **This is a real limit of MCP**: a server exposes capabilities to a
host, it cannot restrict the host's other ones.

The fix had to live one level up, in the harness:

```json
{ "permissions": { "deny": ["WebFetch(domain:linkedin.com)"] } }
```

Plus an explicit instruction in the preferences file the host does read. Belt and braces, because
only one of those two is actually enforced.

### The pattern

> A constraint that depends on the model choosing to honour it is a hope, not a control.

Each guardrail moved from documentation into something deterministic — first into the server, then
into the server's own bookkeeping, then out into the harness. The last one is the most instructive,
because it's the case the server genuinely could not solve.

---

## Tools

| Tool | Purpose |
|---|---|
| `cv_list()` | Lists CVs in a Drive folder, queried by **folder ID rather than file IDs**, so new CVs are picked up with no code change. The folder id comes from a `CV_FOLDER_ID` environment variable, not the source, so nothing account-specific is committed. Excludes trashed files. |
| `get_cv_content(file_id)` | Downloads a CV and extracts its text. Checks the file isn't trashed first, so a deleted CV can't be served as if it still existed. |
| `list_job_alert_emails(max_results)` | Finds job-digest emails, filtering on sender **and** subject — the sender alone also matches generic marketing nudges. |
| `get_all_vacancies_from_email(message_id)` | Parses every vacancy out of one alert email's HTML. One email bundles many jobs, so the plain-text snippet Gmail provides isn't enough. |
| `get_recent_vacancies(days, max_emails)` | The everyday entry point: all vacancies from the last N days in **one** call, deduplicated. An early version needed ten round-trips for the same result. |
| `get_job_preferences()` | Reads target roles, seniority, locations and deal-breakers from a file. |
| `save_digest(entries)` | Writes the digest. Entries are validated by a Pydantic model, so a half-filled entry is rejected before it reaches the file. |
| `get_raw_email_html(message_id)` | Debugging: dumps a raw email, for when the provider changes their template. |

Two design habits visible throughout:

- **Shared internals, one place to fix.** Email fetching and HTML parsing are private helpers used
  by several tools, so a template change is a single-point fix.
- **Pipelines stay separable.** The one-call convenience tool didn't replace the two-step path;
  the steps remain individually callable, which is what made several bugs findable.

### Preferences as a file, not as code

Early results skewed heavily toward one domain — the host was inferring intent from whichever CVs
happened to be in the folder, since that was the only signal about what was wanted.

The fix was a preferences file: target roles, seniority, locations, languages, deal-breakers. It's
read fresh on every run, so behaviour can be retuned without touching Python or restarting
anything. Configuration that changes often shouldn't live in code.

---

## Limitations, on purpose

**Vacancy data is title, company, location and link — never the full description.** Getting the
description means fetching the job page, which is the scraping problem above. This is permanent,
not a gap to close later.

The consequence is honest rather than hidden: matches are a first pass, the digest says so on
every entry, and the human reads the real posting before deciding. That full description is read
once, by a person, in a browser — and never enters the system.

Also true, and worth stating plainly:

- CV extraction handles PDFs only.
- A PDF with a damaged text layer degrades matching silently — nothing detects it.
- Deduplication works within a single run, not across days.
- Nothing is persisted or written anywhere: read-only in, a local file out.

## Stack

Python · FastMCP · Pydantic · Google Drive & Gmail APIs · OAuth 2.0 · pypdf · BeautifulSoup

---

## What I'd take from this into production work

- Least-privilege scopes from the start, rather than as a hardening pass afterwards.
- Credentials never in version control, and paths resolved relative to the module rather than the
  working directory — a subprocess launched by a host doesn't run where you think it does.
- **Measure the model's behaviour instead of assuming it.** Every guardrail here exists because
  something was observed failing, not because it seemed like a good idea.
- Know where your enforcement boundary actually is. An MCP server cannot police the host it's
  connected to, and designing as though it can produces a control that isn't one.
