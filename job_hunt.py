#!/usr/bin/env python3
"""
job_monitor.py — passive monitor for job openings at a curated list of orgs.

Ships configured as a working example for threat intel / influence-ops /
policy roles — replace orgs.yaml and the INCLUDE/EXCLUDE/SEMANTIC_TARGETS
below with your own targets and interest area; nothing else needs to change.

Polls a curated list of high-value target (HVT) orgs — orgs.yaml, sidecar
file next to this script — via their Greenhouse/Lever/Workday APIs. A
posting is a MATCH if its title clears the INCLUDE/EXCLUDE keyword filter
(and, if set, LOCATIONS) — that's the gate, since these orgs are already
curated and a hard keyword miss on a known-good org is worse than an
occasional false positive.

If VOYAGE_API_KEY is set, every match is additionally annotated with a
semantic relevance score (embedding similarity against SEMANTIC_TARGETS) so
matches can be ranked highest-relevance-first — this is prioritization on
top of the keyword gate, not a replacement for it: a bad or noisy score
never excludes a keyword match. (An earlier version tried scoring-as-gate
with no keyword filter at all; real data showed it doesn't separate signal
from noise reliably enough on title text alone — a textbook match like
"Distinguished Chair for Taiwan Policy" scored below "Fraud Analyst". Rank,
don't gate, until embedding input includes real descriptions.)

Dedupes against a local state file, writes a markdown digest for reading and
a JSONL log for later trend analysis (hiring velocity per org, keyword
frequency over time), and reports new roles. Stdlib only (Voyage is called
via urllib, no SDK). Designed to run on a schedule (cron / GH Actions).

Env vars:
    VOYAGE_API_KEY — https://www.voyageai.com/ (optional — enables ranking)
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS — optional email delivery

Usage:
    python3 job_monitor.py                # normal run
    python3 job_monitor.py --dry-run      # show matches without saving state
    python3 job_monitor.py --reset        # clear seen-state (re-surfaces everything)
    python3 job_monitor.py --test-email   # send one synthetic test email, nothing else
"""

import calendar
import hashlib
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# ORGS — curated HVT list. Edit orgs.yaml (sidecar file, next to this
# script), not this section.
# ---------------------------------------------------------------------------

def _parse_orgs_yaml(text):
    """Minimal parser for orgs.yaml: a flat list of string-keyed mappings.

    Handles only what that file actually uses — `- key: value` list items,
    continuation `key: value` lines, and full-line/trailing '#' comments.
    No nesting, no multiline strings, no type coercion beyond quote-stripping.
    If orgs.yaml ever needs more than that, switch to PyYAML instead of
    extending this.
    """
    orgs = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line
        for i, ch in enumerate(line):
            if ch == "#" and (i == 0 or line[i - 1].isspace()):
                line = line[:i]
                break
        line = line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current:
                orgs.append(current)
            current = {}
            stripped = stripped[2:]
        if ":" in stripped and current is not None:
            key, _, value = stripped.partition(":")
            current[key.strip()] = value.strip().strip("'\"")
    if current:
        orgs.append(current)
    return orgs


def load_orgs(path):
    if not path.exists():
        print(f"  ! orgs file not found: {path}", file=sys.stderr)
        return []
    return _parse_orgs_yaml(path.read_text())


ORGS_PATH = Path(__file__).parent / "orgs.yaml"
ORGS = load_orgs(ORGS_PATH)

# EXAMPLE VALUES — this INCLUDE/EXCLUDE/SEMANTIC_TARGETS block below is a
# working demo (threat intel / disinformation / policy roles) so the template
# runs out of the box against orgs.yaml's example orgs. Replace all of it
# with terms matching whatever YOU'RE targeting before relying on this.

# A role is a MATCH if its title contains ANY include term...
INCLUDE = [
    "influence operation", "coordinated inauthentic", "information operation",
    "disinformation", "threat intel", "threat analyst", "trust and safety",
    "trust & safety", "osint", "open source", "investigat", "network analysis",
    "intelligence analyst", "researcher", "policy",
]

# ...and does NOT contain any exclude term (kills sales/eng/recruiting noise).
EXCLUDE = [
    "sales", "account executive", "recruiter", "sdr",
    "intern", "marketing", "designer", "accountant",
]

