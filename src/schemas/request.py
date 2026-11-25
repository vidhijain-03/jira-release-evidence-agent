from enum import Enum

from pydantic import BaseModel, Field, ConfigDict


class QueryIntent(str, Enum):

    RELEASE_ASSESSMENT = "release_assessment"
    REQUIREMENT_ASSESSMENT = "requirement_assessment"
    ISSUE_LOOKUP = "issue_lookup"
    EVIDENCE_SEARCH = "evidence_search"
    RELEASE_INFORMATION = "release_information"
    OUT_OF_SCOPE = "out_of_scope"


class AssessmentRequest(BaseModel):

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True
    )

    user_query: str = Field(
        min_length=1,
        description="Natural language query from the user"
    )

    project_key: str = Field(
        min_length=1,
        description="Jira project key"
    )

    dataset_label: str = Field(
        min_length=1,
        description="Dataset scope label"
    )

    target_release: str | None = Field(
        default=None,
        description="Target release supplied by the application"
    )


class QueryUnderstanding(BaseModel):

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True
    )

    intent: QueryIntent = Field(
        description="Primary intent of the user query"
    )

    service: str | None = Field(
        default=None,
        description="Software service mentioned in the user query"
    )

    release: str | None = Field(
        default=None,
        description="Software release version explicitly mentioned"
    )

    environment: str | None = Field(
        default=None,
        description="Deployment or verification environment"
    )

    requirement: str | None = Field(
        default=None,
        description="Requirement name, ID or acceptance criterion"
    )

    issue_key: str | None = Field(
        default=None,
        description="Native Jira issue key, such as TYNR-25"
    )

    source_id: str | None = Field(
        default=None,
        description="Original source ID, such as REQ-01 or REC-9611"
    )

    commit: str | None = Field(
        default=None,
        description="Software commit identifier"
    )

    prompt_version: str | None = Field(
        default=None,
        description="Prompt or prompt bundle version"
    )
