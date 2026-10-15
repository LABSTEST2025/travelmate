"""Scénarios de bout en bout sur le graphe compilé, avec interruptions HITL et reprises."""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from tests.conftest import DAILY_EUR, make_group, make_trip
from travel_agent.graph import build_travel_graph
from travel_agent.state import get_checkpoint_serde
from travel_agent.tools.flights import StubFlightsProvider


@pytest.fixture
def app():
    return build_travel_graph().compile(checkpointer=MemorySaver(serde=get_checkpoint_serde()))


CONFIG = {"configurable": {"thread_id": "test"}}


def _start(app, preferences):
    return app.invoke({"preferences": preferences, "messages": []}, CONFIG)


def _resume(app, value):
    return app.invoke(Command(resume=value), CONFIG)


def test_full_trip_is_planned_after_country_choice(app):
    result = _start(app, make_group(make_trip(budget_eur=3000)))

    payload = result["__interrupt__"][0].value
    assert [o["country"] for o in payload["overviews"]] == ["Espagne", "Portugal", "Grèce"]
    assert "3000" not in str(payload)  # aucun budget individuel dans l'interruption

    result = _resume(app, {"selected_country": "Portugal"})

    assert "__interrupt__" not in result
    assert result["guardrail_error"] is None
    plan = result["final_plan"]
    assert plan["selected_country"] == "Portugal"
    # vol du stub + hôtel + dépenses sur place (8 jours)
    assert plan["total_cost_eur"] == StubFlightsProvider.PRICE_EUR + 800 + DAILY_EUR * 8
    assert plan["daily_costs"]["level"] == "standard" and plan["daily_costs"]["economy_total_eur"] == 30 * 8
    assert plan["budget_eur"] == 3000


def test_country_feedback_loops_back_to_research(app, llm):
    _start(app, make_group())
    llm.responses["_CandidateCountries"] = {"countries": ["Croatie", "Islande"]}

    result = _resume(app, {"feedback": "plus nordique"})

    payload = result["__interrupt__"][0].value
    assert [o["country"] for o in payload["overviews"]] == ["Croatie", "Islande"]
    assert "plus nordique" in llm.prompts("_CandidateCountries")[-1]


def test_guard_in_error_stops_before_suggestion(app, llm):
    result = _start(app, make_group(make_trip(budget_eur=0)))

    assert "budget" in result["guardrail_error"]
    assert "__interrupt__" not in result
    assert llm.calls == []


def test_over_budget_plan_is_replanned_with_feedback(app, llm):
    llm.responses["_HotelDraft"] = [
        {"name": "Palace", "price_eur": 5000, "notes": ""},
        {"name": "Auberge", "price_eur": 500, "notes": ""},
    ]
    _start(app, make_group(make_trip(budget_eur=2000)))

    result = _resume(app, {"selected_country": "Portugal"})

    assert result["final_plan"]["hotels"][0]["name"] == "Auberge"
    assert result["plan_retries"] == 1
    assert "dépasse le budget" in llm.prompts("_HotelDraft")[1]


def test_over_budget_plan_fails_after_max_retries(app, llm):
    llm.responses["_HotelDraft"] = {"name": "Palace", "price_eur": 5000, "notes": ""}
    _start(app, make_group(make_trip(budget_eur=2000)))

    result = _resume(app, {"selected_country": "Portugal"})

    assert result["final_plan"] is None
    assert "dépasse le budget" in result["guardrail_error"]
    assert len(llm.prompts("_HotelDraft")) == 3  # plan initial + 2 relances


