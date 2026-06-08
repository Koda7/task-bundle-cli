# Task Bundle CLI

A `task` CLI + Docker harness for packaging a coding task, running a solver
(golden patch, stub, or LLM agent) against it in an **isolated container**, and
grading it against hidden `fail2pass` / `pass2pass` tests — SWE-bench style. Every
invocation is logged to a queryable SQLite database.

## Run the demo (≈60s, needs Docker + Python 3.10+)

```bash
cd task-bundle-cli && python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# A ready-to-use example bundle is already committed at bundles/openlibrary-wikidata,
# so you can skip `init` and go straight to validate/run. (To scaffold your own:
#   task init --from-swebench-pro openlibrary-4a5d2a7d24c9e4c11d3069220c0685b736d5ecde --name ol-wikidata )
task validate bundles/openlibrary-wikidata          # baseline fail2pass FAIL / pass2pass PASS; golden all PASS
task run bundles/openlibrary-wikidata --solver golden   # -> RESOLVED 🚀  (writes report.json)
task runs                                            # list runs;  `task log <run_id>` for per-test detail
```

First run pulls a 1–4 GB prebuilt image (amd64; emulated on Apple Silicon, so
slower). A pre-built example bundle is already committed at
[`bundles/openlibrary-wikidata/`](bundles/openlibrary-wikidata/) with sample
[evaluation artifacts](bundles/openlibrary-wikidata/example-report.json), so you
can inspect the output format without running anything.

Then read on for the architecture, the bundle format, and the design notes.
See [DEVLOG.md](DEVLOG.md) for the build log and the non-obvious bugs solved.

---

## What it is

The tool is built around a **task bundle**: a self-contained directory with
everything the harness needs to scaffold, validate, and grade a task. Bundles
can be generated directly from
[SWE-Bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) instances
or hand-written for any repo.

## Why this exists

A naive "ask an LLM to fix a repo, then run the tests" workflow is slow and
brittle: dependencies differ per repo, and it's easy to accidentally leak the
hidden tests to the model. This tool fixes both problems:

- **Containerization** removes the dependency-fragility — each task runs in a
  prebuilt image where the repo already builds and its tests already run.
- **A strict hidden-test boundary** guarantees the solver never sees the
  `fail2pass` / `pass2pass` tests until grading time.

---

## Prerequisites

- **Docker** running locally (Docker Desktop is fine). The first run pulls a
  prebuilt image (1–4 GB). On Apple Silicon these SWE-Bench Pro images are
  `linux/amd64`, so they run under emulation — correct, just slower.
- **Python 3.10+**.
- Optional: an `OPENAI_API_KEY` in your environment if you want to use the
  `openai-agent` solver.

## Install

```bash
cd task-bundle-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # core
pip install -e ".[agent]"   # + OpenAI agent solver (optional)
pip install -e ".[dev]"     # + pytest for the unit tests
```

This installs a `task` command on your PATH (inside the venv).

---

## Quickstart (end to end)

```bash
# 1) Scaffold a bundle from a SWE-Bench Pro instance (downloads one parquet row).
task init --from-swebench-pro openlibrary-4a5d2a7d24c9e4c11d3069220c0685b736d5ecde \
          --name openlibrary-wikidata

# 2) Validate the task itself: baseline (fix absent) must show fail2pass FAIL /
#    pass2pass PASS; golden (fix applied) must show all PASS.
task validate bundles/openlibrary-wikidata

# 3a) Run the no-op stub solver -> UNRESOLVED (the bug is still there).
task run bundles/openlibrary-wikidata --solver stub

# 3b) Run the golden (oracle) solver -> RESOLVED.
task run bundles/openlibrary-wikidata --solver golden

# 3c) Run a real LLM agent (needs OPENAI_API_KEY and the [agent] extra).
#     Git history is stripped by default for agents (anti-cheat); --analyze
#     classifies the trajectory if it fails.
task run bundles/openlibrary-wikidata --solver openai-agent --model gpt-4o --analyze

# 4) Query what happened.
task runs                 # list runs
task log <run_id>         # per-test pass/fail for a run
task report <run_id>      # the full report.json
```

A pre-generated example bundle lives in
[`bundles/openlibrary-wikidata/`](bundles/openlibrary-wikidata/), along with three
sample evaluation artifacts spanning the outcome spectrum:
[`example-report.json`](bundles/openlibrary-wikidata/example-report.json)
(RESOLVED golden run),
[`example-report-agent-resolved.json`](bundles/openlibrary-wikidata/example-report-agent-resolved.json)
(RESOLVED gpt-4o agent run), and
[`example-report-stub-unresolved.json`](bundles/openlibrary-wikidata/example-report-stub-unresolved.json)
(UNRESOLVED stub run). The bundle's `task.json` has its `image_digest` pinned.

