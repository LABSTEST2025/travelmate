import os
import sqlite3
import uuid
from datetime import date

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from travel_agent.graph import build_travel_graph
from travel_agent.state import TripPreferences, TripResponseModelGroup, get_checkpoint_serde
from travel_agent.tracing import configure_tracing

load_dotenv()
configure_tracing()


def _prompt_date(label: str) -> date:
    while True:
        raw = input(f"{label} (AAAA-MM-JJ) : ").strip()
        try:
            return date.fromisoformat(raw)
        except ValueError:
            print("Format invalide, réessaie (ex. 2027-03-01).")


def _prompt_trip(index: int) -> TripPreferences:
    print(f"\n--- Voyageur {index} ---")
    preferred_country = input("Pays souhaité : ").strip()
    budget_eur = float(input("Budget (EUR) : ").strip())
    start_date = _prompt_date("Date de départ")
    end_date = _prompt_date("Date de retour")
    travel_style = input("Style de voyage (optionnel) : ").strip() or None
    origin_city = ""
    while not origin_city:
        origin_city = input("Ville de départ : ").strip()
    return TripPreferences(
        preferred_country=preferred_country,
        budget_eur=budget_eur,
        start_date=start_date,
        end_date=end_date,
        travel_style=travel_style,
        origin_city=origin_city,
    )


def _collect_preferences() -> TripResponseModelGroup:
    nb_travelers = int(input("Combien de voyageurs dans le groupe ? ").strip() or "1")
    trips = [_prompt_trip(i + 1) for i in range(nb_travelers)]
    return TripResponseModelGroup(group_travel=trips)


def _print_country_proposals(payload: dict) -> None:
    for overview in payload["overviews"]:
        print(
            f"\n- {overview['country']} (~{overview['estimated_budget_eur']:.0f} EUR / pers., "
            f"{overview['best_period']})"
        )
        print(f"    pour : {', '.join(overview['pros'])}")
        print(f"    contre : {', '.join(overview['cons'])}")


_PRICE_LEVELS = {"low": "bas", "typical": "habituel", "high": "élevé"}


def _format_flight(flight: dict) -> str:
    line = (
        f"  - {flight.get('origin')} → {flight.get('destination')}, "
        f"{flight.get('departure_date')} / {flight.get('return_date')}, "
        f"{flight.get('airline') or 'compagnie inconnue'}, {flight.get('price_eur', 0):.0f} EUR"
    )
    if flight.get("estimated"):
        line += " (estimation"
        if flight.get("price_range_eur"):
            low, high = flight["price_range_eur"]
            label = "fourchette habituelle" if flight.get("price_range_source") == "google" else "fourchette des vols trouvés"
            line += f", {label} {low:.0f}–{high:.0f} EUR"
        if flight.get("price_level") in _PRICE_LEVELS:
            line += f", prix actuel {_PRICE_LEVELS[flight['price_level']]}"
        line += ")"
    return line


def _print_plan_details(plan: dict) -> None:
    print("\nVols :")
    for flight in plan["flights"]:
        print(_format_flight(flight))
    print("\nHébergement :")
    for hotel in plan["hotels"]:
        print(
            f"  - {hotel['name']}, du {hotel.get('check_in')} au {hotel.get('check_out')}, "
            f"{hotel.get('price_eur', 0):.0f} EUR"
        )
        if hotel.get("notes"):
            print(f"    {hotel['notes']}")
    print("\nItinéraire :")
    for day in plan["itinerary"]:
        print(f"  Jour {day['day']} :")
        for activity in day["activities"]:
            print(f"    - {activity}")


def _print_final_proposal(payload: dict) -> None:
    print(f"\nPays retenu : {payload['selected_country']}")
    print(f"Coût total estimé : {payload['total_cost_eur']:.0f} EUR")
    _print_plan_details(payload)


def _print_final_plan(plan: dict) -> None:
    print("\n=== Plan validé ===")
    print(f"Pays : {plan['selected_country']}")
    print(f"Coût total : {plan['total_cost_eur']:.0f} EUR (budget : {plan['budget_eur']:.0f} EUR)")
    _print_plan_details(plan)
    items = plan["flights"] + plan["hotels"] + plan["itinerary"]
    sources = sorted({url for item in items for url in item.get("sources", [])})
    if sources:
        print("\nSources :")
        for url in sources:
            print(f"  - {url}")


def _resume_value_for(payload: dict) -> dict:
    if "overviews" in payload:
        choice = input("\nPays choisi par le groupe (laisser vide pour donner un feedback) : ").strip()
        if choice:
            return {"selected_country": choice}
        feedback = input("Feedback du groupe : ").strip()
        return {"feedback": feedback}

    approved = input("\nValider ce plan ? (o/n) : ").strip().lower()
    if approved in ("o", "oui", "y", "yes"):
        return {"approved": True}
    feedback = input("Ajustements souhaités : ").strip()
    return {"approved": False, "feedback": feedback}


def run() -> None:
    sqlite_path = os.environ.get("SQLITE_PATH", "checkpoints.sqlite")
    conn = sqlite3.connect(sqlite_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn, serde=get_checkpoint_serde())
    app = build_travel_graph().compile(checkpointer=checkpointer)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    print(f"[thread_id={thread_id}]")

    preferences = _collect_preferences()
    result = app.invoke({"preferences": preferences, "messages": []}, config=config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        if "overviews" in payload:
            print("\n=== Propositions de pays ===")
            _print_country_proposals(payload)
        else:
            print("\n=== Plan final proposé ===")
            _print_final_proposal(payload)
        result = app.invoke(Command(resume=_resume_value_for(payload)), config=config)

    if result.get("guardrail_error"):
        print(f"\nErreur : {result['guardrail_error']}")
    else:
        _print_final_plan(result["final_plan"])

    conn.close()


if __name__ == "__main__":
    run()
