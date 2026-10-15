import random
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from travel_agent.guardrails.input import screen_free_text, validate_trip
from travel_agent.state import TripResponseModelGroup
from travel_agent.web.models import (
    AVATAR_COLORS,
    CreateTripIn,
    FeedbackIn,
    JoinIn,
    Member,
    PreferencesIn,
    TieIn,
    TripRecord,
    VoteIn,
)
from travel_agent.web.repository import TripRepository, hash_token, new_id
from travel_agent.web.runner import AgentState, Status, TripBusyError, TripRunner
from travel_agent.web.views import trip_view, vote_options

MAX_MEMBERS = 20
STATIC_DIR = Path(__file__).parent / "static"
# Les fichiers du front sont revalidés à chaque chargement (ETag) : pas de version périmée en cache.
_NO_CACHE = {"Cache-Control": "no-cache"}
_COOKIE_MAX_AGE_S = 90 * 24 * 3600


def cookie_name(trip_id: str) -> str:
    return f"escapade_{trip_id}"


def create_app(repository: TripRepository, runner: TripRunner, *, secure_cookies: bool = False, lifespan=None) -> FastAPI:
    app = FastAPI(title="Escapade", lifespan=lifespan)
    api = APIRouter(prefix="/api")

    @app.exception_handler(TripBusyError)
    def _busy(_request: Request, _exc: TripBusyError) -> JSONResponse:
        return JSONResponse({"detail": "L'agent travaille déjà sur ce voyage, patiente un instant."}, status_code=409)

    # --- Accès ------------------------------------------------------------------------

    def load(trip_id: str) -> TripRecord:
        trip = repository.get(trip_id)
        if trip is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce voyage n'existe pas.")
        return trip

    @contextmanager
    def editing(trip_id: str) -> Iterator[TripRecord]:
        with repository.edit(trip_id) as trip:
            if trip is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce voyage n'existe pas.")
            yield trip

    def current_member(trip: TripRecord, request: Request) -> Member | None:
        token = request.cookies.get(cookie_name(trip.id))
        if not token:
            return None
        token_hash = hash_token(token)
        return next((m for m in trip.members if secrets.compare_digest(m.token_hash, token_hash)), None)

    def require_member(trip: TripRecord, request: Request) -> Member:
        member = current_member(trip, request)
        if member is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Rejoins d'abord le voyage avec ton lien d'invitation.")
        return member

    def require_organizer(trip: TripRecord, request: Request) -> Member:
        member = require_member(trip, request)
        if not member.is_organizer:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Seul·e l'organisateur·rice peut faire ça.")
        return member

    def agent_state(trip: TripRecord) -> AgentState | None:
        return runner.state(trip.id) if trip.launched else None

    def require_status(trip: TripRecord, *expected: Status) -> AgentState:
        agent = agent_state(trip)
        current = agent.status if agent else "collecting"
        if current not in expected:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Action impossible à cette étape du voyage ({current}).")
        return agent

    def screened(trip_id: str, request: Request, text: str, *statuses: Status) -> str:
        """Contrôle un texte libre avant de le confier à l'agent. Droits et étape sont vérifiés d'abord
        (pas d'appel au LLM pour une requête refusée), et hors verrou (l'appel prend quelques secondes)."""
        trip = load(trip_id)
        require_organizer(trip, request)
        require_status(trip, *statuses)
        error, cleaned = screen_free_text(text)
        if error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
        return cleaned

    def view(trip: TripRecord, member: Member | None) -> dict:
        return trip_view(trip, member, agent_state(trip))

    def set_member_cookie(response: Response, trip_id: str, token: str) -> None:
        response.set_cookie(
            cookie_name(trip_id),
            token,
            max_age=_COOKIE_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=secure_cookies,
        )

    def add_member(trip: TripRecord, name: str, response: Response, *, organizer: bool = False) -> Member:
        token = secrets.token_urlsafe(32)
        member = Member(
            id=new_id(6),
            name=name,
            color=AVATAR_COLORS[len(trip.members) % len(AVATAR_COLORS)],
            token_hash=hash_token(token),
            is_organizer=organizer,
        )
        trip.members.append(member)
        set_member_cookie(response, trip.id, token)
        return member

    # --- Agent ------------------------------------------------------------------------

    def start_generation(trip: TripRecord) -> None:
        group = TripResponseModelGroup(group_travel=[trip.preferences[member_id] for member_id in trip.participants])
        runner.submit(trip.id, "generating", {"preferences": group, "messages": []})

    def reset_vote(trip: TripRecord) -> None:
        trip.vote_round, trip.vote_options, trip.votes, trip.last_tally, trip.tie = 1, [], {}, None, []

    def select_country(trip: TripRecord, country: str) -> None:
        runner.submit(trip.id, "planning", Command(resume={"selected_country": country}))

    def tally(trip: TripRecord, options: list[str]) -> None:
        """Dépouillement : un gagnant relance la planification ; une égalité attend l'organisateur."""
        counts = {country: 0 for country in options}
        for country in trip.votes.values():
            counts[country] += 1
        best = max(counts.values())
        winners = [country for country, count in counts.items() if count == best]
        trip.last_tally, trip.votes = counts, {}
        if len(winners) == 1:
            trip.tie = []
            select_country(trip, winners[0])
        else:
            trip.tie = winners

    # --- Routes -----------------------------------------------------------------------

    @api.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @api.post("/trips", status_code=status.HTTP_201_CREATED)
    def create_trip(body: CreateTripIn, response: Response) -> dict:
        trip = TripRecord(id=new_id(), name=body.trip_name, created_at=datetime.now(UTC))
        organizer = add_member(trip, body.organizer_name, response, organizer=True)
        repository.save(trip)
        return view(trip, organizer)

    @api.get("/trips/{trip_id}")
    def get_trip(trip_id: str, request: Request, response: Response) -> dict:
        trip = load(trip_id)
        member = current_member(trip, request)
        if member:
            # Expiration glissante : chaque visite renouvelle le cookie (un voyage se prépare sur des mois).
            set_member_cookie(response, trip.id, request.cookies[cookie_name(trip.id)])
        return view(trip, member)

    @api.post("/trips/{trip_id}/members")
    def join_trip(trip_id: str, body: JoinIn, request: Request, response: Response) -> dict:
        with editing(trip_id) as trip:
            member = current_member(trip, request)
            if member is None:
                if trip.launched:
                    raise HTTPException(status.HTTP_409_CONFLICT, "Le voyage a déjà commencé : il n'est plus possible de le rejoindre.")
                if len(trip.members) >= MAX_MEMBERS:
                    raise HTTPException(status.HTTP_409_CONFLICT, f"Le groupe est complet ({MAX_MEMBERS} personnes).")
                member = add_member(trip, body.name, response)
        return view(trip, member)

    @api.put("/trips/{trip_id}/preferences")
    def submit_preferences(trip_id: str, body: PreferencesIn, request: Request) -> dict:
        with editing(trip_id) as trip:
            member = require_member(trip, request)
            if trip.launched:
                raise HTTPException(status.HTTP_409_CONFLICT, "Le voyage a déjà commencé : tes envies ne peuvent plus changer.")
            preferences = body.to_trip()
            if error := validate_trip(preferences):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
            trip.preferences[member.id] = preferences
        return view(trip, member)

    @api.post("/trips/{trip_id}/launch")
    def launch(trip_id: str, request: Request) -> dict:
        with editing(trip_id) as trip:
            organizer = require_organizer(trip, request)
            if trip.launched:
                raise HTTPException(status.HTTP_409_CONFLICT, "L'agent a déjà été lancé pour ce voyage.")
            participants = [m.id for m in trip.members if m.id in trip.preferences]
            if not participants:
                raise HTTPException(status.HTTP_409_CONFLICT, "Il faut au moins une personne qui a donné ses envies.")
            trip.launched, trip.participants = True, participants
            start_generation(trip)
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/retry")
    def retry(trip_id: str, request: Request) -> dict:
        with editing(trip_id) as trip:
            organizer = require_organizer(trip, request)
            agent = require_status(trip, "error")
            if agent.resumable:
                runner.resume_pending(trip.id, "planning" if agent.values.get("selected_country") else "generating")
            else:
                reset_vote(trip)
                start_generation(trip)
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/votes")
    def vote(trip_id: str, body: VoteIn, request: Request) -> dict:
        with editing(trip_id) as trip:
            member = require_member(trip, request)
            agent = require_status(trip, "voting")
            if member.id not in trip.participants:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Seuls les membres qui ont donné leurs envies votent.")
            if trip.tie:
                raise HTTPException(status.HTTP_409_CONFLICT, "Égalité : l'organisateur·rice doit d'abord trancher.")
            options = vote_options(trip, agent)
            country = next((o for o in options if o.casefold() == body.country.casefold()), None)
            if country is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Choisis parmi : {', '.join(options)}.")
            trip.votes[member.id] = country
            if len(trip.votes) == len(trip.participants):
                tally(trip, options)
        return view(trip, member)

    @api.post("/trips/{trip_id}/votes/close")
    def close_vote(trip_id: str, request: Request) -> dict:
        with editing(trip_id) as trip:
            organizer = require_organizer(trip, request)
            agent = require_status(trip, "voting")
            if not trip.votes:
                raise HTTPException(status.HTTP_409_CONFLICT, "Personne n'a encore voté.")
            tally(trip, vote_options(trip, agent))
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/tie")
    def break_tie(trip_id: str, body: TieIn, request: Request) -> dict:
        with editing(trip_id) as trip:
            organizer = require_organizer(trip, request)
            require_status(trip, "voting")
            if not trip.tie:
                raise HTTPException(status.HTTP_409_CONFLICT, "Il n'y a pas d'égalité à trancher.")
            if body.method == "random":
                select_country(trip, random.choice(trip.tie))
                trip.tie = []
            else:
                trip.vote_options, trip.tie, trip.vote_round = trip.tie, [], trip.vote_round + 1
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/more-ideas")
    def more_ideas(trip_id: str, body: FeedbackIn, request: Request) -> dict:
        feedback = screened(trip_id, request, body.feedback, "voting")
        with editing(trip_id) as trip:
            organizer = require_organizer(trip, request)
            require_status(trip, "voting")
            reset_vote(trip)
            runner.submit(trip.id, "generating", Command(resume={"feedback": feedback}))
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/plan/approve")
    def approve_plan(trip_id: str, request: Request) -> dict:
        trip = load(trip_id)
        organizer = require_organizer(trip, request)
        require_status(trip, "reviewing")
        # Valider ne rejoue que guard_out : le brouillon a déjà été chiffré et contrôlé (plan_check).
        runner.submit(
            trip.id, "planning", Command(resume={"approved": True}), done_steps=("flights", "hotels", "itinerary", "daily_costs", "plan_check")
        )
        return view(trip, organizer)

    @api.post("/trips/{trip_id}/plan/adjust")
    def adjust_plan(trip_id: str, body: FeedbackIn, request: Request) -> dict:
        feedback = screened(trip_id, request, body.feedback, "reviewing")
        trip = load(trip_id)
        organizer = require_organizer(trip, request)
        require_status(trip, "reviewing")
        runner.submit(trip.id, "planning", Command(resume={"approved": False, "feedback": feedback}))
        return view(trip, organizer)

    app.include_router(api)

    # --- Front (page unique, routée côté navigateur) -------------------------------------

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers.update(_NO_CACHE)
        return response

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json", headers=_NO_CACHE)

    # À la racine : un service worker ne contrôle que les pages situées sous son propre chemin.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers=_NO_CACHE)

    @app.get("/", include_in_schema=False)
    @app.get("/t/{trip_id}", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
