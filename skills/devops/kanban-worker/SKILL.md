---
name: kanban-worker
description: Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail on specific scenarios.
version: 2.0.1
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, multi-agent, collaboration, workflow, pitfalls]
    related_skills: [kanban-orchestrator]
---

# Kanban Worker — Pitfalls and Examples

## ⛔ CRITICAL: GIT COMMIT + PUSH PRZED kanban_complete ⛔

> **HARD-ENFORCED od 2026-06-02:** `kanban_complete` sprawdza twoje cwd przez gita i ODMÓWI completion, jeśli są niezacommitowane zmiany albo commity nieobecne na origin (zwraca błąd, task zostaje in-flight). Nie da się już oznaczyć done bez push. Escape hatch dla tasków bez zmian w plikach: `HERMES_SKIP_PUSH_GATE=1`.

**TO JEST OBOWIĄZKOWE. NARUSZENIE = PRACA ZGINIE.**

Jeśli pracujesz na repo z auto-deploy z `git fetch + merge origin/main` (typowo: AsiaCRM, usage-dashboard) — **edycja pliku na dysku BEZ commitu = ZMIANA ZOSTANIE NADPISANA** przy najbliższym auto-deploy (cron, webhook). Pliki są bind-mountowane do kontenera, więc działają natychmiast (Tomek widzi efekt). Ale jak tylko deploy ściągnie origin/main → twoja edycja znika.

**OBSERWOWANE 2026-05-29:** Tomek raportował "wczorajsze zmiany się cofnęły" — `app/templates/consultations/form.html` zmieniony przez worker'a na dysku, NIE w gitcie → następne `git merge --ff-only origin/main` przywróciło plik do origin/main → regressja.

**PRZED każdym `kanban_complete` na repo z deploy:**

```bash
# 1. Sprawdz że nic nie zostalo bez commitu
git status --short
# musi byc PUSTE (lub tylko untracked .back.* / *.log które należą do .gitignore)

# 2. Jeśli sa zmiany — commit + push
git add -A
git -c user.email=<your-email> -c user.name=<your-name> commit -m "<opis>"
git push origin <branch>  # lub origin main jeśli pracujesz bezpośrednio na main

# 3. Verify że origin ma twój commit
git log origin/<branch>..<branch>  # powinno byc PUSTE — wszystko pushed
```

**REGUŁY PROJEKTU (obowiązkowe):** przed rozpoczęciem pracy przeczytaj plik
`~/.hermes/kanban/boards/$HERMES_KANBAN_BOARD/WORKER-RULES.md` (jeśli istnieje)
i stosuj się do niego bezwzględnie — zawiera specyfikę projektu (stack, weryfikacja,
czego nie wolno). Zasady uniwersalne: (1) NIGDY nie pracuj bezpośrednio w kanonicznym
repo `/home/tomek/projects/<board>` — wyłącznie w swoim worktree; kanoniczne repo ma
zostać nietknięte (żadnych checkout gałęzi, edycji plików, buildów w nim). (2) NIE
uruchamiaj niczego jako root i nie zostawiaj artefaktów budowania (.build itp.) poza
worktree. (3) NIE fabrykuj danych ani wyników — brak danych/zasobów = block z powodem.

**JEŚLI pracujesz na worktree branch** — ZAWSZE otwórz PR (lub merge do main gdy ma sens) zanim oznaczysz `kanban_complete`. Worker który zostawia branch tylko lokalnie BEZ PR = utracona praca.

**JEŚLI edytowałeś plik bezpośrednio w running container** przez `docker exec` — to **anty-wzorzec**. Zmiany w kontenerze giną przy każdym `docker compose up --build` lub restart z świeżym image. Edytuj **tylko** w repo, polagaj na bind mount żeby się pojawiło.

**Sprawdź po `git push`:** czy ostatni commit jest na origin (`git ls-remote origin <branch>`). Jeśli push timeout/permission denied — NIE oznaczaj jako complete, zgłoś `kanban_block` z konkretnym powodem.


## ⛔ CRITICAL: CHECKPOINT-COMMIT W TRAKCIE PRACY — NIE DOPIERO PRZED kanban_complete ⛔

