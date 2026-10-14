from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.conftest import make_group, make_trip
from travel_agent.state import CountryOverview
from travel_agent.subgraphs import planning
from travel_agent.subgraphs.planning import aggregate, flights, hotels, itinerary, route_after_aggregate
from travel_agent.subgraphs.suggestion import _OverviewDraft, research, suggest
from travel_agent.tools.flights import FlightsSearchError

# --- Graph 1 : suggestion ----------------------------------------------------


def test_research_proposes_several_countries_with_preferred_first(llm, search):
    result = research({"preferences": make_group(make_trip(preferred_country="Espagne"))})

    assert [r["country"] for r in result["research"]] == ["Espagne", "Portugal", "Grèce"]
    assert [args[0] for name, args in search.calls] == ["Espagne", "Portugal", "Grèce"]
    assert "Retour du groupe" not in llm.prompts("_CandidateCountries")[0]


def test_research_keeps_preferred_country_order_from_llm(llm):
    llm.responses["_CandidateCountries"] = {"countries": ["Portugal", "espagne", "Italie", "Grèce"]}

    result = research({"preferences": make_group(make_trip(preferred_country="Espagne"))})

    assert [r["country"] for r in result["research"]] == ["Portugal", "espagne", "Italie"]


def test_research_makes_one_list_for_the_whole_group(llm):
    llm.responses["_CandidateCountries"] = {"countries": ["Croatie", "Grèce", "Italie"]}
    group = make_group(
        make_trip(preferred_country="Japon", budget_eur=900, origin_city="Lyon"),
        make_trip(preferred_country="Portugal", budget_eur=1500, travel_style="détente"),
        make_trip(preferred_country="portugal ", budget_eur=1200),
    )

    result = research({"preferences": group})

    # Le pays le plus demandé (2 fois, casse ignorée) passe en tête ; une seule liste pour 3 voyageurs.
    assert [r["country"] for r in result["research"]] == ["Portugal", "Croatie", "Grèce"]
    (prompt,) = llm.prompts("_CandidateCountries")
    assert "3 personne(s)" in prompt and "900 à 1500 EUR" in prompt
    assert "Japon" in prompt and "détente" in prompt and "Lyon" in prompt


def test_research_uses_feedback_and_excludes_previous_countries(llm):
    llm.responses["_CandidateCountries"] = {"countries": ["Portugal", "Croatie", "Islande"]}
    previous = [
        CountryOverview(country=c, pros=[], cons=[], estimated_budget_eur=1, best_period="", sources=[])
        for c in ("Espagne", "Portugal")
    ]

    result = research({"preferences": make_group(), "feedback": "trop cher", "overviews": previous})

    prompt = llm.prompts("_CandidateCountries")[0]
    assert "trop cher" in prompt and "Espagne, Portugal" in prompt
    # Un pays déjà proposé que le LLM renvoie quand même est écarté, et le pays souhaité n'est pas forcé.
    assert [r["country"] for r in result["research"]] == ["Croatie", "Islande"]


def test_suggest_builds_one_overview_per_country(llm):
    first = make_trip(origin_city="Paris")
    second = make_trip(origin_city="Lyon", end_date=first.end_date + timedelta(days=2))
    state = {
        "preferences": make_group(first, second),
        "research": [{"country": "Portugal", "answer": "info", "sources": ["https://ok.example"]}],
    }

    (overview,) = suggest(state)["overviews"]

    assert overview.country == "Portugal"
    assert overview.sources == ["https://ok.example"]
    assert overview.estimated_budget_eur == 900
    prompt = llm.prompts("_OverviewDraft")[0]
    assert "Lyon, Paris" in prompt and "10 jours" in prompt


def test_overview_budget_must_be_positive():
    with pytest.raises(ValidationError):
        _OverviewDraft(pros=[], cons=[], estimated_budget_eur=0, best_period="mai")


# --- Graph 2 : planification -------------------------------------------------


def _planning_state(*trips) -> dict:
    return {"preferences": make_group(*trips), "selected_country": "Portugal", "feedback": None}


def test_flights_searches_once_per_traveler():
    first, second = make_trip(origin_city="Paris"), make_trip(origin_city="Lyon", budget_eur=1000)

    result = flights(_planning_state(first, second))

    assert [f["origin"] for f in result["flights"]] == ["Paris", "Lyon"]
    assert "guardrail_error" not in result


def test_flights_turns_search_errors_into_guardrail_error(monkeypatch):
    class FailingProvider:
        def search(self, *_args):
            raise FlightsSearchError("Aucun vol trouvé.")

    monkeypatch.setattr(planning, "get_flights_provider", FailingProvider)

    assert flights(_planning_state(make_trip())) == {"flights": [], "guardrail_error": "Aucun vol trouvé."}


def test_hotels_covers_whole_group_stay_with_capped_budget(llm, search):
    first = make_trip(budget_eur=2000)
    second = make_trip(
        budget_eur=1000, start_date=first.start_date - timedelta(days=1), end_date=first.end_date + timedelta(days=2)
    )

    (hotel,) = hotels(_planning_state(first, second) | {"feedback": "moins cher"})["hotels"]

    assert hotel["check_in"] == second.start_date.isoformat()
    assert hotel["check_out"] == second.end_date.isoformat()
    assert hotel["sources"] == ["https://example.com/hotels"]
    assert search.calls[0] == ("hotels", ("Portugal", 1500.0))
    prompt = llm.prompts("_HotelDraft")[0]
    assert "1500 EUR" in prompt and "moins cher" in prompt


def test_itinerary_merges_group_styles(llm):
    trips = (make_trip(travel_style="culture"), make_trip(travel_style="aventure"), make_trip(travel_style=None))

    result = itinerary(_planning_state(*trips))

    assert result["itinerary"] == [
        {"day": 1, "activities": ["Visite du centre"], "sources": ["https://example.com/activities"]}
    ]
    assert "aventure, culture" in llm.prompts("_ItineraryDraft")[0]


def test_aggregate_sums_flights_and_hotels():
    state = {"flights": [{"price_eur": 120.5}, {"price_eur": 99.25}], "hotels": [{"price_eur": 700}]}

    assert aggregate(state) == {"total_cost_eur": 919.75}


def test_route_after_aggregate(monkeypatch):
    assert route_after_aggregate({}) == "__end__"
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    assert route_after_aggregate({}) == "hitl_final"
    assert route_after_aggregate({"guardrail_error": "Aucun vol."}) == "__end__"