---

## Deliverables (where to find each)

| Deliverable | Location |
| --- | --- |
| CLI code | [`src/task_bundle/`](src/task_bundle/) (entry point `task` = `cli.py`) |
| Usage instructions | This README ("Run the demo", "Commands") |
| Example task bundle that validates | [`bundles/openlibrary-wikidata/`](bundles/openlibrary-wikidata/) |
| JSON evaluation artifact (which tests passed vs. failed) | [`bundles/openlibrary-wikidata/example-report.json`](bundles/openlibrary-wikidata/example-report.json) (+ agent-resolved and stub-unresolved variants). Every `task run` also writes one to `.taskbundle/runs/<run_id>/report.json`. |
| Design notes (key tradeoffs) | This README, ["Design notes & key tradeoffs"](#design-notes--key-tradeoffs) (4 paragraphs: isolation, reproducibility, arbitrariness/debuggability, performance) |
| Build log / bugs solved | [`DEVLOG.md`](DEVLOG.md) |

The `report.json` shape is `{ resolved, summary: {fail_to_pass, pass_to_pass}, tests: {fail_to_pass: [{id, status}], pass_to_pass: [...] } }` plus full provenance (instance id, base commit, image digest, solver, model).

---

## Commands

| Command | What it does |
| --- | --- |
| `task init` | Scaffold a bundle. `--from-swebench-pro <id>` pulls from the dataset; or generic `--repo <url> --commit <sha> --image <img> --name <n>`. |
| `task validate <bundle>` | Run baseline + golden guardrails. `--offline` checks schema/files without Docker; `--repeat N` re-runs to flag flaky tests. |
| `task run <bundle> --solver {golden,stub,openai-agent}` | Run a solver, then grade. Writes `report.json`. Flags: `--model`, `--max-steps`, `--strip-git-history`/`--allow-git-history`, `--analyze`. |
| `task build <bundle>` | Pull/prepare the image, record + pin its digest (idempotent). `--clone` clones the repo at `base_commit` into the image for generic tasks. |
| `task runs [--commands]` | List recent runs (or the raw command log). |
| `task log <id>` | Show a command's or run's log; for runs, a per-test pass/fail table. |
| `task report <run_id>` | Pretty-print the `report.json` artifact. |
| `task shell <bundle>` | Open an interactive shell in the (sealed) task container for debugging. |

**Git-history stripping** defaults **ON for `openai-agent`** (so the model can't
recover the fix from the repo's git log — SWE-Bench Pro images can leak it) and
**OFF for `golden`/`stub`** (which never read git). Override either way with
`--allow-git-history` / `--strip-git-history`.

Every command accepts `--help`.

---

## The bundle format

A bundle is a directory. The example:

```
bundles/openlibrary-wikidata/
  task.json            # the single source of truth (see schema below)
  description.md       # problem statement + requirements + interface (what the solver sees)
  patch.diff           # the golden (oracle) code patch
  tests/
    hidden.patch       # fail2pass + pass2pass test code — HIDDEN from the solver
```

`task.json` is validated by a Pydantic schema (`extra="forbid"`, so typos are
caught). Key sections:

- `source` — `instance_id`, `repo`, `base_commit`, `fix_commit`, `repo_language`.
- `environment` — `image`, `platform`, `workdir`, resource caps (`memory`,
  `cpus`, `pids_limit`), and a pinned `image_digest`.
- `setup` — `reset_commands` (restore the pristine base), `hidden_test_mode`
  (`patch` or `git_checkout`), and `hidden_test_paths`.
- `test` — `runner` (`pytest` | `generic` | …), a `command_template` where
  `{tests}` is substituted, `visible_test_paths`, and `timeout_seconds`.
- `grading` — `fail_to_pass[]` and `pass_to_pass[]`.

### The hidden-test boundary (the crux)

Hidden tests live in `tests/hidden.patch` (and/or are checked out from
`fix_commit`). They are **never** present while a solver runs. The lifecycle:

```
reset repo to base  ->  run solver (no hidden tests)  ->  capture solver's diff
                    ->  STAGE hidden tests  ->  run fail2pass + pass2pass  ->  report
```

`task validate` reuses the same harness minus the solver, but note the ordering
difference: validate stages the hidden tests *before* the baseline check (they
don't exist in the base repo), whereas `run` stages them *after* the solver.

---

## What `resolved` means

A run is **RESOLVED** iff **every** `fail_to_pass` test passes **and every**
`pass_to_pass` test passes after the solver's changes. This matches SWE-Bench
Pro's criterion.

---

## The database

All state lives under `./.taskbundle/` (override with `TASKBUNDLE_HOME`):

```
.taskbundle/
  db.sqlite3
  runs/<run_id>/report.json   # the evaluation artifact
               /solver.patch  # the normalized diff the solver produced
               /logs/*.log    # solver + test output
               /meta.json     # image digest + the exact docker commands run
```

Three tables (stdlib `sqlite3`, zero setup):

- **`commands`** — one row per CLI invocation (`cid`, command, args, status,
  exit code, duration, image, digest, host). The "brief log" of what happened.
- **`runs`** — one row per `task run` / `validate` (`run_id`, solver, model,
  `resolved`, fail2pass/pass2pass tallies, artifact path). Linked to its command.
- **`test_results`** — one row per (run, phase, test): exact pass/fail per test
  id, so a collaborator can answer "which tests failed in run X?" without
  reproducing anything: `task log <run_id>`.

---

## Language-agnostic by design

Arbitrariness has two axes — *how tests run* and *what environment they run in* —
and the tool handles both:

**Runner (how tests run).** A plugin that builds the test command and parses the
output. Core ships **`pytest`** (parses the `-rA` summary; handles parametrized
ids and `pytest-rerunfailures`) and **`generic`** — no parser at all, it maps the
process **exit code** onto the requested tests, so it runs *any* framework
(`go test`, `npm test`, `cargo test`, `make test`, a shell script) out of the box.
`gotest` and `jest` parsers ship too. Adding another is a ~30-line file calling
`register_runner`.

**Environment (what it runs in).** Two paths:

- **SWE-Bench Pro** uses the dataset's prebuilt per-instance images
  (`jefzda/sweap-images:<tag>`), where the repo is already checked out and deps
  already installed — no build needed.
- **Generic repos** point `environment.image` at any image providing the
  toolchain (e.g. `python:3.12`, `golang:1.22`). If that image doesn't already
  contain the repo, `task build --clone` clones `source.repo` at `base_commit`
  into the image (using a networked build container, then commits the result so
  later runs stay sealed). This keeps the tool honest about "package a repo in a
  container at a commit" without re-implementing a full per-repo dependency
  builder — that (system packages, lockfiles, multi-stage builds) is the genuinely
  hard part that SWE-Bench Pro itself solves by shipping curated images, and the
  bundle's `environment.image` is the seam where a richer builder would plug in.

---

## Design notes & key tradeoffs

**Isolation is the core safety story, and it's a host-orchestrator / sealed-sandbox split.**
All solver code executes *inside* a Docker container started with
`--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, and hard
`--memory` / `--cpus` / `--pids-limit` caps. When the solver is an LLM agent, the
model itself runs on the **host** and reaches the container only through a thin
tool-call shim (`read_file`, `write_file`, `bash`, `run_tests`) that I translate
into `docker exec` calls. This is the same pattern production coding agents
(Claude Code, Cursor's agent mode, SWE-agent) converge on: keep the model dumb
about the environment and mediate every side effect. The payoff is that the
container needs **no** network and never holds API credentials, while the agent
still gets real agentic capability (it can read files, run tests, see failures,
and iterate). To be honest about scope: this defends against accidental host
damage and unsafe solver commands — it is *not* an adversarial-agent containment
story, because the orchestrator necessarily ferries data in both directions (a
malicious agent could encode bytes into a file it writes). That's an acceptable
boundary here: the agent isn't adversarial and nothing sensitive lives in the
container. One concrete wrinkle worth flagging: because we `--cap-drop ALL`, the
container's root loses `CAP_DAC_OVERRIDE`, so files staged via `docker cp`
(which land owned by the host uid) must be made world-readable or the in-
container user can't read them — the harness handles this when copying patches in.

**Reproducibility is pursued at three levels: image, command, and test.**
First, environments are prebuilt SWE-Bench Pro images (`jefzda/sweap-images:<tag>`)
pinned by digest in `task.json` and recorded in every run's `meta.json`;
reusing them sidesteps the multi-hour, fragile per-repo dependency builds that
make naive harnesses flaky, and `task build` is idempotent (it skips the pull if
the digest already matches). Second, I drive Docker through the `docker` CLI as
literal subprocess calls and record each one verbatim, so a run's `meta.json`
contains a copy-pasteable transcript of exactly what executed — a stronger
reproducibility property than an opaque SDK gives. Third, the harness always
resets the repo to `base_commit` and re-stages hidden tests from a fixed source
before each phase, and the captured "solver patch" is a normalized `git diff`
against base that deliberately excludes the hidden-test paths, so the recorded
artifact is purely the code change. (The bundle format also has room for a
`--repeat N` flaky-test check, mirroring the paper's practice of running each
test set three times to filter non-determinism.)

**Arbitrariness and debuggability shaped the plugin boundaries and artifacts.**
Real tasks aren't all Python pytest, so both the *runner* (how tests are executed
and parsed) and the *solver* (how a candidate fix is produced) are small plugin
registries behind one interface each; the `generic` exit-code runner means the
harness works for any language immediately, and a framework-specific parser is
purely additive. For debuggability, a failed run leaves a complete forensic
trail — the solver's normalized patch (`solver.patch`), the raw test output
(`logs/`), the docker transcript (`meta.json`), and a structured `report.json`
with per-test status — and `task log <run_id>` renders the per-test breakdown
without re-running anything. For agent runs that fail, an optional `--analyze`
pass classifies the trajectory into the failure-mode taxonomy from the SWE-Bench
Pro paper (wrong solution, tool error, context overflow, misunderstood problem,
…), turning a bare "unresolved" into an actionable post-mortem.

**Performance is treated as a task-developer feedback-loop problem, not raw
throughput.** The expensive, unavoidable costs are the one-time image pull (1-4 GB)
and amd64 emulation on Apple Silicon, so the design works to never pay them twice
and to offer fast paths that skip them entirely. Concretely: `task build` is
idempotent and skips the pull when the pinned digest already matches; the
SWE-Bench Pro parquet is fetched once via `hf_hub_download` and cached; and a
keepalive container (`sleep` entrypoint) is started once per command and reused
across every `docker exec`, rather than spawning a fresh container per test
invocation. Two deliberate fast lanes exist for the inner dev loop: `--offline`
validate checks the bundle schema and patch applicability with **no Docker at
all**, and the deterministic `golden`/`stub` solvers exercise the full
grade pipeline with **no LLM latency or cost** — so a task author iterating on a
bundle gets sub-second feedback and only pays for an image pull or an API call
when they actually need one. The tradeoff I accepted: I optimize the *develop-a-
task* loop (one task, run repeatedly) rather than *grade-many-tasks throughput*
(parallel containers across a dataset). The latter is a natural extension — the
container lifecycle is already per-run isolated, so a worker pool over a queue of
bundles would slot in — but it wasn't worth the added orchestration complexity for
the assignment's single-task-author use case.

The remaining tradeoffs are smaller but deliberate: **SQLite over a server
database** (the assignment asked for "lightweight"; a single file is zero-setup,
trivially shippable, and easy for a collaborator to copy and query — Postgres
would buy concurrency the single-author workflow doesn't need); the **`docker`
CLI over `docker-py`** (every action becomes a literal, logged, replayable shell
command, which is worth more for reproducibility than the SDK's type-safety here);
and **reusing prebuilt images over building from scratch** (building per-repo
images is the genuinely hard, multi-hour, fragile part — `environment.image` is
the seam where a richer builder plugs in, and `build --clone` already covers the
common "toolchain image + clone the repo" case).

For the build process and the non-obvious bugs solved along the way, see
[DEVLOG.md](DEVLOG.md).

---

## Project layout

```
src/task_bundle/
  cli.py            # Typer app: init / validate / run / runs / log / report
  schema.py         # Pydantic models for task.json
  bundle.py         # load + validate a bundle directory
  swebench_pro.py   # ingest a SWE-Bench Pro instance -> bundle
  scaffold.py       # write bundle dirs to disk
  docker_env.py     # sealed container lifecycle + isolation flags
  git_ops.py        # reset / apply patch / stage hidden tests / capture diff
  phases.py         # shared baseline/golden/grade primitives
  commands_impl.py  # validate + run orchestration
  query_impl.py     # rendering for runs / log / report
  db.py             # SQLite store (commands, runs, test_results)
  proc.py           # subprocess runner with verbatim logging
  runners/          # pytest + generic (+ gotest/jest)
  solvers/          # golden + stub (+ openai_agent)
```

## Running the tests

```bash
pip install -e ".[dev]"
pytest                      # unit tests (no Docker needed)
```
