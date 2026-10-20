"""Judge calls: one tool-less Claude call per rendered prompt file.

A judge sees only its prompt. Each call runs `claude -p` in safe mode with
no tools, no MCP servers, no plugins, hooks or CLAUDE.md, so nothing in the
material it reads (third-party abstracts included) can make it act. Code
writes the reply to disk: the reply text goes to the batch's
<stem>_response.json, and Claude Code's full JSON result (reply, model ID
actually used, cost, duration, session ID) to <stem>_reply.json beside it.
A call that fails is retried once; its error output is kept as
<stem>_error_<attempt>.txt.
"""

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PARALLEL = 4  # concurrent judge calls
TIMEOUT_S = 20 * 60  # per call
ATTEMPTS = 2
FLAGS = [
    "--safe-mode", "--tools", "", "--strict-mcp-config",
    "--permission-mode", "dontAsk", "--no-session-persistence",
    "--output-format", "json",
]
# launchd starts jobs with a minimal PATH; claude lives in ~/.local/bin.
_PATH_EXTRA = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def parse_verdicts(text: str) -> list[dict] | None:
    """A judge's JSON array of verdict objects, or None if the text holds
    none. Tolerates what judges add around it (```json fences, a sentence
    before or after): the outermost [...] span is parsed."""
    start, end = text.find("["), text.rfind("]")
    try:
        verdicts = json.loads(text[start:end + 1]) if start != -1 < end else None
    except json.JSONDecodeError:
        return None
    if isinstance(verdicts, list) and all(isinstance(v, dict) for v in verdicts):
        return verdicts
    return None


def claude_bin() -> str:
    path = os.pathsep.join(_PATH_EXTRA + [os.environ.get("PATH", "")])
    found = shutil.which("claude", path=path)
    if not found:
        raise RuntimeError("claude CLI not found on PATH or in ~/.local/bin")
    return found


def claude_version() -> str:
    out = subprocess.run([claude_bin(), "--version"], capture_output=True, text=True, check=False)
    return out.stdout.strip()


def _paths(prompt: Path) -> tuple[Path, Path]:
    stem = prompt.name.removesuffix("_prompt.txt")
    return prompt.with_name(f"{stem}_response.json"), prompt.with_name(f"{stem}_reply.json")


def judge(prompt: Path, model: str, cwd: Path) -> dict:
    """Run one judge call on `prompt` (retried once on failure) and write
    its reply files. Returns a record for the run manifest; "ok" is False
    when both attempts failed, in which case no response file is written."""
    response, reply = _paths(prompt)
    env = {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    record = {"batch": prompt.name.removesuffix("_prompt.txt"), "model": model,
              "attempts": 0, "ok": False}
    for attempt in range(1, ATTEMPTS + 1):
        record["attempts"] = attempt
        try:
            proc = subprocess.run(
                [claude_bin(), "-p", "--model", model, *FLAGS],
                input=prompt.read_text(), capture_output=True, text=True,
                timeout=TIMEOUT_S, cwd=cwd, env=env, check=False,
            )
            out = json.loads(proc.stdout) if proc.stdout.strip() else {}
            if proc.returncode != 0 or out.get("is_error") or "result" not in out:
                raise RuntimeError(
                    f"exit {proc.returncode}: {(out.get('result') or proc.stderr)[:500]}")
        except Exception as exc:  # noqa: BLE001 — recorded, then retried once
            prompt.with_name(f"{record['batch']}_error_{attempt}.txt").write_text(
                f"{type(exc).__name__}: {exc}\n")
            record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        reply.write_text(json.dumps(out, indent=1, ensure_ascii=False))
        response.write_text(out["result"])
        record.update(
            ok=True, error=None, model_used=sorted(out.get("modelUsage") or {}),
            cost_usd=out.get("total_cost_usd"), duration_ms=out.get("duration_ms"),
            session_id=out.get("session_id"),
        )
        break
    return record


def judge_all(prompts: list[Path], model: str, cwd: Path) -> list[dict]:
    """Judge every prompt file, PARALLEL at a time. Returns one record per
    prompt, in the order given."""
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return list(pool.map(lambda p: judge(p, model, cwd), prompts))
