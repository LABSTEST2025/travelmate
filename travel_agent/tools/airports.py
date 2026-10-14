import re
from functools import lru_cache

from pydantic import BaseModel, Field

from travel_agent.llm import get_llm, with_structured_retry

MAX_AIRPORTS = 3

_IATA_RE = re.compile(r"^[A-Z]{3}$")

_AIRPORTS_PROMPT = (
    "Donne les codes IATA (3 lettres majuscules) des {max_airports} principaux aéroports "
    "internationaux desservant : {place}. Ne renvoie que des aéroports de passagers existants, "
    "du plus au moins fréquenté."
)


class AirportResolutionError(ValueError):
    pass


class _AirportCodes(BaseModel):
    codes: list[str] = Field(description="Codes IATA des aéroports, ex. ['LIS', 'OPO']")


@lru_cache(maxsize=256)
def resolve_airports(place: str) -> tuple[str, ...]:
    """Traduit une ville ou un pays en codes IATA (ex. « Portugal » -> ('LIS', 'OPO', 'FAO'))."""
    llm = with_structured_retry(get_llm(temperature=0), _AirportCodes)
    result = llm.invoke(_AIRPORTS_PROMPT.format(place=place, max_airports=MAX_AIRPORTS))
    codes = [code.strip().upper() for code in result.codes]
    valid = tuple(dict.fromkeys(code for code in codes if _IATA_RE.match(code)))[:MAX_AIRPORTS]
    if not valid:
        raise AirportResolutionError(f"Impossible de trouver un aéroport pour « {place} ».")
    return valid
