import re


def quote_jql(value: str) -> str:

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')

    return f'"{escaped}"'


def build_jql(
    project_key: str,
    dataset_label: str,
    release: str | None = None,
    record_type: str | None = None,
    source_id: str | None = None,
    service: str | None = None,
    search_text: str | None = None
) -> str:

    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project_key):
        raise ValueError("Invalid Jira project key")

    if not dataset_label.strip():
        raise ValueError("Dataset label is required")

    conditions = [
        f"project = {quote_jql(project_key)}",
        f"labels = {quote_jql(dataset_label)}"
    ]

    if release:
        conditions.append(
            f"labels = {quote_jql(f'release-{release}')}"
        )

    if record_type:

        if record_type not in {
            "manifest",
            "requirement",
            "work",
            "evidence"
        }:
            raise ValueError("Invalid record type")

        conditions.append(
            f"labels = {quote_jql(f'rr-{record_type}')}"
        )

    if source_id:
        conditions.append(
            f"labels = {quote_jql(f'source-{source_id.lower()}')}"
        )

    if service:
        conditions.append(
            f"labels = {quote_jql(f'service-{service.lower()}')}"
        )

    if search_text:
        conditions.append(
            f"summary ~ {quote_jql(search_text)}"
        )

    return " AND ".join(conditions)
