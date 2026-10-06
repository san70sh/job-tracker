# Roadmap

v1 (current) is the tracker: add jobs, extract fields, board, watched boards, Notion mirror. This page lists what is deliberately *not* in v1.

## v1.x: finishing and hardening (small, independent)

| Item | Why | Notes |
|---|---|---|
| Run the Notion mirror against the real workspace | only tested with fakes | needs your integration token; start with `notion-import --dry-run`, then `notion-reconcile` |
| Run the LLM fallback against the live API | never exercised | needs `ANTHROPIC_API_KEY`; check the cached results in `llm_calls` |
| Seed `salary_benchmarks` from the Notion rows | blank salary on most Indian postings | those rows already hold researched ranges with source links |
| "Research salary" action | cheaper than manual research | one cited web search per company + level + city, saved as *unverified* until you confirm, cached about six months, allow-listed sites only |
| Re-extract command | snapshots are stored but never re-read | `jobtracker reparse` over `job_snapshots`; generic-HTML jobs would also need their HTML stored |
| Better generic HTML path | weakest source (LinkedIn, Indeed, custom sites) | per-site extractors for the few hosts you actually use; LinkedIn and Indeed return login walls or 401 to plain requests |
| Decide on `intake_queue` | unused | drop it, or use it for bulk paste / a browser extension |
| Update golden harness thresholds | currently informational | turn selected agreements into regression checks |
| Optional Docker Compose | one-command start | `postgres:18` + app |
| Schema migrations | `db.ps1 apply` skips existing tables, so schema changes need a manual migration | adopt a small migration tool or numbered SQL files |
| Authentication | needed before any non-localhost use | reverse proxy with basic auth is enough |

## v2: resume, skills and job comparison

Goal: import your resume once, extract your skills, then compare it against any tracked job and see what matches and what is missing. **Scoring is a second step within v2** and is specified separately below.

### 2.1 Resume import
- **Input:** PDF, DOCX or plain text, uploaded in the UI or `resume-import FILE`.
- **Text extraction** is deterministic (a PDF/DOCX text library); the original file and the extracted text are stored locally. Nothing leaves the machine unless you explicitly run the optional LLM step.
- **Versions:** keep several resumes (for example "backend", "platform") and mark one as the default, because the comparison is only useful against the version you would send.

### 2.2 Skill and keyword extraction
Reuse the machinery already built for job postings, so both sides speak the same vocabulary:
1. **Skills:** match resume text against `config/vocab.json` (the same 100 tags and aliases). Output per skill: found / not found, where it appears, and a short evidence snippet (the sentence).
2. **Experience per skill (heuristic):** attach the date range of the role or project the mention sits in, and sum the span to estimate years. Shown as an estimate you can overwrite.
3. **Other keywords:** terms that recur in your resume but are not in the vocabulary (tools, domains, methodologies), surfaced as suggestions to add to the vocabulary rather than silently ignored.
4. **Sections:** summary, experience entries, education, certifications, via the same heading-driven splitter used for postings.
5. **Optional LLM pass** (explicit, off by default) to structure messy resumes into roles and dates. It would send the resume text to the API, which is why it is opt-in.

### 2.3 Comparison against a posted job
On the job page, a **Resume match** panel for the chosen resume version:
- **Technologies:** matched, missing required, missing nice-to-have, and extras you have that the posting does not ask for. Uses `job_technologies` (already required vs nice-to-have).
- **Keyword coverage:** important terms from the posting's requirements that never appear in your resume, the way an applicant tracker would check, with the exact phrase and where it is used in the posting.
- **Experience gap:** your estimated total and per-skill years against the posting's minimum (`experience_min_years` is already stored).
- **Evidence:** for every matched skill, the resume sentence that supports it, so you can reuse it when tailoring.
- A list view on the board to **sort or filter jobs by match** once scoring exists.

### 2.4 Scoring (second step of v2)
Design only, to be tuned on your data:
- Weighted components: required-skill coverage, nice-to-have coverage, experience fit, location and work-mode fit, salary versus your floor. Required skills weigh more than nice-to-have (the earlier draft used 1.0 and 0.4).
- Missing information is neutral, not a penalty (an unknown salary scores 0.5).
- The score is always shown with its breakdown, never as a bare number.
- The `Notes` text can be generated from the breakdown ("strong on Java/Kafka, missing OAuth and mTLS") instead of written by hand.
- **Calibration:** once enough applications have outcomes, compare scores against which jobs reached interviews and adjust the weights.

### 2.5 Data model sketch
The profile and fit tables removed in v1 come back, redesigned around resumes:

| Table | Holds |
|---|---|
| `resumes` | name, file name, extracted text, version, default flag, uploaded at |
| `resume_skills` | resume, technology, found, estimated years, evidence snippet |
| `resume_keywords` | resume, term, count, suggested for vocabulary |
| `job_resume_match` | job, resume, matched / missing / extra lists, coverage, computed at (cached; recomputed when either side changes) |
| `profile` | location and work-mode preferences, salary floor (inputs to scoring) |

### 2.6 Open questions for v2
- Which resume formats do you actually use (PDF export from Word or Docs)? Two-column PDFs extract badly and may need special handling.
- Should the comparison ever call an LLM, or stay purely rule-based?
- Should skills claimed in the resume but never evidenced with a project or role be flagged as weak?
- Do you want a "tailor my resume to this job" suggestion list (reorder bullets, add missing keywords) in v2 or later?
