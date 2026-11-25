"""The two stores a review may change, and how a change gets into them safely.

MEMORY: core.memory.memory_dir(home): one fact per Markdown file with frontmatter (name, description,
metadata.type = user | feedback | project | reference) and the index MEMORY.md, one line per live file.
SKILLS: <home>/skills/<name>/SKILL.md plus support files; <name>/.history/ keeps earlier versions.

A run never lets the reviewer near the real stores:
  1. prepare(): back up the real stores and copy them into a staging directory (.history/ left out);
  2. the reviewer edits the staged copies, and cannot write anywhere else;
  3. validation: memory files the reviewer created or changed need valid frontmatter and no secrets (problems
     a file already had before the run do not count, so one old broken file never blocks the loop); the
     index is repaired; every touched skill is validated against ITS OWN problems before the run and rolled
     back in the stage when the edit added a problem, deleted something or put a secret in it;
  4. apply_memory() / apply_skills() are three-way: a file goes into the real store only while the real file
     is still what was staged. Whatever the live agent wrote in the meantime wins: it is never overwritten
     and never deleted.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import learnlog  # noqa: E402
from core.config import atomic_write  # noqa: E402
from core.redact import SECRET_RE  # noqa: E402
from tools import new_skill  # noqa: E402

INDEX = "MEMORY.md"
ARCHIVE = ".archive"
TYPES = ("user", "feedback", "project", "reference")
SKIP_PARTS = (".history", "__pycache__")
LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+\.md)>?\s*\)")


def read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------- frontmatter

def frontmatter(text: str) -> dict | None:
    """name, description, type (top level, or under `metadata:` as Claude Code writes it) and archived of a
    memory file; None when there is no frontmatter block."""
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text or "", re.S)
    if not m:
        return None
    block = m.group(1)
    fm: dict = {}
    for key in ("name", "description", "archived"):
        mm = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*$", block, re.M)
        if mm:
            fm[key] = mm.group(1).strip().strip("'\"")
    mm = re.search(r"^type:[ \t]*([\w-]+)", block, re.M)
    if not mm:
        meta = re.search(r"^metadata:[ \t]*\r?\n((?:[ \t]+[^\n]*(?:\n|$))+)", block, re.M)
        mm = re.search(r"^[ \t]+type:[ \t]*['\"]?([\w-]+)", meta.group(1), re.M) if meta else None
    if mm:
        fm["type"] = mm.group(1)
    return fm


def is_archived(fm: dict | None) -> bool:
    return bool(fm) and str(fm.get("archived", "")).lower() in ("true", "yes", "1")


def file_problems(path: Path) -> list[str]:
    text = read(path)
    probs = []
    if SECRET_RE.search(text):
        probs.append("looks like it contains a secret")
    fm = frontmatter(text)
    if fm is None:
        return probs + ["no frontmatter (--- name / description / metadata.type ---)"]
    if is_archived(fm):
        return probs
    if not fm.get("name") or not fm.get("description"):
        probs.append("frontmatter lacks name or description")
    if fm.get("type") not in TYPES:
        probs.append(f"type {fm.get('type')!r} is not one of {', '.join(TYPES)}")
    return probs


# ---------------------------------------------------------------------------------------------- snapshots

def snapshot(d: Path | None, *, md_only: bool = False) -> dict[str, str]:
    """relative path -> sha256 of every file under `d` (.history/ and __pycache__/ left out)."""
    out: dict[str, str] = {}
    if d is None or not Path(d).is_dir():
        return out
    d = Path(d)
    for f in sorted(d.rglob("*")):
        rel = f.relative_to(d)
        if any(p in SKIP_PARTS for p in rel.parts) or f.is_symlink() or not f.is_file():
            continue
        if md_only and f.suffix != ".md":
            continue
        try:
            out[rel.as_posix()] = sha(f)
        except OSError:
            continue
    return out


def copy_tree(src: Path, dest: Path) -> None:
    """Copy a store (links dereferenced, so the copy holds no path that leads out of it)."""
    if Path(src).is_dir():
        shutil.copytree(src, dest, symlinks=False, ignore=shutil.ignore_patterns(*SKIP_PARTS),
                        ignore_dangling_symlinks=True, dirs_exist_ok=True)
    else:
        Path(dest).mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


@dataclass
class Stage:
    root: Path                      # the reviewer's working directory
    memory: Path                    # staged copy of the memory store
    skills: Path | None             # staged copy of the skills (skill reviews in auto mode)
    proposals: Path | None          # where proposals go (skill reviews in propose mode)
    base: Path                      # untouched copy of the stores as staged (the backup of a live run)
    base_memory: dict = field(default_factory=dict)
    base_skills: dict = field(default_factory=dict)
    memory_baseline: dict = field(default_factory=dict)
    skill_baseline: dict = field(default_factory=dict)

    def write_dirs(self) -> list[Path]:
        return [p for p in (self.memory, self.skills, self.proposals) if p is not None]


def prepare(home: Path, name: str, *, real_memory: Path, real_skills: Path, dry: bool, skills: bool = False,
            proposals: bool = False, keep_backups: int = 30) -> Stage:
    """Back up and stage the stores for one run. A live run's base copy is its backup
    (state/learn/backups/<name>/); a dry run keeps it next to its stage. Each store is read ONCE: the base is a
    copy of the staged copy, so the two are identical even when the live agent writes in between."""
    ld = learnlog.learn_dir(home)
    root = ld / ("dryrun" if dry else "stage") / name
    base = (root / "base") if dry else (ld / "backups" / name)
    copy_tree(real_memory, root / "memory")
    if not (root / "memory" / INDEX).exists():
        (root / "memory" / INDEX).write_text("", encoding="utf-8")
    copy_tree(root / "memory", base / "memory")
    st = Stage(root=root, memory=root / "memory", skills=None, proposals=None, base=base)
    if skills:
        copy_tree(real_skills, root / "skills")
        copy_tree(root / "skills", base / "skills")
        st.skills = root / "skills"
    if proposals:
        st.proposals = root / "proposals"
        st.proposals.mkdir(parents=True, exist_ok=True)
    st.base_memory = snapshot(st.memory, md_only=True)
    st.base_skills = snapshot(st.skills)
    st.memory_baseline = {f.name: file_problems(f) for f in st.memory.glob("*.md") if f.name != INDEX}
    st.skill_baseline = skill_problems_of(st.skills) if st.skills else {}
    # This run's own copies are never pruned: the rollback and the archives of this run read from them.
    learnlog.prune(ld / "backups", max(1, int(keep_backups)), protect=(name,))
    learnlog.prune(ld / "stage", 5, protect=(name,))
    learnlog.prune(ld / "dryrun", 5, protect=(name,))
    return st


# ---------------------------------------------------------------------------------------------- memory

def undo_memory_deletions(st: Stage) -> list[str]:
    """A reviewer must not delete memory files (the prompt says so; a Codex sandbox could still do it): every
    staged file that disappeared is put back from the base copy. Archiving goes through stubs instead."""
    after = snapshot(st.memory, md_only=True)
    restored = []
    for rel in sorted(set(st.base_memory) - set(after)):
        src = st.base / "memory" / rel
        if src.is_file():
            copy_file(src, st.memory / rel)
            restored.append(f"memory `{rel}`: deleting memory files is not allowed, restored")
    return restored


def memory_problems(st: Stage) -> list[str]:
    """Problems the reviewer introduced into the staged memory (empty = safe to apply)."""
    problems = []
    for rel, digest in sorted(snapshot(st.memory, md_only=True).items()):
        if st.base_memory.get(rel) == digest:
            continue                                   # not touched by the reviewer
        path = st.memory / rel
        parts = rel.split("/")
        if rel == INDEX or parts[0] == ARCHIVE:
            if SECRET_RE.search(read(path)):
                problems.append(f"{rel}: looks like it contains a secret")
            continue
        if len(parts) > 1:
            problems.append(f"{rel}: memory files belong at the top level of the store")
            continue
        old = st.memory_baseline.get(rel, [])
        problems += [f"{rel}: {p}" for p in file_problems(path) if p not in old]
    return problems


def link_target(line: str) -> str | None:
    m = LINK_RE.search(line)
    if not m:
        return None
    t = m.group(1)
    return t if "/" not in t and not t.startswith(("http://", "https://")) else None


def repair_index(mem: Path) -> list[str]:
    """Make MEMORY.md list every live fact once and nothing else: drop lines of missing, archived or
    duplicated files, add a line for a live file that has none."""
    idx = mem / INDEX
    lines = read(idx).splitlines() if idx.exists() else []
    live: dict[str, dict] = {}
    for f in sorted(mem.glob("*.md")):
        if f.name == INDEX:
            continue
        fm = frontmatter(read(f))
        if not is_archived(fm):
            live[f.name] = fm or {}
    fixes, kept, seen = [], [], set()
    for ln in lines:
        target = link_target(ln)
        if target:
            if target not in live:
                fixes.append(f"index: dropped the line of {target} (no such live file)")
                continue
            if target in seen:
                fixes.append(f"index: dropped a second line of {target}")
                continue
            seen.add(target)
        kept.append(ln)
    for fname, fm in sorted(live.items()):
        if fname not in seen and fm.get("name"):
            hook = f" — {fm['description']}" if fm.get("description") else ""
            kept.append(f"- [{fm['name']}]({fname}){hook}")
            fixes.append(f"index: added a line for {fname}")
    if fixes:
        atomic_write(idx, ("\n".join(kept).rstrip() + "\n") if kept else "", mode=0o644)
    return fixes


def _free_name(arch: Path, stem: str, stamp: str) -> Path:
    dest, n = arch / f"{stem}.{stamp}.md", 0
    while dest.exists():
        n += 1
        dest = arch / f"{stem}.{stamp}.{n}.md"
    return dest


def finalize_archives(mem: Path, base: Path | None = None, stamp: str | None = None) -> list[str]:
    """Archiving = an `archived: true` stub in place of the fact (the reviewer cannot delete files) plus a copy
    of the fact in .archive/. The copy is always the fact as it was before the run (`base`), and an archive copy
    that already existed is never replaced: a second copy of the same name gets a time-stamped name, and a
    reviewer's rewrite of an old archive copy is moved aside the same way. Once the copy exists, the stub goes,
    so nothing is lost and the live store stays lean."""
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    arch = mem / ARCHIVE
    old_arch = Path(base) / ARCHIVE if base else None
    if old_arch is not None and old_arch.is_dir():
        for old in sorted(old_arch.glob("*.md")):
            cur = arch / old.name
            if cur.is_file() and sha(cur) != sha(old):
                os.replace(cur, _free_name(arch, old.stem, stamp))     # keep the reviewer's text, aside
            if not cur.is_file():
                shutil.copy2(old, cur)                                  # the older archive copy stays as it was
    done = []
    for f in sorted(mem.glob("*.md")):
        if f.name == INDEX or not is_archived(frontmatter(read(f))):
            continue
        old = Path(base) / f.name if base else None
        old_text = old if old is not None and old.is_file() and not is_archived(frontmatter(read(old))) else None
        target = arch / f.name
        existed = old_arch is not None and (old_arch / f.name).is_file()
        if old_text is not None:
            if existed:
                target = _free_name(arch, f.stem, stamp)                # never over an older archive copy
            elif target.is_file() and sha(target) != sha(old_text):
                target = _free_name(arch, f.stem, stamp)                # the reviewer's copy differs: keep both
            if not target.is_file():
                arch.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_text, target)
        if old_text is None and base is None and not (arch / f.name).is_file():
            continue          # without the earlier state nothing proves the text is safe: leave the stub
        f.unlink()
        done.append(f"archived {f.name}")
    return done


def _describe(path: Path) -> str:
    fm = frontmatter(read(path)) or {}
    return fm.get("description") or fm.get("name") or path.name


def describe_memory(st: Stage) -> list[tuple[str, str]]:
    """(file, line for the owner) for every fact the review saved, updated or archived."""
    after = snapshot(st.memory, md_only=True)
    before = st.base_memory
    out = []
    facts = lambda names: sorted(n for n in names if n != INDEX and "/" not in n)  # noqa: E731
    for rel in facts(set(after) - set(before)):
        out.append((rel, f"saved: {_describe(st.memory / rel)}"))
    for rel in facts(r for r in set(after) & set(before) if after[r] != before[r]):
        out.append((rel, f"updated: {_describe(st.memory / rel)}"))
    for rel in facts(set(before) - set(after)):
        out.append((rel, f"archived: {_describe(st.base / 'memory' / rel)}"))
    return out


def merge_index(base: str, ours: str, theirs: str) -> str:
    """Three-way merge of MEMORY.md: the live index (theirs) plus the lines the review added, minus the lines
    it removed. A line the review rewrote keeps its place."""
    b, o, t = base.splitlines(), ours.splitlines(), theirs.splitlines()
    removed = {ln for ln in b if ln not in set(o)}
    added = [ln for ln in o if ln not in set(b)]
    by_target = {}
    for ln in added:
        tg = link_target(ln)
        if tg and tg not in by_target:
            by_target[tg] = ln
    out, used = [], set()
    for ln in t:
        if ln in removed:
            rep = by_target.get(link_target(ln) or "")
            if rep and rep not in used:
                out.append(rep)
                used.add(rep)
            continue
        out.append(ln)
    for ln in added:
        if ln not in used and ln not in out:
            out.append(ln)
    return ("\n".join(out).rstrip() + "\n") if out else ""


def apply_memory(st: Stage, real: Path) -> dict:
    """Copy what the review changed into the real store (three-way, see the module docstring)."""
    after = snapshot(st.memory, md_only=True)
    applied, skipped, removed = [], [], []
    for rel in sorted(set(after) | set(st.base_memory)):
        if rel == INDEX:
            continue
        a, b = after.get(rel), st.base_memory.get(rel)
        if a == b:
            continue
        target = real / rel
        try:   # the live file, checked right before acting on it: the live agent may write at any moment
            r = sha(target) if target.is_file() else None
        except OSError:
            r = "unreadable"
        if r != b:
            skipped.append(rel)        # the live agent changed it meanwhile: its version stays
            continue
        if a is None:
            (real / rel).unlink(missing_ok=True)
            removed.append(rel)
        else:
            real.mkdir(parents=True, exist_ok=True)
            copy_file(st.memory / rel, real / rel)
            applied.append(rel)
    fixes = []
    base_idx, ours = read(st.base / "memory" / INDEX), read(st.memory / INDEX)
    if applied or removed or ours != base_idx:
        for _attempt in range(3):
            theirs = read(real / INDEX)
            merged = merge_index(base_idx, ours, theirs)
            if merged == theirs:
                break
            if read(real / INDEX) != theirs:
                continue               # changed while merging: merge with the newer index
            real.mkdir(parents=True, exist_ok=True)
            atomic_write(real / INDEX, merged, mode=0o644)
            break
        fixes = repair_index(real)
    return {"applied": applied, "skipped": skipped, "removed": removed, "index_fixes": fixes}


# ---------------------------------------------------------------------------------------------- skills

def skill_problems_of(skills: Path) -> dict[str, list[str]]:
    return {p.parent.name: new_skill.problems(str(p)) for p in sorted(Path(skills).glob("*/SKILL.md"))}


def _lines(path: Path) -> list[str]:
    return read(path).splitlines() if path.is_file() else []


def changed_lines(old: Path, new: Path) -> int:
    return sum(1 for ln in difflib.unified_diff(_lines(old), _lines(new), lineterm="", n=0)
               if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


def _restore(st: Stage, name: str) -> None:
    """Put one entry of the staged skills back to how it was staged."""
    cur, old = st.skills / name, st.base / "skills" / name
    if cur.is_dir() and not cur.is_symlink():
        shutil.rmtree(cur)
    elif cur.exists() or cur.is_symlink():
        cur.unlink()
    if old.is_dir():
        shutil.copytree(old, cur, symlinks=False)
    elif old.is_file():
        shutil.copy2(old, cur)


def settle_skills(st: Stage, big_lines: int = 40) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Validate the reviewer's skill edits in the stage. Returns (small changes, big changes) as
    (skill, line for the owner) and the rollbacks as lines; whatever was rolled back is back to its staged
    state and will not be applied."""
    small: list[tuple[str, str]] = []
    big: list[tuple[str, str]] = []
    rolled: list[str] = []
    if st.skills is None:
        return small, big, rolled
    after = snapshot(st.skills)
    changed = sorted(r for r in set(after) | set(st.base_skills) if after.get(r) != st.base_skills.get(r))
    for name in sorted({r.split("/")[0] for r in changed}):
        files = [r for r in changed if r.split("/")[0] == name]
        existed = (st.base / "skills" / name).exists()
        sdir = st.skills / name
        if any("/" not in r for r in files):
            _restore(st, name)
            rolled.append(f"`{name}`: only files inside a skill's directory may change, restored")
            continue
        if any(r in st.base_skills and r not in after for r in files):
            _restore(st, name)
            rolled.append(f"skill `{name}`: deleting a skill or its files is not allowed, restored")
            continue
        added = [p for p in new_skill.problems(str(sdir / "SKILL.md"))
                 if p not in st.skill_baseline.get(name, [])]
        if added:
            _restore(st, name)
            rolled.append(f"skill `{name}`: the edit failed validation ({'; '.join(added)[:160]}), rolled back")
            continue
        if any(SECRET_RE.search(read(st.skills / r)) for r in files):
            _restore(st, name)
            rolled.append(f"skill `{name}`: the edit looked like it contained a secret, rolled back")
            continue
        n = sum(changed_lines(st.base / "skills" / r, st.skills / r) for r in files)
        what = "new skill" if not existed else f"{n} line{'s' if n != 1 else ''} changed"
        if len(files) > 1:
            what += f" in {len(files)} files"
        (big if (not existed or n > big_lines) else small).append((name, f"skill `{name}`: {what}"))
    return small, big, rolled


