"""Low-memory discovery of country-local industry peers via Wikidata + BM25."""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ENTITY_DATA_URL = "https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json?flavor=simple"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WDQS_URL = "https://query.wikidata.org/sparql"
COUNTRY_QIDS = {
    "IT": "Q38", "FR": "Q142", "DE": "Q183", "ES": "Q29",
    "GB": "Q145", "US": "Q30",
}
COUNTRY_NAMES = {
    "IT": "Italia", "FR": "Francia", "DE": "Germania",
    "ES": "Spagna", "GB": "Regno Unito", "US": "Stati Uniti",
}
MAX_INDUSTRIES = 4
MAX_CANDIDATES = 40
CACHE_TTL_SECONDS = 6 * 60 * 60
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_STOP_WORDS = set("""
the and for with from that this were was are has have had its into their they
will would could should about after before under over between across company
companies business businesses group corporation limited italian french german
spanish united states producer manufacturer industry of to in on by is at a an
di del della dei delle e la il le per con azienda gruppo produttore società
""".split())
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "company", "co", "plc",
    "ltd", "limited", "llc", "llp", "sa", "spa", "srl", "sas", "sarl",
    "nv", "ag", "gmbh",
    "group", "holdings", "holding",
}


def _norm(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _company_key(value: str) -> str:
    tokens = _norm(value).split()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while tokens:
        if len(tokens) >= 3 and tuple(tokens[-3:]) in {("s", "p", "a"), ("s", "r", "l")}:
            tokens = tokens[:-3]
        elif len(tokens) >= 2 and tuple(tokens[-2:]) in {("s", "a"), ("b", "v"), ("s", "c")}:
            tokens = tokens[:-2]
        elif tokens[-1] in _LEGAL_SUFFIXES:
            tokens.pop()
        else:
            break
    return " ".join(tokens)


def _tokens(value: str) -> list[str]:
    return [token for token in _norm(value).split() if len(token) > 2 and token not in _STOP_WORDS]


def _bm25(query: str, documents: list[str]) -> list[float]:
    """Small BM25 ranker; no model download and negligible memory overhead."""
    query_tokens = list(dict.fromkeys(_tokens(query)))
    tokenized = [_tokens(document) for document in documents]
    if not query_tokens or not tokenized:
        return [0.0] * len(documents)
    average_length = sum(map(len, tokenized)) / max(len(tokenized), 1)
    frequencies = [Counter(tokens) for tokens in tokenized]
    document_frequency = Counter(token for tokens in tokenized for token in set(tokens))
    scores = []
    for tokens, counts in zip(tokenized, frequencies):
        score = 0.0
        for term in query_tokens:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse = 1.0 + max(
                0.0,
                (len(tokenized) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5),
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * len(tokens) / max(average_length, 1)
            )
            score += inverse * frequency * 2.5 / denominator
        scores.append(score)
    return scores


def _fetch_json(url: str, *, accept: str = "application/json", timeout: int = 8) -> dict:
    headers = {
        "Accept": accept,
        "User-Agent": "Traccia/1.0 (public-source research; low-volume, cached requests)",
    }
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("Risposta Wikidata oltre il limite configurato.")
    return json.loads(body.decode("utf-8", errors="replace"))


def _resolve_entity(company: str, entity_id: str = "") -> str:
    if re.fullmatch(r"Q[1-9]\d*", str(entity_id or "")):
        return entity_id
    params = {
        "action": "wbsearchentities", "search": company[:100], "language": "en",
        "format": "json", "formatversion": "2", "type": "item", "limit": "6",
    }
    try:
        payload = _fetch_json(f"{WIKIDATA_API_URL}?{urlencode(params)}", timeout=6)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return ""
    query_key = _company_key(company)
    for row in payload.get("search", [])[:6] if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        entity_id = str(row.get("id", ""))
        label_key = _company_key(str(row.get("label", "")))
        if query_key and query_key == label_key and re.fullmatch(r"Q[1-9]\d*", entity_id):
            return entity_id
    return ""


def _industry_ids(entity_id: str) -> tuple[list[str], str]:
    payload = _fetch_json(ENTITY_DATA_URL.format(entity_id=entity_id), timeout=8)
    entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
    entity = entities.get(entity_id, {}) if isinstance(entities, dict) else {}
    claims = entity.get("claims", {}) if isinstance(entity, dict) else {}
    industries = []
    for statement in claims.get("P452", []) if isinstance(claims, dict) else []:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        value = statement.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        qid = str(value.get("id", "")) if isinstance(value, dict) else ""
        if re.fullmatch(r"Q[1-9]\d*", qid) and qid not in industries:
            industries.append(qid)
        if len(industries) >= MAX_INDUSTRIES:
            break
    descriptions = entity.get("descriptions", {}) if isinstance(entity, dict) else {}
    description = str(
        (descriptions.get("en") or descriptions.get("it") or {}).get("value", "")
    )[:300] if isinstance(descriptions, dict) else ""
    return industries, description


def _peer_query(target_id: str, country_qid: str, industry_ids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in industry_ids)
    return f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?website ?industryLabel WHERE {{
  VALUES ?industry {{ {values} }}
  ?company wdt:P452 ?industry ; wdt:P856 ?website .
  FILTER(?company != wd:{target_id})
  FILTER NOT EXISTS {{ ?company wdt:P749 wd:{target_id} . }}
  {{ ?company wdt:P17 wd:{country_qid} . }} UNION {{
    ?company wdt:P159 ?headquarters .
    ?headquarters wdt:P17 wd:{country_qid} .
  }}
  OPTIONAL {{ ?company schema:description ?companyDescription .
    FILTER(LANG(?companyDescription) = "it" || LANG(?companyDescription) = "en") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "it,en". }}
}}
LIMIT {MAX_CANDIDATES}
""".strip()


def _discover(company: str, country_code: str, entity_id: str) -> dict:
    code = str(country_code or "IT").upper()
    country_qid = COUNTRY_QIDS.get(code)
    if not country_qid:
        return {"items": [], "status": "paese non disponibile nel catalogo automatico"}
    resolved_id = _resolve_entity(company, entity_id)
    if not resolved_id:
        return {"items": [], "status": "identità Wikidata non risolta con corrispondenza esatta"}
    key = (resolved_id, code)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return {**cached[1], "cache_hit": True}
    try:
        industries, target_description = _industry_ids(resolved_id)
        if not industries:
            result = {"items": [], "status": "Wikidata non riporta un settore (P452) per l’azienda"}
        else:
            params = {"query": _peer_query(resolved_id, country_qid, industries), "format": "json"}
            payload = _fetch_json(
                f"{WDQS_URL}?{urlencode(params)}",
                accept="application/sparql-results+json",
                timeout=10,
            )
            rows = payload.get("results", {}).get("bindings", [])
            candidates = []
            seen = set()
            for row in rows[:MAX_CANDIDATES] if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                uri = str(row.get("company", {}).get("value", ""))
                match = re.search(r"/(Q[1-9]\d*)$", uri)
                candidate_id = match.group(1) if match else ""
                name = str(row.get("companyLabel", {}).get("value", "")).strip()
                website = str(row.get("website", {}).get("value", "")).strip()
                if (not candidate_id or candidate_id in seen or not name
                        or not website.startswith(("https://", "http://"))):
                    continue
                seen.add(candidate_id)
                candidates.append({
                    "id": candidate_id,
                    "name": name[:140],
                    "description": str(row.get("companyDescription", {}).get("value", ""))[:260],
                    "website": website[:500],
                    "industry": str(row.get("industryLabel", {}).get("value", "settore correlato"))[:140],
                    "wikidata_url": f"https://www.wikidata.org/wiki/{candidate_id}",
                })
            industry_labels = list(dict.fromkeys(item["industry"] for item in candidates))
            target_text = f"{company} {target_description} {' '.join(industry_labels)}"
            scores = _bm25(target_text, [
                f"{item['name']} {item['description']} {item['industry']}" for item in candidates
            ])
            ranked = sorted(zip(candidates, scores), key=lambda pair: (-pair[1], pair[0]["name"].casefold()))
            items = []
            max_score = max(scores, default=0.0)
            for candidate, score in ranked:
                items.append({
                    **candidate,
                    "similarity_score": round(100 * score / max_score) if max_score else 0,
                })
            result = {
                "items": items,
                "status": f"{len(items)} imprese dello stesso settore Wikidata nel paese selezionato",
                "entity_id": resolved_id,
                "entity_url": f"https://www.wikidata.org/wiki/{resolved_id}",
            }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        result = {"items": [], "status": "ricerca Wikidata temporaneamente non disponibile"}
    with _cache_lock:
        if len(_cache) >= 128:
            _cache.clear()
        _cache[key] = (now, result)
    return result


def get_automatic_alternatives(
    company: str, country_code: str, entity_id: str = "", limit: int = 3
) -> dict:
    """Find local same-industry peers; these are leads, not cleared alternatives."""
    if limit <= 0:
        return {"items": [], "status": "limite di suggerimenti raggiunto", "excluded_count": 0}
    discovery = _discover(company, country_code, entity_id)
    selected = []
    excluded_count = 0
    from signal_sources import get_initial_signal

    for candidate in discovery.get("items", []):
        screened = get_initial_signal(candidate["name"])
        if screened.get("matched"):
            excluded_count += 1
            continue
        providers = screened.get("providers", [])
        lists_checked = len(providers) >= 2 and all(
            bool(provider.get("available")) for provider in providers
        )
        selected.append({
            "brand": candidate["name"],
            "product": "Impresa della stessa categoria · non verifica un prodotto specifico",
            "origin": f"Paese associato su Wikidata: {COUNTRY_NAMES.get(country_code, 'paese selezionato')}",
            "description": (
                f"Wikidata associa questa impresa al settore {candidate['industry']} e al paese selezionato. "
                "È un candidato automatico, non una conferma che venda un prodotto concorrente o che sia disponibile localmente: verifica il sito e la struttura societaria."
            ),
            "product_url": candidate["website"],
            "source_label": "Sito associato nel record Wikidata · da verificare",
            "category_url": candidate["wikidata_url"],
            "category_label": f"Scheda e settore Wikidata: {candidate['industry']}",
            "similarity_score": candidate["similarity_score"],
            "automatic": True,
            "screening_status": (
                "Nessuna segnalazione trovata nelle liste consultate; questo non esclude altri legami."
                if lists_checked else "Screening delle liste incompleto; legami non verificati."
            ),
            "screening_complete": lists_checked,
            "screening_sources": [
                {"source": provider.get("source", "Fonte lista"),
                 "source_url": provider.get("source_url", ""),
                 "status": provider.get("status", "non disponibile")}
                for provider in providers
            ],
        })
        if len(selected) >= limit:
            break
    return {
        "items": selected,
        "status": discovery.get("status", "ricerca automatica non disponibile"),
        "entity_id": discovery.get("entity_id", ""),
        "entity_url": discovery.get("entity_url", ""),
        "excluded_count": excluded_count,
    }
