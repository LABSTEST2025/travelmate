import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def get_llm(**kwargs) -> ChatOpenAI:
    model = os.environ.get("OPENROUTER_MODEL")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not model or not api_key:
        raise RuntimeError(
            "OPENROUTER_MODEL et OPENROUTER_API_KEY doivent être définis (voir .env.example)."
        )
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        **kwargs,
    )


def with_structured_retry(llm: ChatOpenAI, schema, *, stop_after_attempt: int = 4):
    """Les modèles OpenRouter `:free` renvoient parfois une réponse sans `choices`
    (instabilité du provider upstream, HTTP 200 malgré tout) : on retente avec backoff."""
    return llm.with_structured_output(schema).with_retry(
        stop_after_attempt=stop_after_attempt, wait_exponential_jitter=True
    )
