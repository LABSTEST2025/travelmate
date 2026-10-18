import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from travel_agent.graph import build_travel_graph
from travel_agent.state import get_checkpoint_serde
from travel_agent.tracing import configure_tracing
from travel_agent.web.app import create_app
from travel_agent.web.push import PushConfig, PushNotifier
from travel_agent.web.repository import TripRepository
from travel_agent.web.runner import TripRunner

_AGENT_WORKERS = 4


def build_app() -> FastAPI:
    """Application branchée sur le fichier SQLite de `SQLITE_PATH` : checkpoints de l'agent et
    voyages (store LangGraph) partagent le même fichier."""
    load_dotenv()
    configure_tracing()
    sqlite_path = os.environ.get("SQLITE_PATH", "checkpoints.sqlite")
    # Hébergé, le fichier vit sur un volume monté (ex. /data) : le dossier peut ne pas encore exister.
    os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)

    checkpoint_conn = sqlite3.connect(sqlite_path, check_same_thread=False)
    graph = build_travel_graph().compile(checkpointer=SqliteSaver(checkpoint_conn, serde=get_checkpoint_serde()))
    store_conn = sqlite3.connect(sqlite_path, check_same_thread=False, isolation_level=None)
    store = SqliteStore(store_conn)
    store.setup()
    executor = ThreadPoolExecutor(max_workers=_AGENT_WORKERS, thread_name_prefix="agent")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        executor.shutdown(wait=False, cancel_futures=True)
        store_conn.close()
        checkpoint_conn.close()

    return create_app(
        TripRepository(store),
        TripRunner(graph, executor),
        secure_cookies=os.environ.get("WEB_SECURE_COOKIES", "false").lower() == "true",
        lifespan=lifespan,
        notifier=PushNotifier(config) if (config := PushConfig.from_env()) else None,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    uvicorn.run(
        build_app(),
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        # `PORT` est imposé par les hébergeurs (Render…) ; `WEB_PORT` sert en local.
        port=int(os.environ.get("PORT") or os.environ.get("WEB_PORT", "8000")),
        # Derrière le proxy HTTPS de l'hébergeur : vraie IP du visiteur et schéma https.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("WEB_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
