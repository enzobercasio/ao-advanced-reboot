#!/usr/bin/env python3
"""Emulate the Automation Orchestrator workflow on a laptop.

The canvas runs the While loop, Switch, the three parallel tier jobs and the
Converge. This script does the same in the same order so the data they pass
can be checked: batches per round, quorum, a one-failure retry, a two-failure
stop with a restart, a host that never returns, and a fully blocked fleet.
The three tier jobs run at the same time, as they do on the canvas.
"""

import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLAYBOOKS = Path(__file__).resolve().parents[1] / "playbooks"
TIERS = ["web", "app", "db"]


def run(playbook, state_dir, extra=None):
    cmd = ["ansible-playbook", str(PLAYBOOKS / playbook), "-i", "localhost,", "-c", "local",
           "-e", f"reboot_state_dir={state_dir}"]
    if extra:
        cmd += ["-e", json.dumps(extra)]
    r = subprocess.run(cmd, text=True, capture_output=True)
    return r.returncode, r.stdout + r.stderr


def must(playbook, state_dir, extra=None):
    code, out = run(playbook, state_dir, extra)
    if code != 0:
        sys.stderr.write(out)
        raise SystemExit(f"{playbook} failed")
    return out


def reset(state_dir, scenario="happy", fleet="default"):
    must("reset_state.yml", state_dir, {"scenario": scenario, "fleet": fleet})


def plan_of(state_dir):
    return json.loads((Path(state_dir) / "plan.json").read_text())


def tier_job(state_dir, tier, hosts, retries=1):
    """One tier job with the workflow's retry policy (max_retries=1)."""
    for attempt in range(retries + 1):
        code, out = run(f"reboot_{tier}.yml", state_dir, {"host_list": hosts})
        if code == 0:
            return tier, True, attempt, out
    return tier, False, retries, out


def one_round(state_dir):
    """plan_round -> Switch -> three parallel tier jobs -> Converge -> verify_round."""
    must("plan_round.yml", state_dir)
    plan = plan_of(state_dir)
    if plan["decision"] == "done":
        return plan, "done", {}
    if plan["decision"] == "blocked":
        code, out = run("report_blocked.yml", state_dir)
        assert code != 0 and "BLOCKED" in out, "blocked path must fail with a reason"
        return plan, "blocked", {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(tier_job, state_dir, t, plan[f"reboot_{t}"]) for t in TIERS]
        results = {f.result()[0]: f.result()[1:] for f in futures}
    if not all(ok for ok, _, _ in results.values()):
        return plan, "failed", results
    must("verify_round.yml", state_dir)
    return plan, "ok", results


def run_workflow(state_dir, max_iterations=6):
    """The While loop. Returns the list of plans and how it ended."""
    plans = []
    for _ in range(max_iterations):
        plan, status, results = one_round(state_dir)
        if status in ("blocked", "failed"):
            return plans, status, results
        if status == "done":
            return plans, "done", results
        plans.append(plan)
    return plans, "max_iterations", {}


def batch(plan):
    return {t: plan[f"reboot_{t}"] for t in TIERS}


def state(state_dir):
    out = {}
    for t in TIERS:
        out.update(json.loads((Path(state_dir) / f"{t}.json").read_text())["hosts"])
    return out


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"  ok  {msg}")


def main():
    base = Path(tempfile.mkdtemp(prefix="ao-reboot-v2-"))

    print("TC-1 happy path 4/4/3 -> rounds of 6, 4, 1")
    d = str(base / "happy"); reset(d)
    plans, end, _ = run_workflow(d)
    expect([p["selected_count"] for p in plans] == [6, 4, 1], "rounds are 6, 4, 1")
    expect(batch(plans[0]) == {"web": ["web-01", "web-02", "web-03"], "app": ["app-01", "app-02"], "db": ["db-01"]}, "round 1 batch")
    expect(batch(plans[1]) == {"web": ["web-04"], "app": ["app-03", "app-04"], "db": ["db-02"]}, "round 2 batch")
    expect(batch(plans[2]) == {"web": [], "app": [], "db": ["db-03"]}, "round 3 batch")
    expect(end == "done" and not any(h["needs_reboot"] for h in state(d).values()), "ends done, nothing pending")

    print("TC-2 one failure, the job retry heals it")
    d = str(base / "retry"); reset(d, "retry")
    plan, status, results = one_round(d)
    expect(status == "ok" and results["web"][1] == 1, "web job needed exactly one retry")
    expect(results["app"][1] == 0 and results["db"][1] == 0, "app and db jobs did not retry")
    plans, end, _ = run_workflow(d)
    expect(end == "done", "workflow finishes")

    print("TC-3 two failures, the run stops, Retry run replans only what is pending")
    d = str(base / "hard"); reset(d, "hard")
    plan, status, results = one_round(d)
    expect(status == "failed" and results["db"][0] is False, "db job fails after its retry")
    expect(results["web"][0] and results["app"][0], "web and app finished in the same round")
    s = state(d)
    expect(s["db-01"]["needs_reboot"] and not s["web-01"]["needs_reboot"], "db-01 still pending, web-01 done")
    plans, end, _ = run_workflow(d)
    expect(plans[0]["reboot_web"] == ["web-04"] and plans[0]["reboot_db"] == ["db-01"], "restart plans only the pending hosts")
    expect(end == "done", "restarted run finishes")

    print("TC-4 blocked: every tier at its minimum")
    d = str(base / "blocked"); reset(d, "blocked")
    plans, end, _ = run_workflow(d)
    expect(end == "blocked" and not plans, "run stops as blocked, nothing rebooted")

    print("TC-5 other fleet 6/3/5 under the same policy")
    d = str(base / "flex"); reset(d, fleet="flex")
    plans, end, _ = run_workflow(d)
    first = batch(plans[0])
    expect((len(first["web"]), len(first["app"]), len(first["db"])) == (5, 1, 1), "first batch is 5, 1, 1")
    expect(end == "done", "finishes")

    print("TC-6 single database is never selected while others are")
    d = str(base / "solo"); reset(d, fleet="solo_db")
    must("plan_round.yml", d)
    expect(plan_of(d)["reboot_db"] == [], "the only database stays up")

    print("TC-8 a host that never returns shrinks the tier, the run stops safely")
    d = str(base / "noreturn"); reset(d, "no_return")
    plan, status, results = one_round(d)
    expect(status == "failed" and results["app"][0] is False, "app job fails")
    plans, end, _ = run_workflow(d)
    expect(end in ("failed", "blocked"), f"run does not pretend to finish (ended {end})")
    expect(not state(d)["app-01"]["healthy"], "app-01 stays marked unhealthy")

    print("\nAll checks passed")


if __name__ == "__main__":
    main()
