"""Low-memory ingestion of public boycott lists as unverified research leads.

The source records are deliberately kept separate from primary evidence. A
record from either list can never, by itself, confirm a company relationship.
"""

from __future__ import annotations

import csv
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BOYCOTT_HTML_URL = "https://boycott-israel.org/boycott.html"
BOYCOTT_CSV_URL = (
    "https://raw.githubusercontent.com/TechForPalestine/"
    "boycott-israeli-consumer-goods-dataset/main/output/csv/companies.csv"
)
BOYCOTT_CSV_PAGE_URL = (
    "https://github.com/TechForPalestine/"
    "boycott-israeli-consumer-goods-dataset/blob/main/output/csv/companies.csv"
)
MAX_SOURCE_BYTES = 2_000_000
MAX_SOURCE_RECORDS = 5_000
SOURCE_CACHE_SECONDS = 6 * 60 * 60

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _normal(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _compact(value: str, limit: int = 1800) -> str:
    text = re.sub(r"\[\^\d+\]", "", str(value or ""))
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)
    text = re.sub(r"[*_`#]+", "", text)
    text = " ".join(text.split())
    return text[:limit]


def _iso_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        match = re.search(r"\b(20\d{2})[-/]?(\d{2})[-/]?(\d{2})\b", value)
        return "-".join(match.groups()) if match else ""


