# SmartBIMS — Barangay Information Management System

A Flask web application for managing barangay service requests, resident accounts, announcements, and reports. Built with SQLite, featuring a chat assistant ("Moss") to help residents request documents and track their applications.

## Features

- **Resident portal** — Register, sign in, request documents (Barangay clearance, Certificate of residency, Certificate of indigency), and track request status
- **Staff portal (officials & admins)** — Review and update service requests, manage resident accounts, publish announcements, export data to CSV
- **Admin tools** — Activity log, user account management, reports and analytics
- **Moss assistant** — Chat-based help for document requests, status checks, announcements, and portal guidance
- **Optional AI backend** — Moss can be powered by any OpenAI-compatible model, with live database lookups and confirmation-gated actions
- **Docker-ready** — Includes `Dockerfile` for container deployment

## Tech Stack

- Python 3 + Flask
- SQLite (via `sqlite3`)
- HTML/CSS/Jinja2 templates
- Werkzeug (password hashing)
- OpenAI SDK (optional, for the AI assistant)

## Project Structure

```
bims_project/
├── app.py              # Main Flask application
├── moss_ai.py          # AI assistant backend (provider, tools, confirmation gate)
├── setup_bims.py       # Creates the admin account and initializes the DB
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container definition
├── .env.example        # Template for the environment variables
├── .gitignore          # Files excluded from git
├── instance/           # SQLite database (created at runtime, not committed)
├── static/             # CSS, JS, images
├── templates/          # Jinja2 HTML templates
└── test_moss_ai.py     # Tests for the AI assistant (stdlib unittest)
```

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/Witwiti/SmartBIMS.git
cd SmartBIMS
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Mac/Linux:
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize the database and create an admin account

```bash
python setup_bims.py
```

You'll be prompted to create an administrator username and password (minimum 8 characters).

### 5. Run the app

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `BIMS_SECRET_KEY` | Flask session secret | auto-generated |
| `BIMS_DATABASE` | Path to SQLite database | `instance/bims.sqlite3` |
| `BIMS_COOKIE_SECURE` | Set to `1` to require HTTPS cookies | unset |
| `BIMS_ADMIN_USERNAME` | Admin username (setup) | `admin` |
| `BIMS_ADMIN_NAME` | Admin full name (setup) | `System Administrator` |
| `BIMS_ADMIN_PASSWORD` | Admin password (setup) | prompted |

## The Moss AI Assistant

Moss works out of the box with no configuration: it falls back to a built-in
offline assistant that matches keywords and looks up the FAQ list.  Setting
`MOSS_API_KEY` upgrades it to a real AI that can read live records and file
requests on a resident's behalf.

### Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `MOSS_API_KEY` | API key for the provider (empty disables the AI) | empty |
| `MOSS_API_BASE_URL` | Base URL of the OpenAI-compatible endpoint | `https://api.openai.com/v1` |
| `MOSS_MODEL` | Model name | `gpt-4o-mini` |
| `MOSS_ENABLED` | Set to `0` to force the offline assistant | `1` |
| `MOSS_API_TIMEOUT` | Request timeout in seconds | `30` |
| `MOSS_MAX_TOKENS` | Maximum reply length in tokens | `600` |
| `MOSS_TEMPERATURE` | Sampling temperature | `0.3` |

Any OpenAI-compatible endpoint works, so you can point it at a hosted provider
or a local model:

```bash
# OpenAI
export MOSS_API_KEY="sk-..."
export MOSS_MODEL="gpt-4o-mini"

# OpenRouter
export MOSS_API_KEY="sk-or-..."
export MOSS_API_BASE_URL="https://openrouter.ai/api/v1"
export MOSS_MODEL="openai/gpt-4o-mini"

# Ollama, running locally (no key and no cost)
export MOSS_API_KEY="ollama"
export MOSS_API_BASE_URL="http://localhost:11434/v1"
export MOSS_MODEL="llama3.1"
```

### What the AI can do

| Tool | Who | Effect |
|------|-----|--------|
| `get_my_requests` | everyone | The signed-in user's own requests |
| `get_request_details` | everyone | One request plus its status history |
| `get_announcements` | everyone | Latest barangay announcements |
| `search_faqs` | everyone | Search the FAQ list |
| `get_incidents` | everyone | Own concerns for residents, all for staff |
| `get_my_profile` | everyone | The signed-in user's own profile |
| `get_barangay_stats` | staff | Request and resident totals |
| `get_activity_feed` | admin | Recent portal activity |
| `submit_service_request` | resident | File a document request |
| `report_incident` | everyone | Log a community concern |
| `cancel_service_request` | resident | Cancel a Pending or Processing request |

### Safety rules

These are enforced in code, not just in the prompt:

- **Confirmation gate.** Calling a write tool only *prepares* the action. The
  system asks the resident first and files it only after they clearly agree
  with a "yes". A refusal cancels it.
- **Role checks.** Read tools re-check the portal role on the server, so a
  resident cannot pull another resident's records or the barangay statistics.
- **Rate limiting.** Each session is capped at 10 messages per minute and 60
  per hour.
- **Graceful degradation.** If the key is missing, the provider is down, or the
  request fails, Moss answers with the offline assistant instead of erroring.

### Privacy

With the AI enabled, the text a resident types is sent to the configured AI
provider along with the records the model is allowed to read.  The chat widget
says so on every page.  Leave `MOSS_API_KEY` unset to keep everything local.

## Tests

```bash
python -m unittest test_moss_ai -v
```

The suite starts a fake OpenAI-compatible server, so it needs no API key and
no network access.

## Deployment on Render

1. Push your code to GitHub (done — see `.gitignore` to keep `instance/` and `.venv/` out).
2. Log in to [Render](https://render.com) and create a **Web Service**.
3. Connect your GitHub repository: `Witwiti/SmartBIMS`.
4. Choose **Docker** as the runtime (Render will use your `Dockerfile`).
5. Set the environment variables above in the Render dashboard. To enable the
   AI assistant, also set `MOSS_API_KEY` (and `MOSS_MODEL` if you are not
   using the default).
6. After the first deploy, run `setup_bims.py` via the Render shell (or set `BIMS_ADMIN_USERNAME` and `BIMS_ADMIN_PASSWORD` as env vars) to create your admin account.
7. Your app will be available at the Render URL provided.

> Render's free tier sleeps the service after inactivity and the SQLite file
> lives on an ephemeral disk, so the database is reset on every redeploy. Use
> a persistent disk or an external database for a real deployment.

## License

MIT