#!/usr/bin/env python3
"""
Franklin County, Ohio - Motivated Seller Lead Scraper
========================================================

Pulls newly-recorded documents (lis pendens, foreclosures, liens, judgments,
tax deeds, probate, etc.) from the Franklin County Clerk of Courts / Recorder
public search portal, cross-references them against the County Auditor's
bulk parcel data to attach property + mailing addresses, scores each lead,
and writes the results to JSON (for the dashboard) and a GoHighLevel-ready
CSV export.

Data sources
------------
Clerk / Recorder portal (real property record search, "PublicSearch" /
Neumo platform):
    https://franklin.oh.publicsearch.us/

    IMPORTANT: This site is a JavaScript single-page application, so it is
    scraped with Playwright (a real, headless Chromium browser), not requests.
    The site's robots.txt disallows automated crawling of some deep paths
    (e.g. /search/advanced). This script therefore only ever drives the
    public "Quick Search" form on the homepage ("/"), the same form and
    workflow a human visitor uses. You are responsible for making sure your
    use of this script complies with the site's Terms of Service and any
    applicable law before running it in production / on a schedule.

    Because the results are rendered client-side, the exact CSS class names
    used by the results list can change without notice and were not
    directly inspectable while writing this script (the results page sits
    behind the JS bundle and is excluded by robots.txt from automated
    fetching). To make this resilient, `extract_results()` below uses
    several independent, low-fragility extraction strategies (ARIA roles,
    generic "result/row"-ish selectors, and full-text regex parsing) rather
    than one brittle CSS selector. If Franklin County changes their site
    layout and matches stop coming back, run once with
    `HEADLESS=false python scraper/fetch.py`, watch the browser, and update
    the `SELECTORS` dict and/or `ROW_TEXT_PATTERNS` regexes near the top of
    this file to match what you see. `--debug-dump` saves the raw rendered
    HTML of the results page to `scraper/debug/` for offline inspection.

County Auditor bulk parcel data (verified live at time of writing):
    https://apps.franklincountyauditor.com/Parcel_CSV/<year>/<month>/Parcel.csv
    (an auto-indexed IIS file listing; a new CSV is dropped in a dated
    subfolder on a rolling basis). This script auto-discovers the most
    recent file. A DBF-based loader (via `dbfread`, as originally requested)
    is also included as `load_owner_index_from_dbf()` for the GIS shapefile
    parcel attribute table (apps.franklincountyauditor.com/GIS_Shapefiles/
    CurrentExtracts), in case you prefer that source or the CSV endpoint
    moves. The CSV path is used by default since it is the documented,
    directly-supported bulk-download product ("Parcel CSV") and needs no
    shapefile unzip step.

Usage
-----
    pip install -r scraper/requirements.txt
    python -m playwright install --with-deps chromium
    python scraper/fetch.py

Environment variables (all optional):
    LOOKBACK_DAYS       default 7
    HEADLESS            default "true"
    APPRAISER_CSV_URL   override auto-discovered Parcel CSV URL
    APPRAISER_DBF_ZIP_URL  use dbfread path instead of CSV (see above)
    SKIP_APPRAISER      "true" to skip owner/address enrichment (faster testing)
"""

from __future__ import annotations

import asyncio
import csv
import functools
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
except ImportError:  # allows --appraiser-only style testing without playwright installed
    async_playwright = None
    PWTimeoutError = Exception

try:
    from dbfread import DBF
except ImportError:
    DBF = None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT_DIR / "scraper" / "debug"

CLERK_URL = "https://franklin.oh.publicsearch.us/"
APPRAISER_INDEX_URL = "https://apps.franklincountyauditor.com/Parcel_CSV/"
APPRAISER_DBF_ZIP_BASE = "https://apps.franklincountyauditor.com/GIS_Shapefiles/CurrentExtracts/"

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
HEADLESS = os.environ.get("HEADLESS", "true").strip().lower() != "false"
SKIP_APPRAISER = os.environ.get("SKIP_APPRAISER", "false").strip().lower() == "true"
DEBUG_DUMP = "--debug-dump" in sys.argv or os.environ.get("DEBUG_DUMP", "false").lower() == "true"

