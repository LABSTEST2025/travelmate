"""API web (T29–T39) : lobby, budget privé, lancement, vote, égalité, autres idées, plan.

Chaque `TestClient` joue un membre (son propre cookie). Le runner exécute le graphe immédiatement
(sans executor), avec les mocks de conftest.py.
"""

import json
import sqlite3
from concurrent.futures import Executor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.sqlite import SqliteStore

from travel_agent.graph import build_travel_graph
from travel_agent.state import get_checkpoint_serde
from travel_agent.tools.flights import StubFlightsProvider
from travel_agent.web.app import create_app
from travel_agent.web.repository import TripRepository
from travel_agent.web.runner import RUN_FAILED_MESSAGE, RUN_STOPPED_MESSAGE, TripBusyError, TripRunner

HOTEL_EUR = 800  # prix de l'hôtel renvoyé par le faux LLM (conftest.py)
FLIGHT_EUR = StubFlightsProvider.PRICE_EUR


@pytest.fixture
def graph():
    return build_travel_graph().compile(checkpointer=MemorySaver(serde=get_checkpoint_serde()))


@pytest.fixture
def app(graph):
    store = SqliteStore(sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None))
    store.setup()
    return create_app(TripRepository(store), TripRunner(graph))


@pytest.fixture
def client(app):
    """Un nouveau navigateur (= un membre potentiel) à chaque appel."""
    return lambda: TestClient(app)


def _prefs(**overrides) -> dict:
    start = date.today() + timedelta(days=60)
    values = {
        "preferred_country": "Portugal",
        "budget_eur": 1500,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=7)).isoformat(),
        "travel_style": "culture",
        "origin_city": "Paris",
    }
    return values | overrides


def _create(organizer: TestClient, name: str = "Maya") -> str:
    response = organizer.post("/api/trips", json={"trip_name": "Été 2027 entre potes", "organizer_name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _group(client, budgets: list[float]) -> tuple[str, list[TestClient]]:
    """Voyage dont chaque membre (l'organisatrice en premier) a donné ses envies."""
    members = [client() for _ in budgets]
    trip_id = _create(members[0])
    for i, (member, budget) in enumerate(zip(members, budgets)):
        if i:
            assert member.post(f"/api/trips/{trip_id}/members", json={"name": f"Membre {i}"}).status_code == 200
        response = member.put(f"/api/trips/{trip_id}/preferences", json=_prefs(budget_eur=budget))
        assert response.status_code == 200, response.text
    return trip_id, members


def _launched(client, budgets: list[float]) -> tuple[str, list[TestClient]]:
    trip_id, members = _group(client, budgets)
    response = members[0].post(f"/api/trips/{trip_id}/launch")
    assert response.status_code == 200, response.text
    return trip_id, members


def _vote(member: TestClient, trip_id: str, country: str):
    return member.post(f"/api/trips/{trip_id}/votes", json={"country": country})


# --- T29 / T31 : socle et lobby ---------------------------------------------------


def test_health(client):
    assert client().get("/api/health").json() == {"status": "ok"}


def test_create_trip_makes_the_creator_organizer(client):
    organizer = client()
    trip_id = _create(organizer)

    view = organizer.get(f"/api/trips/{trip_id}").json()
    assert view["status"] == "collecting" and view["name"] == "Été 2027 entre potes"
    assert view["me"]["is_organizer"] and view["me"]["name"] == "Maya"
    assert len(trip_id) >= 12  # code d'invitation non devinable
    cookie = organizer.cookies.get(f"escapade_{trip_id}")
    assert cookie and cookie not in json.dumps(view)  # seul le hash du jeton est stocké, jamais renvoyé


def test_join_is_idempotent_and_assigns_colors(client):
    organizer, friend = client(), client()
    trip_id = _create(organizer)

    first = friend.post(f"/api/trips/{trip_id}/members", json={"name": " Léa "}).json()
    again = friend.post(f"/api/trips/{trip_id}/members", json={"name": "Léa"}).json()

    assert first["me"]["id"] == again["me"]["id"] and first["me"]["name"] == "Léa"
    assert [m["name"] for m in again["members"]] == ["Maya", "Léa"]
    assert len({m["color"] for m in again["members"]}) == 2
    assert organizer.get(f"/api/trips/{trip_id}").json()["me"]["name"] == "Maya"


def test_visitor_sees_the_trip_but_is_not_a_member(client):
    trip_id = _create(client())

    view = client().get(f"/api/trips/{trip_id}").json()

    assert view["me"] is None and len(view["members"]) == 1


def test_unknown_trip_returns_404(client):
    visitor = client()
    assert visitor.get("/api/trips/inconnu").status_code == 404
    assert visitor.post("/api/trips/inconnu/members", json={"name": "Léa"}).status_code == 404
    assert visitor.put("/api/trips/inconnu/preferences", json=_prefs()).status_code == 404


def test_input_validation(client):
    organizer = client()
    assert organizer.post("/api/trips", json={"trip_name": "  ", "organizer_name": "Maya"}).status_code == 422
    trip_id = _create(organizer)

    assert client().put(f"/api/trips/{trip_id}/preferences", json=_prefs()).status_code == 401
    past = (date.today() - timedelta(days=1)).isoformat()
    response = organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs(start_date=past))
    assert response.status_code == 422 and "futur" in response.json()["detail"]
    assert organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs(origin_city=" ")).status_code == 422
    assert organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs(budget_eur=0)).status_code == 422