> **HARD RULE od 2026-08-03.** Budżet iteracji (`agent.max_turns`, obecnie 200) to **bezpiecznik, nie usterka**. Możesz go wyczerpać w każdej chwili i bez ostrzeżenia. Wyczerpanie budżetu ma kosztować **tylko restart**, nigdy utratę pracy.

**Powod incydentu:** t_8e92fe70 (board asiacrm, 2026-08-03) — worker zrobił ~90% zadania (fix + 3 zielone testy regresyjne + dokumentacja), po czym padł na `Iteration budget exhausted (120/120)` **przed pierwszym commitem**. Cała praca została jako niezacommitowany diff w worktree i musiała być ratowana ręcznie. Ten sam wzorzec wystąpił wcześniej na hermes-mapy, exsite, usage-dashboard i transcriber (23 taski w logach).

**OBOWIĄZEK:** commituj i pushuj na swoją gałąź **po każdym zamkniętym etapie**, nie na końcu:

1. **zaraz po utworzeniu worktree** — pusty commit-kotwica, jeśli nie masz jeszcze zmian:
   `git commit --allow-empty -m "chore: start $HERMES_KANBAN_TASK" && git push -u origin <branch>`
2. **po odtworzeniu błędu / diagnozie root cause** — commit z notatką diagnostyczną (może być sam wpis w `.ai/`),
3. **po każdym działającym fixie** — osobny commit,
4. **natychmiast gdy testy przechodzą na zielono** — commit + push, zanim ruszysz cokolwiek dalej,
5. **przed każdą długą sekwencją narzędziową** (smoke w przeglądarce, build, migracja) — push, żeby stan sprzed niej był na origin.

Commity WIP są **pożądane**. Lepszy brzydki `WIP: <co działa>` na origin niż czysta historia, która nie istnieje. Squash zrobisz przy PR.

**BUDŻETUJ ITERACJE ŚWIADOMIE.** Każde wywołanie narzędzia = 1 iteracja. Najwięksi pożeracze budżetu, w kolejności:

- **Ręczne klikanie po UI w przeglądarce** (`navigate` / `type` / `click` / `snapshot`) — w t_8e92fe70 to było **67 ze 178 wywołań (38% budżetu)**. Weryfikuj **asercjami w teście** (pytest + `TestClient`/`httpx`, playwright w trybie skryptowym), nie przeklikiwaniem. Przeglądarka wyłącznie na **finalny smoke, 2–3 kroki**, po tym jak testy są zielone i **spushowane**.
- **Powtarzane `read` tego samego pliku** — czytaj raz, trzymaj w kontekście.
- **Zgadywanie ścieżek i interpreterów** — jedno `ls`/`find` na starcie zamiast serii pudłek (`File not found`). Jeśli board ma `WORKER-RULES.md`, tam są gotowe ścieżki — przeczytaj go **pierwszym** ruchem.

**GDY WIDZISZ, ŻE BUDŻET SIĘ KOŃCZY** (≈80% zużycia lub zostało ~30 iteracji) — przerwij pracę merytoryczną i zrob **wyłącznie**: `git add -A` → commit → push → `kanban_block` z powodem zawierającym nazwę gałęzi, ostatni commit i listę „co zostało". Nie zaczynaj nowego etapu, którego nie zdążysz spushować.

**GDY PRZEJMUJESZ TASK ODBLOKOWANY PO WYCZERPANIU BUDŻETU** — `git fetch origin && git checkout <branch>` i **kontynuuj od istniejącej gałęzi**. NIE pisz od nowa, NIE zaczynaj od `git worktree add ... origin/main`. Zacznij od `git log --oneline origin/main..<branch>` i `git diff origin/main...<branch>`, żeby zobaczyć, co już jest zrobione.

**ZAKRES:** jeśli task zawiera dwa niezależne problemy albo widzisz, że nie zmieścisz się w budżecie — zrób i **spushuj** pierwszą część, a dla reszty załóż osobną kartę (`kanban_create`) i opisz to w summary. Lepiej dwie zamknięte karty niż jedna zablokowana.


## ⚠️ CRITICAL — BEFORE YOU EXIT

**Three real-world failure modes seen in production (2026-05-20, 2026-09-02):**

