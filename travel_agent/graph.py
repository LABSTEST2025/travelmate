from langgraph.graph import END, START, StateGraph

from travel_agent.guardrails.input import guard_in
from travel_agent.guardrails.output import guard_out
from travel_agent.state import TravelState, TripPreferences, TripResponseModelGroup
from travel_agent.subgraphs.planning import build_planning_graph
from travel_agent.subgraphs.suggestion import build_suggestion_graph


def _coerce_preferences(preferences) -> TripResponseModelGroup | None:
    if preferences is None or isinstance(preferences, TripResponseModelGroup):
        return preferences
    if isinstance(preferences, dict):
        return TripResponseModelGroup.model_validate(preferences)
    if isinstance(preferences, list):
        return TripResponseModelGroup(
            group_travel=[
                trip if isinstance(trip, TripPreferences) else TripPreferences.model_validate(trip)
                for trip in preferences
            ]
        )
    raise TypeError(f"Format de préférences non supporté : {type(preferences)!r}")


def input_node(state: TravelState) -> dict:
    return {
        "preferences": _coerce_preferences(state.get("preferences")),
        "research": state.get("research") or [],
        "overviews": state.get("overviews") or [],
        "flights": state.get("flights") or [],
        "hotels": state.get("hotels") or [],
        "itinerary": state.get("itinerary") or [],
        # Un nouveau run repart de zéro : un feedback laissé par un run précédent (ex. relance budget)
        # serait sinon pris pour un « autres idées » et exclurait tous les pays déjà proposés.
        "selected_country": None,
        "stay_city": None,
        "feedback": None,
        "total_cost_eur": None,
        "final_plan": None,
        "plan_retries": 0,
        "guardrail_error": None,
    }


def route_after_guard_in(state: TravelState) -> str:
    return END if state.get("guardrail_error") else "suggestion"


def build_travel_graph() -> StateGraph:
    graph = StateGraph(TravelState)
    graph.add_node("input", input_node)
    graph.add_node("guard_in", guard_in)
    graph.add_node("suggestion", build_suggestion_graph().compile())
    graph.add_node("planning", build_planning_graph().compile())
    graph.add_node("guard_out", guard_out)

    graph.add_edge(START, "input")
    graph.add_edge("input", "guard_in")
    graph.add_conditional_edges("guard_in", route_after_guard_in, ["suggestion", END])
    graph.add_edge("suggestion", "planning")
    graph.add_edge("planning", "guard_out")
    graph.add_edge("guard_out", END)

    return graph
