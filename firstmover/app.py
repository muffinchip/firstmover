
import os
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import requests

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
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")  # optional

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email https://www.googleapis.com/auth/gmail.readonly"},
)

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "adoption_curves.json")
if not os.path.exists(DATA_PATH):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump({
            "gmail": {"launch_date": "2004-04-01", "maturity_date": "2012-01-01"},
            "twitter": {"launch_date": "2006-03-21", "maturity_date": "2014-01-01"}
        }, f, indent=2)

with open(DATA_PATH, "r") as f:
    CURVES = json.load(f)

# --- Helpers ---
def iso_to_epoch_ms(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)

def date_to_epoch_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def percentile_from_dates(platform: str, joined_ts_ms: int) -> float:
    meta = CURVES.get(platform)
    if not meta:
        return 0.5
    start = date_to_epoch_ms(meta["launch_date"])
    end = date_to_epoch_ms(meta["maturity_date"])
    if joined_ts_ms <= start: return 0.0
    if joined_ts_ms >= end: return 1.0
    return (joined_ts_ms - start) / max(1, (end - start))

def composite_score(percentiles):
    if not percentiles:
        return 50
    avg = sum(percentiles) / len(percentiles)
    return round(100 * avg, 1)

def twitter_created_at(username: str):
    if not X_BEARER_TOKEN:
        return None, None
    url_primary = f"https://api.x.com/2/users/by/username/{username}?user.fields=created_at"
    url_alt = f"https://api.twitter.com/2/users/by/username/{username}?user.fields=created_at"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    for u in (url_primary, url_alt):
        try:
            r = requests.get(u, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                created = data.get("data", {}).get("created_at")
                if created:
                    ms = iso_to_epoch_ms(created)
                    return ms, created
        except Exception:
            pass
    return None, None

# ---------- Gmail oldest message via date binary search ----------
def gmail_service(credentials: Credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)

def gmail_has_messages_before(service, dt):
    """Return True if there exists at least one message strictly before midnight of dt (UTC)."""
    q = f"before:{dt.strftime('%Y/%m/%d')}"
    resp = service.users().messages().list(userId="me", maxResults=1, q=q).execute()
    return bool(resp.get("messages"))

def gmail_find_earliest_date(service):
    """Binary search the earliest date with any message, between 2004-01-01 and today+1."""
    lo = datetime(2004, 1, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=1)
    for _ in range(32):  # day-resolution bsearch
        mid = lo + (hi - lo) / 2
        if gmail_has_messages_before(service, mid):
            hi = mid
        else:
            lo = mid
    approx = (hi - timedelta(days=1)).date()
    return approx

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
        # Fallback (rare): try the whole mailbox last page
        page_token = None
        last_id = None
        while True:
            resp = service.users().messages().list(userId="me", maxResults=500, pageToken=page_token, q="").execute()
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

def ms_to_datestr(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_google", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google")
def auth_google():
    token = oauth.google.authorize_access_token()
    session["google_token"] = token
    return redirect(url_for("results"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

def get_google_credentials():
    token = session.get("google_token")
    if not token:
        return None
    return Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
    )

@app.route("/results", methods=["GET", "POST"])
def results():
    twitter_username = None
    if request.method == "POST":
        twitter_username = request.form.get("twitter_username", "").strip() or None
    else:
        twitter_username = request.args.get("twitter_username", "").strip() or None

    platforms = []
    percentiles = []

    # Gmail (if logged in)
    creds = get_google_credentials()
    if creds:
        try:
            gmail_ms = gmail_oldest_message_epoch_ms_binary_search(creds)
            if gmail_ms:
                p = percentile_from_dates("gmail", gmail_ms)
                percentiles.append(p)
                platforms.append({
                    "name": "Gmail",
                    "joined": ms_to_datestr(gmail_ms),
                    "percentile": round(100*p, 1),
                })
            else:
                platforms.append({"name": "Gmail", "joined": "Unavailable", "percentile": None, "note": "Could not determine oldest message."})
        except Exception as e:
            platforms.append({"name": "Gmail", "joined": "Error", "percentile": None, "note": str(e)})

    # Twitter/X by username (optional)
    if twitter_username:
        t_ms, t_iso = twitter_created_at(twitter_username)
        if t_ms:
            p = percentile_from_dates("twitter", t_ms)
            percentiles.append(p)
            platforms.append({
                "name": "Twitter/X",
                "joined": ms_to_datestr(t_ms),
                "percentile": round(100*p, 1),
                "username": twitter_username
            })
        else:
            platforms.append({"name": "Twitter/X", "joined": "Unavailable", "percentile": None, "username": twitter_username, "note": "API token missing or lookup failed."})

    score = composite_score(percentiles) if percentiles else None
    return render_template("results.html", platforms=platforms, score=score, twitter_username=twitter_username)

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
