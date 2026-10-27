"""Traccia: low-memory public-source company evidence research app.

The app reports source passages as candidates for analyst review and never infers
a corporate relationship from a keyword alone.
"""

from __future__ import annotations

import gc
import hashlib
import html
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request
from alternative_recommendations import COUNTRIES, get_alternatives
from signal_sources import get_initial_signal

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
GDELT_CONTEXT_URL = "https://api.gdeltproject.org/api/v2/context/context"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
MAX_FILINGS = min(6, max(1, int(os.environ.get("MAX_FILINGS", "4"))))
MAX_FILING_BYTES = int(os.environ.get("MAX_FILING_BYTES", str(2 * 1024 * 1024)))
MAX_CHUNKS = min(1200, max(100, int(os.environ.get("MAX_CHUNKS", "800"))))
MAX_NEWS_RESULTS = min(8, max(1, int(os.environ.get("MAX_NEWS_RESULTS", "5"))))
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
APP_ACCESS_KEY = os.environ.get("APP_ACCESS_KEY", "")
ASSET_VERSION = "7"

_analysis_lock = threading.Lock()
_sec_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_sec_request = 0.0
_embedder = None
_spacy_nlp = None
_spacy_checked = False
_request_counts: dict[str, list[float]] = {}
_wikidata_cache: dict[str, tuple[float, list[dict]]] = {}
_wikidata_cache_lock = threading.Lock()

ISRAEL_TERMS = (
    "israel", "israeli", "israelis", "tel aviv", "jerusalem", "haifa",
    "israel-based", "state of israel", "israelian",
)
RELATION_TERMS = (
    "subsidiar", "affiliate", "joint venture", "partnership", "partner",
    "operat", "office", "facility", "research and development", "r&d",
    "contract", "customer", "supplier", "vendor", "acquisition", "acquired",
    "investment", "invested", "agreement", "distribution", "distribut", "manufactur",
    "employee", "employees", "sales", "revenue", "branch", "incorporat",
    "factory", "fab", "plant", "campus", "headquarter", "hire", "procurement",
)
# Curated starting points are primary sources published by the companies themselves.
# Add entries only after checking the company identifier and the source URL.
CURATED_COMPANY_SOURCES = {
    "0000050863": [
        {
            "title": "Intel in Israel",
            "url": "https://www.intel.com/content/www/us/en/corporate-responsibility/intel-in-israel.html",
            "source_class": "Fonte aziendale ufficiale",
        },
    ],
}
FERRERO_ISRAEL_SOURCE = [{
    "title": "Ferrero in Israel",
    "url": "https://www.ferrero.com/il/en/about-us/ferrero-in-israel",
    "source_class": "Fonte aziendale ufficiale",
}]
CURATED_COMPANY_SOURCES_BY_NAME = {
    name: FERRERO_ISRAEL_SOURCE
    for name in {
        "ferrero", "ferrero group", "ferrero international",
        "ferrero international s a", "ferrero s p a",
    }
}
STOP_WORDS = set("""
the and for with from that this were was are has have had its into their they
will would could should about after before under over between across company
companies business businesses annual report filing fiscal year may can all any
not our we us you your as an of to in on by is be or at it he she them such
also than other which who where when what how per due based pursuant including
said says more less no if but while because through these those
""".split())


class FilingText(HTMLParser):
    """Small stdlib HTML-to-text extractor; avoids loading a browser stack."""

    BLOCK_TAGS = {"p", "div", "br", "tr", "td", "th", "li", "h1", "h2", "h3"}
    SKIP_TAGS = {"script", "style", "ix:header", "xbrl", "xml"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9][a-z0-9&.'-]{1,}", text.lower())
        if token not in STOP_WORDS
    ]


def _sec_headers() -> dict[str, str]:
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not agent:
        raise RuntimeError(
            "Configura SEC_USER_AGENT con nome applicazione e contatto dell'operatore "
            "per rispettare le regole di accesso automatico EDGAR."
        )
    return {"User-Agent": agent, "Accept-Encoding": "identity"}


def _fetch(
    url: str,
    *,
    sec: bool = True,
    max_bytes: int = 4_000_000,
    timeout: int = 18,
    user_agent: str | None = None,
) -> bytes:
    global _last_sec_request
    headers = _sec_headers() if sec else {
        "User-Agent": user_agent or "Traccia/1.0 (public company research)",
    }
    headers["Accept"] = "application/json, text/html, text/plain;q=0.9, */*;q=0.5"
    if sec:
        with _sec_lock:
            wait = 0.12 - (time.monotonic() - _last_sec_request)
            if wait > 0:
                time.sleep(wait)
            _last_sec_request = time.monotonic()
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Il documento supera il limite di download configurato.")
    return data


