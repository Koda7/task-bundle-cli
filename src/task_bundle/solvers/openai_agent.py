"""OpenAI agent solver: a host-side tool-use loop over a sealed container.

The model runs on the HOST. It never has a process inside the container and
never sees credentials or the network; instead it emits tool calls that the
orchestrator translates into ``docker exec`` / ``docker cp`` against the sealed
(``--network none``) task container. Four tools are exposed:

* read_file(path)            -> cat the file in the container
* write_file(path, contents) -> docker cp bytes into the container
* bash(command, timeout)     -> run a shell command in the container
* run_tests(test_ids)        -> run tests THROUGH the Runner, returning parsed
                                pass/fail (so the model spends context on
                                reasoning, not on parsing pytest output)

The loop is bounded (max steps, wall-clock, output truncation) and runs at
temperature 0 for as much determinism as the API allows. The full transcript is
returned for artifacts and optional --analyze post-mortems.

Hidden tests are absent while this runs, so ``run_tests`` can only ever exercise
the repo's own visible tests -- never the fail2pass/pass2pass set.
"""

from __future__ import annotations

import json
import time

from task_bundle.bundle import Bundle
from task_bundle.console import info
from task_bundle.docker_env import TaskContainer
from task_bundle.runners import get_runner
from task_bundle.solvers.base import Solver, SolverResult, register_solver

_SYSTEM_PROMPT = """You are an expert software engineer resolving an issue in a \
repository. You are working inside a sandboxed checkout of the repo at its base \
commit. Use the provided tools to explore the code, make edits, and verify your \
work. When you believe the issue is fixed, call the `submit` tool.

Guidelines:
- Read relevant files before editing. Make minimal, correct changes.
- Implement the behavior described in the problem statement and requirements. If \
an interface (class/function names, signatures, file paths) is given, match it \
exactly -- tests depend on those names.
- You may run the repository's existing tests with `run_tests`, but the official \
grading tests are hidden from you. Do not try to find or modify hidden tests.
- Keep tool outputs small; prefer targeted reads over dumping whole directories.
"""

_MAX_OUTPUT_CHARS = 6000


