"""Lance les évaluations nœud par nœud contre le vrai LLM.

    uv run python -m evals.run                      # tous les nœuds, 1 passage
    uv run python -m evals.run --node itinerary --node hotel --repeat 3
    uv run python -m evals.run --model mistralai/mistral-7b-instruct:free --offline
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from evals.harness import Recorder, TavilyCacheMiss, instrumented, require_model
from evals.nodes import NODES

DATASETS = Path(__file__).parent / "datasets"
REPORTS = Path(__file__).parent / "reports"


def load_cases(node: str) -> list[dict]:
    return json.loads((DATASETS / f"{node}.json").read_text())


def run_case(node: str, case: dict, offline: bool) -> dict:
    recorder = Recorder()
    start = time.perf_counter()
    result: dict = {"case": case["id"]}
    try:
        with instrumented(recorder, offline=offline):
            checks, output = NODES[node].run(case, recorder)
        result["checks"] = [vars(c) for c in checks]
        result["output"] = output
    except TavilyCacheMiss:
        raise
    except Exception as exc:  # une exception est un échec du cas, pas de l'évaluation
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result["seconds"] = round(time.perf_counter() - start, 1)
    result["structured_calls"] = len(recorder.calls)
    result["requests"] = recorder.requests
    result["prompts"] = [{"schema": c.schema, "prompt": c.prompt} for c in recorder.calls]
    return result


def summarize(results: list[dict]) -> dict:
    """Taux de réussite par vérification ; un cas en erreur compte comme un échec de « sortie
    exploitable » et n'entre pas dans le taux des autres vérifications."""
    per_check: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        per_check["sortie exploitable"].append("error" not in r)
        for c in r.get("checks", []):
            per_check[c["name"]].append(c["passed"])
    rates = {name: sum(v) / len(v) for name, v in per_check.items()}
    runs = len(results)
    return {
        "runs": runs,
        "score": round(sum(rates.values()) / len(rates), 3) if rates else 0.0,
        "checks": {name: {"rate": round(rate, 3), "n": len(per_check[name])} for name, rate in rates.items()},
        "mean_seconds": round(sum(r["seconds"] for r in results) / runs, 1),
        "mean_requests": round(sum(r["requests"] for r in results) / runs, 2),
        "mean_structured_calls": round(sum(r["structured_calls"] for r in results) / runs, 2),
    }


def print_node(node: str, summary: dict, results: list[dict]) -> None:
    print(f"\n## {NODES[node].description}  —  score {summary['score']:.0%}")
    print(
        f"{summary['runs']} exécution(s) · {summary['mean_seconds']} s · "
        f"{summary['mean_requests']} requête(s) LLM en moyenne ({summary['mean_structured_calls']} appel(s) structuré(s))"
    )
    for name, stats in summary["checks"].items():
        mark = "✓" if stats["rate"] == 1 else "✗"
        print(f"  {mark} {name:<45} {stats['rate']:>5.0%}  ({stats['n']})")
    for r in results:
        if "error" in r:
            print(f"    · {r['case']} : erreur — {r['error']}")
        for c in r.get("checks", []):
            if not c["passed"]:
                print(f"    · {r['case']} — {c['name']} : {c['detail']}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Évaluations nœud par nœud contre le vrai LLM.")
    parser.add_argument("--node", action="append", choices=sorted(NODES), help="nœud à évaluer (répétable) ; défaut : tous")
    parser.add_argument("--case", action="append", help="identifiant de cas à garder (répétable)")
    parser.add_argument("--repeat", type=int, default=1, help="passages par cas (le LLM n'est pas déterministe)")
    parser.add_argument("--model", help="modèle OpenRouter, remplace OPENROUTER_MODEL")
    parser.add_argument("--offline", action="store_true", help="n'appelle pas Tavily : échec si une requête manque au cache")
    parser.add_argument("--delay", type=float, default=0.0, help="pause en secondes entre deux cas (limites de débit)")
    args = parser.parse_args(argv)

    model = require_model(args.model)
    nodes = args.node or list(NODES)
    print(f"# Évaluation par le code — {model}")

    report: dict = {"model": model, "date": datetime.now().isoformat(timespec="seconds"), "repeat": args.repeat, "nodes": {}}
    for node in nodes:
        cases = [c for c in load_cases(node) if not args.case or c["id"] in args.case]
        results = []
        for case in cases:
            for _ in range(args.repeat):
                try:
                    results.append(run_case(node, case, args.offline))
                except TavilyCacheMiss as exc:
                    print(f"\n{exc}. Relance sans --offline pour compléter le cache.", file=sys.stderr)
                    return 2
                if args.delay:
                    time.sleep(args.delay)
        if not results:
            continue
        summary = summarize(results)
        report["nodes"][node] = {"summary": summary, "results": results}
        print_node(node, summary, results)

    scores = [n["summary"]["score"] for n in report["nodes"].values()]
    if scores:
        report["score"] = round(sum(scores) / len(scores), 3)
        print(f"\n**Score global : {report['score']:.0%}** (moyenne des nœuds)")
    REPORTS.mkdir(exist_ok=True)
    slug = model.replace("/", "_").replace(":", "_")
    path = REPORTS / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(f"Rapport : {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
