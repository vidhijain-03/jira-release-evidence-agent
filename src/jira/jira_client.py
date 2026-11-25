import os

from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv
from jira import JIRA


ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def get_jira_client() -> JIRA:

    return JIRA(
        server=os.environ["JIRA_BASE_URL"],

        basic_auth=(
            os.environ["JIRA_EMAIL"],
            os.environ["JIRA_API_TOKEN"]
        ),

        options={
            "verify": os.getenv(
                "JIRA_VERIFY_SSL", "true"
            ).lower() == "true"
        },

        timeout=int(
            os.getenv("JIRA_TIMEOUT_SECONDS", "30")
        ),

        max_retries=2
    )