def _normalize_company(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def find_companies(query: str) -> list[dict]:
    query_norm = _normalize_company(query)
    if len(query_norm) < 2:
        return []
    query_tokens = query_norm.split()
    query_ticker = re.sub(r"[^A-Z0-9]", "", query.upper())
    payload = json.loads(_fetch(SEC_TICKERS_URL, max_bytes=8_000_000))
    matches = []
    for row in payload.values():
        title = row.get("title", "")
        normalized = _normalize_company(title)
        title_tokens = normalized.split()
        ticker = re.sub(r"[^A-Z0-9]", "", str(row.get("ticker", "")).upper())
        if query_norm == normalized:
            score = 100
        elif query_ticker and query_ticker == ticker:
            score = 95
        elif query_tokens and all(part in title_tokens for part in query_tokens):
            score = 80
        else:
            continue
        matches.append({
            "name": title,
            "ticker": row.get("ticker", ""),
            "cik": str(row.get("cik_str", "")).zfill(10),
            "score": score,
        })
    matches.sort(key=lambda item: (-item["score"], item["name"]))
    return matches[:12]


def find_wikidata_entities(query: str) -> list[dict]:
    """Resolve non-SEC names to human-confirmed Wikidata candidates."""
    cache_key = _normalize_company(query)
    now = time.monotonic()
    with _wikidata_cache_lock:
        cached = _wikidata_cache.get(cache_key)
        if cached and now - cached[0] < 3600:
            return [dict(item) for item in cached[1]]

    params = {
        "action": "wbsearchentities",
        "search": query[:100],
        "language": "en",
        "format": "json",
        "formatversion": "2",
        "type": "item",
        "limit": "6",
        "maxlag": "5",
    }
    url = f"{WIKIDATA_API_URL}?{urlencode(params)}"
    try:
        user_agent = os.environ.get("SEC_USER_AGENT", "Traccia/1.0 (public company research)")
        payload = json.loads(_fetch(
            url, sec=False, max_bytes=400_000, timeout=8, user_agent=user_agent
        ))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return []

    matches = []
    rows = payload.get("search", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    for row in rows[:6]:
        if not isinstance(row, dict):
            continue
        entity_id = str(row.get("id", ""))
        label = str(row.get("label", "")).strip()
        if not re.fullmatch(r"Q[1-9]\d*", entity_id) or not label:
            continue
        matches.append({
            "name": label,
            "ticker": "",
            "cik": "",
            "entity_id": entity_id,
            "entity_source": "Wikidata",
            "description": str(row.get("description", ""))[:220],
            "score": 60,
        })
    with _wikidata_cache_lock:
        if len(_wikidata_cache) >= 256:
            _wikidata_cache.clear()
        _wikidata_cache[cache_key] = (now, [dict(item) for item in matches])
    return matches


def _recent_filings(cik: str) -> tuple[dict, list[dict]]:
    submissions = json.loads(_fetch(SEC_SUBMISSIONS_URL.format(cik=cik), timeout=10))
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    selected = []
    accepted_forms = {"10-K", "10-K/A", "20-F", "20-F/A", "8-K", "8-K/A", "6-K", "6-K/A"}
    rows = []
    for index, form in enumerate(forms):
        if form not in accepted_forms:
            continue
        rows.append({
            "form": form,
            "date": recent.get("filingDate", [""])[index],
            "accession": recent.get("accessionNumber", [""])[index],
            "document": recent.get("primaryDocument", [""])[index],
            "description": recent.get("primaryDocDescription", [""])[index]
            if index < len(recent.get("primaryDocDescription", [])) else "",
        })
    annual = [row for row in rows if row["form"].startswith(("10-K", "20-F"))]
    current = [row for row in rows if row["form"].startswith(("8-K", "6-K"))]
    # Keep one annual baseline plus a few recent event filings where possible.
    if annual:
        selected.append(annual[0])
    selected.extend(current[:max(0, MAX_FILINGS - len(selected))])
    if len(selected) < MAX_FILINGS:
        selected.extend(annual[1:MAX_FILINGS - len(selected)])
    selected = selected[:MAX_FILINGS]
    return submissions, selected


def _filing_url(cik: str, filing: dict) -> str:
    accession = filing["accession"].replace("-", "")
    # Primary-document names come from SEC submissions JSON; quote each URL segment.
    doc = quote(filing["document"], safe=".-_()")
    return SEC_ARCHIVE_URL.format(cik=int(cik), accession=accession, document=doc)


def _html_to_text(data: bytes) -> str:
    parser = FilingText()
    parser.feed(data.decode("utf-8", errors="replace"))
    raw = html.unescape("".join(parser.parts))
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:3_000_000]


def _chunks_for_filing(
    text: str, source: dict, company: str, max_chunks: int
) -> list[dict]:
    # Split on sentence boundaries, then make bounded overlapping passages.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    chunks: list[dict] = []
    buffer = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 35:
            continue
        if len(buffer) + len(sentence) > 850 and buffer:
            chunks.append({**source, "text": buffer[:1200], "company": company})
            buffer = sentence
        else:
            buffer = f"{buffer} {sentence}".strip()
        if len(chunks) >= max_chunks:
            break
    if buffer and len(chunks) < max_chunks:
        chunks.append({**source, "text": buffer[:1200], "company": company})
    return chunks


def _bm25_scores(query: str, documents: list[str]) -> list[float]:
    query_terms = list(dict.fromkeys(_tokenize(query)))
    tokenized = [_tokenize(text) for text in documents]
    if not tokenized:
        return []
    n_docs = len(tokenized)
    avg_len = sum(map(len, tokenized)) / max(n_docs, 1)
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    scores = []
    k1, b = 1.5, 0.75
    for tokens in tokenized:
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            freq = frequencies.get(term, 0)
            if not freq:
                continue
            df = document_frequency[term]
            inverse = max(0.0, (n_docs - df + 0.5) / (df + 0.5))
            inverse = (inverse + 1.0)  # smooth IDF for small corpora
            denominator = freq + k1 * (1.0 - b + b * len(tokens) / max(avg_len, 1))
            score += inverse * (freq * (k1 + 1.0)) / denominator
        scores.append(score)
    return scores


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            cache_dir=os.environ.get("FASTEMBED_CACHE_PATH") or None,
            threads=1,
        )
    return _embedder