def test_preferences_can_be_updated_before_launch(client):
    trip_id, (organizer,) = _group(client, [1500])

    organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs(preferred_country="Japon"))

    me = organizer.get(f"/api/trips/{trip_id}").json()["me"]
    assert me["submitted"] and me["preferences"]["preferred_country"] == "Japon"


def test_only_the_organizer_can_launch(client):
    trip_id, (organizer, friend) = _group(client, [1500, 1200])

    assert friend.post(f"/api/trips/{trip_id}/launch").status_code == 403
    assert organizer.post(f"/api/trips/{trip_id}/launch").status_code == 200
    assert organizer.post(f"/api/trips/{trip_id}/launch").status_code == 409


def test_launch_needs_at_least_one_answer(client):
    organizer = client()
    trip_id = _create(organizer)

    assert organizer.post(f"/api/trips/{trip_id}/launch").status_code == 409


def test_trip_is_closed_after_launch(client):
    trip_id, (organizer,) = _launched(client, [1500])

    assert client().post(f"/api/trips/{trip_id}/members", json={"name": "Retard"}).status_code == 409
    assert organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs()).status_code == 409


def test_late_members_are_left_out_of_the_agent(client, llm):
    organizer, late = client(), client()
    trip_id = _create(organizer)
    late.post(f"/api/trips/{trip_id}/members", json={"name": "Karim"})
    organizer.put(f"/api/trips/{trip_id}/preferences", json=_prefs())

    view = organizer.post(f"/api/trips/{trip_id}/launch").json()

    assert view["status"] == "voting" and view["vote"]["voters_total"] == 1
    assert "1 personne(s)" in llm.prompts("_CandidateCountries")[0]
    assert late.post(f"/api/trips/{trip_id}/votes", json={"country": "Portugal"}).status_code == 403


# --- T32 : budget privé ---------------------------------------------------------------