def apply_skills(st: Stage, real: Path, stamp: str | None = None) -> dict:
    """Copy the settled skill edits into the real skills directory (three-way, per skill). The previous
    SKILL.md of every changed skill is kept in its .history/."""
    applied: list[str] = []
    skipped: list[str] = []
    if st.skills is None:
        return {"applied": applied, "skipped": skipped}
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    after = snapshot(st.skills)
    changed = sorted(r for r in set(after) | set(st.base_skills) if after.get(r) != st.base_skills.get(r))
    for name in sorted({r.split("/")[0] for r in changed if "/" in r}):
        files = [r for r in changed if "/" in r and r.split("/")[0] == name]
        now = snapshot(real / name)       # checked right before acting on this skill
        if any(now.get(r.split("/", 1)[1]) != st.base_skills.get(r) for r in files):
            skipped.append(name)         # changed in the real store meanwhile: the live version stays
            continue
        old_md = real / name / "SKILL.md"
        if f"{name}/SKILL.md" in files and old_md.is_file():
            hist = real / name / ".history"
            hist.mkdir(parents=True, exist_ok=True)
            n, dest = 0, hist / f"SKILL.{stamp}.review.md"
            while dest.exists():
                n += 1
                dest = hist / f"SKILL.{stamp}.review.{n}.md"
            shutil.copy2(old_md, dest)
        for r in files:
            if r in after:
                copy_file(st.skills / r, real / r)
            else:
                (real / r).unlink(missing_ok=True)
        applied.append(name)
    return {"applied": applied, "skipped": skipped}


def skills_index(skills: Path | None, limit: int = 140) -> str:
    lines = []
    for p in sorted(Path(skills).glob("*/SKILL.md")) if skills else []:
        fm, _ = new_skill.parse_frontmatter(read(p))
        lines.append(f"- {p.parent.name} — {((fm or {}).get('description') or '')[:limit]}")
    return "\n".join(lines) or "(no skills)"
