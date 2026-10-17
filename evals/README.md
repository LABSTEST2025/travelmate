# Évaluations du LLM

Les tests (`tests/`) vérifient le code avec un faux LLM. Les évaluations vérifient **ce que répond le vrai
modèle** (`OPENROUTER_MODEL`), nœud par nœud, avec les vrais prompts. Elles ne tournent pas avec `pytest`.

1. **Par le code** (ce dossier) : vérifications déterministes (formats, nombres, contraintes du prompt).
2. **Par un LLM juge** (à venir) : ce qui ne se compte pas (pertinence, qualité, respect du style).

## Lancer

```bash
uv sync --extra dev
uv run python -m evals.run                                  # tous les nœuds, 1 passage par cas
uv run python -m evals.run --node hotel --node itinerary --repeat 3
uv run python -m evals.run --model <autre-modèle>:free     # comparer deux modèles
uv run python -m evals.run --offline                        # Tavily depuis le cache seulement
```

Le LLM n'est pas déterministe : un taux mesuré sur un seul passage reste indicatif. Pour comparer deux
modèles ou deux versions d'un prompt, prends `--repeat 3` ou plus. `--delay 2` espace les cas si le modèle
gratuit renvoie des 429.

Chaque lancement affiche un tableau par nœud et écrit un rapport JSON dans `evals/reports/` (non versionné) :
sorties, vérifications, durée, nombre de requêtes et prompts envoyés. Ce sera l'entrée du juge LLM.

## Coût et services externes

- **LLM** : environ 50 appels pour un passage complet.
- **Tavily** : réponses mises en cache dans `evals/cache/tavily/` (versionné). Deux lancements voient les
  mêmes informations, seul le LLM varie. Un nouveau cas ajoute ses requêtes au cache au premier lancement.
- **SerpApi** : jamais appelé (aucune recherche de vols, le fournisseur de vols est bloqué).

## Ce qui est mesuré

Les vérifications portent sur la **sortie brute du modèle** (premier appel), avant les corrections du code
(troncature des points, borne du niveau économique, filtrage des pays exclus, nouvelles tentatives). Une
exception compte comme un échec de « sortie exploitable ».

| Nœud | Jeu de cas | Vérifications |
|---|---|---|
| `candidates` (`research`) | complément des pays rêvés, « autres idées » | assez de pays, sans doublon, sans pays exclu, vrais pays (CLDR via Babel), zone demandée respectée |
| `overview` (`suggest`) | Portugal, Japon, Maroc | 1 à 3 pros / cons de 12 mots au plus, budget plausible, période courte, français |
| `stay_city` (`planner`) | avec et sans retour du groupe | nom de ville valide (`short_text_error`), ville retenue après nettoyage, pas le pays, ville connue, retour respecté |
| `hotel` (`hotels`) | 2 à 4 personnes, plafond serré | prix plausible du premier coup, sous le plafond, prix par nuit et par personne, notes courtes sans le plafond, français |
| `itinerary` | 4 à 8 jours, styles mêlés, retour | nombre de jours (premier coup et final), numérotation, jours non vides, pas de répétition, français |
| `daily_costs` | Lisbonne, Kyoto, Marrakech | économique ≤ standard, total standard plausible |
| `airports` | villes et pays | format IATA, 3 codes au plus, aéroport principal en tête, aéroports attendus |
| `topic` (`guard_in`) | 7 légitimes, 5 hors sujet, 6 injections | bonne classification par catégorie |

Les fourchettes de prix et les listes de référence (villes, aéroports, zones) sont dans `datasets/*.json`.
Ce sont des bornes de vraisemblance, pas des valeurs exactes. Ajoute un cas quand un problème est repéré en
test réel.

## Fichiers

```
evals/
├── run.py        # CLI, résumé et rapport
├── harness.py    # enregistrement des appels LLM, cache Tavily, construction des cas
├── checks.py     # vérifications réutilisables (pays, français, montants…)
├── nodes.py      # un évaluateur par nœud
├── datasets/     # un fichier de cas par nœud
└── cache/tavily/ # réponses Tavily enregistrées
```
