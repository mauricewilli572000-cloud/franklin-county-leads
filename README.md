# Franklin County, Ohio — Motivated Seller Lead Scraper

Automated pipeline that pulls newly-recorded documents (lis pendens,
foreclosures, tax deeds, judgments, liens, probate, etc.) from the Franklin
County Clerk of Courts / Recorder public search portal, cross-references
them against the County Auditor's bulk parcel data for owner/property/
mailing addresses, scores each lead, and publishes a dashboard + a
GoHighLevel-ready CSV export.

## Quick start

```bash
cd scraper
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python fetch.py
```

Outputs:
- `dashboard/records.json` and `data/records.json` — lead data
- `data/ghl_export.csv` — GoHighLevel import file
- `dashboard/index.html` — open directly in a browser to review leads, or let GitHub Pages serve it

## Data sources (verified live)

**Clerk / Recorder portal:** `https://franklin.oh.publicsearch.us/`
This is a JavaScript single-page app, so it's driven with Playwright
(a real headless browser), not `requests`. The scraper only automates the
public "Quick Search" form on the homepage — the same form and workflow a
human visitor uses.

**County Auditor bulk parcel data:** `https://apps.franklincountyauditor.com/Parcel_CSV/<year>/<month>/Parcel.csv`
`scraper/fetch.py` auto-discovers the newest file by walking the Auditor's
public directory listing. A `dbfread`-based loader is also included
(`build_owner_index_from_dbf`) for the parcel attribute table bundled in the
Auditor's GIS shapefile package, in case you prefer that source — set
`APPRAISER_DBF_ZIP_URL` to use it instead of the CSV.

## Important: one thing you should verify before relying on this in production

The clerk portal renders its search results client-side, and the results
page itself sits behind a `robots.txt` disallow rule, which meant it
couldn't be directly inspected while building this script. `extract_results()`
in `scraper/fetch.py` therefore uses several independent, low-fragility
strategies (ARIA roles, generic result/row selectors, regex parsing of row
text) instead of one brittle CSS selector, and everything is wrapped so a
layout change degrades gracefully (fewer fields populated) rather than
crashing.

**Before your first scheduled run**, do one local test run with the browser
visible and dump the rendered HTML so you can confirm (and tighten, if you
want) the extraction:

```bash
HEADLESS=false python scraper/fetch.py --debug-dump
```

This saves the rendered HTML/screenshots for each search to
`scraper/debug/`. If a category comes back with zero results but you can
see matching documents on the live site, open the matching `.html` file and
adjust `RESULT_CONTAINER_SELECTORS` / the regexes near the top of the file
(`AMOUNT_RE`, `DATE_RE`, `DOC_NUM_RE`, `GRANTOR_RE`, `GRANTEE_RE`) to match
what you see.

## Compliance note

Automating a government records portal is generally fine for personal /
research use, but you're responsible for reviewing `franklin.oh.publicsearch.us`'s
Terms of Service and robots.txt, and Franklin County Auditor's data-use
terms, before running this on a recurring schedule or at scale, and for
respecting any rate limits. This script does not attempt to work around any
paywall, login wall, or CAPTCHA — if the portal ever requires one, it will
simply fail that search term and move on (see "never crash on bad records"
in `fetch.py`).

## Seller score (0–100)

- Base: 30
- +10 per flag (`Lis pendens`, `Pre-foreclosure`, `Judgment lien`, `Tax lien`,
  `Mechanic lien`, `Probate / estate`, `LLC / corp owner`, `New this week`)
- +20 if both `Lis pendens` and `Pre-foreclosure` are present
- +15 if amount > $100k, else +10 if amount > $50k
- +5 if filed within the lookback window ("new this week")
- +5 if a mailing or property address was matched from parcel data
- Clamped to 0–100

## File structure

```
scraper/
  fetch.py           # main scraper — clerk portal, appraiser data, scoring, output
  requirements.txt
  debug/             # created by --debug-dump, gitignored
dashboard/
  index.html         # lead review dashboard (reads records.json)
  records.json       # latest scrape output (committed by CI)
data/
  records.json       # duplicate of the above for programmatic consumers
  ghl_export.csv      # GoHighLevel import (created on each run)
.github/workflows/
  scrape.yml         # nightly cron (07:00 UTC) + manual trigger, commits results, deploys dashboard/ to Pages
```

## GitHub Actions setup

1. Push this repo to GitHub.
2. Settings → Pages → Source → "GitHub Actions".
3. Settings → Actions → General → Workflow permissions → "Read and write permissions" (needed for the commit-back step).
4. The workflow runs nightly at 07:00 UTC and on manual dispatch (Actions tab → "Franklin County Lead Scraper" → "Run workflow").
