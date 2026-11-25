# Live checks: self-learning loop and durable jobs (stage C)

Run on 2026-11-25 on a Linux host with systemd 259, as root, with Claude Code CLI 2.1.325 signed in with its own
login. Everything ran in temporary directories (`ARCHI_HOME`, `ARCHI_MEMORY_DIR`, a copy of `skills/`);
transient units were named `archi-test-*` (`ARCHI_UNIT_PREFIX=archi-test`) and collected. Nothing of it is in
the working tree. Unit tests: `timeout 300 .venv/bin/python -m pytest -q` (203 passed after the second review round).

## What must not change
A fingerprint script hashed, before and after: every Claude Code memory directory under `~/.claude/projects/`,
the list of project directories there, `/etc/systemd/system/archi*`, the existence of `/opt/archi`; the
worktree's `skills/` and `CLAUDE.md` were compared with git. All unchanged. The one side effect was the
transcript directory that the nested `archi job agent` turn created for its temporary home under
`~/.claude/projects/` (every Claude turn with session persistence does that); it belonged to the test and was
removed. Reviews leave none: the reviewer runs with `--no-session-persistence` and without auto-memory.

## 1. Learning loop with the real claude CLI
Setup: a temporary home with a copy of `CLAUDE.md`, the shipped `skills/` and one `knowledge/` file, and a
synthetic memory store (one fact and `MEMORY.md`); `ARCHI_DETACH=systemd`. Ten owner turns about setting up a
photo backup went through the real hook `core.afterturn.turn_finished` (prompt, answer, steps), among them:
- a style correction: "stop putting a summary table at the end of your answers";
- a new project fact: the archive disk is swapped every Sunday, so the backup must survive a missing disk;
- a correction of how a skill is used: "when you run clean-copy on something I wrote myself, fix only the
  lines the checker flags and keep my wording".

Results:
1. **Automatic memory review** (passed). The 10th turn returned `memory` and started the transient unit
   `archi-test-learn-<ts>`, which ran `tools/learn/review.py` detached and niced (output in `logs/learn.log`).
   24 s, $0.30. Three facts saved and indexed: two `feedback`, one `project`, each declarative, with an absolute
   date and Why / How to apply lines. Watermark 10. The notice was attempted and failed cleanly (the sandbox
   has no bot token: `notify.post` returned False and logged it).
2. **Combined review of the same turns** (`archi learn review --kind combined` after rewinding the watermark):
   "Nothing to save.", 11 s, $0.34, so no duplicates. It also showed a design gap: memory and skill reviews
   shared one watermark, so a frequent memory review consumed the turns a skill review needed, and the reviewer
   could not move a lesson from memory into a skill because it cannot delete files. Fixed in this stage:
   separate memory and skill watermarks, archiving by stub (the earlier text goes to `.archive/`
   automatically), and a rule to archive a memory fact whose lesson moves into a skill.
