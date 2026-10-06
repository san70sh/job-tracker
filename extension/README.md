# Job Tracker capture (Chrome extension)

Click the toolbar button on a job page. The extension reads the page you are looking at, sends it to your Job Tracker on this computer, and opens a small review window. Nothing goes anywhere else, and it never sees your logins: it reads what is already on screen.

## Install (once)
1. In the project folder, create the token and restart the app:
   ```powershell
   uv run python -m jobtracker capture-token
   ```
   The command adds `CAPTURE_TOKEN=...` to `.env` and prints the value. Restart `serve` so it picks it up.
2. In Chrome open `chrome://extensions`, switch on **Developer mode**, press **Load unpacked** and choose this `extension` folder. Pin the extension from the puzzle-piece menu.
3. Its options page opens the first time you click the button. Paste the token and press **Save**.

## Use
Open a job (LinkedIn, Indeed, a company career page), click the button. A review window opens with the fields filled in; fix anything highlighted and press **Save for later** or **Save as applied**. The window shows "Saved" and can be closed.

- **Select the description first** if a page reads badly: a selection of 200+ characters is used as the posting text.
- A red `!` on the button means it could not send. Hover for the reason (app not running, wrong token, nothing readable on the page).

## What it reads
In order of trust: the page's own job data (JSON-LD, which most career sites publish), a LinkedIn or Indeed reader, then the page heading and main text as a weak guess (those fields are highlighted). Chrome's own pages and the web store cannot be read.

## The job's link
1. If the page links to the company's own posting (LinkedIn's **Go to company site** under *Application status*, or its external **Apply** link), that address is the job link. LinkedIn wraps it in a redirect; the extension unwraps it.
2. Otherwise the job link is this job's LinkedIn address (`linkedin.com/jobs/view/<id>`), tidied of search and tracking parts.

The Job ID is always LinkedIn's own id, taken from the page address. You can change both in the review window.

## Reading the company's page instead of LinkedIn's text
When the page links to the company's own posting, the app fetches that page and reads the job from it (clean structured data for Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Oracle, Eightfold, Amazon, Jibe and iCIMS; JSON-LD or the page's text for other sites). The review window says where it read from, and **Use the LinkedIn text instead** switches. LinkedIn's card still fills what the company page lacks (work mode, location).

The link is used only if it is a careers position page: a recognised ATS posting, a page that declares itself a JobPosting, or a careers-style address with real job sections (responsibilities, requirements) on it. Anything else (a seller portal, a blog post, another job board) is ignored, the reason is shown, and the LinkedIn text is used. The job title is not compared: a company may name the role differently from LinkedIn.

## Limits
- LinkedIn and Indeed change their page markup now and then; if a field comes out empty, select the description and click again, and tell me which page so the reader can be updated (`reader.js`).
- The extension is allowed to talk to `http://127.0.0.1:8000` and `http://localhost:8000` only. Another port means editing `host_permissions` in `manifest.json`.
- After editing a file here, press the reload arrow on the extension's card in `chrome://extensions`.
