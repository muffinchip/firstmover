import os
import json
import uuid
from datetime import datetime, timedelta, timezone, date
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL")
engine = create_engine(DB_URL, pool_pre_ping=True) if DB_URL else None

def init_db():
    if not engine:
        return
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS firstmover_results (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            run_id TEXT,
            platform TEXT,
            percentile NUMERIC,
            verified BOOLEAN,
            join_date DATE,
            source TEXT,
            joined_users BIGINT,
            today_users BIGINT,
            composite_overall NUMERIC,
            composite_verified NUMERIC,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );"""))
init_db()

def save_results(uid, platforms, score, score_verified):
    if not engine or not platforms:
        return
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        for p in platforms:
            conn.execute(text("""
            INSERT INTO firstmover_results
            (user_id, run_id, platform, percentile, verified, join_date, source,
             joined_users, today_users, composite_overall, composite_verified)
            VALUES
            (:user_id, :run_id, :platform, :percentile, :verified, :join_date, :source,
             :joined_users, :today_users, :composite_overall, :composite_verified)"""), {
                "user_id": uid,
                "run_id": run_id,
                "platform": p["name"],
                "percentile": p.get("percentile"),
                "verified": bool(p.get("verified")),
                "join_date": p.get("join_iso"),
                "source": "gmail" if p.get("verified") else "manual",
                "joined_users": p.get("joined_users"),
                "today_users": p.get("today_users"),
                "composite_overall": score,
                "composite_verified": score_verified
            })

def getenv(name, default=None):
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing environment variable: {name}")
    return v

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = getenv("SECRET_KEY", "dev-secret")

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
CURVES = json.load(open(DATA_PATH))

METRIC_TAGS = {"gmail":"Users","facebook":"MAU","twitter":"MAU","instagram":"MAU","linkedin":"Members","dropbox":"Registered users","openai":"WAU","spotify":"MAU","reddit":"DAU","amazonprime":"Paid subscribers"}
PRETTY_NAMES = {"gmail":"Gmail","facebook":"Facebook","twitter":"Twitter/X","instagram":"Instagram","linkedin":"LinkedIn","dropbox":"Dropbox","openai":"OpenAI/ChatGPT","spotify":"Spotify","reddit":"Reddit","amazonprime":"Amazon Prime"}
LOGO_URLS = {
    "gmail":"https://upload.wikimedia.org/wikipedia/commons/4/4e/Gmail_Icon.png",
    "facebook":"https://upload.wikimedia.org/wikipedia/commons/0/05/Facebook_Logo_%282019%29.png",
    "twitter":"https://upload.wikimedia.org/wikipedia/commons/5/53/X_logo_2023_original.svg",
    "instagram":"https://upload.wikimedia.org/wikipedia/commons/a/a5/Instagram_icon.png",
    "linkedin":"https://upload.wikimedia.org/wikipedia/commons/8/81/LinkedIn_icon.svg",
    "dropbox":"https://upload.wikimedia.org/wikipedia/commons/7/78/Dropbox_Icon.svg",
    "openai":"https://upload.wikimedia.org/wikipedia/commons/4/4d/OpenAI_Logo.svg",
    "spotify":"https://upload.wikimedia.org/wikipedia/commons/1/19/Spotify_logo_without_text.svg",
    "reddit":"https://upload.wikimedia.org/wikipedia/commons/5/58/Reddit_logo_new.svg",
    "amazonprime":"https://upload.wikimedia.org/wikipedia/commons/a/a9/Amazon_logo.svg"
}

def token_has_gmail_scope(token):
    if not token: return False
    scopes = token.get("scope") or token.get("scopes")
    if isinstance(scopes, str): scopes = scopes.split()
    return scopes and "https://www.googleapis.com/auth/gmail.readonly" in scopes

def ms_to_pretty(ms):
    dt = datetime.fromtimestamp(ms/1000, tz=timezone.utc)
    try: return dt.strftime("%B %-d, %Y")
    except Exception: return dt.strftime("%B %d, %Y")
def ms_to_iso(ms): return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")

def month_year_to_ms(m, y):
    try: return int(datetime(int(y), int(m), 1, tzinfo=timezone.utc).timestamp()*1000)
    except: return None

def abs_month_diff(a, b):
    return abs((a.year*12+a.month) - (b.year*12+b.month))
def month_diff(a, b): return (b.year-a.year)*12 + (b.month-a.month)

def gmail_service(credentials): return build("gmail","v1",credentials=credentials, cache_discovery=False)

def gmail_has_before(service, dt):
    q=f"before:{dt.strftime('%Y/%m/%d')}"
    return bool(service.users().messages().list(userId='me', maxResults=1, q=q).execute().get("messages"))

def gmail_find_first_day(service):
    lo=datetime(2004,1,1,tzinfo=timezone.utc); hi=datetime.now(timezone.utc)+timedelta(days=1)
    for _ in range(32):
        mid=lo+(hi-lo)/2
        if gmail_has_before(service, mid): hi=mid
        else: lo=mid
    return (hi-timedelta(days=1)).date()

def gmail_first_msg_ms(credentials):
    svc=gmail_service(credentials)
    d=gmail_find_first_day(svc)
    start=d - timedelta(days=3); end=d + timedelta(days=11)
    q=f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"
    page=None; last=None
    while True:
        resp=svc.users().messages().list(userId='me',maxResults=500,pageToken=page,q=q).execute()
        ids=resp.get("messages",[])
        if ids: last=ids[-1]["id"]
        page=resp.get("nextPageToken")
        if not page: break
    if not last: return None
    msg=svc.users().messages().get(userId='me',id=last,format="metadata").execute()
    return int(msg.get("internalDate"))

# Broadened, multi-try queries per platform (most-specific first)
WELCOME_QUERY_SETS = {
    "reddit": [
        'from:(noreply@reddit.com OR do-not-reply@reddit.com OR noreply@redditmail.com) subject:(welcome OR verify OR confirm) -subject:password -subject:reset',
        'from:(noreply@reddit.com OR do-not-reply@reddit.com OR noreply@redditmail.com) -subject:password -subject:reset',
    ],
    "amazonprime": [
        'from:(no-reply@amazon.com OR prime@amazon.com OR digital-no-reply@amazon.com OR prime-enroll@amazon.com) subject:(prime OR welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(no-reply@amazon.com OR prime@amazon.com OR digital-no-reply@amazon.com OR prime-enroll@amazon.com) -subject:password -subject:reset',
    ],
    "dropbox": [
        'from:(no-reply@dropbox.com OR dropbox@mail.dropbox.com OR no-reply@dropboxmail.com) subject:(welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(no-reply@dropbox.com OR dropbox@mail.dropbox.com OR no-reply@dropboxmail.com) -subject:password -subject:reset',
    ],
    "openai": [
        'from:(noreply@openai.com OR team@openai.com OR no-reply@accounts.openai.com OR noreply@accounts.openai.com OR no-reply@chat.openai.com) subject:(welcome OR confirm OR verify OR "ChatGPT") -subject:password -subject:reset',
        'from:(noreply@openai.com OR team@openai.com OR no-reply@accounts.openai.com OR noreply@accounts.openai.com OR no-reply@chat.openai.com) -subject:password -subject:reset',
    ],
    "facebook": [
        'from:(facebookmail.com OR notify@facebookmail.com) subject:(welcome OR confirm OR verify OR "Just one more step") -subject:password -subject:reset',
        'from:(facebookmail.com OR notify@facebookmail.com) -subject:password -subject:reset',
    ],
    "instagram": [
        'from:(mail.instagram.com OR security@mail.instagram.com) subject:(welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(mail.instagram.com OR security@mail.instagram.com) -subject:password -subject:reset',
    ],
    "linkedin": [
        'from:(linkedin.com) subject:(welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(linkedin.com) -subject:password -subject:reset',
    ],
    "spotify": [
        'from:(no-reply@spotify.com) subject:(welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(no-reply@spotify.com) -subject:password -subject:reset',
    ],
    "twitter": [
        'from:(twitter.com OR verify@twitter.com OR info@twitter.com OR hello@twitter.com) subject:(welcome OR confirm OR verify) -subject:password -subject:reset',
        'from:(twitter.com OR verify@twitter.com OR info@twitter.com OR hello@twitter.com) -subject:password -subject:reset',
    ],
}

def gmail_oldest_for_query(credentials, q):
    """Return ms timestamp of the oldest hit for a single Gmail search query."""
    svc = gmail_service(credentials)
    page = None
    last = None
    pages = 0
    while True:
        resp = svc.users().messages().list(userId="me", maxResults=500, pageToken=page, q=q).execute()
        ids = resp.get("messages", [])
        if ids:
            last = ids[-1]["id"]
        page = resp.get("nextPageToken")
        pages += 1
        if not page or pages > 50:
            break
    if not last:
        return None
    msg = svc.users().messages().get(userId="me", id=last, format="metadata").execute()
    return int(msg.get("internalDate"))

def gmail_oldest_from_queries(credentials, queries):
    """Try multiple queries (most-specific first). Return earliest timestamp found (ms)."""
    best_ts = None
    for q in queries:
        try:
            ts = gmail_oldest_for_query(credentials, q)
        except Exception:
            ts = None
        if ts is not None:
            if best_ts is None or ts < best_ts:
                best_ts = ts
    return best_ts

def parse_timeline(platform):
    tl = sorted([(datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=timezone.utc), int(u)) for d,u in CURVES.get(platform,{}).get("timeline",[])], key=lambda x:x[0])
    return tl

def launch_date(platform):
    ld = CURVES.get(platform,{}).get("launch_date")
    if ld: return datetime.strptime(ld,"%Y-%m-%d").replace(tzinfo=timezone.utc)
    tl=parse_timeline(platform); return tl[0][0] if tl else None

def users_at(platform, when_ms):
    tl=parse_timeline(platform)
    if not tl: return None
    when=datetime.fromtimestamp(when_ms/1000, tz=timezone.utc)
    if when<=tl[0][0]: return tl[0][1]
    if when>=tl[-1][0]: return tl[-1][1]
    for (d0,u0),(d1,u1) in zip(tl, tl[1:]):
        if d0<=when<=d1:
            span=(d1-d0).total_seconds()
            frac=(when-d0).total_seconds()/span if span else 0
            return round(u0 + frac*(u1-u0))
    return tl[-1][1]

def users_today(platform):
    tl=parse_timeline(platform); return tl[-1][1] if tl else None

def timeline_series(platform):
    tl=parse_timeline(platform)
    if not tl: return {"points_m":[]}
    pts=[{"x":d.strftime("%Y-%m-%d"), "y": round(u/1_000_000,3)} for d,u in tl]
    today=datetime.now(timezone.utc).date()
    if tl[-1][0].date()<today:
        pts.append({"x": today.strftime("%Y-%m-%d"), "y": round(tl[-1][1]/1_000_000,3)})
    return {"points_m": pts}

def early_pct(joined, today):
    if not joined or not today: return None
    return round(100*(joined/today),1)

def before_pct(joined, today):
    if not joined or not today: return None
    return round(100*(1-(joined/today)),1)

@app.route("/")
def index():
    months = ["January","February","March","April","May","June","July","August","September","October","November","December"]
    years = list(range(datetime.now(timezone.utc).year, 1999, -1))
    return render_template("index.html", months=months, years=years)

@app.post("/save_manual")
def save_manual():
    data = request.get_json(silent=True) or {}
    platform_keys=["facebook","twitter","instagram","linkedin","dropbox","openai","spotify","reddit","amazonprime"]
    manual = {}
    for k in platform_keys:
        m = data.get(f"{k}_join_month")
        y = data.get(f"{k}_join_year")
        if m and y:
            try:
                ms = int(datetime(int(y), int(m), 1, tzinfo=timezone.utc).timestamp()*1000)
            except Exception:
                ms = None
            if ms: manual[k] = ms
    session["manual_entries"] = manual
    return jsonify({"ok": True, "count": len(manual)})

@app.route("/login/google")
def login_google():
    session.setdefault("uid", session.get("uid") or str(uuid.uuid4()))
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
    info = token.get("userinfo") or {}
    session["uid"] = info.get("sub") or info.get("email") or session.get("uid") or str(uuid.uuid4())
    return redirect(url_for("results"))

def get_google_credentials():
    token = session.get("google_token")
    if not token or not token_has_gmail_scope(token): return None
    return Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["openid","email","https://www.googleapis.com/auth/gmail.readonly"],
    )

@app.route("/results", methods=["GET","POST"])
def results():
    platform_keys=["facebook","twitter","instagram","linkedin","dropbox","openai","spotify","reddit","amazonprime"]
    manual = session.get("manual_entries", {}) if request.method=="GET" else {}

    if request.method=="POST":
        for k in platform_keys:
            m=request.form.get(f"{k}_join_month"); y=request.form.get(f"{k}_join_year")
            if m and y:
                try:
                    ms=int(datetime(int(y),int(m),1,tzinfo=timezone.utc).timestamp()*1000)
                except Exception:
                    ms=None
                if ms: manual[k]=ms
        session["manual_entries"]=manual

    gmail_hits={}
    creds=get_google_credentials()
    if creds:
        # Try multiple queries per platform; pick earliest
        for k, query_list in WELCOME_QUERY_SETS.items():
            if k not in platform_keys: 
                continue
            try:
                ts = gmail_oldest_from_queries(creds, query_list)
            except Exception:
                ts = None
            if ts: gmail_hits[k]=ts

    gmail_baseline=None
    if creds:
        try: gmail_baseline=gmail_first_msg_ms(creds)
        except Exception as e: print("gmail baseline failed:", e)

    def ms_to_iso(ms): 
        return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def ms_to_pretty(ms):
        dt = datetime.fromtimestamp(ms/1000, tz=timezone.utc)
        try: return dt.strftime("%B %-d, %Y")
        except Exception: return dt.strftime("%B %d, %Y")

    def launch_date(platform):
        ld = CURVES.get(platform,{}).get("launch_date")
        if ld: return datetime.strptime(ld,"%Y-%m-%d").replace(tzinfo=timezone.utc)
        tl=parse_timeline(platform); return tl[0][0] if tl else None

    def month_diff(a, b): return (b.year-a.year)*12 + (b.month-a.month)

    resolved={}
    for k in platform_keys:
        m_ms=manual.get(k); g_ms=gmail_hits.get(k)
        if m_ms and g_ms:
            m_d=datetime.fromtimestamp(m_ms/1000,tz=timezone.utc).date()
            g_d=datetime.fromtimestamp(g_ms/1000,tz=timezone.utc).date()
            if abs((m_d.year*12+m_d.month)-(g_d.year*12+g_d.month)) < 12:
                resolved[k]={"date_ms":g_ms,"verified":True,"email_hint_ms":None}
            else:
                resolved[k]={"date_ms":m_ms,"verified":False,"email_hint_ms":g_ms}
        elif g_ms:
            resolved[k]={"date_ms":g_ms,"verified":True,"email_hint_ms":None}
        elif m_ms:
            resolved[k]={"date_ms":m_ms,"verified":False,"email_hint_ms":None}

    platforms=[]; all_pcts=[]; v_pcts=[]
    if gmail_baseline:
        add_card(platforms, all_pcts, v_pcts, "gmail", gmail_baseline, True, None)

    for k,info in resolved.items():
        if k not in CURVES: continue
        add_card(platforms, all_pcts, v_pcts, k, info["date_ms"], info["verified"], info.get("email_hint_ms"))

    score_all = round(100*(sum([p/100.0 for p in all_pcts])/len(all_pcts)),1) if all_pcts else None
    score_verified = round(100*(sum([p/100.0 for p in v_pcts])/len(v_pcts)),1) if v_pcts else None

    platforms=[p for p in platforms if p.get("join_iso")]

    labels=[p["name"] for p in platforms]
    values=[p["percentile"] for p in platforms]
    verified_flags=[bool(p.get("verified")) for p in platforms]
    logos_map={p["name"]: p.get("logo_url","") for p in platforms}

    uid=session.get("uid") or str(uuid.uuid4()); session["uid"]=uid
    try: save_results(uid, platforms, score_all, score_verified)
    except Exception as e: print("save_results failed:", e)

    return render_template("results.html",
        platforms=platforms, score=score_all, score_verified=score_verified,
        chart_labels=labels, chart_values=values, chart_verified=verified_flags, chart_logos=logos_map
    )

def add_card(platforms, all_pcts, v_pcts, key, ts, verified, email_hint_ms):
    def parse_timeline(platform):
        tl = sorted([(datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=timezone.utc), int(u)) for d,u in CURVES.get(platform,{}).get("timeline",[])], key=lambda x:x[0])
        return tl
    def launch_date(platform):
        ld = CURVES.get(platform,{}).get("launch_date")
        if ld: return datetime.strptime(ld,"%Y-%m-%d").replace(tzinfo=timezone.utc)
        tl=parse_timeline(platform); return tl[0][0] if tl else None
    def users_at(platform, when_ms):
        tl=parse_timeline(platform)
        if not tl: return None
        when=datetime.fromtimestamp(when_ms/1000, tz=timezone.utc)
        if when<=tl[0][0]: return tl[0][1]
        if when>=tl[-1][0]: return tl[-1][1]
        for (d0,u0),(d1,u1) in zip(tl, tl[1:]):
            if d0<=when<=d1:
                span=(d1-d0).total_seconds()
                frac=(when-d0).total_seconds()/span if span else 0
                return round(u0 + frac*(u1-u0))
        return tl[-1][1]
    def users_today(platform):
        tl=parse_timeline(platform); return tl[-1][1] if tl else None
    def timeline_series(platform):
        tl=parse_timeline(platform)
        if not tl: return {"points_m":[]}
        pts=[{"x":d.strftime("%Y-%m-%d"), "y": round(u/1_000_000,3)} for d,u in tl]
        today=datetime.now(timezone.utc).date()
        if tl[-1][0].date()<today:
            pts.append({"x": today.strftime("%Y-%m-%d"), "y": round(tl[-1][1]/1_000_000,3)})
        return {"points_m": pts}
    def early_pct(joined, today):
        if not joined or not today: return None
        return round(100*(joined/today),1)
    def before_pct(joined, today):
        if not joined or not today: return None
        return round(100*(1-(joined/today)),1)

    ld=launch_date(key)
    if ld and datetime.fromtimestamp(ts/1000,tz=timezone.utc) < ld:
        ts=int(ld.timestamp()*1000)
    joined_u=users_at(key, ts); today_u=users_today(key)
    if not (joined_u and today_u): return
    pct=early_pct(joined_u, today_u)
    if pct is not None:
        all_pcts.append(pct)
        if verified: v_pcts.append(pct)
    points=timeline_series(key)
    joined_dt=datetime.fromtimestamp(ts/1000,tz=timezone.utc)
    months_after=(joined_dt.year-ld.year)*12 + (joined_dt.month-ld.month) if ld else 0
    y, mm = divmod(months_after, 12)
    human = (f"{y} year{'s' if y!=1 else ''} " if y else "") + (f"{mm} month{'s' if mm!=1 else ''}" if mm else "0 months")
    card={
        "name": PRETTY_NAMES.get(key, key.title()),
        "logo_url": LOGO_URLS.get(key,""),
        "joined": ms_to_pretty(ts),
        "join_iso": ms_to_iso(ts),
        "percentile": pct,
        "narrative_percent": before_pct(joined_u, today_u),
        "narrative_after": human,
        "joined_users": joined_u, "today_users": today_u,
        "chart": points,
        "metric_tag": METRIC_TAGS.get(key,"Users"),
        "y_label": f"{METRIC_TAGS.get(key,'Users')} (Millions)",
        "verified": bool(verified)
    }
    if email_hint_ms:
        def ms_to_pretty(ms):
            dt = datetime.fromtimestamp(ms/1000, tz=timezone.utc)
            try: return dt.strftime("%B %-d, %Y")
            except Exception: return dt.strftime("%B %d, %Y")
        card["email_hint"] = f"Also found in email: {ms_to_pretty(email_hint_ms)}"
    platforms.append(card)

@app.route("/healthz")
def healthz(): return jsonify({"ok":True})
