#!/usr/bin/env python3
"""Scrape every Bellhaven Senior Living community from the public website.

Source: https://analyst-assessment-production.up.railway.app
  - Listing: /communities?page=N  ("Page X of Y · N communities listed")
  - Detail:  /communities/<slug>  (name, address, care offerings, admin, phone)

Stdlib only (urllib + html.parser) so it reruns anywhere.
Writes:
  - data/locations.json          clean dataset (array of objects)
  - data/raw/communities_pN.html raw listing pages
  - data/raw/details/<slug>.html raw detail pages
"""

import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://analyst-assessment-production.up.railway.app"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DETAILS_RAW = RAW / "details"
OUT = ROOT / "data" / "locations.json"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "bellhaven-scraper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


class ListingParser(HTMLParser):
    """Collect detail-page links and visible page text from a listing page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text_chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if re.fullmatch(r"/communities/[a-z0-9\-]+", href):
                self.links.append(href)

    def handle_data(self, data):
        self.text_chunks.append(data)


class DetailParser(HTMLParser):
    """Parse a community detail page: h1 name + dl.detail dt/dd pairs.

    For each dt label we keep the dd text (with <br> as newline) and any
    <span class="badge"> values separately.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.name = None
        self.fields = {}   # dt label -> dd text
        self.badges = {}   # dt label -> [badge texts]
        self._in_h1 = False
        self._in_dt = False
        self._dt_buf = ""
        self._label = None
        self._in_dd = False
        self._dd_buf = ""
        self._in_badge = False
        self._badge_buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self._in_h1 = True
        elif tag == "dt":
            self._in_dt = True
            self._dt_buf = ""
        elif tag == "dd":
            self._in_dd = True
            self._dd_buf = ""
            if self._label is not None:
                self.badges.setdefault(self._label, [])
        elif tag == "br" and self._in_dd:
            self._dd_buf += "\n"
        elif tag == "span" and self._in_dd and dict(attrs).get("class") == "badge":
            self._in_badge = True
            self._badge_buf = ""

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        elif tag == "dt":
            self._in_dt = False
            self._label = self._dt_buf.strip() or None
        elif tag == "dd":
            self._in_dd = False
            if self._label is not None:
                self.fields[self._label] = self._dd_buf.strip()
        elif tag == "span" and self._in_badge:
            self._in_badge = False
            if self._label is not None:
                self.badges[self._label].append(self._badge_buf.strip())

    def handle_data(self, data):
        if self._in_h1 and self.name is None:
            self.name = data.strip()
        elif self._in_badge:
            self._badge_buf += data
        elif self._in_dt:
            self._dt_buf += data
        elif self._in_dd:
            self._dd_buf += data


CITY_STATE_ZIP = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)?\s*$"
)


def parse_detail(html: str, url: str, slug: str) -> dict:
    p = DetailParser()
    p.feed(html)

    address_raw = p.fields.get("Address", "")
    street, city, state, zip_ = "", "", "", ""
    if address_raw:
        lines = [ln.strip() for ln in address_raw.split("\n") if ln.strip()]
        if len(lines) >= 2:
            street = lines[0]
            m = CITY_STATE_ZIP.match(lines[-1])
            if m:
                city, state = m.group("city"), m.group("state")
                zip_ = m.group("zip") or ""
            else:
                city = lines[-1]
        elif lines:
            street = lines[0]

    return {
        "name": p.name or "",
        "slug": slug,
        "street_address": street,
        "city": city,
        "state": state,
        "zip": zip_,
        "address_raw": re.sub(r"\s*\n\s*", " | ", address_raw),
        "care_offerings": p.badges.get("Care Offerings", []),
        "administrator": p.fields.get("Administrator", ""),
        "phone": p.fields.get("Phone", ""),
        "source_url": url,
    }


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    DETAILS_RAW.mkdir(parents=True, exist_ok=True)

    # 1) Walk the paginated listing; learn total pages + claimed count from the pager text.
    detail_paths = []
    claimed_count = None
    total_pages = 1
    page = 1
    while page <= total_pages:
        url = f"{BASE}/communities" if page == 1 else f"{BASE}/communities?page={page}"
        html = fetch(url)
        (RAW / f"communities_p{page}.html").write_text(html, encoding="utf-8")
        lp = ListingParser()
        lp.feed(html)
        text = " ".join(" ".join(lp.text_chunks).split())
        m = re.search(r"Page (\d+) of (\d+) · (\d+) communities listed", text)
        if m:
            total_pages = int(m.group(2))
            claimed_count = int(m.group(3))
        for link in lp.links:
            if link not in detail_paths:
                detail_paths.append(link)
        page += 1
        time.sleep(0.2)

    print(f"Listing: {total_pages} pages, site claims {claimed_count} communities, "
          f"found {len(detail_paths)} unique detail links")

    # 2) Fetch + parse every detail page.
    records = []
    for path in detail_paths:
        slug = path.rsplit("/", 1)[-1]
        url = BASE + path
        html = fetch(url)
        (DETAILS_RAW / f"{slug}.html").write_text(html, encoding="utf-8")
        records.append(parse_detail(html, url, slug))
        time.sleep(0.2)

    records.sort(key=lambda r: r["name"].lower())
    OUT.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 3) Summary + anomalies.
    print(f"Wrote {len(records)} records -> {OUT}")
    if claimed_count is not None and len(records) != claimed_count:
        print(f"WARNING: scraped {len(records)} != site-claimed {claimed_count}")
    for r in records:
        issues = []
        if not r["zip"]:
            issues.append("no zip")
        if not r["street_address"]:
            issues.append("no street address")
        if not r["city"]:
            issues.append("no city")
        if not r["care_offerings"]:
            issues.append("no care offerings")
        if not r["phone"]:
            issues.append("no phone")
        if not r["administrator"]:
            issues.append("no administrator")
        if issues:
            print(f"ANOMALY {r['name']}: {', '.join(issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
