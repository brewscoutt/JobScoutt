# Task: identify ATS platforms + config for target orgs

I'm building a job-posting monitor (`job_hunt/job_hunt.py`) that polls each
target org's applicant-tracking-system (ATS) API directly and alerts me to
new postings matching keywords like "threat intelligence," "OSINT,"
"disinformation," "trust & safety," "policy analyst," etc. It currently
supports three ATS backends: Greenhouse, Lever, and Workday. For each org
below, I need you to figure out which ATS they use and the exact parameters
needed to query it directly (no API key required for any of these three —
all public endpoints).

## Target orgs to research

Atlantic Council / DFRLab, Carnegie Endowment for International Peace, CSIS,
Brookings Institution, Microsoft (MTAC or MSTIC — look for MTAC-relevant reqs, but
Microsoft's ATS is fine to identify generally), Mandiant / Google TAG or GTIG (may be
under the general Google careers ATS), NewsGuard, Logically, Alethea, Nisos,
Reset.tech, ISD (Institute for Strategic Dialogue), ASPI (Australian
Strategic Policy Institute), Bellingcat, ProPublica.

## For each org, do this

1. Find their public careers page. Look at the page source, network requests,
   or any embedded iframe/application links to identify the ATS platform.
2. Identify which of these three it is, and extract the specific identifier:
   - **Greenhouse** → the board `token` (slug). Careers page or job links look
     like `boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>`.
     Verify with:
     `curl -s https://boards-api.greenhouse.io/v1/boards/<token>/jobs`
     (200 + a `jobs` array = confirmed; 404 = wrong token or not on Greenhouse)
   - **Lever** → the `token` (slug). Job links look like
     `jobs.lever.co/<token>`. Verify with:
     `curl -s https://api.lever.co/v0/postings/<token>?mode=json`
     (200 + a JSON array = confirmed)
   - **Workday** → three parts from the URL
     `https://<tenant>.<host>.myworkdayjobs.com/<site>`:
     `host` (the `wdN` subdomain segment, e.g. `wd1`, `wd3`, `wd5`), `tenant`
     (company slug), `site` (career-site name, often `External_Career_Site`
     or `Careers`). Verify with:
     ```
     curl -s -X POST "https://<tenant>.<host>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs" \
       -H "Content-Type: application/json" \
       -H "User-Agent: Mozilla/5.0 (compatible; job-monitor/1.0)" \
       -d '{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}'
     ```
     (200 + a `total`/`jobPostings` structure = confirmed). Note: some Workday
     tenants reject `limit` values above ~20 with an opaque 400 — if you hit a
     400, retry with a smaller limit before concluding it's misconfigured.
   - **Something else entirely** (iCIMS, SmartRecruiters, Ashby, Workable,
     BambooHR, a custom homegrown board, etc.) → don't try to force-fit it.
     Just tell me the actual platform name and, if you can find one, whether
     it has a public JSON endpoint at all.
3. Actually run the verification `curl` command yourself and confirm it
   returns real data — don't report a token you found by inspection without
   confirming the live API call works.
4. If the org currently has zero open positions, that's fine — note it as
   "confirmed, 0 current postings" rather than treating it as a failure, as
   long as the API call itself succeeds (200, not 404/400).

## Output format

Save your findings as a file (I'll wire it into `job_hunt.py` myself). For
each org, give me:

1. A ready-to-paste Python dict matching this project's `ORGS` list format:
   ```python
   {"name": "Bellingcat", "ats": "greenhouse", "token": "bellingcat"},
   # or for Workday:
   {"name": "RAND", "ats": "workday", "host": "wd5", "tenant": "rand", "site": "External_Career_Site"},
   ```
2. One line noting how you verified it (the curl command's result — status
   code and posting count), so I don't have to redo the legwork.
3. For orgs on an unsupported ATS: the platform name, the careers page URL,
   and whether it looks like it has any programmatically-queryable endpoint
   (even if `job_hunt.py` doesn't support it yet — I may add support later).

Group the output into two sections: "Ready to add" (confirmed Greenhouse/
Lever/Workday configs) and "Needs a new fetcher or unsupported" (everything
else), so I can see at a glance what's immediately usable.
