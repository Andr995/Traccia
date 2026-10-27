"""Low-memory discovery of country-local industry peers via Wikidata + BM25."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from company_identity import (
    company_key as _company_key, normalize_name as _norm, public_link,
    business_description, unrelated_description,
)


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
FAILURE_CACHE_SECONDS = 60
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_STOP_WORDS = set("""
the and for with from that this were was are has have had its into their they
will would could should about after before under over between across company
companies business businesses group corporation limited italian french german
spanish united states producer manufacturer industry of to in on by is at a an
di del della dei delle e la il le per con azienda gruppo produttore società
""".split())


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
            inverse = math.log1p(max(
                0.0,
                (len(tokenized) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5),
            ))
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
    payload = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Risposta Wikidata non valida.")
    return payload


def _resolve_entity(company: str, entity_id: str = "") -> str:
    if re.fullmatch(r"Q[1-9]\d*", str(entity_id or "")):
        return entity_id
    params = {
        "action": "wbsearchentities", "search": company[:100], "language": "en",
        "format": "json", "formatversion": "2", "type": "item", "limit": "6",
    }
    payload = _fetch_json(f"{WIKIDATA_API_URL}?{urlencode(params)}", timeout=6)
    query_key = _company_key(company)
    rows = payload.get("search")
    if not isinstance(rows, list):
        raise ValueError("Risultati di ricerca Wikidata non validi.")
    matches = set()
    exact_labels = set()
    exact_products = set()
    businesses = set()
    for row in rows[:6]:
        if not isinstance(row, dict):
            continue
        if unrelated_description(row.get("description", "")):
            continue
        entity_id = str(row.get("id", ""))
        matched = row.get("match") or {}
        names = [row.get("label", ""), matched.get("text", "") if isinstance(matched, dict) else ""]
        if _norm(company) == _norm(row.get("label", "")) and re.fullmatch(r"Q[1-9]\d*", entity_id):
            exact_labels.add(entity_id)
            if re.search(r"\b(?:brand|drink|confectionery|product|chocolate|footwear)\b", str(row.get("description", "")), re.I):
                exact_products.add(entity_id)
        if query_key and query_key in {_company_key(name) for name in names} and re.fullmatch(r"Q[1-9]\d*", entity_id):
            matches.add(entity_id)
            if business_description(row.get("description", "")):
                businesses.add(entity_id)
    # Do not silently choose the first of several homonymous entities.
    if len(exact_products) == 1:
        return next(iter(exact_products))
    if businesses:
        return next(iter(businesses)) if len(businesses) == 1 else ""
    if len(exact_labels) == 1:
        return next(iter(exact_labels))
    return next(iter(matches)) if len(matches) == 1 else ""


def _entity(entity_id: str) -> dict:
    payload = _fetch_json(ENTITY_DATA_URL.format(entity_id=entity_id), timeout=8)
    entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
    entity = entities.get(entity_id, {}) if isinstance(entities, dict) else {}
    if not isinstance(entity, dict) or "missing" in entity or not isinstance(entity.get("claims"), dict):
        raise ValueError("Scheda Wikidata non disponibile.")
    return entity


def _claim_ids(entity: dict, property_id: str) -> list[str]:
    claims = entity.get("claims") or {}
    statements = claims.get(property_id, []) if isinstance(claims, dict) else []
    ids = []
    for statement in statements if isinstance(statements, list) else []:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        qualifiers = statement.get("qualifiers") or {}
        if isinstance(qualifiers, dict) and qualifiers.get("P582"):
            continue  # Bounded conservative fallback: skip dated/historical relationships.
        snak = statement.get("mainsnak") or {}
        data = (snak.get("datavalue") or {}) if isinstance(snak, dict) else {}
        value = data.get("value") if isinstance(data, dict) else None
        qid = str(value.get("id", "")) if isinstance(value, dict) else ""
        if re.fullmatch(r"Q[1-9]\d*", qid) and qid not in ids:
            ids.append(qid)
    return ids[:MAX_INDUSTRIES]


def _localized(entity: dict, field: str) -> str:
    values = entity.get(field) or {}
    if not isinstance(values, dict):
        return ""
    for language in ("en", "it"):
        value = values.get(language)
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return value["value"][:300]
    return ""


def _peer_query(target_id: str, country_qid: str, industry_ids: list[str], excluded_ids: list[str], product_ids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in industry_ids)
    excluded = ", ".join(f"wd:{qid}" for qid in sorted(set([target_id, *excluded_ids])))
    product_filter = ""
    if product_ids:
        products = " ".join(f"wd:{qid}" for qid in product_ids)
        product_filter = f"""
  FILTER EXISTS {{
    VALUES ?targetProduct {{ {products} }}
    ?company wdt:P1056 ?output .
    ?output wdt:P279* ?targetProduct .
  }}
