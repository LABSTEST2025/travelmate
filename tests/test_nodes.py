from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.conftest import make_group, make_trip
from travel_agent.state import CountryOverview
from travel_agent.subgraphs import planning
from travel_agent.subgraphs.planning import (
    _DayCosts,
    aggregate,
    daily_costs,
    flights,
    hotels,
    itinerary,
    plan_check,
    planner,
)
from travel_agent.subgraphs.suggestion import _OverviewDraft, research, suggest
from travel_agent.tools.flights import FlightsSearchError

# --- Graph 1 : suggestion ----------------------------------------------------


def test_research_completes_a_single_wish_with_ai_suggestions(llm, search):
    result = research({"preferences": make_group(make_trip(preferred_country="Espagne"))})

    assert [r["country"] for r in result["research"]] == ["Espagne", "Portugal", "Grèce"]
    assert [r["wished_by"] for r in result["research"]] == [1, 0, 0]
    assert [args[0] for name, args in search.calls] == ["Espagne", "Portugal", "Grèce"]
    (prompt,) = llm.prompts("_CandidateCountries")
    assert "Propose 2 pays" in prompt and "sans reproposer : Espagne" in prompt
    assert "Retour du groupe" not in prompt


def test_research_ignores_ai_duplicates_of_wished_countries(llm):
    llm.responses["_CandidateCountries"] = {"countries": ["espagne", "Italie", "Grèce"]}

    result = research({"preferences": make_group(make_trip(preferred_country="Espagne"))})

    assert [r["country"] for r in result["research"]] == ["Espagne", "Italie", "Grèce"]


def test_research_studies_every_wished_country_most_wished_first(llm):
    """T62 : chaque pays rêvé a sa fiche, sans plafond ; l'IA n'est pas sollicitée au-delà de 3 pays."""
    group = make_group(
        make_trip(preferred_country="Japon", budget_eur=900, origin_city="Lyon"),
        make_trip(preferred_country="Islande"),
        make_trip(preferred_country="Portugal", travel_style="détente"),
        make_trip(preferred_country="portugal ", budget_eur=1200),
        make_trip(preferred_country="Maroc"),
    )

    result = research({"preferences": group})

    assert [(r["country"], r["wished_by"]) for r in result["research"]] == [
        ("Portugal", 2), ("Japon", 1), ("Islande", 1), ("Maroc", 1)
    ]
    assert llm.prompts("_CandidateCountries") == []


def test_research_capitalizes_a_lowercase_wish(llm):
    group = make_group(make_trip(preferred_country="pays-bas"), make_trip(preferred_country="Pays-Bas"))

    result = research({"preferences": group})

    assert result["research"][0]["country"] == "Pays-bas" and result["research"][0]["wished_by"] == 2
    (prompt,) = llm.prompts("_CandidateCountries")
    assert "2 personne(s)" in prompt and "Propose 2 pays" in prompt


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
    assert [(r["country"], r["wished_by"]) for r in result["research"]] == [("Croatie", 0), ("Islande", 0)]


def test_suggest_builds_one_overview_per_country(llm):
    first = make_trip(origin_city="Paris")
    second = make_trip(origin_city="Lyon", end_date=first.end_date + timedelta(days=2))
    state = {
        "preferences": make_group(first, second),
        "research": [{"country": "Portugal", "wished_by": 2, "answer": "info", "sources": ["https://ok.example"]}],
    }

    (overview,) = suggest(state)["overviews"]

    assert overview.country == "Portugal"
    assert overview.sources == ["https://ok.example"] and overview.wished_by == 2
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


def test_aggregate_sums_flights_hotels_and_on_site_costs():
    state = {
        "flights": [{"price_eur": 120.5}, {"price_eur": 99.25}],
        "hotels": [{"price_eur": 700}],
        "daily_costs": {"total_eur": 300},
    }

    assert aggregate(state) == {"total_cost_eur": 1219.75}


