# Cue card — Storefront advanced reboot

Fill the blanks during prep. Do not invent host names in the room. The lists below are what `local/smoke.py` produced from `state/storefront.initial.json`.

| Blank | Value |
|-------|-------|
| State host | |
| AAP organization | |
| AO build | |
| Workflow smoke date | |

Reset job: `reboot-reset`. Working file: `/var/tmp/ao-reboot-demo/storefront.json`.

| Live run | `scenario` |
|----------|------------|
| Three rounds | `happy` |
| One failure, then the step retries | `retry` |
| Two failures, workflow stops, then Retry run | `hard` |

Rule on the plan artifact: `max_allowed = min(needs_reboot, healthy − minimum)`. The minimum comes from the host list, not from a fixed 4/4/3 sheet.

| Tier | Floor, for any instance count |
|------|-------------------------------|
| web | Keep 1 available. Rule `keep-1`. |
| app | Keep 2 available. Rule `keep-2`. |
| database | Keep instance count − 1 available. Rule `keep-count-minus-1`. One database per round; the rest stay up. |
| one database | That node is the quorum. Rule `keep-the-only-node`. It is not selected. |

Storefront is 4 web, 4 app, and 3 database, so the floors are 1, 2, and 2. A web tier of 1, or an app tier of 2, has no room while those hosts are healthy. `state/storefront-flex.initial.json` is the other size the laptop check runs: 6 web, 3 app, 5 database. Its first batch is 5, 1, and 1, and the database floor is 4.

`decision` is `reboot`, `blocked`, or `done`. The While continues while verify `gate` is `continue`.

Logic steps, in the order to point at them: [Use logic steps to govern the control flow](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/discover-workflow_concepts#use-logic-steps-to-govern-the-control-flow).

| Pattern | Step |
|---------|------|
| Task | `plan_round`, `shutdown_tier`, `startup_tier`, `confirm_web`, `confirm_app`, `confirm_db`, `verify_round` |
| Loop | `until_clear` (While), `shutdown_tiers` and `startup_tiers` (For each) |
| Switch | `round_gate`: reboot, blocked, done |
| Conditional | `shutdown_has_hosts` and `startup_has_hosts` skip an empty list. `still_safe` is true when every confirm says `quorum=held`. |
| Sequential | Done of `shutdown_tiers` starts `startup_tiers` |
| Parallel | Done of `startup_tiers` starts the three confirms together |
| Converge | `tiers_reported`, all branches |

## Round 1 — `max_eligible_sum` 6

| Tier | Instances | Healthy | Needs reboot | Minimum | Rule | Max allowed | Hosts |
|------|-----------|---------|--------------|---------|------|-------------|-------|
| web | 4 | 4 | 4 | 1 | keep-1 | 3 | web-01, web-02, web-03 |
| app | 4 | 4 | 4 | 2 | keep-2 | 2 | app-01, app-02 |
| db | 3 | 3 | 3 | 2 | keep-count-minus-1 | 1 | db-01 |

Shutdown tasks, in order: those web hosts, then those app hosts, then `db-01`.

Startup tasks, in order: `db-01`, then the two app hosts, then the three web hosts.

Confirms, started together: web, app, and db each print `quorum=held`. Converge, then verify: `needs_reboot=5`.

## Round 2 — `max_eligible_sum` 4

| Tier | Instances | Healthy | Needs reboot | Minimum | Rule | Max allowed | Hosts |
|------|-----------|---------|--------------|---------|------|-------------|-------|
| web | 4 | 4 | 1 | 1 | keep-1 | 1 | web-04 |
| app | 4 | 4 | 2 | 2 | keep-2 | 2 | app-03, app-04 |
| db | 3 | 3 | 2 | 2 | keep-count-minus-1 | 1 | db-02 |

Verify: `needs_reboot=1` and the remaining host is `db-03`.

## Round 3 — `max_eligible_sum` 1

| Tier | Instances | Healthy | Needs reboot | Minimum | Rule | Max allowed | Hosts |
|------|-----------|---------|--------------|---------|------|-------------|-------|
| web | 4 | 4 | 0 | 1 | keep-1 | 0 | none — `shutdown_has_hosts` takes the false port, no shutdown task |
| app | 4 | 4 | 0 | 2 | keep-2 | 0 | none — false port, no shutdown task |
| db | 3 | 3 | 1 | 2 | keep-count-minus-1 | 1 | db-03 |

Startup: `STARTUP db-03 tier=db`. App and web take the false port on `startup_has_hosts`.

Confirms still print `quorum=held`. Verify: `needs_reboot=0`, `gate=done`. The While badge should read 3.

## Failure lines

`retry`: the database iteration of `shutdown_tier` prints `SIMULATED FAILURE shutdown db-01 tier=db remaining_after=0`, then the retry prints `SHUTDOWN db-01 tier=db`. Web and app iterations stay green.

`hard`: first failure ends `remaining_after=1`. Second failure ends `remaining_after=0`. The workflow status is Failed. After **Retry run**, the database iteration prints `SHUTDOWN db-01 tier=db`.

## Hours line

Leave both minute counts blank until someone who runs this service by hand gives them.

| Input | Minutes |
|-------|---------|
| A person rebooting this 11-host service by hand, including the quorum check | |
| This workflow, unattended, for the same service | |

Hours saved per month = windows per month × (manual minutes − workflow minutes) / 60.

Windows per month: ________
