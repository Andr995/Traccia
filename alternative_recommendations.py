"""Curated, source-linked alternatives; absence from signal lists is not clearance."""

from __future__ import annotations

import re
import unicodedata

from signal_sources import get_initial_signal


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


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _company_key(value: str) -> str:
    words = _normalize(value).split()
    while words and words[0] == "the":
        words.pop(0)
    while words and words[-1] in {
        "inc", "incorporated", "corp", "corporation", "company", "co",
        "plc", "ltd", "limited", "llc", "llp", "sa", "nv", "ag",
    }:
        words.pop()
    return " ".join(words)


def get_alternatives(company: str, country_code: str) -> dict:
    """Return country-matched suggestions with product and signal-list provenance."""
    code = str(country_code or "IT").upper()
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
    if not matches:
        return {
            "applicable": False,
            "country_code": code,
            "country_name": COUNTRIES.get(code, "Paese selezionato"),
            "target": company,
            "items": [],
            "excluded_count": 0,
            "catalog_note": "Il catalogo è curato e parziale; un abbinamento assente non significa che non esistano alternative.",
        }

    country_matches = [item for item in matches if item["country_code"] == code]
    items = []
    excluded_count = 0
    for item in country_matches:
        screening = get_initial_signal(item["brand"])
        if screening.get("matched"):
            excluded_count += 1
            continue
        providers = screening.get("providers", [])
        both_lists_checked = len(providers) >= 2 and all(
            bool(provider.get("available")) for provider in providers
        )
        items.append({
            "brand": item["brand"],
            "product": item["product"],
            "origin": item["origin"],
            "description": item["description"],
            "product_url": item["product_url"],
            "source_label": item["source_label"],
            "screening_status": (
                "Nessuna segnalazione trovata nelle liste consultate; questo non esclude altri legami."
                if both_lists_checked else
                "Screening delle liste incompleto; legami non verificati."
            ),
            "screening_complete": both_lists_checked,
            "screening_sources": [
                {"source": provider.get("source", "Fonte lista"),
                 "source_url": provider.get("source_url", ""),
                 "status": provider.get("status", "non disponibile")}
                for provider in providers
            ],
        })

    return {
        "applicable": True,
        "country_code": code,
        "country_name": COUNTRIES.get(code, "Paese selezionato"),
        "target": company,
        "items": items,
        "excluded_count": excluded_count,
        "catalog_note": (
            "Le fonti ufficiali documentano prodotto e provenienza dichiarata, non l’assenza di rapporti con Israele. "
            "Il controllo delle liste è limitato e non equivale a una verifica completa dei legami societari."
        ),
    }