def test_individual_budgets_never_leak(client, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    budgets = [1234.5, 2345.5, 3456.5]
    trip_id, members = _group(client, budgets)

    def leaks(viewer: TestClient, own: float) -> list[float]:
        body = viewer.get(f"/api/trips/{trip_id}").text
        return [b for b in budgets if b != own and str(b) in body]

    view = members[1].get(f"/api/trips/{trip_id}").json()
    assert view["me"]["preferences"]["budget_eur"] == 2345.5
    assert view["group_budget_range_eur"] == [1200, 3500]
    assert all(leaks(m, b) == [] for m, b in zip(members, budgets))

    members[0].post(f"/api/trips/{trip_id}/launch")
    for member in members:
        _vote(member, trip_id, "Portugal")
    assert members[1].get(f"/api/trips/{trip_id}").json()["status"] == "reviewing"
    assert all(leaks(m, b) == [] for m, b in zip(members, budgets))
    assert leaks(client(), own=0) == []  # visiteur sans cookie


def test_group_range_needs_three_answers(client):
    trip_id, members = _group(client, [1500, 1200])

    view = members[0].get(f"/api/trips/{trip_id}").json()

    assert view["responses"] == 2 and view["group_budget_range_eur"] is None


def test_suggestions_say_if_they_fit_everyone(client, llm):
    llm.responses["_OverviewDraft"] = lambda prompt: {
        "pros": [],
        "cons": [],
        "estimated_budget_eur": 1300 if "Grèce" in prompt else 900,
        "best_period": "mai",
    }
    trip_id, members = _launched(client, [1000, 1500])

    suggestions = members[0].get(f"/api/trips/{trip_id}").json()["suggestions"]

    fits = {s["country"]: s["fits_everyone_budget"] for s in suggestions}
    assert fits == {"Portugal": True, "Grèce": False, "Croatie": True}
    assert all(s["in_group_range"] is None for s in suggestions)  # moins de 3 réponses


# --- T33 / T34 : lancement, statut, reprise ------------------------------------------------


def test_launch_proposes_three_countries_for_the_group(client):
    trip_id, members = _launched(client, [1500, 1200, 900])

    view = members[2].get(f"/api/trips/{trip_id}").json()

    assert view["status"] == "voting"
    assert [s["country"] for s in view["suggestions"]] == ["Portugal", "Grèce", "Croatie"]
    assert view["vote"] == {
        "round": 1,
        "options": ["Portugal", "Grèce", "Croatie"],
        "voters_total": 3,
        "votes_cast": 0,
        "my_vote": None,
        "results": None,
        "tie": [],
    }


def test_failed_run_can_be_retried_from_its_checkpoint(client, llm):
    llm.responses["_OverviewDraft"] = RuntimeError("OpenRouter indisponible")
    trip_id, (organizer, friend) = _launched(client, [1500, 1200])

    view = organizer.get(f"/api/trips/{trip_id}").json()
    assert view["status"] == "error" and view["error"] == RUN_FAILED_MESSAGE and view["can_retry"]
    assert friend.post(f"/api/trips/{trip_id}/retry").status_code == 403

    llm.responses["_OverviewDraft"] = {"pros": [], "cons": [], "estimated_budget_eur": 900, "best_period": "mai"}
    view = organizer.post(f"/api/trips/{trip_id}/retry").json()

    assert view["status"] == "voting" and len(view["suggestions"]) == 3
    assert len(llm.prompts("_CandidateCountries")) == 1  # reprise au checkpoint : research n'est pas rejoué


def test_run_stopped_by_a_restart_is_reported_and_resumable(client, llm, graph, app):
    llm.responses["_OverviewDraft"] = RuntimeError("serveur arrêté")
    trip_id, _ = _launched(client, [1500])

    # Nouveau runner sur le même checkpointer : il ne sait rien du run précédent (redémarrage).
    restarted = TripRunner(graph)
    agent = restarted.state(trip_id)
    assert agent.status == "error" and agent.resumable and agent.error == RUN_STOPPED_MESSAGE


def test_retry_after_a_final_guardrail_error_restarts_the_suggestions(client, llm):
    llm.responses["_HotelDraft"] = {"name": "Palace", "price_eur": 50_000, "notes": ""}
    trip_id, (organizer,) = _launched(client, [1500])
    _vote(organizer, trip_id, "Portugal")

    view = organizer.get(f"/api/trips/{trip_id}").json()
    assert view["status"] == "error" and "dépasse le budget" in view["error"]

    view = organizer.post(f"/api/trips/{trip_id}/retry").json()
    assert view["status"] == "voting" and view["vote"]["results"] is None


class _PendingExecutor(Executor):
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args))


