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
| Site web | **FastAPI** + HTML/CSS/JS sans build, **PWA** | `uv run travel-web` ; manifeste et service worker servis à la racine |

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
                         ┌──── Graph 2 : planification ─────────────────────────────────────┐
                         │ planner ─┬─► flights ─────┐                                       │
                         │    ▲     ├─► hotels ──────┤                                       │
                         │    │     ├─► itinerary ───┼─► aggregate ─► plan_check ─► hitl_final │◄─┐ ajustements
                         │    │     └─► daily_costs ─┘                 │                     │
                         │    └──── budget dépassé (≤ 2 relances) ─────┘                     │
                         └───────────────────────────┬──────────────────────────────────────┘
                                                     ▼
                                               [guard_out] ─► plan final
```

### Nœuds

**Graphe parent**
- `input` : normalise les préférences dans l'état.
- `guard_in` : guardrail d'entrée. Il rejette les demandes hors-sujet, détecte les tentatives de prompt injection, masque les PII et valide les champs (dates futures, budget > 0). En cas d'échec, il renvoie un message clair et **n'entre pas** dans Graph 1.
- `guard_out` : guardrail de sortie, après la validation du groupe. Il valide le plan contre le schéma Pydantic et retire les URLs non issues des résultats d'outils. Il ne renvoie jamais vers `planner` : un plan validé au HITL final est définitif.

**Graph 1 : suggestion pays**
- `research` : choisit les pays candidats puis appelle Tavily pour chacun (climat, coût, visa, sécurité). Au 1er tour : **tous les pays rêvés** des membres (casse ignorée, les plus cités d'abord, sans plafond), complétés par le LLM jusqu'à 3 pays. Après un refus : 3 nouveaux pays du LLM selon le feedback. `wished_by` compte les membres qui ont rêvé de chaque pays (0 = suggestion de l'IA).
- `suggest` : le LLM produit un `CountryOverview` par pays : pros, cons, budget estimé, meilleure période.
- `hitl_country` : appelle `interrupt()` avec les overviews, puis reprend avec `Command(resume=...)`. Si l'utilisateur choisit un pays, on passe à Graph 2. S'il refuse ou fait un retour, on revient à `research` avec ce feedback.

**Graph 2 : planification**
- `planner` : choisit la **ville de séjour** (`stay_city`, en tenant compte du feedback), puis lance les quatre branches **en parallèle** (arêtes multiples). Vols, hôtel, itinéraire et dépenses sur place visent tous cette ville.
- `flights` : estime le prix des vols via SerpApi (Google Flights), ou le stub sans clé. En cas d'échec, écrit un message dans `guardrail_error` et le graphe s'arrête.
- `hotels` : appelle Tavily, filtre selon le budget et les zones.
- `itinerary` : appelle Tavily pour les activités et le LLM pour le programme jour par jour.
- `daily_costs` : appelle Tavily puis le LLM pour les dépenses sur place par personne et par jour (transports locaux, repas, activités), en niveau standard et économique ; applique le niveau choisi (`daily_level`) à la durée du séjour de chaque voyageur. Aucun budget dans le prompt.
- `aggregate` : fan-in. Assemble les résultats et calcule le coût total (vols + hôtel + dépenses sur place).
- `plan_check` : contrôle le brouillon **avant** qu'il soit montré : cohérence des dates (l'hôtel du groupe couvre du premier départ au dernier retour, sinon erreur) et total ≤ budget du groupe. Si le budget est dépassé, il renvoie vers `planner` avec une consigne de budget (`budget_note`, distincte du `feedback` du groupe, qui reste dans les prompts ; 2 relances au maximum, `MAX_PLAN_RETRIES`). Relances épuisées : le brouillon est montré quand même au HITL final (écart affiché), ou c'est une erreur si le HITL final est désactivé.
- `hitl_final` : **optionnel**, activé par configuration. L'utilisateur valide (définitif, seul `guard_out` tourne ensuite), demande des ajustements (texte libre) ou choisit des **pistes d'économie chiffrées** (`savings` : dépenses sur place en économique → `daily_level`, hôtel −30 % → `hotel_max_eur`, l'ajustement précédent reste en vigueur). Les deux ramènent à `planner` avec de nouvelles relances budget (`budget_note` effacée, `plan_retries` à 0).

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
├── tracing.py          # client LangSmith qui masque les budgets dans les traces
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
│   ├── push.py         # notifications Web Push (VAPID, envoi, services de push autorisés)
│   ├── static/         # front sans build : index.html, app.css (Confetti), app.js (routage, écrans)
│   └── server.py       # branchement SQLite + uvicorn
└── main.py             # boucle CLI : invoke → interrupt → resume
tests/                  # dont test_api.py (API web) et test_e2e.py (parcours complet)
docs/checklist-navigateur.md
diagrams.drawio         # diagramme d'architecture
README.md               # installation, lancement et déploiement Railway
Dockerfile              # image du site (uv), une seule instance, SQLite sur /data
railway.json            # build Docker, healthcheck /api/health, 1 réplique
.env.example
```

