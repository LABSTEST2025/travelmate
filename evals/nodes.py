"""Évaluations nœud par nœud : chaque évaluateur lance le vrai code du nœud sur un cas et renvoie ses
vérifications, plus un résumé de la sortie pour le rapport (et, plus tard, pour le juge LLM)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from travel_agent.guardrails import input as guard_input
from travel_agent.guardrails.input import short_text_error
from travel_agent.subgraphs import planning, suggestion
from travel_agent.tools import airports
from travel_agent.tools.search import search_country_overview

from evals.checks import (
    Check,
    check,
    in_list,
    mentions_any,
    is_country,
    looks_french,
    mentions_amount,
    normalize,
    same_place,
    sentence_count,
    word_count,
)
from evals.harness import Recorder, build_group, build_state


@dataclass
class NodeEval:
    name: str
    description: str
    run: Callable[[dict, Recorder], tuple[list[Check], Any]]


# --- Graph 1 --------------------------------------------------------------------------------------------


def eval_candidates(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`research` : pays proposés par le LLM (complément des pays rêvés, ou « autres idées »)."""
    trips = build_group(case["travelers"]).group_travel
    excluded = case.get("excluded", [])
    count = case.get("count", 3)
    final = suggestion._generate_candidates(trips, case.get("feedback"), excluded, count)
    raw = rec.first("_CandidateCountries").countries
    checks = [
        check("assez de pays", len(raw) >= count, f"{len(raw)} pays pour {count} demandés : {raw}"),
        check("aucun doublon", len({normalize(c) for c in raw}) == len(raw), f"{raw}"),
        check("aucun pays exclu", not [c for c in raw if in_list(c, excluded)], f"exclus reproposés : {[c for c in raw if in_list(c, excluded)]}"),
        check("vrais pays", all(is_country(c) for c in raw), f"inconnus : {[c for c in raw if not is_country(c)]}"),
        check("liste finale complète", len(final) == count, f"{final}"),
    ]
    if allowed := case.get("allowed"):
        outside = [c for c in raw if not in_list(c, allowed)]
        checks.append(check("retour du groupe respecté", not outside, f"hors de la zone demandée : {outside}"))
    return checks, {"raw": raw, "final": final}