OUTPUT_PATHS = [
    ROOT_DIR / "dashboard" / "records.json",
    ROOT_DIR / "data" / "records.json",
]
GHL_CSV_PATH = ROOT_DIR / "data" / "ghl_export.csv"

# Date-range presets exposed by the clerk portal's Quick Search UI as of
# this writing ("Last 24 Hours", "Last 3 Days", "Last 1 Week", "Last 2
# Weeks", "Last 1 Month", "Last 3 Months", "Last 6 Months", "Last 1 Year").
# We map LOOKBACK_DAYS to the closest matching preset label so the UI
# button click matches what a human would click. If LOOKBACK_DAYS doesn't
# land on a preset, we fall back to typing an explicit from/to date range.
DATE_PRESETS = [
    (1, "Last 24 Hours"),
    (3, "Last 3 Days"),
    (7, "Last 1 Week"),
    (14, "Last 2 Weeks"),
    (30, "Last 1 Month"),
    (90, "Last 3 Months"),
    (180, "Last 6 Months"),
    (365, "Last 1 Year"),
]

# Lead type categories -> the search terms typed into the portal's single
# "Search for grantor/grantee, subdivision, doc type, or doc#" box (it
# indexes document type among other things), plus the human label and the
# scoring flag it maps to (if any).
CATEGORIES: dict[str, dict[str, Any]] = {
    "LP":        {"label": "Lis Pendens",             "terms": ["LIS PENDENS"]},
    "RELLP":     {"label": "Release of Lis Pendens",  "terms": ["RELEASE OF LIS PENDENS", "LIS PENDENS RELEASE"]},
    "NOFC":      {"label": "Notice of Foreclosure",   "terms": ["NOTICE OF FORECLOSURE", "FORECLOSURE"]},
    "TAXDEED":   {"label": "Tax Deed",                "terms": ["TAX DEED"]},
    "JUD":       {"label": "Judgment",                "terms": ["JUDGMENT"]},
    "CCJ":       {"label": "Certified Judgment",      "terms": ["CERTIFICATE OF JUDGMENT", "CERTIFIED JUDGMENT"]},
    "DRJUD":     {"label": "Domestic Judgment",       "terms": ["DOMESTIC JUDGMENT"]},
    "LNCORPTX":  {"label": "Corp Tax Lien",           "terms": ["CORPORATE FRANCHISE TAX LIEN", "CORP TAX LIEN"]},
    "LNIRS":     {"label": "IRS Lien",                "terms": ["IRS LIEN", "INTERNAL REVENUE SERVICE LIEN"]},
    "LNFED":     {"label": "Federal Lien",            "terms": ["FEDERAL TAX LIEN", "FEDERAL LIEN"]},
    "LNMECH":    {"label": "Mechanic Lien",           "terms": ["MECHANICS LIEN", "MECHANIC'S LIEN"]},
    "LNHOA":     {"label": "HOA Lien",                "terms": ["HOA LIEN", "ASSESSMENT LIEN", "CONDO ASSESSMENT LIEN"]},
    "MEDLN":     {"label": "Medicaid Lien",           "terms": ["MEDICAID LIEN"]},
    "LN":        {"label": "Lien",                    "terms": ["LIEN"]},  # broad catch-all, run last
    "PRO":       {"label": "Probate",                 "terms": ["PROBATE", "CERTIFICATE OF TRANSFER"]},
    "NOC":       {"label": "Notice of Commencement",  "terms": ["NOTICE OF COMMENCEMENT"]},
}

# Category groups -> scoring flag label. Order matters (checked top-down).
FLAG_RULES: list[tuple[set[str], str]] = [
    ({"LP"}, "Lis pendens"),
    ({"NOFC"}, "Pre-foreclosure"),
    ({"JUD", "CCJ", "DRJUD"}, "Judgment lien"),
    ({"TAXDEED", "LNCORPTX", "LNIRS", "LNFED"}, "Tax lien"),
    ({"LNMECH"}, "Mechanic lien"),
    ({"PRO"}, "Probate / estate"),
]

