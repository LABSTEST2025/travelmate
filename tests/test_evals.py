"""Les vérifications des évaluations (evals/) sont du code : on teste qu'elles jugent juste, sans LLM."""

import json
from pathlib import Path

from evals.checks import is_country, looks_french, mentions_amount, mentions_any, sentence_count
from evals.harness import build_group
from evals.nodes import NODES
from evals.run import DATASETS, summarize


def test_country_names_are_recognized_in_french_and_english():
    for name in ("Viêt Nam", "Vietnam", "Etats-Unis", "Birmanie", "Corée du Sud", "Thailand", "côte d'ivoire"):
        assert is_country(name), name
    for name in ("Lisbonne", "Europe", "Asie du Sud-Est", "Union européenne"):
        assert not is_country(name), name


def test_text_checks():
    assert looks_french("Visite du château et dîner au port")
    assert not looks_french("Visit the castle and have dinner at the port")
    assert mentions_amount("Hôtel à moins de 1 200 EUR pour le groupe", 1200)
    assert not mentions_amount("Hôtel central, petit-déjeuner compris", 1200)
    assert sentence_count("Un hôtel calme. Proche du centre ! Piscine ?") == 3
    assert mentions_any("La Canée (Chania), Crète", ["Chania"])
    assert not mentions_any("Athènes", ["Chania", "Rhodes"])


def test_an_error_counts_as_a_failed_run():
    results = [
        {"case": "a", "checks": [{"name": "x", "passed": True, "detail": ""}], "seconds": 1, "requests": 1, "structured_calls": 1},
        {"case": "b", "error": "ValueError: boom", "seconds": 1, "requests": 3, "structured_calls": 1},
    ]
    summary = summarize(results)
    assert summary["checks"]["sortie exploitable"]["rate"] == 0.5
    assert summary["checks"]["x"] == {"rate": 1.0, "n": 1}
    assert summary["mean_requests"] == 2


def test_every_node_has_a_valid_dataset():
    for node in NODES:
        cases = json.loads((DATASETS / f"{node}.json").read_text())
        assert cases, node
        assert len({c["id"] for c in cases}) == len(cases), node
        for case in cases:
            if "travelers" in case:
                assert build_group(case["travelers"]).group_travel


def test_reports_are_not_committed():
    assert "evals/reports/" in (Path(__file__).parents[1] / ".gitignore").read_text()
