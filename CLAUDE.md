# CLAUDE.md — Travel Agent (LangGraph)

Ce fichier donne à Claude le contexte du projet. Lis-le en entier avant de coder.
L'architecture de référence est dans `diagrams.drawio` . En cas de doute, c'est ce fichier qui fait foi : garde le code et le diagramme alignés.

## Objectif

Un agent de voyage conversationnel qui :
1. collecte les préférences de l'utilisateur ;
2. **propose plusieurs pays** avec pros / cons et attend que l'utilisateur en choisisse un (HITL) ;
3. **planifie le voyage** dans le pays choisi : vols, hôtels, itinéraire jour par jour ;
4. renvoie un plan final validé.

## Stack

| Composant | Choix | Notes |
|---|---|---|
| Orchestration | **LangGraph** (Python) | Un graphe parent + deux sous-graphes |
| LLM | **OpenRouter**, modèle gratuit (`<model>:free`) | Via `ChatOpenrouter` avec `base_url="https://openrouter.ai/api/v1"`. Le nom du modèle vient de `OPENROUTER_MODEL` dans `.env` |
| Mémoire | **SQLite** via le checkpointer LangGraph (`SqliteSaver` / `AsyncSqliteSaver`) | Paquet `langgraph-checkpoint-sqlite`. Un `thread_id` par conversation |
| Recherche web | **Tavily** (`langchain-tavily`) | Infos pays, hôtels, activités |
| Vols | **SerpApi Google Flights** (bloc `price_insights`) | Estimation du prix d'un aller-retour par voyageur (médiane des vols, fourchette `price_insights` sinon quartiles). Recherche sur la paire d'aéroports principaux, repli multi-aéroports. Aéroports résolus par le LLM (`tools/airports.py`). Plan gratuit : 250 recherches/mois, les tests doivent toujours mocker SerpApi. Sans `SERPAPI_API_KEY`, un stub renvoie des données factices |
| Validation | **Pydantic** | Schémas d'entrée et de sortie |

Avant d'utiliser une API LangGraph ou LangChain, vérifie la version installée et sa documentation : ces bibliothèques évoluent vite.

## Architecture

```
Utilisateur
   │ préférences
   ▼
[input] ─► [guard_in] ─► ┌──── Graph 1 : suggestion pays ────┐
                         │ research ─► suggest ─► hitl_country │◄─┐
                         └───────────────────────────┬────────┘  │ refus / feedback
                                                     │ pays validé
                         ┌──── Graph 2 : planification ───────────┐
                         │ planner ─┬─► flights ──┐                │
                         │          ├─► hotels ───┼─► aggregate ─► hitl_final │◄─┐ ajustements
                         │          └─► itinerary ┘                │
                         └───────────────────────────┬────────────┘
                                                     ▼
                                               [guard_out] ─► plan final
                                                    └─ budget dépassé ─► planner (≤ 2 relances)
```

### Nœuds

