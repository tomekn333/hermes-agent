#!/usr/bin/env python3
"""Idempotent hardening patches for Hermes kanban (2026-07-23; adapted for 0.21 on 2026-09-04).

fix7a: hermes_state.py - remove "disk i/o error" from _WAL_INCOMPAT_MARKERS.
       Transient SQLITE_IOERR on a local FS triggered WAL->DELETE journal-mode
       flip during live WAL traffic => single-page corruption of kanban.db
       (incidents 2026-05-25, 2026-07-21, 2026-07-23).
fix7b: kanban_db.py - rate-limit .corrupt snapshot creation in
       _backup_corrupt_db (max 1 per 10 min; incident produced 3728 files).
fix7c: kanban_db.py - SKILL-SYNC (per-task skills auto-copied to the worker's
       profile home; unresolvable skill -> skip flag + warning, not crash).

Exit 0 = all applied (or already present). Exit 1 = anchor missing.
"""
import os
import re
import sys
from pathlib import Path

AGENT = Path(os.environ.get("HERMES_AGENT_DIR") or Path(__file__).resolve().parents[1])
STATE = AGENT / "hermes_state.py"
KDB = AGENT / "hermes_cli" / "kanban_db.py"
rc = 0

def report(name, status):
    print(f"{name}: {status}")

# ---------- fix7a: WAL marker ----------
s = STATE.read_text(encoding="utf-8")
NEW_MARKER = (
    '    # HARDENING 2026-07-23 (fix7a): "disk i/o error" removed from markers.\n'
    '    # A transient SQLITE_IOERR on a local FS used to trigger a WAL->DELETE\n'
    '    # journal-mode flip during live WAL traffic, corrupting kanban.db pages.\n'
    '    # Real network-FS cases are still covered by the two markers above.\n'
)
try:
    block_start = s.index("_WAL_INCOMPAT_MARKERS = (")
    block_end = s.index("\n)", block_start) + 2
    marker_block = s[block_start:block_end]
except ValueError:
    marker_block = ""

# Idempotencję określa stan semantyczny listy markerów, a nie obecność naszego
# komentarza. Upstream może zachować poprawkę bez komentarza po rebase/update.
if marker_block and not re.search(r"(?i)[\"']disk i/o error[\"']", marker_block):
    report("fix7a", "already-applied")
elif marker_block and "Disambiguate by retrying the pragma" in s and "another process already set WAL on disk" in s:
    # Upstream >= 0.21 ("Bug D" fix): a disk i/o error on PRAGMA journal_mode=WAL
    # is retried (transient EIO clears -> stays WAL) and the DELETE fallback is
    # refused when the on-disk journal mode is already WAL or cannot be verified.
    # That covers the 2026-05/07 local-FS incidents without removing the marker,
    # so the ZFS/APFS deterministic case keeps its fallback. Nothing to apply.
    report("fix7a", "superseded-by-upstream (EIO retry + on-disk WAL guard)")
elif marker_block:
    updated_block, replacements = re.subn(
        r"(?m)^[ \t]*[\"']disk i/o error[\"'][^\n]*\n",
        NEW_MARKER,
        marker_block,
        count=1,
    )
    if replacements == 1:
        STATE.write_text(
            s[:block_start] + updated_block + s[block_end:],
            encoding="utf-8",
        )
        report("fix7a", "APPLIED")
    else:
        report("fix7a", "ANCHOR-MISSING"); rc = 1
else:
    report("fix7a", "ANCHOR-MISSING"); rc = 1

# ---------- fix7b: snapshot rate-limit ----------
k = KDB.read_text(encoding="utf-8")
OLD_B = '''    base_name = resolved.name  # basename only
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
'''
NEW_B = '''    base_name = resolved.name  # basename only
    # HARDENING 2026-07-23 (fix7b): rate-limit corrupt snapshots. Every failed
    # connect() (notifier tick, dispatcher, workers - many per second during an
    # outage) used to create its own copy: 3728 files / 2.7 GB on 2026-07-23.
    # Keep at most one snapshot per 10 minutes per DB basename.
    import time as _time
    try:
        for _existing in parent.glob(base_name + ".corrupt.*"):
            try:
                if _time.time() - _existing.stat().st_mtime < 600:
                    return None
            except OSError:
                continue
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
'''
if "HARDENING 2026-07-23 (fix7b)" in k:
    report("fix7b", "already-applied")
elif "def _backup_corrupt_db" in k and "hashlib.sha256()" in k.split("def _backup_corrupt_db", 1)[1][:4000]:
    # Upstream >= 0.21 deduplicates corrupt snapshots by content hash (same
    # corrupt bytes -> one backup), which solves the 3728-files incident
    # differently. Nothing to apply.
    report("fix7b", "superseded-by-upstream (content-hash dedup)")