1. **DO NOT exit your process without calling `kanban_complete` or `kanban_block` first.** Exiting with rc=0 after doing real work, but without one of those tool calls, is classified by the dispatcher as `crashed` (protocol violation). The task's `consecutive_failures` counter ticks up. After 2 such "completions" the task is auto-blocked with no human-readable reason. **Before every potential return/exit point**, re-check: did I call `kanban_complete(summary=..., metadata=...)` or `kanban_block(reason=...)`? If not — call it now.

2. **You ARE ON debiantest (192.168.1.36). DO NOT ssh to that host.** Your workspace at `$HERMES_KANBAN_WORKSPACE` is a directory on `debiantest`. To manage local state (kill tmux sessions, read system files outside workspace, check `ao status`, etc.), use **direct shell commands**, not `ssh tomek@192.168.1.36`. SSH-to-self will fail with "Permission denied" or "Host key verification failed" because the agent process has no SSH keys for itself.


3. **PODAGENCI NIE DOTYKAJA KARTY. NIGDY.** Tylko TY — worker nadrzedny, ten spawnowany przez dyspozytora — wolasz `kanban_complete` i `kanban_block`. Podagent uruchomiony do review, analizy, audytu czy czegokolwiek innego **nie ma prawa** wywolac zadnego z tych narzedzi, nawet jesli uzna, ze zadanie jest skonczone albo beznadziejnie zablokowane. Zdarzylo sie to w produkcji **trzy razy** (2026-09-01 i dwa razy 2026-09-02): raz podagent zablokowal karte, ktora dzialala poprawnie, raz zamknal jako `done` karte, w ktorej **nie powstala ani jedna linia kodu** — karta stala zamknieta 7,5 godziny, a wlasciciel czekal na poprawke swojego uszkodzonego nagrania.

   Konkretnie:
   - Spawnujac podagenta, **napisz mu wprost w promptcie**, ze nie wolno mu wywolywac `kanban_complete`, `kanban_block` ani zadnej innej operacji na tablicy i na GitHubie. Sam zakaz "tylko do odczytu" NIE WYSTARCZYL — trzy razy zostal zignorowany. Wymien te narzedzia z nazwy.
   - Podagent **zwraca werdykt do Ciebie** (tekst: findingi, werdykt PASS/FAIL, rekomendacje). Decyzje o losie karty podejmujesz TY, na podstawie tego werdyktu.
   - Werdykt FAIL od podagenta to **lista rzeczy do naprawienia**, a nie powod do `kanban_block`. Napraw je i merguj. Blokada jest wlasciwa tylko w przypadkach wymienionych nizej (sekrety, jawne zadanie review przez wlasciciela).
   - Jesli mimo to podagent zamknie lub zablokuje karte: **zostaw komentarz opisujacy, co realnie zostalo zrobione**, podaj nazwe galezi i ostatni commit, i jawnie napisz, ze status nie odzwierciedla stanu prac. Operator na tym polega przy odtwarzaniu karty.



> You're seeing this skill because the Hermes Kanban dispatcher spawned you as a worker with `--skills kanban-worker` — it's loaded automatically for every dispatched worker. The **lifecycle** (6 steps: orient → work → heartbeat → block/complete) also lives in the `KANBAN_GUIDANCE` block that's auto-injected into your system prompt. This skill is the deeper detail: good handoff shapes, retry diagnostics, edge cases.

## Workspace handling

Your workspace kind determines how you should behave inside `$HERMES_KANBAN_WORKSPACE`:

| Kind | What it is | How to work |
|---|---|---|
| `scratch` | Fresh tmp dir, yours alone | Read/write freely; it gets GC'd when the task is archived. |
| `dir:<path>` | Shared persistent directory | Other runs will read what you write. Treat it like long-lived state. Path is guaranteed absolute (the kernel rejects relative paths). |
| `worktree` | Git worktree at the resolved path | If `.git` doesn't exist, run `git worktree add <path> <branch>` from the main repo first, then cd and work normally. Commit work here. |

## Git sync protocol (READ THIS before any git command)

**The shared project checkouts under `/home/tomek/projects/<project>` are LIVE auto-deploy working trees** — a push/webhook pulls them straight to production. NEVER `cd` into them to run `git pull`, `git reset --hard`, `git merge`, or to edit files. Doing so (a) corrupts the running deployment and (b) triggers `divergent branches / cannot fast-forward` fatals that then hang your run on a `git reset --hard` approval gate. This exact failure spawn-looped one task 6x on 2026-05-21.

