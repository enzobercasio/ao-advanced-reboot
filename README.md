# Storefront advanced reboot — lab runbook

Build this workflow on the Automation Orchestrator 2026.8 and Ansible Automation Platform 2.7 you already have. This runbook does not install either product. The thing on screen is the Orchestrator canvas. The job templates only print a line and write a state file. They do not reboot a machine.

Presenter script: `demo-ao-advanced-reboot-2026-11-03.md`.

The laptop smoke checks the 4/4/3 rounds, the tier order, a one-failure retry, a two-failure stop, a 6/3/5 fleet under the same floors, and a single database that stays up:

```shell
cd demo-assets/ao-advanced-reboot
python3 local/smoke.py
```

That smoke used ansible-core 2.21.5 on the workstation. The lab job templates should use the default AAP 2.7 execution environment. The playbooks use `ansible.builtin` only.

Product docs used below:

- [Use logic steps to govern the control flow](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/discover-workflow_concepts#use-logic-steps-to-govern-the-control-flow)
- [Understand control flow steps](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-understand_control_flow_steps)
- [Add a While loop](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_while_loop_step)
- [Add a For each loop](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_for_each_loop_step)
- [Add a switch](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_switch_step_to_a_workflow)
- [Add a converge step](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_converge_step_to_a_workflow)
- [Arrange and connect the steps](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/get_started-arrange_and_connect_steps) — one output connected to several steps runs those steps at the same time
- [Launch an AAP job template](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_an_aap_job_template_step)
- [Job artifacts and `set_stats`](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-understand_inputs_and_outputs_for_aap_steps)
- [Pass job output downstream](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-use_job_output_in_a_downstream_step)
- [Run and monitor](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/get_started-run_and_monitor_your_workflow)
- [Create a job template](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-proc_controller_create_job_template) — Automation Execution → Templates → Create template → Create job template
- [Prompt on launch for variables](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-ref_controller_job_template_variables)

---

## What the canvas has to show

The workflow is the control-flow example in [Use logic steps to govern the control flow](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/discover-workflow_concepts#use-logic-steps-to-govern-the-control-flow). Each pattern on that page has one job on this canvas.

| Pattern | Step | Role on this canvas |
|---------|------|----------------|
| Task | `plan_round`, `shutdown_tier`, `startup_tier`, `confirm_web`, `confirm_app`, `confirm_db`, `verify_round`, `quorum_blocked` | Ansible job templates. They print the lines and write the state file. |
| Loop | `until_clear` (While) and `shutdown_tiers` / `startup_tiers` (For each) | The While replans until verify says `gate=done`. Each For each walks one batch list. |
| Switch | `round_gate` | Three ways out of the plan: `reboot`, `blocked`, or `done`. |
| Conditional | `shutdown_has_hosts`, `startup_has_hosts`, `still_safe` | An empty host list takes the false port and skips the task. `still_safe` continues only when every confirm says `quorum=held`. |
| Sequential | Done of `shutdown_tiers` → `startup_tiers` | Shutdown finishes before startup starts. Inside each For each, web runs before app before database, because that is the list order. |
| Parallel | Done of `startup_tiers` connected to `confirm_web`, `confirm_app`, and `confirm_db` | One step, three outgoing edges. The three checks start together. |
| Converge | `tiers_reported` | **All branches reach this step.** `verify_round` waits until web, app, and database have all reported. |

The floor is computed from the host list, so the same workflow fits any instance count. Web keeps 1 available. App keeps 2 available. Database keeps instance count − 1 available, so at most one database is in the batch and the rest stay up as the quorum. A single database is that quorum and is not selected. `max allowed = min(needs reboot, healthy − minimum)`, and the minimum cannot go below zero room. Storefront is 4 web, 4 app, and 3 database, so the floors are 1, 2, and 2. `state/storefront-flex.initial.json` is 6, 3, and 5; its first batch is 5 web, 1 app, and 1 database, with a database floor of 4.

`shutdown_batches` is web, then app, then database. `startup_batches` is the reverse. An empty list stays in the batch so the condition's false port is visible. Round 3 skips web and app, then shuts down `db-03`.

The state file changes when a startup task succeeds. A failed shutdown leaves every host still needing a reboot.

There is no separate Parallel step. Parallel is three edges from one handle. [Arrange and connect the steps](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/get_started-arrange_and_connect_steps). A Condition or Switch does not get a Converge after it, because only one of its ports runs. [Add a converge step](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_converge_step_to_a_workflow).

---

## 1. Set up the environment

You need a login to the AAP you already run, and to Automation Orchestrator on that same platform. Use one organization for every object below. Leave privilege escalation off. These playbooks write under `/var/tmp`, which the SSH user can create.

The jobs SSH to one host, `state-host`, and write:

`/var/tmp/ao-reboot-demo/storefront.json`

`web-01` through `web-04`, `app-01` through `app-04`, and `db-01` through `db-03` are names in that file. The canvas prints them. The jobs do not log in to those eleven names. A local connection inside the execution environment is not a state host: each job gets a fresh project copy, and a file written there is gone on the next job.

### 1a. State host, on a machine you already have

Skip this subsection if you are launching the host on AWS.

Pick any Linux host the AAP execution nodes can reach on TCP 22. A RHEL 9 host is the one this lab was written for. Create a user that can SSH with a key, or use the cloud default user if the host is already a cloud image. You will put that user and that private key in the Machine credential in step 1c. On a RHEL AMI the default user is `ec2-user`. [Connect to the instance](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/deploying_rhel_9_on_amazon_web_services/index).

Confirm from a host on the same network as the execution nodes:

```shell
chmod 400 /path/to/lab-key.pem
ssh -i /path/to/lab-key.pem ec2-user@STATE_HOST_ADDRESS
```

Then continue at step 1c. The address in `ansible_host` is the address those execution nodes use, which is the private address when they share a network with this host.

### 1b. State host, on AWS

Use this when you want a new dummy machine. One instance is enough. Launch it from a RHEL 9 AMI that is already Available in the region your execution nodes can reach. [Launching an instance from the AMI](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/deploying_rhel_9_on_amazon_web_services/index).

1. In the AWS console, open EC2 in that region. Go to Images, then AMIs, and launch the RHEL 9 image. A small general-purpose instance type is enough. This host stores one JSON file.
2. Set the instance count to 1. Choose the VPC and subnet your execution nodes can route to. Turn on auto-assign public IP only when those nodes are outside the VPC and must use a public address.
3. Keep the default storage. On the tags step, set the Name tag to `state-host`.
4. Choose a security group that allows inbound TCP 22 from the execution nodes only. Do not open SSH to the internet for this lab.
5. Launch with a key pair you can download. After the instance is running, Connect shows the SSH command. The RHEL cloud user is `ec2-user`:

```shell
chmod 400 /path/to/lab-key.pem
ssh -i /path/to/lab-key.pem ec2-user@STATE_HOST_ADDRESS
```

6. Disconnect. The playbook creates `/var/tmp/ao-reboot-demo/` on the first reset. You do not create that directory by hand.

Optional dummy tier instances, if you want the same names sitting in EC2 while you talk. They are not part of the job. Repeat the launch for 4 web, 4 app, and 3 database instances, and set the Name tag to `web-01` … `web-04`, `app-01` … `app-04`, and `db-01` … `db-03`. Same AMI, subnet, and security group. Leave them running and do not reboot them from this workflow. Register them only in the optional inventory in step 1c. Point every job template at `reboot-lab`, which contains `state-host` alone.

### 1c. Credential, inventory, and project

Machine credential. [Create new credentials](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/secure-proc_controller_create_credential). The Machine type takes the SSH user and private key. [Machine credential](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/secure-con_controller_access_machine_credentials_playbook).

1. Automation Execution → Infrastructure → Credentials → Create credential.
2. Name `reboot-lab-ssh`. Organization: yours. Credential type: Machine.
3. Username `ec2-user`, or the user you chose in step 1a. Paste the private key into SSH Private Key. Leave Privilege Escalation Method empty.
4. Create credential.

Job inventory. One host. [Add a new inventory](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/administer-proc_controller_adding_new_inventory).

1. Automation Execution → Infrastructure → Inventories → Create inventory, and select the inventory type that asks for a name and an organization. That is the standard inventory. Do not create a smart inventory or a constructed inventory.
2. Name `reboot-lab`. Organization: yours. Create inventory.
3. Automation Execution → Infrastructure → Hosts → Create host. [Add a host](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/administer-assembly_controller_hosts).
4. Name `state-host`. Inventory `reboot-lab`. Variables:

```yaml
ansible_host: STATE_HOST_ADDRESS
```

5. Create host. `inventory/reboot-lab.ini` is the same shape, for your notes. Do not add `web-01` or the other tier names to `reboot-lab`. The playbooks target `hosts: all` with `run_once`. A second host would put the state file on whichever host the job contacts.

Optional picture inventory, only if you launched the eleven instances. Create a second standard inventory named `storefront-nodes`. Create one host per name in `inventory/storefront-nodes.ini`, with that instance's reachable address in `ansible_host`. On the host's Groups tab, Associate groups is how you attach a group after the group exists. These hosts are not selected by the job templates.

Project. Copy `playbooks/` and `state/` into a Git repository the platform can read. The repository root must contain those two directories. Leave `local/` out of the repository. [Add a new project](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-proc_controller_adding_a_project). [Git source control](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-ref_projects_manage_playbooks_with_source_control).

1. Automation Execution → Projects → Create project.
2. Name `ao-reboot-demo`. Organization: yours. Execution environment: the default AAP 2.7 environment, or leave it to the organization default.
3. Source control type: Git. Source control URL: that repository. Add a source control credential if the repository is private.
4. Under Options, select Update revision on launch.
5. Create project, then sync it. The project status needs to be Successful before you create templates.

A Manual project is the other documented source control type. It expects the files under the project base path, `/var/lib/awx/projects`, owned by the automation controller user, and the project status shows OK rather than Missing. [Manage playbooks manually](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-assembly_controller_projects). Use Git when the controller runs in a pod. That base path is inside the pod and is not where this lab keeps the state file.

### 1d. Go / no-go

Run these four templates by hand after section 2 exists. Do this before anyone is watching.

1. `reboot-reset` with `scenario=happy`.
2. `reboot-plan-round`. The artifact `decision` is `reboot`, and `shutdown_web` has three names.
3. `reboot-startup-tier` with extra vars `tier_name=web` and `host_list=["web-01"]`.
4. `reboot-plan-round` again.

Pass: `needs_reboot` is 10, and `web-01` is absent from `shutdown_web`.

Fail: `needs_reboot` is still 11. The job wrote the file somewhere that the next job cannot read. Point `state-host` at the Linux host from step 1a or 1b and repeat. Do not debug this with the audience watching. The fallback is `local/happy-path.log` plus the cue card.

After a pass, run `reboot-reset` with `scenario=happy` again so the workflow smoke starts from eleven hosts.

---

## 2. Project and job templates

On each template: inventory `reboot-lab`, credential `reboot-lab-ssh`, project `ao-reboot-demo`, privilege escalation off. [Create a job template](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/develop-proc_controller_create_job_template): Automation Execution → Templates → Create template → Create job template. Playbook paths:

| Job template | Playbook | Prompt on launch |
|--------------|----------|------------------|
| `reboot-reset` | `playbooks/reset_state.yml` | Variables, so `scenario` can be set |
| `reboot-plan-round` | `playbooks/plan_round.yml` | No |
| `reboot-shutdown-tier` | `playbooks/shutdown_tier.yml` | Variables |
| `reboot-startup-tier` | `playbooks/startup_tier.yml` | Variables |
| `reboot-verify-round` | `playbooks/verify_round.yml` | No |
| `reboot-confirm-tier` | `playbooks/confirm_tier.yml` | Variables |
| `reboot-quorum-blocked` | `playbooks/quorum_blocked.yml` | No |

Set this on every template's extra variables, including the ones Automation Orchestrator will override. Orchestrator sends the step's extra variables as the job's extra variables, so each workflow step below repeats the paths.

```json
{
  "reboot_state_file": "/var/tmp/ao-reboot-demo/storefront.json",
  "reboot_plan_file": "/var/tmp/ao-reboot-demo/plan.json"
}
```

`scenario` values for `reboot-reset`: `happy`, `retry`, `hard`, `clear_fault`, `blocked`.

---

## 3. Workflow

Create workflow `Storefront advanced reboot`. Leave the manual trigger's input schema empty. Reset is a separate job, run before **Run**.

Name each step exactly as written. Drag artifact fields into the extra variables. If the builder inserts a different step id than the name, keep the inserted token. Data flows forward through edges, so a step only references steps upstream of it. [Workflow concepts](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/discover-workflow_concepts#use-logic-steps-to-govern-the-control-flow).

Repeat these two paths in every job-template step's extra variables:

```json
{
  "reboot_state_file": "/var/tmp/ao-reboot-demo/storefront.json",
  "reboot_plan_file": "/var/tmp/ao-reboot-demo/plan.json"
}
```

1. **Add step > Logic > Loop.** Name it `until_clear`. Type **While**. Custom condition: `${verify_round.artifacts.gate} == "continue"`. Max iterations `4`. Settings → On failure: **Stop workflow or branch on failure**. The procedure calls the outgoing handles the loop body and **Done**. The node catalog calls them **Iterate** and **Complete**. [Add a While loop](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_while_loop_step).

2. On the loop body, **Add step > AAP Execution > Launch AAP job template.** Name it `plan_round`. Template `reboot-plan-round`. Extra variables are the two paths.

3. **Add step > Logic > Switch.** Name it `round_gate`. [Add a switch](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_switch_step_to_a_workflow).
   - Path `reboot`: `${plan_round.artifacts.decision} == "reboot"`
   - Path `blocked`: `${plan_round.artifacts.decision} == "blocked"`
   - Fallback: no expression. Connect it to `verify_round` once that step exists.

4. On the `reboot` handle, **Add step > Logic > Loop.** Name it `shutdown_tiers`. Type **For each**. Items: drag `shutdown_batches` from `plan_round`. Max iterations `3`. [Add a For each loop](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_for_each_loop_step).

5. In that loop body, **Add step > Logic > Condition.** Name it `shutdown_has_hosts`. In the visual expression builder, set the field to the current item's `hosts` (drag it; the inserted token looks like `${loop.item.hosts}`), operator **is empty**, and select **Not**. The true port means the list has hosts. Leave the false port unconnected so an empty tier ends that iteration. [Understand control flow steps](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-understand_control_flow_steps). If an empty tier stops the whole For each instead of advancing, connect the false port back to `shutdown_tiers` as well.

6. On the true port, add **Launch AAP job template** `shutdown_tier` on template `reboot-shutdown-tier`. Extra variables, besides the two paths:
   - `tier_name`: drag `tier` from the current loop item
   - `host_list`: drag `hosts` from the current loop item
   Connect `shutdown_tier` back to `shutdown_tiers`. On **Settings**, On failure: **Stop workflow or branch on failure**. Toggle **Override retry policy**: `max_retries` `1`, `initial_interval` `5`, `max_interval` `5`, `backoff_coefficient` `1`. [Job template step settings](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_an_aap_job_template_step).

7. From the **Done** handle of `shutdown_tiers`, add For each `startup_tiers`. Items: drag `startup_batches`. Max iterations `3`. Body condition `startup_has_hosts`, same empty-list check. True port launches `startup_tier` on template `reboot-startup-tier`, with `tier_name` and `host_list` dragged from that loop's current item. No retry policy. On failure: **Stop workflow or branch on failure**. Connect `startup_tier` back to `startup_tiers`.

8. From the **Done** handle of `startup_tiers`, draw three edges to three **Launch AAP job template** steps on template `reboot-confirm-tier`. Name them `confirm_web`, `confirm_app`, and `confirm_db`. Set `tier_name` to `web`, `app`, and `db`. Those three steps start together. [Arrange and connect the steps](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/get_started-arrange_and_connect_steps).

9. **Add step > Logic > Converge.** Name it `tiers_reported`. Continue when: **All branches reach this step**. On failure: **Stop workflow or branch on failure**. Connect `confirm_web`, `confirm_app`, and `confirm_db` into it. [Add a converge step](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/develop-add_a_converge_step_to_a_workflow).

10. After the converge, **Add step > Logic > Condition.** Name it `still_safe`. Visual builder, one AND group:
    - `${confirm_web.artifacts.quorum} == "held"`
    - `${confirm_app.artifacts.quorum} == "held"`
    - `${confirm_db.artifacts.quorum} == "held"`
    Drag the `quorum` field from each confirm step so the token matches the step id. True goes to `verify_round`. False goes to `quorum_blocked`.

11. `verify_round` uses template `reboot-verify-round` and the two paths. Connect it back to `until_clear`.

12. `quorum_blocked` uses template `reboot-quorum-blocked`. On failure: **Stop workflow or branch on failure**. It is the target of the switch's `blocked` path and of `still_safe` false. Do not connect it back to the loop.

13. Save. Do not add a Script step. In 2026.8, script execution is disabled and the step fails with `Script node execution is not enabled`. [Developer Preview](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/whats_new-developer_preview).

---

## 4. Workflow smoke

Run `reboot-reset` before every workflow run.

| Run | Reset scenario | Pass |
|-----|----------------|------|
| Happy path | `happy` | While badge ends at 3. Switch takes `reboot`. Shutdown For each order is web, then app, then database, and those iterations do not overlap. Round 3 marks the web and app conditions false and still shuts down `db-03`. `confirm_web`, `confirm_app`, and `confirm_db` are running at the same time, then `tiers_reported` completes, then `still_safe` takes true. Last verify `gate` is `done`. |
| Step retry | `retry` | The database iteration of `shutdown_tier` fails once. The retry succeeds and the job output contains `SHUTDOWN db-01 tier=db`. The web and app iterations stay green. |
| Restart | `hard` | The database iteration fails twice and the workflow is **Failed**. Hosts in the state file still have `needs_reboot` true. **Retry run** plans round 1 again, and the database iteration prints `SHUTDOWN db-01`. |

**Retry run** is on the execution detail view, the Workflow Runs row menu, or the builder run history. It starts a new execution with the same workflow version and inputs. [Monitor workflow runs](https://docs.redhat.com/en/documentation/automation_orchestrator/2026.8/observe-monitor_workflow_runs_expanded).

The `hard` counter is consumed by the two failed attempts. The next execution finds `remaining` at 0, so the dummy fault no longer fires. That counter is the lab's stand-in for "someone fixed the host". Say that out loud. `clear_fault` is only for a run you aborted before the counter reached 0.

`blocked` is a plan check, not a live segment. Reset with `scenario=blocked`. That leaves each tier healthy only up to its floor (1 web, 2 app, and count − 1 databases). Run `reboot-plan-round` and confirm `decision` is `blocked` and `selected_count` is 0. Reset to `happy` afterwards.

---

## 5. Session desk

Tabs, left to right: the workflow in the builder, the run canvas, the cue card. Hide bookmarks. Zoom until `shutdown_tiers`, the three confirm steps, and `tiers_reported` are readable on the projector.

Write the state-host name and the AAP organization on `CUE-CARD.md` during prep.