def test_daily_costs_cover_each_traveler_stay(llm, search):
    first = make_trip()  # 8 jours
    second = make_trip(end_date=first.end_date + timedelta(days=2))  # 10 jours

    costs = daily_costs(_planning_state(first, second) | {"stay_city": "Porto"})["daily_costs"]

    assert costs["level"] == "standard" and costs["per_day_eur"]["meals_eur"] == 30
    assert costs["per_traveler_eur"] == [480, 600]
    assert costs["lines_eur"] == {"transport_eur": 180, "meals_eur": 540, "activities_eur": 360}
    assert costs["total_eur"] == 1080 and costs["economy_total_eur"] == 540
    assert costs["sources"] == ["https://example.com/daily_costs"]
    assert search.calls[-1] == ("daily_costs", ("Portugal", "Porto"))
    prompt = llm.prompts("_DailyCostsDraft")[0]
    assert "Porto (Portugal)" in prompt and "budget" not in prompt.lower()  # aucun budget dans le prompt


def test_daily_costs_economy_level_is_never_above_standard(llm):
    llm.responses["_DailyCostsDraft"] = {
        "standard": {"transport_eur": 10, "meals_eur": 30, "activities_eur": 20},
        "economy": {"transport_eur": 12, "meals_eur": 15, "activities_eur": 10},
    }

    costs = daily_costs(_planning_state(make_trip()) | {"daily_level": "economy"})["daily_costs"]

    assert costs["level"] == "economy"
    assert costs["per_day_eur"] == {"transport_eur": 10, "meals_eur": 15, "activities_eur": 10}
    assert costs["total_eur"] == costs["economy_total_eur"] == 35 * 8


def test_daily_costs_reject_a_whole_stay_given_as_a_day():
    with pytest.raises(ValidationError):
        _DayCosts(transport_eur=10, meals_eur=900, activities_eur=20)


def test_check_routes_to_review_only_when_enabled(monkeypatch):
    state = {"preferences": make_group(make_trip(budget_eur=2000)), "total_cost_eur": 1000}
    assert plan_check(state).goto == "__end__"
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    assert plan_check(state).goto == "hitl_final"
    assert plan_check(state | {"guardrail_error": "Aucun vol."}).goto == "__end__"


