from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import requests
from pydantic import TypeAdapter
from requests.adapters import HTTPAdapter

from cairn.contracts.models import (
    ContextSummary,
    Document,
    DocumentMeta,
    ProjectDetail,
    ProjectSummary,
    Question,
    RecallPlan,
    RecallResponse,
    Settings,
)

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        auth: tuple[str, str] | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # The server enforces web.auth for every UI request, and the dispatcher talks to
        # the same endpoints as a browser does. Without credentials every call would 401.
        self._auth = auth
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._questions_adapter = TypeAdapter(list[Question])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    # --- Execute: claim / keep / release a step, then submit its result -------------

    def heartbeat_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/steps/{step_id}/heartbeat", json={"worker": worker}
        )

    def release_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/steps/{step_id}/release", json={"worker": worker}
        )

    def conclude_step(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        *,
        facts: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        status: str = "done",
        note: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/steps/{step_id}/conclude",
            json={
                "worker": worker,
                "status": status,
                "facts": facts,
                "findings": findings,
                "note": note,
            },
        )

    # --- Decide: serial lease per project, then graph mutations --------------------

    def claim_decide(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/decide/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def decide_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/decide/heartbeat", json={"worker": worker}
        )

    def release_decide(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/decide/release", json={"worker": worker}
        )

    def create_step(
        self,
        project_id: str,
        *,
        from_ids: list[str],
        description: str,
        creator: str,
        title: str | None = None,
        goal: str | None = None,
        asset: str | None = None,
        exclusive: bool = False,
        batch: str | None = None,
        rationale: str | None = None,
        priority: int = 0,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/steps",
            json={
                "from": from_ids,
                "description": description,
                "title": title,
                "creator": creator,
                "goal": goal,
                "asset": asset,
                "exclusive": exclusive,
                "batch": batch,
                "rationale": rationale,
                "priority": priority,
                "worker": None,
            },
        )

    def update_step(
        self,
        project_id: str,
        step_id: str,
        *,
        priority: int | None = None,
        status: str | None = None,
        note: str | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {}
        if priority is not None:
            payload["priority"] = priority
        if status is not None:
            payload["status"] = status
        if note is not None:
            payload["note"] = note
        return self._request_json(
            "POST", f"/projects/{project_id}/steps/{step_id}/update", json=payload
        )

    def answer_steer(self, project_id: str, steer_id: str, *, text: str) -> ApiResult:
        """The planner's answer to one human instruction (idempotent on the server)."""
        return self._request_json(
            "POST",
            f"/projects/{project_id}/steers/{steer_id}/answer",
            json={"text": text},
        )

    def create_constraint(
        self,
        project_id: str,
        *,
        text: str,
        kind: str = "deny",
        origin: str = "planner",
    ) -> ApiResult:
        """Decide tightens the authorization boundary; only a human may add allow."""
        return self._request_json(
            "POST",
            f"/projects/{project_id}/constraints",
            json={"text": text, "kind": kind, "origin": origin},
        )

    def create_goal(
        self, project_id: str, *, description: str, creator: str, parent: str | None = None
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/goals",
            json={"description": description, "creator": creator, "parent": parent},
        )

    def close_goal(
        self, project_id: str, goal_id: str, *, status: str, worker: str, note: str | None = None
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/goals/{goal_id}/close",
            json={"status": status, "worker": worker, "note": note},
        )

    def complete(
        self, project_id: str, from_ids: list[str], description: str, worker: str
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    # --- Sessions: the transcript of each model conversation -----------------------

    def create_session(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/sessions", json=payload)

    def append_session_messages(
        self, project_id: str, session_id: str, messages: list[dict[str, Any]]
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/sessions/{session_id}/messages",
            json={"messages": messages},
        )

    def finish_session(
        self, project_id: str, session_id: str, payload: dict[str, Any]
    ) -> ApiResult:
        return self._request_json(
            "PATCH", f"/projects/{project_id}/sessions/{session_id}", json=payload
        )

    def session_heartbeat(self, project_id: str, session_id: str) -> ApiResult:
        """Tell the server this session is still running even if stdout is quiet."""
        return self._request_json("POST", f"/projects/{project_id}/sessions/{session_id}/heartbeat")

    # --- Side questions: read-only snapshots, never written back into the graph ----

    def list_pending_questions(self, limit: int = 5) -> list[Question]:
        response = self._session().get(
            self._url("/questions/pending"), params={"limit": limit}, timeout=self._timeout
        )
        response.raise_for_status()
        return self._questions_adapter.validate_python(response.json())

    def get_question(self, project_id: str, question_id: str) -> Question:
        response = self._session().get(
            self._url(f"/projects/{project_id}/questions/{question_id}"), timeout=self._timeout
        )
        response.raise_for_status()
        return Question.model_validate(response.json())

    def claim_question(self, project_id: str, question_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/questions/{question_id}/claim",
            json={"worker": worker},
        )

    def answer_question(
        self,
        project_id: str,
        question_id: str,
        *,
        status: str,
        answer: str | None = None,
        error: str | None = None,
        worker: str | None = None,
        duration_ms: int | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/questions/{question_id}/answer",
            json={
                "status": status,
                "answer": answer,
                "error": error,
                "worker": worker,
                "duration_ms": duration_ms,
            },
        )

    # --- Rolling context summary for the graph budget ------------------------------

    def get_context_summary(self, project_id: str) -> ContextSummary:
        response = self._session().get(
            self._url(f"/projects/{project_id}/context"), timeout=self._timeout
        )
        response.raise_for_status()
        return ContextSummary.model_validate(response.json())

    def save_context_summary(
        self,
        project_id: str,
        *,
        summary: str = "",
        facts_covered: int = 0,
        step_summary: str = "",
        steps_covered: int = 0,
        finding_summary: str = "",
        findings_covered: int = 0,
    ) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/context",
            json={
                "summary": summary,
                "facts_covered": facts_covered,
                "step_summary": step_summary,
                "steps_covered": steps_covered,
                "finding_summary": finding_summary,
                "findings_covered": findings_covered,
            },
        )

    # --- Titles, evidence documents and keyword recall ----------------------------

    def create_document(
        self,
        project_id: str,
        *,
        title: str,
        content: str,
        fact_id: str | None = None,
        step_id: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/documents",
            json={"title": title, "content": content, "fact_id": fact_id, "step_id": step_id},
        )

    def get_document(self, project_id: str, document_id: str) -> Document:
        response = self._session().get(
            self._url(f"/projects/{project_id}/documents/{document_id}"), timeout=self._timeout
        )
        response.raise_for_status()
        return Document.model_validate(response.json())

    def list_documents(self, project_id: str) -> list[DocumentMeta]:
        response = self._session().get(
            self._url(f"/projects/{project_id}/documents"), timeout=self._timeout
        )
        response.raise_for_status()
        return [DocumentMeta.model_validate(item) for item in response.json()]

    def recall(self, project_id: str, query: str, *, limit: int = 5) -> RecallResponse:
        response = self._session().get(
            self._url(f"/projects/{project_id}/recall"),
            params={"q": query, "limit": limit},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return RecallResponse.model_validate(response.json())

    def get_recall_plan(self, project_id: str) -> RecallPlan:
        response = self._session().get(
            self._url(f"/projects/{project_id}/recall-plan"), timeout=self._timeout
        )
        response.raise_for_status()
        return RecallPlan.model_validate(response.json())

    def save_recall_plan(
        self, project_id: str, *, queries: list[str], doc_ids: list[dict[str, Any]]
    ) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/recall-plan",
            json={"queries": queries, "doc_ids": doc_ids},
        )

    def _request_json(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        if self._auth is not None:
            session.auth = self._auth
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