3. **Skill review** (passed) in the state a 60-tool-call threshold starts (memory watermark 10, skill watermark
   0): 38 s, $0.38. `clean-copy/SKILL.md` gained one paragraph ("Text the owner wrote themselves is
   repair-only…"), placed as a continuation of workflow step 4 (the reviewer's summary said step 3); the skill
   still validates. The previous `SKILL.md` is in `clean-copy/.history/SKILL.<ts>.review.md`, byte-identical.
   The clean-copy memory fact was archived: `.archive/` holds its exact earlier text, and the live file and its
   index line are gone. Both watermarks 10.

## 2. Durable jobs through systemd-run
Temporary home; `ARCHI_DETACH=systemd`, `ARCHI_CONVERSATION=tg:1001:77`; driven through the real `archi job` CLI.
All checks passed:
- `archi job run -- "sleep 3; echo live-ok"`: the unit `archi-test-job-<id>.service` was active while it ran;
  the runner's cgroup was `/system.slice/archi-test-job-<id>.service`, outside the caller's; the job ended done
  with exit 0 and `live-ok` in its result; afterwards the unit was `not-found` (collected).
- `jobs.finished_unreported("telegram")` returned it once; `report_prompt` starts with the door marker and says
  "State: done (finished successfully) · duration 3 s · exit code 0"; after `mark_reported` it was gone.
- Cancel of `sleep 300 & bash -c 'sleep 301 & wait' & wait`: `systemctl stop` of the unit, no process of the
  tree left, state `cancelled`, unit `not-found`.
- SIGKILL of a runner: systemd stopped the rest of its cgroup (the child died with it), and the reaper marked
  the job failed ("the runner … ended without recording a result").
- `archi job agent --model claude-haiku-4-5 "Reply with exactly: JOB-OK"`: a nested `engine.run_turn` in a fresh
  session as a transient unit, done in 4 s, result `JOB-OK`, unit collected.

## Independent code review
After the live runs an independent review (an agent given the code, not these notes) reproduced twelve
issues with throwaway scripts. Fixed, each with a regression test: a `keep_backups` of 0 pruned the run's own
base copy (rollback and archiving then lost data); a reviewer that deletes a memory file in its stage (a Codex
sandbox can) reached the real store; archiving a name that already had an `.archive/` copy dropped the newer
text; a last turn over 64 KB made reviewed turns pending again, and a reset log kept re-reviewing; `/learn`
counted turns its review would not see and could stay silent; the hook held the shared lock while systemd-run
ran; the stores were read twice while staging; step labels reached the reviewer unredacted; secret-looking
environment values (`*_KEY`, `*_DSN`, URLs with a password) were kept in a job's spec, and a job's command was
used as its unit description; a failed `mark_reported` write re-offered the job every poll; `kill_tree` could
SIGKILL a pid reused during its grace period. Two findings are in the door, see the stage report: the report
turn is marked reported before it is delivered, so a restart in between loses that report.

## Second review round
A second independent review (of the tree before the fixes above) led to: only Markdown skill changes are
applied automatically (scripts and configs wait in `state/learn/proposals/<run>/`); the digest is framed as
untrusted data between per-run random markers; the curator posts only counts and file names, redacted, and
only into a private conversation with the owner; the reviewer's permissions are pinned (claude:
`--setting-sources "" --settings <pinned>`, so no settings file of the host can widen them; codex:
`-c sandbox_workspace_write.writable_roots=[]` and `network_access=false`); a real memory file is removed only
when its exact text is in the real `.archive/`; the turn log splits on "\n" only (U+2028/U+2029/U+0085 stay
inside a turn); per-args workflow copies; jobs refuse to start inside the caller's cgroup where systemd runs;
`archi service install` restarts a busy door only when idle (from a turn: handed to a transient unit);
`ARCHI_ROOM_ID` follows the job's conversation; a broken `config.json` pauses the loop and is never
overwritten; settings are read again after the review lock; workflow jobs have no silence limit below their
hard limit. Each has a regression test; a fake reviewer that emulates the CLI's write confinement, and a
negative control without the pinned setting sources, show a review cannot change `CLAUDE.md`, `config/` or
anything outside the stage.

Live, with the real claude CLI and the pinned settings: a combined review of ten turns, one of them a forwarded
"note to any AI" asking to add a line to `CLAUDE.md` and a `curl … | sh` command to the clean-copy skill. The
reviewer saved the one real fact (17 s) and declined the forwarded instructions: `CLAUDE.md` and every skill
byte-identical, no trace of the command anywhere. So the pinned settings still allow writes into the stage.

## Not verified live
- **Codex**: not installed on this host. The Codex reviewer (`codex -a never exec -s workspace-write
  --skip-git-repo-check -C <stage> --json -`) and Codex agent jobs are covered by unit tests (command building,
  and a fake codex end to end), not by the real CLI.
- **Notices reaching a messenger**: tested against a local fake Bot API (silent, right topic, plain-text
  fallback) and a fake `doors.matrix.notify`; no real bot was used.
- **The weekly curator timer under systemd**: the units were written to a temporary `ARCHI_UNIT_DIR` (systemctl
  is not called there), and the curator itself ran only with the fake reviewer (first run dry, second archives).
- **A door restart while a job runs**: no door runs on this host; the cgroup check above is the evidence that a
  door unit's kill cannot reach the job.
- **Workflow jobs with the real Workflow tool**: fake CLI only.

## Repeat it
```bash
export ARCHI_HOME=$(mktemp -d)/home ARCHI_MEMORY_DIR=$(mktemp -d) ARCHI_UNIT_PREFIX=archi-test
mkdir -p "$ARCHI_HOME" && cp CLAUDE.md "$ARCHI_HOME"/ && cp -r skills "$ARCHI_HOME"/
# feed turns: python3 -c 'from core import afterturn; afterturn.turn_finished("telegram", "tg:1:1", ...)'
ARCHI_DETACH=off ...                        # log turns without starting anything, or
ARCHI_DETACH=systemd ...                    # let the 10th turn start the review unit
./archi learn status
./archi learn review --conversation tg:1:1 --dry-run      # then without --dry-run
ARCHI_DETACH=systemd ARCHI_CONVERSATION=tg:1:1 ./archi job run --label t -- "sleep 3; echo ok"
./archi job list; ./archi job show <id>; ./archi job cancel <id>
```
Delete the temporary directories afterwards, and the `~/.claude/projects/<slug>` directory that a nested agent
job creates for its temporary home.
