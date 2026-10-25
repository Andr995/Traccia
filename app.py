"""Traccia: low-memory public-company evidence research app.

The app deliberately reports filing mentions as candidates for analyst review.
It does not infer that a company has a relationship from a keyword alone.
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
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
MAX_FILINGS = min(6, max(1, int(os.environ.get("MAX_FILINGS", "4"))))
MAX_FILING_BYTES = int(os.environ.get("MAX_FILING_BYTES", str(2 * 1024 * 1024)))
MAX_CHUNKS = min(1200, max(100, int(os.environ.get("MAX_CHUNKS", "800"))))
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
APP_ACCESS_KEY = os.environ.get("APP_ACCESS_KEY", "")

_analysis_lock = threading.Lock()
_sec_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_sec_request = 0.0
_embedder = None
_spacy_nlp = None
_spacy_checked = False
_request_counts: dict[str, list[float]] = {}

ISRAEL_TERMS = (
    "israel", "israeli", "israelis", "tel aviv", "jerusalem", "haifa",
    "israel-based", "state of israel", "israelian",
)
RELATION_TERMS = (
    "subsidiar", "affiliate", "joint venture", "partnership", "partner",
    "operat", "office", "facility", "research and development", "r&d",
    "contract", "customer", "supplier", "vendor", "acquisition", "acquired",
    "investment", "invested", "agreement", "distribution", "manufactur",
    "employee", "employees", "sales", "revenue", "branch", "incorporat",
)
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


def _fetch(url: str, *, sec: bool = True, max_bytes: int = 4_000_000) -> bytes:
    global _last_sec_request
    headers = _sec_headers() if sec else {"User-Agent": "Traccia/1.0"}
    headers["Accept"] = "application/json, text/html, text/plain;q=0.9, */*;q=0.5"
    if sec:
        with _sec_lock:
            wait = 0.12 - (time.monotonic() - _last_sec_request)
            if wait > 0:
                time.sleep(wait)
            _last_sec_request = time.monotonic()
    req = Request(url, headers=headers)
    with urlopen(req, timeout=18) as response:
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


def _recent_filings(cik: str) -> tuple[dict, list[dict]]:
    submissions = json.loads(_fetch(SEC_SUBMISSIONS_URL.format(cik=cik)))
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
    text = html.unescape(" ".join(" ".join(parser.parts).split()))
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
    """An evidence-quality heuristic, explicitly not a probability of a tie."""
    if not evidence:
        return min(68, 30 + filing_count * 7)
    explicit = sum(bool(item["relation_terms"]) for item in evidence)
    distinct_filings = len({item["filing_url"] for item in evidence})
    return min(88, 38 + min(explicit, 3) * 9 + min(distinct_filings - 1, 2) * 6
               + (4 if model_used else 0))


def analyze_company(company: str, cik: str) -> dict:
    global _embedder, _spacy_nlp, _spacy_checked
    if not re.fullmatch(r"\d{10}", cik):
        raise ValueError("CIK non valido.")
    metadata, filings = _recent_filings(cik)
    company_name = metadata.get("name") or company
    documents: list[dict] = []
    chunks: list[dict] = []
    for filing in filings:
        if not filing.get("document") or not filing.get("accession"):
            continue
        if not filing["document"].lower().endswith((".htm", ".html", ".txt")):
            continue
        url = _filing_url(cik, filing)
        try:
            raw = _fetch(url, max_bytes=MAX_FILING_BYTES)
            text = _html_to_text(raw)
            if len(text) < 300:
                continue
            source = {
                "form": filing["form"],
                "date": filing["date"],
                "description": filing.get("description", ""),
                "filing_url": url,
            }
            documents.append(source)
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
    if evidence and any(item["candidate"] for item in evidence):
        verdict = "Possibile relazione: revisione analista necessaria"
        explanation = (
            "Sono state trovate menzioni di Israele vicine a termini di relazione nei filing "
            "consultati. La corrispondenza lessicale non prova da sola una relazione societaria: "
            "verifica il passaggio originale prima di concludere."
        )
    elif evidence:
        verdict = "Menzione trovata, relazione non stabilita"
        explanation = (
            "I filing contengono menzioni di Israele, ma i passaggi recuperati non mostrano "
            "un termine di relazione riconosciuto. Questo risultato non dimostra un legame."
        )
    elif documents:
        verdict = "Nessuna evidenza trovata nel perimetro consultato"
        explanation = (
            "Non sono state trovate menzioni pertinenti nei filing recuperati. La ricerca "
            "copre solo i documenti SEC indicati sotto e non prova l’assenza di rapporti."
        )
    else:
        verdict = "Analisi incompleta"
        explanation = "Non è stato possibile recuperare filing analizzabili per questa società."
    score = _evidence_score(evidence, len(documents), vector_used)
    return {
        "company": company_name,
        "cik": cik,
        "ticker": metadata.get("tickers", [""])[0] if metadata.get("tickers") else "",
        "verdict": verdict,
        "explanation": explanation,
        "confidence": score,
        "confidence_label": "qualità/pertinenza del recupero, non probabilità di relazione",
        "source_count": len(documents),
        "evidence_count": len(evidence),
        "documents": documents,
        "evidence": evidence,
        "retrieval": {
            "bm25": True,
            "faiss": vector_used,
            "embedding_model": EMBEDDING_MODEL if vector_used else None,
        },
        "scope": "Ultimi filing SEC recuperabili tra 10-K, 20-F, 8-K e 6-K; ricerca lessicale e vettoriale in inglese.",
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
    return render_template("index.html", access_key_required=bool(APP_ACCESS_KEY))


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
    try:
        return jsonify({"results": find_companies(query)})
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        return jsonify({"error": f"Ricerca SEC non disponibile: {type(error).__name__}."}), 502
    except ValueError as error:
        return jsonify({"error": f"Risposta SEC non utilizzabile: {error}"}), 502


@app.post("/api/analyze")
def analyze():
    global _embedder, _spacy_nlp, _spacy_checked
    if not _authorized():
        return jsonify({"error": "Chiave di accesso non valida."}), 401
    if _rate_limited(6, 3600):
        return jsonify({"error": "Limite gratuito: massimo 6 analisi per ora da questo indirizzo."}), 429
    payload = request.get_json(silent=True) or {}
    company = str(payload.get("company", ""))[:160].strip()
    cik = str(payload.get("cik", ""))[:10].strip().zfill(10)
    if not company or not re.fullmatch(r"\d{10}", cik):
        return jsonify({"error": "Seleziona una società SEC prima di avviare l’analisi."}), 400
    if not _analysis_lock.acquire(blocking=False):
        return jsonify({"error": "Un’analisi è già in corso. Riprova tra poco."}), 429
    try:
        result = analyze_company(company, cik)
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
