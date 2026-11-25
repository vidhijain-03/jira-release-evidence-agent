import os

from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


@lru_cache(maxsize=1)
def get_llm():
    """LLM_PROVIDER=openai (default) or groq."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    retries = int(os.getenv("LLM_MAX_RETRIES", "0"))

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            api_key=os.environ["GROQ_API_KEY"],
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            max_retries=retries,
            timeout=timeout,
        )

    from langchain_openai import ChatOpenAI
    kwargs = {}
    if os.getenv("OPENAI_REASONING_EFFORT"):  
        kwargs["reasoning_effort"] = os.environ["OPENAI_REASONING_EFFORT"]
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        api_key=os.environ["OPENAI_API_KEY"],
        max_retries=retries,
        timeout=timeout,
        **kwargs,
    )
