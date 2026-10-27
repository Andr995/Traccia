"""Shared, conservative normalization for company and brand names."""

import re
import unicodedata
from urllib.parse import urlsplit


def business_description(value: str) -> bool:
    return bool(re.search(
        r"\b(?:company|corporation|business|manufacturer|brand|retailer|bank|"
        r"multinational|enterprise|producer|azienda|impresa|societa|marchio|produttore)\b",
        normalize_name(value),
    ))


def unrelated_description(value: str) -> bool:
    return bool(re.search(
        r"\b(?:surname|family name|given name|noble family|painting|academic journal|"
        r"human settlement|disambiguation page|Wikimedia category)\b",
        str(value or ""), re.I,
    ))


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    # Preserve non-Latin company names instead of silently turning them empty.
    return re.sub(r"[\W_]+", " ", folded).strip()


def company_key(value: str) -> str:
    tokens = normalize_name(value).split()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    suffixes = {
        "inc", "incorporated", "corp", "corporation", "company", "co", "plc",
        "ltd", "limited", "llc", "llp", "sa", "spa", "srl", "sas", "sarl",
        "nv", "ag", "gmbh", "group", "holdings", "holding",
    }
    while tokens:
        if tuple(tokens[-3:]) in {("s", "p", "a"), ("s", "r", "l")}:
            tokens = tokens[:-3]
        elif tuple(tokens[-2:]) in {("s", "a"), ("b", "v")}:
            tokens = tokens[:-2]
        elif tokens[-1] in suffixes:
            tokens.pop()
        else:
            break
    return " ".join(tokens)


def public_link(value: str) -> str:
    """Validate external citation links (never used as fetch authorization)."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return value.strip()
    except ValueError:
        pass
    return ""
