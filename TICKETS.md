# Suivi d'implémentation — Travel Agent

Statuts : `todo` · `in_progress` · `done` · `blocked`

Référence : `CLAUDE.md` (contexte projet) et `diagrams.drawio` (architecture).

## 0. Setup

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T00 | Scaffold du projet : arborescence `travel_agent/`, `tests/`, `.env.example`, `pyproject.toml`/`requirements.txt` | done | — |
| T01 | `state.py` : `TripPreferences`, `CountryOverview`, `TravelState` (Pydantic v2 + TypedDict) | done | T00 |
| T02 | `llm.py` : client OpenRouter (`ChatOpenrouter`, `base_url`, modèle lu depuis `OPENROUTER_MODEL`) | done | T00 |

## 1. Guardrails

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T03 | `guardrails/input.py` : `guard_in` — rejet hors-sujet, détection prompt injection, masquage PII, validation champs (dates futures, budget > 0) | done | T01 |
| T04 | `guardrails/output.py` : `guard_out` — validation schéma Pydantic, cohérence dates vol/hôtel, budget, filtrage URLs non issues des outils | done | T01 |

## 2. Outils

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T05 | `tools/search.py` : intégration Tavily (climat, coût, visa, sécurité, hôtels, activités) | done | T00 |
| T06 | `tools/flights.py` : interface + stub (données factices) en attendant le choix d'API vols | done | T00 |

## 3. Graph 1 — Suggestion pays

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T07 | Nœud `research` : appel Tavily par pays candidat | done | T05 |
| T08 | Nœud `suggest` : génération `CountryOverview` par pays via LLM structuré | done | T02, T07 |
| T09 | Nœud `hitl_country` : `interrupt()` + reprise via `Command(resume=...)`, boucle feedback → `research` | done | T08 |
| T10 | `subgraphs/suggestion.py` : assemblage Graph 1 | done | T07, T08, T09 |

## 4. Graph 2 — Planification

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T11 | Nœud `planner` : fan-out vers `flights` / `hotels` / `itinerary` (via `Send` ou arêtes multiples) | done | T01 |
| T12 | Nœud `flights` : appel `tools/flights.py` | done | T06, T11 |
| T13 | Nœud `hotels` : appel Tavily filtré par budget/zones | done | T05, T11 |
| T14 | Nœud `itinerary` : Tavily + LLM pour programme jour par jour | done | T05, T02, T11 |
| T15 | Nœud `aggregate` : fan-in, calcul coût total | done | T12, T13, T14 |
| T16 | Nœud `hitl_final` (optionnel, flag `ENABLE_FINAL_HITL`) : validation/ajustements → retour `planner` | done | T15 |
| T17 | `subgraphs/planning.py` : assemblage Graph 2 | done | T11–T16 |

## 5. Graphe parent & exécution

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T18 | `graph.py` : graphe parent (`input` → `guard_in` → Graph 1 → Graph 2 → `guard_out`), compile avec `SqliteSaver`/`AsyncSqliteSaver` | done | T03, T04, T10, T17 |
| T19 | `main.py` : boucle CLI `invoke → interrupt → resume`, gestion `thread_id` | done | T18 |

## 6. Qualité

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T20 | Mocks pour tous les appels réseau (Tavily, vols, LLM) — `tests/conftest.py`, réponses SerpApi enregistrées dans `tests/fixtures/` | done | T05, T06, T02 |
| T21 | Tests unitaires nœuds + guardrails + scénarios de bout en bout (`uv run pytest`) | done | T20 |
| T22 | Gestion erreurs 429 OpenRouter (backoff, prompts courts) | done | T02 |

## 7. Vols réels — SerpApi Google Flights

| ID | Ticket | Statut | Dépend de |
|---|---|---|---|
| T23 | `tools/airports.py` : résolution ville / pays → codes IATA par le LLM (validée, mise en cache) | done | T02 |
| T24 | `tools/flights.py` : `SerpApiFlightsProvider` (`price_insights`, repli sur le vol le moins cher, retry 429/5xx), choisi si `SERPAPI_API_KEY` est défini, sinon stub | done | T06, T23 |
| T25 | Erreur de vols → `guardrail_error` affiché à l'utilisateur ; `origin_city` obligatoire dans `guard_in` et la CLI | done | T24 |
| T26 | Vérification sur un appel réel : le prix d'un aller-retour (`type=1`) est très probablement le total aller + retour (options marquées `"type": "Round trip"`, non confirmé via `departure_token`). Constat : `price_insights` absent sur une recherche multi-aéroports | done | T24 |
| T28 | Recherche sur la paire d'aéroports principaux (seule à renvoyer `price_insights`), repli multi-aéroports sans résultat ; prix = médiane des vols, fourchette Google ou 1er–3e quartile | done | T26 |
| T27 | Mettre à jour `diagrams.drawio` : boucle `guard_out → planning`, voyage de groupe, SerpApi | done | — |
