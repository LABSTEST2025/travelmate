import logging
import threading
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

Stage = Literal["generating", "planning"]
Status = Literal["collecting", "generating", "voting", "planning", "reviewing", "done", "error"]

RUN_FAILED_MESSAGE = "L'agent n'a pas pu finir. Tes réponses sont gardées : tu peux réessayer."
RUN_STOPPED_MESSAGE = "La génération a été interrompue. Tes réponses sont gardées : tu peux réessayer."


class TripBusyError(RuntimeError):
    """Un run de l'agent est déjà en cours pour ce voyage."""


@dataclass(frozen=True)
class AgentState:
    status: Status
    values: dict
    interrupt: dict | None = None
    error: str | None = None
    # Le dernier run s'est arrêté en cours de route : `resume_pending` le reprend.
    resumable: bool = False

    @property
    def overviews(self) -> list:
        """Pays proposés. Tant que le sous-graphe de suggestion est interrompu, ses écritures ne sont
        pas encore reportées dans l'état parent : on les lit dans la charge utile de l'interruption."""
        if self.interrupt and "overviews" in self.interrupt:
            return self.interrupt["overviews"]
        return self.values.get("overviews") or []


def _config(trip_id: str) -> dict:
    return {"configurable": {"thread_id": trip_id}}


class TripRunner:
    """Lance le graphe en tâche de fond (un run à la fois par voyage) et déduit le statut du voyage
    de son checkpoint. Sans `executor`, le run est exécuté immédiatement (tests)."""

    def __init__(self, graph: CompiledStateGraph, executor: Executor | None = None):
        self._graph = graph
        self._executor = executor
        self._lock = threading.Lock()
        self._running: dict[str, Stage] = {}
        self._errors: dict[str, str] = {}

    def submit(self, trip_id: str, stage: Stage, graph_input: Any) -> None:
        with self._lock:
            if trip_id in self._running:
                raise TripBusyError(trip_id)
            self._running[trip_id] = stage
            self._errors.pop(trip_id, None)
        if self._executor is None:
            self._run(trip_id, graph_input)
        else:
            self._executor.submit(self._run, trip_id, graph_input)

    def _run(self, trip_id: str, graph_input: Any) -> None:
        try:
            self._graph.invoke(graph_input, _config(trip_id))
        except Exception:
            logger.exception("Run de l'agent en échec pour le voyage %s", trip_id)
            with self._lock:
                self._errors[trip_id] = RUN_FAILED_MESSAGE
        finally:
            with self._lock:
                self._running.pop(trip_id, None)

    def state(self, trip_id: str) -> AgentState:
        with self._lock:
            stage = self._running.get(trip_id)
            run_error = self._errors.get(trip_id)
        snapshot = self._graph.get_state(_config(trip_id))
        values = snapshot.values or {}
        if stage:
            return AgentState(status=stage, values=values)

        # Les interruptions d'un sous-graphe sont lues sur les tâches : après une erreur reprise,
        # `snapshot.interrupts` peut rester vide alors que la tâche est bien interrompue.
        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        if interrupts:
            payload = interrupts[0].value
            return AgentState(status="voting" if "overviews" in payload else "reviewing", values=values, interrupt=payload)
        if snapshot.next:
            return AgentState(status="error", values=values, error=run_error or RUN_STOPPED_MESSAGE, resumable=True)
        if values.get("final_plan"):
            return AgentState(status="done", values=values)
        return AgentState(status="error", values=values, error=values.get("guardrail_error") or run_error or RUN_FAILED_MESSAGE)

    def resume_pending(self, trip_id: str, stage: Stage) -> None:
        """Reprend un run arrêté en cours de route, depuis son dernier checkpoint."""
        self.submit(trip_id, stage, None)
