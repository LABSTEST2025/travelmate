"""Vérifications déterministes réutilisables par les évaluations de nœuds."""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def check(name: str, passed: bool, detail: str = "") -> Check:
    return Check(name, bool(passed), "" if passed else detail)


def normalize(name: str) -> str:
    """« Viêt Nam », « vietnam », « Viet-Nam » -> « vietnam »."""
    text = unicodedata.normalize("NFKD", name.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\s'’\-.]", "", text)


# Codes CLDR qui ne sont pas des pays.
_NOT_COUNTRIES = {"ZZ", "EU", "EZ", "UN", "QO", "XA", "XB", "AQ"}
_ALIASES = {"Birmanie", "République tchèque", "Hollande", "Corée", "USA", "Angleterre", "Écosse", "Pays de Galles"}


@lru_cache(maxsize=1)
def known_countries() -> frozenset[str]:
    """Noms de pays en français et en anglais (CLDR, via Babel), normalisés."""
    from babel import Locale

    names: set[str] = set(_ALIASES)
    for locale in ("fr", "en"):
        for code, name in Locale(locale).territories.items():
            if len(code) == 2 and code.isalpha() and code not in _NOT_COUNTRIES:
                names.add(name)
                # « Myanmar (Birmanie) » : les deux formes sont acceptées.
                names.update(part.strip(" )") for part in name.split("("))
    return frozenset(normalize(n) for n in names)


def is_country(name: str) -> bool:
    return normalize(name) in known_countries()


def same_place(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def in_list(name: str, references: list[str]) -> bool:
    return any(same_place(name, ref) for ref in references)


def mentions_any(text: str, references: list[str]) -> bool:
    """« La Canée (Chania), Crète » mentionne « Chania » : le sens, indépendamment du format."""
    return any(normalize(ref) in normalize(text) for ref in references)


_FR_WORDS = {
    "le", "la", "les", "de", "des", "du", "et", "un", "une", "en", "au", "aux", "pour", "avec", "sur",
    "dans", "est", "à", "par", "vous", "votre", "très", "plus", "visite", "ville",
}
_EN_WORDS = {
    "the", "and", "of", "to", "in", "for", "with", "on", "is", "a", "an", "your", "visit", "city", "very",
}


def looks_french(text: str) -> bool:
    """Heuristique : plus de mots-outils français qu'anglais. Un texte sans aucun indice passe."""
    words = re.findall(r"[^\W\d_]+", text.casefold())
    fr = sum(w in _FR_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    return fr >= en


def word_count(text: str) -> int:
    return len(text.split())


def sentence_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()])


def mentions_amount(text: str, amount: float) -> bool:
    """Le montant (ex. 1 250 EUR) apparaît dans le texte, avec ou sans séparateur de milliers."""
    digits = re.sub(r"[\s  .,]", "", text)
    return f"{amount:.0f}" in digits
