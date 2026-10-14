from dotenv import load_dotenv
from langchain_tavily import TavilySearch

load_dotenv()


def _get_tool(**kwargs) -> TavilySearch:
    return TavilySearch(max_results=5, include_answer=True, **kwargs)


def _run(query: str, **kwargs) -> dict:
    response = _get_tool(**kwargs).invoke({"query": query})
    results = response.get("results", [])
    return {
        "answer": response.get("answer"),
        "sources": [r["url"] for r in results],
        "results": results,
    }


def search_country_overview(country: str) -> dict:
    query = f"Climat, coût de la vie, visa et sécurité pour voyager en {country}"
    return _run(query)


def search_hotels(country: str, budget_eur: float, zones: list[str] | None = None) -> dict:
    zones_part = f" dans les zones suivantes : {', '.join(zones)}" if zones else ""
    query = f"Hôtels en {country}{zones_part} pour un budget total de {budget_eur:.0f} EUR"
    return _run(query)


def search_activities(country: str, travel_style: str | None = None, city: str | None = None) -> dict:
    style_part = f" de style {travel_style}" if travel_style else ""
    place = f"à {city} et ses environs ({country})" if city else f"en {country}"
    query = f"Activités et lieux à visiter {place} pour un voyage{style_part}"
    return _run(query)
