"""T60 : aucun budget individuel en clair dans les traces LangSmith."""

import langsmith

from tests.conftest import make_group, make_trip
from travel_agent import tracing
from travel_agent.tracing import REDACTED, configure_tracing, redact_budgets


def test_budget_amounts_are_masked_in_prompts_and_queries():
    texts = {
        "Budget par personne, vols compris : 900 à 1500 EUR.": "Budget par personne, vols compris : [budget masqué] EUR.",
        "Hôtels à Porto pour un budget total de 1500 EUR": "Hôtels à Porto pour un budget total de [budget masqué] EUR",
        "ne doit pas dépasser 1500 EUR. Les notes": "ne doit pas dépasser [budget masqué] EUR. Les notes",
        "Le coût total (2436 EUR) dépasse le budget (2000 EUR).": "Le coût total (2436 EUR) dépasse le budget ([budget masqué] EUR).",
    }
    for text, expected in texts.items():
        assert redact_budgets(text) == expected


def test_budget_keys_are_masked_but_estimates_and_prices_are_kept():
    data = {"plan": {"budget_eur": 3000, "total_cost_eur": 2436}, "overviews": [{"estimated_budget_eur": 900}]}

    assert redact_budgets(data) == {
        "plan": {"budget_eur": REDACTED, "total_cost_eur": 2436},
        "overviews": [{"estimated_budget_eur": 900}],
    }


def test_client_masks_graph_state_with_pydantic_models():
    client = langsmith.Client(api_key="test", anonymizer=redact_budgets)

    inputs = client._hide_run_inputs({"preferences": make_group(make_trip(budget_eur=1234.5))})

    assert "1234" not in str(inputs) and REDACTED in str(inputs)


def test_tracing_is_configured_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing.langsmith, "configure", lambda **kwargs: calls.append(kwargs))

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    configure_tracing()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_REDACT_BUDGETS", "false")
    configure_tracing()
    assert calls == []

    monkeypatch.delenv("LANGSMITH_REDACT_BUDGETS")
    configure_tracing()
    (call,) = calls
    assert call["client"]._anonymizer is redact_budgets
