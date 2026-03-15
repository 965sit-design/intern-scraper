#!/usr/bin/env python3
"""infomatin_get: collect internship postings for companies from a CSV watchlist.

- Reads companies from CSV (company_name, site_url, careers_url, notes)
- Crawls career/intern pages with keyword filtering
- Saves the latest state to CSV and labels rows as new/updated/unchanged
- Appends new/updated rows to a history CSV for manual run tracking
- Logs to file and console
Designed for manual execution from a button, terminal, or batch file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit("Missing dependency: pip install requests beautifulsoup4") from exc

FIELDS = [
    "company_name",
    "title",
    "url",
    "description",
    "intern_type",
    "location",
    "deadline",
    "published_date",
    "updated_date",
    "status",
    "last_checked",
]

DEFAULT_KEYWORDS = [
    "インターン",
    "インターンシップ",
    "intern",
    "internship",
    "1day",
    "１day",
    "オープンカンパニー",
    "open company",
    "サマーインターン",
    "ウィンターインターン",
    "長期インターン",
    "短期インターン",
]

DEFAULT_HEADERS = {
    "User-Agent": "intern-monitor/0.1 (+https://example.com)",
    "Accept-Language": "ja,en;q=0.8",
}

DATE_PATTERNS = [
    r"(?P<y>20\d{2})[./-](?P<m>\d{1,2})[./-](?P<d>\d{1,2})",
    r"(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日",
    r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日",
]

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect internship postings from company career pages.")
    parser.add_argument("--companies", default="intern/companies.csv", help="CSV with company_name,site_url,careers_url,notes")
    parser.add_argument("--out", default="intern/output/internships.csv", help="CSV to store the latest state")
    parser.add_argument("--diff-out", default="intern/output/internship_changes.csv", help="CSV for new/updated rows from the current run")
    parser.add_argument("--history-out", default="intern/output/internship_history.csv", help="CSV to append new/updated rows across manual runs")
    parser.add_argument("--keywords", default="intern/keywords.txt", help="Optional keyword list file (one per line)")
    parser.add_argument("--max-links", type=int, default=25, help="Max candidate links per company")
    parser.add_argument("--timeout", type=float, default=12.0, help="Request timeout seconds")
    parser.add_argument("--retries", type=int, default=2, help="Retry count per request")
    parser.add_argument("--log-file", default="intern/logs/intern_monitor.log", help="Log file path")
    parser.add_argument("--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--user-agent", default=DEFAULT_HEADERS["User-Agent"], help="Override User-Agent header")
    return parser.parse_args()

def setup_logging(log_path: Path, level: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )

def load_companies(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Company list not found: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = [row for row in reader if any(row.values())]
    for row in rows:
        for key in ("company_name", "site_url", "careers_url", "notes"):
            row.setdefault(key, "")
    return rows

def read_keywords(path: Optional[Path]) -> List[str]:
    if path and path.exists():
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(DEFAULT_KEYWORDS)

def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())

def fetch(session: requests.Session, url: str, timeout: float, retries: int) -> Optional[str]:
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code >= 400:
                logging.warning("HTTP %s for %s", resp.status_code, url)
                continue
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except Exception as exc:
            logging.warning("Fetch failed (%s/%s) for %s: %s", attempt + 1, retries + 1, url, exc)
    return None

def keyword_in_text(text: str, keywords: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)

def find_candidate_links(soup: BeautifulSoup, base_url: str, keywords: Sequence[str], limit: int) -> List[str]:
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        text = normalize_whitespace(anchor.get_text(" ", strip=True))
        href = anchor["href"]
        target = urljoin(base_url, href)
        if keyword_in_text(text, keywords) or keyword_in_text(href, keywords):
            if target not in links:
                links.append(target)
        if len(links) >= limit:
            break
    return links

def extract_title(soup: BeautifulSoup, keywords: Sequence[str]) -> str:
    for tag in ("h1", "h2"):
        h = soup.find(tag)
        if h:
            text = normalize_whitespace(h.get_text(" ", strip=True))
            if text:
                return text
    title = soup.title.string if soup.title else ""
    title = normalize_whitespace(title or "")
    if not title:
        return ""
    if keyword_in_text(title, keywords):
        return title
    return title

def extract_meta_content(soup: BeautifulSoup, names: Sequence[str]) -> str:
    for name in names:
        meta = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if meta and meta.get("content"):
            return normalize_whitespace(meta["content"])
    return ""

def extract_description(soup: BeautifulSoup) -> str:
    meta_desc = extract_meta_content(soup, ["description", "og:description"])
    if meta_desc:
        return meta_desc
    paragraph = soup.find("p")
    if paragraph:
        return normalize_whitespace(paragraph.get_text(" ", strip=True))
    text = soup.get_text(" ", strip=True)
    return normalize_whitespace(text[:400]) if text else ""

def parse_date_string(raw: str) -> str:
    raw = raw.strip()
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, raw)
        if not match:
            continue
        year = int(match.groupdict().get("y") or dt.date.today().year)
        month = int(match.group("m"))
        day = int(match.group("d"))
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""

def extract_dates(soup: BeautifulSoup, page_text: str) -> Tuple[str, str]:
    published = extract_meta_content(soup, ["article:published_time", "datePublished"])
    updated = extract_meta_content(soup, ["article:modified_time", "last-modified", "dateModified"])
    if published:
        published = parse_date_string(published)
    if updated:
        updated = parse_date_string(updated)
    if not published:
        published = parse_date_string(page_text)
    if not updated:
        updated = parse_date_string(page_text)
    return published, updated

def extract_deadline(text: str) -> str:
    for marker in ("締切", "締め切り", "締切り", "deadline", "応募締切"):
        pos = text.find(marker)
        if pos != -1:
            snippet = text[pos : pos + 40]
            parsed = parse_date_string(snippet)
            if parsed:
                return parsed
    return parse_date_string(text)

def extract_location(text: str) -> str:
    location_markers = ["勤務地", "location", "開催地", "place", "住所"]
    for marker in location_markers:
        if marker in text:
            snippet = text.split(marker, 1)[-1][:60]
            return normalize_whitespace(snippet)
    match = re.search(r"(東京|大阪|名古屋|福岡|札幌|横浜)", text)
    if match:
        return match.group(0)
    return ""

def guess_intern_type(text: str) -> str:
    text_lower = text.lower()
    mapping = {
        "サマー": "夏季",
        "summer": "夏季",
        "ウィンター": "冬季",
        "winter": "冬季",
        "1day": "1day",
        "１day": "1day",
        "one day": "1day",
        "長期": "長期",
        "short": "短期",
        "短期": "短期",
    }
    for key, value in mapping.items():
        if key in text or key in text_lower:
            return value
    return ""

def build_record(company: Dict[str, str], url: str, soup: BeautifulSoup, keywords: Sequence[str]) -> Dict[str, str]:
    page_text = soup.get_text(" ", strip=True)
    title = extract_title(soup, keywords)
    description = extract_description(soup)
    published_date, updated_date = extract_dates(soup, page_text)
    deadline = extract_deadline(page_text)
    intern_type = guess_intern_type(title + " " + description + " " + page_text)
    location = extract_location(page_text)
    return {
        "company_name": company.get("company_name", ""),
        "title": title,
        "url": url,
        "description": description,
        "intern_type": intern_type,
        "location": location,
        "deadline": deadline,
        "published_date": published_date,
        "updated_date": updated_date,
        "status": "",
        "last_checked": "",
    }

def classify(record: Dict[str, str], previous: Dict[Tuple[str, str], Dict[str, str]]) -> str:
    key = (record["company_name"], record["url"])
    if key not in previous:
        return "new"
    old = previous[key]
    comparable_fields = ["title", "description", "intern_type", "location", "deadline", "published_date", "updated_date"]
    if any(record.get(field, "") != old.get(field, "") for field in comparable_fields):
        return "updated"
    return "unchanged"

def load_existing(csv_path: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    if not csv_path.exists():
        return {}
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        data: Dict[Tuple[str, str], Dict[str, str]] = {}
        for row in reader:
            key = (row.get("company_name", ""), row.get("url", ""))
            data[key] = row
        return data

def write_csv(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})

def append_csv(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})

def collect_for_company(company: Dict[str, str], session: requests.Session, keywords: Sequence[str], args: argparse.Namespace) -> List[Dict[str, str]]:
    targets = []
    for key in ("careers_url", "site_url"):
        value = (company.get(key) or "").strip()
        if value:
            targets.append(value)
    targets = list(dict.fromkeys(targets))
    seen: set[str] = set()
    records: List[Dict[str, str]] = []
    for target in targets:
        html = fetch(session, target, args.timeout, args.retries)
        if not html:
            logging.warning("Skip %s (%s): fetch failed", company.get("company_name"), target)
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        if keyword_in_text(text, keywords):
            records.append(build_record(company, target, soup, keywords))
        for link in find_candidate_links(soup, target, keywords, args.max_links):
            if link in seen:
                continue
            seen.add(link)
            child_html = fetch(session, link, args.timeout, args.retries)
            if not child_html:
                logging.warning("Failed to fetch child page %s for %s", link, company.get("company_name"))
                continue
            child_soup = BeautifulSoup(child_html, "html.parser")
            records.append(build_record(company, link, child_soup, keywords))
            if len(seen) >= args.max_links:
                break
    dedup: Dict[str, Dict[str, str]] = {}
    for record in records:
        url = record["url"]
        if url not in dedup or len(record.get("description", "")) > len(dedup[url].get("description", "")):
            dedup[url] = record
    return list(dedup.values())

def merge_records(new_records: List[Dict[str, str]], existing: Dict[Tuple[str, str], Dict[str, str]], run_ts: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    final: Dict[Tuple[str, str], Dict[str, str]] = {}
    diffs: List[Dict[str, str]] = []
    for record in new_records:
        status = classify(record, existing)
        record["status"] = status
        record["last_checked"] = run_ts
        key = (record["company_name"], record["url"])
        final[key] = record
        if status in {"new", "updated"}:
            diffs.append(record)
    for key, prev in existing.items():
        if key in final:
            continue
        carry = dict(prev)
        carry["status"] = "unchanged"
        carry["last_checked"] = run_ts
        final[key] = carry
    return list(final.values()), diffs

def run() -> None:
    args = parse_args()
    setup_logging(Path(args.log_file), args.log_level)
    keywords = read_keywords(Path(args.keywords)) if args.keywords else list(DEFAULT_KEYWORDS)
    companies = load_companies(Path(args.companies))
    existing = load_existing(Path(args.out))
    session = requests.Session()
    headers = dict(DEFAULT_HEADERS)
    headers["User-Agent"] = args.user_agent or DEFAULT_HEADERS["User-Agent"]
    session.headers.update(headers)
    run_ts = dt.datetime.now().isoformat(timespec="seconds")
    all_records: List[Dict[str, str]] = []
    for company in companies:
        name = company.get("company_name") or company.get("site_url")
        try:
            company_records = collect_for_company(company, session, keywords, args)
            if company_records:
                logging.info("Collected %d postings for %s", len(company_records), name)
            else:
                logging.info("No postings found for %s", name)
            all_records.extend(company_records)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Error while processing %s: %s", name, exc)
            continue
    merged, diffs = merge_records(all_records, existing, run_ts)
    write_csv(Path(args.out), merged)
    if diffs and args.diff_out:
        write_csv(Path(args.diff_out), diffs)
        logging.info("Wrote %d new/updated rows to %s", len(diffs), args.diff_out)
    if diffs and args.history_out:
        append_csv(Path(args.history_out), diffs)
        logging.info("Appended %d new/updated rows to %s", len(diffs), args.history_out)
    logging.info("Done. Total records: %d", len(merged))

if __name__ == "__main__":
    run()