Work ONLY in `$HERMES_KANBAN_WORKSPACE`. To get the latest code, base your branch on the freshly-fetched REMOTE ref, never on a possibly-diverged local `main`:

```bash
MAIN=/home/tomek/projects/<project>          # source repo — do NOT mutate it
git -C "$MAIN" fetch origin                   # updates remote refs only, safe
BR="feat/${HERMES_KANBAN_TASK}-<slug>"
git -C "$MAIN" worktree add "$HERMES_KANBAN_WORKSPACE" -b "$BR" origin/main
cd "$HERMES_KANBAN_WORKSPACE"
# ...edit, commit, git push -u origin "$BR", open PR with gh...
```

Rules:
- NEVER `git pull` on a shared checkout. Use `git fetch` + branch off `origin/main`.
- NEVER `git reset --hard` / `git checkout -f` inside `/home/tomek/projects/*` — it hits an approval gate and hangs your run until the gateway restarts (which loses your progress and re-spawns you = spawn-loop).
- If `git worktree add` complains the path already exists from a dead run: `git -C "$MAIN" worktree remove --force "$HERMES_KANBAN_WORKSPACE"`, then re-add. Don't pull into the stale tree.
- The repos are configured `pull.ff only` as a backstop, so a stray `git pull` aborts cleanly instead of leaving conflicts in the tree — but don't rely on it; follow the protocol.

## Zasada merge: AUTO-MERGE domyślnie, we wszystkich projektach

**Nie zasypuj CI buildami z gałęzi.** Build weryfikacyjny uruchamiaj dopiero wtedy, gdy uważasz pracę za skończoną — nie po każdym commicie i nie w trybie „test-first, zobaczę co powie CI". Każdy czerwony build z gałęzi to powiadomienie u właściciela repo, a on nie ma jak odróżnić Twojej pośredniej iteracji od prawdziwej awarii. Zanim odpalisz workflow: przeczytaj własny diff i sprawdź, czy testy odwołują się wyłącznie do typów i sygnatur, które faktycznie istnieją w tym samym commicie. Najczęstsza przyczyna czerwonego builda w tym repo to test wypchnięty przed implementacją albo zła sygnatura w asercji — to wychwycisz czytaniem, bez CI. Jeżeli musisz iterować przez CI, powiedz o tym wprost w `kanban_comment`, żeby dało się to odróżnić od regresji.

**Twardy warunek zamknięcia:** nie wolno wywołać `kanban_complete`, dopóki PR nie ma stanu `MERGED`. Sprawdź to jawnie (`gh pr view <n> --json state`) i dopiero wtedy kończ. Ukończone review NIE jest ukończonym zadaniem — jeśli podsumowanie Twojego przebiegu opisuje przegląd kodu, a nie scalony PR, task nie jest gotowy. Zdarzyło się już zamknięcie zadania z otwartym PR-em i czterema znaleziskami HIGH w środku.

**Wyjątek bezpieczeństwa:** jeśli zmiana dotyka sekretów (klucze API, tokeny, pęk kluczy, uprawnienia), NIE mergujesz sam — zostaw PR otwarty, opisz zakres zmiany w komentarzu i zakończ `kanban_block(reason="security-review-required: ...")`. To jedyny przypadek, w którym blokada jest właściwa mimo zasady auto-merge.

**Decyzja właściciela (2026-08-09, obowiązuje na wszystkich boardach):** kod ma trafiać od razu na produkcję. Task kodowy jest skończony dopiero wtedy, gdy PR jest `MERGED` do gałęzi domyślnej. Żadnych draftów, żadnej etykiety `hold`, żadnego `review-required` — chyba że treść zadania jawnie prosi o review człowieka przed merge. Właściciel ogląda wypuszczone buildy, nie kolejkę PR-ów.

### Gdy projekt/board ma politykę auto-merge

Jeżeli task, lokalne instrukcje repo, albo prompt projektu mówią wprost o **auto-merge / self-merge / żelaznej zasadzie auto-merge**, to ścieżka końcowa dla tasku kodowego jest:

