---
name: kanban-worker
description: Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail on specific scenarios.
version: 2.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, multi-agent, collaboration, workflow, pitfalls]
    related_skills: [kanban-orchestrator]
---

# Kanban Worker — Pitfalls and Examples

> You're seeing this skill because the Hermes Kanban dispatcher spawned you as a worker with `--skills kanban-worker` — it's loaded automatically for every dispatched worker. The **lifecycle** (6 steps: orient → work → heartbeat → block/complete) also lives in the `KANBAN_GUIDANCE` block that's auto-injected into your system prompt. This skill is the deeper detail: good handoff shapes, retry diagnostics, edge cases.

## ⚠️ KRYTYCZNE: NIE usuwaj swojego CWD

**Pattern który już 6x dzisiaj zatrzymał workery** (2026-06-09):

Worker w trakcie "stale-worktree cleanup" robi `rm -rf $WORKTREE` w katalogu w którym sam siedzi (z którego ma CWD). Po tym:
- `os.getcwd()` zwraca `FileNotFoundError: [Errno 2] No such file or directory`
- **WSZYSTKIE tools (terminal, execute_code, git) failują** — bo Python próbuje resolve CWD przed każdym subprocess

**ZAWSZE PRZED `rm -rf` lub `git worktree remove`:**

```bash
# 1) Zapisz aktualny path
ORIG_CWD=$(pwd)
# 2) Chdir na BEZPIECZNE miejsce (poza tym co kasujesz)
cd /tmp || cd /
# 3) Dopiero teraz usuwaj
rm -rf "$WORKTREE"  # lub git worktree remove
# 4) Jeśli odtwarzasz worktree, chdir do nowego
cd "$NEW_WORKTREE"
```

**Lub w Python:**
```python
import os
os.chdir('/tmp')  # PRZED kasowaniem
shutil.rmtree(workdir)
os.makedirs(workdir)
os.chdir(workdir)  # po odtworzeniu
```

**Sygnał alarmowy:** jeśli zobaczysz "The cwd got removed" lub "FileNotFoundError: os.getcwd()" w log workera — to TY zrobiłeś rm -rf na CWD. Recovery: gateway musi być zrestartowany przez Tomka (nie da się fix from within worker).

---

## Workspace handling

Your workspace kind determines how you should behave inside `$HERMES_KANBAN_WORKSPACE`:

| Kind | What it is | How to work |
|---|---|---|
| `scratch` | Fresh tmp dir, yours alone | Read/write freely; it gets GC'd when the task is archived. |
| `dir:<path>` | Shared persistent directory | Other runs will read what you write. Treat it like long-lived state. Path is guaranteed absolute (the kernel rejects relative paths). |
| `worktree` | Git worktree at the resolved path | If `.git` doesn't exist, run `git worktree add <path> ${HERMES_KANBAN_BRANCH:-wt/$HERMES_KANBAN_TASK}` from the main repo first, then cd and work normally. Commit work here. |

## Tenant isolation

If `$HERMES_TENANT` is set, the task belongs to a tenant namespace. When reading or writing persistent memory, prefix memory entries with the tenant so context doesn't leak across tenants:

- Good: `business-a: Acme is our biggest customer`
- Bad (leaks): `Acme is our biggest customer`

## Good summary + metadata shapes

The `kanban_complete(summary=..., metadata=...)` handoff is how downstream workers read what you did. Patterns that work:

**Coding task:**
```python
kanban_complete(
    summary="shipped rate limiter — token bucket, keys on user_id with IP fallback, 14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    },
)
```

**Coding task → AUTO-MERGE (ŻELAZNA ZASADA od 2026-06-08):**

⚠️ **NIE używaj `kanban_block(reason="review-required: ...")` jako default end-state.** Tomek wycofał fazę review dla wszystkich projektów (asiacrm wyjątek tylko dla danych pacjentów real prod, nie test).

**Standardowy flow dla każdego coding-taska zakończonego sukcesem:**

```bash
# 1) Commit + push branch
git push origin "$BRANCH"

# 2) PR (jeśli jeszcze nie ma)
gh pr create --base main --head "$BRANCH" \
  --title "<short title>" --body "<body z metadanymi>"

# 3) AUTO-MERGE od razu (nie czekamy na CI ani na człowieka)
gh pr merge --squash --delete-branch

# 4) Auto-deploy uruchamia się sam (webhook na origin/main)
```

Następnie:

```python
kanban_complete(
    result=json.dumps({
        "pr_url": "https://github.com/tomekn333/<repo>/pull/<N>",
        "merge_commit": "<sha>",
        "branch_deleted": True,
        "changed_files": ["..."],
        "tests_run": 14,
        "tests_passed": 14,
        "deploy_triggered": True,  # webhook auto-deploy
    }),
)
```

