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
with open(DATA_PATH, "r") as f:
    CURVES = json.load(f)

# --- Helpers ---
def token_has_gmail_scope(token: dict) -> bool:
    """Return True if the OAuth token includes the Gmail read-only scope."""
    if not token:
        return False
    scopes = token.get("scope") or token.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    return bool(scopes and "https://www.googleapis.com/auth/gmail.readonly" in scopes)

def iso_to_epoch_ms(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)

def date_to_epoch_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

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
    for _ in range(32):  # ~day resolution
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
        # Fallback (rare): try last page of whole mailbox
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

# --------- Analytics helpers (timeline + milestones) ---------
def parse_timeline(platform):
    meta = CURVES.get(platform, {})
    tl = meta.get("timeline") or []
    tl = sorted([(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc), int(u)) for d, u in tl],
                key=lambda x: x[0])
    return tl

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

def pick_milestone(platform, joined_users):
    meta = CURVES.get(platform, {})
    milestones = meta.get("milestones") or []
    milestones = sorted(milestones, key=lambda m: m["users"])
    after = next((m for m in milestones if joined_users < m["users"]), None)
    if after:
        return f"before the platform {after['text']}"
    before = [m for m in milestones if joined_users >= m["users"]]
    if before:
        return f"shortly after it {before[-1]['text']}"
    return "very early in its growth"

def timeline_series_annual(platform):
    """
    Return yearly points (Jan 1) from launch to current year.
    Also provide values in millions for charting.
    """
    meta = CURVES.get(platform, {})
    launch = datetime.strptime(meta["launch_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_year = launch.year
    end_year = datetime.now(timezone.utc).year
    labels = []
    values = []
    for y in range(start_year, end_year + 1):
        dt = datetime(y, 1, 1, tzinfo=timezone.utc)
        labels.append(str(y))
        u = users_at(platform, int(dt.timestamp() * 1000))
        values.append(u if u is not None else 0)
    values_m = [round(v / 1_000_000, 3) for v in values]
    return {"labels": labels, "values": values, "values_m": values_m}

def nearest_year_label_for_ts(platform, when_ms):
    dt = datetime.fromtimestamp(when_ms/1000, tz=timezone.utc)
    return str(dt.year)

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_google", _external=True)
    # Force consent so users see the Gmail checkbox; allow upgrading scopes
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

def get_google_credentials():
    token = session.get("google_token")
    if not token or not token_has_gmail_scope(token):
        return None
    return Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
    )

def users_based_percentile(joined_users, today_users):
    """Percent of current users who arrived AFTER you (users-based percentile)."""
    if joined_users is None or today_users in (None, 0):
        return None
    # Example: join at 10M, today 1000M -> 1 - 10/1000 = 0.99 -> 99.0th percentile (you were early)
    return round(100 * (1 - (joined_users / today_users)), 1)

@app.route("/results", methods=["GET", "POST"])
def results():
    twitter_username = None
    if request.method == "POST":
        twitter_username = request.form.get("twitter_username", "").strip() or None
    else:
        twitter_username = request.args.get("twitter_username", "").strip() or None

    platforms = []
    percentiles = []  # now users-based; we'll average for composite

    # Gmail (if logged in with scope)
    creds = get_google_credentials()
    if creds:
        try:
            gmail_ms = gmail_oldest_message_epoch_ms_binary_search(creds)
            if gmail_ms:
                joined_u = users_at("gmail", gmail_ms)
                today_u = users_today("gmail")
                series = timeline_series_annual("gmail")
                join_label = nearest_year_label_for_ts("gmail", gmail_ms)
                p_users = users_based_percentile(joined_u, today_u)
                if p_users is not None:
                    percentiles.append(p_users / 100.0)  # store as 0..1 for averaging
                platforms.append({
                    "name": "Gmail",
                    "joined": ms_to_datestr(gmail_ms),
                    "percentile": p_users,  # users-based percentile (matches narrative)
                    "joined_users": joined_u,
                    "today_users": today_u,
                    "narrative_percent": p_users,  # same definition to avoid mismatch
                    "narrative_fact": pick_milestone("gmail", joined_u) if joined_u else None,
                    "chart": series,
                    "join_label": join_label
                })
            else:
                platforms.append({"name": "Gmail", "joined": "Unavailable", "percentile": None, "note": "Could not determine oldest message."})
        except Exception as e:
            platforms.append({"name": "Gmail", "joined": "Error", "percentile": None, "note": str(e)})
    else:
        platforms.append({"name": "Gmail", "joined": "Not connected", "percentile": None, "note": "Grant Gmail Read-Only to estimate your start date."})

    # Twitter/X by username (optional)
    if twitter_username:
        t_ms, _ = twitter_created_at(twitter_username)
        if t_ms:
            joined_u = users_at("twitter", t_ms)
            today_u = users_today("twitter")
            series = timeline_series_annual("twitter")
            join_label = nearest_year_label_for_ts("twitter", t_ms)
            p_users = users_based_percentile(joined_u, today_u)
            if p_users is not None:
                percentiles.append(p_users / 100.0)
            platforms.append({
                "name": "Twitter/X",
                "joined": ms_to_datestr(t_ms),
                "percentile": p_users,
                "username": twitter_username,
                "joined_users": joined_u,
                "today_users": today_u,
                "narrative_percent": p_users,
                "narrative_fact": pick_milestone("twitter", joined_u) if joined_u else None,
                "chart": series,
                "join_label": join_label
            })
        else:
            platforms.append({"name": "Twitter/X", "joined": "Unavailable", "percentile": None, "username": twitter_username, "note": "API token missing or lookup failed."})

    # Composite score: average of users-based percentiles across connected platforms
    score = None
    if percentiles:
        avg = sum(percentiles) / len(percentiles)   # 0..1
        score = round(100 * avg, 1)

    return render_template("results.html", platforms=platforms, score=score, twitter_username=twitter_username)

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

# Optional: minimal diag
@app.route("/diag")
def diag():
    token = session.get("google_token")
    safe = {
        "GOOGLE_CLIENT_ID_set": bool(os.getenv("GOOGLE_CLIENT_ID")),
        "GOOGLE_CLIENT_SECRET_set": bool(os.getenv("GOOGLE_CLIENT_SECRET")),
        "has_token": bool(token),
        "token_has_gmail_scope": token_has_gmail_scope(token) if token else False,
    }
    return jsonify(safe)

# ---- Twitter/X helper ----
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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
