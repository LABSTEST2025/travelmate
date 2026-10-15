# Image du site Escapade (FastAPI + agent LangGraph). Un seul processus : SQLite et le verrou du
# dépôt de voyages l'imposent (ne pas lancer plusieurs instances).
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dépendances d'abord : la couche est réutilisée tant que uv.lock ne change pas.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY travel_agent ./travel_agent
RUN uv sync --frozen --no-dev

# Le proxy HTTPS de l'hébergeur est le seul accès au conteneur : on lui fait confiance pour l'IP
# du visiteur. Le fichier SQLite vit sur le volume monté en /data.
ENV WEB_HOST=0.0.0.0 \
    WEB_FORWARDED_ALLOW_IPS=* \
    WEB_SECURE_COOKIES=true \
    SQLITE_PATH=/data/checkpoints.sqlite

# Pas d'utilisateur dédié : les volumes Railway sont montés en root, un autre utilisateur ne
# pourrait pas écrire le fichier SQLite.

EXPOSE 8000
CMD ["/app/.venv/bin/travel-web"]