"""
    return f"""
SELECT DISTINCT ?company ?companyLabel ?descriptionEn ?descriptionIt ?website ?industryLabel ?parent ?parentLabel WHERE {{
  {{ SELECT DISTINCT ?company ?industry ?website WHERE {{
  VALUES ?industry {{ {values} }}
  ?company wdt:P452 ?industry ; wdt:P856 ?website .
  FILTER(?company NOT IN ({excluded}))
  FILTER NOT EXISTS {{ ?company (wdt:P749|wdt:P127) ?owner . FILTER(?owner IN ({excluded})) }}
  {{ ?company wdt:P17 wd:{country_qid} . }} UNION {{
    ?company wdt:P159 ?headquarters .
    ?headquarters wdt:P17 wd:{country_qid} .
  }}
  }} ORDER BY ?company LIMIT {MAX_CANDIDATES} }}
  {product_filter}
  OPTIONAL {{ ?company schema:description ?descriptionEn . FILTER(LANG(?descriptionEn) = "en") }}
  OPTIONAL {{ ?company schema:description ?descriptionIt . FILTER(LANG(?descriptionIt) = "it") }}
  OPTIONAL {{ ?company (wdt:P749|wdt:P127) ?parent . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "it,en". }}
}}
ORDER BY ?company ?parent
LIMIT {MAX_CANDIDATES * 4}
""".strip()


def _binding(row: dict, key: str) -> str:
    value = row.get(key)
    return str(value.get("value") or "").strip() if isinstance(value, dict) else ""


def _discover(company: str, country_code: str, entity_id: str, category_ids: tuple[str, ...] = ()) -> dict:
    code = str(country_code or "IT").upper()
    country_qid = COUNTRY_QIDS.get(code)
    if not country_qid:
        return {"items": [], "status": "paese non disponibile nel catalogo automatico"}
    key = (entity_id or _norm(company), code, category_ids)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        ttl = FAILURE_CACHE_SECONDS if cached and cached[1].get("retryable") else CACHE_TTL_SECONDS
        if cached and now - cached[0] < ttl:
            return {**cached[1], "cache_hit": True}
    metadata = {}
    try:
        resolved_id = _resolve_entity(company, entity_id)
        if not resolved_id:
            return {"items": [], "status": "Identità assente o ambigua: cerca e seleziona la scheda Wikidata dell’azienda.", "retryable": True}
        target = _entity(resolved_id)
        profile = target
        profile_id = resolved_id
        industries = _claim_ids(target, "P452")
        related = list(dict.fromkeys(
            _claim_ids(target, "P749") + _claim_ids(target, "P127") + _claim_ids(target, "P176")
        ))
        # A brand/product often has no industry. Follow a documented manufacturer
        # or owner for discovery only; keep the report identity unchanged.
        if not industries:
            for related_id in related[:2]:
                candidate_profile = _entity(related_id)
                candidate_industries = _claim_ids(candidate_profile, "P452")
                if candidate_industries:
                    profile, industries = candidate_profile, candidate_industries
                    profile_id = related_id
                    break
        target_description = _localized(profile, "descriptions")
        related = list(dict.fromkeys(related + _claim_ids(profile, "P749") + _claim_ids(profile, "P127")))
        metadata = {
            "entity_id": resolved_id,
            "entity_url": f"https://www.wikidata.org/wiki/{resolved_id}",
            "profile_url": f"https://www.wikidata.org/wiki/{profile_id}",
            "profile_name": _localized(profile, "labels") or company,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        if not industries:
            result = {"items": [], "status": "Wikidata non riporta un settore (P452) per l’azienda"}
        else:
            excluded_ids = [resolved_id, profile_id, *related]
            # A named product (e.g. a cola) must not inherit every product line
            # of its manufacturer (e.g. alcoholic beverages).
            products = list(category_ids) or (_claim_ids(target, "P279") if profile_id != resolved_id else _claim_ids(profile, "P1056"))
            params = {"query": _peer_query(resolved_id, country_qid, industries, excluded_ids, products), "format": "json"}
            payload = _fetch_json(
                f"{WDQS_URL}?{urlencode(params)}",
                accept="application/sparql-results+json",
                timeout=15,
            )
            results = payload.get("results")
            rows = results.get("bindings") if isinstance(results, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Risposta del servizio di confronto non valida.")
            by_id = {}
            for row in rows[:MAX_CANDIDATES * 4]:
                if not isinstance(row, dict):
                    continue
                uri = _binding(row, "company")
                match = re.search(r"/(Q[1-9]\d*)$", uri)
                candidate_id = match.group(1) if match else ""
                name = _binding(row, "companyLabel")
                website = public_link(_binding(row, "website"))
                if (not candidate_id or candidate_id in excluded_ids or not name or name == candidate_id
                        or _company_key(name) == _company_key(company) or not website):
                    continue
                candidate = by_id.setdefault(candidate_id, {
                    "id": candidate_id,
                    "name": name[:140],
                    "description": (_binding(row, "descriptionEn") or _binding(row, "descriptionIt"))[:260],
                    "website": website[:500],
                    "industry": _binding(row, "industryLabel")[:140] or "settore correlato",
                    "wikidata_url": f"https://www.wikidata.org/wiki/{candidate_id}",
                    "parents": [],
                    "ownership_incomplete": False,
                })
                parent_uri = _binding(row, "parent")
                parent_name = _binding(row, "parentLabel")
                if parent_uri:
                    if not parent_name or re.fullmatch(r"Q\d+", parent_name):
                        candidate["ownership_incomplete"] = True
                    elif parent_name not in candidate["parents"]:
                        candidate["parents"].append(parent_name[:140])
            candidates = list(by_id.values())[:MAX_CANDIDATES]
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
                "status": f"{len(items)} imprese dello stesso settore Wikidata nel paese selezionato" + (" e con una categoria di prodotto in comune" if products else "; prodotti non confrontabili per dati mancanti"),
                "product_comparison": bool(products),
            }
        result.update(metadata)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        result = {**metadata, "items": [], "status": "Ricerca Wikidata temporaneamente non disponibile; puoi riprovare tra un minuto.", "retryable": True}
    with _cache_lock:
        if len(_cache) >= 128:
            _cache.clear()
        _cache[key] = (time.monotonic(), result)
    return result


def get_automatic_alternatives(
    company: str, country_code: str, entity_id: str = "", limit: int = 3,
    category_ids: tuple[str, ...] = (),
) -> dict:
    """Find local same-industry peers; these are leads, not cleared alternatives."""
    if limit <= 0:
        return {"items": [], "status": "limite di suggerimenti raggiunto", "excluded_count": 0}
    discovery = _discover(company, country_code, entity_id, category_ids)
    selected = []
    excluded_count = 0
    from signal_sources import screen_names

    for candidate in discovery.get("items", []):
        screened = screen_names([candidate["name"], *candidate.get("parents", [])])
        if screened.get("matched"):
            excluded_count += 1
            continue
        providers = screened.get("providers", [])
        lists_checked = screened["complete"]
        selected.append({
            "brand": candidate["name"],
            "product": "Categoria di prodotto in comune" if discovery.get("product_comparison") else "Stesso settore · prodotto da verificare",
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
            "ownership_note": (
                "Nomi societari collegati controllati: " + ", ".join(candidate["parents"]) + ". Controllo limitato ai collegamenti riportati da Wikidata."
                if candidate.get("parents") else "Wikidata non riporta nomi di proprietari utilizzabili: struttura societaria non verificata."
            ) + (" Alcuni proprietari non sono identificabili per nome." if candidate.get("ownership_incomplete") else ""),
            "screening_status": (
                "Nessuna segnalazione trovata nelle liste consultate; questo non esclude altri legami."
                if lists_checked else "Screening delle liste incompleto; legami non verificati."
            ),
            "screening_complete": lists_checked,
            "screening_sources": providers,
        })
        if len(selected) >= limit:
            break
    return {
        "items": selected,
        "status": discovery.get("status", "ricerca automatica non disponibile"),
        "entity_id": discovery.get("entity_id", ""),
        "entity_url": discovery.get("entity_url", ""),
        "profile_url": discovery.get("profile_url", ""),
        "profile_name": discovery.get("profile_name", ""),
        "retrieved_at": discovery.get("retrieved_at", ""),
        "retryable": bool(discovery.get("retryable")),
        "excluded_count": excluded_count,
    }
