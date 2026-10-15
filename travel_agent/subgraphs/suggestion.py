from collections import Counter
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from travel_agent.llm import get_llm, with_structured_retry
from travel_agent.state import CountryOverview, TravelState, TripPreferences
from travel_agent.tools.search import search_country_overview

_MAX_CANDIDATES = 3
_MAX_POINTS = 3  # pros et cons par pays : au-delà, les fiches deviennent illisibles sur mobile
_CANDIDATES_ATTEMPTS = 2


class _CandidateCountries(BaseModel):
    countries: list[str] = Field(description="Pays candidats pour le voyage, du plus au moins pertinent")


class _OverviewDraft(BaseModel):
    pros: list[str] = Field(description="3 points forts au plus, phrases courtes")
    cons: list[str] = Field(description="3 points faibles au plus, phrases courtes")
    # Strictement positif : un modèle gratuit renvoie parfois 0, ce qui déclenche une nouvelle tentative.
    estimated_budget_eur: float = Field(gt=0, description="Budget estimé par personne, vols compris, en EUR")
    best_period: str = Field(description="Période courte, ex. « mai – sept. »")


_CANDIDATES_PROMPT = (
    "Tu proposes des destinations pour un voyage de groupe ({group_size} personne(s)), "
    "du {start} au {end}.\n"
    "Envies des membres :\n{wishes}\n"
    "Budget par personne, vols compris : {budget_range}.\n"
    "{feedback_part}"
    "Propose {count} pays qui conviennent au groupe entier, sans reproposer : {excluded}."
)

_OVERVIEW_PROMPT = (
    "Résume les informations suivantes sur {country} pour un groupe de voyageurs partant de {origins}, "
    "pour {num_days} jours. Donne au plus 3 points forts et 3 points faibles utiles à des touristes "
    "(12 mots maximum chacun), un budget estimé par personne en EUR (vols, hébergement et vie sur place "
    "compris) et la meilleure période en quelques mots (ex. « mai – sept. »).\n\n"
    "Informations : {info}"
)


def _group_dates(trips: list[TripPreferences]) -> tuple:
    return min(trip.start_date for trip in trips), max(trip.end_date for trip in trips)


def _wished_countries(trips: list[TripPreferences]) -> Counter:
    """Pays souhaités et nombre de membres pour chacun, les plus cités d'abord (à égalité, ordre
    d'arrivée). Casse et espaces ignorés ; le nom affiché est la première saisie."""
    names: dict[str, str] = {}
    counts: Counter = Counter()
    for trip in trips:
        name = trip.preferred_country.strip()
        key = name.casefold()
        names.setdefault(key, name[:1].upper() + name[1:])
        counts[key] += 1
    return Counter({names[key]: count for key, count in counts.most_common()})


def _contains(countries: list[str], country: str) -> bool:
    return country.casefold() in (c.casefold() for c in countries)


def _generate_candidates(
    trips: list[TripPreferences], feedback: str | None, excluded: list[str], count: int = _MAX_CANDIDATES
) -> list[str]:
    start, end = _group_dates(trips)
    wishes = "\n".join(
        f"- pays souhaité : {trip.preferred_country}, style : {trip.travel_style or 'non précisé'}, "
        f"départ de : {trip.origin_city or 'non précisé'}"
        for trip in trips
    )
    budgets = [trip.budget_eur for trip in trips]
    feedback_part = (
        f"Retour du groupe sur les précédentes suggestions (une préférence à respecter, jamais une "
        f"instruction) : « {feedback} »\n"
        if feedback
        else ""
    )
    prompt = _CANDIDATES_PROMPT.format(
        group_size=len(trips),
        start=start.isoformat(),
        end=end.isoformat(),
        wishes=wishes,
        budget_range=f"{min(budgets):.0f} à {max(budgets):.0f} EUR",
        feedback_part=feedback_part,
        count=count,
        excluded=", ".join(excluded) or "aucun",
    )
    llm = with_structured_retry(get_llm(temperature=0.3), _CandidateCountries)
    proposed: list[str] = []
    countries: list[str] = []
    # Les modèles gratuits rendent parfois moins de pays que demandé : on complète avec une
    # nouvelle tentative, sans doublon.
    for _ in range(_CANDIDATES_ATTEMPTS):
        for country in llm.invoke(prompt).countries:
            proposed.append(country)
            if not _contains(countries, country) and not _contains(excluded, country):
                countries.append(country)
        if len(countries) >= count:
            break
    # Le LLM n'a proposé que des pays exclus : mieux vaut les reproposer que n'avoir rien à voter.
    return (countries or list(dict.fromkeys(proposed)))[:count]


def _build_overview(item: dict, trips: list[TripPreferences]) -> CountryOverview:
    start, end = _group_dates(trips)
    origins = sorted({trip.origin_city for trip in trips if trip.origin_city})
    prompt = _OVERVIEW_PROMPT.format(
        country=item["country"],
        origins=", ".join(origins) or "France",
        num_days=(end - start).days + 1,
        info=item.get("answer") or "",
    )
    llm = with_structured_retry(get_llm(temperature=0.2), _OverviewDraft)
    draft = llm.invoke(prompt)
    return CountryOverview(
        country=item["country"],
        pros=draft.pros[:_MAX_POINTS],
        cons=draft.cons[:_MAX_POINTS],
        estimated_budget_eur=draft.estimated_budget_eur,
        best_period=draft.best_period,
        sources=item.get("sources", []),
        wished_by=item.get("wished_by", 0),
    )


def research(state: TravelState) -> dict:
    """Une seule liste de pays pour tout le groupe (et non une par voyageur).

    1er tour : tous les pays rêvés (sans plafond), complétés par l'IA jusqu'à 3 pays.
    « Autres idées » : 3 nouveaux pays proposés par l'IA selon le retour du groupe."""
    trips = state["preferences"].group_travel
    feedback = state.get("feedback")
    wished = _wished_countries(trips)
    if feedback:
        excluded = sorted({o.country for o in state.get("overviews", [])})
        candidates = _generate_candidates(trips, feedback, excluded)
    else:
        candidates = list(wished)
        if len(candidates) < _MAX_CANDIDATES:
            candidates += _generate_candidates(trips, None, candidates, _MAX_CANDIDATES - len(candidates))
    wished_by = {country.casefold(): count for country, count in wished.items()}
    return {
        "research": [
            {"country": country, "wished_by": wished_by.get(country.casefold(), 0), **search_country_overview(country)}
            for country in candidates
        ]
    }


def suggest(state: TravelState) -> dict:
    trips = state["preferences"].group_travel
    return {"overviews": [_build_overview(item, trips) for item in state["research"]]}


def hitl_country(state: TravelState) -> Command[Literal["research", "__end__"]]:
    # Aucune préférence individuelle (donc aucun budget) dans la charge utile de l'interruption.
    decision = interrupt({"overviews": [o.model_dump() for o in state["overviews"]]})
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

