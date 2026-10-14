# Suivi d'implémentation — Travel Agent

Statuts : `todo` · `in_progress` · `done` · `blocked` · `standby` (en attente d'une décision, ne pas commencer)

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

---

# Site web Escapade (PWA) — sprint planning

Référence visuelle : `design/Escapade Directions.dc.html`, direction **1a Confetti** avec deux éléments de **1b Atlas** (graphique des budgets sur une même échelle à l'étape 3, grand livre du budget à l'étape 5).

**Hypothèses retenues** (à confirmer ou corriger avant le sprint 1) :
- Pas de comptes : un membre = un jeton aléatoire dans un cookie. Lien d'invitation non devinable.
- Persistance : un seul fichier SQLite. L'état de l'agent vit dans le checkpointer (un voyage = un `thread_id`) ; membres, envies et votes vivent dans le store LangGraph (`SqliteStore`, même fichier) — écrire dans l'état du graphe pendant une interruption de sous-graphe ou un run en cours est fragile. Un seul processus serveur, verrou sur les écritures.
- Backend FastAPI + uvicorn dans le même process que le graphe. Front en HTML / CSS / JS sans build, servi par FastAPI, mobile d'abord puis adapté au desktop.
- Rafraîchissement par interrogation régulière (2–3 s), pas de WebSocket.
- Exécution en local (`localhost` est un contexte sécurisé, donc le service worker fonctionne). Pas d'hébergement ni de notifications push dans ces sprints.

**Tailles** : S ≈ ½ journée · M ≈ 1 journée · L ≈ 2–3 journées. Sprints d'une semaine.

## Sprint 1 — Groupe et API

**Objectif** : un voyage de groupe se crée, se rejoint et se remplit via l'API ; l'organisateur lance l'IA et obtient 3 pays pour le groupe.
**Démo** : avec des requêtes HTTP, 3 membres rejoignent, soumettent leurs envies, l'organisateur lance la génération et récupère 3 pays, sans qu'aucun budget individuel n'apparaisse dans les réponses.

| ID | Ticket | Taille | Statut | Dépend de |
|---|---|---|---|---|
| T29 | Socle web : dépendances `fastapi` et `uvicorn`, paquet `travel_agent/web/`, commande `uv run travel-web`, `/api/health` | S | done | — |
| T30 | État de groupe dans `TravelState` : membres (id, prénom, couleur, hash du jeton, organisateur, a soumis), préférences par membre, phase du voyage. Écritures via `update_state`, verrou par voyage | M | done | T29 |
| T31 | API lobby : créer un voyage, rejoindre par code, vue publique du voyage, soumettre / modifier mes envies. Jeton en cookie `HttpOnly`, l'organisateur seul peut lancer | M | done | T30 |
| T32 | Budget privé : la vue publique n'expose jamais les budgets individuels ; fourchette du groupe arrondie à 100 € à partir de 3 réponses, « dans le budget de chacun : oui / non » à 2 | S | done | T31 |
| T33 | Graph 1 pour le groupe : envies agrégées, 3 pays pour tout le groupe au lieu de 3 par voyageur, budget estimé par personne | M | done | T30 |
| T34 | Lancement de l'IA en tâche de fond par l'organisateur (avec ou sans retardataires) ; statut exposé (`collecting`, `generating`, `voting`, `planning`, `done`, `error`) ; reprise depuis le dernier checkpoint | M | done | T31, T33 |
| T35 | Tests API (`TestClient`, mocks existants) : parcours lobby, droits organisateur, non-fuite des budgets, jeton invalide | M | done | T31–T34 |

## Sprint 2 — Vote et plan

> T41 : API (`POST /retry`, reprise au dernier checkpoint ou relance complète) et bouton « Réessayer » de l'écran d'erreur. T42 : couvert par `tests/test_api.py`.

**Objectif** : tout le parcours fonctionne côté API, du vote au plan final, avec les états d'attente et d'erreur.
**Démo** : vote secret à 3, égalité tranchée, plan final avec grand livre (postes, total, budget, écart en € et en %).

| ID | Ticket | Taille | Statut | Dépend de |
|---|---|---|---|---|
| T36 | Vote secret : voter, changer d'avis, compteur sans révéler qui a voté quoi, dépouillement au dernier vote ou clôture par l'organisateur, reprise du graphe avec le pays gagnant | M | done | T34 |
| T37 | Égalité : second tour entre les ex æquo ou tirage au sort, au choix de l'organisateur | S | done | T36 |
| T38 | « Autres idées » : feedback du groupe → retour à `research` sans reproposer les mêmes pays | S | done | T34 |
| T39 | Plan final via l'API : grand livre (vols, hébergement), total, budget du groupe, écart € et %, coût par personne ; validation / ajustements de l'organisateur (`hitl_final`) | M | done | T36 |
| T40 | Avancement de la génération : vraies étapes (envies lues, pays étudiés, budgets vérifiés, rédaction) exposées dans le statut | M | done | T34 |
| T41 | Erreurs : message clair (« L'agent n'a pas pu finir, tes réponses sont gardées »), bouton « Réessayer » qui repart du dernier checkpoint | S | done | T34 |
| T42 | Tests API du vote, de l'égalité, du plan et des erreurs | M | done | T36–T41 |

## Sprint 3 — Interface Confetti

> Vérifié dans Chrome headless (mobile 360–390 px, tablette 820 px, desktop 1280 px) : parcours complet à plusieurs membres, égalité, erreur, relecture du plan, onglets au clavier ; audit sans débordement, sans champ sans libellé, cibles ≥ 44 px. Pas encore de test navigateur automatisé dans le dépôt (cf. T54).

**Objectif** : les 5 écrans utilisables sur téléphone et sur ordinateur, branchés sur l'API.
**Démo** : un voyage complet joué à 3 navigateurs, de la création au plan final.

| ID | Ticket | Taille | Statut | Dépend de |
|---|---|---|---|---|
| T43 | Socle front : jetons de design Confetti (palette, Bricolage Grotesque + DM Sans), composants (bouton, chips, avatars-pastilles, cartes, onglets), mise en page mobile d'abord avec points de rupture desktop, routage par URL `/t/{code}`, interrogation régulière de l'API | M | done | T31 |
| T44 | Écran 1 — Création et invitation : nom du voyage, lien (copier, partage natif via Web Share API), pile d'avatars en direct | S | done | T43 |
| T45 | Écran 2 — Envies : obligatoires d'abord, style en chips, badge « visible par toi seul·e », progression du groupe, formulaire incomplet (le bouton dit ce qui manque), brouillon gardé en local, attente des autres | M | done | T43, T32 |
| T46 | Écran 3 — Suggestions : cartes pays, graphique des 3 budgets sur une même échelle avec la fourchette du groupe (Atlas), pros / cons, sources, « Autres idées », écran de génération en cours | L | done | T43, T40 |
| T47 | Écran 4 — Vote : grandes cibles, résultats cachés, compteur, égalité | M | done | T43, T36, T37 |
| T48 | Écran 5 — Plan : bloc budget toujours visible, grand livre (Atlas), onglets itinéraire / vols / hôtels, budget dépassé et erreur avec « Réessayer » | M | done | T43, T39, T41 |
| T49 | Contrôle responsive et accessibilité : cibles ≥ 44 px, contrastes, navigation clavier, mise en page desktop (cartes côte à côte), `prefers-reduced-motion` | S | done | T44–T48 |

## Sprint 4 — PWA et finitions

**Objectif** : le site s'installe sur l'écran d'accueil, résiste aux coupures réseau et la documentation est à jour.
**Démo** : installation depuis Chrome (desktop et Android), Lighthouse « installable », reprise d'un voyage après fermeture.

| ID | Ticket | Taille | Statut | Dépend de |
|---|---|---|---|---|
| T50 | Manifeste web (nom, couleurs Confetti, mode `standalone`), icônes (dont *maskable*) | S | todo | T43 |
| T51 | Service worker : cache de l'interface, page hors ligne, API jamais mise en cache, mise à jour propre des versions | M | todo | T50 |
| T52 | Reprise : rouvrir le lien ramène à la bonne étape ; message clair si le jeton a été perdu (autre navigateur) | S | todo | T43 |
| T53 | Moments forts légers : pastille qui tombe à l'arrivée d'un membre, confettis au dernier envoi, révélation du vote (désactivés avec `prefers-reduced-motion`) | M | todo | T44–T47 |
| T54 | Test de bout en bout du parcours web à plusieurs membres (au niveau API, plus une checklist manuelle sur navigateur) | M | todo | T42, T49 |
| T55 | Documentation : `CLAUDE.md` (architecture web, PWA), `diagrams.drawio`, instructions de lancement | S | todo | — |

## Problèmes repérés en test réel (LLM, Tavily, SerpApi)

| ID | Ticket | Taille | Statut | Dépend de |
|---|---|---|---|---|
| T56 | Cohérence de la destination : transmettre l'aéroport / la ville d'arrivée des vols aux nœuds `hotels` et `itinerary` (le premier plan réel avait des vols vers Lisbonne mais un hôtel et un itinéraire à Porto). Les branches étant parallèles, il faut décider la ville d'arrivée dans `planner` (ou après `flights`) | M | done | — |
| T57 | `tools/search.py` chargeait le `.env` avec `override=True` : une variable d'environnement ne pouvait pas remplacer une valeur du `.env` (piège pour les tests et le déploiement) | S | done | — |

| T58 | Garde-fou sur les textes libres du site (`guard_in` ne voit que `messages`, que le site n'envoie pas) : champs courts (pays, ville, style) validés strictement (lettres, espaces, tirets, apostrophes, ≤ 60 car.) ; « Autres idées » et « ajustement » passés au classifieur LLM existant (hors sujet, injection) avant de lancer l'agent, avec masquage des PII ; textes délimités dans les prompts comme « retour du groupe, pas des instructions ». Fonction partagée dans `guardrails/input.py` | M | done | — |
| T59 | Limites d'usage : par IP (création de voyages, lancements de l'agent), par voyage (tours d'« autres idées » et d'ajustements), plafond quotidien de runs et réserve de crédits SerpApi. **Standby** : pas de limite tant que le site n'est pas déployé (à faire avec B02) | M | standby | B02 |
| T60 | Confidentialité des traces LangSmith (budgets individuels envoyés dans les traces) : masquer les budgets (`hide_inputs`, API à vérifier) ou couper le traçage en production. **Standby** : on garde le comportement actuel pour le moment | S | standby | B02 |
| T61 | Durée de conservation : suppression automatique des voyages (store + checkpoints) et purge des anciens fils de la CLI. Durée et événement déclencheur à décider (1 h évoqué ; après le plan final, la dernière activité ou la création ?). **Standby** : ne rien supprimer pour le moment | S–M | standby | — |

Déjà couverts par le backlog : « Relancer » ne fait que repartager le lien (B03) ; le budget ne chiffre que vols et hôtel (B04).

## Backlog — après le MVP

| ID | Ticket | Prérequis |
|---|---|---|
| B01 | **Notifications push** (fin de génération, rappel de vote) : clés VAPID, stockage des abonnements, gestionnaire `push` du service worker. iPhone : iOS 16.4+ et site ajouté à l'écran d'accueil | B02 |
| B02 | Hébergement en ligne en HTTPS avec un domaine (nécessaire pour le push et pour tester sur téléphone hors du réseau local) | — |
| B03 | Relance des retardataires : message pré-écrit à partager, limité à 1 toutes les 12 h | — |
| B04 | Postes « transports locaux » et « activités + repas » dans le budget | — |
| B05 | Ajustements chiffrés en cas de dépassement (« 6 nuits au lieu de 7 : −140 € ») | B04 |
| B06 | QR code d'invitation | — |
| B07 | Export PDF souvenir du voyage (direction Carnet) | — |
| B08 | App native Expo branchée sur la même API, si l'usage le justifie | B02 |
| B09 | Nationalité des voyageurs : le LLM cite parfois des règles de visa d'une autre nationalité (sources Tavily canadiennes). **Reporté** : on laisse en l'état pour le moment | — |

## Risques à surveiller

- **Quotas** : un plan pour 6 voyageurs = 6 recherches SerpApi (plan gratuit : 250 / mois), et beaucoup plus d'appels LLM gratuits (limites de débit OpenRouter).
- **Durée de génération** : 1 à 3 minutes. L'interface doit rester claire pendant l'attente (T40, T46).
- **SQLite et concurrence** : un seul processus serveur. Suffisant en local, à revoir avant un hébergement multi-instance.
- **Test sur téléphone** : hors `localhost`, le service worker exige HTTPS. Pour tester sur un vrai téléphone avant B02, il faudra un tunnel HTTPS.