## Site web (API)

- **Un voyage = un `thread_id`** du checkpointer. L'état de l'agent (pays, plan) vit dans le checkpointer ; le groupe (membres, envies, votes) vit dans un `TripRecord` du store LangGraph (`SqliteStore`), dans le même fichier SQLite. Pas de comptes : un membre = un jeton aléatoire en cookie `HttpOnly`, seul son hash est stocké.
- **Statut** déduit du runner et du checkpoint : `collecting` → `generating` → `voting` → `planning` → (`reviewing` si `ENABLE_FINAL_HITL`) → `done`, ou `error` (reprise via `POST /retry`). Les interruptions sont lues sur `snapshot.tasks` ; pendant le vote, les pays proposés sont dans la charge utile de l'interruption (pas encore dans l'état parent).
- **Budgets privés** : aucun budget individuel ne sort de l'API, sauf celui du membre qui regarde. Les pistes d'économie (`savings_options`) sont chiffrées sur les prix du plan, jamais sur les budgets. Fourchette du groupe arrondie à 100 € à partir de 3 réponses ; en dessous, seulement « dans le budget de chacun : oui / non ». Ne jamais dériver un prix affiché d'un budget (cf. stub de vols à prix fixe).
- Un seul processus serveur : le verrou de `TripRepository` sérialise les écritures.
- **Front** (`web/static/`) : HTML/CSS/JS sans build, servi sur `/` et `/t/{code}`, rafraîchi toutes les 3 s. Tout texte venant de l'API passe par `textContent` (jamais `innerHTML`). Accents de la palette en aplats sous texte encre ; texte coloré en variantes foncées (contraste ≥ 4,5:1). Animations (arrivée d'un membre, confettis, révélation du vote) coupées avec `prefers-reduced-motion`. **Carnet souvenir** (plan validé) : page `#carnet` construite à la demande et visible seulement à l'impression (`@media print`, direction Carnet : kraft, Young Serif, Caveat décoratif) ; le navigateur l'enregistre en PDF. Aucun budget individuel dedans.
- **PWA** : `/manifest.webmanifest` et `/sw.js` à la racine (un service worker ne contrôle que les pages sous son chemin). Interface en réseau d'abord avec cache de secours, polices en cache, **API jamais mise en cache**. Modifier `VERSION` dans `sw.js` purge les anciens caches.
- **Textes libres** : pays, ville et style sont validés comme des noms (`short_text_error`) ; « autres idées » et ajustements passent par `screen_free_text` (PII masquées, classifieur hors sujet / injection) avant d'atteindre l'agent, et sont délimités dans les prompts comme des préférences, jamais des instructions.
- **Notifications push** (`web/push.py`) : Web Push avec clés VAPID (`uv run travel-vapid`), abonnements par membre dans le `TripRecord`. Envoyées à la fin d'un run : idées prêtes (participants), brouillon prêt et échec (organisateur), plan prêt (tous). Contenu : titre, une phrase et le lien du voyage, jamais de budget ni de vote. Seuls les services de push des navigateurs sont acceptés comme `endpoint` (pas de requête vers une URL arbitraire) ; un 404/410 supprime l'abonnement. iPhone : iOS 16.4+ et site ajouté à l'écran d'accueil.
- **Reprise** : le cookie d'un membre (90 jours) est renouvelé à chaque visite ; un cookie perdu est signalé si le navigateur connaît déjà le voyage.

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
WEB_FORWARDED_ALLOW_IPS=127.0.0.1 # proxys de confiance (X-Forwarded-For) ; * dans l'image Docker
PORT=                    # fourni par l'hébergeur, prioritaire sur WEB_PORT
LANGSMITH_TRACING=false  # optionnel ; budgets masqués dans les traces (travel_agent/tracing.py)
LANGSMITH_REDACT_BUDGETS=true # false seulement pour déboguer en local
VAPID_PUBLIC_KEY=         # notifications push (uv run travel-vapid) ; vide = push désactivé
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=            # mailto:… du responsable du site
```

Ne jamais committer `.env` ni le fichier SQLite.

## Conventions

- Python ≥ 3.11, typage partout, Pydantic v2.
- Un nœud est une fonction pure `state -> dict partiel`. Pas d'effet de bord, sauf les appels d'outils.
- Les sorties du LLM passent par `with_structured_output(...)` dès qu'un schéma existe. Les modèles gratuits sont moins fiables : prévois un retry et une validation.
- Les modèles `:free` d'OpenRouter ont des limites de débit. Gère les erreurs 429 avec un backoff et garde des prompts courts.
- Tous les appels réseau (Tavily, vols, LLM) doivent pouvoir être mockés pour les tests.
- Les textes destinés à l'utilisateur sont en français. Le code et les identifiants sont en anglais.
