# SmartBIMS — Barangay Information Management System

A Flask web application for managing barangay service requests, resident accounts, announcements, and reports. Built with SQLite, featuring a chat assistant ("Moss") to help residents request documents and track their applications.

## Features

- **Resident portal** — Register, sign in, request documents (Barangay clearance, Certificate of residency, Certificate of indigency), and track request status
- **Staff portal (officials & admins)** — Review and update service requests, manage resident accounts, publish announcements, export data to CSV
- **Admin tools** — Activity log, user account management, reports and analytics
- **Moss assistant** — Chat-based help for document requests, status checks, announcements, and portal guidance
- **Docker-ready** — Includes `Dockerfile` for container deployment

## Tech Stack

- Python 3 + Flask
- SQLite (via `sqlite3`)
- HTML/CSS/Jinja2 templates
- Werkzeug (password hashing)

## Project Structure

```
bims_project/
├── app.py              # Main Flask application
├── setup_bims.py       # Creates the admin account and initializes the DB
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container definition
├── .gitignore          # Files excluded from git
├── instance/           # SQLite database (created at runtime, not committed)
├── static/             # CSS, JS, images
└── templates/          # Jinja2 HTML templates
```

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/Mitwiti/SmartBIMS.git
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

## Deployment on Render

1. Push your code to GitHub (done — see `.gitignore` to keep `instance/` and `.venv/` out).
2. Log in to [Render](https://render.com) and create a **Web Service**.
3. Connect your GitHub repository: `Mitwiti/SmartBIMS`.
4. Choose **Docker** as the runtime (Render will use your `Dockerfile`).
5. Set the environment variables above in the Render dashboard.
6. After the first deploy, run `setup_bims.py` via the Render shell (or set `BIMS_ADMIN_USERNAME` and `BIMS_ADMIN_PASSWORD` as env vars) to create your admin account.
7. Your app will be available at the Render URL provided.

## License

MIT