class BoycottHTMLParser(HTMLParser):
    """Extract company cards from the list's semantic headings and paragraphs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict] = []
        self.category = ""
        self.current: dict | None = None
        self.capture: dict | None = None
        self.updated_at = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "meta":
            field = (attr.get("name") or attr.get("property") or attr.get("itemprop", "")).casefold()
            if field in {"last-modified", "datemodified", "article:modified_time", "og:updated_time"}:
                self.updated_at = _iso_date(attr.get("content")) or self.updated_at
        elif tag == "time" and not self.updated_at and any(
            marker in " ".join((attr.get("itemprop", ""), attr.get("class", ""), attr.get("aria-label", "")).casefold().split())
            for marker in ("modified", "updated", "datemodified")
        ):
            self.updated_at = _iso_date(attr.get("datetime"))

        if tag in {"h1", "h2", "h3"}:
            self._finish_entry()
            self.capture = {"tag": tag, "kind": "category", "depth": 1, "parts": []}
        elif tag == "h4":
            self._finish_entry()
            self.capture = {"tag": tag, "kind": "company", "depth": 1, "parts": []}
        elif tag == "p" and self.current and not self.current.get("description"):
            self.capture = {"tag": tag, "kind": "description", "depth": 1, "parts": []}
        elif self.capture:
            self.capture["depth"] += 1

        if self.current and tag == "a":
            href = attr.get("href", "").strip()
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                if parsed.netloc.casefold() != "boycott-israel.org":
                    refs = self.current.setdefault("references", [])
                    if href not in refs and len(refs) < 8:
                        refs.append(href[:500])

    def handle_endtag(self, tag: str) -> None:
        if not self.capture:
            return
        if tag.lower() == self.capture["tag"]:
            self.capture["depth"] -= 1
            if self.capture["depth"] <= 0:
                value = _compact(" ".join(self.capture["parts"]), 2200)
                kind = self.capture["kind"]
                self.capture = None
                if kind == "category" and value:
                    self.category = value
                elif kind == "company" and value:
                    self.current = self._new_entry(value)
                elif kind == "description" and self.current and value:
                    self.current["description"] = value
        elif self.capture:
            self.capture["depth"] = max(1, self.capture["depth"] - 1)

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.capture["parts"].append(data)

    def _new_entry(self, heading: str) -> dict | None:
        match = re.match(r"^(.*?)\s+(very\s+high|high|medium|low)\s*$", heading, re.I)
        name = (match.group(1) if match else heading).strip()
        if (not name or _normal(name) == "info"
                or _normal(name).startswith("warning dont stop medications for boycotts")):
            return None
        return {
            "name": name[:180],
            "id": "",
            "impact": match.group(2).lower() if match else "",
            "category": self.category[:120],
            "description": "",
            "references": [],
        }

    def _finish_entry(self) -> None:
        if self.current:
            self.current["description"] = _compact(self.current.get("description", ""))
            self.records.append(self.current)
            self.current = None

    def close(self) -> None:
        super().close()
        self._finish_entry()


def _fetch(url: str) -> tuple[bytes, dict]:
    req = Request(url, headers={
        "User-Agent": "Traccia/1.0 (public-source research; contact configured by operator)",
        "Accept": "text/html,text/csv,text/plain;q=0.9,*/*;q=0.5",
    })
    with urlopen(req, timeout=10) as response:
        body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError("La fonte supera il limite di download configurato.")
        headers = {
            "last_modified": _iso_date(response.headers.get("Last-Modified")),
            "date": _iso_date(response.headers.get("Date")),
            "etag": str(response.headers.get("ETag", ""))[:160],
        }
    return body, headers


def _cached_source(key: str, loader) -> dict:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < SOURCE_CACHE_SECONDS:
            return cached[1]
    try:
        value = loader()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, csv.Error):
        value = {"ok": False, "records": [], "updated_at": "", "retrieved_at": ""}
    with _cache_lock:
        if len(_cache) >= 8:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def _load_html() -> dict:
    body, headers = _fetch(BOYCOTT_HTML_URL)
    parser = BoycottHTMLParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return {
        "ok": bool(parser.records),
        "records": parser.records[:MAX_SOURCE_RECORDS],
        "updated_at": parser.updated_at or headers["last_modified"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_csv() -> dict:
    body, headers = _fetch(BOYCOTT_CSV_URL)
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(text))
    records = []
    for row in reader:
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        description = str(row.get("description") or "")
        refs = list(dict.fromkeys(
            url.rstrip(".,;:") for url in re.findall(r"https?://[^\s)\]<>\"']+", description)
        ))[:8]
        records.append({
            "name": name[:180],
            "id": str(row.get("id") or "")[:180],
            "impact": str(row.get("status") or "")[:40],
            "category": "",
            "description": _compact(description),
            "references": refs,
        })
        if len(records) >= MAX_SOURCE_RECORDS:
            break
    return {
        "ok": bool(records),
        "records": records,
        "updated_at": headers["last_modified"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _canonical_name(value: str) -> str:
    tokens = _normal(value).split()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    suffixes = {
        "inc", "incorporated", "corp", "corporation", "company", "co",
        "plc", "ltd", "limited", "llc", "llp", "sa", "spa", "nv", "ag",
        "gmbh", "group", "holdings", "holding",
    }
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    return " ".join(tokens)


def _record_matches(query: str, record: dict) -> bool:
    query_names = {_canonical_name(query), _normal(query)} - {""}
    record_names = {
        _canonical_name(record.get("name", "")),
        _normal(record.get("name", "")),
        _canonical_name(record.get("id", "")),
        _normal(record.get("id", "")),
    } - {""}
    for left in query_names:
        for right in record_names:
            if left == right:
                return True
            # Permit brand names embedded in a legal company name, but keep
            # token boundaries and a minimum length to avoid Intel/Intellicorp.
            if len(left) >= 4 and f" {left} " in f" {right} ":
                return True
            if len(right) >= 4 and f" {right} " in f" {left} ":
                return True
    # The HTML list sometimes names a parent plus a brand in parentheses.
    for alias in re.findall(r"\(([^)]{2,80})\)", str(record.get("name", ""))):
        alias_norm = _canonical_name(alias)
        if alias_norm and alias_norm in query_names:
            return True
    return False


def _provider(source: dict, display_url: str, label: str) -> dict:
    return {
        "source": label,
        "source_url": display_url,
        "updated_at": source.get("updated_at") or "",
        "retrieved_at": source.get("retrieved_at") or "",
        "available": bool(source.get("ok")),
    }


def get_initial_signal(company: str) -> dict:
    """Return a matched lead or a source-search record; never mark it verified."""
    html_source = _cached_source("boycott_html", _load_html)
    html_match = next((
        row for row in html_source.get("records", []) if _record_matches(company, row)
    ), None)
    if html_match:
        record = html_match
        provider = _provider(html_source, BOYCOTT_HTML_URL, "Boycott Israel — lista HTML")
        fallback_status = "non consultato: segnalazione trovata nella lista HTML"
    else:
        csv_source = _cached_source("boycott_csv", _load_csv)
        csv_match = next((
            row for row in csv_source.get("records", []) if _record_matches(company, row)
        ), None)
        if csv_match:
            record = csv_match
            provider = _provider(csv_source, BOYCOTT_CSV_PAGE_URL, "TechForPalestine — CSV di fallback")
            fallback_status = "segnalazione trovata nel CSV di fallback"
        else:
            record = None
            if html_source.get("ok"):
                provider = _provider(html_source, BOYCOTT_HTML_URL, "Boycott Israel — lista HTML")
            elif csv_source.get("ok"):
                provider = _provider(csv_source, BOYCOTT_CSV_PAGE_URL, "TechForPalestine — CSV di fallback")
            else:
                provider = _provider(html_source, BOYCOTT_HTML_URL, "Boycott Israel — lista HTML")
            fallback_status = (
                "consultato, nessuna corrispondenza"
                if csv_source.get("ok") else "non disponibile"
            )

    if record:
        signal = {
            **record,
            "description": _compact(record.get("description", "")),
            "matched": True,
            "source": provider["source"],
            "source_url": provider["source_url"],
            "source_updated_at": provider["updated_at"],
            "retrieved_at": provider["retrieved_at"],
            "references": record.get("references", [])[:8],
            "status": "segnalazione iniziale da verificare",
        }
    else:
        searched = bool(html_source.get("ok") or _cache.get("boycott_csv", (0, {}))[1].get("ok"))
        signal = {
            "name": "",
            "impact": "",
            "category": "",
            "description": (
                "Nessuna voce corrispondente trovata nelle liste consultate."
                if searched else
                "Le fonti di segnalazione non sono state raggiunte; non è possibile concludere che non esista una voce."
            ),
            "matched": False,
            "searched": searched,
            "source": provider["source"],
            "source_url": provider["source_url"],
            "source_updated_at": provider["updated_at"],
            "retrieved_at": provider["retrieved_at"],
            "references": [],
            "status": "nessuna corrispondenza" if searched else "fonti di segnalazione non disponibili",
        }

    html_status = (
        "consultata · segnalazione trovata"
        if html_match else
        "consultata · nessuna corrispondenza"
        if html_source.get("ok") else
        "non disponibile o formato non riconosciuto"
    )
    csv_cached = _cache.get("boycott_csv", (0, {}))[1]
    return {
        "signal": signal,
        "providers": [
            {
                **_provider(html_source, BOYCOTT_HTML_URL, "Boycott Israel — lista HTML"),
                "status": html_status,
            },
            {
                **_provider(
                    csv_cached,
                    BOYCOTT_CSV_PAGE_URL,
                    "TechForPalestine — CSV di fallback",
                ),
                "status": fallback_status,
            },
        ],
        "matched": bool(record),
    }