**Graphe parent**
- `input` : normalise les préférences dans l'état.
- `guard_in` : guardrail d'entrée. Il rejette les demandes hors-sujet, détecte les tentatives de prompt injection, masque les PII et valide les champs (dates futures, budget > 0). En cas d'échec, il renvoie un message clair et **n'entre pas** dans Graph 1.
- `guard_out` : guardrail de sortie. Il valide le plan contre le schéma Pydantic, vérifie la cohérence (l'hôtel du groupe couvre du premier départ au dernier retour, total ≤ budget) et retire les URLs non issues des résultats d'outils. Si le budget est dépassé, il renvoie vers `planner` avec un feedback (2 relances au maximum, `MAX_PLAN_RETRIES`).

**Graph 1 : suggestion pays**
- `research` : appelle Tavily pour chaque pays candidat (climat, coût, visa, sécurité).
- `suggest` : le LLM produit un `CountryOverview` par pays : pros, cons, budget estimé, meilleure période.
- `hitl_country` : appelle `interrupt()` avec les overviews, puis reprend avec `Command(resume=...)`. Si l'utilisateur choisit un pays, on passe à Graph 2. S'il refuse ou fait un retour, on revient à `research` avec ce feedback.

**Graph 2 : planification**
- `planner` : découpe la tâche et lance les trois branches **en parallèle** (fan-out via `Send` ou des arêtes multiples).
- `flights` : estime le prix des vols via SerpApi (Google Flights), ou le stub sans clé. En cas d'échec, écrit un message dans `guardrail_error` et le graphe s'arrête.
- `hotels` : appelle Tavily, filtre selon le budget et les zones.
- `itinerary` : appelle Tavily pour les activités et le LLM pour le programme jour par jour.
- `aggregate` : fan-in. Assemble les trois résultats et calcule le coût total.
- `hitl_final` : **optionnel**, activé par configuration. L'utilisateur valide ou demande des ajustements, ce qui ramène à `planner`.

## État (proposition)

```python

class TripPreferences(BaseModel):
    preferred_countrie: str
    budget_eur: float
    start_date: date
    end_date: date
    travel_style: str | None = None   # ex. "aventure", "détente", "culture"
    origin_city: str | None = None


class TripResponseModelGroup(BaseModel):
    groupTravel: list[TripPreferences]
    
class CountryOverview(BaseModel):
    country: str
    pros: list[str]
    cons: list[str]
    estimated_budget_eur: float
    best_period: str
    sources: list[str]

class TravelState(TypedDict):
    messages: Annotated[list, add_messages]
    preferences: TripPreferences | None
    overviews: list[CountryOverview]
    selected_country: str | None
    feedback: str | None
    flights: list[dict]
    hotels: list[dict]
    itinerary: list[dict]
    final_plan: dict | None
    guardrail_error: str | None
```

Pour les champs écrits en parallèle dans Graph 2, chaque branche écrit dans sa propre clé. Si plusieurs branches doivent écrire dans une même clé, utilise un reducer.

## Structure de fichiers (proposition)

```
travel_agent/
├── graph.py            # graphe parent + compile(checkpointer=SqliteSaver)
├── state.py            # TravelState + modèles Pydantic
├── llm.py              # client OpenRouter
├── subgraphs/
│   ├── suggestion.py   # Graph 1
│   └── planning.py     # Graph 2
├── nodes/              # un fichier par nœud si ça grossit
├── tools/
│   ├── search.py       # Tavily
│   ├── airports.py     # ville / pays -> codes IATA (LLM)
│   └── flights.py      # interface + SerpApi + stub
├── guardrails/
│   ├── input.py
│   └── output.py
├── web/                # API du site Escapade (FastAPI) — `uv run travel-web`
│   ├── app.py          # routes /api/…
│   ├── models.py       # TripRecord (groupe, envies, votes) + corps des requêtes
│   ├── repository.py   # voyages dans le store LangGraph (SqliteStore)
│   ├── runner.py       # runs de l'agent en tâche de fond, statut du voyage
│   ├── views.py        # vue renvoyée à un membre (règles de confidentialité des budgets)
│   ├── static/         # front sans build : index.html, app.css (Confetti), app.js (routage, écrans)
│   └── server.py       # branchement SQLite + uvicorn
└── main.py             # boucle CLI : invoke → interrupt → resume
tests/
diagrams.drawio         # diagramme d'architecture
.env.example
```

## Site web (API)

- **Un voyage = un `thread_id`** du checkpointer. L'état de l'agent (pays, plan) vit dans le checkpointer ; le groupe (membres, envies, votes) vit dans un `TripRecord` du store LangGraph (`SqliteStore`), dans le même fichier SQLite. Pas de comptes : un membre = un jeton aléatoire en cookie `HttpOnly`, seul son hash est stocké.
- **Statut** déduit du runner et du checkpoint : `collecting` → `generating` → `voting` → `planning` → (`reviewing` si `ENABLE_FINAL_HITL`) → `done`, ou `error` (reprise via `POST /retry`). Les interruptions sont lues sur `snapshot.tasks` ; pendant le vote, les pays proposés sont dans la charge utile de l'interruption (pas encore dans l'état parent).
- **Budgets privés** : aucun budget individuel ne sort de l'API, sauf celui du membre qui regarde. Fourchette du groupe arrondie à 100 € à partir de 3 réponses ; en dessous, seulement « dans le budget de chacun : oui / non ». Ne jamais dériver un prix affiché d'un budget (cf. stub de vols à prix fixe).
- Un seul processus serveur : le verrou de `TripRepository` sérialise les écritures.
- **Front** (`web/static/`) : HTML/CSS/JS sans build, servi sur `/` et `/t/{code}`, rafraîchi toutes les 3 s. Tout texte venant de l'API passe par `textContent` (jamais `innerHTML`). Accents de la palette en aplats sous texte encre ; texte coloré en variantes foncées (contraste ≥ 4,5:1).

## Variables d'environnement

```
OPENROUTER_API_KEY=
OPENROUTER_MODEL=        # un modèle suffixé :free
TAVILY_API_KEY=
SERPAPI_API_KEY=         # vide = stub de vols factices
SQLITE_PATH=checkpoints.sqlite
ENABLE_FINAL_HITL=false
WEB_HOST=127.0.0.1       # optionnel, serveur web
WEB_PORT=8000            # optionnel
WEB_SECURE_COOKIES=false # true derrière HTTPS
```

Ne jamais committer `.env` ni le fichier SQLite.

## Conventions

- Python ≥ 3.11, typage partout, Pydantic v2.
- Un nœud est une fonction pure `state -> dict partiel`. Pas d'effet de bord, sauf les appels d'outils.
- Les sorties du LLM passent par `with_structured_output(...)` dès qu'un schéma existe. Les modèles gratuits sont moins fiables : prévois un retry et une validation.
- Les modèles `:free` d'OpenRouter ont des limites de débit. Gère les erreurs 429 avec un backoff et garde des prompts courts.
- Tous les appels réseau (Tavily, vols, LLM) doivent pouvoir être mockés pour les tests.
- Les textes destinés à l'utilisateur sont en français. Le code et les identifiants sont en anglais.
