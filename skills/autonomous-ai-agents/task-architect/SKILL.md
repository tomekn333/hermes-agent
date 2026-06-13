---
name: task-architect
description: "Use whenever user wants to PLAN, BREAK DOWN, or CREATE Kanban tasks for a project. Triggers: 'zaplanuj', 'rozbij na taski', 'opracuj plan', 'stwórz zadanie', 'co dalej z X', '/plan', '/architect', '/refine'. ALSO triggers automatically in #proj-* Slack channels when user describes work to be done. NOT for executing tasks — use kanban-worker for that."
version: 1.0.0
author: Cowork (Claude Opus 4.7) + Tomek
metadata:
  hermes:
    tags: [kanban, planning, task-creation, slack, architect]
    related_skills: [kanban-worker, kanban-codex-lane, hermes-agent]
    auto_load_in_channels: ["proj-asiacrm", "proj-usage-dashboard", "proj-exsite", "proj-hermes-mapy", "proj-pingmon", "proj-n8n", "proj-ai-infra"]
---

# Task Architect

## Cel skilla

Wymusić **wysokiej jakości** opisy zadań w Hermes Kanban przed dispatchem do workerów. Aktualnie taski są często mgliste — zawierają cel i acceptance criteria, ale brakuje:

- **konkretnych ścieżek plików** (worker traci czas zgadując gdzie jest dany komponent)
- **konkretnych identyfikatorów** (klasy CSS, nazwy React komponentów, funkcji w Go)
- **biznesowego "Why"** (dlaczego? dla kogo? jakie konsekwencje?)
- **linku do wątku Slack/screenshot** (worker nie ma kontekstu rozmowy)
- **worker hinta** (codex / claude-code / human review)

Twoim zadaniem jest **wymuszenie pełnego formatu** — nie pozwalaj sobie ani Tomkowi pominąć żadnej sekcji.

---

## Proces — 5 kroków (ZAWSZE w tej kolejności)

> ⚠️ **Ten skill ZAWSZE TWORZY taski** — nigdy nie jest „tylko planem". Slash `/plan` to INNY, wbudowany skill (plan-only, zapisuje plik do `.hermes/plans/` i NIC nie tworzy) — to nie jest task-architect. Jeśli trafi do Ciebie request planowania, kończysz utworzeniem tasków + dispatch.
> ⚠️ **Oszczędny discovery:** użyj `kanban_list_tasks` + 1-2 celowanych `grep` na frazach z requestu. NIE czytaj całego repo w czacie (drenaż tokenów + minuty zwłoki) — głębokie czytanie kodu to robota workera, nie planisty. Cel: od requestu do utworzonych tasków w ≤ kilka wywołań.

### Krok 1: DISCOVER (zanim cokolwiek zaproponujesz)

**Channel → board mapping** (z `config.yaml/routing.slack_channel_to_board`):

| Slack | Board | Lokalne repo |
|---|---|---|
| `#proj-asiacrm` | `asiacrm` | `/home/tomek/projects/asiacrm` |
| `#proj-usage-dashboard` | `usage-dashboard` | `/home/tomek/projects/usage-dashboard` |
| `#proj-exsite` | `exsite` | (zdalne na `tomek@192.168.1.35:/var/www/html/exsite/`) |
| `#proj-hermes-mapy` | `hermes-mapy` | `/home/tomek/projects/hermes-mapy` |
| `#proj-pingmon` | `pingmon` | `/home/tomek/projects/pingmon` |
| `#proj-n8n` | `n8n-workflows` | `/home/tomek/projects/n8n-workflows` |
| `#proj-ai-infra` | `ai-infrastructure` | `/home/tomek/projects/ai-observability` + `/home/tomek/.hermes/` |

**Co musisz zrobić zanim zaproponujesz plan:**

