"""Données d'un voyage de groupe (lobby, votes) et corps des requêtes de l'API.

L'état de l'agent (pays proposés, plan…) vit dans le checkpointer LangGraph ; ce qui concerne le
groupe lui-même vit dans un `TripRecord`, rangé dans le store LangGraph du même fichier SQLite.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from travel_agent.state import TripPreferences

AVATAR_COLORS = ("#FF6B4A", "#FFC93C", "#3CCB9A", "#8F7CFF")


class Member(BaseModel):
    id: str
    name: str
    color: str
    token_hash: str
    is_organizer: bool = False


class TripRecord(BaseModel):
    id: str
    name: str
    created_at: datetime
    members: list[Member] = []
    # Envies par id de membre. Les budgets ne quittent jamais le serveur (cf. views.py).
    preferences: dict[str, TripPreferences] = {}
    launched: bool = False
    # Membres pris en compte par l'agent, figés au lancement, dans l'ordre de `group_travel`.
    participants: list[str] = []
    vote_round: int = 1
    # Pays soumis au vote ; vide = tous les pays proposés par l'agent.
    vote_options: list[str] = []
    votes: dict[str, str] = {}
    last_tally: dict[str, int] | None = None
    tie: list[str] = []

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)


# --- Corps des requêtes -----------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=40)]


class _In(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class CreateTripIn(_In):
    trip_name: str = Field(min_length=1, max_length=80)
    organizer_name: Name


class JoinIn(_In):
    name: Name


class PreferencesIn(_In):
    preferred_country: str = Field(min_length=1, max_length=60)
    budget_eur: float = Field(gt=0, le=100_000)
    start_date: date
    end_date: date
    travel_style: str | None = Field(default=None, max_length=40)
    origin_city: str = Field(min_length=1, max_length=60)

    def to_trip(self) -> TripPreferences:
        return TripPreferences(**self.model_dump())


class VoteIn(_In):
    country: str = Field(min_length=1, max_length=60)


class TieIn(_In):
    method: Literal["revote", "random"]


class FeedbackIn(_In):
    feedback: str = Field(min_length=1, max_length=500)


class SavingsIn(_In):
    options: list[Literal["economy", "hotel"]] = Field(min_length=1, max_length=2)
