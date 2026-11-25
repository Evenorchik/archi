"""`archi update`: bring the installed code up to date with its upstream branch without losing the owner's
local changes, and never leave the live checkout half-merged.

  1. fetch the upstream branch;
  2. uncommitted local changes (tracked and untracked, not ignored) are stashed;
  3. fast-forward, or rebase local commits onto upstream;
  4. reapply the stash;
  5. reinstall requirements, then validate with the NEW code (`archi validate`);
  6. drop the stash, restart when idle.

On a conflict in 3 or 4, or a failed step 5, everything is put back exactly as it was (branch at the old
commit, local changes in the working tree) and the conflicting files are listed. Nothing is ever forced.
Exit codes: 0 updated or already current, 1 error, 2 conflict (rolled back), 3 validation failed (rolled
back).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from core.config import CODE_DIR

from .service import venv_python


class GitError(Exception):
    pass


class Conflict(Exception):
    def __init__(self, files: list[str], what: str):
        super().__init__(what)
        self.files = files
        self.what = what


class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._ident: list[str] | None = None

    def __call__(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", str(self.repo), *self.ident(), *args]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)}: timed out") from None
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {(res.stderr or res.stdout).strip()}")
        return res

    def out(self, *args: str) -> str:
        return self(*args).stdout.strip()

    def ident(self) -> list[str]:
        """Stash and rebase write commits; a fresh server often has no git identity configured."""
        if self._ident is None:
            res = subprocess.run(["git", "-C", str(self.repo), "config", "user.email"], capture_output=True,
                                 text=True)
            self._ident = [] if res.stdout.strip() else ["-c", "user.name=Archi", "-c", "user.email=archi@localhost"]
        return self._ident

    def unmerged(self) -> list[str]:
        res = self("diff", "--name-only", "--diff-filter=U", check=False)
        return [x for x in res.stdout.splitlines() if x.strip()]


def _stash_index(git: Git, sha: str) -> str | None:
    for i, line in enumerate(git("stash", "list", "--format=%H", check=False).stdout.splitlines()):
        if line.strip() == sha:
            return f"stash@{{{i}}}"
    return None


def rollback(git: Git, orig: str, stash: str | None, out=print) -> None:
    """Back to the state before the update: branch at `orig`, local changes (if any) reapplied."""
    for state, abort in (("rebase-merge", "rebase"), ("rebase-apply", "rebase")):
        if (git.repo / ".git" / state).exists():
            git(abort, "--abort", check=False)
    git("reset", "--hard", orig)
    if not stash:
        return
    # A failed or later rolled-back apply may already have restored the stash's untracked files; they are
    # still in the stash, and must go before it can be applied again.
    if git("rev-parse", "-q", "--verify", f"{stash}^3", check=False).returncode == 0:
        for rel in git.out("ls-tree", "-r", "--name-only", f"{stash}^3").splitlines():
            p = git.repo / rel
            if p.is_symlink() or p.is_file():
                p.unlink()
    res = git("stash", "apply", stash, check=False)
    if res.returncode != 0:
        out(f"WARNING: could not reapply your local changes automatically; they are safe in the stash "
            f"({stash[:10]}): run `git stash list` and `git stash apply` in {git.repo}")
        return
    ref = _stash_index(git, stash)
    if ref:
        git("stash", "drop", ref, check=False)


def update(repo: Path = CODE_DIR, *, remote: str = "origin", branch: str | None = None, restart: bool = True,
           validate: bool = True, reinstall: bool = True, out=print, runner=subprocess.run) -> int:
    git = Git(repo)
    if not (Path(repo) / ".git").exists():
        out(f"{repo} is not a git checkout; reinstall with install.sh")
        return 1
    for state in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if (Path(repo) / ".git" / state).exists():
            out("A merge or rebase is in progress in the checkout; finish or abort it first (git status).")
            return 1
    try:
        branch = branch or git.out("symbolic-ref", "--short", "-q", "HEAD")
    except GitError:
        out("The checkout is not on a branch (detached HEAD); check out your branch first.")
        return 1
    if remote not in git.out("remote").split():
        out(f"There is no remote {remote!r}; add it: git -C {repo} remote add {remote} <repository url>")
        return 1
    out(f"Fetching {remote}/{branch} …")
    try:
        git("fetch", "--quiet", remote, branch, timeout=180)
    except GitError as e:
        out(f"Fetch failed: {e}")
        return 1
    up = git.out("rev-parse", "FETCH_HEAD")
    orig = git.out("rev-parse", "HEAD")
    behind = int(git.out("rev-list", "--count", f"{orig}..{up}"))
    ahead = int(git.out("rev-list", "--count", f"{up}..{orig}"))
    if behind == 0:
        out(f"Already up to date ({orig[:7]})" + (f", {ahead} local commit(s) on top" if ahead else "") + ".")
        return 0

    stash = None
    if git.out("status", "--porcelain", "--untracked-files=all"):
        git("stash", "push", "--include-untracked", "-m", f"archi update {time.strftime('%Y-%m-%d %H:%M')}")
        stash = git.out("rev-parse", "stash@{0}")
        out("Local changes stashed.")
    try:
        if ahead == 0:
            res = git("merge", "--ff-only", up, check=False)
            if res.returncode != 0:
                raise Conflict(git.unmerged(), f"fast-forward failed: {res.stderr.strip()}")
        else:
            res = git("rebase", up, check=False)
            if res.returncode != 0:
                raise Conflict(git.unmerged(), f"your {ahead} local commit(s) conflict with the update")
        if stash:
            res = git("stash", "apply", stash, check=False)
            if res.returncode != 0:
                raise Conflict(git.unmerged(), "your uncommitted local changes conflict with the update")
    except (Conflict, GitError) as e:
        files = getattr(e, "files", [])
        rollback(git, orig, stash, out=out)
        out(f"Update stopped: {getattr(e, 'what', str(e))}.")
        for f in files:
            out(f"  conflict: {f}")
        out(f"Nothing changed: the code is back at {orig[:7]}" + (" with your local changes." if stash else "."))
        out("To merge by hand: git stash push -u; git merge --ff-only FETCH_HEAD (or git rebase FETCH_HEAD); "
            "git stash pop; resolve the <<<<<<< markers, git add; then archi validate.")
        return 2

    new = git.out("rev-parse", "HEAD")
    out(f"Updated {orig[:7]} -> {new[:7]} ({behind} new commit(s))" + (", local changes reapplied." if stash else "."))
    for line in git.out("log", "--oneline", "--no-decorate", f"{orig}..{up}").splitlines()[:15]:
        out(f"  {line}")
    python = venv_python(Path(repo))
    if reinstall and Path(python).parent.parent.name == ".venv":
        out("Installing requirements …")
        res = runner([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
                      str(Path(repo) / "requirements.txt")], capture_output=True, text=True, timeout=900)
        if res.returncode != 0:
            rollback(git, orig, stash, out=out)
            out(f"pip failed, update rolled back to {orig[:7]}:\n{(res.stderr or res.stdout)[-1500:]}")
            return 1
    if validate:
        res = runner([sys.executable, str(Path(repo) / "archi"), "validate"], capture_output=True, text=True,
                     timeout=300)
        if res.returncode != 0:
            rollback(git, orig, stash, out=out)
            out(f"The updated code does not validate, rolled back to {orig[:7]}:\n{res.stdout[-2000:]}")
            return 3
        out("Validated.")
    if stash:
        ref = _stash_index(git, stash)
        if ref:
            git("stash", "drop", ref, check=False)
    if restart:
        res = runner([sys.executable, str(Path(repo) / "archi"), "restart", "--when-idle"], timeout=None)
        return 0 if res.returncode == 0 else 1
    return 0