1. **`kanban_list_tasks --board <board>`** — ZAWSZE NAJPIERW. **TWARDA ZASADA ANTY-DUPLIKAT:** jeśli na boardzie już istnieje task (lub komplet) o tym samym/zbliżonym tytule albo zakresie (np. ktoś — Cowork/Tomek — utworzył je ręcznie), NIE twórz drugiej kopii. Zamiast tego odpisz w wątku ID istniejących tasków i ewentualnie je uzupełnij (`kanban_update_task`). Tworzysz NOWE taski tylko gdy realnie brak pokrycia.
2. **`git ls-files <repo>` + `grep`** na frazach z requestu Tomka — znajdź konkretne pliki/komponenty
3. **Memory check** — przeczytaj `project_<board>.md` z `/home/tomek/.hermes/memories/` (znana wiedza o projekcie)
4. **Slack thread context** — jeśli rozmowa toczyła się w wątku, przeczytaj ostatnie 5-10 wiadomości

### Krok 2: REFINE (1-3 pytania, opcjonalne)

Zadawaj pytania **tylko gdy:**
- Scope jest niejasny ("popraw UI" — co konkretnie?)
- Są dwie sensowne interpretacje
- Brakuje informacji o priorytecie / deadline
- Nie wiesz czy worker ma działać czy potrzebny human review

**Format pytań** (PO POLSKU, krótko):
> 🤔 Zanim zaplanuję, muszę dopytać:
> 1. **Pytanie konkretne?** Opcje: A) ... B) ... C) ...
> 2. **Drugie pytanie?**

Max 3 pytania. Jeśli masz pewność co do scope — pomiń ten krok.

### Krok 3: PLAN (propozycja w tabeli w wątku Slack)

Ogłoś breakdown **w wątku Slack jako tabelę** — to INFORMACJA dla Tomka, NIE pytanie. Bezpośrednio po niej przechodzisz do Kroku 4 i tworzysz taski. NIE czekasz na odpowiedź.

```
🎯 Plan dla #proj-X (board: X):

| # | Title | Why | Files | Effort |
|---|-------|-----|-------|--------|
| 1 | Usunąć kartę statystyk z widoku mapy | UX cleanup mobile | `src/components/MapView.jsx:120-150` `.stats-card` w map-cards.css | S (codex, 30min) |
| 2 | ... | ... | ... | ... |

Tworzę je teraz i dispatchuję — bez czekania na potwierdzenie.
```

**Limity:**
- **Max 7 tasków na jeden plan** (więcej = zbyt szeroki scope, podziel)
- **Każdy task = 1 worker session** (~150 iteracji max, nie "and"-taski)
- **Effort estimate** (S/M/L) + sugerowany worker (codex/claude-code/human)

### Krok 4: COMMIT (BEZ czekania na confirmation)

⚠️ **ŻELAZNA ZASADA od 2026-06-08:** NIE czekamy na "ok" — od razu tworzymy taski + dispatch + auto-merge gdy gotowe. Tomek wycofał fazę confirmation równolegle z fazą review.

Tylko **w trzech przypadkach** wracaj do Kroku 3 z poprawą zamiast committować od razu:
1. Plan zawiera zmiany dot. danych pacjentów AsiaCRM (real prod)
2. Plan wymaga rotacji credentials/secrets
3. Plan wymaga migracji DB destrukcyjnych

W pozostałych przypadkach: tabela z Kroku 3 → od razu komenda `kanban_create_task` → dispatcher spawnuje workera w ciągu 60s.

Po commicie tasków napisz w wątku Slack:
> ✅ Utworzyłem 3 taski w boardzie `<name>` i odpaliłem workery. Pierwsze edycje za ~3-15 min, status będę raportował co kolejne 5-10 min. PR-y zmergują się automatycznie po complete (rollback gdy potrzeba — mów "rollback").

### Krok 5: PROGRESS PING (auto status reporting)

Po commicie z Kroku 4, **aktywnie monitoruj** workery i raportuj progres:

- **Co 5-10 min** sprawdź stan: `kanban_show <task_id>`, sprawdź log workera (`ls -la /home/tomek/.hermes/kanban/boards/<board>/logs/<task_id>.log` + `tail -5`)
- **Pisz krótki status w wątku Slack** gdy zauważysz znaczącą zmianę:
  - "🔍 Worker w discovery (czyta X plików)" — gdy worker długo czyta przed edycją
  - "✏️ Worker zaczął edytować (plik X)" — pierwszy write
  - "🧪 Build/testy w toku" — gdy worker odpalił testy
  - "✅ Done, PR <link>, merge w toku" — complete
  - "❌ Worker padł (reason: X) — spawn nowego" — crash
- Tomek nie powinien musieć pytać "czy juz pracuje?". Jeśli pyta — to znaczy że za rzadko pingujesz.

---

## FORMAT TASKA v2 (WYMAGANY)

Każdy `body` taska MUSI zawierać DOKŁADNIE te sekcje, w tej kolejności:

```markdown
## Why
[1-2 zdania — kontekst biznesowy, dlaczego to robimy.]
[Przykład: "Tomek raportuje że UI mapy jest zatłoczone na mobile (<400px viewport).
Cleanup nagłówków kart zwiększy czytelność dla nowych użytkowników (~30% trafiku)."]

## What
[Konkretne, mierzalne zmiany w bulletpointach. NIE "wyczyścić" — TAK "usunąć element <X>".]
- Usunąć `<StatsCard>` z głównego layoutu mapy (`MapView.jsx:130`)
- Skrócić tekst nagłówka karty warstw z "Warstwy — przeciągnij aby przesunąć" na "Warstwy"
- Usunąć element `.drag-hint-text` z `LayersCard.jsx`

## Where (konkretne ścieżki — DISCOVERY przez grep!)
[NIGDY "Frontend mapy w repo X". ZAWSZE konkretne pliki + linie + klasy/komponenty.]
- `src/components/MapView.jsx` (linia ~120-150, gdzie renderowane są overlay cards)
- `src/components/cards/StatsCard.jsx` (komponent do usunięcia)
- `src/components/cards/LayersCard.jsx` (linia ~45, tekst nagłówka)
- `src/styles/map-cards.css` (klasy `.stats-card`, `.drag-hint-text`)

## Acceptance criteria (mierzalne, weryfikowalne)
[Każdy bullet = test, który można wykonać. NIE "zweryfikowane wizualnie" bez konkretu.]
- [ ] `<StatsCard>` nieobecny w DOM dla widoku `/map` (test: query `document.querySelector('.stats-card')` zwraca `null`)
- [ ] Nagłówek karty warstw zawiera tylko tekst "Warstwy" (test: `LayersCard` snapshot test)
- [ ] Klasa `.drag-hint-text` nie pojawia się w żadnym renderowanym DOM (grep `.drag-hint-text` w build output = 0 matches)
- [ ] Screenshot before/after wstawiony do PR (Tomek zatwierdza wizualnie)

## Worker hint
[codex | claude-code | human-review | mixed]
**Rekomendacja:** codex (single-file UI fix, < 60 linii diff, brak nowych zależności)

## Context
- Slack thread: <link do wątku>
- Powiązany task: (jeśli jest)
- Memory: `project_hermes_mapy.md` (Cowork)
- Screenshot: <link jeśli załączony>
```

---

## Anti-patterny (NIGDY)

❌ **Title vague**: "popraw UI" → ✅ "Usunąć kartę statystyk + cleanup nagłówków w widoku mapy"
❌ **Where = "Frontend mapy w repo X"** → ✅ konkretne ścieżki z `grep`
❌ **Acceptance: "zweryfikowane wizualnie"** → ✅ "Screenshot before/after wstawiony do PR"
❌ **And-task**: "Dodać feature X **i** zrefaktorować Y" → ✅ podziel na 2
❌ **Task bez Why** → worker nie wie jak rozstrzygać edge case
❌ **Task bez worker hint** → dispatcher musi zgadywać kogo spawn'ować
❌ **Tworzenie tasków bez confirmation** → Tomek zgubił kontrolę nad scope