# Optional location filter. Leave empty to accept all locations.
# Terms are matched case-insensitively against the posting's location string.
LOCATIONS = []  # e.g. ["remote", "seattle", "washington, dc", "united states"]

# ---------------------------------------------------------------------------
# SEMANTIC CONFIG — optional relevance ranking on top of the keyword-matched
# results above. Never excludes a match; only orders the output.
# ---------------------------------------------------------------------------

# EXAMPLE VALUES — replace with sentences describing what YOU'RE targeting.
# Reference descriptions of the KIND of role you want. Each match is scored
# against ALL of these; its best cosine similarity becomes its rank score.
# Write these as full sentences, not keywords — the embedding model works on
# meaning, not substrings.
SEMANTIC_TARGETS = [
    "Investigates and analyzes coordinated inauthentic behavior, disinformation, "
    "and influence operations online.",
    "Threat intelligence analyst tracking malicious actors, campaigns, or "
    "adversary networks.",
    "Open-source intelligence (OSINT) research into extremism, propaganda, or "
    "information manipulation.",
    "Trust and safety policy work addressing platform abuse, harmful content, "
    "or integrity issues.",
    "Geopolitical or national security research and policy analysis role.",
    "Network analysis or investigative research into bad-actor networks and "
    "attribution.",
]

VOYAGE_MODEL = "voyage-3-lite"

# ---------------------------------------------------------------------------
# SHARED CONFIG
#
# All four paths below can be overridden by an env var (e.g. for GH Actions,
# where the runner's home dir doesn't persist between runs — the workflow
# points these at repo-relative paths instead and commits them back after
# each run). Locally, they default to your home dir as before. Setting an
# env var to an empty string disables that file (matches the None-to-disable
# behavior of DIGEST_PATH/LOG_PATH).
# ---------------------------------------------------------------------------

def _path_from_env(env_var, default, disableable=False):
    val = os.environ.get(env_var)
    if val is None:
        return default
    if disableable and val == "":
        return None
    return Path(val)


# Cache for SEMANTIC_TARGETS embeddings (avoids re-embedding the fixed target
# list every run — only new matches need fresh embeddings each time).
TARGET_EMBED_CACHE_PATH = _path_from_env(
    "JOB_MONITOR_TARGET_EMBED_CACHE_PATH", Path.home() / ".job_monitor_target_embeddings.json"
)

# Where to write the rolling markdown digest. Point this at your Obsidian vault
# to have new roles land as a note. Set to None (or the env var to "") to disable.
DIGEST_PATH = _path_from_env(
    "JOB_MONITOR_DIGEST_PATH", Path.home() / "job-monitor-digest.md", disableable=True
)
# e.g. Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/<Vault>/Inbox/Job Openings.md"

# Entries older than this are dropped from DIGEST_PATH on every write, so the
# file (and its git history, for the GH Actions copy) doesn't grow forever.
# The JSONL log has no such cutoff — it's the permanent record for trend
# analysis; the digest is just a recent-reading view.
DIGEST_RETENTION_MONTHS = 6

# Structured, append-only log of every match ever surfaced — one JSON object
# per line. This is the substrate for trend analysis later (hiring velocity
# per org, keyword frequency over time, etc.) that the markdown digest can't
# support since it's meant for reading, not querying. Set to None (or the env
# var to "") to disable.
LOG_PATH = _path_from_env(
    "JOB_MONITOR_LOG_PATH", Path.home() / "job-monitor-log.jsonl", disableable=True
)

STATE_PATH = _path_from_env(
    "JOB_MONITOR_STATE_PATH", Path.home() / ".job_monitor_state.json"
)

# Optional email. Leave EMAIL_TO empty to disable. Creds come from env vars
# so you never commit them: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS.
EMAIL_TO = ""        # e.g. "you@example.com"
EMAIL_FROM = ""      # defaults to SMTP_USER if blank

# ---------------------------------------------------------------------------
# Engine — org fetchers
# ---------------------------------------------------------------------------

# A generic-looking UA — Workday's WAF (and possibly others) blocks obvious
# bot/script UAs like "job-monitor/1.0" with a 400, even though the request
# itself is otherwise valid.
UA = "Mozilla/5.0 (compatible; job-monitor/1.0)"


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _get_json(url)
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "updated": j.get("updated_at", ""),
        })
    return out


def fetch_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = _get_json(url)
    out = []
    for j in data:
        cats = j.get("categories", {}) or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "updated": str(j.get("createdAt", "")),
        })
    return out


