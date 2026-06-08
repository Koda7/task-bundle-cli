# Development log

A short record of the non-obvious problems hit while building this tool and how
they were resolved. Kept separate from the README so the README stays a usage
doc; this is the "how it actually went" companion.

## Approach

Built smallest-deterministic-path-first: the bundle schema, SQLite store, Docker
harness, hidden-test staging, runners, and the `golden`/`stub` solvers came
first so the whole `init -> validate -> run -> query` loop worked with zero LLM
nondeterminism. The OpenAI agent solver, failure analysis, and the extra runners
were layered on only after that core was proven end-to-end on a real SWE-Bench
Pro instance (`internetarchive/openlibrary`, the `get_statement_values` task).

## Bugs found and fixes

### 1. Wrong/truncated Docker image tag hung the daemon
The HuggingFace dataset *preview* truncates the `dockerhub_tag` column. My first
pull used a guessed/truncated tag (`...172bf8129942...`), which doesn't resolve;
`docker pull` hung on the manifest lookup with no output. **Fix:** read the exact
tag from the parquet via the HF datasets-server API. The real tag ends
`...172bf81299`. Lesson: never eyeball a value that the UI may have truncated.

### 2. Docker daemon wedged (twice)
Once on the bad-tag manifest lookup (#1), and once when the host disk filled
mid-pull. In both cases `docker version` / `docker ps` timed out. **Fix:**
detect the hang with a short `timeout`, kill stuck CLI processes, restart Docker
Desktop, and poll for the daemon to come back healthy before continuing.

### 3. Host disk full mid-pull
The SWE-Bench Pro images are 1-4 GB and run under amd64 emulation on Apple
Silicon. A full host disk (565 MiB free) meant the pull couldn't complete and
even small file writes failed. **Fix (environmental):** free space, then resume.
Worth noting because it shaped the decision to make `build` idempotent (skip the
pull when the digest already matches) and to cache aggressively.

### 4. `--cap-drop ALL` made files unreadable even by container root
After `docker cp`'ing a patch into the container, `cat`-ing it back failed with
"Permission denied" — *as root*. The cause: `docker cp` lands files owned by the
host uid with mode `0600`, and `--cap-drop ALL` strips `CAP_DAC_OVERRIDE`, which
is exactly the capability that normally lets root bypass file-permission checks.
**Fix:** `chmod 0644` the host temp file before `docker cp` so the in-container
user can read it regardless of uid mapping. (The contents are task data, not
secrets, so widening read permission is fine.) This is a genuine consequence of
taking `--cap-drop ALL` seriously rather than running a privileged container.

### 5. `stage_hidden_tests` required a patch even in `git_checkout` mode
The function demanded `hidden_patch_text` unconditionally, but `git_checkout`
mode stages tests by checking them out from the fix commit and doesn't need it.
**Fix:** made the argument optional, and guarded `patch` mode with a clear error
when the hidden patch is genuinely missing.

### 6. Typer rejected subcommands with a single command registered
With only one `@app.command()`, Typer collapses the app into a root callback, so
`task init` was parsed as "unexpected extra argument `init`". **Fix:** resolved
automatically once a second command was registered; no special handling needed.

### 7. `--strip-git-history` conflicted with `git_checkout` hidden-test staging
`--strip-git-history` (anti-cheat: stop an agent from reading the fix out of the
git log) rewrites history so the fix commit becomes unreachable. But the default
`git_checkout` staging mode *needs* that commit to pull the hidden tests, so
grading failed with `reference is not a tree`. A real design tension between two
features. **Fix:** `stage_hidden_tests` now falls back to the bundle's stored
`tests/hidden.patch` when the fix commit is unreachable. Since `init` always
writes `hidden.patch`, the fallback is always available — so the anti-cheat and
the grading guarantee coexist cleanly.

## Post-review hardening

After a self-review pass, three follow-ups:

- **Default git-history stripping ON for agents.** Previously `--strip-git-history`
  was opt-in, so a forgetful user could let an agent read the fix from the git
  log. Now it defaults ON for LLM/agent solvers (a benchmark-integrity concern)
  and OFF for `golden`/`stub` (which never read git); `--allow-git-history`
  overrides. Verified: golden logs `strip_git_history=False`, agent logs `True`.
- **Pinned the example bundle's image digest.** The README sells digest pinning
  as part of reproducibility, so `bundles/openlibrary-wikidata/task.json` now
  carries a real `image_digest` (`sha256:580f9ff4…`) via `task build`.
- **`task build --clone` for arbitrary repos.** The generic path assumed a
  prebuilt image; `--clone` now clones `source.repo` at `base_commit` into the
  image (networked build container, then `docker commit` so later runs stay
  sealed). The README is explicit that a full per-repo dependency builder is out
  of scope — that's what SWE-Bench Pro's curated images solve — and that
  `environment.image` is the seam where one would plug in.

## Things verified end-to-end

- Baseline guardrail: fail2pass FAIL + pass2pass PASS before any fix.
- Golden guardrail / solver: all tests pass after `patch.diff`.
- Stub solver: UNRESOLVED (the control case — proves the harness reports failure
  correctly).
- OpenAI agent solver: resolved the task in a sealed (`--network none`) container
  via the host-side tool-call shim, with `--strip-git-history` active.
- `--network none` actually blocks egress (verified with a urllib probe).
- Per-test results are queryable by run id from the SQLite store.

## Optimization: fast git-history stripping

The original approach (`git gc --aggressive`) had to walk every object in
`.git/objects/` to determine reachability, which can be slow for repos with
deep history — especially under Docker emulation. The shallow re-clone
(`git clone --depth=1` into a temp dir, then swap `.git` directories) skips
the object-graph walk entirely: it builds a fresh `.git` containing only
what's needed for the current commit, then `rm -rf`'s the old one.
Wall-clock improvement is repo-dependent but consistently meaningful in our
testing on the SWE-Bench Pro openlibrary instance. A subtlety: repos with
submodules (e.g. OpenLibrary's `vendor/infogami`) need `.git/modules/`
preserved during the swap, otherwise submodule worktree references break.
The old GC approach is kept as a fallback if the shallow clone fails for
any reason.
