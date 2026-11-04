#!/usr/bin/env python3
"""Run the dummy tasks in the order the Automation Orchestrator logic steps will.

The canvas, not this script, is what runs the While loop, For each loops,
Condition, Switch, parallel confirms, and Converge. This script checks the
data those steps will pass: batch order, empty-list skips, quorum held, a
one-failure retry, and a two-failure stop.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "playbooks"
LOG = Path(__file__).resolve().parent / "happy-path.log"

EXPECTED = [
    {
        "rows": [
            {"tier": "web", "instances": 4, "healthy": 4, "needs_reboot": 4, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 3},
            {"tier": "app", "instances": 4, "healthy": 4, "needs_reboot": 4, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 2},
            {"tier": "db", "instances": 3, "healthy": 3, "needs_reboot": 3, "minimum": 2, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 6,
        "shutdown": {
            "web": ["web-01", "web-02", "web-03"],
            "app": ["app-01", "app-02"],
            "db": ["db-01"],
        },
    },
    {
        "rows": [
            {"tier": "web", "instances": 4, "healthy": 4, "needs_reboot": 1, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 1},
            {"tier": "app", "instances": 4, "healthy": 4, "needs_reboot": 2, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 2},
            {"tier": "db", "instances": 3, "healthy": 3, "needs_reboot": 2, "minimum": 2, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 4,
        "shutdown": {
            "web": ["web-04"],
            "app": ["app-03", "app-04"],
            "db": ["db-02"],
        },
    },
    {
        "rows": [
            {"tier": "web", "instances": 4, "healthy": 4, "needs_reboot": 0, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 0},
            {"tier": "app", "instances": 4, "healthy": 4, "needs_reboot": 0, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 0},
            {"tier": "db", "instances": 3, "healthy": 3, "needs_reboot": 1, "minimum": 2, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 1,
        "shutdown": {
            "web": [],
            "app": [],
            "db": ["db-03"],
        },
    },
]


def run(playbook, state, plan, extra=None, check=True):
    cmd = [
        "ansible-playbook",
        str(PLAYBOOKS / playbook),
        "-i", "localhost,",
        "-c", "local",
        "-e", f"reboot_state_file={state}",
        "-e", f"reboot_plan_file={plan}",
    ]
    if extra:
        cmd.extend(["-e", json.dumps(extra)])
    result = subprocess.run(cmd, text=True, capture_output=True)
    output = result.stdout + result.stderr
    if check and result.returncode != 0:
        sys.stderr.write(output)
        raise SystemExit(f"{playbook} failed with {result.returncode}")
    return result.returncode, output


def ints(row):
    return {
        "tier": row["tier"],
        "instances": int(row["instances"]),
        "healthy": int(row["healthy"]),
        "needs_reboot": int(row["needs_reboot"]),
        "minimum": int(row["minimum"]),
        "minimum_rule": row["minimum_rule"],
        "max_allowed": int(row["max_allowed"]),
    }


def load(path):
    return json.loads(Path(path).read_text())


def play_tier(playbook, state, plan, phase, tier, hosts, lines, events, db_failures_left):
    if not list(hosts):
        lines.append(f"LOGIC condition false: skip {phase} tier={tier} (empty host list)\n")
        events.append((phase, tier, "skip"))
        return db_failures_left, True
    code, output = run(
        playbook,
        state,
        plan,
        {"tier_name": tier, "host_list": list(hosts)},
        check=False,
    )
    lines.append(output)
    if code != 0:
        if phase != "shutdown" or tier != "db" or "SIMULATED FAILURE" not in output:
            sys.stderr.write(output)
            raise SystemExit(f"unexpected {phase} failure on {tier}")
        db_failures_left -= 1
        code, output = run(
            playbook,
            state,
            plan,
            {"tier_name": tier, "host_list": list(hosts)},
            check=False,
        )
        lines.append(output)
        if code != 0:
            events.append((phase, tier, "fail"))
            return db_failures_left, False
    events.append((phase, tier, "task"))
    return db_failures_left, True


def one_round(state, plan, lines, db_failures_left, events):
    _, output = run("plan_round.yml", state, plan)
    lines.append(output)
    got = load(plan)
    lines.append("LOGIC switch path=reboot\n")
    lines.append("LOGIC for-each shutdown_batches order=web,app,db\n")
    for batch in got["shutdown_batches"]:
        db_failures_left, ok = play_tier(
            "shutdown_tier.yml",
            state,
            plan,
            "shutdown",
            batch["tier"],
            batch["hosts"],
            lines,
            events,
            db_failures_left,
        )
        if not ok:
            return got, db_failures_left, False
    lines.append("LOGIC for-each startup_batches order=db,app,web\n")
    for batch in got["startup_batches"]:
        db_failures_left, ok = play_tier(
            "startup_tier.yml",
            state,
            plan,
            "startup",
            batch["tier"],
            batch["hosts"],
            lines,
            events,
            db_failures_left,
        )
        if not ok:
            return got, db_failures_left, False
    lines.append("LOGIC parallel confirm_web confirm_app confirm_db\n")
    for tier in ("web", "app", "db"):
        _, output = run("confirm_tier.yml", state, plan, {"tier_name": tier})
        lines.append(output)
        if f"CONFIRM tier={tier}" not in output or "quorum=held" not in output:
            sys.stderr.write(output)
            raise SystemExit(f"confirm {tier} did not report quorum=held")
    lines.append("LOGIC converge all branches\n")
    lines.append("LOGIC condition still_safe true\n")
    _, output = run("verify_round.yml", state, plan)
    lines.append(output)
    return got, db_failures_left, True


def assert_plan(got, expected, label):
    rows = [ints(row) for row in got["rows"]]
    if rows != expected["rows"]:
        raise SystemExit(f"{label} table mismatch:\n{rows}\n!=\n{expected['rows']}")
    if int(got["max_eligible_sum"]) != expected["sum"]:
        raise SystemExit(f"{label} sum {got['max_eligible_sum']} != {expected['sum']}")
    if got["decision"] != "reboot":
        raise SystemExit(f"{label} decision {got['decision']} != reboot")
    for tier, hosts in expected["shutdown"].items():
        if list(got[f"shutdown_{tier}"]) != hosts:
            raise SystemExit(f"{label} shutdown {tier} {got[f'shutdown_{tier}']} != {hosts}")
        if list(got[f"startup_{tier}"]) != hosts:
            raise SystemExit(f"{label} startup list {tier} drifted")
    shutdown_batches = [(batch["tier"], list(batch["hosts"])) for batch in got["shutdown_batches"]]
    expected_shutdown = [(tier, hosts) for tier, hosts in expected["shutdown"].items()]
    if shutdown_batches != expected_shutdown:
        raise SystemExit(f"{label} shutdown_batches {shutdown_batches} != {expected_shutdown}")
    startup_batches = [(batch["tier"], list(batch["hosts"])) for batch in got["startup_batches"]]
    expected_startup = [("db", expected["shutdown"]["db"]), ("app", expected["shutdown"]["app"]), ("web", expected["shutdown"]["web"])]
    if startup_batches != expected_startup:
        raise SystemExit(f"{label} startup_batches {startup_batches} != {expected_startup}")


def happy(state, plan):
    lines = []
    events = []
    run("reset_state.yml", state, plan, {"scenario": "happy"})
    for index, expected in enumerate(EXPECTED, start=1):
        before = len(events)
        got, _, finished = one_round(state, plan, lines, 0, events)
        if not finished:
            raise SystemExit(f"happy path stopped in round {index}")
        assert_plan(got, expected, f"round {index}")
        got_logic = {
            "shutdown": [(tier, action) for phase, tier, action in events[before:] if phase == "shutdown"],
            "startup": [(tier, action) for phase, tier, action in events[before:] if phase == "startup"],
        }
        want = {
            "shutdown": [(tier, "skip" if not hosts else "task") for tier, hosts in expected["shutdown"].items()],
            "startup": [
                (tier, "skip" if not expected["shutdown"][tier] else "task")
                for tier in ("db", "app", "web")
            ],
        }
        if got_logic != want:
            raise SystemExit(f"round {index} logic path {got_logic} != {want}")
    LOG.write_text("".join(lines))
    _, output = run("plan_round.yml", state, plan)
    final = load(plan)
    if int(final["needs_reboot"]) != 0 or int(final["selected_count"]) != 0 or final["decision"] != "done":
        raise SystemExit(f"expected a clear service, got {final['needs_reboot']} still needing reboot")
    LOG.write_text("".join(lines))
    print(f"happy path: 3 rounds matched the 4/4/3 sheet. Log: {LOG}")


def retry_once(state, plan):
    run("reset_state.yml", state, plan, {"scenario": "retry"})
    lines = []
    got, left, finished = one_round(state, plan, lines, 1, [])
    if not finished or left != 0:
        raise SystemExit("retry scenario should fail shutdown once, then finish the round")
    assert_plan(got, EXPECTED[0], "retry round 1")
    if "SIMULATED FAILURE shutdown db-01" not in "".join(lines):
        raise SystemExit("retry scenario did not record the simulated failure")
    if "SHUTDOWN db-01 tier=db" not in "".join(lines):
        raise SystemExit("retry scenario did not shut down db-01 after the retry")
    for index, expected in enumerate(EXPECTED[1:], start=2):
        got, _, finished = one_round(state, plan, lines, 0, [])
        if not finished:
            raise SystemExit(f"retry path stopped in round {index}")
        assert_plan(got, expected, f"retry round {index}")
    final = load(state)
    if any(host["needs_reboot"] for host in final["hosts"]):
        raise SystemExit("retry path left a host needing reboot")
    print("retry path: db-01 failed once, the second shutdown succeeded, three rounds finished")


def hard_stop(state, plan):
    run("reset_state.yml", state, plan, {"scenario": "hard"})
    before = load(state)
    _, plan_out = run("plan_round.yml", state, plan)
    got = load(plan)
    code, output = run(
        "shutdown_tier.yml",
        state,
        plan,
        {"tier_name": "db", "host_list": got["shutdown_db"]},
        check=False,
    )
    if code == 0 or "remaining_after=1" not in output:
        raise SystemExit("hard fail: first attempt should leave one failure")
    code, output = run(
        "shutdown_tier.yml",
        state,
        plan,
        {"tier_name": "db", "host_list": got["shutdown_db"]},
        check=False,
    )
    if code == 0 or "remaining_after=0" not in output:
        raise SystemExit("hard fail: second attempt should still fail")
    after = load(state)
    if [host["needs_reboot"] for host in after["hosts"]] != [host["needs_reboot"] for host in before["hosts"]]:
        raise SystemExit("hard fail changed needs_reboot before a successful startup")
    run("reset_state.yml", state, plan, {"scenario": "clear_fault"})
    code, output = run(
        "shutdown_tier.yml",
        state,
        plan,
        {"tier_name": "db", "host_list": got["shutdown_db"]},
    )
    if "SHUTDOWN db-01 tier=db" not in output:
        raise SystemExit("clear_fault did not allow the same shutdown to succeed")
    print("hard stop: two failures left every host pending, and clear_fault lets the step succeed")


def blocked(state, plan):
    run("reset_state.yml", state, plan, {"scenario": "blocked"})
    run("plan_round.yml", state, plan)
    got = load(plan)
    if int(got["selected_count"]) != 0 or int(got["needs_reboot"]) != 11 or got["decision"] != "blocked":
        raise SystemExit(f"blocked plan should select nobody, got {got['selected_count']} of {got['needs_reboot']}")
    code, output = run("quorum_blocked.yml", state, plan, check=False)
    if code == 0 or "QUORUM BLOCK" not in output:
        raise SystemExit("quorum playbook should fail closed")
    print("blocked path: each tier is already on its floor, so the plan selects nobody")


FLEX = [
    {
        "rows": [
            {"tier": "web", "instances": 6, "healthy": 6, "needs_reboot": 6, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 5},
            {"tier": "app", "instances": 3, "healthy": 3, "needs_reboot": 3, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 1},
            {"tier": "db", "instances": 5, "healthy": 5, "needs_reboot": 5, "minimum": 4, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 7,
        "shutdown": {
            "web": ["web-01", "web-02", "web-03", "web-04", "web-05"],
            "app": ["app-01"],
            "db": ["db-01"],
        },
    },
    {
        "rows": [
            {"tier": "web", "instances": 6, "healthy": 6, "needs_reboot": 1, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 1},
            {"tier": "app", "instances": 3, "healthy": 3, "needs_reboot": 2, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 1},
            {"tier": "db", "instances": 5, "healthy": 5, "needs_reboot": 4, "minimum": 4, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 3,
        "shutdown": {
            "web": ["web-06"],
            "app": ["app-02"],
            "db": ["db-02"],
        },
    },
    {
        "rows": [
            {"tier": "web", "instances": 6, "healthy": 6, "needs_reboot": 0, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 0},
            {"tier": "app", "instances": 3, "healthy": 3, "needs_reboot": 1, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 1},
            {"tier": "db", "instances": 5, "healthy": 5, "needs_reboot": 3, "minimum": 4, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 2,
        "shutdown": {"web": [], "app": ["app-03"], "db": ["db-03"]},
    },
    {
        "rows": [
            {"tier": "web", "instances": 6, "healthy": 6, "needs_reboot": 0, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 0},
            {"tier": "app", "instances": 3, "healthy": 3, "needs_reboot": 0, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 0},
            {"tier": "db", "instances": 5, "healthy": 5, "needs_reboot": 2, "minimum": 4, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 1,
        "shutdown": {"web": [], "app": [], "db": ["db-04"]},
    },
    {
        "rows": [
            {"tier": "web", "instances": 6, "healthy": 6, "needs_reboot": 0, "minimum": 1, "minimum_rule": "keep-1", "max_allowed": 0},
            {"tier": "app", "instances": 3, "healthy": 3, "needs_reboot": 0, "minimum": 2, "minimum_rule": "keep-2", "max_allowed": 0},
            {"tier": "db", "instances": 5, "healthy": 5, "needs_reboot": 1, "minimum": 4, "minimum_rule": "keep-count-minus-1", "max_allowed": 1},
        ],
        "sum": 1,
        "shutdown": {"web": [], "app": [], "db": ["db-05"]},
    },
]


def clear_selected(state, plan, got):
    for batch in got["startup_batches"]:
        if not list(batch["hosts"]):
            continue
        run(
            "startup_tier.yml",
            state,
            plan,
            {"tier_name": batch["tier"], "host_list": list(batch["hosts"])},
        )


def flex(state, plan):
    run(
        "reset_state.yml",
        state,
        plan,
        {"scenario": "happy", "initial_state_file": str(ROOT / "state" / "storefront-flex.initial.json")},
    )
    for index, expected in enumerate(FLEX, start=1):
        run("plan_round.yml", state, plan)
        got = load(plan)
        assert_plan(got, expected, f"flex round {index}")
        clear_selected(state, plan, got)
    run("plan_round.yml", state, plan)
    done = load(plan)
    if done["decision"] != "done" or int(done["needs_reboot"]) != 0:
        raise SystemExit(f"flex fleet should finish clear, got {done['decision']} needs={done['needs_reboot']}")
    print("flex path: 6 web, 3 app, 5 db kept 1 web, 2 app, and 4 databases")


def solo_db(state, plan):
    run(
        "reset_state.yml",
        state,
        plan,
        {"scenario": "happy", "initial_state_file": str(ROOT / "state" / "storefront-solo-db.initial.json")},
    )
    run("plan_round.yml", state, plan)
    got = load(plan)
    db = ints(got["rows"][2])
    if db["instances"] != 1 or db["minimum"] != 1 or db["minimum_rule"] != "keep-the-only-node" or db["max_allowed"] != 0:
        raise SystemExit(f"a single database should stay up, got {db}")
    if int(got["selected_count"]) != 0 or got["decision"] != "blocked":
        raise SystemExit(f"solo database plan should block, got {got['decision']} selected {got['selected_count']}")
    print("solo database: the only node is the quorum, so the plan selects nobody")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        state = str(Path(tmp) / "storefront.json")
        plan = str(Path(tmp) / "plan.json")
        happy(state, plan)
        retry_once(state, plan)
        hard_stop(state, plan)
        blocked(state, plan)
        flex(state, plan)
        solo_db(state, plan)
    print("smoke passed")


if __name__ == "__main__":
    main()
