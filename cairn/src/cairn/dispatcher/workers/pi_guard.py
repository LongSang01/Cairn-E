"""Writes the pi extension that asks Cairn before running a shell command.

pi is the only worker CLI, and it loads TypeScript extensions from
``$PI_CODING_AGENT_DIR/extensions``. The dispatcher already creates that agent
directory (see ``PiDriver``), so the guard is just two files dropped into it:

* ``extensions/cairn-guard.ts`` -- blocks ``bash`` tool calls on the server's verdict
* ``cairn-guard.json`` -- per-task config (project id, API url, fail policy)

Doing it here, instead of baking the extension into the worker image, keeps policy
and code in one place: changing the guard never needs an image rebuild.
"""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cairn.dispatcher.config import ToolGuardConfig, WorkerConfig
    from cairn.dispatcher.runtime.backend import ExecutionBackend

AGENT_DIR = PurePosixPath("/tmp/cairn-pi")
EXTENSION_FILE = "cairn-guard.ts"
CONFIG_FILE = "cairn-guard.json"

# Plain TypeScript-compatible JavaScript: pi compiles it on load, and the syntax
# stays simple enough to keep the failure modes obvious.
EXTENSION_SOURCE = """\
import { readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const CFG = JSON.parse(readFileSync(join(HERE, "..", "cairn-guard.json"), "utf8"));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The reviewer runs on the server, which cannot see inside this container, so a
// judgement on `bash ./poc.sh` needs the script body. Snapshots are bounded: a huge
// file is truncated and flagged, which the server treats as incomplete evidence
// rather than as proof of innocence.
const MAX_SCRIPT_BYTES = 65536;
const MAX_SCRIPTS = 5;
const SCRIPT_PATTERNS = [
  /(?:^|[\\s;&|()])(?:bash|sh|source|\\.)\\s+([^\\s;&|()<>]+\\.(?:sh|bash))/g,
  /(?:^|[\\s;&|()])(?:python3?|node|perl|ruby|php)\\s+([^\\s;&|()<>]+\\.(?:py|js|mjs|pl|rb|php))/g,
  /(?:^|[\\s;&|()])(\\.?\\/[^\\s;&|()<>]+\\.(?:sh|bash|py|js|mjs|pl|rb|php))/g,
];

async function call(path, body) {
  const response = await fetch(`${CFG.api_url}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "content-type": "application/json",
      // Marks this as the worker channel. The UI token that can rewrite rules or
      // approve a pending command is deliberately absent from this container.
      "x-cairn-worker-token": CFG.worker_token || "",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`cairn guard http ${response.status}`);
  return response.json();
}

function scriptCandidates(command) {
  const found = [];
  for (const pattern of SCRIPT_PATTERNS) {
    let match;
    while ((match = pattern.exec(command)) !== null) {
      if (!found.includes(match[1])) found.push(match[1]);
    }
  }
  return found.slice(0, MAX_SCRIPTS);
}

function captureScripts(command, cwd) {
  const scripts = [];
  for (const candidate of scriptCandidates(command)) {
    const path = candidate.startsWith("/") ? candidate : join(cwd, candidate);
    try {
      if (!statSync(path).isFile()) continue;
      const raw = readFileSync(path);
      const truncated = raw.length > MAX_SCRIPT_BYTES;
      scripts.push({
        path,
        source: "file",
        content: raw.subarray(0, MAX_SCRIPT_BYTES).toString("utf8"),
        complete: !truncated,
        truncated,
      });
    } catch {
      // Referenced but unreadable: report it so the server can route to a human
      // instead of judging a command whose body nobody has seen.
      scripts.push({ path, source: "file", content: "", complete: false, truncated: false });
    }
  }
  return scripts;
}

function blocked(reason) {
  return { block: true, reason };
}

async function waitForDecision(verdict) {
  const timeoutMs = Math.max(5, Number(verdict.timeout_seconds) || 60) * 1000;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(1500);
    let row;
    try {
      row = await call(`/internal/projects/${CFG.project_id}/tool-approvals/${verdict.approval_id}`);
    } catch {
      continue;
    }
    if (row.status === "allowed") return undefined;
    if (row.status === "denied") return blocked(verdict.reason + "\\n人工审批已拒绝。");
    if (row.status === "timeout") {
      return verdict.timeout_action === "allow"
        ? undefined
        : blocked(verdict.reason + "\\n人工审批超时，按拒绝处理。");
    }
  }
  return verdict.timeout_action === "allow"
    ? undefined
    : blocked(verdict.reason + "\\n等待人工审批超时，按拒绝处理。");
}

export default function (pi) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = event.input && event.input.command;
    if (!command) return undefined;

    const cwd = typeof process !== "undefined" && process.cwd ? process.cwd() : "/";
    let verdict;
    try {
      verdict = await call(`/internal/projects/${CFG.project_id}/tool-guard/check`, {
        tool: "bash",
        command,
        worker: CFG.worker,
        session: CFG.session_id,
        arguments: event.input || { command },
        working_directory: cwd,
        script_files: captureScripts(command, cwd),
      });
    } catch (error) {
      if (CFG.fail_action === "deny") {
        return blocked(`【Cairn 平台管控·非目标防御】命令审计服务不可达（${error}）。`);
      }
      return undefined;
    }

    // A steer is delivered exactly once, at the next tool call of a worker that
    // is already running. Blocking this one call hands the instruction back to
    // the model without killing the run or dropping its in-flight work.
    const steerLines = Array.isArray(verdict.steers) ? verdict.steers : [];
    const steerText = steerLines.length
      ? "【Cairn 用户新指令·优先于当前任务】\\n" +
        steerLines.map((s) => `- ${s.text}`).join("\\n") +
        "\\n本次工具调用已暂停，请先落实以上指令，再决定是否重新发起。"
      : "";

    if (verdict.decision === "deny") {
      return blocked(steerText ? verdict.reason + "\\n" + steerText : verdict.reason);
    }
    if (steerText) return blocked(steerText);
    if (verdict.decision === "ask") return waitForDecision(verdict);
    return undefined;
  });
}
"""