1. `git push -u origin <branch>`
2. otwórz PR (`gh pr create`)
3. spróbuj merge: `gh pr merge --squash --delete-branch`
4. zweryfikuj, że PR jest faktycznie `MERGED` (np. `gh pr view --json state,mergeStateStatus,url,number`)
5. zakończ `kanban_complete(...)`, nie `kanban_block(review-required)`

Jeśli `gh pr merge --delete-branch` zwróci błąd tylko dlatego, że lokalna gałąź została już usunięta albo cleanup nie domknął się idealnie, ale sam PR ma stan `MERGED`, traktuj to jako **sukces merge**, opisz drobny cleanup issue w `summary`/`metadata` i nadal kończ task przez `kanban_complete`.

### Gdy projekt wymaga review człowieka

Dopiero jeśli task lub instrukcje projektu **jawnie** wymagają review człowieka przed merge, użyj ścieżki handoff:
- `git push -u origin <branch>` + otwórz PR (`gh pr create`)
- zostaw `kanban_comment(...)` ze structured handoffem
- zakończ `kanban_block(reason="review-required: ...")`

### Reguła decyzyjna

- **Explicit project/task policy beats generic skill text.**
- Jeżeli widzisz konflikt między tym skillem a promptem projektu / task body, podążaj za bardziej lokalną i bardziej konkretną instrukcją.
- Jeżeli poprzednie runy kończyły się `blocked` z `review-required`, ale PR-y i tak były już mergowane automatycznie, potraktuj to jako sygnał błędnej instrukcji, nie jako wzorzec do powielania.

Reference: `references/auto-merge-vs-review-required.md` zawiera skrócony wzorzec diagnostyczny dla tego konfliktu instrukcji.

### Pitfall z tej klasy incydentów

Powtarzalny antywzorzec to para sprzecznych instrukcji:
- skill workerowy mówi „oddaj do review-required”,
- a prompt projektu / polityka użytkownika wymaga auto-merge + `kanban_complete`.

W takiej sytuacji worker będzie konsekwentnie kończył taski jako `blocked`, mimo że kod jest już zmergowany. Gdy diagnozujesz takie przypadki, sprawdź **oba miejsca naraz**:
- `SKILL.md` workera
- systemowy `KANBAN_GUIDANCE` / prompt projektu

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

**Coding task that needs human review (review-required) — RARE, opt-in only:**

DEFAULT IS AUTO-MERGE. Use this path ONLY when the task body explicitly asks for human review before merge. Otherwise merge the PR yourself and end with `kanban_complete`. When the task does ask for it: block instead of complete, with `reason` prefixed `review-required: ` so the dashboard surfaces the row as needing review. Drop the structured metadata (changed files, test counts, diff/PR url) into a comment first, since `kanban_block` only carries the human-readable reason — comments are the durable annotation channel. Reviewer either approves and runs `hermes kanban unblock <id>` (which re-spawns you with the comment thread for any follow-ups) or asks for changes via another comment.

```python
import json

kanban_comment(
    body="review-required handoff:\n" + json.dumps({
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "diff_path": "/path/to/worktree",  # or PR url if pushed
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    }, indent=2),
)
kanban_block(
    reason="review-required: rate limiter shipped, 14/14 tests pass — needs eyes on the user_id/IP fallback choice before merging",
)
```

Use `kanban_complete` for everything else — including normal code changes, which you merge yourself once the required checks are green. The owner reviews shipped builds, not queued PRs.

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
- `outcome: "crashed"` — OOM, segfault, or a process that dies before it can report a structured outcome. Read the run log before retrying; if it shows provider/auth/profile initialization failure, fix or change the profile first instead of dispatching into a crash loop.
- `outcome: "spawn_failed"` + `error: "..."` — usually a profile config issue (missing credential, bad PATH). Ask the human via `kanban_block` instead of retrying blindly.
- `outcome: "reclaimed"` + `summary: "task archived..."` — operator archived the task out from under the previous run; you probably shouldn't be running at all, check status carefully.
- `outcome: "blocked"` — a previous attempt blocked; the unblock comment should be in the thread by now.

