"""Curated and discovered candidates; absence from signal lists is not clearance."""

from __future__ import annotations

from company_identity import company_key as _company_key, normalize_name as _normalize
from signal_sources import screen_names
from automatic_alternatives import get_automatic_alternatives


COUNTRIES = {
    "IT": "Italia",
    "FR": "Francia",
    "DE": "Germania",
    "ES": "Spagna",
    "GB": "Regno Unito",
    "US": "Stati Uniti",
}

# This is a small, cited starter catalog. Suggestions are alternatives by
# product category, never a certification that a producer has no Israel ties.
ALTERNATIVES = [
    {
        "target_aliases": {
            "coca cola", "coca cola company", "the coca cola company",
            "coca cola co", "coca cola company inc",
            "coca cola hbc", "coca cola hbc ag", "coca cola europacific partners",
            "coca cola consolidated", "coca cola bottling company",
        },
        "country_code": "IT",
        "brand": "Tomarchio",
        "comparison_category": "Q134041",  # Cola: https://www.wikidata.org/wiki/Q134041
        "screening_names": ["Tomarchio", "Sibat Tomarchio"],
        "product": "Cola",
        "origin": "Italia · Acireale, Sicilia",
        "description": "Bibita cola prodotta e confezionata da Sibat Tomarchio S.r.l.",
        "product_url": "https://tomarchiobibite.it/product/cola-glass-275-ml/?lang=en",
        "source_label": "Scheda prodotto ufficiale Tomarchio",
    },
    {
        "target_aliases": {
            "coca cola", "coca cola company", "the coca cola company",
            "coca cola co", "coca cola company inc",
        },
        "country_code": "IT",
        "brand": "Molecola",
        "comparison_category": "Q134041",
        "product": "Classica",
        "origin": "Italia · Torino / produzione e imbottigliamento in Italia",
        "description": "Il produttore presenta Molecola Classica come cola italiana e dichiara produzione e imbottigliamento in Italia.",
        "product_url": "https://molecolaitalia.it/molecola-classica/",
        "source_label": "Scheda ufficiale Molecola Classica",
    },
    {
        "target_aliases": {
            "ferrero", "ferrero group", "ferrero international",
            "ferrero international s a", "ferrero s p a",
        },
        "country_code": "IT",
        "brand": "Novi",
        "screening_names": ["Novi", "Elah Dufour Novi"],
        "product": "Cioccolato Novi",
        "origin": "Italia · Novi Ligure / Genova",
        "description": "Il gruppo Elah Dufour Novi indica Novi tra i propri marchi di cioccolato e dichiara che i suoi stabilimenti sono in Italia.",
        "product_url": "https://www.elah-dufour.it/il-gruppo",
        "source_label": "Profilo ufficiale Elah Dufour Novi",
    },
    {
        "target_aliases": {
            "ferrero", "ferrero group", "ferrero international",
            "ferrero international s a", "ferrero s p a",
        },
        "country_code": "IT",
        "brand": "Venchi",
        "product": "Tavoletta Mandorlata Fondente",
        "origin": "Italia · azienda fondata a Torino",
        "description": "Alternativa nella categoria cioccolato; il produttore documenta il prodotto e la propria storia italiana.",
        "product_url": "https://it.venchi.com/products/tavoletta-mandorlata-cioccolato-fondente",
        "source_label": "Prodotto ufficiale Venchi",
    },
]


def get_alternatives(company: str, country_code: str, entity_id: str = "", *, include_automatic: bool = True) -> dict:
    """Return curated alternatives and automatically discovered local industry peers."""
    code = str(country_code or "IT").upper()
    if code not in COUNTRIES:
        raise ValueError("Paese non supportato.")
    normalized = {_normalize(company), _company_key(company)} - {""}
    matches = []
    for item in ALTERNATIVES:
        aliases = {
            key
            for alias in item["target_aliases"]
            for key in (_normalize(alias), _company_key(alias))
            if key
        }
        if normalized.intersection(aliases):
            matches.append(item)
    country_matches = [item for item in matches if item["country_code"] == code]
    items = []
    excluded_count = 0
    for item in country_matches:
        names = item.get("screening_names", [item["brand"]])
        screening = screen_names(names)
        if screening.get("matched"):
            excluded_count += 1
            continue
        providers = screening.get("providers", [])
        both_lists_checked = screening["complete"]
        items.append({
            "brand": item["brand"],
            "product": item["product"],
            "origin": item["origin"],
            "description": item["description"],
            "product_url": item["product_url"],
            "source_label": item["source_label"],
            "ownership_note": "Nomi controllati: " + ", ".join(names) + ". Struttura societaria completa non verificata.",
            "screening_status": (
                "Nessuna segnalazione trovata nelle liste consultate; questo non esclude altri legami."
                if both_lists_checked else
                "Screening delle liste incompleto; legami non verificati."
            ),
            "screening_complete": both_lists_checked,
            "screening_sources": providers,
        })

    curated_count = len(items)
    automatic = get_automatic_alternatives(
        company, code, entity_id, limit=max(0, 5 - curated_count),
        category_ids=tuple(dict.fromkeys(item["comparison_category"] for item in matches if item.get("comparison_category"))),
    ) if include_automatic else {"items": [], "status": "Ricerca automatica in attesa", "pending": True}
    seen_brands = {_normalize(item["brand"]) for item in items}
    for candidate in automatic.get("items", []):
        brand_key = _normalize(candidate["brand"])
        if brand_key and brand_key not in seen_brands:
            items.append(candidate)
            seen_brands.add(brand_key)

    applicable = bool(matches or items)

    return {
        "applicable": applicable,
        "country_code": code,
        "country_name": COUNTRIES.get(code, "Paese selezionato"),
        "target": company,
        "items": items,
        "excluded_count": excluded_count + automatic.get("excluded_count", 0),
        "automatic_status": automatic.get("status", "ricerca automatica non disponibile"),
        "automatic_entity_url": automatic.get("entity_url", ""),
        "automatic_profile_url": automatic.get("profile_url", ""),
        "automatic_profile_name": automatic.get("profile_name", ""),
        "automatic_retrieved_at": automatic.get("retrieved_at", ""),
        "pending": bool(automatic.get("pending")),
        "retryable": bool(automatic.get("retryable")),
        "catalog_note": (
            "Le schede ufficiali documentano prodotto o sito dell’impresa; Wikidata e BM25 aiutano a individuare aziende della stessa categoria. "
            "Sono candidati da controllare, non aziende certificate senza rapporti con Israele. Il controllo delle liste è limitato e non equivale a una verifica completa dei legami societari."
        ),
    }
