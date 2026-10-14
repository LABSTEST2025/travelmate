import os
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from travel_agent.llm import get_llm, with_structured_retry
from travel_agent.state import TravelState
from travel_agent.tools.flights import FlightsSearchError, get_flights_provider
from travel_agent.tools.search import search_activities, search_hotels

# Part du budget total du groupe allouée à l'hébergement (les vols et les activités se partagent le reste).
_HOTEL_BUDGET_SHARE = 0.5
_ITINERARY_ATTEMPTS = 2

_HOTEL_PROMPT = (
    "Propose un hôtel pour {group_size} personne(s), en {country}, du {check_in} au {check_out}. Le prix total du séjour "
    "pour tout le groupe ne doit pas dépasser {budget_eur:.0f} EUR. Ne mentionne pas ce plafond "
    "dans les notes : elles sont visibles par tout le groupe.\n"
    "Informations : {info}{feedback_part}"
)

_ITINERARY_PROMPT = (
    "Construis un itinéraire jour par jour pour un séjour de {num_days} jours en {country}, "
    "pour un groupe avec les styles de voyage suivants : {styles}. Donne exactement {num_days} jours, "
    "numérotés de 1 à {num_days}.\n"
    "Informations : {info}{feedback_part}"
)


class _HotelDraft(BaseModel):
    name: str
    # Strictement positif : un modèle a déjà renvoyé 0 €, ce qui faussait le total du voyage.
    price_eur: float = Field(gt=0, description="Prix total du séjour pour tout le groupe, en EUR")
    notes: str


class _ItineraryDay(BaseModel):
    day: int
    activities: list[str]


class _ItineraryDraft(BaseModel):
    days: list[_ItineraryDay]


def _final_hitl_enabled() -> bool:
    return os.environ.get("ENABLE_FINAL_HITL", "false").strip().lower() == "true"


def _build_hotel(
    country: str, group_size: int, check_in, check_out, budget_eur: float, query_result: dict, feedback: str | None
) -> dict:
    feedback_part = f"\nAjustement demandé par le groupe : {feedback}" if feedback else ""
    prompt = _HOTEL_PROMPT.format(
        group_size=group_size,
        country=country,
        check_in=check_in.isoformat(),
        check_out=check_out.isoformat(),
        budget_eur=budget_eur,
        info=query_result.get("answer") or "",
        feedback_part=feedback_part,
    )
    llm = with_structured_retry(get_llm(temperature=0.2), _HotelDraft)
    draft = llm.invoke(prompt)
    return {
        "name": draft.name,
        "price_eur": draft.price_eur,
        "notes": draft.notes,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "sources": query_result.get("sources", []),
    }


def _build_itinerary(country: str, num_days: int, styles: list[str], query_result: dict, feedback: str | None) -> _ItineraryDraft:
    feedback_part = f"\nAjustement demandé par le groupe : {feedback}" if feedback else ""
    prompt = _ITINERARY_PROMPT.format(
        country=country,
        num_days=num_days,
        styles=", ".join(styles) or "non précisé",
        info=query_result.get("answer") or "",
        feedback_part=feedback_part,
    )
    llm = with_structured_retry(get_llm(temperature=0.3), _ItineraryDraft)
    # Les modèles gratuits rendent parfois moins de jours que demandé : une seconde tentative, puis
    # on garde le programme le plus complet.
    best = llm.invoke(prompt)
    for _ in range(_ITINERARY_ATTEMPTS - 1):
        if len(best.days) >= num_days:
            break
        retry = llm.invoke(prompt)
        best = max(best, retry, key=lambda draft: len(draft.days))
    return best


def planner(state: TravelState) -> dict:
    return {}


def flights(state: TravelState) -> dict:
    trips = state["preferences"].group_travel
    selected_country = state["selected_country"]
    provider = get_flights_provider()
    try:
        results = [
            flight
            for trip in trips
            for flight in provider.search(
                trip.origin_city, selected_country, trip.start_date, trip.end_date, trip.budget_eur
            )
        ]
    except FlightsSearchError as exc:
        return {"flights": [], "guardrail_error": str(exc)}
    return {"flights": results}


def hotels(state: TravelState) -> dict:
    trips = state["preferences"].group_travel
    selected_country = state["selected_country"]
    feedback = state.get("feedback")
    check_in = min(trip.start_date for trip in trips)
    check_out = max(trip.end_date for trip in trips)
    budget_eur = sum(trip.budget_eur for trip in trips) * _HOTEL_BUDGET_SHARE
    query_result = search_hotels(selected_country, budget_eur)
    hotel = _build_hotel(selected_country, len(trips), check_in, check_out, budget_eur, query_result, feedback)
    return {"hotels": [hotel]}


def itinerary(state: TravelState) -> dict:
    trips = state["preferences"].group_travel
    selected_country = state["selected_country"]
    feedback = state.get("feedback")
    start = min(trip.start_date for trip in trips)
    end = max(trip.end_date for trip in trips)
    num_days = (end - start).days + 1
    styles = sorted({trip.travel_style for trip in trips if trip.travel_style})
    query_result = search_activities(selected_country, ", ".join(styles) or None)
    draft = _build_itinerary(selected_country, num_days, styles, query_result, feedback)
    sources = query_result.get("sources", [])
    return {"itinerary": [{"day": d.day, "activities": d.activities, "sources": sources} for d in draft.days]}


def aggregate(state: TravelState) -> dict:
    total_cost_eur = sum(f.get("price_eur", 0) for f in state.get("flights", [])) + sum(
        h.get("price_eur", 0) for h in state.get("hotels", [])
    )
    return {"total_cost_eur": round(total_cost_eur, 2)}


def route_after_aggregate(state: TravelState) -> str:
    if state.get("guardrail_error") or not _final_hitl_enabled():
        return END
    return "hitl_final"


def hitl_final(state: TravelState) -> Command[Literal["planner", "__end__"]]:
    payload = {
        "selected_country": state["selected_country"],
        "flights": state.get("flights", []),
        "hotels": state.get("hotels", []),
        "itinerary": state.get("itinerary", []),
        "total_cost_eur": state["total_cost_eur"],
    }
    decision = interrupt(payload)
    if decision.get("approved"):
        return Command(goto=END)
    return Command(goto="planner", update={"feedback": decision.get("feedback", "")})


def build_planning_graph() -> StateGraph:
    graph = StateGraph(TravelState)
    graph.add_node("planner", planner)
    graph.add_node("flights", flights)
    graph.add_node("hotels", hotels)
    graph.add_node("itinerary", itinerary)
    graph.add_node("aggregate", aggregate)
    graph.add_node("hitl_final", hitl_final)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "flights")
    graph.add_edge("planner", "hotels")
    graph.add_edge("planner", "itinerary")
    graph.add_edge("flights", "aggregate")
    graph.add_edge("hotels", "aggregate")
    graph.add_edge("itinerary", "aggregate")
    graph.add_conditional_edges("aggregate", route_after_aggregate, ["hitl_final", END])

    return graph