Operator-side crash-loop triage lives in `references/operator-spawn-crash-loop-triage.md`: inspect `task_runs`/logs, repair or switch the worker profile after a timestamped backup, then dispatch once and observe. Capture the remediation path, not a permanent negative claim about the provider.

## Do NOT

- Call `delegate_task` as a substitute for `kanban_create`. `delegate_task` is for short reasoning subtasks inside YOUR run; `kanban_create` is for cross-agent handoffs that outlive one API loop.
- Modify files outside `$HERMES_KANBAN_WORKSPACE` unless the task body says to.
- Create follow-up tasks assigned to yourself — assign to the right specialist.
- Complete a task you didn't actually finish. Block it instead.

## Kanban notify subscriptions lifecycle

Subskrypcje w `kanban_notify_subs` powinny być tworzone tylko dla aktywnych tasków i usuwane gdy task osiąga stan finalny. Zasady:

- `kanban-auto-notify.py` (cron co minutę): **nie tworzy** subs dla `status IN ('done','archived')` — filtr w zapytaniu SELECT
- Gateway notifier (`run.py`): po dostarczeniu eventu `completed`/`archived`, jeśli `task.status in {done, archived}` — automatycznie usuwa sub (via `_kanban_unsub`)
- `_cleanup_stale_done_subs()` w `kanban-auto-notify.py`: sweep co minutę usuwa stare subs dla done/archived tasków starszych niż 1h (safety net)

**ANTI-PATTERN który był przyczyną Slack 429 flood (2026-05-30):** stale subskrypcje dla done/archived tasków + backlog ~790 eventów = 75× 429 w ciągu dnia. Jeśli widzisz anomalię Slack rate-limit, sprawdź:
```bash
sqlite3 ~/.hermes/kanban/boards/*/kanban.db \
  "SELECT s.task_id, t.status, t.completed_at FROM kanban_notify_subs s JOIN tasks t ON s.task_id=t.id WHERE t.status IN ('done','archived')"
```
Jeśli cokolwiek wyświetla — zombie subs. Czyść ręcznie lub poczekaj na sweep (do 1h).

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

## ŁAŃCUCH Z KARTĄ REVIEW — kończ `kanban_complete`, nigdy `kanban_block` (decyzja Tomka, 2026-08-17)

**Jeśli twoje zadanie ma kartę potomną typu review/merge/release — zakończ przez
`kanban_complete(summary=..., metadata=...)` z pełnym handoffem. NIE przez
`kanban_block(reason="review-required: ...")`.** Dotyczy to również sytuacji, gdy treść zadania
mówi „nie merge'uj i nie wdrażaj samodzielnie" — to polecenie zabrania ci **merge'a**, a nie
każe ci **blokować kartę**.

Powód (incydent movielib t_b0d9e71b, 2026-08-17): bramka zależności w `kanban_db.py`
(`recompute_ready`) promuje kartę potomną do `ready` dopiero wtedy, gdy **wszyscy rodzice mają
status `done` albo `archived`**. Rodzic w stanie `blocked` **nigdy** nie zwalnia dziecka. Karta
review nie wystartowała, release za nią też nie, a gotowy PR przeleżał bezczynnie — mimo że
review miał wykonać worker, nie człowiek. To zakleszczenie, nie zabezpieczenie.

**Merge wstrzymuje stan PR-a, nie status twojej karty.** Zabezpieczenie zostaje bez zmian:
`gh pr create --draft` + `gh pr edit <nr> --add-label hold` (konieczne zwłaszcza na boardach
z automerge-cronem — patrz lekcja hermes-mapy PR #286). Worker review po PASS robi
`gh pr edit <nr> --remove-label hold && gh pr ready <nr>` i dopiero wtedy merge; po FAIL dokłada
etykietę `review-failed` i blokuje z findingami.

**`kanban_block` zostaje wyłącznie dla sytuacji, których nie podejmie żadna karta potomna:**
brak dostępu lub uprawnień, czerwony build nie do naprawy w 3 próbach, decyzja produktowa
właściciela, wyczerpany budżet iteracji, oraz zmiany dotykające sekretów/kluczy API
(`security-review-required`).

**Zanim zablokujesz z powodem zawierającym „review" — sprawdź, czy nie masz karty potomnej
review.** Jeśli masz: to nie jest przypadek na blokadę.