def test_runner_runs_one_job_per_trip(graph):
    executor = _PendingExecutor()
    runner = TripRunner(graph, executor)

    runner.submit("t1", "generating", {"messages": []})
    assert runner.state("t1").status == "generating"
    with pytest.raises(TripBusyError):
        runner.submit("t1", "planning", None)
    runner.submit("t2", "generating", {"messages": []})  # un autre voyage n'est pas bloqué
    assert len(executor.jobs) == 2


# --- T36 / T37 : vote et égalité ------------------------------------------------------------


def test_secret_vote_selects_the_winner_and_plans_the_trip(client):
    trip_id, members = _launched(client, [1500, 1200, 900])

    _vote(members[0], trip_id, "Portugal")
    view = _vote(members[1], trip_id, "grèce").json()  # casse ignorée
    assert view["vote"]["votes_cast"] == 2 and view["vote"]["my_vote"] == "Grèce"
    assert view["vote"]["results"] is None  # rien n'est révélé avant le dernier bulletin
    assert [m["voted"] for m in view["members"]] == [True, True, False]
    other = members[2].get(f"/api/trips/{trip_id}").json()["vote"]
    assert other["my_vote"] is None and other["results"] is None  # le vote des autres reste secret

    _vote(members[1], trip_id, "Portugal")  # on peut changer d'avis
    view = _vote(members[2], trip_id, "Croatie").json()

    assert view["status"] == "done"
    assert view["vote"]["results"] == {"Portugal": 2, "Grèce": 0, "Croatie": 1}
    assert view["plan"]["selected_country"] == "Portugal"


def test_vote_validation(client):
    trip_id, (organizer,) = _launched(client, [1500])

    response = _vote(organizer, trip_id, "Atlantide")
    assert response.status_code == 422 and "Portugal" in response.json()["detail"]
    assert _vote(client(), trip_id, "Portugal").status_code == 401


def test_vote_is_only_possible_while_voting(client):
    trip_id, (organizer,) = _group(client, [1500])

    assert _vote(organizer, trip_id, "Portugal").status_code == 409


def test_organizer_can_close_the_vote_early(client):
    trip_id, (organizer, friend, other) = _launched(client, [1500, 1200, 900])
    assert organizer.post(f"/api/trips/{trip_id}/votes/close").status_code == 409  # aucun vote
    _vote(friend, trip_id, "Croatie")

    assert other.post(f"/api/trips/{trip_id}/votes/close").status_code == 403
    view = organizer.post(f"/api/trips/{trip_id}/votes/close").json()

    assert view["status"] == "done" and view["plan"]["selected_country"] == "Croatie"


def test_tie_then_second_round(client):
    trip_id, (organizer, friend) = _launched(client, [1500, 1200])
    _vote(organizer, trip_id, "Portugal")
    view = _vote(friend, trip_id, "Grèce").json()

    assert view["status"] == "voting" and view["vote"]["tie"] == ["Portugal", "Grèce"]
    assert view["vote"]["results"] == {"Portugal": 1, "Grèce": 1, "Croatie": 0}
    assert _vote(friend, trip_id, "Portugal").status_code == 409  # l'organisatrice doit trancher
    assert friend.post(f"/api/trips/{trip_id}/tie", json={"method": "revote"}).status_code == 403

    view = organizer.post(f"/api/trips/{trip_id}/tie", json={"method": "revote"}).json()
    assert view["vote"]["round"] == 2 and view["vote"]["options"] == ["Portugal", "Grèce"]
    assert _vote(organizer, trip_id, "Croatie").status_code == 422  # plus au second tour

    _vote(organizer, trip_id, "Grèce")
    view = _vote(friend, trip_id, "Grèce").json()
    assert view["status"] == "done" and view["plan"]["selected_country"] == "Grèce"


