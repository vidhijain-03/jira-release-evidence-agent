import asyncio
import json
import os
import time
import logging

from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder


load_dotenv()


from src.agent.llm import get_llm
from src.agent.mcp_client import jira_session
from src.agent.graph import Runner
from src.schemas.request import AssessmentRequest
from src.services.validation import overall


# --------------------------------------------------
# Logging Configuration
# --------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# FastAPI Initialization
# --------------------------------------------------

app = FastAPI(
    title="Jira Release Evidence Agent",
    description=(
        "Agentic release readiness assessment using "
        "LangGraph, Groq, Jira MCP and evidence validation."
    ),
    version="1.0.0"
)


OUTPUT_DIR = Path("outputs") / "runs"

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# --------------------------------------------------
# Report Generation
# --------------------------------------------------

def save_reports(
    output_dir: Path,
    report: dict,
    trace: list
) -> None:

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # JSON Report
    (output_dir / "report.json").write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # Investigation Trace
    (output_dir / "investigation_trace.json").write_text(
        json.dumps(
            trace,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # Markdown Report
    lines = [
        "# Release readiness report",
        "",
        f"Overall decision: **{report['overall_decision']}**",
        "",
        f"Target: `{json.dumps(report.get('target'))}`",
        ""
    ]

    if report.get("error"):

        lines += [
            "Execution issue: " + report["error"],
            ""
        ]

    for decision in report.get("requirements", []):

        lines += [
            (
                f"## {decision['requirement_id']} — "
                f"{decision['requirement_name']}"
            ),
            (
                f"**{decision['decision']}** — "
                f"{decision['reason']}"
            ),
            ""
        ]

        for label, key in [
            ("Supporting evidence", "supporting_evidence"),
            ("Rejected evidence", "rejected_evidence")
        ]:

            lines.append(f"**{label}**")

            for reference in decision.get(key, []):

                lines.append(
                    f"- [{reference['issue_key']} / "
                    f"{reference['comment_id']}]"
                    f"({reference['url']}): "
                    f"{reference['quote']} — "
                    f"{reference['explanation']}"
                )

            lines.append("")

        lines.append("**Outstanding verification**")

        for verification in decision.get(
            "outstanding_verification",
            []
        ):

            lines.append(f"- {verification}")

        lines.append("")

    (output_dir / "report.md").write_text(
        "\n".join(lines),
        encoding="utf-8"
    )

    logger.info(
        "Assessment reports saved: %s",
        output_dir
    )


# --------------------------------------------------
# Release Assessment Execution
# --------------------------------------------------

async def execute_assessment(
    request: AssessmentRequest
) -> tuple[dict, int]:

    runner = None

    start = time.monotonic()

    status_code = 200

    run_id = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    output_dir = OUTPUT_DIR / run_id

    logger.info(
        "Starting assessment | Run: %s | Project: %s | Release: %s",
        run_id,
        request.project_key,
        request.target_release
    )

    # Initialize and execute the LangGraph workflow
    async def run_workflow():

        nonlocal runner

        async with jira_session(
            request.project_key,
            request.dataset_label
        ) as session:

            # Discover MCP tools
            tools = (
                await session.list_tools()
            ).tools

            logger.info(
                "Discovered %s MCP tools",
                len(tools)
            )

            # Initialize LangGraph Runner
            runner = Runner(
                session,
                get_llm(),
                tools
            )

            graph = runner.build()

            # Execute LangGraph
            state = await graph.ainvoke(
                request.model_dump(),
                config={
                    "recursion_limit": int(
                        os.getenv(
                            "AGENT_RECURSION_LIMIT",
                            "1000"
                        )
                    ),
                    "run_name": "jira-release-assessment",
                    "tags": [
                        "fastapi",
                        "jira-mcp",
                        "release-assessment"
                    ],
                    "metadata": {
                        "assessment_run_id": run_id,
                        "project_key": request.project_key,
                        "dataset_label": request.dataset_label,
                        "target_release": request.target_release
                    }
                }
            )

            return state["report"]

    # Handle interrupted assessments
    def partial_report(error: str) -> dict:

        decisions = (
            runner.partial_decisions
            if runner
            else []
        )

        expected = (
            runner.expected
            if runner
            else set()
        )

        return {
            "overall_decision": overall(
                expected,
                decisions,
                complete=False
            ),
            "error": error,
            "requirements": decisions,
            "requested_release": request.target_release,
            "target": (
                runner.target
                if runner
                else None
            ),
            "tool_calls": (
                runner.calls
                if runner
                else 0
            ),
            "model_usage": (
                runner.tokens
                if runner
                else {}
            )
        }

    try:

        timeout = int(
            os.getenv(
                "AGENT_TIMEOUT_SECONDS",
                "900"
            )
        )

        report = await asyncio.wait_for(
            run_workflow(),
            timeout=timeout
        )

    except asyncio.TimeoutError:

        logger.error(
            "Assessment timed out | Run: %s",
            run_id
        )

        status_code = 504

        report = partial_report(
            "Assessment exceeded the execution timeout."
        )

    except Exception as exc:

        logger.error(
            "Assessment interrupted | Run: %s | Error: %s",
            run_id,
            type(exc).__name__
        )

        status_code = 500

        report = partial_report(
            f"{type(exc).__name__}: "
            "assessment execution interrupted"
        )

    # Handle an error returned by the graph itself
    if report.get("error") and status_code == 200:

        status_code = 422

    # Execution metadata
    report["run_id"] = run_id

    report["total_wall_seconds"] = round(
        time.monotonic() - start,
        3
    )

    report["model"] = os.getenv(
        "GROQ_MODEL",
        "openai/gpt-oss-120b"
    )

    report["project_key"] = request.project_key

    report["dataset_label"] = request.dataset_label

    # Collect investigation trace
    trace = runner.trace if runner else []

    # Save reports without blocking the event loop
    await asyncio.to_thread(
        save_reports,
        output_dir,
        report,
        trace
    )

    logger.info(
        "Assessment completed | Run: %s | Decision: %s",
        run_id,
        report["overall_decision"]
    )

    return report, status_code

@app.get("/")
async def root():

    return {
        "service": "Jira Release Evidence Agent",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health_check():

    return {
        "status": "healthy"
    }

@app.post("/assess-release")
async def assess_release(
    request: AssessmentRequest
):

    if not request.target_release:

        raise HTTPException(
            status_code=422,
            detail="target_release is required"
        )

    report, status_code = await execute_assessment(
        request
    )

    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(report)
    )