def _tools_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file with the given contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "contents": {"type": "string"},
                    },
                    "required": ["path", "contents"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a shell command in the repo working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer", "description": "seconds (<=120)"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": (
                    "Run the given repo test ids/paths and get parsed pass/fail results. "
                    "These are the repo's own tests, not the hidden grading tests."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "test_ids": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["test_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit",
                "description": "Signal that the fix is complete and stop.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...[truncated {len(text) - limit} chars]...\n{tail}"


class OpenAIAgentSolver(Solver):
    name = "openai-agent"

    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        max_steps: int = 40,
        wallclock_seconds: int = 900,
        bash_timeout: int = 120,
    ) -> None:
        self.model = model
        self.max_steps = max_steps
        self.wallclock_seconds = wallclock_seconds
        self.bash_timeout = bash_timeout

    def solve(self, container: TaskContainer, bundle: Bundle) -> SolverResult:
        try:
            from openai import OpenAI
        except ImportError as e:
            return SolverResult(
                ok=False,
                message="openai package not installed",
                log=f"install with: pip install -e '.[agent]' ({e})",
            )

        client = OpenAI()
        spec = bundle.spec
        description = (
            bundle.read_description()
            if bundle.description_path.is_file()
            else "(no description provided)"
        )
        visible = spec.test.visible_test_paths
        user_prompt = (
            f"Repository: {spec.source.repo}\n"
            f"Working directory: {spec.environment.workdir}\n\n"
            f"# Task\n{description}\n\n"
            + (
                f"Visible test files you may run: {', '.join(visible)}\n"
                if visible
                else "There are no designated visible tests; explore as needed.\n"
            )
            + "\nResolve the issue, then call submit."
        )

        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        tools = _tools_schema()
        transcript: list[dict] = []
        started = time.time()
        steps = 0
        submitted = False

        while steps < self.max_steps:
            if time.time() - started > self.wallclock_seconds:
                transcript.append({"event": "stop", "reason": "wallclock_exceeded"})
                break
            steps += 1
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0,
                )
            except Exception as e:  # API error -> stop gracefully
                transcript.append({"event": "api_error", "error": str(e)})
                return SolverResult(
                    ok=False,
                    message=f"OpenAI API error: {e}",
                    log=json.dumps(transcript, indent=2),
                    transcript=transcript,
                    steps=steps,
                )

            choice = resp.choices[0]
            msg = choice.message
            messages.append(msg.model_dump(exclude_none=True))

            if msg.content:
                transcript.append({"event": "assistant", "text": msg.content[:1000]})
                info(f"  [agent step {steps}] {msg.content[:120].strip()}")

            if not msg.tool_calls:
                # Model produced a final answer without calling submit; treat as done.
                transcript.append({"event": "stop", "reason": "no_tool_calls"})
                break

            for tc in msg.tool_calls:
                fn = tc.function.name
                try:
                    fargs = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fargs = {}

                if fn == "submit":
                    submitted = True
                    transcript.append({"event": "tool", "name": "submit"})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "submitted",
                        }
                    )
                    continue

                result_text = self._dispatch(container, bundle, fn, fargs)
                transcript.append(
                    {
                        "event": "tool",
                        "name": fn,
                        "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in fargs.items()},
                        "result_preview": result_text[:300],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _truncate(result_text),
                    }
                )

            if submitted:
                break

        msg_summary = (
            "agent submitted a solution"
            if submitted
            else f"agent stopped after {steps} steps without submitting"
        )
        return SolverResult(
            ok=True,
            message=msg_summary,
            log=json.dumps(transcript, indent=2),
            transcript=transcript,
            steps=steps,
        )

    # --- tool dispatch ---------------------------------------------------

    def _dispatch(self, container: TaskContainer, bundle: Bundle, fn: str, args: dict) -> str:
        if fn == "read_file":
            path = args.get("path", "")
            res = container.read_file(_abspath(bundle, path))
            return res.stdout if res.ok else f"ERROR: {res.stderr or res.stdout}"

        if fn == "write_file":
            path = args.get("path", "")
            contents = args.get("contents", "")
            try:
                container.copy_in_text(contents, _abspath(bundle, path))
                return f"wrote {len(contents)} bytes to {path}"
            except Exception as e:  # noqa: BLE001
                return f"ERROR writing {path}: {e}"

        if fn == "bash":
            command = args.get("command", "")
            timeout = min(int(args.get("timeout", self.bash_timeout) or self.bash_timeout), 120)
            res = container.bash(command, timeout=timeout)
            return (
                f"$ {command}\n(exit {res.returncode}"
                + (", TIMED OUT" if res.timed_out else "")
                + f")\n{res.stdout}\n{res.stderr}"
            )

        if fn == "run_tests":
            test_ids = args.get("test_ids") or []
            if not test_ids:
                return "no test_ids provided"
            runner = get_runner(bundle.spec.test.runner.value)
            cmd = runner.build_command(bundle.spec.test.command_template, test_ids)
            res = container.bash(cmd, timeout=bundle.spec.test.timeout_seconds)
            parsed = runner.parse(
                stdout=res.stdout, stderr=res.stderr, exit_code=res.returncode, test_ids=test_ids
            )
            lines = [f"{tid}: {parsed.status_for(tid).value}" for tid in test_ids]
            return "Test results:\n" + "\n".join(lines)

        return f"unknown tool {fn!r}"


def _abspath(bundle: Bundle, path: str) -> str:
    """Resolve a possibly-relative path against the repo workdir."""
    if path.startswith("/"):
        return path
    workdir = bundle.spec.environment.workdir.rstrip("/")
    return f"{workdir}/{path}"


def _factory(**kwargs) -> OpenAIAgentSolver:
    return OpenAIAgentSolver(**kwargs)


register_solver("openai-agent", _factory)
