# gmail_search.py
from datetime import datetime, timezone
import time
import logging

log = logging.getLogger("firstmover")

# ---- Platform boot years to shrink the binary-search window ----
SERVICE_START = {
    "facebook":   datetime(2004, 2, 1, tzinfo=timezone.utc),
    "twitter":    datetime(2006, 7, 1, tzinfo=timezone.utc),
    "instagram":  datetime(2010,10, 1, tzinfo=timezone.utc),
    "linkedin":   datetime(2003, 5, 1, tzinfo=timezone.utc),
    "dropbox":    datetime(2008, 6, 1, tzinfo=timezone.utc),
    "amazonprime":datetime(2005, 2, 1, tzinfo=timezone.utc),
    "openai":     datetime(2022,11, 1, tzinfo=timezone.utc),  # ChatGPT era
    "spotify":    datetime(2008,10, 1, tzinfo=timezone.utc),
    "reddit":     datetime(2005, 6, 1, tzinfo=timezone.utc),
}

# ---- Two-tier query map: STRICT pass first, then a BROAD fallback ----
QUERY_MAP = {
    "twitter": [
        # STRICT
        'from:(verify@twitter.com OR info@twitter.com OR hello@twitter.com OR no-reply@twitter.com OR twitter.com) subject:(welcome OR confirm OR verify)',
        # BROAD
        'from:(twitter.com OR x.com) (welcome OR confirm OR verify OR "Thanks for signing up" OR activate OR "confirm your email")',
    ],
    "linkedin": [
        'from:(linkedin.com OR messages-noreply@linkedin.com OR security-noreply@linkedin.com OR customer_service@linkedin.com OR news-noreply@linkedin.com) subject:(welcome OR confirm OR verify)',
        'from:(linkedin.com) (welcome OR confirm OR verify OR "confirm your email" OR "Thanks for joining")',
    ],
    "reddit": [
        'from:(noreply@reddit.com OR do-not-reply@reddit.com OR noreply@redditmail.com) subject:(welcome OR confirm OR verify)',
        'from:(reddit.com OR redditmail.com) (welcome OR confirm OR verify OR activate OR "confirm your email")',
    ],
    "instagram": [
        'from:(mail.instagram.com OR security@mail.instagram.com) subject:(welcome OR confirm OR verify)',
        'from:(instagram.com) (welcome OR confirm OR verify OR "confirm your email")',
    ],
    "spotify": [
        'from:(no-reply@spotify.com) subject:(welcome OR confirm OR verify)',
        'from:(spotify.com) (welcome OR confirm OR verify OR "thanks for signing up")',
    ],
    "dropbox": [
        'from:(no-reply@dropbox.com OR dropbox@mail.dropbox.com OR no-reply@dropboxmail.com) subject:(welcome OR confirm OR verify)',
        'from:(dropbox.com OR dropboxmail.com) (welcome OR confirm OR verify OR activate)',
    ],
    "amazonprime": [
        'from:(no-reply@amazon.com OR prime@amazon.com OR digital-no-reply@amazon.com OR prime-enroll@amazon.com) subject:(prime OR welcome OR confirm OR verify)',
        'from:(amazon.com) (prime OR welcome OR confirm OR verify OR "thanks for joining")',
    ],
    "openai": [
        'from:(noreply@openai.com OR team@openai.com OR no-reply@accounts.openai.com OR noreply@accounts.openai.com OR no-reply@chat.openai.com) subject:(welcome OR confirm OR verify OR "ChatGPT")',
        'from:(openai.com OR accounts.openai.com OR chat.openai.com) (welcome OR confirm OR verify OR "ChatGPT")',
    ],
    "facebook": [
        'from:(facebookmail.com OR notify@facebookmail.com) subject:(welcome OR confirm OR verify OR "Just one more step")',
        'from:(facebookmail.com OR facebook.com) ("confirm your email" OR activate)',
    ],
    # You likely already handle Gmail itself elsewhere
}

# ---- Core Gmail helpers (use your existing build_gmail in app.py) ----

def gmail_search_exists(svc, base_q: str, start_dt: datetime, end_dt: datetime):
    """
    Return (exists_bool, final_query_string). Uses a date-bounded query for speed.
    """
    # Gmail expects YYYY/MM/DD for before/after
    q = f'{base_q} after:{start_dt:%Y/%m/%d} before:{end_dt:%Y/%m/%d}'
    resp = svc.users().messages().list(
        userId="me",
        q=q,
        maxResults=1,
        includeSpamTrash=False,
        fields="messages/id,nextPageToken"
    ).execute()
    return ("messages" in resp, q)

def gmail_find_earliest_hit_ts_bisect(svc, base_q: str, start_dt: datetime, end_dt: datetime, time_budget_s: float = 1.6):
    """
    Binary search the window to find the earliest hit quickly.
    Returns ms epoch for earliest message (by internalDate), or None.
    """
    t0 = time.monotonic()
    lo, hi = start_dt, end_dt
    best_ts = None

    while (time.monotonic() - t0) < time_budget_s:
        # mid date
        mid = datetime.fromtimestamp((lo.timestamp() + hi.timestamp())/2, tz=timezone.utc)
        exists_left, q_left = gmail_search_exists(svc, base_q, lo, mid)
        if exists_left:
            # Narrow to left half
            hi = mid
            # Try to fetch exact first doc in the left window (1 page)
            resp = svc.users().messages().list(
                userId="me", q=q_left, maxResults=1, includeSpamTrash=False,
                fields="messages/id,nextPageToken"
            ).execute()
            if "messages" in resp:
                msg_id = resp["messages"][0]["id"]
                m = svc.users().messages().get(
                    userId="me", id=msg_id,
                    format="metadata",
                    metadataHeaders=["Date","From","Subject"],
                    fields="id,internalDate"
                ).execute()
                ts = int(m.get("internalDate"))
                best_ts = ts if (best_ts is None or ts < best_ts) else best_ts
        else:
            # No results on left; search right half
            lo = mid

        # Stop if window is very small, but still keep best_ts
        if (hi - lo).total_seconds() < 1:
            break

    return best_ts

def gmail_oldest_from_queries_bounded(creds, key: str, query_list, time_budget_s: float = 1.6):
    """
    Try STRICT queries first, then BROAD. Keep best earliest hit even if a call tips over budget.
    """
    # You already have a build_gmail(creds) in app.py — use that
    from app import build_gmail  # import local to avoid circular imports on boot
    svc = build_gmail(creds)

    overall_start = time.monotonic()
    overall_best = None

    start_dt = SERVICE_START.get(key, datetime(2003,1,1,tzinfo=timezone.utc))
    end_dt = datetime.now(timezone.utc)

    # Split into strict vs broad (first half strict, second half broad)
    if key in QUERY_MAP:
        queries = QUERY_MAP[key]
    else:
        queries = query_list  # fallback to whatever the caller gave us

    for pass_ix, base_q in enumerate(queries):
        remaining = time_budget_s - (time.monotonic() - overall_start)
        if remaining <= 0:
            log.info("[gmail-scan] time budget exhausted before %s pass=%d", key, pass_ix)
            break

        ts = gmail_find_earliest_hit_ts_bisect(
            svc, base_q, start_dt, end_dt, time_budget_s=remaining
        )

        # Record hit immediately even if we overrun after this
        if ts:
            if overall_best is None or ts < overall_best:
                overall_best = ts

        # Check budget AFTER the call, so we preserve hits
        if (time.monotonic() - overall_start) >= time_budget_s:
            log.info("[gmail-scan] time budget exhausted for %s (kept best=%s)", key, overall_best)
            break

    return overall_best
