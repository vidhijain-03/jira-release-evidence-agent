"""Streamlit frontend for the Jira release assessment FastAPI service."""
import json
import os
from collections import Counter

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="Jira Release Readiness", page_icon="📋", layout="wide")
st.title("Jira Release Readiness Assessment")
st.caption("Evidence-based release assessment powered by LangGraph, Jira MCP and Groq")

API_URL = os.getenv("ASSESSMENT_API_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = float(os.getenv("ASSESSMENT_CLIENT_TIMEOUT", "1000"))

with st.sidebar:
    st.header("Configuration")
    project = st.text_input("Jira project key", os.getenv("JIRA_PROJECT_KEY", "TYNR"))
    dataset = st.text_input("Dataset label", os.getenv("JIRA_DATASET_LABEL", "tyn-release-assessment-v2"))
    release = st.text_input("Target release", os.getenv("TARGET_RELEASE", "R2.4"))
    st.caption(f"Backend: {API_URL}")
    if st.button("Check API connection"):
        try:
            response = httpx.get(f"{API_URL}/health", timeout=5)
            response.raise_for_status()
            st.success("API reachable")
        except httpx.HTTPError:
            st.error("API unavailable. Check that Uvicorn is running.")
    st.caption("API liveness does not verify Jira or Groq connectivity.")

with st.form("assessment_form"):
    query = st.text_area("Enter your release assessment query", "Can we deploy atlas-assistant R2.4 to production?", height=100)
    submitted = st.form_submit_button("Run Assessment", type="primary")

if submitted:
    st.session_state.pop("assessment_result", None)
    st.session_state.pop("assessment_status", None)
    if not all(value.strip() for value in (query, project, dataset, release)):
        st.warning("Please provide the query, project, dataset and release.")
    else:
        payload = {"user_query": query.strip(), "project_key": project.strip(), "dataset_label": dataset.strip(), "target_release": release.strip()}
        try:
            with st.spinner("Investigating Jira evidence and evaluating requirements…"):
                response = httpx.post(f"{API_URL}/assess-release", json=payload, timeout=httpx.Timeout(TIMEOUT, connect=10.0))
                data = response.json()
            if isinstance(data, dict) and "overall_decision" in data:
                st.session_state.assessment_result = data
                st.session_state.assessment_status = response.status_code
            else:
                st.error(f"Assessment API returned HTTP {response.status_code} without a report.")
                st.json(data)
        except httpx.TimeoutException:
            st.error("The API request timed out. Check the backend logs and saved reports before retrying.")
        except httpx.HTTPError as exc:
            st.error(f"Could not communicate with the API ({type(exc).__name__}).")
        except ValueError:
            st.error("The API returned an invalid JSON response.")

report = st.session_state.get("assessment_result")
if report is None:
    st.info("Enter a query and run an assessment to see the results.")
    st.stop()

status = st.session_state.get("assessment_status", 200)
if status >= 400 or report.get("error"):
    st.warning(f"Assessment execution incomplete (HTTP {status}). The available report may be partial.")
    if report.get("error"):
        st.write(report["error"])

decision = report.get("overall_decision", "UNKNOWN")
if decision == "READY":
    st.success("Overall release decision: READY")
elif decision == "BLOCKED":
    st.error("Overall release decision: BLOCKED")
else:
    st.warning(f"Overall release decision: {decision}")

requirements = report.get("requirements") or []
counts = Counter(item.get("decision") for item in requirements)
cols = st.columns(4)
for col, label, value in zip(cols, ["Reported requirements", "Passed", "Failed", "Insufficient evidence"], [len(requirements), counts["PASS"], counts["FAIL"], counts["INSUFFICIENT_EVIDENCE"]]):
    col.metric(label, value)

with st.expander("Target configuration", expanded=True):
    st.json(report.get("target") or {})

summary, evidence, metrics, raw = st.tabs(["Requirement summary", "Evidence details", "Execution metrics", "Raw JSON"])
with summary:
    selected = st.selectbox("Filter decisions", ["All", "PASS", "FAIL", "INSUFFICIENT_EVIDENCE"])
    rows = [{"Requirement ID": d.get("requirement_id"), "Requirement": d.get("requirement_name"), "Decision": d.get("decision"), "Reason": d.get("reason")} for d in requirements if selected == "All" or d.get("decision") == selected]
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No matching requirements.")