def _semantic_scores(query: str, documents: list[str]) -> list[float] | None:
    try:
        import faiss
        import numpy as np

        model = _get_embedder()
        doc_vectors = np.asarray(list(model.embed(documents)), dtype="float32")
        query_vectors = np.asarray(list(model.query_embed(query)), dtype="float32")
        if doc_vectors.ndim != 2 or not len(doc_vectors):
            return None
        faiss.normalize_L2(doc_vectors)
        faiss.normalize_L2(query_vectors)
        index = faiss.IndexFlatIP(doc_vectors.shape[1])
        index.add(doc_vectors)
        distances, positions = index.search(query_vectors, len(documents))
        scores = [0.0] * len(documents)
        for rank, position in enumerate(positions[0]):
            if position >= 0:
                scores[int(position)] = float(distances[0][rank])
        return scores
    except Exception:
        # The transparent lexical retrieval path remains usable if model files or
        # native FAISS are temporarily unavailable; the report says so explicitly.
        return None


def _hybrid_rank(query: str, chunks: list[dict]) -> tuple[list[dict], bool]:
    texts = [item["text"] for item in chunks]
    lexical = _bm25_scores(query, texts)
    lex_order = sorted(range(len(chunks)), key=lambda i: lexical[i], reverse=True)
    # Keep neural inference bounded on Render Free: BM25 selects a candidate set,
    # then FAISS adds a semantic ranking for that set.
    candidate_ids = lex_order[:min(200, len(lex_order))]
    semantic = _semantic_scores(query, [texts[index] for index in candidate_ids])
    sem_order = (
        [candidate_ids[position] for position in sorted(
            range(len(candidate_ids)), key=lambda i: semantic[i], reverse=True
        )]
        if semantic is not None else []
    )
    # Reciprocal-rank fusion combines lexical and vector rankings without pretending
    # their raw scores share a common scale.
    fused: dict[int, float] = {}
    for order in (lex_order, sem_order):
        for rank, index in enumerate(order):
            fused[index] = fused.get(index, 0.0) + 1.0 / (60 + rank + 1)
    ordered = sorted(fused, key=fused.get, reverse=True)
    return [chunks[index] for index in ordered[:25]], semantic is not None


def _sentence_mentions(text: str) -> list[dict]:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    output = []
    for sentence in sentences:
        lowered = sentence.casefold()
        terms = [term for term in ISRAEL_TERMS if term in lowered]
        if not terms:
            continue
        relation_hits = [term for term in RELATION_TERMS if term in lowered]
        normalized = " ".join(sentence.split())
        output.append({
            "text": normalized[:900],
            "israel_terms": terms,
            "relation_terms": relation_hits,
            "candidate": bool(relation_hits),
        })
    return output


def _source_date(value: str) -> str:
    """Normalize source dates without presenting GDELT's seen date as a publish date."""
    match = re.match(r"^(\d{4})(\d{2})(\d{2})", str(value or ""))
    if match:
        return "-".join(match.groups())
    return str(value or "")[:10]


