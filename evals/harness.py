"""Harnais des évaluations : vrai LLM, Tavily en cache disque, SerpApi jamais appelé.

Chaque appel structuré au LLM (`with_structured_retry(...).invoke`) est enregistré avec son prompt et sa
sortie brute : les vérifications portent sur ce que le modèle a répondu, avant les corrections du code
(troncature des pros / cons, bornes du niveau économique, filtrage des pays exclus…).
"""

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel

import travel_agent.llm as llm_module
from travel_agent.guardrails import input as guard_input
from travel_agent.state import TripPreferences, TripResponseModelGroup
from travel_agent.subgraphs import planning, suggestion
from travel_agent.tools import airports, search

CACHE_DIR = Path(__file__).parent / "cache" / "tavily"

# Modules qui importent `get_llm` et `with_structured_retry` par leur nom : on les remplace là où ils servent.
_LLM_MODULES = (suggestion, planning, airports, guard_input)


class TavilyCacheMiss(RuntimeError):
    pass


@dataclass
class StructuredCall:
    schema: str
    prompt: str
    output: BaseModel | None
    error: str | None
    seconds: float


@dataclass
class Recorder:
    """Appels d'une exécution de cas : appels structurés (logiques) et requêtes HTTP au modèle,
    relances comprises (une sortie invalide ou une erreur 429 fait une requête de plus)."""

    calls: list[StructuredCall] = field(default_factory=list)
    requests: int = 0
    request_errors: int = 0

    def outputs(self, schema: str) -> list[BaseModel]:
        return [c.output for c in self.calls if c.schema == schema and c.output is not None]

    def first(self, schema: str) -> BaseModel | None:
        outputs = self.outputs(schema)
        return outputs[0] if outputs else None


class _RequestCounter(BaseCallbackHandler):
    def __init__(self, recorder: Recorder):
        self.recorder = recorder

    def on_chat_model_start(self, *args, **kwargs) -> None:
        self.recorder.requests += 1

    def on_llm_error(self, *args, **kwargs) -> None:
        self.recorder.request_errors += 1


class _RecordedRunnable:
    def __init__(self, runnable, schema: type[BaseModel], recorder: Recorder):
        self.runnable, self.schema, self.recorder = runnable, schema, recorder

    def invoke(self, prompt, *args, **kwargs):
        start = time.perf_counter()
        try:
            output = self.runnable.invoke(prompt, *args, **kwargs)
        except Exception as exc:
            self.recorder.calls.append(
                StructuredCall(self.schema.__name__, str(prompt), None, repr(exc), time.perf_counter() - start)
            )
            raise
        self.recorder.calls.append(
            StructuredCall(self.schema.__name__, str(prompt), output, None, time.perf_counter() - start)
        )
        return output


def _cache_key(query: str, kwargs: dict) -> str:
    raw = json.dumps({"query": query, **kwargs}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cached_run(real_run, offline: bool):
    """Réponses Tavily mises en cache : deux exécutions voient les mêmes informations, seul le LLM varie."""

    def run(query: str, **kwargs) -> dict:
        path = CACHE_DIR / f"{_cache_key(query, kwargs)}.json"
        if path.exists():
            return json.loads(path.read_text())["response"]
        if offline:
            raise TavilyCacheMiss(f"Requête Tavily absente du cache : « {query} »")
        response = real_run(query, **kwargs)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"query": query, "response": response}, ensure_ascii=False, indent=1))
        return response

    return run


def _no_flights():
    raise RuntimeError("Les évaluations ne cherchent jamais de vols (quota SerpApi).")


@contextmanager
def instrumented(recorder: Recorder, *, offline: bool = False) -> Iterator[None]:
    """Branche l'enregistreur sur le LLM et le cache sur Tavily, le temps d'une exécution de cas."""
    real_get_llm = llm_module.get_llm
    real_structured = llm_module.with_structured_retry
    counter = _RequestCounter(recorder)

    def get_llm(**kwargs):
        return real_get_llm(callbacks=[counter], **kwargs)

    def with_structured_retry(llm, schema, **kwargs):
        return _RecordedRunnable(real_structured(llm, schema, **kwargs), schema, recorder)

    saved: list[tuple[Any, str, Any]] = []

    def patch(target, name: str, value) -> None:
        saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    try:
        for module in _LLM_MODULES:
            patch(module, "get_llm", get_llm)
            patch(module, "with_structured_retry", with_structured_retry)
        patch(search, "_run", _cached_run(search._run, offline))
        patch(planning, "get_flights_provider", _no_flights)
        airports.resolve_airports.cache_clear()
        yield
    finally:
        for target, name, value in reversed(saved):
            setattr(target, name, value)


def require_model(model: str | None) -> str:
    if model:
        os.environ["OPENROUTER_MODEL"] = model
    name = os.environ.get("OPENROUTER_MODEL")
    if not name or not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_MODEL et OPENROUTER_API_KEY doivent être définis (voir .env.example).")
    # Ceinture et bretelles : même un appel inattendu au fournisseur de vols prendrait le stub.
    os.environ["SERPAPI_API_KEY"] = ""
    return name


# --- Construction des entrées depuis un cas ------------------------------------------------------------


def build_group(travelers: list[dict]) -> TripResponseModelGroup:
    """Dates relatives (`start_in_days`, `days`) : un cas ne périme jamais."""
    today = date.today()
    trips = []
    for t in travelers:
        start = today + timedelta(days=t.get("start_in_days", 60))
        trips.append(
            TripPreferences(
                preferred_country=t["country"],
                budget_eur=t["budget_eur"],
                start_date=start,
                end_date=start + timedelta(days=t["days"] - 1),
                travel_style=t.get("style"),
                origin_city=t.get("origin", "Paris"),
            )
        )
    return TripResponseModelGroup(group_travel=trips)


def build_state(case: dict) -> dict:
    """État minimal du Graph 2 pour un nœud de planification."""
    return {
        "preferences": build_group(case["travelers"]),
        "selected_country": case["country"],
        "stay_city": case.get("stay_city"),
        "feedback": case.get("feedback"),
        "budget_note": case.get("budget_note"),
        "daily_level": None,
        "hotel_max_eur": None,
    }
