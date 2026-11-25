from typing import TypedDict, Any, NotRequired
class AgentState(TypedDict):
    user_query:str
    project_key:str
    dataset_label:str
    target_release:str
    query:NotRequired[dict]
    target:NotRequired[dict]
    manifest:NotRequired[dict]
    requirements:NotRequired[list[dict]]
    index:NotRequired[int]
    decisions:NotRequired[list[dict]]
    messages:NotRequired[list[Any]]
    issues:NotRequired[dict]
    links:NotRequired[dict]
    comments:NotRequired[dict]
    rounds:NotRequired[int]
    pending:NotRequired[list]
    model_stopped:NotRequired[bool]
    investigation_finished:NotRequired[bool]
    continue_investigation:NotRequired[bool]
    error:NotRequired[str]
    report:NotRequired[dict]