def test_tie_broken_at_random(client):
    trip_id, (organizer, friend) = _launched(client, [1500, 1200])
    assert organizer.post(f"/api/trips/{trip_id}/tie", json={"method": "random"}).status_code == 409
    _vote(organizer, trip_id, "Portugal")
    _vote(friend, trip_id, "Grèce")

    view = organizer.post(f"/api/trips/{trip_id}/tie", json={"method": "random"}).json()

    assert view["status"] == "done" and view["plan"]["selected_country"] in ("Portugal", "Grèce")
    assert view["vote"]["tie"] == []


# --- T38 : autres idées ----------------------------------------------------------------------


def test_more_ideas_restarts_suggestions_with_feedback(client, llm):
    trip_id, (organizer, friend) = _launched(client, [1500, 1200])
    _vote(friend, trip_id, "Grèce")
    llm.responses["_CandidateCountries"] = {"countries": ["Islande", "Norvège", "Écosse"]}

    assert friend.post(f"/api/trips/{trip_id}/more-ideas", json={"feedback": "plus au nord"}).status_code == 403
    view = organizer.post(f"/api/trips/{trip_id}/more-ideas", json={"feedback": "plus au nord"}).json()

    assert view["status"] == "voting"
    assert [s["country"] for s in view["suggestions"]] == ["Islande", "Norvège", "Écosse"]
    assert view["vote"]["votes_cast"] == 0 and view["vote"]["options"] == ["Islande", "Norvège", "Écosse"]
    prompt = llm.prompts("_CandidateCountries")[-1]
    assert "plus au nord" in prompt and "Croatie, Grèce, Portugal" in prompt


# --- T39 : plan final ---------------------------------------------------------------------------


def test_plan_ledger_for_a_group_of_three(client):
    trip_id, members = _launched(client, [1000, 1200, 2000])
    for member in members:
        _vote(member, trip_id, "Portugal")

    plan = members[1].get(f"/api/trips/{trip_id}").json()["plan"]

    total = 3 * FLIGHT_EUR + HOTEL_EUR
    assert plan["ledger"] == [
        {"label": "Vols", "amount_eur": 3 * FLIGHT_EUR},
        {"label": "Hébergement", "amount_eur": HOTEL_EUR},
    ]
    assert plan["total_cost_eur"] == total and plan["per_person_eur"] == round(total / 3, 2)
    assert plan["group_budget_eur"] == 4200 and plan["gap_eur"] == total - 4200
    assert plan["gap_pct"] == round((total - 4200) / 4200 * 100, 1)
    assert plan["my_share_eur"] == round(FLIGHT_EUR + HOTEL_EUR / 3, 2) and plan["within_my_budget"]
    assert plan["within_each_budget"] and len(plan["itinerary"]) == 1


def test_plan_with_two_people_only_says_if_it_fits(client):
    trip_id, members = _launched(client, [400, 3000])
    for member in members:
        _vote(member, trip_id, "Portugal")

    plan = members[1].get(f"/api/trips/{trip_id}").json()["plan"]

    assert plan["group_budget_eur"] is None and plan["gap_eur"] is None and plan["gap_pct"] is None
    assert plan["within_each_budget"] is False  # 250 + 400 > 400 pour le premier membre
    assert plan["within_my_budget"] is True


def test_final_review_by_the_organizer(client, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    trip_id, (organizer, friend) = _launched(client, [1500, 1200])
    _vote(organizer, trip_id, "Portugal")
    view = _vote(friend, trip_id, "Portugal").json()

    assert view["status"] == "reviewing" and view["plan"]["total_cost_eur"] == 2 * FLIGHT_EUR + HOTEL_EUR
    assert friend.post(f"/api/trips/{trip_id}/plan/approve").status_code == 403

    view = organizer.post(f"/api/trips/{trip_id}/plan/adjust", json={"feedback": "avec piscine"}).json()
    assert view["status"] == "reviewing" and "avec piscine" in llm.prompts("_HotelDraft")[-1]

    view = organizer.post(f"/api/trips/{trip_id}/plan/approve").json()
    assert view["status"] == "done" and view["plan"]["selected_country"] == "Portugal"
    assert organizer.post(f"/api/trips/{trip_id}/plan/approve").status_code == 409
