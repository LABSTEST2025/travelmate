"""Vue d'un voyage telle que l'API la renvoie à un membre.

Règles de confidentialité (cf. design « Règles communes ») :
- aucun budget individuel ne sort, sauf celui du membre qui regarde ;
- la fourchette du groupe, arrondie à 100 €, n'apparaît qu'à partir de 3 réponses ;
- en dessous, on dit seulement si un coût tient « dans le budget de chacun ».
"""

import math

from travel_agent.state import TripPreferences
from travel_agent.web.models import Member, TripRecord
from travel_agent.web.runner import AgentState

MIN_RESPONSES_FOR_RANGE = 3
_ROUNDING_EUR = 100


def _as_dict(value) -> dict:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _counted_members(trip: TripRecord) -> list[str]:
    """Avant le lancement : ceux qui ont répondu. Après : les participants figés au lancement."""
    return trip.participants if trip.launched else [m.id for m in trip.members if m.id in trip.preferences]


def _budgets(trip: TripRecord) -> list[float]:
    return [trip.preferences[member_id].budget_eur for member_id in _counted_members(trip)]


def budget_range(budgets: list[float]) -> list[int] | None:
    if len(budgets) < MIN_RESPONSES_FOR_RANGE:
        return None
    low = math.floor(min(budgets) / _ROUNDING_EUR) * _ROUNDING_EUR
    high = math.ceil(max(budgets) / _ROUNDING_EUR) * _ROUNDING_EUR
    return [low, high]


def _member_view(trip: TripRecord, member: Member) -> dict:
    return {
        "id": member.id,
        "name": member.name,
        "color": member.color,
        "is_organizer": member.is_organizer,
        "submitted": member.id in trip.preferences,
        "voted": member.id in trip.votes,
    }


def _me_view(trip: TripRecord, me: Member) -> dict:
    preferences: TripPreferences | None = trip.preferences.get(me.id)
    return _member_view(trip, me) | {
        "is_participant": me.id in trip.participants,
        "preferences": preferences.model_dump(mode="json") if preferences else None,
    }


def _suggestions_view(trip: TripRecord, overviews: list, budgets: list[float]) -> list[dict]:
    group_range = budget_range(budgets)
    suggestions = []
    for overview in map(_as_dict, overviews):
        estimate = overview["estimated_budget_eur"]
        suggestions.append(
            overview
            | {
                "fits_everyone_budget": bool(budgets) and estimate <= min(budgets),
                "in_group_range": group_range[0] <= estimate <= group_range[1] if group_range else None,
            }
        )
    return suggestions


def vote_options(trip: TripRecord, agent: AgentState) -> list[str]:
    """Pays soumis au vote : ceux du second tour, sinon tous les pays proposés par l'agent."""
    return trip.vote_options or [_as_dict(o)["country"] for o in agent.overviews]


def _vote_view(trip: TripRecord, me: Member | None, agent: AgentState) -> dict:
    return {
        "round": trip.vote_round,
        "options": vote_options(trip, agent),
        "voters_total": len(trip.participants),
        "votes_cast": len(trip.votes),
        "my_vote": trip.votes.get(me.id) if me else None,
        "results": trip.last_tally,
        "tie": trip.tie,
    }


_DAILY_LABELS = {"transport_eur": "Transports sur place", "meals_eur": "Repas", "activities_eur": "Activités"}


def _traveler_shares(flights: list[dict], hotels_total: float, daily: dict, total: float, count: int) -> list[float]:
    """Part de chaque participant : son vol + sa part d'hébergement + ses dépenses sur place. Vols et
    dépenses sur place sont rendus par voyageur, dans l'ordre de `group_travel` (= `participants`)."""
    per_traveler = daily.get("per_traveler_eur") or [daily.get("total_eur", 0) / count] * count
    if len(flights) != count or len(per_traveler) != count:
        return [total / count] * count
    return [flight.get("price_eur", 0) + hotels_total / count + own for flight, own in zip(flights, per_traveler)]


def _plan_view(trip: TripRecord, me: Member | None, plan: dict) -> dict:
    flights, hotels = plan.get("flights", []), plan.get("hotels", [])
    flights_total = sum(f.get("price_eur", 0) for f in flights)
    hotels_total = sum(h.get("price_eur", 0) for h in hotels)
    daily = plan.get("daily_costs") or {}
    total = plan.get("total_cost_eur") or flights_total + hotels_total + daily.get("total_eur", 0)
    count = max(len(trip.participants), 1)
    budgets = _budgets(trip)
    shares = _traveler_shares(flights, hotels_total, daily, total, count)

    view = {
        "selected_country": plan["selected_country"],
        "ledger": [
            {"label": "Vols", "amount_eur": round(flights_total, 2)},
            {"label": "Hébergement", "amount_eur": round(hotels_total, 2)},
            *(
                {"label": label, "amount_eur": daily["lines_eur"][key]}
                for key, label in _DAILY_LABELS.items()
                if key in daily.get("lines_eur", {})
            ),
        ],
        "daily_level": daily.get("level"),
        "total_cost_eur": round(total, 2),
        "per_person_eur": round(total / count, 2),
        "within_each_budget": len(budgets) == len(shares) and all(s <= b for s, b in zip(shares, budgets)),
        "group_budget_eur": None,
        "gap_eur": None,
        "gap_pct": None,
        "my_share_eur": None,
        "within_my_budget": None,
        "flights": flights,
        "hotels": hotels,
        "itinerary": plan.get("itinerary", []),
    }
    if len(budgets) >= MIN_RESPONSES_FOR_RANGE:
        group_budget = round(sum(budgets) / _ROUNDING_EUR) * _ROUNDING_EUR
        gap = total - group_budget
        view |= {
            "group_budget_eur": group_budget,
            "gap_eur": round(gap, 2),
            "gap_pct": round(gap / group_budget * 100, 1) if group_budget else None,
        }
    if me and me.id in trip.participants:
        my_share = shares[trip.participants.index(me.id)]
        view |= {
            "my_share_eur": round(my_share, 2),
            "within_my_budget": my_share <= trip.preferences[me.id].budget_eur,
        }
    return view


def trip_view(trip: TripRecord, me: Member | None, agent: AgentState | None) -> dict:
    budgets = _budgets(trip)
    view = {
        "id": trip.id,
        "name": trip.name,
        "status": agent.status if agent else "collecting",
        "error": agent.error if agent else None,
        "can_retry": bool(agent and agent.status == "error"),
        "progress": agent.progress if agent else None,
        "me": _me_view(trip, me) if me else None,
        "members": [_member_view(trip, m) for m in trip.members],
        "responses": len(budgets),
        "group_budget_range_eur": budget_range(budgets),
        "suggestions": None,
        "vote": None,
        "plan": None,
    }
    if agent is None:
        return view

    overviews = agent.overviews
    if overviews and agent.status in ("voting", "planning", "reviewing", "done", "error"):
        view["suggestions"] = _suggestions_view(trip, overviews, budgets)
        view["vote"] = _vote_view(trip, me, agent)

    plan = agent.values.get("final_plan") if agent.status == "done" else None
    if agent.status == "reviewing":
        plan = agent.interrupt
    if plan:
        view["plan"] = _plan_view(trip, me, plan)
    return view
