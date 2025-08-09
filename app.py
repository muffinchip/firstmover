import os
import json
from datetime import datetime, timedelta, timezone
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

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "adoption_curves.json")
with open(DATA_PATH, "r") as f:
    CURVES = json.load(f)

# -------- Utilities --------
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

def date_input_to_ms(s: str):
    # expects "YYYY-MM-DD"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

# -------- Gmail helpers --------
def gmail_service(credentials: Credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)

def gmail_oldest_for_query(credentials: Credentials, query: str):
    """Find the oldest message matching the Gmail search query."""
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

# Gmail welcome queries (NO Facebook)
WELCOME_QUERIES = {
    "instagram": 'from:(mail.instagram.com OR security@mail.instagram.com) (subject:(Welcome) OR subject:(Confirm))',
    "linkedin":  'from:(linkedin.com) (subject:(Welcome) OR subject:(Confirm))',
    "dropbox":   'from:(no-reply@dropbox.com) (subject:(Welcome) OR subject:(Confirm))',
    "openai":    'from:(noreply@openai.com OR team@openai.com) (subject:(Welcome) OR subject:(Confirm))',
    "spotify":   'from:(no-reply@spotify.com) (subject:(Welcome) OR subject:(Confirm) OR subject:(Welcome to Spotify))',
    "twitter":   'from:(twitter.com OR verify@twitter.com OR info@twitter.com OR hello@twitter.com) (subject:(Welcome) OR subject:(Confirm))',
    "reddit":    'from:(noreply@reddit.com) (subject:(welcome) OR subject:(confirm))',
    "amazonprime": 'from:(no-reply@amazon.com OR prime@amazon.com) (subject:(Welcome to Amazon Prime) OR subject:(Your Amazon Prime) OR subject:(Confirm))'
}

# -------- Adoption math --------
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

def timeline_series_time(platform):
    """Return [{x:'YYYY-MM-DD', y:<millions>}] along the platform timeline, extending flat to today if needed."""
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
    # small number = earlier (top percentile)
    if joined_users is None or today_users in (None, 0):
        return None
    return round(100 * (joined_users / today_users), 1)

def joined_before_percent(joined_users, today_users):
    if joined_users is None or today_users in (None, 0):
        return None
    return round(100 * (1 - (joined_users / today_users)), 1)

# -------- Routes --------
@app.route("/")
def index():
    return render_template("index.html")

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

@app.route("/results", methods=["GET", "POST"])
def results():
    # Collect manual entries
    manual_dates = {}  # key -> ms
    platform_keys = ["twitter", "instagram", "linkedin", "dropbox", "openai", "spotify", "reddit", "amazonprime"]

    # optional username for Twitter card label
    twitter_username = None

    if request.method == "POST":
        twitter_username = (request.form.get("twitter_username") or "").strip() or None
        for key in platform_keys:
            field = f"{key}_join_date"
            if field in request.form and request.form[field]:
                ms = date_input_to_ms(request.form[field])
                if ms:
                    manual_dates[key] = ms
    else:
        # allow querystring ?twitter_join_date=YYYY-MM-DD, etc.
        twitter_username = (request.args.get("twitter_username") or "").strip() or None
        for key in platform_keys:
            s = request.args.get(f"{key}_join_date")
            if s:
                ms = date_input_to_ms(s)
                if ms:
                    manual_dates[key] = ms

    platforms = []
    percentiles = []

    # A) Gmail-based detection (if available)
    creds = get_google_credentials()
    if creds:
        for key in ["instagram", "linkedin", "dropbox", "openai", "spotify", "twitter", "reddit", "amazonprime"]:
            q = WELCOME_QUERIES.get(key)
            if not q:
                continue
            try:
                ts = gmail_oldest_for_query(creds, q)
            except Exception as e:
                ts = None
            if ts:
                # If user also provided a manual date, prefer manual (assume more accurate)
                if key not in manual_dates:
                    manual_dates[key] = ts

    # B) Build cards for any platform we now have a date for
    pretty_names = {
        "gmail": "Gmail",
        "twitter": "Twitter/X",
        "instagram": "Instagram",
        "linkedin": "LinkedIn",
        "dropbox": "Dropbox",
        "openai": "OpenAI/ChatGPT",
        "spotify": "Spotify",
        "reddit": "Reddit",
        "amazonprime": "Amazon Prime"
    }

    # Gmail baseline (mailbox oldest) — keep as its own card if desired (optional)
    # If you don't want Gmail itself as a platform card, comment this block out.
    # (We keep it because many users like seeing their email start date.)
    if creds:
        try:
            # Oldest message in mailbox as proxy for "started using email"
            # You can re-enable the binary-search logic if you had it before; here we reuse a broad fallback.
            # For stability, we’ll query the whole mailbox last page:
            ts = gmail_oldest_for_query(creds, "")  # empty query -> whole mailbox
            if ts:
                joined_u = users_at("gmail", ts)
                today_u = users_today("gmail")
                if joined_u and today_u:
                    badge = early_adopter_percentile(joined_u, today_u)
                    if badge is not None:
                        percentiles.append(badge / 100.0)
                    platforms.append({
                        "name": pretty_names["gmail"],
                        "joined": ms_to_pretty_date(ts),
                        "percentile": badge,
                        "joined_users": joined_u,
                        "today_users": today_u,
                        "narrative_percent": joined_before_percent(joined_u, today_u),
                        "chart": timeline_series_time("gmail"),
                        "join_iso": ms_to_iso_date(ts)
                    })
        except Exception:
            pass

    # Now add all other platforms for which we have a date
    for key, ts in manual_dates.items():
        if key not in CURVES:
            continue  # no curve data to plot against
        joined_u = users_at(key, ts)
        today_u = users_today(key)
        series = timeline_series_time(key)
        if joined_u and today_u:
            badge = early_adopter_percentile(joined_u, today_u)
            if badge is not None:
                percentiles.append(badge / 100.0)
            name = pretty_names.get(key, key.title())
            title = name
            extra = {}
            if key == "twitter" and twitter_username:
                extra["username"] = twitter_username
            platforms.append({
                "name": title,
                "joined": ms_to_pretty_date(ts),
                "percentile": badge,
                "joined_users": joined_u,
                "today_users": today_u,
                "narrative_percent": joined_before_percent(joined_u, today_u),
                "chart": series,
                "join_iso": ms_to_iso_date(ts),
                **extra
            })

    # Composite (small = earlier). Only if at least one platform produced a percentile.
    score = round(100 * (sum(percentiles) / len(percentiles)), 1) if percentiles else None

    # Only render platforms that we actually have dates for
    platforms = [p for p in platforms if p.get("join_iso")]

    return render_template("results.html", platforms=platforms, score=score, twitter_username=twitter_username)

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
