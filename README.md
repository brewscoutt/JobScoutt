# JobScoutt

[![JobScoutt](https://github.com/brewscoutt/JobScoutt/actions/workflows/job_hunt.yml/badge.svg)](https://github.com/brewscoutt/JobScoutt/actions/workflows/job_hunt.yml)

A passive monitor for job openings at a curated list of orgs. Polls each
org's applicant-tracking system (ATS) directly, filters by keyword,
optionally ranks matches by semantic relevance, and reports what's new —
automatically, on a schedule, via GitHub Actions. No server, no local
machine that needs to stay on, no paid infrastructure.

Ships pre-configured as a **working example** for threat-intel/policy roles
at two real companies, so you can see it work before customizing anything.
Swap in your own targets and interest area — nothing else needs to change.

## Quick start

1. Click **Use this template** (top of this repo's GitHub page) → create
   your own repo from it. (Or clone it directly if you'd rather not use a
   separate GitHub account/repo.)
2. Clone your new repo locally.
3. Get a free API key at https://dashboard.voyageai.com/ (enables semantic
   ranking — optional, but recommended).
4. In your new repo: `gh secret set VOYAGE_API_KEY` (paste the key when
   prompted), or set it via **Settings → Secrets and variables → Actions**
   in the GitHub web UI.
5. Trigger a run to confirm it works:
   `gh workflow run job_hunt.yml` (or **Actions** tab → *Run workflow*).
6. Check `data/digest.md` in your repo a minute later — you should see 1-2
   real matches from the example orgs (Recorded Future, RAND).
7. Now customize: edit `orgs.yaml` with your own targets, and
   `INCLUDE`/`EXCLUDE`/`SEMANTIC_TARGETS` in `job_hunt.py` with your own
   interest area (see below for both).

That's it — it now runs on its own every ~4 hours.

## How it works

1. **`orgs.yaml`** lists your high-value targets (HVTs) — the specific orgs
   you already know are worth watching. Each entry names an org and its ATS
   (Greenhouse, Lever, or Workday) plus whatever identifier that ATS needs.
2. **`job_hunt.py`** polls each org's public ATS API directly (no scraping,
   no API keys required for this part) and pulls every current posting.
3. **Keyword filter (the gate).** A posting only gets reported if its title
   contains an `INCLUDE` term and no `EXCLUDE` term (and, if `LOCATIONS` is
   set, matches a location). This is a hard filter — miss it, and the
   posting is never surfaced, no matter how relevant it actually is.
4. **Semantic ranking (optional, additive).** If `VOYAGE_API_KEY` is set,
   every match that passed the keyword gate also gets a relevance score
   (embedding similarity against `SEMANTIC_TARGETS`, via Voyage AI) and the
   final output is sorted highest-relevance-first. A low score never
   excludes a match — it only affects where it sits in the list.
5. Results are deduped against a state file (so you only ever see a posting
   once), appended to a markdown digest and a JSONL log, optionally
   emailed, and printed to the console.

**Why keyword-gate + semantic-rank, and not semantic-only filtering:** an
earlier version tried scoring every posting purely by embedding similarity
with no keyword gate at all. On real data, it didn't work — a genuinely
on-target policy role at one target org scored *below* an unrelated "Fraud
Analyst" posting at another. Scoring bare titles (plus org name) isn't
enough signal to reliably separate real matches from noise. Keyword
matching on a curated org list is precise enough to trust as the gate;
semantic scoring is used only to prioritize attention across whatever the
gate lets through.

## Configuration

### `orgs.yaml`

Add or edit target orgs here — not in `job_hunt.py`. Each entry:

```yaml
- name: Recorded Future
  ats: greenhouse
  token: recordedfuture

- name: RAND
  ats: workday
  host: wd5
  tenant: rand
  site: External_Career_Site
```

Supported `ats` values and how to find their identifiers (all visible in
the org's careers page URL — see comments at the top of `orgs.yaml`):

- **greenhouse** → `token` (board slug, e.g. `boards.greenhouse.io/<token>`)
- **lever** → `token` (board slug, e.g. `jobs.lever.co/<token>`)
- **workday** → `host` (the `wdN` subdomain segment), `tenant`, `site`

After adding an org, verify it resolves with `python3 job_hunt.py --dry-run`
— an HTTP error means the token/ats is wrong, or the org isn't on that
platform. Not every org will be on one of these three ATSs — many
companies (especially larger ones) run iCIMS, SilkRoad, ApplicantPro,
Recruitee, Personio, or a fully custom homegrown system. If you need one of
those, see `ORG_RESEARCH_PROMPT.md` below.

`orgs.yaml` is parsed by a small stdlib-only parser in `job_hunt.py`, not
PyYAML — it only supports this flat shape (a list of string-keyed mappings,
with full-line and trailing `#` comments). Don't add nesting or multiline
values.

### Keyword and semantic config (in `job_hunt.py`)

- `INCLUDE` / `EXCLUDE` — the keyword gate. This is the whole ballgame:
  tune these to match how the orgs you're watching actually title their
  postings. Expect to revisit this as you notice real matches slipping
  through (add a term) or too much noise getting in (add an exclude term)
  — different orgs title similar roles very differently, so this is an
  ongoing loop, not a one-time tune.
- `LOCATIONS` — optional hard geographic filter, empty by default (accepts
  all locations).
- `SEMANTIC_TARGETS` — full-sentence descriptions of the kind of role
  you're after. Used only for ranking, not filtering.

### Environment variables

| Var | Required? | Purpose |
|---|---|---|
| `VOYAGE_API_KEY` | Optional | Enables semantic ranking. Without it, matches are still found and reported — just in original fetch order, unranked. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` | Optional | Enables email delivery of new matches. Only used if `EMAIL_TO` is also set in `job_hunt.py`. |
| `JOB_MONITOR_STATE_PATH` / `JOB_MONITOR_DIGEST_PATH` / `JOB_MONITOR_LOG_PATH` / `JOB_MONITOR_TARGET_EMBED_CACHE_PATH` | Optional | Override where each data file lives. Default to `~/...` for local runs; the GH Actions workflow overrides them to repo-relative `data/...` paths (see below). Setting `JOB_MONITOR_DIGEST_PATH` or `JOB_MONITOR_LOG_PATH` to an empty string disables that file. |

### Email (optional)

To get emailed when new matches are found:

1. Set `EMAIL_TO` in `job_hunt.py` to your address.
2. Get SMTP credentials. For Gmail: `smtp.gmail.com`, port `587`, and an
   **App Password** (not your regular password) from
   https://myaccount.google.com/apppasswords — requires 2FA enabled on
   that account.
3. `gh secret set SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS`
   (or set them in the GitHub web UI under Settings → Secrets).
4. Test it without waiting for a real match:
   `gh workflow run job_hunt.yml -f test_email=true` — sends one synthetic
   email and exits, no state touched.

## Running locally

No external dependencies — stdlib only. Requires Python 3.

```bash
python3 job_hunt.py --dry-run       # show matches without saving state or notifying
```

For a run with your API key, copy `.env.example` to `.env`, fill in real
values, and use the wrapper script (sources `.env` automatically):

```bash
cp .env.example .env    # then edit .env with real values
./run.sh --dry-run
```

```bash
python3 job_hunt.py                # normal run — saves state, sends digest/email
python3 job_hunt.py --reset         # clear seen-state (re-surfaces everything next run)
python3 job_hunt.py --test-email    # send one synthetic test email, nothing else
```

## Running on a schedule (GitHub Actions)

The workflow (`.github/workflows/job_hunt.yml`) runs on `ubuntu-latest`
every 4 hours (`cron: "0 */4 * * *"`, plus `workflow_dispatch` for manual
triggers), independent of whether any of your own machines are on.

- **Persistence:** Actions runners don't keep disk between runs, so the
  workflow points `JOB_MONITOR_*_PATH` at repo-relative `data/` files
  instead of `~/...`, then commits and pushes any changes after each run
  under a `job_hunt bot` author. `git pull` before making local changes to
  avoid diverging from what the bot has committed.
- **Changing the interval:** edit the `cron` line in the workflow file.
  GitHub Actions cron is UTC and best-effort (can lag under load) — treat 4
  hours as approximate, not exact.

## Output

- **Console** — printed on every run, ranked by relevance if scoring is on.
- **Markdown digest** (`data/digest.md`, or `~/job-monitor-digest.md` for
  local-only runs if you override the path) — appended to on every real
  (non-dry-run) run. Entries older than `DIGEST_RETENTION_MONTHS` (6, in
  `job_hunt.py`) are pruned on every write, so this file doesn't grow
  forever. Point it at an Obsidian vault locally if you want new roles to
  land as notes.
- **JSONL log** (`data/log.jsonl`) — one JSON record per match ever
  surfaced (org, title, location, url, score, timestamps), with no
  retention cutoff — this is the permanent record, meant for later trend
  analysis (hiring velocity per org, keyword frequency over time) that the
  markdown digest can't support since it's meant for reading, not querying.
- **Email** — optional, off by default.
- **State file** (`data/state.json`) — tracks which posting IDs have
  already been reported, per org, so you never see a posting twice.

## Other files in this repo

- **`ORG_RESEARCH_PROMPT.md`** — a reusable research prompt for identifying
  a new target org's ATS platform and verifying its API endpoint. Hand it
  to an AI coding assistant (or work through it yourself) when you want to
  figure out how to add an org that isn't obviously on Greenhouse/Lever/
  Workday.
- **`.github/workflows/job_hunt.yml`** — the scheduled GitHub Actions
  workflow described above.
- **`run.sh`** — local convenience wrapper that sources `.env` before
  running `job_hunt.py`. Not used by the GitHub Actions workflow (secrets
  come from repo secrets there instead).
- **`.env.example`** — copy to `.env` (gitignored) and fill in real values
  for local runs.
- **`data/`** — committed by the GitHub Actions workflow after each run.
  Starts empty (just `.gitkeep`) until your first real run.

## Known limitations

- Only Greenhouse, Lever, and Workday are supported out of the box. Many
  orgs — especially larger/older institutions — run something else
  entirely (iCIMS, SilkRoad, ApplicantPro, a custom homegrown system).
  You'll need to write a new fetcher function for those (see
  `fetch_greenhouse`/`fetch_lever`/`fetch_workday` in `job_hunt.py` for the
  pattern to follow).
- The keyword gate is fragile per-org: a generic title (e.g. just
  "Analyst") can slip past `INCLUDE` even at an org that's squarely
  on-topic. There's no ranking signal to catch this, since scoring only
  runs on postings that already passed the gate.
- Voyage's free tier has a per-minute rate limit; a large first run (many
  orgs, many postings) may need to retry after a 429.