def _curated_company_evidence(
    cik: str, company: str = ""
) -> tuple[list[dict], list[dict], str]:
    documents: list[dict] = []
    evidence: list[dict] = []
    sources = list(CURATED_COMPANY_SOURCES.get(cik, []))
    company_key = _normalize_company(company)
    sources.extend(CURATED_COMPANY_SOURCES_BY_NAME.get(company_key, []))
    sources = list({source["url"]: source for source in sources}.values())
    if not sources:
        return documents, evidence, "non configurata per questa società"
    for source in sources:
        record = {
            "form": "Profilo aziendale",
            "date": "",
            "description": source["title"],
            "source_url": source["url"],
            "source_class": source["source_class"],
        }
        try:
            raw = _fetch(source["url"], sec=False, max_bytes=1_200_000, timeout=10)
            text = _html_to_text(raw)
            documents.append(record)
            for mention in _sentence_mentions(text):
                evidence.append({
                    **mention,
                    **record,
                    "entities": [],
                })
                if len(evidence) >= 8:
                    break
        except (HTTPError, URLError, TimeoutError, ValueError):
            continue
    status = "consultata" if documents else "non raggiungibile"
    return documents, evidence, status


def _company_search_alias(company: str) -> str:
    tokens = _normalize_company(company).split()
    legal_suffixes = {
        "inc", "incorporated", "corp", "corporation", "company", "co",
        "plc", "ltd", "limited", "llc", "llp", "sa", "nv", "ag",
    }
    while tokens and tokens[-1] in legal_suffixes:
        tokens.pop()
    return " ".join(tokens) or _normalize_company(company)


def _gdelt_article_leads(
    company: str,
) -> tuple[list[dict], list[dict], str]:
    """Find up to a few media leads in GDELT's rolling three-month article index."""
    alias = _company_search_alias(company)
    query = (
        f'"{alias}" (Israel OR Israeli OR "Tel Aviv" OR Jerusalem OR Haifa '
        'OR "Kiryat Gat")'
    )
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(MAX_NEWS_RESULTS),
        "timespan": "3m",
    }
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?{urlencode(params)}"
    try:
        payload = json.loads(_fetch(url, sec=False, max_bytes=1_000_000, timeout=8))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return [], [], "indice articoli non disponibile"
    if not isinstance(payload, dict) or not isinstance(payload.get("articles", []), list):
        return [], [], "indice articoli con risposta non valida"

    documents: list[dict] = []
    headline_evidence: list[dict] = []
    for article in payload.get("articles", [])[:MAX_NEWS_RESULTS]:
        if not isinstance(article, dict):
            continue
        source_url = str(article.get("url", ""))
        if not source_url.startswith(("https://", "http://")):
            continue
        title = str(article.get("title", ""))[:300]
        domain = str(article.get("domain", ""))[:160]
        seen_date = _source_date(article.get("seendate") or "")
        record = {
            "form": "Articolo indicizzato",
            "date": seen_date,
            "date_kind": "rilevazione GDELT",
            "description": title or domain or "Articolo trovato tramite GDELT",
            "source_url": source_url,
            "source_class": "Fonte giornalistica / GDELT",
            "domain": domain,
            "language": str(article.get("language", ""))[:40],
        }
        documents.append(record)
        for mention in _sentence_mentions(title):
            headline_evidence.append({
                **mention,
                **record,
                "source_class": "Titolo giornalistico / GDELT",
                "entities": [],
            })
    return documents, headline_evidence, f"indice consultato: {len(documents)} titoli / ultimi 3 mesi"


def _gdelt_context_evidence(
    company: str,
) -> tuple[list[dict], list[dict], str]:
    """Retrieve recent sentence-level media context; links remain analyst-review leads."""
    alias = _company_search_alias(company)
    if len(alias) < 2:
        return [], [], "saltata: nome non sufficiente"
    query = (
        f'"{alias}" (Israel OR Israeli OR "Tel Aviv" OR Jerusalem OR Haifa '
        'OR "Kiryat Gat")'
    )
    params = {
        'query': query,
        'mode': 'artlist',
        'format': 'json',
        'maxrecords': str(MAX_NEWS_RESULTS),
        'timespan': '3d',
        'sort': 'datedesc',
    }
    url = f"{GDELT_CONTEXT_URL}?{urlencode(params)}"
    try:
        payload = json.loads(_fetch(url, sec=False, max_bytes=1_000_000, timeout=8))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return [], [], "non disponibile"

    documents: list[dict] = []
    evidence: list[dict] = []
    if not isinstance(payload, dict):
        return [], [], "risposta non valida"
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return [], [], "risposta non valida"
    for article in articles[:MAX_NEWS_RESULTS]:
        if not isinstance(article, dict):
            continue
        source_url = str(article.get("url", ""))
        if not source_url.startswith(("https://", "http://")):
            continue
        title = str(article.get("title", ""))[:300]
        domain = str(article.get("domain", ""))[:160]
        seen_date = _source_date(article.get("seendate") or article.get("seen_at") or "")
        record = {
            "form": "Articolo indicizzato",
            "date": seen_date,
            "date_kind": "rilevazione GDELT",
            "description": title or domain or "Articolo trovato tramite GDELT",
            "source_url": source_url,
            "source_class": "Fonte giornalistica / GDELT",
            "domain": domain,
            "language": str(article.get("language", ""))[:40],
        }
        documents.append(record)
        snippet = str(article.get("context") or article.get("sentence") or title)
        for mention in _sentence_mentions(snippet):
            evidence.append({**mention, **record, "entities": []})
            if len(evidence) >= 12:
                break
        if len(evidence) >= 12:
            break
    return documents, evidence, f"consultata: {len(documents)} risultati negli ultimi 3 giorni"


