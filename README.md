# Storefront advanced reboot, v2 (no tier order, existing tier playbooks)

What changed from v1:

| v1 | v2 |
| --- | --- |
| Shut down web, app, then database; start up in reverse | No tier order. The minimum per tier is the only control |
| One generic `shutdown_tier` and `startup_tier` | One reboot playbook per tier: `reboot_web`, `reboot_app`, `reboot_db`, standing in for the customer's existing playbooks |
| One shared state file | One state file per tier, so the three tier jobs can run at the same time |
| Quorum checked after startup | Each round re-reads live state; the verify step stops the run if any tier is below its minimum |
| A failed host stopped the whole tier | A failed host does not stop the others in its tier; only the failed host stays pending |

The job templates print lines and write state files. They do not reboot anything. Run the laptop check first:

```shell
cd v2
python3 local/smoke.py
```

## The contract with the existing playbooks

The workflow needs three things from each tier playbook, nothing more:

1. It accepts `host_list` (a list of host names) as an extra variable.
2. It does nothing when the list is empty.
3. It fails the job if any host did not reboot cleanly, and skips a host that no longer needs a reboot (so a retry never reboots a host twice).

`playbooks/reboot_web.yml`, `reboot_app.yml` and `reboot_db.yml` show that contract with different dummy steps per tier. `tasks/sim_reboot.yml` is the only lab-only part (it writes the state file and injects faults). Replace the three playbooks with the customer's own and keep `plan_round.yml` and `verify_round.yml`.

## Job templates (7)

Inventory `reboot-lab` has one host, `state-host`. Machine credential is SSH to that host. Project is this repo.

| Job template | Playbook | Extra variables |
| --- | --- | --- |
| `reboot-reset` | `v2/playbooks/reset_state.yml` | prompt on launch: `scenario`, `fleet` |
| `reboot-plan-round` | `v2/playbooks/plan_round.yml` | none |
| `reboot-web` | `v2/playbooks/reboot_web.yml` | `host_list` from the workflow |
| `reboot-app` | `v2/playbooks/reboot_app.yml` | `host_list` from the workflow |
| `reboot-db` | `v2/playbooks/reboot_db.yml` | `host_list` from the workflow |
| `reboot-verify-round` | `v2/playbooks/verify_round.yml` | none |
| `reboot-report-blocked` | `v2/playbooks/report_blocked.yml` | none |

Optional on every template: `reboot_state_dir: /var/tmp/ao-reboot-v2`.

## The workflow, built on the canvas

Name: `Storefront advanced reboot v2`. Manual trigger with an empty input schema. Reset is a separate job, run before **Run**.

```text
[Manual trigger]
      |
 until_clear  (While, max iterations 6)
      |  Iterate
 plan_round  (reboot-plan-round)
      |
 round_gate  (Switch on ${plan_round.artifacts.decision})
      |-- reboot  --+-- reboot_web (reboot-web, retry 1) --+
      |             +-- reboot_app (reboot-app, retry 1) --+--> tiers_done (Converge: all)
      |             +-- reboot_db  (reboot-db,  retry 1) --+          |
      |                                                         verify_round
      |                                                  (loops back to until_clear)
      |-- blocked --> report_blocked   (stops here, run shows Failed with the reason)
      '-- default (done) --> end
```

| Step | Type | Settings |
| --- | --- | --- |
| `until_clear` | Logic > Loop > While | Condition `${verify_round.artifacts.gate} == "continue"`, max iterations `6`, on failure stop |
| `plan_round` | AAP job template | `reboot-plan-round` |
| `round_gate` | Logic > Switch | Cases `reboot`, `blocked` on `${plan_round.artifacts.decision}`; anything else is the default (done) |
| `reboot_web` | AAP job template | `reboot-web`; extra vars `host_list` = `${plan_round.artifacts.reboot_web}`; override retry policy `max_retries 1`, interval `5`, backoff `1`; on failure stop |
| `reboot_app` | AAP job template | Same, with `${plan_round.artifacts.reboot_app}` |
| `reboot_db` | AAP job template | Same, with `${plan_round.artifacts.reboot_db}` |
| `tiers_done` | Logic > Converge | Continue when all branches reach it |
| `verify_round` | AAP job template | `reboot-verify-round`; connect back to `until_clear` |
| `report_blocked` | AAP job template | `reboot-report-blocked`; on the `blocked` case only; not connected back |

Parallel is three edges leaving the `reboot` case of `round_gate`. There is no Condition in front of the tier jobs: an empty list is a no-op in the playbook, which is also what makes the Converge safe (a Condition would leave one port unused).

The Switch case and artifact expressions follow the same pattern as the v1 workflow; if the builder inserts a different token when you drag an artifact in, keep the inserted token.

## Scenarios

| Run `reboot-reset` with | Expected |
| --- | --- |
| `scenario=happy` | Three rounds of 6, 4 and 1 hosts. Round 1: web-01..03, app-01..02, db-01 together |
| `scenario=retry` | `reboot_web` fails on web-02, the job retry succeeds, the workflow finishes |
| `scenario=hard` | `reboot_db` fails twice, the workflow is Failed with db-01 pending; **Retry run** plans only the pending hosts and finishes |
| `scenario=no_return` | app-01 never comes back healthy; the run stops, the tier is not pushed below its minimum |
| `scenario=blocked` | Every tier at its minimum: `report_blocked` fails with the reason, nothing is rebooted |
| `fleet=flex` | 6 web, 3 app, 5 database under the same policy: first batch 5, 1, 1 |
| `fleet=solo_db` | The only database is never selected |

Run `reboot-reset` again before every workflow run.

## Where the rules live

`playbooks/paths.yml` holds `tier_policy`:

```yaml
tier_policy:
  web: {keep: 1}
  app: {keep: 2}
  db: {keep_all_but: 1}
```

Override it per service with a job template extra variable. Allowed per round = the smaller of "needs a reboot" and "healthy minus the minimum".

## Answering the dynamic inventory question

Inventory gives the candidates. `plan_round.yml` reads state through `tasks/load_state.yml`. For a real service, replace that one file with a task that builds the same `tiers` structure from the inventory (group per tier) and from a reboot-required check on each host (for example `needs-restarting -r`). Nothing else changes.