elif k.count(OLD_B) == 1:
    k = k.replace(OLD_B, NEW_B, 1)
    KDB.write_text(k, encoding="utf-8")
    report("fix7b", "APPLIED")
else:
    report("fix7b", "ANCHOR-MISSING"); rc = 1

# ---------- fix7c: SKILL-SYNC (re-apply after hermes update) ----------
k = KDB.read_text(encoding="utf-8")
CLI = AGENT / "cli.py"
_cli = CLI.read_text(encoding="utf-8") if CLI.exists() else ""
if "SKILL-SYNC PATCH" in k:
    report("fix7c", "already-applied")
elif "Unknown skill(s) requested, skipping" in _cli:
    # Upstream >= 0.21: an unknown per-task skill is skipped with a warning
    # as long as at least one requested skill loads (kanban-worker always
    # does), so the worker no longer crash-loops on a typo'd skill name.
    report("fix7c", "superseded-by-upstream (graceful unknown-skill skip)")
else:
    HELPER = '''
# SKILL-SYNC PATCH (2026-07-23, fix7c): per-task skills were passed blindly to
# the worker CLI; a name unresolvable under the worker's HERMES_HOME is fatal
# at startup ("Error: Unknown skill(s): ..."), crash-looping the task. Ensure
# the skill resolves in the worker home: copy from root home if missing there;
# if missing everywhere, skip the flag and log a warning instead of crashing.
def _ensure_task_skill_available(skill_name, hermes_home):
    import shutil as _shutil
    from pathlib import Path as _Path
    try:
        name = str(skill_name or "").strip()
        if not name or "/" in name or "\\\\" in name or ".." in name:
            return False
        base = _Path(hermes_home) if hermes_home else (_Path.home() / ".hermes")
        skills_root = base / "skills"
        try:
            for skill_md in skills_root.rglob(name + "/SKILL.md"):
                if skill_md.is_file():
                    return True
        except OSError:
            pass
        root_skills = _Path.home() / ".hermes" / "skills"
        src_dir = None
        try:
            for skill_md in root_skills.rglob(name + "/SKILL.md"):
                if skill_md.is_file():
                    src_dir = skill_md.parent
                    break
        except OSError:
            src_dir = None
        if src_dir is None:
            return False
        try:
            rel = src_dir.relative_to(root_skills)
        except ValueError:
            rel = _Path(name)
        dest = skills_root / rel
        if dest.exists():
            return (dest / "SKILL.md").is_file()
        dest.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copytree(src_dir, dest)
        _log.info(
            "kanban skill-sync: copied skill %r from %s to %s", name, src_dir, dest
        )
        return (dest / "SKILL.md").is_file()
    except Exception:
        _log.warning(
            "kanban skill-sync: failed to ensure skill %r for home %r",
            skill_name, hermes_home, exc_info=True,
        )
        return False


def _skill_sync_warn(task_id, skill_name):
    _log.warning(
        "kanban skill-sync: skill %r does not resolve for task %s worker "
        "(also absent from root home) - skipping --skills flag instead of crashing",
        skill_name, task_id,
    )


'''
    ANCHOR = "def _worker_terminal_timeout_env(\n"
    OLD_A = '''    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                cmd.extend(["--skills", sk])
'''
    NEW_A = '''    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                # SKILL-SYNC PATCH: see _ensure_task_skill_available
                if _ensure_task_skill_available(sk, env.get("HERMES_HOME")):
                    cmd.extend(["--skills", sk])
                else:
                    _skill_sync_warn(task.id, sk)
'''
    OLD_C = '''            if task.skills:
                for sk in task.skills:
                    if sk and sk != "kanban-worker":
                        _c.extend(["--skills", sk])
'''
    NEW_C = '''            if task.skills:
                for sk in task.skills:
                    if sk and sk != "kanban-worker":
                        # SKILL-SYNC PATCH: see _ensure_task_skill_available
                        if _ensure_task_skill_available(sk, env.get("HERMES_HOME")):
                            _c.extend(["--skills", sk])
                        else:
                            _skill_sync_warn(task.id, sk)
'''
    if k.count(ANCHOR) == 1 and k.count(OLD_A) == 1 and k.count(OLD_C) == 1:
        k = k.replace(ANCHOR, HELPER.lstrip("\n") + ANCHOR, 1)
        k = k.replace(OLD_A, NEW_A, 1)
        k = k.replace(OLD_C, NEW_C, 1)
        KDB.write_text(k, encoding="utf-8")
        report("fix7c", "APPLIED")
    else:
        report("fix7c", "ANCHOR-MISSING"); rc = 1

sys.exit(rc)
