"""Traces LangSmith sans budgets individuels (T60).

Les budgets restent privés sur le site : ils ne doivent pas non plus partir en clair dans les traces.
Le client LangSmith global reçoit un anonymiseur appliqué aux entrées, sorties et erreurs de chaque run
(état du graphe, prompts, requêtes Tavily). `LANGSMITH_REDACT_BUDGETS=false` le désactive (débogage local).
"""

import os
import re
from typing import Any

import langsmith

REDACTED = "[budget masqué]"

_BUDGET_KEYS = {"budget_eur"}
_AMOUNT = r"\d[\d  .,]*"
_BUDGET_PATTERNS = (
    # « Budget par personne, vols compris : 900 à 1500 EUR », « budget total de 1500 EUR »,
    # « dépasse le budget (2000 EUR) »
    re.compile(rf"(?i)(budget[^\d\n]{{0,40}}?){_AMOUNT}(?:\s*à\s*{_AMOUNT})?(?=\s*EUR)"),
    # Plafond d'hôtel : une part fixe de la somme des budgets.
    re.compile(rf"(ne doit pas dépasser ){_AMOUNT}(?=\s*EUR)"),
)


def _redact_text(text: str) -> str:
    for pattern in _BUDGET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED} ", text)
    return text


def redact_budgets(data: Any) -> Any:
    """Copie de `data` (JSON déjà sérialisé) sans montant de budget."""
    if isinstance(data, dict):
        return {key: REDACTED if key in _BUDGET_KEYS else redact_budgets(value) for key, value in data.items()}
    if isinstance(data, list):
        return [redact_budgets(item) for item in data]
    if isinstance(data, str):
        return _redact_text(data)
    return data


def configure_tracing() -> None:
    """À appeler au démarrage, une fois le `.env` chargé. Sans traçage LangSmith, ne fait rien."""
    if os.environ.get("LANGSMITH_TRACING", "").strip().lower() != "true":
        return
    if os.environ.get("LANGSMITH_REDACT_BUDGETS", "true").strip().lower() == "false":
        return
    langsmith.configure(client=langsmith.Client(anonymizer=redact_budgets))