def test_check_replans_an_over_budget_draft_before_review(monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    state = {"preferences": make_group(make_trip(budget_eur=2000)), "total_cost_eur": 2500, "plan_retries": 0}

    command = plan_check(state)

    assert command.goto == "planner"
    assert command.update["plan_retries"] == 1 and "dépasse le budget" in command.update["budget_note"]


def test_research_never_returns_an_empty_list(llm):
    llm.responses["_CandidateCountries"] = {"countries": ["Espagne", "Portugal"]}
    previous = [
        CountryOverview(country=c, pros=[], cons=[], estimated_budget_eur=1, best_period="", sources=[])
        for c in ("Espagne", "Portugal")
    ]

    result = research({"preferences": make_group(), "feedback": "autre chose", "overviews": previous})

    assert [r["country"] for r in result["research"]] == ["Espagne", "Portugal"]


def test_itinerary_retries_when_days_are_missing(llm):
    start = make_trip().start_date
    trip = make_trip(start_date=start, end_date=start + timedelta(days=2))  # 3 jours
    llm.responses["_ItineraryDraft"] = [
        {"days": [{"day": 1, "activities": ["a"]}]},
        {"days": [{"day": d, "activities": ["a"]} for d in (1, 2, 3)]},
    ]

    result = itinerary(_planning_state(trip))

    assert [d["day"] for d in result["itinerary"]] == [1, 2, 3]
    assert "exactement 3 jours" in llm.prompts("_ItineraryDraft")[0]


def test_itinerary_keeps_the_most_complete_attempt(llm):
    start = make_trip().start_date
    trip = make_trip(start_date=start, end_date=start + timedelta(days=4))  # 5 jours
    llm.responses["_ItineraryDraft"] = [
        {"days": [{"day": d, "activities": ["a"]} for d in (1, 2)]},
        {"days": [{"day": 1, "activities": ["a"]}]},
    ]

    assert len(itinerary(_planning_state(trip))["itinerary"]) == 2
    assert len(llm.prompts("_ItineraryDraft")) == 2


def test_hotel_prompt_mentions_group_size(llm):
    hotels(_planning_state(make_trip(), make_trip(), make_trip()))

    assert "3 personne(s)" in llm.prompts("_HotelDraft")[0]


def test_research_completes_a_short_list_of_countries(llm):
    llm.responses["_CandidateCountries"] = [{"countries": ["Portugal"]}, {"countries": ["Portugal", "Italie", "Grèce"]}]

    result = research({"preferences": make_group(make_trip(preferred_country="Portugal"))})

    assert [r["country"] for r in result["research"]] == ["Portugal", "Italie", "Grèce"]
    assert len(llm.prompts("_CandidateCountries")) == 2


def test_hotel_price_must_be_positive():
    from travel_agent.subgraphs.planning import _HotelDraft

    with pytest.raises(ValidationError):
        _HotelDraft(name="ibis", price_eur=0, notes="")


def test_overview_keeps_three_points_at_most(llm):
    llm.responses["_OverviewDraft"] = {
        "pros": [f"pro {i}" for i in range(6)], "cons": [f"con {i}" for i in range(5)],
        "estimated_budget_eur": 900, "best_period": "mai – sept.",
    }
    state = {"preferences": make_group(), "research": [{"country": "Portugal", "answer": "", "sources": []}]}

    (overview,) = suggest(state)["overviews"]

    assert overview.pros == ["pro 0", "pro 1", "pro 2"] and len(overview.cons) == 3
    assert "au plus 3 points forts" in llm.prompts("_OverviewDraft")[0]


# --- T56 : une seule ville de séjour pour les vols, l'hôtel et l'itinéraire ----------------------


def test_planner_chooses_the_stay_city(llm):
    state = _planning_state(make_trip(travel_style="culture")) | {"feedback": "un hôtel à Porto"}

    assert planner(state) == {"stay_city": "Lisbonne"}
    prompt = llm.prompts("_StayDraft")[0]
    assert "Portugal" in prompt and "culture" in prompt and "« un hôtel à Porto »" in prompt


def test_planner_ignores_an_invalid_city(llm):
    llm.responses["_StayDraft"] = {"city": "Lisbonne, puis Porto, puis l'Algarve en voiture de location"}

    assert planner(_planning_state(make_trip())) == {"stay_city": None}


def test_planner_keeps_the_city_name_without_region_or_parentheses(llm):
    # Réponse réelle (evals, Grèce « sur une île ») : rejetée, elle faisait oublier l'ajustement du groupe.
    for answer, expected in (
        ("La Canée (Chania), Crète", "La Canée"),
        ("Héraklion, Crète", "Héraklion"),
        ("Porto (Portugal)", "Porto"),
        ("  Kyoto  ", "Kyoto"),
    ):
        llm.responses["_StayDraft"] = {"city": answer}
        assert planner(_planning_state(make_trip())) == {"stay_city": expected}, answer


def test_all_branches_target_the_stay_city(llm, search):
    state = _planning_state(make_trip(origin_city="Lyon")) | {"stay_city": "Lisbonne"}

    (flight,) = flights(state)["flights"]
    hotels(state)
    itinerary(state)

    assert flight["destination"] == "Lisbonne (Portugal)"
    assert "Destination : Lisbonne (Portugal) : l'hôtel doit se trouver à Lisbonne même" in llm.prompts("_HotelDraft")[0]
    assert "avec Lisbonne comme ville d'arrivée" in llm.prompts("_ItineraryDraft")[0]
    assert ("activities", ("Portugal", "culture")) in search.calls


def test_branches_fall_back_to_the_country_without_stay_city(llm):
    (flight,) = flights(_planning_state(make_trip()))["flights"]
    hotels(_planning_state(make_trip()))

    assert flight["destination"] == "Portugal"
    assert "Destination : Portugal." in llm.prompts("_HotelDraft")[0]


def test_hotel_retries_an_implausible_price(llm):
    llm.responses["_HotelDraft"] = [
        {"name": "Aucune proposition possible", "price_eur": 0.05, "notes": ""},
        {"name": "Riad Zahra", "price_eur": 700, "notes": "Dans la médina."},
    ]

    (hotel,) = hotels(_planning_state(make_trip()))["hotels"]

    assert hotel["name"] == "Riad Zahra" and len(llm.prompts("_HotelDraft")) == 2


def test_hotel_gives_up_after_repeated_implausible_prices(llm):
    llm.responses["_HotelDraft"] = {"name": "Rien", "price_eur": 1, "notes": ""}

    with pytest.raises(ValueError, match="invraisemblable"):
        hotels(_planning_state(make_trip()))
    assert len(llm.prompts("_HotelDraft")) == 3


def test_hotel_knows_the_city_already_reflects_the_feedback(llm):
    hotels(_planning_state(make_trip()) | {"stay_city": "Essaouira", "feedback": "au bord de la mer"})

    assert "(la ville de séjour, Essaouira, en tient déjà compte)" in llm.prompts("_HotelDraft")[0]
