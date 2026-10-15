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
    feedback: str | None
    flights: list[dict]
    hotels: list[dict]
    itinerary: list[dict]
    total_cost_eur: float | None
    final_plan: dict | None
    plan_retries: int
    guardrail_error: str | None
