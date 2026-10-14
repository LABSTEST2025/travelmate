"""Mocks partagés (T20) : aucun test ne doit toucher le réseau (LLM, Tavily, SerpApi, LangSmith).

Le `.env` contient de vraies clés (dont SerpApi, plan gratuit limité) : tout appel réseau est
remplacé ici par défaut, via des fixtures `autouse`.
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from travel_agent.guardrails import input as guard_input
from travel_agent.state import TripPreferences, TripResponseModelGroup
from travel_agent.subgraphs import planning, suggestion
from travel_agent.tools import airports

# Importer travel_agent a chargé le `.env` (avec override) : on coupe le traçage LangSmith après coup.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

FIXTURES = Path(__file__).parent / "fixtures"

DEFAULT_LLM_RESPONSES: dict[str, dict] = {
    "_TopicCheck": {"is_on_topic": True, "is_injection_attempt": False},
    "_CandidateCountries": {"countries": ["Portugal", "Grèce", "Croatie"]},
    "_OverviewDraft": {
        "pros": ["Climat doux"],
        "cons": ["Affluence en été"],
        "estimated_budget_eur": 900,
        "best_period": "mai-juin",
    },
    "_StayDraft": {"city": "Lisbonne"},
    "_HotelDraft": {"name": "Hôtel Test", "price_eur": 800, "notes": "Centre-ville"},
    "_ItineraryDraft": {"days": [{"day": 1, "activities": ["Visite du centre"]}]},
    "_AirportCodes": {"codes": ["CDG", "ORY"]},
}


class FakeLLM:
    """Remplace `with_structured_retry(llm, schema)`. Les réponses sont indexées par nom de schéma :
    un dict, une instance du schéma, une exception, ou une fonction `prompt -> l'un des trois`.
    Une liste donne une réponse différente à chaque appel."""

    def __init__(self):
        self.responses: dict = dict(DEFAULT_LLM_RESPONSES)
        self.calls: list[tuple[str, str]] = []

    def prompts(self, schema_name: str) -> list[str]:
        return [prompt for name, prompt in self.calls if name == schema_name]

    def structured(self, _llm, schema, **_kwargs):
        fake = self

        class _Runnable:
            def invoke(self, prompt):
                fake.calls.append((schema.__name__, prompt))
                response = fake.responses[schema.__name__]
                if isinstance(response, list):
                    response = response.pop(0)
                if callable(response) and not isinstance(response, type):
                    response = response(prompt)
                if isinstance(response, Exception):
                    raise response
                return response if isinstance(response, BaseModel) else schema.model_validate(response)

        return _Runnable()


class FakeSearch:
    """Remplace les fonctions Tavily de `tools/search.py`."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def make(self, name: str):
        def _search(*args, **_kwargs) -> dict:
            self.calls.append((name, args))
            return {"answer": f"infos {args[0]}", "sources": [f"https://example.com/{name}"], "results": []}

        return _search


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("ENABLE_FINAL_HITL", "false")

    def _no_network(*_args, **_kwargs):
        raise RuntimeError("Appel réseau interdit dans les tests : mocke httpx.get.")

    monkeypatch.setattr(httpx, "get", _no_network)


@pytest.fixture(autouse=True)
def llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    for module in (guard_input, suggestion, planning, airports):
        monkeypatch.setattr(module, "with_structured_retry", fake.structured)
        monkeypatch.setattr(module, "get_llm", lambda **_kwargs: None)
    airports.resolve_airports.cache_clear()
    yield fake
    airports.resolve_airports.cache_clear()


@pytest.fixture(autouse=True)
def search(monkeypatch) -> FakeSearch:
    fake = FakeSearch()
    monkeypatch.setattr(suggestion, "search_country_overview", fake.make("country"))
    monkeypatch.setattr(planning, "search_hotels", fake.make("hotels"))
    monkeypatch.setattr(planning, "search_activities", fake.make("activities"))
    return fake


def make_trip(**overrides) -> TripPreferences:
    start = date.today() + timedelta(days=60)
    values = {
        "preferred_country": "Espagne",
        "budget_eur": 2000,
        "start_date": start,
        "end_date": start + timedelta(days=7),
        "travel_style": "culture",
        "origin_city": "Paris",
    }
    return TripPreferences(**(values | overrides))


def make_group(*trips: TripPreferences) -> TripResponseModelGroup:
    return TripResponseModelGroup(group_travel=list(trips) or [make_trip()])


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
