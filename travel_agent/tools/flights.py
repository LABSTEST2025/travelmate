import os
import statistics
import time
from abc import ABC, abstractmethod
from datetime import date

import httpx

from travel_agent.tools.airports import AirportResolutionError, resolve_airports

SERPAPI_URL = "https://serpapi.com/search"
_TIMEOUT_S = 60
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Message renvoyé par SerpApi quand la recherche aboutit mais ne trouve aucun vol.
_NO_RESULTS_ERROR = "hasn't returned any results"


class FlightsSearchError(RuntimeError):
    """Erreur affichable à l'utilisateur : recherche de vols impossible ou sans résultat."""


class FlightsProvider(ABC):
    @abstractmethod
    def search(
        self,
        origin_city: str,
        destination_country: str,
        start_date: date,
        end_date: date,
        budget_eur: float,
    ) -> list[dict]:
        ...


class StubFlightsProvider(FlightsProvider):
    def search(
        self,
        origin_city: str,
        destination_country: str,
        start_date: date,
        end_date: date,
        budget_eur: float,
    ) -> list[dict]:
        return [
            {
                "origin": origin_city,
                "destination": destination_country,
                "departure_date": start_date.isoformat(),
                "return_date": end_date.isoformat(),
                "price_eur": round(budget_eur * 0.3, 2),
                "airline": "Stub Airlines",
                "stub": True,
            }
        ]


def _fetch_serpapi(params: dict) -> dict:
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = httpx.get(SERPAPI_URL, params=params, timeout=_TIMEOUT_S)
        except httpx.TransportError as exc:
            if attempt == _MAX_ATTEMPTS - 1:
                raise FlightsSearchError("Le service de recherche de vols est injoignable.") from exc
        else:
            if response.status_code not in _RETRYABLE_STATUS:
                break
            if attempt == _MAX_ATTEMPTS - 1:
                raise FlightsSearchError(
                    f"Le service de recherche de vols ne répond pas (HTTP {response.status_code})."
                )
        time.sleep(2**attempt)

    data = response.json()
    error = data.get("error")
    if error and _NO_RESULTS_ERROR not in error:
        raise FlightsSearchError(f"La recherche de vols a échoué : {error}")
    return data


def _priced_options(data: dict) -> list[dict]:
    return [o for o in data.get("best_flights", []) + data.get("other_flights", []) if o.get("price")]


def _estimate(data: dict) -> dict | None:
    """Prix estimé = médiane des vols trouvés (le minimum est souvent un low-cost sans bagage).
    Fourchette : celle de Google (`price_insights`) si disponible, sinon 1er–3e quartile des vols."""
    insights = data.get("price_insights") or {}
    options = _priced_options(data)
    prices = sorted(o["price"] for o in options)
    if not prices and not insights.get("lowest_price"):
        return None

    price = statistics.median(prices) if prices else insights["lowest_price"]
    if insights.get("typical_price_range"):
        price_range, range_source = insights["typical_price_range"], "google"
    elif len(prices) >= 2:
        q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
        price_range, range_source = [round(q1, 2), round(q3, 2)], "options"
    else:
        price_range, range_source = None, None

    representative = min(options, key=lambda o: abs(o["price"] - price), default={})
    legs = representative.get("flights", [])
    return {
        "price_eur": float(price),
        "price_range_eur": price_range,
        "price_range_source": range_source,
        "price_level": insights.get("price_level"),
        "airline": ", ".join(dict.fromkeys(leg["airline"] for leg in legs if leg.get("airline"))) or None,
        "stops": max(len(legs) - 1, 0) if legs else None,
    }


class SerpApiFlightsProvider(FlightsProvider):
    """Estimation du prix d'un aller-retour via SerpApi (Google Flights).
    Recherche d'abord entre les aéroports principaux : Google ne renvoie `price_insights` que pour
    une paire d'aéroports. Sans résultat, repli sur tous les aéroports de la ville et du pays.
    Une recherche par voyageur (`adults=1`) : le prix renvoyé est celui d'un passager."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _search(self, origin_airports, destination_airports, start_date: date, end_date: date) -> dict:
        return _fetch_serpapi(
            {
                "engine": "google_flights",
                "api_key": self._api_key,
                "departure_id": ",".join(origin_airports),
                "arrival_id": ",".join(destination_airports),
                "outbound_date": start_date.isoformat(),
                "return_date": end_date.isoformat(),
                "type": 1,
                "adults": 1,
                "travel_class": 1,
                "currency": "EUR",
                "hl": "fr",
                "gl": "fr",
            }
        )

    def search(
        self,
        origin_city: str,
        destination_country: str,
        start_date: date,
        end_date: date,
        budget_eur: float,
    ) -> list[dict]:
        try:
            origin_airports = resolve_airports(origin_city)
            destination_airports = resolve_airports(destination_country)
        except AirportResolutionError as exc:
            raise FlightsSearchError(str(exc)) from exc

        searched = (origin_airports[:1], destination_airports[:1])
        data = self._search(*searched, start_date, end_date)
        estimate = _estimate(data)
        if estimate is None and (len(origin_airports) > 1 or len(destination_airports) > 1):
            searched = (origin_airports, destination_airports)
            data = self._search(*searched, start_date, end_date)
            estimate = _estimate(data)
        if estimate is None:
            raise FlightsSearchError(
                f"Aucun vol trouvé de {origin_city} vers {destination_country} "
                f"du {start_date.isoformat()} au {end_date.isoformat()}."
            )

        search_url = data.get("search_metadata", {}).get("google_flights_url")
        return [
            {
                "origin": origin_city,
                "origin_airports": list(searched[0]),
                "destination": destination_country,
                "destination_airports": list(searched[1]),
                "departure_date": start_date.isoformat(),
                "return_date": end_date.isoformat(),
                **estimate,
                "estimated": True,
                "sources": [search_url] if search_url else [],
            }
        ]


def get_flights_provider() -> FlightsProvider:
    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    return SerpApiFlightsProvider(api_key) if api_key else StubFlightsProvider()
