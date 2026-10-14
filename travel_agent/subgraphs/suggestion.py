from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from travel_agent.llm import get_llm, with_structured_retry
from travel_agent.state import CountryOverview, TravelState, TripOverviews, TripPreferences
from travel_agent.tools.search import search_country_overview

_MAX_CANDIDATES = 3


class _CandidateCountries(BaseModel):
    countries: list[str] = Field(description="Pays candidats pour le voyage, du plus au moins pertinent")


class _OverviewDraft(BaseModel):
    pros: list[str]
    cons: list[str]
    estimated_budget_eur: float
    best_period: str


_CANDIDATES_PROMPT = (
    "Tu proposes des destinations de voyage.\n"
    "Préférences : pays souhaité {preferred_country}, budget {budget_eur} EUR, "
    "style {travel_style}, ville de départ {origin_city}.\n"
    "{feedback_part}"
    "Propose jusqu'à {max_candidates} pays candidats pertinents (inclure le pays souhaité s'il est "
    "toujours pertinent), sans répéter les pays suivants déjà proposés : {excluded}."
)

_OVERVIEW_PROMPT = (
    "Résume les informations suivantes sur {country} pour un voyageur et produis des pros, des cons, "
    "un budget estimé en EUR pour le séjour et la meilleure période pour y aller.\n\n"
    "Informations : {info}"
)


def _generate_candidates(preferences: TripPreferences, feedback: str | None, excluded: list[str]) -> list[str]:
    feedback_part = f"Retour de l'utilisateur sur les précédentes suggestions : {feedback}\n" if feedback else ""
    prompt = _CANDIDATES_PROMPT.format(
        preferred_country=preferences.preferred_country,
        budget_eur=preferences.budget_eur,
        travel_style=preferences.travel_style or "non précisé",
        origin_city=preferences.origin_city or "non précisée",
        feedback_part=feedback_part,
        max_candidates=_MAX_CANDIDATES,
        excluded=", ".join(excluded) or "aucun",
    )
    llm = with_structured_retry(get_llm(temperature=0.3), _CandidateCountries)
    result = llm.invoke(prompt)
    return result.countries[:_MAX_CANDIDATES]


def _build_overview(item: dict) -> CountryOverview:
    llm = with_structured_retry(get_llm(temperature=0.2), _OverviewDraft)
    prompt = _OVERVIEW_PROMPT.format(country=item["country"], info=item.get("answer") or "")
    draft = llm.invoke(prompt)
    return CountryOverview(country=item["country"], sources=item.get("sources", []), **draft.model_dump())


def research(state: TravelState) -> dict:
    trips = state["preferences"].group_travel
    feedback = state.get("feedback")
    excluded = (
        sorted({o.country for trip_overviews in state.get("overviews", []) for o in trip_overviews.overviews})
        if feedback
        else []
    )
    research_per_trip = []
    for trip in trips:
        candidates = _generate_candidates(trip, feedback, excluded)
        if not feedback and trip.preferred_country not in candidates:
            candidates = [trip.preferred_country, *candidates][:_MAX_CANDIDATES]
        results = [{"country": country, **search_country_overview(country)} for country in candidates]
        research_per_trip.append({"preferences": trip, "results": results})
    return {"research": research_per_trip}


def suggest(state: TravelState) -> dict:
    overviews = [
        TripOverviews(
            preferences=trip_research["preferences"],
            overviews=[_build_overview(item) for item in trip_research["results"]],
        )
        for trip_research in state["research"]
    ]
    return {"overviews": overviews}


def hitl_country(state: TravelState) -> Command[Literal["research", "__end__"]]:
    payload = {
        "trips": [
            {
                "preferences": trip_overviews.preferences.model_dump(),
                "overviews": [o.model_dump() for o in trip_overviews.overviews],
            }
            for trip_overviews in state["overviews"]
        ]
    }
    decision = interrupt(payload)
    selected = decision.get("selected_country")
    if selected:
        return Command(goto=END, update={"selected_country": selected, "feedback": None})
    return Command(goto="research", update={"feedback": decision.get("feedback", "")})


def build_suggestion_graph() -> StateGraph:
    graph = StateGraph(TravelState)
    graph.add_node("research", research)
    graph.add_node("suggest", suggest)
    graph.add_node("hitl_country", hitl_country)
    graph.add_edge(START, "research")
    graph.add_edge("research", "suggest")
    graph.add_edge("suggest", "hitl_country")
    return graph

