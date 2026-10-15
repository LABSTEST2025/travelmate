# Escapade — agent de voyage de groupe (LangGraph)

Chacun donne ses envies (pays rêvé, budget privé, dates, style, ville de départ), l'IA propose trois
pays au groupe, le groupe vote, puis l'agent planifie le voyage : vols, hébergement, itinéraire jour par
jour et budget détaillé. Utilisable depuis un **site web responsif installable (PWA)** ou en **ligne de
commande**.

- Architecture, conventions, règles de confidentialité : [`CLAUDE.md`](CLAUDE.md)
- Diagramme : [`diagrams.drawio`](diagrams.drawio) · Suivi : [`TICKETS.md`](TICKETS.md)
- Design du site : [`design/Escapade Directions.dc.html`](design/) (direction « Confetti »)

## Prérequis

- Python ≥ 3.11 et [uv](https://docs.astral.sh/uv/)
- Clés d'API : [OpenRouter](https://openrouter.ai) (LLM) et [Tavily](https://tavily.com) (recherche web).
  [SerpApi](https://serpapi.com) (prix des vols Google Flights) est facultatif : sans clé, les vols sont factices.

## Installation

```bash
uv sync --extra dev
cp .env.example .env   # puis renseigner les clés
```

| Variable | Rôle |
|---|---|
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | LLM (modèle `:free` conseillé) |
| `TAVILY_API_KEY` | Recherche web (pays, hôtels, activités) |
| `SERPAPI_API_KEY` | Prix des vols ; vide = vols factices. Plan gratuit : 250 recherches / mois, ~1 par voyageur et par plan |
| `SQLITE_PATH` | Fichier SQLite (checkpoints de l'agent + voyages du site) |
| `ENABLE_FINAL_HITL` | `true` : l'organisateur relit et valide le plan avant qu'il soit définitif |
| `WEB_HOST`, `WEB_PORT`, `WEB_SECURE_COOKIES` | Serveur web (défaut `127.0.0.1:8000`) ; cookies `Secure` derrière HTTPS |

Ne jamais committer `.env` ni le fichier SQLite (déjà dans `.gitignore`).

## Lancer le site web

```bash
uv run travel-web
```

Puis ouvrir <http://127.0.0.1:8000>, créer un voyage et partager le lien `/t/{code}` avec le groupe. Pas de
compte : chaque membre est reconnu par un cookie de son navigateur.

Parcours : envies → l'organisateur lance l'IA (1 à 3 min) → une fiche par pays rêvé (complétée par l'IA jusqu'à 3) → vote secret → plan (1 à 3 min).

**Installer l'application (PWA)** : Chrome / Edge proposent l'installation dans la barre d'adresse ; sur
iPhone, « Partager » puis « Sur l'écran d'accueil ». Hors `localhost`, l'installation et le mode hors ligne
exigent HTTPS.

## Ligne de commande

```bash
uv run python -m travel_agent.main
```

Saisie des envies de chaque voyageur, choix du pays et validation du plan dans le terminal.

## Tests

```bash
uv run pytest
```

Les tests n'appellent aucun service externe (LLM, Tavily et SerpApi sont simulés), y compris quand
`.env` contient de vraies clés. Ce que seul un navigateur montre est décrit dans
[`docs/checklist-navigateur.md`](docs/checklist-navigateur.md).

## Structure

```
travel_agent/
├── graph.py, state.py, llm.py   # graphe parent, état, client OpenRouter
├── subgraphs/                   # Graph 1 (suggestion de pays), Graph 2 (planification)
├── guardrails/                  # entrée (validation, injection, PII), sortie (schéma, budget, URLs)
├── tools/                       # Tavily, vols (SerpApi / factices), résolution des aéroports
├── web/                         # API FastAPI, runner de l'agent, vues, front PWA (static/)
└── main.py                      # CLI
tests/                           # unitaires, API, parcours complet (test_e2e.py)
docs/checklist-navigateur.md
```