def test_final_hitl_adjustments_loop_back_to_planner(app, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    _start(app, make_group(make_trip(budget_eur=3000)))

    result = _resume(app, {"selected_country": "Portugal"})
    assert result["__interrupt__"][0].value["total_cost_eur"] == StubFlightsProvider.PRICE_EUR + 800 + DAILY_EUR * 8

    result = _resume(app, {"approved": False, "feedback": "un hôtel avec piscine"})
    assert "__interrupt__" in result
    assert "un hôtel avec piscine" in llm.prompts("_HotelDraft")[-1]

    result = _resume(app, {"approved": True})
    assert result["final_plan"]["selected_country"] == "Portugal"


def test_over_budget_draft_is_replanned_before_review_and_approval_is_final(app, llm, monkeypatch):
    """T63 : la relance budget a lieu avant le HITL final, valider ne relance plus la planification."""
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    llm.responses["_HotelDraft"] = [
        {"name": "Palace", "price_eur": 5000, "notes": ""},
        {"name": "Auberge", "price_eur": 500, "notes": ""},
    ]
    _start(app, make_group(make_trip(budget_eur=2000)))

    result = _resume(app, {"selected_country": "Portugal"})
    assert result["__interrupt__"][0].value["hotels"][0]["name"] == "Auberge"
    hotel_calls = len(llm.prompts("_HotelDraft"))

    result = _resume(app, {"approved": True})

    assert result["final_plan"]["hotels"][0]["name"] == "Auberge"
    assert len(llm.prompts("_HotelDraft")) == hotel_calls


def test_over_budget_draft_is_shown_for_review_once_retries_are_exhausted(app, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    llm.responses["_HotelDraft"] = {"name": "Palace", "price_eur": 5000, "notes": ""}
    _start(app, make_group(make_trip(budget_eur=2000)))

    result = _resume(app, {"selected_country": "Portugal"})
    assert result["__interrupt__"][0].value["hotels"][0]["name"] == "Palace"

    result = _resume(app, {"approved": True})

    assert result["guardrail_error"] is None
    assert result["final_plan"]["total_cost_eur"] > result["final_plan"]["budget_eur"]
    assert len(llm.prompts("_HotelDraft")) == 3  # plan initial + 2 relances, aucune après validation


def test_budget_retry_keeps_the_group_adjustment(app, llm, monkeypatch):
    """T64 : la relance budget s'ajoute à l'ajustement demandé au lieu de le remplacer."""
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    _start(app, make_group(make_trip(budget_eur=3000)))
    _resume(app, {"selected_country": "Portugal"})
    llm.responses["_StayDraft"] = lambda prompt: {"city": "Porto" if "Porto" in prompt else "Lisbonne"}
    llm.responses["_HotelDraft"] = [
        {"name": "Palace", "price_eur": 5000, "notes": ""},
        {"name": "Auberge", "price_eur": 500, "notes": ""},
    ]

    result = _resume(app, {"approved": False, "feedback": "plutôt Porto, avec piscine"})

    payload = result["__interrupt__"][0].value
    assert payload["hotels"][0]["name"] == "Auberge"
    assert payload["flights"][0]["destination"] == "Porto (Portugal)"
    retry_prompt = llm.prompts("_HotelDraft")[-1]
    assert "plutôt Porto, avec piscine" in retry_prompt and "dépasse le budget" in retry_prompt


def test_new_adjustment_gets_its_own_budget_retries(app, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    llm.responses["_HotelDraft"] = {"name": "Palace", "price_eur": 5000, "notes": ""}
    _start(app, make_group(make_trip(budget_eur=2000)))
    _resume(app, {"selected_country": "Portugal"})  # plan initial + 2 relances, toutes hors budget
    llm.responses["_HotelDraft"] = [
        {"name": "Palace bis", "price_eur": 5000, "notes": ""},
        {"name": "Auberge", "price_eur": 500, "notes": ""},
    ]

    result = _resume(app, {"approved": False, "feedback": "plus calme"})

    assert result["__interrupt__"][0].value["hotels"][0]["name"] == "Auberge"
    assert "Contrainte de budget" not in llm.prompts("_HotelDraft")[-2]  # l'ancienne consigne est effacée


def test_savings_keep_the_previous_adjustment(app, llm, monkeypatch):
    """B05 : une piste d'économie change le niveau ou le plafond, sans effacer l'ajustement du groupe."""
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    _start(app, make_group(make_trip(budget_eur=3000)))
    _resume(app, {"selected_country": "Portugal"})
    _resume(app, {"approved": False, "feedback": "près de la plage"})

    result = _resume(app, {"approved": False, "savings": ["economy", "hotel"]})

    payload = result["__interrupt__"][0].value
    assert payload["daily_costs"]["level"] == "economy"
    hotel_prompt = llm.prompts("_HotelDraft")[-1]
    assert "ne doit pas dépasser 560 EUR" in hotel_prompt and "près de la plage" in hotel_prompt


def test_flights_error_reaches_the_user(app, monkeypatch):
    from travel_agent.subgraphs import planning
    from travel_agent.tools.flights import FlightsSearchError

    class FailingProvider:
        def search(self, *_args):
            raise FlightsSearchError("Aucun vol trouvé de Paris vers Portugal.")

    monkeypatch.setattr(planning, "get_flights_provider", FailingProvider)
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    _start(app, make_group())

    result = _resume(app, {"selected_country": "Portugal"})

    assert "__interrupt__" not in result  # pas de HITL final sur un plan incomplet
    assert result["guardrail_error"] == "Aucun vol trouvé de Paris vers Portugal."
    assert result["final_plan"] is None


def test_new_run_on_the_same_thread_forgets_previous_feedback(app, llm):
    _start(app, make_group())
    _resume(app, {"feedback": "plus au nord"})

    result = _start(app, make_group())

    assert [o["country"] for o in result["__interrupt__"][0].value["overviews"]] == ["Espagne", "Portugal", "Grèce"]
    assert "plus au nord" not in llm.prompts("_CandidateCountries")[-1]


def test_final_adjustment_can_move_the_stay_city(app, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    _start(app, make_group(make_trip(budget_eur=3000)))
    _resume(app, {"selected_country": "Portugal"})
    llm.responses["_StayDraft"] = lambda prompt: {"city": "Porto" if "Porto" in prompt else "Lisbonne"}

    result = _resume(app, {"approved": False, "feedback": "plutôt Porto"})

    payload = result["__interrupt__"][0].value
    assert payload["flights"][0]["destination"] == "Porto (Portugal)"
    assert "Destination : Porto (Portugal)" in llm.prompts("_HotelDraft")[-1]