def fetch_workday(host, tenant, site):
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    offset = 0
    limit = 20  # Workday rejects larger page sizes with a 400 on some tenants
    total = None
    while total is None or offset < min(total, 500):
        req = urllib.request.Request(
            url,
            data=json.dumps({
                "appliedFacets": {}, "limit": limit, "offset": offset, "searchText": "",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            out.append({
                "id": path,
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": f"{base}/{site}{path}",
                "updated": "",
            })
        offset += limit
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "workday": fetch_workday}


def matches(job):
    title = job["title"].lower()
    if not any(term in title for term in INCLUDE):
        return False
    if any(term in title for term in EXCLUDE):
        return False
    if LOCATIONS:
        loc = job["location"].lower()
        if not any(term in loc for term in LOCATIONS):
            return False
    return True


# ---------------------------------------------------------------------------
# Engine — semantic ranking (optional, additive)
# ---------------------------------------------------------------------------

def embed_texts(texts):
    """Call Voyage AI's embeddings endpoint. Returns None if no API key set."""
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return None
    req = urllib.request.Request(
        "https://api.voyageai.com/v1/embeddings",
        data=json.dumps({"input": texts, "model": VOYAGE_MODEL}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [d["embedding"] for d in data["data"]]


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def get_target_embeddings():
    """Embeddings for SEMANTIC_TARGETS, cached until the target list/model changes.
    Returns None if VOYAGE_API_KEY isn't set — callers must treat that as
    "ranking unavailable", not an error.
    """
    cache_key = hashlib.sha256(
        (VOYAGE_MODEL + "|" + "|".join(SEMANTIC_TARGETS)).encode("utf-8")
    ).hexdigest()
    if TARGET_EMBED_CACHE_PATH.exists():
        cached = json.loads(TARGET_EMBED_CACHE_PATH.read_text())
        if cached.get("key") == cache_key:
            return cached["embeddings"]
    embeddings = embed_texts(SEMANTIC_TARGETS)
    if embeddings is None:
        return None
    TARGET_EMBED_CACHE_PATH.write_text(json.dumps({"key": cache_key, "embeddings": embeddings}))
    return embeddings


def collect(state, target_embeddings):
    """Return list of new keyword-matching jobs across all curated orgs,
    each annotated with a semantic `score` for ranking if target_embeddings
    is available. The keyword filter is the gate; the score never excludes.
    """
    new = []
    for org in ORGS:
        fetcher = FETCHERS.get(org["ats"])
        if not fetcher:
            print(f"  ! unknown ats '{org['ats']}' for {org['name']}", file=sys.stderr)
            continue
        kwargs = {k: v for k, v in org.items() if k not in ("name", "ats")}
        try:
            jobs = fetcher(**kwargs)
        except urllib.error.HTTPError as e:
            print(f"  ! {org['name']}: HTTP {e.code} (check token)", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ! {org['name']}: {e}", file=sys.stderr)
            continue

        seen_for_org = set(state["seen"].get(org["name"], []))
        org_matches = [j for j in jobs if matches(j)]
        fresh = []
        for j in org_matches:
            j["org"] = org["name"]
            j["tier"] = "org"
            if j["id"] not in seen_for_org:
                fresh.append(j)

        new.extend(fresh)
        print(f"  · {org['name']}: {len(jobs)} postings, "
              f"{len(org_matches)} relevant, {len(fresh)} new")

    if target_embeddings is not None and new:
        try:
            embeddings = embed_texts([f"{j['title']} at {j['org']}" for j in new])
        except Exception as e:
            print(f"  ! semantic ranking failed: {e}", file=sys.stderr)
            embeddings = None
        if embeddings:
            for j, emb in zip(new, embeddings):
                j["score"] = round(max(cosine_similarity(emb, t) for t in target_embeddings), 3)

    return new


# ---------------------------------------------------------------------------
# State, digest, email
# ---------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def mark_seen(state, jobs):
    for j in jobs:
        state["seen"].setdefault(j["org"], [])
        if j["id"] not in state["seen"][j["org"]]:
            state["seen"][j["org"]].append(j["id"])


def render_markdown(jobs):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n## New roles — {ts}\n"]
    for j in jobs:
        loc = f" — {j['location']}" if j["location"] else ""
        score = f" (semantic {j['score']})" if "score" in j else ""
        lines.append(f"- **{j['org']}**: [{j['title']}]({j['url']}){loc}{score}")
    return "\n".join(lines) + "\n"


def _months_before(dt, months):
    total_month_index = dt.month - 1 - months
    year = dt.year + total_month_index // 12
    month = total_month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


DIGEST_ENTRY_MARKER = "## New roles — "


def prune_digest(now=None):
    """Drop digest entries older than DIGEST_RETENTION_MONTHS. Malformed
    entries (marker present but date unparseable) are kept rather than
    dropped, so a format change never silently loses history.
    """
    if not DIGEST_PATH or not DIGEST_PATH.exists():
        return
    cutoff = _months_before(now or datetime.now(timezone.utc), DIGEST_RETENTION_MONTHS)

    text = DIGEST_PATH.read_text(encoding="utf-8")
    parts = text.split(DIGEST_ENTRY_MARKER)
    preamble, entries = parts[0], parts[1:]

    kept = []
    for entry in entries:
        try:
            entry_date = datetime.strptime(entry[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            kept.append(entry)
            continue
        if entry_date >= cutoff:
            kept.append(entry)

    if len(kept) == len(entries):
        return  # nothing to prune

    new_text = preamble + "".join(DIGEST_ENTRY_MARKER + e for e in kept)
    DIGEST_PATH.write_text(new_text, encoding="utf-8")
    print(f"  → digest pruned: {len(entries) - len(kept)} entr(y/ies) older than "
          f"{DIGEST_RETENTION_MONTHS} months removed")


def write_digest(jobs):
    if not DIGEST_PATH:
        return
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    prune_digest()
    with open(DIGEST_PATH, "a", encoding="utf-8") as f:
        f.write(render_markdown(jobs))
    print(f"  → digest appended: {DIGEST_PATH}")


def log_matches(jobs):
    if not LOG_PATH:
        return
    ts = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for j in jobs:
            record = {
                "logged_at": ts,
                "posting_id": j["id"],
                "org": j["org"],
                "title": j["title"],
                "location": j.get("location", ""),
                "url": j.get("url", ""),
                "posting_updated": j.get("updated", ""),
                "score": j.get("score"),
            }
            f.write(json.dumps(record) + "\n")
    print(f"  → log appended: {LOG_PATH} ({len(jobs)} record(s))")


def send_email(jobs):
    if not EMAIL_TO:
        return
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not (host and user and pw):
        print("  ! email skipped: SMTP_HOST/USER/PASS not set", file=sys.stderr)
        return
    msg = EmailMessage()
    msg["Subject"] = f"{len(jobs)} new role(s) in your field"
    msg["From"] = EMAIL_FROM or user
    msg["To"] = EMAIL_TO
    body = "\n".join(
        f"{j['org']}: {j['title']} ({j['location']})\n{j['url']}\n" for j in jobs
    )
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ctx)
        s.login(user, pw)
        s.send_message(msg)
    print(f"  → emailed {EMAIL_TO}")


def main():
    dry_run = "--dry-run" in sys.argv
    if "--reset" in sys.argv:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        print("State reset.")
        return

    if "--test-email" in sys.argv:
        send_email([{
            "org": "TEST", "title": "job_hunt SMTP wiring test — safe to ignore",
            "location": "nowhere", "url": "https://example.com/test",
        }])
        return

    print(f"job_monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    state = load_state()

    target_embeddings = get_target_embeddings()
    if target_embeddings is None:
        print("  ! VOYAGE_API_KEY not set — matches will be unranked", file=sys.stderr)

    new = collect(state, target_embeddings)
    new.sort(key=lambda j: j["score"] if j.get("score") is not None else -1, reverse=True)

    if not new:
        print("No new matching roles.")
        return

    print(f"\n{len(new)} new matching role(s):\n")
    for j in new:
        loc = f" — {j['location']}" if j["location"] else ""
        score = f" [semantic {j['score']}]" if "score" in j else ""
        print(f"  [{j['org']}] {j['title']}{loc}{score}\n      {j['url']}")

    if dry_run:
        print("\n(dry run — state not saved, no notifications sent)")
        return

    write_digest(new)
    log_matches(new)
    send_email(new)
    mark_seen(state, new)
    save_state(state)


if __name__ == "__main__":
    main()
