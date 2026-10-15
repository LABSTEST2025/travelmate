import re

from pydantic import ValidationError

from travel_agent.state import FinalPlan, TravelState

MAX_PLAN_RETRIES = 2

_URL_RE = re.compile(r"https?://\S+")

_START_DATE_KEYS = ("departure_date", "check_in", "start_date")
_END_DATE_KEYS = ("return_date", "check_out", "end_date")


def _extract_date_range(item: dict) -> tuple[str | None, str | None]:
    start = next((item[k] for k in _START_DATE_KEYS if item.get(k)), None)
    end = next((item[k] for k in _END_DATE_KEYS if item.get(k)), None)
    return start, end


def _dates_are_consistent(flights: list[dict], hotels: list[dict]) -> bool:
    """L'hôtel du groupe doit couvrir du premier départ au dernier retour."""
    flight_ranges = [r for f in flights if None not in (r := _extract_date_range(f))]
    hotel_ranges = [r for h in hotels if None not in (r := _extract_date_range(h))]
    if not flight_ranges or not hotel_ranges:
        return True
    expected = (min(start for start, _ in flight_ranges), max(end for _, end in flight_ranges))
    return all(hotel_range == expected for hotel_range in hotel_ranges)


def _collect_known_urls(*collections: list[dict]) -> set[str]:
    known: set[str] = set()
    for items in collections:
        for item in items:
            for key in ("url", "source"):
                if item.get(key):
                    known.add(item[key])
            for source in item.get("sources") or []:
                known.add(source)
    return known


def _strip_unknown_urls(value, known_urls: set[str]):
    if isinstance(value, str):
        return _URL_RE.sub(lambda m: m.group(0) if m.group(0) in known_urls else "[URL retirée]", value)
    if isinstance(value, list):
        return [_strip_unknown_urls(item, known_urls) for item in value]
    if isinstance(value, dict):
        return {key: _strip_unknown_urls(val, known_urls) for key, val in value.items()}
    return value


def group_budget_eur(state: TravelState) -> float:
    return sum(trip.budget_eur for trip in state["preferences"].group_travel)


def check_plan(state: TravelState, *, can_review: bool) -> dict:
    """Contrôle du brouillon avant qu'il soit montré au groupe (fin de Graph 2).

    - dates incohérentes : erreur, le plan n'est pas montré ;
    - budget dépassé : nouvelle planification avec une `budget_note` (le retour du groupe est gardé),
      dans la limite de MAX_PLAN_RETRIES. Relances épuisées : le brouillon est montré tel quel si le
      groupe peut le relire (`can_review`), sinon c'est une erreur.
    """
    if state.get("guardrail_error"):
        return {}
    if not _dates_are_consistent(state.get("flights") or [], state.get("hotels") or []):
        return {"guardrail_error": "Incohérence entre les dates de vol et les dates d'hôtel."}
    total, budget = state.get("total_cost_eur") or 0, group_budget_eur(state)
    if total <= budget:
        return {}
    error = f"Le coût total ({total:.0f} EUR) dépasse le budget ({budget:.0f} EUR)."
    retries = state.get("plan_retries") or 0
    if retries < MAX_PLAN_RETRIES:
        return {"plan_retries": retries + 1, "budget_note": f"{error} Réduis le coût de l'hébergement et des activités."}
    return {} if can_review else {"guardrail_error": error}


def guard_out(state: TravelState) -> dict:
    preferences = state.get("preferences")
    selected_country = state.get("selected_country")
    flights = state.get("flights") or []
    hotels = state.get("hotels") or []
    itinerary = state.get("itinerary") or []
    total_cost_eur = state.get("total_cost_eur")

    if state.get("guardrail_error"):
        # Erreur déjà levée pendant la planification (ex. recherche de vols impossible).
        return {"final_plan": None}

    if preferences is None or not preferences.group_travel or selected_country is None:
        return {"guardrail_error": "Plan incomplet : préférences ou pays sélectionné manquant."}
    if total_cost_eur is None:
        return {"guardrail_error": "Plan incomplet : coût total non calculé."}

    try:
        plan = FinalPlan(
            selected_country=selected_country,
            flights=flights,
            hotels=hotels,
            itinerary=itinerary,
            total_cost_eur=total_cost_eur,
            budget_eur=group_budget_eur(state),
        )
    except ValidationError as exc:
        return {"guardrail_error": f"Plan final invalide : {exc}"}

    known_urls = _collect_known_urls(flights, hotels, itinerary)
    cleaned_plan = plan.model_copy(
        update={
            "flights": _strip_unknown_urls(plan.flights, known_urls),
            "hotels": _strip_unknown_urls(plan.hotels, known_urls),
            "itinerary": _strip_unknown_urls(plan.itinerary, known_urls),
        }
    )

    return {"guardrail_error": None, "final_plan": cleaned_plan.model_dump()}
