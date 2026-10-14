from datetime import date

import httpx
import pytest

from tests.conftest import load_fixture
from travel_agent.tools import airports, flights
from travel_agent.tools.flights import (
    FlightsSearchError,
    SerpApiFlightsProvider,
    StubFlightsProvider,
    _estimate,
    get_flights_provider,
)

START, END = date(2027, 5, 1), date(2027, 5, 8)
NO_RESULTS = {"error": "Google Flights hasn't returned any results for this query."}


class FakeSerpApi:
    """Remplace `httpx.get` : renvoie les réponses prévues dans l'ordre et garde les paramètres."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.params: list[dict] = []

    def __call__(self, url, params, timeout):
        self.params.append(params)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, body = response if isinstance(response, tuple) else (200, response)
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def serpapi(monkeypatch):
    def install(*responses) -> FakeSerpApi:
        fake = FakeSerpApi(*responses)
        monkeypatch.setattr(flights.httpx, "get", fake)
        monkeypatch.setattr(flights.time, "sleep", lambda _s: None)
        return fake

    monkeypatch.setattr(
        flights, "resolve_airports", lambda place: {"Paris": ("CDG", "ORY"), "Portugal": ("LIS", "OPO")}[place]
    )
    return install


def _search():
    return SerpApiFlightsProvider("cle-test").search("Paris", "Portugal", START, END, 1500)[0]


# --- estimation sur de vraies réponses SerpApi enregistrées -----------------


def test_estimate_uses_google_price_insights_when_available():
    estimate = _estimate(load_fixture("serpapi_single_airport.json"))

    assert estimate["price_eur"] == 182.5  # médiane des vols trouvés
    assert estimate["price_range_eur"] == [130, 380]
    assert estimate["price_range_source"] == "google"
    assert estimate["price_level"] == "typical"


def test_estimate_falls_back_to_quartiles_without_price_insights():
    estimate = _estimate(load_fixture("serpapi_multi_airports.json"))

    assert estimate["price_eur"] == 150.0
    assert estimate["price_range_eur"] == [125.0, 167.75]
    assert estimate["price_range_source"] == "options"
    assert estimate["price_level"] is None


def test_estimate_edge_cases():
    assert _estimate({}) is None
    single = _estimate({"best_flights": [{"price": 100, "flights": [{"airline": "A"}, {"airline": "B"}]}]})
    assert single["price_eur"] == 100 and single["price_range_eur"] is None
    assert single["airline"] == "A, B" and single["stops"] == 1


# --- SerpApiFlightsProvider ---------------------------------------------------


def test_search_queries_main_airports_first(serpapi):
    fake = serpapi(load_fixture("serpapi_single_airport.json"))

    flight = _search()

    assert len(fake.params) == 1
    params = fake.params[0]
    assert (params["departure_id"], params["arrival_id"]) == ("CDG", "LIS")
    assert params["type"] == 1 and params["adults"] == 1 and params["currency"] == "EUR"
    assert params["outbound_date"] == "2027-05-01" and params["return_date"] == "2027-05-08"
    assert flight["estimated"] is True and flight["price_eur"] == 182.5
    assert flight["departure_date"] == "2027-05-01" and flight["return_date"] == "2027-05-08"
    assert flight["sources"][0].startswith("https://www.google.com/travel/flights")


def test_search_falls_back_to_all_airports_without_results(serpapi):
    fake = serpapi(NO_RESULTS, load_fixture("serpapi_multi_airports.json"))

    flight = _search()

    assert [(p["departure_id"], p["arrival_id"]) for p in fake.params] == [("CDG", "LIS"), ("CDG,ORY", "LIS,OPO")]
    assert flight["origin_airports"] == ["CDG", "ORY"] and flight["price_range_source"] == "options"


def test_search_raises_when_no_flight_is_found(serpapi):
    serpapi(NO_RESULTS, NO_RESULTS)

    with pytest.raises(FlightsSearchError, match="Aucun vol trouvé de Paris vers Portugal"):
        _search()


def test_search_retries_on_rate_limit(serpapi):
    fake = serpapi((429, {}), (503, {}), load_fixture("serpapi_single_airport.json"))

    assert _search()["price_eur"] == 182.5
    assert len(fake.params) == 3


def test_search_errors_are_user_readable(serpapi):
    serpapi((401, {"error": "Invalid API key."}))
    with pytest.raises(FlightsSearchError, match="Invalid API key"):
        _search()

    serpapi((429, {}), (429, {}), (429, {}))
    with pytest.raises(FlightsSearchError, match="HTTP 429"):
        _search()

    serpapi(*[httpx.ConnectError("boom")] * 3)
    with pytest.raises(FlightsSearchError, match="injoignable"):
        _search()


def test_search_reports_unknown_places(monkeypatch):
    def unknown(place):
        raise airports.AirportResolutionError(f"Impossible de trouver un aéroport pour « {place} ».")

    monkeypatch.setattr(flights, "resolve_airports", unknown)
    with pytest.raises(FlightsSearchError, match="Atlantide"):
        SerpApiFlightsProvider("cle-test").search("Atlantide", "Portugal", START, END, 1500)


# --- choix du fournisseur et stub ---------------------------------------------


def test_provider_depends_on_serpapi_key(monkeypatch):
    assert isinstance(get_flights_provider(), StubFlightsProvider)
    monkeypatch.setenv("SERPAPI_API_KEY", "cle-test")
    assert isinstance(get_flights_provider(), SerpApiFlightsProvider)


def test_stub_returns_one_fake_round_trip():
    (flight,) = StubFlightsProvider().search("Paris", "Portugal", START, END, 1000)

    assert flight["price_eur"] == 300 and flight["stub"] is True


# --- résolution des aéroports -------------------------------------------------


def test_resolve_airports_cleans_and_validates_codes(llm):
    llm.responses["_AirportCodes"] = {"codes": ["lis", "OPO", "OPO", "Porto", "FAO", "PDL"]}

    assert airports.resolve_airports("Portugal") == ("LIS", "OPO", "FAO")


def test_resolve_airports_is_cached(llm):
    airports.resolve_airports("Portugal")
    airports.resolve_airports("Portugal")

    assert len(llm.prompts("_AirportCodes")) == 1


def test_resolve_airports_fails_without_valid_code(llm):
    llm.responses["_AirportCodes"] = {"codes": ["Atlantide"]}

    with pytest.raises(airports.AirportResolutionError, match="Atlantide"):
        airports.resolve_airports("Atlantide")