def agent_dir(worker: WorkerConfig) -> PurePosixPath:
    return AGENT_DIR / worker.name


def extension_path(worker: WorkerConfig) -> PurePosixPath:
    return agent_dir(worker) / "extensions" / EXTENSION_FILE


def config_path(worker: WorkerConfig) -> PurePosixPath:
    return agent_dir(worker) / CONFIG_FILE


def config_json(
    *, project_id: str, worker: WorkerConfig, guard: ToolGuardConfig, session_id: str
) -> str:
    """One config per worker run. `session_id` scopes the steer delivery receipts."""
    return json.dumps(
        {
            "api_url": guard.api_url.rstrip("/"),
            "project_id": project_id,
            "worker": worker.name,
            "session_id": session_id,
            "fail_action": guard.fail_action,
            # From the dispatcher environment, not dispatch.yaml: that file is also
            # mounted into the nginx container, and this token must not travel there.
            "worker_token": os.environ.get("CAIRN_WORKER_TOKEN", "").strip(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def install(
    backend: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    project_id: str,
    guard: ToolGuardConfig,
    session_id: str,
) -> None:
    """Drop the extension + per-task config into the worker's pi agent directory.

    Both backends accept the same absolute path: the container one writes through
    the docker archive API, the local one writes to the host filesystem.

    Config goes first: pi aborts the whole run when an extension fails to load, so a
    half-written pair must never leave the extension without its config.
    """
    backend.write_text_file(
        container_name,
        str(config_path(worker)),
        config_json(project_id=project_id, worker=worker, guard=guard, session_id=session_id),
    )
    backend.write_text_file(container_name, str(extension_path(worker)), EXTENSION_SOURCE)
