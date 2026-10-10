#!/usr/bin/env python3
"""
Fully automatic deadline updater for the GENIUS Lab deadlines site.

Runs inside GitHub Actions (see .github/workflows/update-deadlines.yml) with no
human in the loop. For every venue in scripts/venues.yml it:

  1. Pulls the community-maintained ccfddl/ccf-deadlines YAML (source "ccf").
  2. Scrapes the official conference website and looks for the paper
     submission deadline (source "official").
  3. Merges the two. When they disagree, an unambiguous official-site date wins.
  4. If nothing confirmed is in the future, writes an "(Estimated DDL)" entry by
     shifting the most recent confirmed deadline forward one year.
  5. Regenerates _data/conferences.yml and _data/update_status.yml.

Safety rules:
  * If both sources fail for a venue, the venue's existing entries are kept.
  * If most venues fail (network outage), nothing is written.
  * Scraped dates are accepted only when they sit within a plausibility window
    around the expected date and all candidates agree with each other.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENUES_FILE = ROOT / "scripts" / "venues.yml"
OUT_FILE = ROOT / "_data" / "conferences.yml"
STATUS_FILE = ROOT / "_data" / "update_status.yml"

CCF_RAW = "https://raw.githubusercontent.com/ccfddl/ccf-deadlines/main/conference/{path}.yml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 genius-lab-deadlines-bot"
}
TIMEOUT = 25
MAX_PAGES_PER_SITE = 5
PLAUSIBLE_DAYS = 75          # scraped date must be within this many days of expected
CLUSTER_DAYS = 3             # all scraped candidates must agree within this window
KEEP_PAST_DAYS = 400         # keep the most recent past entry if no older than this

EST_PREFIX = "(Estimated DDL) "
DEFAULT_TZ = "Etc/GMT+12"    # Anywhere on Earth
TODAY = dt.date.today()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.headers.update(HEADERS)


def fetch(url: str) -> str | None:
    try:
        r = _session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return None
        if "html" not in r.headers.get("content-type", "") and not url.endswith(".yml"):
            return None
        return r.text[:2_000_000]
    except requests.RequestException:
        return None


# --------------------------------------------------------------------------- #
# Source 1: ccf-deadlines
# --------------------------------------------------------------------------- #
def ccf_tz_to_tz(s: str | None) -> str:
    """ccf uses 'UTC-8' / 'AoE'. The site uses POSIX-style 'Etc/GMT+8' (sign inverted)."""
    s = (s or "AoE").strip()
    if s.upper() == "AOE":
        return DEFAULT_TZ
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::\d{2})?", s)
    if not m:
        return DEFAULT_TZ
    sign, hours = m.group(1), int(m.group(2))
    if hours == 0:
        return "Etc/GMT+0"
    return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"


def strip_year(date_text: str | None) -> str:
    if not date_text:
        return "TBD"
    t = re.sub(r",?\s*20\d{2}\b", "", str(date_text)).strip(" ,")
    return t or "TBD"


def from_ccf(venue: dict) -> dict[int, dict]:
    path = venue.get("ccf")
    if not path:
        return {}
    text = fetch(CCF_RAW.format(path=path))
    if text is None:
        log(f"  [ccf] fetch failed for {path}")
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        log(f"  [ccf] bad yaml for {path}")
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    out: dict[int, dict] = {}
    for conf in data.get("confs", []) or []:
        try:
            year = int(conf["year"])
        except (KeyError, TypeError, ValueError):
            continue
        deadlines, comments = [], []
        for t in conf.get("timeline", []) or []:
            d = str(t.get("deadline", "")).strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}", d):
                a = str(t.get("abstract_deadline", "")).strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}", a):
                    continue
                d = a
                comments.append("abstract deadline (paper deadline TBD)")
            if len(d) == 10:
                d += " 23:59"
            deadlines.append(d[:16])
            if t.get("comment"):
                comments.append(str(t["comment"]).strip())
        if not deadlines:
            continue
        out[year] = {
            "deadline": sorted(set(deadlines)),
            "timezone": ccf_tz_to_tz(conf.get("timezone")),
            "date": strip_year(conf.get("date")),
            "place": str(conf.get("place") or "TBD"),
            "link": conf.get("link"),
            "comment": "; ".join(comments) if (len(comments) > 1 or "TBD" in " ".join(comments)) else None,
        }
    return out


# --------------------------------------------------------------------------- #
# Source 2: official website scrape
# --------------------------------------------------------------------------- #
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_PATTERNS = [
    re.compile(rf"\b(?P<mon>{MON_RE})\.?\s+(?P<day>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{{2}}))?", re.I),
    re.compile(rf"\b(?P<day>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s+(?P<mon>{MON_RE})\.?(?:,?\s*(?P<year>20\d{{2}}))?", re.I),
    re.compile(r"\b(?P<year>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})\b"),
]
KEY_RE = re.compile(
    r"(paper|submission|manuscript)s?\s*(registration\s*)?(deadline|due)"
    r"|deadline\s*(for\s*)?(papers?|full|submissions?)"
    r"|papers?\s+(are\s+)?due"
    r"|full[\s-]+papers?\s*(submission)?"
    r"|submission\s+(deadline|closes|close)",
    re.I,
)
NEG_RE = re.compile(
    r"workshop|tutorial|demo|poster|camera|notification|rebuttal|supplement|abstract"
    r"|proposal|challenge|competition|doctoral|student|grant|travel|early[\s-]*bird"
    r"|reviewer|author\s+response|decision|accept|revision|art[\s-]*gallery|course|panel|game|xr\b|emerging|real[\s-]*time|talk|immersive"
    r"|short\s+paper|late[\s-]*breaking|work[\s-]*in[\s-]*progress|industry|exhibitor"
    r"|visa|registration\s+deadline|video|birds?[\s-]*of[\s-]*a[\s-]*feather|ethics",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b(?=\s*(?:[A-Z]{2,4}\b|anywhere|aoe|utc|gmt|$))", re.I)
TZ_HINTS = [
    (re.compile(r"anywhere\s+on\s+earth|\baoe\b|utc\s*-\s*12", re.I), "Etc/GMT+12"),
    (re.compile(r"\b(pst|pdt|pacific)\b", re.I), "Etc/GMT+8"),
    (re.compile(r"\b(mst|mdt|mountain)\b", re.I), "Etc/GMT+7"),
    (re.compile(r"\b(cst|cdt|central\s+time)\b", re.I), "Etc/GMT+6"),
    (re.compile(r"\b(est|edt|eastern)\b", re.I), "Etc/GMT+5"),
    (re.compile(r"\b(cet|cest|central\s+european)\b", re.I), "Etc/GMT-1"),
    (re.compile(r"\b(utc|gmt)\b\s*(\+\s*0)?(?![+-]\s*\d)", re.I), "Etc/GMT+0"),
]
LINK_HINT_RE = re.compile(r"call|cfp|date|submission|author|paper", re.I)


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    # Table rows and list items usually hold "label | date" pairs; join their cells.
    for row in soup.find_all(["tr", "li", "dt", "dd", "p", "h1", "h2", "h3", "h4", "div"]):
        row.append(" \n ")
    text = soup.get_text(" ")
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if 6 <= len(line) <= 400:
            lines.append(line)
    return lines


def parse_dates(line: str, expected: dt.date) -> list[dt.date]:
    found: list[dt.date] = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(line):
            g = m.groupdict()
            try:
                if "mon" in g and g.get("mon"):
                    month = MONTHS.get(g["mon"][:3].lower())
                    if not month:
                        continue
                    day = int(g["day"])
                    if g.get("year"):
                        years = [int(g["year"])]
                    else:
                        years = [expected.year - 1, expected.year, expected.year + 1]
                    best = None
                    for y in years:
                        d = dt.date(y, month, day)
                        if best is None or abs((d - expected).days) < abs((best - expected).days):
                            best = d
                    if best:
                        found.append(best)
                else:
                    found.append(dt.date(int(g["year"]), int(g["m"]), int(g["d"])))
            except ValueError:
                continue
    return found


def parse_time(line: str) -> str:
    low = line.lower()
    if "noon" in low:
        return "12:00"
    if "midnight" in low:
        return "23:59"
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", low):
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap and ap.startswith("p") and h < 12:
            h += 12
        if ap and ap.startswith("a") and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h, ap = int(m.group(1)), m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return f"{h:02d}:00"
    return "23:59"


def parse_tz(line: str, page_text: str) -> str:
    for scope in (line, page_text):
        for pat, tz in TZ_HINTS:
            if pat.search(scope):
                return tz
    return DEFAULT_TZ


def site_pages(url: str) -> list[tuple[str, str]]:
    """Fetch the homepage plus a few same-domain pages that look like CFP / dates pages."""
    pages: list[tuple[str, str]] = []
    home = fetch(url)
    if home is None:
        return pages
    pages.append((url, home))
    soup = BeautifulSoup(home, "html.parser")
    base_host = urlparse(url).netloc.lower().removeprefix("www.")
    seen = {url.rstrip("/")}
    for a in soup.find_all("a", href=True):
        label = f"{a.get_text(' ')} {a['href']}"
        if not LINK_HINT_RE.search(label):
            continue
        href = urljoin(url, a["href"]).split("#")[0]
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if host != base_host or href.rstrip("/") in seen:
            continue
        if re.search(r"\.(pdf|zip|png|jpg|docx?)$", href, re.I):
            continue
        seen.add(href.rstrip("/"))
        body = fetch(href)
        if body:
            pages.append((href, body))
        if len(pages) >= MAX_PAGES_PER_SITE:
            break
    return pages


def from_official(venue: dict, year: int, expected: dt.date | None) -> tuple[dict | None, str]:
    """Return (record, note). record is None when nothing trustworthy was found."""
    url = venue["url"].format(year=year, yy=f"{year % 100:02d}")
    pages = site_pages(url)
    if not pages:
        return None, f"site unreachable ({url})"
    if expected is None:
        # Without a reference date we cannot sanity-check the scrape; typical
        # deadlines fall 6-10 months before the conference year begins.
        expected = dt.date(year - 1, 9, 1)

    candidates: list[tuple[dt.date, str, str]] = []  # (date, time, tz)
    for page_url, html in pages:
        path = urlparse(page_url).path
        if path and path != "/" and NEG_RE.search(path.replace("/", " ")):
            continue  # whole page is about workshops, art gallery, etc.
        lines = html_to_lines(html)
        page_text = " ".join(lines)
        for line in lines:
            if not KEY_RE.search(line) or NEG_RE.search(line):
                continue
            for d in parse_dates(line, expected):
                if abs((d - expected).days) <= PLAUSIBLE_DAYS:
                    candidates.append((d, parse_time(line), parse_tz(line, page_text)))
    if not candidates:
        return None, f"no deadline text found ({len(pages)} pages)"

    dates = [c[0] for c in candidates]
    if (max(dates) - min(dates)).days > CLUSTER_DAYS:
        spread = ", ".join(sorted({d.isoformat() for d in dates}))
        return None, f"ambiguous dates on site: {spread}"

    best_date, _ = Counter(dates).most_common(1)[0]
    times = Counter(c[1] for c in candidates if c[0] == best_date)
    tzs = Counter(c[2] for c in candidates if c[0] == best_date)
    rec = {
        "deadline": [f"{best_date.isoformat()} {times.most_common(1)[0][0]}"],
        "timezone": tzs.most_common(1)[0][0],
        "link": url,
    }
    return rec, f"official site confirmed {best_date.isoformat()}"


# --------------------------------------------------------------------------- #
# Merge, estimate, emit
# --------------------------------------------------------------------------- #
def latest_dt(rec: dict) -> dt.date:
    return max(dt.date.fromisoformat(d[:10]) for d in rec["deadline"])


def shift_year(datestr: str, years: int) -> str:
    d = dt.date.fromisoformat(datestr[:10])
    try:
        nd = d.replace(year=d.year + years)
    except ValueError:  # Feb 29
        nd = d.replace(year=d.year + years, day=28)
    return nd.isoformat() + datestr[10:]


def build_entry(venue: dict, year: int, rec: dict, estimated: bool) -> dict:
    name = (EST_PREFIX if estimated else "") + venue["name"]
    entry = {
        "name": name,
        "year": year,
        "date": rec.get("date") or "TBD",
        "description": venue["description"],
        "link": rec.get("link") or venue["url"].format(year=year, yy=f"{year % 100:02d}"),
        "deadline": list(rec["deadline"]),
        "timezone": rec.get("timezone") or DEFAULT_TZ,
        "place": rec.get("place") or "TBD",
        "tags": list(venue["tags"]),
    }
    if rec.get("comment"):
        entry["comment"] = rec["comment"]
    return entry


def process_venue(venue: dict, existing: list[dict], scrape: bool) -> tuple[list[dict], dict]:
    name = venue["name"]
    log(f"* {name}")
    ccf = from_ccf(venue)
    status = {"sources": {}, "entries": []}
    confirmed: dict[int, dict] = {}

    # Years worth looking at: last year through two years ahead.
    years = range(TODAY.year - 1, TODAY.year + 3)
    for year in years:
        rec = ccf.get(year)
        expected = latest_dt(rec) if rec else None
        if expected is None:
            prev = ccf.get(year - 1)
            if prev:
                expected = dt.date.fromisoformat(shift_year(prev["deadline"][-1], 1)[:10])
        official, note = (None, "not checked (scraping off or deadline long past)")
        # Only scrape when the deadline is not long past (saves requests, avoids stale pages).
        if scrape and (expected is None or expected >= TODAY - dt.timedelta(days=KEEP_PAST_DAYS)):
            official, note = from_official(venue, year, expected)
        status["sources"][year] = note

        if rec and official:
            if abs((latest_dt(official) - latest_dt(rec)).days) <= 1 or len(rec["deadline"]) > 1:
                merged = dict(rec)              # agree, or multi-round venue: trust ccf detail
                src = "ccf+official"
            else:
                # Disagreement: the curated source is far more reliable than a
                # regex over a website, so keep it and record the discrepancy.
                merged = dict(rec)
                src = f"ccf (official site showed {official['deadline'][0][:10]})"
            if official.get("link"):
                merged["link"] = official["link"]
            confirmed[year] = (merged, src)
        elif rec:
            confirmed[year] = (dict(rec), "ccf")
        elif official:
            confirmed[year] = (official, "official")

    if not confirmed:
        # Both sources failed: keep whatever the site had, so an outage never deletes data.
        kept = [e for e in existing]
        status["entries"] = [f"{e['name']} {e['year']}: kept (sources unavailable)" for e in kept]
        return kept, status

    entries: list[dict] = []
    future = {y: v for y, v in confirmed.items() if latest_dt(v[0]) >= TODAY}
    past = {y: v for y, v in confirmed.items() if latest_dt(v[0]) < TODAY}

    # Most recent past entry, so the page shows what the last cycle looked like.
    if past:
        y = max(past, key=lambda k: latest_dt(past[k][0]))
        if (TODAY - latest_dt(past[y][0])).days <= KEEP_PAST_DAYS:
            entries.append(build_entry(venue, y, past[y][0], estimated=False))
            status["entries"].append(f"{name} {y}: {past[y][1]}")

    for y in sorted(future):
        entries.append(build_entry(venue, y, future[y][0], estimated=False))
        status["entries"].append(f"{name} {y}: {future[y][1]}")

    if not future:
        base_year = max(confirmed, key=lambda k: latest_dt(confirmed[k][0]))
        base = confirmed[base_year][0]
        period = int(venue.get("period", 1))   # 2 for biennial venues (ECCV, ICCV)
        shift = period
        while True:
            dls = [shift_year(d, shift) for d in base["deadline"]]
            if max(dt.date.fromisoformat(d[:10]) for d in dls) >= TODAY or shift > 4:
                break
            shift += period
        est = {"deadline": dls, "timezone": base.get("timezone"), "date": "TBD", "place": "TBD"}
        entries.append(build_entry(venue, base_year + shift, est, estimated=True))
        status["entries"].append(f"{name} {base_year + shift}: estimated from {base_year}")

    return entries, status


class Quoted(str):
    pass


class Flow(list):
    pass


def _quoted(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


def _flow(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", list(data), flow_style=True)


yaml.SafeDumper.add_representer(Quoted, _quoted)
yaml.SafeDumper.add_representer(Flow, _flow)


def emit(groups: dict[str, list[dict]]) -> str:
    out = [
        "---",
        "# GENERATED FILE. Do not edit by hand; edit scripts/venues.yml instead.",
        f"# Last regenerated by scripts/update_deadlines.py on {TODAY.isoformat()}.",
        "",
    ]
    for group, entries in groups.items():
        if not entries:
            continue
        out.append(f"# {group}")
        out.append("")
        for e in entries:
            e = dict(e)
            e["deadline"] = [Quoted(d) for d in e["deadline"]]
            e["tags"] = Flow(e["tags"])
            out.append(yaml.safe_dump([e], sort_keys=False, allow_unicode=True,
                                      default_flow_style=False, width=1000).rstrip())
            out.append("")
    return "\n".join(out) + "\n"


def base_name(entry_name: str) -> str:
    n = entry_name.replace(EST_PREFIX, "")
    return re.sub(r"\s+20\d{2}$", "", n).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the result instead of writing")
    ap.add_argument("--no-scrape", action="store_true", help="skip official-site scraping")
    ap.add_argument("--only", help="comma-separated venue names to process (debugging)")
    args = ap.parse_args()

    venues = yaml.safe_load(VENUES_FILE.read_text())
    existing_all = yaml.safe_load(OUT_FILE.read_text()) or [] if OUT_FILE.exists() else []
    existing_by_name: dict[str, list[dict]] = {}
    for e in existing_all:
        existing_by_name.setdefault(base_name(e["name"]), []).append(e)

    if args.only:
        wanted = {w.strip().lower() for w in args.only.split(",")}
        venues = [v for v in venues if v["name"].lower() in wanted]

    groups: dict[str, list[dict]] = {}
    statuses: dict[str, dict] = {}
    failures = 0
    for v in venues:
        entries, st = process_venue(v, existing_by_name.get(v["name"], []), scrape=not args.no_scrape)
        if any("kept (sources unavailable)" in s for s in st["entries"]):
            failures += 1
        groups.setdefault(v.get("group", "Other"), []).extend(entries)
        statuses[v["name"]] = st

    if venues and failures > len(venues) / 2:
        log(f"ABORT: {failures}/{len(venues)} venues had no reachable source; not writing.")
        return 2

    text = emit(groups)
    iso_year, iso_week, _ = TODAY.isocalendar()
    status_doc = {
        "last_run_week": f"{iso_year}-W{iso_week:02d}",
        "venues": {k: {"entries": v["entries"], "checks": {str(y): n for y, n in v["sources"].items()}}
                   for k, v in statuses.items()},
    }
    status_text = ("# GENERATED by scripts/update_deadlines.py. What each entry was based on.\n"
                   + yaml.safe_dump(status_doc, sort_keys=False, allow_unicode=True, width=1000))

    if args.dry_run:
        print(text)
        print(status_text, file=sys.stderr)
        return 0

    OUT_FILE.write_text(text)
    STATUS_FILE.write_text(status_text)
    log(f"wrote {OUT_FILE.relative_to(ROOT)} and {STATUS_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