CORP_OWNER_RE = re.compile(
    r"\b(LLC|L\.L\.C\.?|INC|INCORPORATED|CORP|CORPORATION|LTD|L\.P\.?|LP\b|TRUST|TRUSTEE"
    r"|HOLDINGS|PROPERTIES|ENTERPRISES|GROUP|COMPANY|CO\.|PARTNERS|VENTURES|REALTY|CAPITAL)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fc_lead_scraper")


# --------------------------------------------------------------------------
# Retry helper (sync + async, 3 attempts by default). Never lets an
# exception from a single source escape and kill the whole run.
# --------------------------------------------------------------------------

def retry(attempts: int = 3, delay: float = 2.0, backoff: float = 2.0, exceptions=(Exception,)):
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                cur_delay = delay
                for attempt in range(1, attempts + 1):
                    try:
                        return await fn(*args, **kwargs)
                    except exceptions as exc:
                        log.warning("%s failed (attempt %d/%d): %s", fn.__name__, attempt, attempts, exc)
                        if attempt == attempts:
                            log.error("%s giving up after %d attempts", fn.__name__, attempts)
                            return None
                        await asyncio.sleep(cur_delay)
                        cur_delay *= backoff
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                cur_delay = delay
                for attempt in range(1, attempts + 1):
                    try:
                        return fn(*args, **kwargs)
                    except exceptions as exc:
                        log.warning("%s failed (attempt %d/%d): %s", fn.__name__, attempt, attempts, exc)
                        if attempt == attempts:
                            log.error("%s giving up after %d attempts", fn.__name__, attempts)
                            return None
                        time.sleep(cur_delay)
                        cur_delay *= backoff
            return sync_wrapper
    return decorator


def safe(default=None):
    """Decorator: swallow any exception from a per-record processing step
    and log it, so one bad record never crashes the whole batch."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                log.warning("safe(%s) suppressed error: %s", fn.__name__, exc)
                return default
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Appraiser bulk parcel data -> owner / address index
# --------------------------------------------------------------------------

# Candidate column names per the spec; matched case-insensitively against
# whatever headers the live file actually has.
COLUMN_CANDIDATES = {
    "owner": ["OWNER", "OWN1", "OWNERNAME", "OWNER_NAME", "OWNER1"],
    "site_addr": ["SITE_ADDR", "SITEADDR", "SITE_ADDRESS", "PROPERTY_ADDRESS", "LOCATION"],
    "site_city": ["SITE_CITY", "SITECITY", "PROPERTY_CITY"],
    "site_zip": ["SITE_ZIP", "SITEZIP", "PROPERTY_ZIP"],
    "mail_addr": ["ADDR_1", "MAILADR1", "MAIL_ADDR1", "MAILING_ADDRESS", "MAIL_ADDRESS"],
    "mail_city": ["CITY", "MAILCITY", "MAIL_CITY"],
    "mail_state": ["STATE", "MAILSTATE", "MAIL_STATE"],
    "mail_zip": ["ZIP", "MAILZIP", "MAIL_ZIP"],
    "parcel_id": ["PARCELID", "PARCEL_ID", "PARID", "PIN"],
}


def _find_column(fieldnames: list[str], candidates: list[str]) -> Optional[str]:
    upper_map = {f.strip().upper(): f for f in fieldnames if f}
    for cand in candidates:
        if cand in upper_map:
            return upper_map[cand]
    # substring fallback
    for cand in candidates:
        for upper, original in upper_map.items():
            if cand in upper:
                return original
    return None


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.upper().strip()
    name = re.sub(r"[^\w\s,&/-]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def name_variants(raw_name: str) -> set[str]:
    """Generate 'FIRST LAST', 'LAST FIRST', and 'LAST, FIRST' style
    variants for matching between the clerk's grantor field and the
    auditor's owner field, whose formats commonly differ."""
    variants: set[str] = set()
    if not raw_name:
        return variants
    n = normalize_name(raw_name)
    if not n:
        return variants
    variants.add(n)

    if "," in n:
        last, _, rest = n.partition(",")
        last = last.strip()
        rest = rest.strip()
        if last and rest:
            first = rest.split()[0] if rest.split() else ""
            variants.add(f"{last}, {rest}")
            variants.add(f"{last} {rest}")
            if first:
                variants.add(f"{first} {last}")
    else:
        parts = n.split()
        if len(parts) >= 2 and not CORP_OWNER_RE.search(n):
            first, last = parts[0], parts[-1]
            variants.add(f"{first} {last}")
            variants.add(f"{last} {first}")
            variants.add(f"{last}, {first}")
            variants.add(f"{last} {' '.join(parts[:-1])}")  # LAST FIRST MI...
        else:
            variants.add(n)
    return {v for v in variants if v}


