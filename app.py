import os
import json
from datetime import datetime, timedelta, timezone, date
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def getenv(name, default=None):
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing environment variable: {name}")
    return v

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = getenv("SECRET_KEY", "dev-secret-change-me")

GOOGLE_CLIENT_ID = getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = getenv("GOOGLE_CLIENT_SECRET", "")

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email https://www.googleapis.com/auth/gmail.readonly"},
)

# ---- safe curve loader
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "adoption_curves.json")
def load_curves(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print("ERROR loading adoption_curves.json:", e)
        return {"gmail": {"launch_date":"2004-04-01","timeline":[
            ["2004-04-01",0],["2012-01-01",350000000],["2018-10-26",1500000000],["2025-01-01",1800000000]
        ]}}
CURVES = load_curves(DATA_PATH)

# ---------- Metric tags & pretty names ----------
METRIC_TAGS = {
    "gmail": "Users",
    "twitter": "MAU",
    "instagram": "MAU",
    "linkedin": "Members",
    "dropbox": "Registered users",
    "openai": "WAU",
    "spotify": "MAU",
    "reddit": "DAU",
    "amazonprime": "Paid subscribers",
}
PRETTY_NAMES = {
    "gmail": "Gmail",
    "twitter": "Twitter/X",
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "dropbox": "Dropbox",
    "openai": "OpenAI/ChatGPT",
    "spotify": "Spotify",
    "reddit": "Reddit",
    "amazonprime": "Amazon Prime",
}
# Simple logo URLs (SVG/PNG). Use your own /static files later if you prefer.
LOGO_URLS = {
    "gmail": "https://upload.wikimedia.org/wikipedia/commons/4/4e/Gmail_Icon.png",
    "twitter": "https://upload.wikimedia.org/wikipedia/commons/5/53/X_logo_2023_original.svg",
    "instagram": "https://upload.wikimedia.org/wikipedia/commons/a/a5/Instagram_icon.png",
    "linkedin": "https://upload.wikimedia.org/wikipedia/commons/8/81/LinkedIn_icon.svg",
    "dropbox": "https://upload.wikimedia.org/wikipedia/commons/7/78/Dropbox_Icon.svg",
    "openai": "https://upload.wikimedia.org/wikipedia/commons/4/4d/OpenAI_Logo.svg",
    "spotify": "https://upload.wikimedia.org/wikipedia/commons/1/19/Spotify_logo_without_text.svg",
    "reddit": "https://upload.wikimedia.org/wikipedia/en/5/58/Reddit_logo_new.svg",
    "amazonprime": "https://upload.wikimedia.org/wikipedia/commons/f/f1/Prime_logo.png"
}

# ---------- Utilities ----------
def token_has_gmail_scope(token: dict) -> bool:
    if not token:
        return False
    scopes = token.get("scope") or token.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    return bool(scopes and "https://www.googleapis.com/auth/gmail.readonly" in scopes)

def ms_to_pretty_date(ms):
    dt = datetime.fromtimestamp(ms/1000, tz=timezone.utc)
    try:
        return dt.strftime("%B %-d, %Y")
    except Exception:
        return dt.strftime("%B %d, %Y")

def ms_to_iso_date(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")

def month_year_to_ms(month_str: str, year_str: str):
    try:
        m = int(month_str); y = int(year_str)
        dt = datetime(y, m, 1, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def abs_month_diff(a_dt: date, b_dt: date) -> int:
    am = a_dt.year * 12 + a_dt.month
    bm = b_dt.year * 12 + b_dt.month
    return abs(am - bm)

def month_diff(a_dt: date, b_dt: date) -> int:
    """Whole months from a_dt to b_dt (signed)."""
    return (b_dt.year - a_dt.year) * 12 + (b_dt.month - a_dt.month)

def months_to_human(m: int) -> str:
    """Convert months -> 'X years Y months' (omit zero parts)."""
    years = m // 12
    months = m % 12
    parts = []
    if years:
        parts.append(f"{years} year" + ("s" if years != 1 else ""))
    if months:
        parts.append(f"{months} month" + ("s" if months != 1 else ""))
    if not parts:
        return "0 months"
    return " ".join(parts)

# ---------- Gmail helpers ----------
def gmail_service(credentials: Credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)

def gmail_oldest_for_query(credentials: Credentials, query: str):
    svc = gmail_service(credentials)
    page_token = None
    last_id = None
    while True:
        resp = svc.users().messages().list(userId="me", maxResults=500, pageToken=page_token, q=query).execute()
        ids = resp.get("messages", [])
        if ids:
            last_id = ids[-1]["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if not last_id:
        return None
    msg = svc.users().messages().get(userId="me", id=last_id, format="metadata").execute()
    return int(msg.get("internalDate"))

# Fast baseline (binary search) — avoids timeouts
def gmail_has_messages_before(service, dt):
    q = f"before:{dt.strftime('%Y/%m/%d')}"
    resp = service.users().messages().list(userId="me", maxResults=1, q=q).execute()
    return bool(resp.get("messages"))

def gmail_find_earliest_date(service):
    lo = datetime(2004, 1, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=1)
    for _ in range(32):
        mid = lo + (hi - lo) / 2
        if gmail_has_messages_before(service, mid):
            hi = mid
        else:
            lo = mid
    return (hi - timedelta(days=1)).date()

def gmail_oldest_message_epoch_ms_binary_search(credentials: Credentials):
    service = gmail_service(credentials)
    earliest_day = gmail_find_earliest_date(service)
    start = earliest_day - timedelta(days=3)
    end = earliest_day + timedelta(days=11)
    q = f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"
    page_token = None
    last_id = None
    while True:
        resp = service.users().messages().list(userId="me", maxResults=500, pageToken=page_token, q=q).execute()
        ids = resp.get("messages", [])
        if ids:
            last_id = ids[-1]["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if not last_id:
        return None
    msg = service.users().messages().get(userId="me", id=last_id, format="metadata").execute()
    return int(msg.get("internalDate"))

# Gmail welcome queries (NO Facebook)
WELCOME_QUERIES = {
    "instagram":   'from:(mail.instagram.com OR security@mail.instagram.com) (subject:(Welcome) OR subject:(Confirm))',
    "linkedin":    'from:(linkedin.com) (subject:(Welcome) OR subject:(Confirm))',
    "dropbox":     'from:(no-reply@dropbox.com) (subject:(Welcome) OR subject:(Confirm))',
    "openai":      'from:(noreply@openai.com OR team@openai.com) (subject:(Welcome) OR subject:(Confirm))',
    "spotify":     'from:(no-reply@spotify.com) (subject:(Welcome) OR subject:(Confirm) OR subject:(Welcome to Spotify))',
    "twitter":     'from:(twitter.com OR verify@twitter.com OR info@twitter.com OR hello@twitter.com) (subject:(Welcome) OR subject:(Confirm))',
    "reddit":      'from:(noreply@reddit.com) (subject:(welcome) OR subject:(confirm))',
    "amazonprime": 'from:(no-reply@amazon.com OR prime@amazon.com) (subject:(Welcome to Amazon Prime) OR subject:(Your Amazon Prime) OR subject:(Confirm))'
}

# ---------- Adoption math ----------
def parse_timeline(platform):
    meta = CURVES.get(platform, {})
    tl = meta.get("timeline") or []
    tl = sorted([(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc), int(u)) for d, u in tl],
                key=lambda x: x[0])
    return tl

def launch_date(platform):
    meta = CURVES.get(platform, {})
    ld = meta.get("launch_date")
    if not ld:
        tl = parse_timeline(platform)
        return tl[0][0] if tl else None
    return datetime.strptime(ld, "%Y-%m-%d").replace(tzinfo=timezone.utc)

def users_at(platform, when_ms):
    tl = parse_timeline(platform)
    if not tl:
        return None
    when = datetime.fromtimestamp(when_ms/1000, tz=timezone.utc)
    if when <= tl[0][0]: return tl[0][1]
    if when >= tl[-1][0]: return tl[-1][1]
    for (d0, u0), (d1, u1) in zip(tl, tl[1:]):
        if d0 <= when <= d1:
            span = (d1 - d0).total_seconds()
            frac = (when - d0).total_seconds() / span if span else 0.0
            return round(u0 + frac * (u1 - u0))
    return tl[-1][1]

def users_today(platform):
    tl = parse_timeline(platform)
    return tl[-1][1] if tl else None

def timeline_series_time(platform):
    tl = parse_timeline(platform)
    if not tl:
        return {"points_m": []}
    points = [{"x": d.strftime("%Y-%m-%d"), "y": round(u/1_000_000, 3)} for d, u in tl]
    today = datetime.now(timezone.utc).date()
    last_date = tl[-1][0].date()
    if last_date < today:
        points.append({"x": today.strftime("%Y-%m-%d"), "y": round(tl[-1][1]/1_000_000, 3)})
    return {"points_m": points}

def early_adopter_percentile(joined_users, today_users):
    if joined_users is None or today_users in (None, 0):
        return None
    return round(100 * (joined_users / today_users), 1)  # smaller = earlier

def joined_before_percent(joined_users, today_users):
    if joined_users is None or today_users in (None, 0):
        return None
    return round(100 * (1 - (joined_users / today_users)), 1)

# ---------- Routes ----------
@app.route("/")
def index():
    months = ["January","February","March","April","May","June","July","August","September","October","November","December"]
    current_year = datetime.now(timezone.utc).year
    years = list(range(current_year, 1999, -1))  # current -> 2000
    return render_template("index.html", months=months, years=years)

@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_google", _external=True)
    return oauth.google.authorize_redirect(
        redirect_uri,
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
        scope="openid email https://www.googleapis.com/auth/gmail.readonly",
    )

@app.route("/auth/google")
def auth_google():
    token = oauth.google.authorize_access_token()
    session["google_token"] = token
    if not token_has_gmail_scope(token):
        return render_template("need_gmail.html")
    return redirect(url_for("results"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

def get_google
