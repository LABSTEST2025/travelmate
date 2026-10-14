"""Parcours web complet à plusieurs membres (T54), au niveau de l'API, avec contrôle de la
confidentialité des budgets à chaque étape. Complète la checklist manuelle docs/checklist-navigateur.md."""

import pytest

from tests.test_api import _create, _prefs, _vote, app, client, graph  # noqa: F401  (fixtures)

BUDGETS = {"Maya": 1234.5, "Léa": 1567.5, "Inès": 2089.5}


@pytest.fixture
def journey(client, llm, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    members = {name: client() for name in [*BUDGETS, "Karim"]}
    trip_id = _create(members["Maya"], "Maya")
    for name in ("Léa", "Inès", "Karim"):
        assert members[name].post(f"/api/trips/{trip_id}/members", json={"name": name}).status_code == 200
    return trip_id, members


def _view(member, trip_id) -> dict:
    return member.get(f"/api/trips/{trip_id}").json()


def _assert_no_budget_leak(trip_id, members, visitor):
    for name, member in members.items():
        body = member.get(f"/api/trips/{trip_id}").text
        leaked = [other for other, budget in BUDGETS.items() if other != name and str(budget) in body]
        assert leaked == [], f"{name} voit le budget de {leaked}"
    body = visitor.get(f"/api/trips/{trip_id}").text
    assert not [b for b in BUDGETS.values() if str(b) in body]


def test_full_journey_with_four_members(journey, client, llm):
    trip_id, members = journey
    maya, lea, ines, karim = members.values()
    visitor = client()

    # 1. Envies : Karim ne répond pas.
    for name in BUDGETS:
        response = members[name].put(f"/api/trips/{trip_id}/preferences", json=_prefs(budget_eur=BUDGETS[name]))
        assert response.status_code == 200
    view = _view(lea, trip_id)
    assert view["status"] == "collecting" and view["responses"] == 3
    assert view["group_budget_range_eur"] == [1200, 2100]
    assert [m["submitted"] for m in view["members"]] == [True, True, True, False]
    _assert_no_budget_leak(trip_id, members, visitor)

    # 2. Lancement sans le retardataire, qui ne peut plus rejoindre ni voter.
    view = maya.post(f"/api/trips/{trip_id}/launch").json()
    assert view["status"] == "voting" and view["vote"]["voters_total"] == 3
    assert karim.put(f"/api/trips/{trip_id}/preferences", json=_prefs()).status_code == 409
    assert _vote(karim, trip_id, "Portugal").status_code == 403
    _assert_no_budget_leak(trip_id, members, visitor)

    # 3. Un tour d'« autres idées ».
    llm.responses["_CandidateCountries"] = {"countries": ["Islande", "Norvège", "Écosse"]}
    view = maya.post(f"/api/trips/{trip_id}/more-ideas", json={"feedback": "plus au nord"}).json()
    assert view["vote"]["options"] == ["Islande", "Norvège", "Écosse"]

    # 4. Égalité à 1-1-1, second tour entre les trois, puis un gagnant.
    _vote(maya, trip_id, "Islande")
    _vote(lea, trip_id, "Norvège")
    view = _vote(ines, trip_id, "Écosse").json()
    assert view["vote"]["tie"] == ["Islande", "Norvège", "Écosse"]
    view = maya.post(f"/api/trips/{trip_id}/tie", json={"method": "revote"}).json()
    assert view["vote"]["round"] == 2
    _vote(maya, trip_id, "Norvège")
    _vote(lea, trip_id, "Norvège")
    view = _vote(ines, trip_id, "Islande").json()
    assert view["vote"]["results"] == {"Islande": 1, "Norvège": 2, "Écosse": 0}
    assert view["status"] == "reviewing" and view["plan"]["selected_country"] == "Norvège"
    _assert_no_budget_leak(trip_id, members, visitor)

    # 5. Ajustement puis validation du plan par l'organisatrice.
    llm.responses["_StayDraft"] = lambda prompt: {"city": "Bergen" if "Bergen" in prompt else "Oslo"}
    view = maya.post(f"/api/trips/{trip_id}/plan/adjust", json={"feedback": "plutôt Bergen"}).json()
    assert view["status"] == "reviewing"
    assert view["plan"]["flights"][0]["destination"] == "Bergen (Norvège)"
    view = maya.post(f"/api/trips/{trip_id}/plan/approve").json()
    assert view["status"] == "done"

    # 6. Plan final, vu par chacun : grand livre commun, part et budget personnels.
    for name in BUDGETS:
        plan = _view(members[name], trip_id)["plan"]
        assert plan["group_budget_eur"] == 4900 and plan["within_my_budget"] is True
        assert [line["label"] for line in plan["ledger"]] == ["Vols", "Hébergement"]
    assert _view(karim, trip_id)["plan"]["my_share_eur"] is None  # pas participant
    _assert_no_budget_leak(trip_id, members, visitor)
