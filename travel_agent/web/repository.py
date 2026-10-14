import hashlib
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.store.base import BaseStore

from travel_agent.web.models import TripRecord

_NAMESPACE = ("trips",)


def new_id(nbytes: int = 9) -> str:
    """Identifiant non devinable, utilisable dans une URL (ex. le code d'invitation)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TripRepository:
    """Voyages rangés dans le store LangGraph. Un seul verrou : les lectures-modifications-écritures
    d'un même voyage ne s'entremêlent pas (un seul processus serveur, cf. TICKETS.md)."""

    def __init__(self, store: BaseStore):
        self._store = store
        self._lock = threading.RLock()

    def get(self, trip_id: str) -> TripRecord | None:
        with self._lock:
            item = self._store.get(_NAMESPACE, trip_id)
        return TripRecord.model_validate(item.value) if item else None

    def save(self, trip: TripRecord) -> None:
        with self._lock:
            self._store.put(_NAMESPACE, trip.id, trip.model_dump(mode="json"))

    @contextmanager
    def edit(self, trip_id: str) -> Iterator[TripRecord | None]:
        """Lit, laisse modifier puis enregistre le voyage, sous verrou. Rien n'est enregistré si le
        bloc lève une exception."""
        with self._lock:
            trip = self.get(trip_id)
            yield trip
            if trip is not None:
                self.save(trip)