@retry(attempts=3, delay=3)
def _discover_latest_parcel_csv_url() -> str:
    """Walk the Auditor's public IIS directory listing to find the newest
    Parcel.csv (structure verified live: .../Parcel_CSV/<year>/<month>/Parcel.csv)."""
    resp = requests.get(APPRAISER_INDEX_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    years = sorted(
        (a.text.strip().rstrip("/") for a in soup.find_all("a") if re.fullmatch(r"\d{4}/?", a.text.strip())),
        reverse=True,
    )
    if not years:
        raise RuntimeError("Could not find any year directories in Parcel_CSV listing")

    for year in years:
        year_url = f"{APPRAISER_INDEX_URL}{year}/"
        r2 = requests.get(year_url, timeout=30)
        if not r2.ok:
            continue
        soup2 = BeautifulSoup(r2.text, "lxml")
        months = sorted(
            (a.text.strip().rstrip("/") for a in soup2.find_all("a") if re.fullmatch(r"\d{1,2}/?", a.text.strip())),
            key=lambda m: int(m),
            reverse=True,
        )
        for month in months:
            month_url = f"{year_url}{month}/"
            r3 = requests.get(month_url, timeout=30)
            if not r3.ok:
                continue
            soup3 = BeautifulSoup(r3.text, "lxml")
            for a in soup3.find_all("a"):
                if a.text.strip().lower() == "parcel.csv":
                    return f"{month_url}Parcel.csv"
    raise RuntimeError("Could not locate Parcel.csv in any year/month subfolder")


@retry(attempts=3, delay=3)
def _download(url: str) -> bytes:
    resp = requests.get(url, timeout=180, stream=True)
    resp.raise_for_status()
    return resp.content


def build_owner_index_from_csv(csv_bytes: bytes) -> dict[str, dict]:
    text = csv_bytes.decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    cols = {key: _find_column(fieldnames, cands) for key, cands in COLUMN_CANDIDATES.items()}
    log.info("Parcel CSV column mapping: %s", cols)

    index: dict[str, dict] = {}
    count = 0
    for row in reader:
        owner_col = cols.get("owner")
        if not owner_col:
            continue
        owner_raw = (row.get(owner_col) or "").strip()
        if not owner_raw:
            continue
        parcel = {
            "owner_raw": owner_raw,
            "site_address": (row.get(cols.get("site_addr") or "", "") or "").strip(),
            "site_city": (row.get(cols.get("site_city") or "", "") or "").strip(),
            "site_state": "OH",
            "site_zip": (row.get(cols.get("site_zip") or "", "") or "").strip(),
            "mail_address": (row.get(cols.get("mail_addr") or "", "") or "").strip(),
            "mail_city": (row.get(cols.get("mail_city") or "", "") or "").strip(),
            "mail_state": (row.get(cols.get("mail_state") or "", "") or "OH").strip() or "OH",
            "mail_zip": (row.get(cols.get("mail_zip") or "", "") or "").strip(),
            "parcel_id": (row.get(cols.get("parcel_id") or "", "") or "").strip(),
        }
        for variant in name_variants(owner_raw):
            index.setdefault(variant, parcel)
        count += 1
    log.info("Indexed %d parcel owner records (%d name-variant keys)", count, len(index))
    return index


def build_owner_index_from_dbf(dbf_zip_url: str) -> dict[str, dict]:
    """Alternate loader using dbfread against the parcel attribute table
    bundled inside the Auditor's GIS shapefile package, as originally
    requested. Used when APPRAISER_DBF_ZIP_URL is set, or as a fallback."""
    if DBF is None:
        raise RuntimeError("dbfread is not installed; run `pip install dbfread`")

    raw = _download(dbf_zip_url)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
        if not dbf_names:
            raise RuntimeError("No .dbf file found inside downloaded shapefile zip")
        dbf_name = dbf_names[0]
        tmp_path = ROOT_DIR / "scraper" / "_tmp_parcels.dbf"
        tmp_path.write_bytes(zf.read(dbf_name))

    try:
        table = DBF(str(tmp_path), load=False, ignore_missing_memofile=True, encoding="latin-1")
        fieldnames = table.field_names
        cols = {key: _find_column(fieldnames, cands) for key, cands in COLUMN_CANDIDATES.items()}
        log.info("Parcel DBF column mapping: %s", cols)

        index: dict[str, dict] = {}
        count = 0
        for row in table:
            owner_col = cols.get("owner")
            if not owner_col:
                continue
            owner_raw = str(row.get(owner_col) or "").strip()
            if not owner_raw:
                continue
            parcel = {
                "owner_raw": owner_raw,
                "site_address": str(row.get(cols.get("site_addr") or "", "") or "").strip(),
                "site_city": str(row.get(cols.get("site_city") or "", "") or "").strip(),
                "site_state": "OH",
                "site_zip": str(row.get(cols.get("site_zip") or "", "") or "").strip(),
                "mail_address": str(row.get(cols.get("mail_addr") or "", "") or "").strip(),
                "mail_city": str(row.get(cols.get("mail_city") or "", "") or "").strip(),
                "mail_state": str(row.get(cols.get("mail_state") or "", "") or "OH").strip() or "OH",
                "mail_zip": str(row.get(cols.get("mail_zip") or "", "") or "").strip(),
                "parcel_id": str(row.get(cols.get("parcel_id") or "", "") or "").strip(),
            }
            for variant in name_variants(owner_raw):
                index.setdefault(variant, parcel)
            count += 1
        log.info("Indexed %d parcel owner records from DBF (%d name-variant keys)", count, len(index))
        return index
    finally:
        tmp_path.unlink(missing_ok=True)


def load_owner_index() -> dict[str, dict]:
    if SKIP_APPRAISER:
        log.info("SKIP_APPRAISER=true, skipping owner/address enrichment")
        return {}

    dbf_zip_override = os.environ.get("APPRAISER_DBF_ZIP_URL")
    if dbf_zip_override:
        try:
            return build_owner_index_from_dbf(dbf_zip_override) or {}
        except Exception as exc:  # noqa: BLE001
            log.error("DBF owner index load failed (%s); falling back to CSV", exc)

    try:
        csv_url = os.environ.get("APPRAISER_CSV_URL") or _discover_latest_parcel_csv_url()
        log.info("Using Parcel CSV: %s", csv_url)
        raw = _download(csv_url)
        if raw:
            return build_owner_index_from_csv(raw) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("CSV owner index load failed: %s", exc)

    log.warning("Proceeding with no owner/address enrichment (appraiser data unavailable)")
    return {}


def lookup_parcel(owner_index: dict[str, dict], grantor_name: str) -> Optional[dict]:
    if not owner_index or not grantor_name:
        return None
    for variant in name_variants(grantor_name):
        hit = owner_index.get(variant)
        if hit:
            return hit
    return None


# --------------------------------------------------------------------------
# Clerk portal scraping (Playwright, async)
# --------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
DOC_NUM_RE = re.compile(r"\b(?:Doc(?:ument)?\.?\s*#?\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-]{4,24})\b", re.IGNORECASE)
GRANTOR_RE = re.compile(r"Grantors?\s*[:\-]\s*([^\n|]+)", re.IGNORECASE)
GRANTEE_RE = re.compile(r"Grantees?\s*[:\-]\s*([^\n|]+)", re.IGNORECASE)
LEGAL_RE = re.compile(r"(?:Legal(?: Description)?)\s*[:\-]\s*([^\n|]+)", re.IGNORECASE)

# Generic selector strategies tried in order. Update these first if
# extraction stops finding results after a site redesign.
RESULT_CONTAINER_SELECTORS = [
    "[data-testid*='result' i]",
    "[class*='result' i][class*='row' i]",
    "[class*='SearchResult' i]",
    "[role='row']",
    "table tbody tr",
    "ul[class*='result' i] li",
    "div[class*='result' i] > div",
]


def _parse_amount(text: str) -> Optional[float]:
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_date(text: str) -> Optional[str]:
    m = DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _row_to_record(text: str, href: Optional[str], base_url: str, category_code: str, category_label: str) -> dict:
    text = re.sub(r"\s+", " ", text).strip()
    doc_num_match = DOC_NUM_RE.search(text)
    grantor_match = GRANTOR_RE.search(text)
    grantee_match = GRANTEE_RE.search(text)
    legal_match = LEGAL_RE.search(text)

    doc_num = doc_num_match.group(1).strip() if doc_num_match else None
    if not doc_num:
        # fall back to a stable hash so we can still dedupe/track the record
        doc_num = "TMP-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]

    url = href
    if url and not url.startswith("http"):
        url = base_url.rstrip("/") + "/" + url.lstrip("/")

    return {
        "doc_num": doc_num,
        "doc_type": category_label,
        "category": category_code,
        "filed": _parse_date(text),
        "grantor": (grantor_match.group(1).strip() if grantor_match else None),
        "grantee": (grantee_match.group(1).strip() if grantee_match else None),
        "legal": (legal_match.group(1).strip() if legal_match else None),
        "amount": _parse_amount(text),
        "clerk_url": url or base_url,
        "raw_text": text[:500],
    }


async def _select_date_range(page, lookback_days: int) -> None:
    preset_label = None
    for days, label in DATE_PRESETS:
        if days == lookback_days:
            preset_label = label
            break
    try:
        # Open the "Recorded Date" range control shown on the homepage.
        await page.get_by_text("Recorded Date", exact=False).first.click(timeout=5000)
        if preset_label:
            await page.get_by_text(preset_label, exact=True).first.click(timeout=5000)
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("Preset date-range selection unavailable (%s); trying explicit from/to fields", exc)

    # Fallback: explicit From / To date inputs.
    try:
        today = datetime.now()
        start = today - timedelta(days=lookback_days)
        date_inputs = await page.query_selector_all("input[type='date'], input[placeholder*='date' i]")
        if len(date_inputs) >= 2:
            await date_inputs[0].fill(start.strftime("%m/%d/%Y"))
            await date_inputs[1].fill(today.strftime("%m/%d/%Y"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not set explicit date range either: %s", exc)


async def extract_results(page, category_code: str, category_label: str, base_url: str) -> list[dict]:
    records: list[dict] = []
    seen_hrefs: set[str] = set()

    for selector in RESULT_CONTAINER_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        if not elements:
            continue

        for el in elements:
            try:
                text = await el.inner_text()
            except Exception:
                continue
            if not text or len(text.strip()) < 5:
                continue

            href = None
            try:
                link = await el.query_selector("a[href]")
                if link:
                    href = await link.get_attribute("href")
            except Exception:
                pass

            key = href or text[:120]
            if key in seen_hrefs:
                continue
            seen_hrefs.add(key)

            records.append(_row_to_record(text, href, base_url, category_code, category_label))

        if records:
            # First selector strategy that produced anything wins; avoids
            # double-counting the same results via a second, broader selector.
            break

    return records


@retry(attempts=3, delay=4, exceptions=(Exception,))
async def scrape_category(page, category_code: str, cfg: dict, lookback_days: int, base_url: str) -> list[dict]:
    all_records: list[dict] = []
    for term in cfg["terms"]:
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
            search_box = page.get_by_placeholder(re.compile("search for grantor", re.IGNORECASE))
            await search_box.first.fill(term, timeout=10000)

            await _select_date_range(page, lookback_days)

            search_button = page.get_by_role("button", name=re.compile("search", re.IGNORECASE))
            await search_button.first.click(timeout=10000)

            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeoutError:
                pass
            await page.wait_for_timeout(1500)  # let client-side render finish

            if DEBUG_DUMP:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                safe_term = re.sub(r"[^A-Za-z0-9]+", "_", term)
                html = await page.content()
                (DEBUG_DIR / f"{category_code}_{safe_term}.html").write_text(html, encoding="utf-8")
                await page.screenshot(path=str(DEBUG_DIR / f"{category_code}_{safe_term}.png"), full_page=True)

            no_results = await page.get_by_text(re.compile("no documents|no results", re.IGNORECASE)).count()
            if no_results:
                log.info("[%s] '%s': no results", category_code, term)
                continue

            term_records = await extract_results(page, category_code, cfg["label"], base_url)
            log.info("[%s] '%s': %d row(s) extracted", category_code, term, len(term_records))
            all_records.extend(term_records)

        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] search term '%s' failed: %s", category_code, term, exc)
            continue

    return all_records


# --------------------------------------------------------------------------
# Flags + scoring
# --------------------------------------------------------------------------

def derive_flags(category_codes: set[str], owner_name: Optional[str], is_new_this_week: bool) -> list[str]:
    flags: list[str] = []
    for codes, flag_label in FLAG_RULES:
        if category_codes & codes:
            flags.append(flag_label)
    if owner_name and CORP_OWNER_RE.search(owner_name):
        flags.append("LLC / corp owner")
    if is_new_this_week:
        flags.append("New this week")
    return flags


def compute_score(flags: list[str], category_codes: set[str], amount: Optional[float],
                   is_new_this_week: bool, has_address: bool) -> int:
    score = 30
    score += 10 * len(flags)

    if {"Lis pendens"} & set(flags) and {"Pre-foreclosure"} & set(flags):
        score += 20

    if amount:
        if amount > 100_000:
            score += 15
        elif amount > 50_000:
            score += 10

    if is_new_this_week:
        score += 5

    if has_address:
        score += 5

    return max(0, min(100, score))


# --------------------------------------------------------------------------
# Record assembly
# --------------------------------------------------------------------------

@safe(default=None)
def build_final_record(doc_num: str, hit: dict, owner_index: dict, lookback_days: int) -> Optional[dict]:
    cats = hit["cats"]
    primary_cat = sorted(cats)[0]
    label = CATEGORIES.get(primary_cat, {}).get("label", primary_cat)

    grantor = hit.get("grantor") or ""
    parcel = lookup_parcel(owner_index, grantor)

    filed = hit.get("filed")
    is_new_this_week = False
    if filed:
        try:
            filed_dt = datetime.strptime(filed, "%Y-%m-%d")
            is_new_this_week = (datetime.now() - filed_dt).days <= lookback_days
        except ValueError:
            pass
    else:
        # If we couldn't parse a filed date, assume it's in-range since it
        # only surfaced because it matched the portal's own date filter.
        is_new_this_week = True

    has_address = bool(parcel and (parcel.get("site_address") or parcel.get("mail_address")))

    flags = derive_flags(cats, grantor, is_new_this_week)
    score = compute_score(flags, cats, hit.get("amount"), is_new_this_week, has_address)

    return {
        "doc_num": doc_num,
        "doc_type": hit.get("doc_type") or label,
        "filed": filed,
        "cat": primary_cat,
        "cat_label": label,
        "owner": grantor or None,
        "grantee": hit.get("grantee") or None,
        "amount": hit.get("amount"),
        "legal": hit.get("legal") or None,
        "prop_address": parcel.get("site_address") if parcel else None,
        "prop_city": parcel.get("site_city") if parcel else None,
        "prop_state": parcel.get("site_state") if parcel else "OH",
        "prop_zip": parcel.get("site_zip") if parcel else None,
        "mail_address": parcel.get("mail_address") if parcel else None,
        "mail_city": parcel.get("mail_city") if parcel else None,
        "mail_state": parcel.get("mail_state") if parcel else None,
        "mail_zip": parcel.get("mail_zip") if parcel else None,
        "clerk_url": hit.get("clerk_url"),
        "flags": flags,
        "score": score,
    }


# --------------------------------------------------------------------------
# Output writers
# --------------------------------------------------------------------------

def write_json_outputs(records: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": "Franklin County, Ohio Clerk of Courts / Recorder (franklin.oh.publicsearch.us)",
        "date_range": {
            "from": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
            "to": now.strftime("%Y-%m-%d"),
            "lookback_days": LOOKBACK_DAYS,
        },
        "total": len(records),
        "with_address": sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
        "records": records,
    }

    for path in OUTPUT_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            log.info("Wrote %d records -> %s", len(records), path)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed writing %s: %s", path, exc)


def _split_owner_name(owner: str) -> tuple[str, str]:
    owner = (owner or "").strip()
    if not owner:
        return "", ""
    if CORP_OWNER_RE.search(owner):
        return "", owner
    if "," in owner:
        last, _, rest = owner.partition(",")
        first = rest.strip().split()[0] if rest.strip() else ""
        return first, last.strip()
    parts = owner.split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])


