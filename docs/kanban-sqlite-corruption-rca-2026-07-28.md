# RCA: nawracająca korupcja Kanban SQLite (2026-07-28)

## Zakres incydentu

Board `transcriber` uległ czterem fizycznym korupcjom w ciągu doby 27–28 lipca 2026. Wcześniej ten sam rodzaj awarii wystąpił na boardzie `hermes-mapy`.

Objawy:

- `database disk image is malformed`,
- pozorny `NULL` w `kanban_notify_subs.task_id` widoczny w `integrity_check`, ale nie w zwykłym `SELECT`,
- `disk I/O error` podczas `PRAGMA journal_mode=WAL`,
- proces gatewaya trzymający otwarte, odlinkowane pliki `kanban.db-wal` i `kanban.db-shm`.

## Potwierdzona przyczyna

`apply_wal_with_fallback()` traktował każdy `disk I/O error` podczas ustawiania WAL jak trwałą niekompatybilność filesystemu. Każde nowe połączenie Kanban wykonywało `PRAGMA journal_mode=WAL`; po przejściowym EIO helper przełączał aktywną bazę na `journal_mode=DELETE`.

Na lokalnym ext4 EIO nie oznacza trwałej niekompatybilności WAL. Wiele procesów i wątków nadal otwierało tę samą bazę w WAL. Powstawały kohorty używające różnych protokołów journalingu, a przełączenie trybu usuwało lub zastępowało sidecary przy żywych deskryptorach. To tłumaczy jednocześnie:

1. odlinkowane WAL/SHM nadal otwarte przez gateway,
2. kolejne `disk I/O error` przy tworzeniu nowych sidecarów,
3. fizyczne podstawianie stron B-tree z innych tabel.

Analiza kopii wykazała m.in.:

- w obrazie z 12:34 root page tabeli `kanban_notify_subs` była bajtowo identyczna ze stroną `task_comments`,
- w obrazie z 11:08 root page `kanban_notify_subs` była niemal kopią strony `tasks`,
- obraz z 00:04 był obcięty o dwie strony,
- nie wykryto wzorca TLS w badanych stronach.

Zwykłe `SQLITE_BUSY`, brak jawnego `PRAGMA busy_timeout` ani współdzielenie jednego obiektu `sqlite3.Connection` między wątkami nie wyjaśniają fizycznego nadpisania stron. Połączenia produkcyjne mają domyślne `check_same_thread=True`, timeout 30 s i używają `BEGIN IMMEDIATE` dla transakcji wielozapisowych.

## Poprawka

Commit `59efaee36` (`fix(state): never silently downgrade WAL to DELETE on transient EIO`) wprowadza dwie warstwy ochrony:

1. usuwa `disk i/o error` z `_WAL_INCOMPAT_MARKERS`; przejściowy EIO jest ponownie zgłaszany zamiast uruchamiać fallback,
2. przed każdym legalnym fallbackiem sprawdza aktualny tryb bazy; jeśli plik jest już w WAL, odmawia przełączenia na DELETE i ponownie zgłasza pierwotny błąd.

Fallback pozostaje dostępny wyłącznie dla deterministycznych błędów niekompatybilnego filesystemu (`locking protocol`, `not authorized`) i tylko dla bazy, która nie jest już w WAL.

## Recovery i backupy

Kod Kanban już działa defensywnie przy wykryciu korupcji:

- `_validate_sqlite_header()` odrzuca uszkodzony page 0,
- `_guard_existing_db_is_healthy()` wykonuje `PRAGMA integrity_check`,
- `_backup_corrupt_db()` zachowuje plik główny oraz obecne sidecary WAL/SHM,
- `KanbanDbCorruptError` blokuje ciche utworzenie pustej bazy na miejscu uszkodzonej,
- snapshoty korupcji są limitowane do jednego na 10 minut, aby awaria nie wypełniła dysku.

Nie wdrożono automatycznej podmiany aktywnej bazy przez `.recover`. Taka operacja przy żywych połączeniach mogłaby odtworzyć dokładnie ten sam mechanizm dwóch kohort WAL. Bezpieczna procedura odzyskania musi być offline: zatrzymać nowych pisarzy, zachować main/WAL/SHM, odzyskać do nowego pliku, zweryfikować pełnym `integrity_check`, a dopiero potem atomowo podmienić bazę i ponownie uruchomić konsumentów.

Kopie diagnostyczne wykonane zwykłym `copy2` dla main/WAL/SHM nie są transakcyjnie atomowym snapshotem. Służą do forensics; nie wolno uznawać ich za spójny punkt odzyskania bez ponownego `integrity_check`.

## Weryfikacja

TDD:

- przed poprawką reproduktor `existing WAL + przejściowy błąd markera` nie zgłaszał wyjątku i wykonywał fallback do DELETE,
- po poprawce ten sam reproduktor oraz testy regresyjne przechodzą.

Testy ukierunkowane obejmują:

- ponowne zgłoszenie `disk I/O error`,
- odmowę downgrade, gdy baza jest już w WAL,
- zachowanie legalnego fallbacku dla `locking protocol` i `not authorized`,
- ścieżkę `kanban_db.connect()` na świeżej bazie z niekompatybilnym WAL.

Wynik: 6/6 testów ukierunkowanych PASS. Szerszy przebieg: 353 PASS i jeden niezwiązany test skill-sync FAIL (`test_default_spawn_appends_per_task_skills`), wynikający z wcześniejszego lokalnego patcha pomijającego nierozwiązywalne skille.

## Pozostałe ryzyka operacyjne

- Nie wykonywać ręcznego `rm`, `mv` ani kopiowania aktywnych `kanban.db-wal`/`kanban.db-shm`.
- Recovery wykonywać wyłącznie po zatrzymaniu wszystkich pisarzy do danego boardu.
- Wyłączony `kanban-auto-notify` i `hermes_orphan_worker_reaper.sh` nadal omijają część transakcyjnego API; mogą powodować `BUSY` lub niespójność semantyczną, ale audyt nie wykazał, by powodowały badaną fizyczną korupcję.
- `remove_board()` nadal wymaga ostrożności, ponieważ rename/rmtree aktywnego katalogu bazy nie ma międzyprocesowej blokady.