Powiadom kanał Slack projektu jednoznacznie:
> ✅ Zrobione + zmergowane + deploy w toku. PR: <link>. Jeśli coś nie tak — mów "rollback".

**WYJĄTKI** (tylko te 4 — wtedy `kanban_block` z konkretnym reason):
1. Migracje DB destrukcyjne (DROP, DELETE bez WHERE, ALTER TABLE w prod)
2. AsiaCRM zmiany dotykające danych pacjentów REAL (nie test data) — patrz [[asiacrm-patient-data]]
3. Zmiany w integracjach wymagających rotacji secrets/credentials
4. Zmiany w produkcyjnych systemach finansowych

Reason w tych przypadkach: `needs-human-decision: <konkretna decyzja do podjęcia>`. NIE `review-required`.

**Rollback procedure** (gdy Tomek mówi "rollback"):
```bash
gh pr list --base main --merged --limit 5  # znaleźć ostatni merge
git revert -m 1 <merge-commit>
git push origin main
# Auto-deploy z rewertem
```

---

**[ARCHIWALNY przykład review handoffu, ZACHOWANY tylko dla wyjątków z listy 1-4 powyżej]:**

```python
import json

kanban_comment(
    body="needs-human-decision handoff:\n" + json.dumps({
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "diff_path": "/path/to/worktree",
        "decisions_pending": ["user_id primary vs IP fallback — wpływ na compliance"],
    }, indent=2),
)
kanban_block(
    reason="review-required: rate limiter shipped, 14/14 tests pass — needs eyes on the user_id/IP fallback choice before merging",
)
```

Use `kanban_complete` only when the task is genuinely terminal — e.g. a one-line typo fix, a docs change with no functional consequences, or a research task where the artifact IS the writeup itself.

**Research task:**
```python
kanban_complete(
    summary="3 competing libraries reviewed; vLLM wins on throughput, SGLang on latency, Tensorrt-LLM on memory efficiency",
    metadata={
        "sources_read": 12,
        "recommendation": "vLLM",
        "benchmarks": {"vllm": 1.0, "sglang": 0.87, "trtllm": 0.72},
    },
)
```

**Review task:**
```python
kanban_complete(
    summary="reviewed PR #123; 2 blocking issues found (SQL injection in /search, missing CSRF on /settings)",
    metadata={
        "pr_number": 123,
        "findings": [
            {"severity": "critical", "file": "api/search.py", "line": 42, "issue": "raw SQL concat"},
            {"severity": "high", "file": "api/settings.py", "issue": "missing CSRF middleware"},
        ],
        "approved": False,
    },
)
```

Shape `metadata` so downstream parsers (reviewers, aggregators, schedulers) can use it without re-reading your prose.

## Claiming cards you actually created

If your run produced new kanban tasks (via `kanban_create`), pass the ids in `created_cards` on `kanban_complete`. The kernel verifies each id exists and was created by your profile; any phantom id blocks the completion with an error listing what went wrong, and the rejected attempt is permanently recorded on the task's event log. **Only list ids you captured from a successful `kanban_create` return value — never invent ids from prose, never paste ids from earlier runs, never claim cards another worker created.**

```python
# GOOD — capture return values, then claim them.
c1 = kanban_create(title="remediate SQL injection", assignee="security-worker")
c2 = kanban_create(title="fix CSRF middleware", assignee="web-worker")

kanban_complete(
    summary="Review done; spawned remediations for both findings.",
    metadata={"pr_number": 123, "approved": False},
    created_cards=[c1["task_id"], c2["task_id"]],
)
```

```python
# BAD — claiming ids you don't have captured return values for.
kanban_complete(
    summary="Created remediation cards t_a1b2c3d4, t_deadbeef",  # hallucinated
    created_cards=["t_a1b2c3d4", "t_deadbeef"],                   # → gate rejects
)
```

If a `kanban_create` call fails (exception, tool_error), the card was NOT created — do not include a phantom id for it. Retry the create, or omit the id and mention the failure in your summary. The prose-scan pass also catches `t_<hex>` references in your free-form summary that don't resolve; these don't block the completion but show up as advisory warnings on the task in the dashboard.

## Block reasons that get answered fast

Bad: `"stuck"` — the human has no context.

Good: one sentence naming the specific decision you need. Leave longer context as a comment instead.

```python
kanban_comment(
    task_id=os.environ["HERMES_KANBAN_TASK"],
    body="Full context: I have user IPs from Cloudflare headers but some users are behind NATs with thousands of peers. Keying on IP alone causes false positives.",
)
kanban_block(reason="Rate limit key choice: IP (simple, NAT-unsafe) or user_id (requires auth, skips anonymous endpoints)?")
```