def _gdelt_public_sources(company: str) -> tuple[list[dict], list[dict], str]:
    articles, headlines, article_status = _gdelt_article_leads(company)
    context, snippets, context_status = _gdelt_context_evidence(company)
    by_url = {item["source_url"]: item for item in articles}
    by_url.update({item["source_url"]: item for item in context})
    seen = set()
    evidence = []
    for item in headlines + snippets:
        key = (item["source_url"], item["text"][:160])
        if key not in seen:
            seen.add(key)
            evidence.append(item)
    return list(by_url.values()), evidence, f"{article_status}; {context_status}"


def _get_spacy():
    global _spacy_nlp, _spacy_checked
    if _spacy_checked:
        return _spacy_nlp
    _spacy_checked = True
    try:
        import spacy

        _spacy_nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])
    except Exception:
        _spacy_nlp = None
    return _spacy_nlp


def _extract_entities(text: str) -> list[str]:
    nlp = _get_spacy()
    if nlp is None:
        return []
    try:
        doc = nlp(text[:5000])
        return list(dict.fromkeys(
            entity.text.strip() for entity in doc.ents
            if entity.label_ in {"ORG", "GPE", "LOC", "PERSON"}
        ))[:12]
    except Exception:
        return []


def _evidence_score(evidence: list[dict], filing_count: int, model_used: bool) -> int:
    """A rough retrieval-quality signal, explicitly not a probability of a tie."""
    if not evidence:
        return min(48, 18 + filing_count * 5)
    explicit = sum(bool(item["relation_terms"]) for item in evidence)
    distinct_sources = len({item.get("source_class", "") for item in evidence})
    primary = any(item.get("source_class") in {
        "Fonte aziendale ufficiale", "Filing regolamentare SEC",
    } for item in evidence)
    return min(92, 42 + min(explicit, 4) * 6 + min(distinct_sources, 3) * 5
               + (4 if model_used else 0) + (5 if primary else 0))


PRIMARY_SOURCE_CLASSES = {"Fonte aziendale ufficiale", "Filing regolamentare SEC"}


def _primary_tie_assertion(text: str) -> bool:
    """Require an assertive company-to-Israel statement, not mere topic proximity."""
    sentence = " ".join(str(text or "").casefold().split())
    if not sentence or re.search(
        r"\b(?:does not|do not|did not|has no|have no|no longer|without|"
        r"may|might|could|potential|alleged|allegedly|reportedly|accused|"
        r"claims?|allegation|risk of)\b",
        sentence,
    ):
        return False
    relation = (
        r"(?:operat\w*|establish\w*|found\w*|set\s+up|maintain\w*|run\w*|"
        r"own\w*|open\w*|base\w*|"
        r"acquir\w*|invest\w*|manufactur\w*|sell\w*|suppl\w*|distribut\w*|"
        r"sign\w*|enter\w* into|provid\w*|headquarter\w*|presence|facilit\w*|"
        r"office\w*|subsidiar\w*|branch\w*|factory|factories|plant\w*|"
        r"research and development center\w*|r&d center\w*|joint venture|contract\w*)"
    )
    israel = r"(?:israel|israeli|tel aviv|jerusalem|haifa|kiryat gat)"
    # Positive action/location syntax is intentional: terms such as "Israel may
    # affect operations" remain leads for review, not an automatic confirmation.
    forward = rf"\b{relation}\b.{{0,100}}\b(?:in|within|across|to|with|from)\s+(?:the\s+)?{israel}\b"
    reverse = rf"\b{israel}\b.{{0,55}}\b(?:office\w*|facilit\w*|subsidiar\w*|branch\w*|factory|factories|plant\w*|headquarter\w*|research and development center\w*|r&d center\w*)\b"
    direct_israeli_entity = re.search(
        r"\bisraeli\s+(?:company|multinational|corporation|business|subsidiary|startup|firm)\b",
        sentence,
    )
    return bool(
        re.search(forward, sentence)
        or re.search(reverse, sentence)
        or direct_israeli_entity
    )


def _independent_primary_confirmations(evidence: list[dict]) -> list[dict]:
    """Count distinct primary-source domains with an Israel/relationship passage."""
    by_domain: dict[str, dict] = {}
    for item in evidence:
        if item.get("source_class") not in PRIMARY_SOURCE_CLASSES:
            continue
        if not item.get("candidate") or not _primary_tie_assertion(item.get("text", "")):
            continue
        url = str(item.get("source_url", ""))
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        domain = parsed.hostname.casefold().removeprefix("www.")
        if not domain:
            continue
        by_domain.setdefault(domain, {
            "domain": domain,
            "source_class": item["source_class"],
            "description": item.get("description", "Fonte primaria consultata"),
            "source_url": url,
            "date": item.get("date", ""),
            "text": item["text"],
        })
    return sorted(by_domain.values(), key=lambda item: item["domain"])


