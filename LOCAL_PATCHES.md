# 4handy local patches (branch `4handy/v0.23.1` = tag `v0.23.1` + these commits)

Checkout: `teamtech@cloudzone.4-handy.com:/home/teamtech/.hermes/hermes-agent`. Updates must preserve these
contracts unless upstream ships equivalent tested behaviour. To move to a new release: create
`4handy/<tag>` from the tag, cherry-pick the commits below, run the listed tests with `scripts/run_tests.sh`,
then switch the server checkout by hand (see "Updater" below). This file's presence also arms the updater guard.

## Mattermost tool progress is silent (core half)

- `gateway/run.py::_tool_progress_metadata` sets `metadata["silent"] = True` for platform value `mattermost`
  (enum or string, so it also matches the `mattermost-platform` override plugin, which registers
  `name="mattermost"`); `gateway/run_turn.py` applies it to `_progress_metadata` right after
  `_non_conversational_metadata`. Pure per-turn function: multiplex-safe.
- The adapter half (`POST /posts?silent=true`, notify wins over silent) lives in the override plugin
  `/home/teamtech/.hermes/shared-plugins/platforms/mattermost`, NOT in bundled `plugins/platforms/mattermost`.
  The bundled adapter ignores `silent`, so with the bundled adapter this patch is a no-op (that is what
  happened on v0.19.1: commit aaae331e62 shipped only this half).
- Tests: `tests/gateway/test_mattermost_silent_tool_progress.py`.

## Updater never drops local patches

- `hermes_cli/update_cmd.py::_refuse_dropping_local_commits`, called from `_prepare_checkout_for_update`
  before anything is stashed or moved. Armed only when this `LOCAL_PATCHES.md` exists at the checkout root.
- Refuses (exit 1, "No update was applied") when HEAD has commits on no `origin/*` ref and in no `v*` tag
  (`git rev-list HEAD --not --remotes=origin --tags='v*'`) and the update would move the tree off them:
  a stable release (lands detached at the tag), a parked-branch switch back to `main`, a same-name reset.
- Allowed: `updates.parked_branch_strategy: update_in_place` (merge into the branch), `--switch-branch` on the
  branch path, a detached HEAD (upstream parks it under `refs/hermes-update-backups/`), and any target that
  already contains HEAD (`git merge-base --is-ancestor HEAD <target>`).
- While the latest stable tag is already inside HEAD, upstream's `forward_only` pin makes HEAD itself the target:
  a no-move run (deps, migrations, restarts) that the guard lets through. Until 2026-11-15 the guard refused
  that run too (cloudzone upgrade day; worked around with a detached HEAD). It fires once a newer release ships.
- Tests: `tests/hermes_cli/test_update_keeps_local_patches.py`; upstream `tests/hermes_cli/test_update_*.py`
  must keep the same pass/fail set as the bare tag.
- Never `git reset --hard origin/main` to resolve a local-patch conflict.

## Dropped at v0.23.1 (upstream has equivalent tested behaviour)

| v0.19.1 local patch | Upstream replacement in v0.23.1 |
|---|---|
| 54638f8892 cross-loop plugin adapter delivery | `tools/send_message_tool.py::_dispatch_on_gateway_loop` runs the live adapter send on the gateway loop; `tests/tools/test_send_message_cross_loop.py` |
| 97a7033d6e prompt probe shares the default terminal env | `agent/prompt_builder.py::_run_backend_probe` uses the shared container-config shaper and force-removes the throwaway `prompt-backend-probe` sandbox (9640f8e1f8); `tests/agent/test_prompt_builder.py::test_probe_remote_backend_tears_down_its_sandbox` and siblings. Difference: one short-lived probe container per (profile, cwd) per process instead of zero |
| 88061bde40 updater rebases instead of reset | upstream merges on a custom branch / parks rescue refs; replaced by the guard above |
| uncommitted `[CRON_FAILURE]` | `cron/scheduler.py::_cron_failure_marker_error` (+ prompt text in `cron/scheduler_prompt.py`); `tests/cron/test_run_one_job.py::test_run_one_job_agent_declared_failure_*`, `test_run_one_job_marker_mentioned_in_report_stays_successful`. Differences: marker must be the literal first line (no leading blank line/spaces), English notice text, repeats of the same evidence are de-duplicated for `cron.failure_repeat_alert_hours` (default 6, `0` = every run) |
| `package-lock.json` churn | dropped: v0.23.1 has no `package-lock.json` (pnpm workspace); the churn was npm adding `"peer": true` flags |
| fork `feat/mattermost-known-command-prefix` | adapter-only; carried by the override plugin (036dcda), nothing in core |
