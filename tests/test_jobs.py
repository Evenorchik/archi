"""Durable jobs: real runners launched through the setsid fallback (no systemd units in tests), reaping,
cancelling whole process trees, the door contract, agent and workflow jobs, the CLI, detach itself."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import pid_alive
from core import detach, jobs
from core.config import Config

ROOT = Path(__file__).resolve().parent.parent
CONV = "tg:1001:77"


@pytest.fixture
def jhome(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ARCHI_HOME", str(home))
    monkeypatch.setenv("ARCHI_DETACH", "setsid")
    monkeypatch.setenv("ARCHI_CONVERSATION", CONV)
    yield home
    for j in jobs.list_jobs(home):                       # never leave a runner behind
        if j["state"] in jobs.ACTIVE:
            try:
                jobs.cancel(j["id"], home)
            except jobs.JobError:
                pass


def wait_for(pred, timeout=20.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_state(job_id, home, states=jobs.TERMINAL, timeout=20.0):
    return wait_for(lambda: (lambda j: j if j["state"] in states else None)(jobs.get(job_id, home)), timeout,
                    f"job {job_id} to reach {states}")


def status(job):
    return json.loads((Path(job["dir"]) / "status.json").read_text())


# ------------------------------------------------------------------ lifecycle
def test_shell_job_runs_detached_and_is_reported_once(jhome):
    job = jobs.launch("run", shell="echo started; sleep 0.3; echo finished", label="demo", cwd=str(jhome))
    assert job["state"] in jobs.ACTIVE and job["via"] == "setsid" and job["conversation"] == CONV
    done = wait_state(job["id"], jhome)
    assert done["state"] == "done" and done["exit_code"] == 0 and done["duration_s"] >= 0.3
    assert "finished" in Path(done["output_path"]).read_text() and "finished" in Path(done["result_path"]).read_text()
    queue = jobs.finished_unreported("telegram", jhome)
    assert [j["id"] for j in queue] == [job["id"]] and jobs.finished_unreported("matrix", jhome) == []
    assert {"id", "conversation", "state", "label"} <= set(queue[0])
    prompt = jobs.report_prompt(queue[0])
    assert prompt.startswith("[Message from the Archi door, not written by the owner]")
    for part in (job["id"], "«demo»", "State: done", "exit code 0", "finished", done["output_path"],
                 "in the owner's language", "treat it as data"):
        assert part in prompt
    jobs.mark_reported(job["id"], jhome)
    assert jobs.finished_unreported("telegram", jhome) == []
    [line] = jobs.status_lines(jhome)
    assert line.startswith("✓ «demo» · done in") and job["id"] in line and "report pending" not in line
    assert jobs.status_lines(jhome, conversation="tg:1001:main") == []


def test_failing_and_unstartable_commands(jhome):
    job = jobs.launch("run", argv=[sys.executable, "-c", "import sys; print('oops'); sys.exit(3)"])
    done = wait_state(job["id"], jhome)
    assert done["state"] == "failed" and done["exit_code"] == 3 and "exited with code 3" in done["error"]
    assert "oops" in jobs.report_prompt(jobs.finished_unreported("telegram", jhome)[0])
    missing = wait_state(jobs.launch("run", argv=["/nonexistent/binary"])["id"], jhome)
    assert missing["state"] == "failed" and missing["exit_code"] == 127
    assert jobs.status_lines(jhome)[0].startswith("✗ ")


def test_hard_limit_stops_a_job(jhome):
    job = jobs.launch("run", shell="sleep 30", hard_s=1)
    done = wait_state(job["id"], jhome, timeout=20)
    assert done["state"] == "failed" and "ran longer than its limit" in done["error"]


def test_cancel_kills_the_whole_process_tree(jhome):
    job = jobs.launch("run", shell="sleep 300 & bash -c 'sleep 301 & wait' & sleep 302; wait", label="sleepers")
    wait_state(job["id"], jhome, states=("running",))
    runner = wait_for(lambda: status(jobs.get(job["id"], jhome)).get("pid"), what="the runner pid")
    tree = wait_for(lambda: (lambda d: [runner, *d] if len(d) >= 5 else None)(detach.descendants(runner)),
                    what="the command's process tree")
    assert os.getsid(runner) != os.getsid(0)                         # its own session
    ppid = int(Path(f"/proc/{runner}/stat").read_text().rsplit(")", 1)[1].split()[1])
    assert ppid != os.getpid()                                       # not our child: double-forked away
    assert "cancelled job" in jobs.cancel(job["id"], jhome, reason="not needed any more")
    wait_for(lambda: not any(pid_alive(p) for p in tree), timeout=15, what="the tree to die")
    final = jobs.get(job["id"], jhome)
    assert final["state"] == "cancelled" and "not needed any more" in final["error"]
    with pytest.raises(jobs.JobError, match="not running"):
        jobs.cancel(job["id"], jhome)
    assert jobs.status_lines(jhome)[0].startswith("■ «sleepers» · cancelled after")


def test_reaper_fails_jobs_whose_runner_died(jhome, tmp_path):
    job = jobs.launch("run", shell="sleep 300", label="victim")
    wait_state(job["id"], jhome, states=("running",))
    st = wait_for(lambda: (lambda s: s if s.get("child_pid") else None)(status(jobs.get(job["id"], jhome))),
                  what="the child pid")
    os.kill(st["pid"], signal.SIGKILL)
    wait_for(lambda: not pid_alive(st["pid"]), what="the runner to die")
    assert jobs.reap(jhome) == [job["id"]]
    dead = jobs.get(job["id"], jhome)
    assert dead["state"] == "failed" and "ended without recording a result" in dead["error"] and dead["reaped"]
    wait_for(lambda: not pid_alive(st["child_pid"]), what="the orphaned command to be stopped")
    assert [j["id"] for j in jobs.finished_unreported("telegram", jhome)] == [job["id"]]

    # Recorded states that cannot be trusted, in a home of their own.
    other = tmp_path / "other"
    me = os.getpid()
    cases = {
        "20200101-000000-aaaaaa": {"state": "queued", "created_at": time.time() - 999},     # runner never came
        "20200101-000000-bbbbbb": {"state": "running", "pid": me, "ticks": detach.start_ticks(me) + 1},  # pid reused
        "20200101-000000-cccccc": {"state": "running", "pid": me, "ticks": detach.start_ticks(me), "boot": "old"},
        "20200101-000000-dddddd": {"state": "running", "pid": me, "ticks": detach.start_ticks(me),
                                   "boot": detach.boot_id()},                                  # really alive
        "20200101-000000-eeeeee": {"state": "queued", "created_at": time.time()},             # just launched
    }
    for jid, st in cases.items():
        d = jobs.jobs_dir(other) / jid
        d.mkdir(parents=True)
        (d / "spec.json").write_text(json.dumps({"id": jid, "kind": "run", "label": jid, "conversation": CONV,
                                                 "created_at": time.time() - 999}))
        (d / "status.json").write_text(json.dumps(st))
    assert sorted(jobs.reap(other)) == ["20200101-000000-aaaaaa", "20200101-000000-bbbbbb", "20200101-000000-cccccc"]
    assert jobs.get("20200101-000000-dddddd", other)["state"] == "running"


def test_active_job_limit_and_force(jhome):
    Config(jhome).set("JOBS_MAX_ACTIVE", "1")
    first = jobs.launch("run", shell="sleep 30", label="one")
    wait_state(first["id"], jhome, states=("running",))
    with pytest.raises(jobs.JobError, match="the limit is 1"):
        jobs.launch("run", shell="true")
    forced = jobs.launch("run", shell="true", force=True)
    assert wait_state(forced["id"], jhome)["state"] == "done"
    jobs.cancel(first["id"], jhome)
    assert wait_state(jobs.launch("run", shell="true")["id"], jhome)["state"] == "done"    # the slot is free again


def test_a_job_needs_a_conversation_and_reports_to_its_door(jhome, monkeypatch):
    with pytest.raises(jobs.JobError, match="working directory does not exist"):
        jobs.launch("run", shell="true", cwd=str(jhome / "missing"))
    monkeypatch.delenv("ARCHI_CONVERSATION")
    with pytest.raises(jobs.JobError, match="conversation"):
        jobs.launch("run", shell="true")
    job = jobs.launch("run", shell="echo $ARCHI_CONVERSATION $ARCHI_DOOR", conversation="mx:!room:example.org")
    done = wait_state(job["id"], jhome)
    assert "mx:!room:example.org matrix" in Path(done["output_path"]).read_text()
    assert [j["id"] for j in jobs.finished_unreported("matrix", jhome)] == [job["id"]]
    assert jobs.finished_unreported("telegram", jhome) == []
    tg = wait_state(jobs.launch("run", shell="echo $ARCHI_CHAT_ID/$ARCHI_THREAD_ID", conversation="tg:42:7")["id"], jhome)
    assert "42/7" in Path(tg["output_path"]).read_text()
    jobs.mark_reported(tg["id"], jhome)
    monkeypatch.setenv("ARCHI_DETACH", "off")
    with pytest.raises(jobs.JobError, match="could not start"):
        jobs.launch("run", shell="true", conversation=CONV)
    assert jobs.finished_unreported("telegram", jhome) == []        # a job that never started is not "reported"


def test_job_environment_never_carries_secrets():
    env = jobs.job_env({"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OPENAI_API_KEY": "x", "MY_PASSWORD": "x",
                        "GITHUB_TOKEN": "x", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "LANG": "C.UTF-8",
                        "STRIPE_KEY": "x", "GEMINI_KEY": "x", "MY_PASSPHRASE": "x", "SENTRY_DSN": "x",
                        "DATABASE_URL": "x", "BACKUP_TARGET": "postgres://app:" + "pw42@localhost:5432/x",
                        "INNOCENT_NAME": "sk-" + "d" * 30, "SSH_KEY_PATH": "/root/.ssh/id",
                        "ARCHI_CONVERSATION": CONV})
    assert env == {"PATH": "/bin", "LANG": "C.UTF-8", "SSH_KEY_PATH": "/root/.ssh/id", "ARCHI_CONVERSATION": CONV}


def test_the_unit_description_never_shows_the_command(jhome, monkeypatch):
    seen = {}
    monkeypatch.setattr(detach, "spawn", lambda argv, **kw: seen.update(kw) or detach.Spawned(
        True, "setsid", pid=1, warning="systemd-run failed: no bus; started as a detached process instead"))
    job = jobs.launch("run", shell="curl -u admin:" + "hunter2hunter2 https://example.org")
    assert seen["description"] == f"Archi job {job['id']} (run)" and "hunter2" not in seen["description"]
    assert "systemd-run failed" in job["via_warning"]


def test_a_failed_report_record_does_not_repeat_the_report(jhome, monkeypatch):
    done = wait_state(jobs.launch("run", shell="true")["id"], jhome)
    assert [j["id"] for j in jobs.finished_unreported("telegram", jhome)] == [done["id"]]

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    real_update = jobs._update
    monkeypatch.setattr(jobs, "_update", disk_full)
    jobs.mark_reported(done["id"], jhome)                         # logs, never raises
    monkeypatch.setattr(jobs, "_update", real_update)
    assert not jobs.get(done["id"], jhome).get("reported_at")     # the write really failed
    assert jobs.finished_unreported("telegram", jhome) == []      # and it is not offered again by this process


# ------------------------------------------------------------------ agent and workflow jobs
def test_agent_job_is_a_fresh_turn_in_the_home(jhome, fake_cli):
    job = jobs.launch("agent", prompt="Summarize the logs", label="summary")
    done = wait_state(job["id"], jhome, timeout=30)
    assert done["state"] == "done" and done["exit_code"] == 0 and done["session_id"] == "sess-new"
    call = [json.loads(x) for x in fake_cli.read_text().splitlines()][-1]
    assert "--resume" not in call["argv"] and call["cwd"] == str(jhome) and "Summarize the logs" in call["stdin"]
    system = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "durable background job" in system and job["id"] in system
    assert call["env"]["ARCHI_CONVERSATION"] == CONV and call["env"]["ARCHI_JOB_ID"] == job["id"]
    assert "ANSWER stdin=" in Path(done["result_path"]).read_text()
    assert "· Listing files" in Path(done["output_path"]).read_text()
    prompt = jobs.report_prompt(jobs.finished_unreported("telegram", jhome)[0])
    assert "a background agent task" in prompt and "its final answer" in prompt


def test_failed_agent_job(jhome, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    done = wait_state(jobs.launch("agent", prompt="do it")["id"], jhome, timeout=30)
    assert done["state"] == "failed" and "boom" in done["error"]


def test_workflow_job_bakes_its_args_into_the_script(jhome, fake_cli, tmp_path):
    from tools import workflow_job
    script = tmp_path / "wf.js"
    script.write_text("export const meta = { name: 'x', nested: { a: 1 } };\nexport default async () => args.who;\n")
    (tmp_path / "args.json").write_text('{"who": "owner"}')
    assert workflow_job.main(["launch", "--script", str(script), "--args-file", str(tmp_path / "args.json"),
                              "--label", "wf"]) == 0
    [job] = jobs.list_jobs(jhome)
    [copy] = sorted(tmp_path.glob("wf.wfjob-*.js"))
    wrapped = copy.read_text()
    assert wrapped.index('globalThis.args = {"who": "owner"};') > wrapped.index("nested: { a: 1 } };")
    spec = json.loads((Path(job["dir"]) / "spec.json").read_text())
    assert spec["kind"] == "workflow" and spec["provider"] == "claude"
    assert spec["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert f"scriptPath = {copy}" in spec["prompt"]
    assert wait_state(job["id"], jhome, timeout=30)["state"] == "done"
    call = [json.loads(x) for x in fake_cli.read_text().splitlines()][-1]
    assert "--append-system-prompt" not in call["argv"] and "Workflow" in call["argv"][call["argv"].index("--allowedTools") + 1]
    assert "headless workflow runner" in call["stdin"]
    with pytest.raises(ValueError):
        workflow_job.inject_args("no meta here", "{}")


# ------------------------------------------------------------------ the command line
def test_archi_job_cli(jhome):
    def archi(*args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "archi"), "job", *args], capture_output=True, text=True,
                              timeout=60, env=env or dict(os.environ))

    res = archi("run", "--label", "cli", "--", "echo", "from-cli")
    assert res.returncode == 0 and "started: «cli»" in res.stdout, res.stderr
    [job] = jobs.list_jobs(jhome)
    assert job["argv"] == ["echo", "from-cli"]
    wait_state(job["id"], jhome)
    show = archi("show", job["id"][:18])
    assert show.returncode == 0 and "done" in show.stdout and "from-cli" in show.stdout
    assert "«cli»" in archi("list").stdout
    one = archi("run", "--", "echo one && echo two")
    assert one.returncode == 0
    shell_job = [j for j in jobs.list_jobs(jhome) if j["id"] != job["id"]][0]
    assert shell_job["shell"] == "echo one && echo two"
    env = {k: v for k, v in os.environ.items() if k != "ARCHI_CONVERSATION"}
    bad = archi("run", "--", "true", env=env)
    assert bad.returncode == 1 and "not started" in bad.stderr and "conversation" in bad.stderr
    assert archi("cancel", "nope").returncode == 1


# ------------------------------------------------------------------ detach
def test_spawn_modes(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    code = f"import os, time; open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{os.getsid(0)}} {{os.getppid()}}')"
    monkeypatch.setenv("ARCHI_DETACH", "setsid")
    res = detach.spawn([sys.executable, "-c", code], name="x", env={"PATH": os.environ["PATH"]},
                       log_path=tmp_path / "log")
    assert res.ok and res.via == "setsid" and res.pid > 0
    pid, sid, ppid = map(int, wait_for(lambda: marker.exists() and marker.read_text().strip(), what="marker").split())
    assert pid == res.pid and sid == pid and ppid != os.getpid()
    assert not detach.spawn(["/nonexistent"], name="x", env={"PATH": "/bin"}).ok
    monkeypatch.setenv("ARCHI_DETACH", "off")
    assert detach.spawn(["true"], name="x").via == "off"
    monkeypatch.setenv("ARCHI_DETACH", "systemd")
    monkeypatch.setattr(detach.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus"))
    res = detach.spawn(["true"], name="x")
    assert not res.ok and "Failed to connect to bus" in res.error
    assert detach.kill_tree(os.getpid()) == [] and detach.kill_tree(1) == []


def test_kill_tree_never_signals_a_reused_pid(monkeypatch):
    # A tree that ignores SIGTERM, and pids that "belong to other processes" after the grace period: nothing
    # may be SIGKILLed then (a recycled pid is somebody else's process).
    proc = subprocess.Popen(["bash", "-c", "trap '' TERM; sleep 30 & wait"], start_new_session=True)
    try:
        wait_for(lambda: detach.descendants(proc.pid), what="the sleep child")
        child = detach.descendants(proc.pid)[0]
        real, real_sleep, swapped = detach.start_ticks, time.sleep, {"on": False}
        monkeypatch.setattr(detach, "start_ticks", lambda p: (real(p) + 1) if swapped["on"] and real(p) else real(p))
        monkeypatch.setattr(detach.time, "sleep", lambda s: swapped.update(on=True))
        detach.kill_tree(proc.pid, grace=0.2)
        monkeypatch.setattr(detach.time, "sleep", real_sleep)
        monkeypatch.setattr(detach, "start_ticks", real)
        assert pid_alive(proc.pid) and pid_alive(child)
        assert proc.pid in detach.kill_tree(proc.pid, grace=0.2)          # identities intact: SIGKILLed
        proc.wait(timeout=10)
        wait_for(lambda: not pid_alive(child), what="the child to die")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_systemd_run_command(monkeypatch):
    monkeypatch.setenv("ARCHI_UNIT_PREFIX", "archi-test")
    name = detach.unit_name("job", "20261125-140512-ab12cd")
    assert name == "archi-test-job-20261125-140512-ab12cd"
    argv = ["/usr/bin/python3", "/opt/archi/archi", "job", "_run", "X"]
    cmd = detach.systemd_run_command(argv, name=name, description="Archi job X", env={"ARCHI_HOME": "/opt/archi"},
                                     cwd="/opt/archi", log_path="/opt/archi/state/jobs/X/runner.log", nice=10)
    assert cmd[:5] == ["systemd-run", "--unit", name, "--collect", "--quiet"] and cmd[-5:] == argv
    for part in ("Nice=10", "ARCHI_HOME=/opt/archi", "StandardOutput=append:/opt/archi/state/jobs/X/runner.log"):
        assert part in cmd
    assert cmd[cmd.index("--working-directory") + 1] == "/opt/archi"



# ------------------------------------------------------------------ second review round
def test_a_job_for_another_conversation_gets_that_conversations_variables(jhome, monkeypatch):
    monkeypatch.setenv("ARCHI_CONVERSATION", "mx:!here:example.org")    # launched from a Matrix room …
    monkeypatch.setenv("ARCHI_ROOM_ID", "!here:example.org")
    monkeypatch.setenv("ARCHI_DOOR", "matrix")
    job = jobs.launch("run", shell="echo room=$ARCHI_ROOM_ID chat=$ARCHI_CHAT_ID door=$ARCHI_DOOR",
                      conversation="tg:42:7")                            # … for a Telegram topic
    out = Path(wait_state(job["id"], jhome)["output_path"]).read_text()
    assert "room= chat=42 door=telegram" in out
    job = jobs.launch("run", shell="echo room=$ARCHI_ROOM_ID door=$ARCHI_DOOR", conversation="mx:!there:example.org")
    assert "room=!there:example.org door=matrix" in Path(wait_state(job["id"], jhome)["output_path"]).read_text()


def test_jobs_refuse_a_fallback_that_a_door_restart_would_kill(jhome, monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHI_DETACH", "auto")
    monkeypatch.setattr(detach, "systemd_running", lambda: True)
    monkeypatch.setattr(detach, "systemd_usable", lambda: True)
    real_run = subprocess.run
    monkeypatch.setattr(detach.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "Failed to connect to bus") if cmd[0] == "systemd-run" else real_run(cmd, **kw))
    with pytest.raises(jobs.JobError, match="Not started.*ARCHI_DETACH=setsid"):
        jobs.launch("run", shell="true")
    [job] = jobs.list_jobs(jhome)
    assert job["state"] == "failed" and job.get("reported_at")      # the caller was told; no later report
    # Reviews are cheap to lose: they still start, with a warning.
    marker = tmp_path / "ran"
    res = detach.spawn([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"], name="r",
                       env={"PATH": os.environ["PATH"]})
    assert res.ok and res.via == "setsid" and "Failed to connect to bus" in res.warning
    wait_for(marker.exists, what="the fallback process")
    # Without systemd at all the fallback is all there is, for jobs too.
    monkeypatch.setattr(detach, "systemd_running", lambda: False)
    monkeypatch.setattr(detach, "systemd_usable", lambda: False)
    assert wait_state(jobs.launch("run", shell="true")["id"], jhome)["state"] == "done"


def test_workflow_jobs_wait_through_long_silences(jhome, monkeypatch):
    seen = {}

    async def fake_run_turn(provider, prompt, session_id, **kw):
        seen[kw["system_prompt"] == ""] = kw["limits"]
        from core.engine import TurnResult
        return TurnResult("ok", "s1", True)

    monkeypatch.setattr("core.engine.run_turn", fake_run_turn)
    d = jobs.jobs_dir(jhome) / "x"
    d.mkdir(parents=True)
    (d / "status.json").write_text("{}")
    for kind in ("workflow", "agent"):
        spec = {"id": "x", "kind": kind, "label": "l", "conversation": CONV, "prompt": "p", "provider": "claude",
                "hard_s": 7 * 3600, "cwd": str(jhome)}
        assert jobs._run_agent(d, spec, jhome)["state"] == "done"
    assert seen[True].silence_s == seen[True].hard_s == 7 * 3600          # workflow: silence is fine
    assert seen[False].silence_s < seen[False].hard_s                       # agent: the usual silence limit


def test_workflow_args_copies_are_per_args(tmp_path):
    from tools import workflow_job
    script = tmp_path / "wf.js"
    script.write_text("export const meta = { name: 'x' };\n")
    a1, a2 = json.dumps({"who": "a"}), json.dumps({"who": "b"})
    p1 = workflow_job.args_copy(script, workflow_job.inject_args(script.read_text(), a1), a1)
    p2 = workflow_job.args_copy(script, workflow_job.inject_args(script.read_text(), a2), a2)
    again = workflow_job.args_copy(script, workflow_job.inject_args(script.read_text(), a1), a1)
    assert p1 != p2 and p1 == again and '"who": "a"' in p1.read_text() and '"who": "b"' in p2.read_text()
    assert not list(tmp_path.glob(".*.tmp"))
