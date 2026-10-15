from datetime import date
from typing import Annotated, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import add_messages
from pydantic import BaseModel


class TripPreferences(BaseModel):
    preferred_country: str
    budget_eur: float
    start_date: date
    end_date: date
    travel_style: str | None = None  # ex. "aventure", "détente", "culture"
    origin_city: str | None = None


class TripResponseModelGroup(BaseModel):
    group_travel: list[TripPreferences]


class CountryOverview(BaseModel):
    country: str
    pros: list[str]
    cons: list[str]
    estimated_budget_eur: float
    best_period: str
    sources: list[str]
    wished_by: int = 0  # membres ayant choisi ce pays comme pays rêvé ; 0 = suggestion de l'IA


class FinalPlan(BaseModel):
    selected_country: str
    flights: list[dict]
    hotels: list[dict]
    itinerary: list[dict]
    total_cost_eur: float
    budget_eur: float
    daily_costs: dict | None = None  # transports locaux, repas, activités (B04)


CHECKPOINT_MODELS = (
    TripPreferences,
    TripResponseModelGroup,
    CountryOverview,
    FinalPlan,
)


def get_checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_MODELS)


class TravelState(TypedDict):
    messages: Annotated[list, add_messages]
    preferences: TripResponseModelGroup | None
    research: list[dict]
    overviews: list[CountryOverview]
    selected_country: str | None
    stay_city: str | None  # ville de séjour choisie par `planner` : vols, hôtel et itinéraire y convergent
    feedback: str | None  # retour du groupe (refus de pays, ajustement du plan)
    budget_note: str | None  # consigne de `plan_check` après un dépassement, distincte du retour du groupe
    flights: list[dict]
    hotels: list[dict]
    itinerary: list[dict]
    daily_costs: dict | None  # dépenses sur place du groupe, écrites par la branche `daily_costs`
    daily_level: str | None  # "standard" (défaut) ou "economy" : niveau de confort des dépenses sur place
    hotel_max_eur: float | None  # plafond d'hôtel choisi comme piste d'économie au HITL final (B05)
    total_cost_eur: float | None
    final_plan: dict | None
    plan_retries: int
    guardrail_error: str | None