def eval_overview(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`suggest` : fiche d'un pays (pros, cons, budget, période) à partir des informations Tavily."""
    trips = build_group(case["travelers"]).group_travel
    item = {"country": case["country"], **search_country_overview(case["country"])}
    overview = suggestion._build_overview(item, trips)
    raw = rec.first("_OverviewDraft")
    points = raw.pros + raw.cons
    low, high = case["budget_range_eur"]
    long_points = [p for p in points if word_count(p) > 12]
    checks = [
        check("1 à 3 points forts", 1 <= len(raw.pros) <= 3, f"{len(raw.pros)} points forts"),
        check("1 à 3 points faibles", 1 <= len(raw.cons) <= 3, f"{len(raw.cons)} points faibles"),
        check("points de 12 mots au plus", not long_points, f"{long_points}"),
        check("aucun point vide", all(p.strip() for p in points)),
        check("budget plausible", low <= raw.estimated_budget_eur <= high, f"{raw.estimated_budget_eur:.0f} € hors [{low}, {high}]"),
        check("période courte", 0 < len(raw.best_period) <= 30, f"« {raw.best_period} »"),
        check("en français", looks_french(" ".join(points + [raw.best_period])), f"{points}"),
    ]
    return checks, overview.model_dump(exclude={"sources"})


# --- Graph 2 --------------------------------------------------------------------------------------------


def eval_stay_city(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`planner` : ville de séjour dans le pays choisi, selon le retour du groupe."""
    city = planning.planner(build_state(case))["stay_city"]
    raw = rec.first("_StayDraft").city.strip()
    checks = [
        check("nom de ville valide", short_text_error("Ville", raw) is None, f"« {raw} »"),
        check("ville retenue par le code", city is not None, f"« {raw} » rejetée : repli sur le pays"),
        check("pas le nom du pays", not same_place(raw, case["country"]), f"« {raw} »"),
        check("ville connue du pays", mentions_any(raw, case["cities"]), f"« {raw} » absente de la liste de référence"),
    ]
    if expected := case.get("expected"):
        checks.append(check("retour du groupe respecté", mentions_any(raw, expected), f"« {raw} », attendu : {expected}"))
    return checks, {"stay_city": city}


def eval_hotel(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`hotels` : un hôtel dans la ville de séjour, sous le plafond, au prix plausible."""
    state = build_state(case)
    trips = state["preferences"].group_travel
    cap = sum(t.budget_eur for t in trips) * planning._HOTEL_BUDGET_SHARE
    nights = (max(t.end_date for t in trips) - min(t.start_date for t in trips)).days
    try:
        hotel = planning.hotels(state)["hotels"][0]
    except ValueError as exc:  # prix invraisemblable trois fois de suite
        return [check("hôtel obtenu", False, str(exc))], None
    drafts = rec.outputs("_HotelDraft")
    first = drafts[0]
    low, high = case["night_per_person_eur"]
    per_night_person = first.price_eur / max(nights, 1) / len(trips)
    notes = first.notes
    checks = [
        check("hôtel obtenu", True),
        check("prix plausible du premier coup", len(drafts) == 1, f"{len(drafts)} tentatives : {[d.price_eur for d in drafts]}"),
        check("sous le plafond", first.price_eur <= cap, f"{first.price_eur:.0f} € > {cap:.0f} €"),
        check("prix par nuit et par personne plausible", low <= per_night_person <= high, f"{per_night_person:.0f} € hors [{low}, {high}]"),
        check("nom d'hôtel réel", bool(first.name.strip()) and "aucun" not in first.name.casefold(), f"« {first.name} »"),
        check("notes en 2 phrases au plus", sentence_count(notes) <= 2, f"{sentence_count(notes)} phrases"),
        check(
            "plafond non mentionné",
            not mentions_amount(notes, cap) and "plafond" not in notes.casefold(),
            f"« {notes} »",
        ),
        check("en français", looks_french(notes), f"« {notes} »"),
    ]
    return checks, {"name": hotel["name"], "price_eur": hotel["price_eur"], "notes": hotel["notes"]}


def eval_itinerary(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`itinerary` : exactement un programme par jour, numéroté, sans jour vide."""
    state = build_state(case)
    trips = state["preferences"].group_travel
    num_days = (max(t.end_date for t in trips) - min(t.start_date for t in trips)).days + 1
    days = planning.itinerary(state)["itinerary"]
    first = rec.first("_ItineraryDraft")
    activities = [a for d in days for a in d["activities"]]
    repeated = len(activities) - len({normalize(a) for a in activities})
    checks = [
        check("bon nombre de jours du premier coup", len(first.days) == num_days, f"{len(first.days)} jours pour {num_days}"),
        check("bon nombre de jours au final", len(days) == num_days, f"{len(days)} jours pour {num_days}"),
        check("jours numérotés 1..N", [d["day"] for d in days] == list(range(1, len(days) + 1)), f"{[d['day'] for d in days]}"),
        check("aucun jour vide", all(d["activities"] for d in days)),
        check("1 à 6 activités par jour", all(1 <= len(d["activities"]) <= 6 for d in days), f"{[len(d['activities']) for d in days]}"),
        check("aucune activité répétée", repeated == 0, f"{repeated} répétition(s)"),
        check("en français", looks_french(" ".join(activities))),
    ]
    return checks, [{"day": d["day"], "activities": d["activities"]} for d in days]


def eval_daily_costs(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`daily_costs` : dépenses sur place par personne et par jour, standard et économique."""
    result = planning.daily_costs(build_state(case))["daily_costs"]
    raw = rec.first("_DailyCostsDraft")
    standard, economy = raw.standard.model_dump(), raw.economy.model_dump()
    pricier = [k for k in standard if economy[k] > standard[k]]
    low, high = case["standard_per_day_eur"]
    total = sum(standard.values())
    checks = [
        check("économique ≤ standard sur chaque poste", not pricier, f"postes plus chers en économique : {pricier}"),
        check("économique moins cher au total", sum(economy.values()) < total, f"{sum(economy.values()):.0f} € ≥ {total:.0f} €"),
        check("standard plausible par jour", low <= total <= high, f"{total:.0f} € hors [{low}, {high}]"),
    ]
    return checks, {"standard": standard, "economy": economy, "total_eur": result["total_eur"]}


def eval_airports(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """`tools/airports.py` : codes IATA d'une ville ou d'un pays (sert aux vols)."""
    try:
        codes = airports.resolve_airports(case["place"])
    except airports.AirportResolutionError as exc:
        return [check("aéroport trouvé", False, str(exc))], None
    raw = [c.strip().upper() for c in rec.first("_AirportCodes").codes]
    unknown = [c for c in raw if c not in case["known"]]
    checks = [
        check("aéroport trouvé", True),
        check("codes au format IATA", all(len(c) == 3 and c.isalpha() for c in raw), f"{raw}"),
        check("3 codes au plus", len(raw) <= airports.MAX_AIRPORTS, f"{raw}"),
        check("aéroport principal en tête", raw[0] in case["primary"], f"{raw[0]}, attendu : {case['primary']}"),
        check("aéroports attendus pour la zone", not unknown, f"inconnus : {unknown}"),
    ]
    return checks, {"codes": list(codes)}


def eval_topic(case: dict, rec: Recorder) -> tuple[list[Check], Any]:
    """Classifieur `_TopicCheck` (guard_in et textes libres du site) sur des messages étiquetés."""
    error, _ = guard_input.screen_free_text(case["message"])
    expected = {"ok": None, "off_topic": guard_input.OFF_TOPIC_MESSAGE, "injection": guard_input.INJECTION_MESSAGE}[case["label"]]
    got = {None: "ok", guard_input.OFF_TOPIC_MESSAGE: "off_topic", guard_input.INJECTION_MESSAGE: "injection"}[error]
    return [check(f"bien classé ({case['label']})", error == expected, f"classé « {got} »")], {"label": got}


NODES: dict[str, NodeEval] = {
    e.name: e
    for e in (
        NodeEval("candidates", "research — pays proposés par le LLM", eval_candidates),
        NodeEval("overview", "suggest — fiches pays", eval_overview),
        NodeEval("stay_city", "planner — ville de séjour", eval_stay_city),
        NodeEval("hotel", "hotels — hôtel du groupe", eval_hotel),
        NodeEval("itinerary", "itinerary — programme jour par jour", eval_itinerary),
        NodeEval("daily_costs", "daily_costs — dépenses sur place", eval_daily_costs),
        NodeEval("airports", "airports — codes IATA", eval_airports),
        NodeEval("topic", "guard_in — classifieur hors sujet / injection", eval_topic),
    )
}