def export_ghl_csv(records: list[dict], path: Path) -> None:
    fieldnames = [
        "First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags", "Source", "Public Records URL",
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                first, last = _split_owner_name(r.get("owner") or "")
                writer.writerow({
                    "First Name": first,
                    "Last Name": last,
                    "Mailing Address": r.get("mail_address") or "",
                    "Mailing City": r.get("mail_city") or "",
                    "Mailing State": r.get("mail_state") or "",
                    "Mailing Zip": r.get("mail_zip") or "",
                    "Property Address": r.get("prop_address") or "",
                    "Property City": r.get("prop_city") or "",
                    "Property State": r.get("prop_state") or "",
                    "Property Zip": r.get("prop_zip") or "",
                    "Lead Type": r.get("cat_label") or r.get("cat") or "",
                    "Document Type": r.get("doc_type") or "",
                    "Date Filed": r.get("filed") or "",
                    "Document Number": r.get("doc_num") or "",
                    "Amount/Debt Owed": r.get("amount") if r.get("amount") is not None else "",
                    "Seller Score": r.get("score", ""),
                    "Motivated Seller Flags": "; ".join(r.get("flags") or []),
                    "Source": "Franklin County, OH Clerk of Courts / Recorder",
                    "Public Records URL": r.get("clerk_url") or "",
                })
        log.info("Wrote GHL export CSV -> %s (%d rows)", path, len(records))
    except Exception as exc:  # noqa: BLE001
        log.error("Failed writing GHL CSV: %s", exc)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def run_clerk_scrape() -> dict[str, dict]:
    """Returns hits keyed by doc_num, each with a merged 'cats' set covering
    every search term/category that surfaced that document."""
    hits: dict[str, dict] = {}

    if async_playwright is None:
        log.error("playwright is not installed; skipping clerk portal scrape. "
                   "Run: pip install playwright && python -m playwright install chromium")
        return hits

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        for category_code, cfg in CATEGORIES.items():
            log.info("=== Category %s (%s) ===", category_code, cfg["label"])
            raw_records = await scrape_category(page, category_code, cfg, LOOKBACK_DAYS, CLERK_URL)
            raw_records = raw_records or []
            for rec in raw_records:
                doc_num = rec["doc_num"]
                existing = hits.get(doc_num)
                if existing:
                    existing["cats"].add(category_code)
                    for k, v in rec.items():
                        if k in ("cats",):
                            continue
                        if not existing.get(k) and v:
                            existing[k] = v
                else:
                    rec = dict(rec)
                    rec["cats"] = {category_code}
                    hits[doc_num] = rec

        await context.close()
        await browser.close()

    return hits


async def main() -> None:
    log.info("Franklin County lead scraper starting | lookback_days=%d headless=%s", LOOKBACK_DAYS, HEADLESS)

    owner_index = load_owner_index()
    hits = await run_clerk_scrape()
    log.info("Total unique documents found across all categories: %d", len(hits))

    final_records: list[dict] = []
    for doc_num, hit in hits.items():
        rec = build_final_record(doc_num, hit, owner_index, LOOKBACK_DAYS)
        if rec:
            final_records.append(rec)

    final_records.sort(key=lambda r: r.get("score", 0), reverse=True)

    write_json_outputs(final_records)
    export_ghl_csv(final_records, GHL_CSV_PATH)

    log.info("Done. %d leads written, %d with an address on file.",
              len(final_records), sum(1 for r in final_records if r.get("prop_address") or r.get("mail_address")))


if __name__ == "__main__":
    asyncio.run(main())