The block message is what appears in the dashboard / gateway notifier. The comment is the deeper context a human reads when they open the task.

## Heartbeats worth sending

Good heartbeats name progress: `"epoch 12/50, loss 0.31"`, `"scanned 1.2M/2.4M rows"`, `"uploaded 47/120 videos"`.

Bad heartbeats: `"still working"`, empty notes, sub-second intervals. Every few minutes max; skip entirely for tasks under ~2 minutes.

## Retry scenarios

If you open the task and `kanban_show` returns `runs: [...]` with one or more closed runs, you're a retry. The prior runs' `outcome` / `summary` / `error` tell you what didn't work. Don't repeat that path. Typical retry diagnostics:

- `outcome: "timed_out"` — the previous attempt hit `max_runtime_seconds`. You may need to chunk the work or shorten it.
- `outcome: "crashed"` — OOM or segfault. Reduce memory footprint.
- `outcome: "spawn_failed"` + `error: "..."` — usually a profile config issue (missing credential, bad PATH). Ask the human via `kanban_block` instead of retrying blindly.
- `outcome: "reclaimed"` + `summary: "task archived..."` — operator archived the task out from under the previous run; you probably shouldn't be running at all, check status carefully.
- `outcome: "blocked"` — a previous attempt blocked; the unblock comment should be in the thread by now.

## Notification routing

You can configure the gateway to receive cross-profile Kanban task notifications by adding `notification_sources` to `~/.hermes/config.yaml`.
- `notification_sources: ['*']` accepts subscriptions from all profiles.
- `notification_sources: ['default', 'zilor-ppt']` or `"default,zilor-ppt"` restricts subscriptions to specified profiles.
- Omitting the key keeps the default behavior (profile isolation).

## Do NOT

- Call `delegate_task` as a substitute for `kanban_create`. `delegate_task` is for short reasoning subtasks inside YOUR run; `kanban_create` is for cross-agent handoffs that outlive one API loop.
- Modify files outside `$HERMES_KANBAN_WORKSPACE` unless the task body says to.
- Create follow-up tasks assigned to yourself — assign to the right specialist.
- Complete a task you didn't actually finish. Block it instead.

## Pitfalls

**Task state can change between dispatch and your startup.** Between when the dispatcher claimed and when your process actually booted, the task may have been blocked, reassigned, or archived. Always `kanban_show` first. If it reports `blocked` or `archived`, stop — you shouldn't be running.

**Workspace may have stale artifacts.** Especially `dir:` and `worktree` workspaces can have files from previous runs. Read the comment thread — it usually explains why you're running again and what state the workspace is in.

**Don't rely on the CLI when the guidance is available.** The `kanban_*` tools work across all terminal backends (Docker, Modal, SSH). `hermes kanban <verb>` from your terminal tool will fail in containerized backends because the CLI isn't installed there. When in doubt, use the tool.

## CLI fallback (for scripting)

Every tool has a CLI equivalent for human operators and scripts:
- `kanban_show` ↔ `hermes kanban show <id> --json`
- `kanban_complete` ↔ `hermes kanban complete <id> --summary "..." --metadata '{...}'`
- `kanban_block` ↔ `hermes kanban block <id> "reason"`
- `kanban_create` ↔ `hermes kanban create "title" --assignee <profile> [--parent <id>]`
- etc.

Use the tools from inside an agent; the CLI exists for the human at the terminal.


## DŁUGIE JOBY W TLE (>30 min) — NIE blokuj "do ręcznego odblokowania"

Gdy odpalasz długi proces (scrape, build, migracja) którego nie dożyjesz w swojej sesji:

1. Uruchom job w tle (nohup/setsid, log do pliku).
2. Zarejestruj wait dla job_waitera — dopisz linię do `/home/tomek/.hermes/job_waits.txt`:
   `<board>|<task_id>|<host>|<pid1,pid2>` (host = `local` dla debiantest, albo IP np. `192.168.1.35`).
3. `kanban_block` z krótkim powodem: co działa, gdzie logi, co zostało do dokończenia po wznowieniu.
4. Cron job_waiter (*/10) sam odblokuje task gdy PID-y umrą — dispatcher respawnuje workera, który dokończy wg Twojego komentarza.

NIGDY nie pisz w powodzie blocka "odblokuj gdy skończą" / "czekam na review" — nikt tego ręcznie nie zrobi, praca stoi. Review-required jest ZAKAZANE (żelazna zasada auto-merge); jedyne 4 wyjątki: realne dane pacjentów AsiaCRM, destrukcyjne migracje DB, rotacja secrets, finanse.