---

## Przykład — dobre vs słabe

### ❌ SŁABY task (przed nowym formatem)

> **Title**: Uprościć górne sekcje kart na widoku mapy
> **Body**: Cel — Uprościć UI bez ruszania layoutu. Zakres — ukryć kartę statystyk, usunąć drag hint, zminimalizować nagłówek karty warstw. Pliki — Frontend mapy w repo hermes-mapy. Acceptance — karty mają zwięzły nagłówek, layout bez zmian, zweryfikowane wizualnie.

**Problem**: worker musi sam grep'ować pliki, "zweryfikowane wizualnie przez kogo" niejasne, brak Why.

### ✅ DOBRY task (po nowym formacie)

> **Title**: Usunąć kartę statystyk + cleanup nagłówków w widoku mapy
>
> ## Why
> Tomek raportuje że UI mapy jest zatłoczone helper-textami szczególnie na mobile. Cleanup nagłówków + usunięcie redundantnej karty statystyk zwiększy czytelność (Tomek to potwierdza w wątku <link>).
>
> ## What
> - Usunąć `<StatsCard>` z głównego layoutu mapy
> - Skrócić nagłówek karty warstw do samego słowa "Warstwy"
> - Usunąć element `.drag-hint-text` (tekst "przeciągnij aby przesunąć")
>
> ## Where
> - `/home/tomek/projects/hermes-mapy/src/components/MapView.jsx:130` (render `<StatsCard>`)
> - `/home/tomek/projects/hermes-mapy/src/components/cards/LayersCard.jsx:45` (tekst nagłówka)
> - `/home/tomek/projects/hermes-mapy/src/styles/map-cards.css` (klasy `.stats-card`, `.drag-hint-text`)
>
> ## Acceptance
> - [ ] `<StatsCard>` usunięty z `MapView.jsx`
> - [ ] Nagłówek karty warstw = tylko "Warstwy"
> - [ ] Klasa `.drag-hint-text` usunięta z CSS
> - [ ] Build przechodzi (`npm run build`)
> - [ ] Screenshot before/after w PR (Tomek zatwierdza)
>
> ## Worker hint
> **codex** (single-file UI fix, ~50 linii diff)
>
> ## Context
> - Slack: <link do wątku>
> - Memory: `project_hermes_mapy.md`

---

## Jak aktywować

Skill aktywuje się **automatycznie** gdy:

1. User pisze w kanale `#proj-*` cokolwiek brzmiącego jak request (nie pytanie czystą informacyjne)
2. User używa keywords: "zaplanuj", "rozbij na taski", "opracuj plan", "stwórz zadanie", "co dalej z X"
3. User używa slash commands: `/plan`, `/architect`, `/refine`
4. Hermes dostaje DM z requestem planowania

Skill nie aktywuje się gdy:
- User pyta o status istniejącego taska
- User chce wykonać task (to robi `kanban-worker`)
- User pyta o coś czysto informacyjnego ("jak działa X?")

---

## Komendy Hermes którym ufasz

- `kanban_list_tasks --board <name>` — lista tasków w boardzie
- `kanban_show <task_id>` — pełne dane taska
- `kanban_create_task --board <name> --title "..." --body "..."` — tworzenie
- `kanban_update_task <id> --body "..."` — edycja istniejącego
- Lokalne `bash` do `grep`, `git ls-files`, `cat`, `find` w `/home/tomek/projects/<board>`

---

## Final reminder

**Jakość taska = jakość workera.** Worker spędza średnio 30-150 iteracji na tasku. Każda minuta którą Ty (Hermes Opus 4-8) poświęcisz na precyzję — oszczędza godziny workerom (Codex 5.5 / Claude Code) plus zapobiega rework gdy Tomek odrzuca PR bo "to nie to o co prosiłem".

**Zasada główna:** lepszy 1 dobrze opisany task niż 5 niedopracowanych.