def analyze_company(
    company: str, cik: str = "", entity_id: str = "", country_code: str = "IT"
) -> dict:
    global _embedder, _spacy_nlp, _spacy_checked
    if cik and not re.fullmatch(r"\d{10}", cik):
        raise ValueError("CIK non valido.")
    if cik:
        sec_status = "consultata"
        try:
            metadata, filings = _recent_filings(cik)
        except (HTTPError, URLError, TimeoutError, ValueError):
            metadata, filings = {"name": company, "tickers": []}, []
            sec_status = "non disponibile"
    else:
        metadata, filings = {"name": company, "tickers": []}, []
        sec_status = "non applicabile: analisi per nome senza CIK SEC"
    company_name = metadata.get("name") or company
    country_code = country_code.upper()
    if country_code not in COUNTRIES:
        raise ValueError("Paese non supportato per la ricerca delle alternative.")
    filing_documents: list[dict] = []
    chunks: list[dict] = []
    for filing in filings:
        if not filing.get("document") or not filing.get("accession"):
            continue
        if not filing["document"].lower().endswith((".htm", ".html", ".txt")):
            continue
        url = _filing_url(cik, filing)
        try:
            raw = _fetch(url, max_bytes=MAX_FILING_BYTES, timeout=10)
            text = _html_to_text(raw)
            if len(text) < 300:
                continue
            source = {
                "form": filing["form"],
                "date": filing["date"],
                "description": filing.get("description", ""),
                "filing_url": url,
                "source_url": url,
                "source_class": "Filing regolamentare SEC",
            }
            filing_documents.append(source)
            remaining = max(0, MAX_CHUNKS - len(chunks))
            if remaining:
                chunks.extend(_chunks_for_filing(text, source, company_name, remaining))
        except (HTTPError, URLError, TimeoutError, ValueError):
            continue
    chunks = chunks[:MAX_CHUNKS]
    query = (
        f"{company_name} Israel Israeli Tel Aviv Jerusalem subsidiary operations "
        "office facility contract customer supplier partnership investment acquisition"
    )
    ranked, vector_used = _hybrid_rank(query, chunks) if chunks else ([], False)
    # Free ONNX model/session allocations before loading spaCy for the evidence set.
    _embedder = None
    gc.collect()
    evidence = []
    seen = set()
    for item in ranked:
        for mention in _sentence_mentions(item["text"]):
            key = (item["filing_url"], mention["text"][:160])
            if key in seen:
                continue
            seen.add(key)
            evidence.append({
                **mention,
                "form": item["form"],
                "date": item["date"],
                "description": item["description"],
                "filing_url": item["filing_url"],
                "source_url": item["filing_url"],
                "entities": _extract_entities(mention["text"]),
                "source_class": "Filing regolamentare SEC",
            })
            if len(evidence) >= 12:
                break
        if len(evidence) >= 12:
            break
    evidence.sort(key=lambda item: (item["candidate"], item["date"]), reverse=True)
    _spacy_nlp = None
    _spacy_checked = False
    gc.collect()

    if cik:
        profile_documents, profile_evidence, profile_status = _curated_company_evidence(
            cik, company_name
        )
    else:
        profile_documents, profile_evidence, profile_status = _curated_company_evidence(
            "", company_name
        )
    news_documents, news_evidence, news_status = _gdelt_public_sources(company_name)
    evidence.extend(profile_evidence)
    evidence.extend(news_evidence)
    evidence.sort(key=lambda item: (item["candidate"], item.get("date", "")), reverse=True)
    initial_research = get_initial_signal(company_name)
    initial_signal = initial_research["signal"]
    alternatives = get_alternatives(company_name, country_code, entity_id)
    signal_document = {
        "form": "Segnalazione iniziale",
        "date": initial_signal.get("source_updated_at", ""),
        "date_kind": "ultimo aggiornamento dichiarato dalla fonte",
        "description": (
            f"{initial_signal['name']} · segnalazione da verificare"
            if initial_signal.get("matched") else "Ricerca effettuata nella lista; nessuna voce corrispondente"
        ) if initial_signal.get("searched", True) else "Fonti di segnalazione non raggiungibili",
        "source_url": initial_signal["source_url"],
        "source_class": "Segnalazione iniziale (non prova)",
    }
    documents = filing_documents + profile_documents + news_documents + [signal_document]
    source_status = [
        {"source": "SEC EDGAR", "status": f"{len(filing_documents)} filing consultati" if sec_status == "consultata" else sec_status},
        {"source": "Profilo aziendale ufficiale", "status": profile_status},
        {"source": "Contesto notizie GDELT", "status": news_status},
        {"source": "Lista HTML di segnalazione", "status": initial_research["providers"][0].get("status", "consultata; nessuna inferenza di conferma")},
        {"source": "CSV TechForPalestine (fallback)", "status": initial_research["providers"][1].get("status", "non consultato")},
    ]

    primary_confirmations = _independent_primary_confirmations(evidence)
    if primary_confirmations:
        verdict = "Relazione documentata da fonte primaria"
        explanation = (
            f"Almeno una fonte primaria consultata contiene un passaggio pertinente. "
            f"Sono stati individuati {len(primary_confirmations)} domini primari distinti; "
            "la lista iniziale resta una segnalazione e non determina il verdetto. Apri le citazioni "
            "per controllare contesto, data e natura del rapporto."
        )
    else:
        verdict = "Non verificato"
        if initial_signal.get("matched"):
            explanation = (
                "Una lista di segnalazioni riporta questa azienda, ma non è emersa una conferma "
                "pertinente in una fonte primaria nel perimetro consultato. Articoli, liste e "
                "risultati di ricerca non sono conteggiati come conferme. Il rapporto resta non verificato."
            )
        elif initial_signal.get("searched", True):
            explanation = (
                "Non è stata trovata una conferma pertinente in una fonte primaria nel perimetro "
                "consultato, né una voce corrispondente nelle liste di segnalazione disponibili. "
                "Questo esito non dimostra l’assenza di rapporti."
            )
        else:
            explanation = (
                "Non è stata trovata una conferma pertinente in una fonte primaria e le liste di "
                "segnalazione non erano raggiungibili. L’esito è inconclusivo e non dimostra l’assenza di rapporti."
            )
    if not cik:
        identity_note = (
            f" Identità selezionata tramite Wikidata ({entity_id}); verifica che corrisponda "
            "all’azienda cercata."
            if entity_id else
            " Analisi per nome inserito: l’identità societaria non è verificata nei registri."
        )
        explanation += identity_note
    score = _evidence_score(evidence, len(filing_documents), vector_used)
    if not cik:
        score = min(score, 64)
    confirmation_count = len(primary_confirmations)
    reliability_label = (
        "Nessuna conferma primaria"
        if confirmation_count == 0 else
        "Una fonte primaria · affidabilità limitata"
        if confirmation_count == 1 else
        f"{confirmation_count} domini primari distinti"
    )
    return {
        "company": company_name,
        "cik": cik,
        "entity_id": entity_id,
        "identity_source": "Wikidata" if entity_id else ("SEC" if cik else "Nome inserito"),
        "ticker": metadata.get("tickers", [""])[0] if metadata.get("tickers") else "",
        "country_code": country_code,
        "country_name": COUNTRIES[country_code],
        "alternatives": alternatives,
        "verdict": verdict,
        "explanation": explanation,
        "confidence": score,
        "confidence_label": "qualità/pertinenza del recupero, non probabilità di relazione",
        "initial_signal": initial_signal,
        "initial_signal_providers": initial_research["providers"],
        "primary_confirmations": primary_confirmations,
        "reliability": {
            "independent_primary_count": confirmation_count,
            "label": reliability_label,
            "note": "Domini distinti usati come proxy di indipendenza; eventuali proprietari comuni non sono deduplicati. Non è una probabilità.",
        },
        "source_count": len(documents),
        "evidence_count": len(evidence),
        "documents": documents,
        "evidence": evidence,
        "source_status": source_status,
        "retrieval": {
            "bm25": True,
            "faiss": vector_used,
            "embedding_model": EMBEDDING_MODEL if vector_used else None,
            "gdelt_context": (
                "indice consultato:" in news_status or "consultata:" in news_status
            ),
            "official_profile": profile_status == "consultata",
        },
        "scope": (
            "Quando è disponibile un CIK: ultimi filing SEC recuperabili (10-K, 20-F, 8-K e 6-K) "
            "e profili ufficiali nel catalogo verificato. Per ogni nome: indici GDELT di articoli "
            f"degli ultimi 3 mesi e snippet degli ultimi 3 giorni (massimo {MAX_NEWS_RESULTS} ciascuno). "
            "La lista Boycott Israel e il CSV TechForPalestine sono solo fonti di segnalazioni: "
            "non determinano il verdetto. Un risultato positivo richiede un passaggio pertinente "
            "in almeno una fonte primaria; le conferme sono contate per dominio distinto. "
            "Wikidata aiuta a identificare candidati, non è un registro completo di tutte le aziende. "
            "Le opzioni automatiche sono peer candidati basati su settore e paese Wikidata, ordinati con BM25; non dimostrano disponibilità commerciale né assenza di rapporti. "
            "La copertura varia per società e l’assenza di risultati non prova l’assenza di rapporti."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_id": hashlib.sha256(f"{cik}:{time.time_ns()}".encode()).hexdigest()[:12],
    }


def _authorized() -> bool:
    if not APP_ACCESS_KEY:
        return True
    return request.headers.get("X-Access-Key", "") == APP_ACCESS_KEY


def _rate_limited(limit: int, window_seconds: int) -> bool:
    now = time.time()
    key = request.remote_addr or "unknown"
    with _rate_lock:
        attempts = [stamp for stamp in _request_counts.get(key, []) if now - stamp < window_seconds]
        if len(attempts) >= limit:
            _request_counts[key] = attempts
            return True
        attempts.append(now)
        _request_counts[key] = attempts
        if len(_request_counts) > 2048:
            for stale_key, stamps in list(_request_counts.items()):
                fresh = [stamp for stamp in stamps if now - stamp < 3600]
                if fresh:
                    _request_counts[stale_key] = fresh
                else:
                    _request_counts.pop(stale_key, None)
                if len(_request_counts) <= 1024:
                    break
        return False


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/")
def index():
    response = app.make_response(render_template(
        "index.html",
        access_key_required=bool(APP_ACCESS_KEY),
        asset_version=ASSET_VERSION,
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "traccia"})


@app.get("/api/companies")
def companies():
    if not _authorized():
        return jsonify({"error": "Chiave di accesso non valida."}), 401
    if _rate_limited(30, 60):
        return jsonify({"error": "Troppe ricerche. Attendi un minuto."}), 429
    query = request.args.get("q", "")[:100]
    if len(query.strip()) < 2:
        return jsonify({"results": [], "manual_name": ""})
    sec_results = []
    try:
        sec_results = find_companies(query)
    except (RuntimeError, HTTPError, URLError, TimeoutError, ValueError):
        # Keep name-based search available when SEC is unavailable or the company
        # is not a SEC filer; the analyst can choose a Wikidata or manual match.
        sec_results = []
    if sec_results:
        return jsonify({"results": sec_results, "manual_name": ""})
    entity_results = find_wikidata_entities(query.strip())
    return jsonify({"results": entity_results, "manual_name": query.strip()})


@app.post("/api/alternatives")
def alternatives():
    """Refresh country-specific suggestions without rerunning company research."""
    if not _authorized():
        return jsonify({"error": "Chiave di accesso non valida."}), 401
    if _rate_limited(30, 60):
        return jsonify({"error": "Troppe richieste. Attendi un minuto."}), 429
    payload = request.get_json(silent=True) or {}
    company = str(payload.get("company", ""))[:160].strip()
    entity_id = str(payload.get("entity_id", ""))[:16].strip().upper()
    if entity_id and not re.fullmatch(r"Q[1-9]\d*", entity_id):
        entity_id = ""
    country_code = str(payload.get("country_code", "IT")).upper()[:2]
    if not company:
        return jsonify({"error": "Manca il nome dell’azienda."}), 400
    if country_code not in COUNTRIES:
        return jsonify({"error": "Seleziona un paese disponibile nel menu."}), 400
    return jsonify(get_alternatives(company, country_code, entity_id))


@app.post("/api/analyze")
def analyze():
    global _embedder, _spacy_nlp, _spacy_checked
    if not _authorized():
        return jsonify({"error": "Chiave di accesso non valida."}), 401
    if _rate_limited(6, 3600):
        return jsonify({"error": "Limite gratuito: massimo 6 analisi per ora da questo indirizzo."}), 429
    payload = request.get_json(silent=True) or {}
    company = str(payload.get("company", ""))[:160].strip()
    raw_cik = str(payload.get("cik", ""))[:10].strip()
    cik = raw_cik.zfill(10) if raw_cik.isdigit() and raw_cik else ""
    entity_id = str(payload.get("entity_id", ""))[:16].strip().upper()
    if entity_id and not re.fullmatch(r"Q[1-9]\d*", entity_id):
        entity_id = ""
    if not company:
        return jsonify({"error": "Inserisci o seleziona il nome di un’azienda."}), 400
    if not _analysis_lock.acquire(blocking=False):
        return jsonify({"error": "Un’analisi è già in corso. Riprova tra poco."}), 429
    try:
        country_code = str(payload.get("country_code", "IT")).upper()[:2]
        if country_code not in COUNTRIES:
            return jsonify({"error": "Seleziona un paese disponibile nel menu."}), 400
        result = analyze_company(company, cik, entity_id, country_code)
        return jsonify(result)
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except (HTTPError, URLError, TimeoutError) as error:
        return jsonify({"error": f"Fonte pubblica non raggiungibile: {type(error).__name__}."}), 502
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        app.logger.exception("Analisi fallita")
        return jsonify({"error": "Analisi non completata. Riprova più tardi."}), 500
    finally:
        _analysis_lock.release()
        # Drop model references; gc is best-effort because native runtimes may
        # keep memory arenas resident until the process exits.
        _embedder = None
        _spacy_nlp = None
        _spacy_checked = False
        gc.collect()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
