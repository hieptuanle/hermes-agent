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

## Cron preflight trusts sandbox-held credential files

- `cron/scheduler_preflight.py::_preflight_check_skills(job, cfg)` + `_job_terminal_backend(cfg)`. When the job's
  own profile uses the docker terminal backend (`terminal.backend` of the config the scheduler loaded for the
  job's home, else the scope-aware `TERMINAL_ENV`), a skill verdict whose ONLY gap is
  `missing_credential_files` does not block the job. Missing env vars or commands still block; other backends
  keep upstream's behaviour.
- Why: our bots keep google-workspace's `google_token.json` / `google_client_secret.json` in the persistent
  sandbox home (`sandboxes/docker/default/home/.hermes`, mounted at `/root/.hermes`), where the skill reads and
  refreshes them. `skill_view` checks only the host HERMES_HOME, so v0.23.1's new cron preflight recorded
  `blocked_config` for ~59 jobs on 24 bots (2026-11-15). Copying or symlinking the files into the host home is
  not a fix: a present host file is bind-mounted READ-ONLY at `/root/.hermes/google_token.json`
  (`tools/environments/docker.py::_readonly_skill_mount_args`) and the skill's in-place token refresh
  (`google_api.py`/`gws_bridge.py` `write_text`) then fails. `cron.preflight: false` would also work but drops
  the provider-key and delivery checks.
- Upstream issue candidate: credential-file readiness should consider the sandbox home for persistent docker.
- Tests: `tests/cron/test_preflight_sandbox_credentials.py`, plus `tests/cron/test_preflight_*.py`.

## Sanitized skill copies are stable per source (docker skills mounts)

- `tools/credential_files.py::_safe_skills_path` + `_sync_sanitized_tree` + `_skills_copy_file_lock`. A skills root
  that contains any symlink is still replaced by a regular-files-only copy, but the copy now lives at
  `<HERMES_HOME>/cache/skills-safe/<name>-<sha256(realpath)[:16]>`, is refreshed in place (changed files via temp +
  `os.replace`, removed entries pruned) under a thread lock plus a `flock` beside it, and is never deleted.
- Why: upstream (v0.19.1 and v0.23.1 alike) kept ONE per-process `mkdtemp` slot that each call `rmtree`'d first.
  (1) A profile whose local `skills/` AND an `external_dirs` entry both hold symlinks lost the first copy before
  docker checked it: "Docker: skipping skills mount — source is not a directory", so `/root/.hermes/skills` (with
  google-workspace `scripts/`) was missing in the sandbox. On cloudzone: 9 docker profiles (local skills symlink
  `erp-data-retrieval`/`erp-database`/`google-workspace` SKILL.md + `shared-skills` symlinks `erp-cli`/`outline-cli`).
  (2) Persistent containers reused across environment constructions kept bind mounts to copies that the next
  construction or process exit deleted, so `/root/.hermes/external_skills/0` (shared-skills scripts) went empty.
- The mount source path changes from the tempdir to `cache/skills-safe/…`, so the reuse fingerprint changes once
  for profiles with a symlinked root: their next terminal use starts a fresh sandbox container (the old one stays
  until removed by hand or by the reaper).
- Tests: `tests/tools/test_skills_safe_copy_stable.py` (5 of 6 fail on the bare tag), plus
  `tests/tools/test_credential_files.py`, `tests/tools/test_docker_environment.py`, `tests/tools/test_file_sync.py`.
- Upstream issue candidate: same bug on upstream main.

## google-workspace setup.py continues without `pm` when the Google libraries import

- `skills/productivity/google-workspace/scripts/setup.py`: `_google_imports_available()` (imports
  `googleapiclient.discovery`, `google.oauth2.credentials`, `google.auth.transport.requests`,
  `google_auth_oauthlib.flow`). When `pm is None`, `_ensure_deps()` returns and `install_deps()` succeeds without
  installing anything if they import; otherwise upstream's refusal stays ("Run this script in the Hermes
  environment", never an ambient pip install).
- Why: v0.23.1 made the script install/activate the Google extra only through Hermes' `pm`. Docker sandboxes have
  no `pm`, so `--check`, `--auth-url`, `--auth-code`, `--revoke` exited 1 even with the libraries present. On
  cloudzone the libraries come from `~/.hermes/shared-tools/pylib/py311` (built with the sandbox image), reached via
  `hermes-pylib.pth` in each sandbox home's user site (2026-11-15).
- Bundled-skill change: reaches a profile only where its copy still matches the bundled origin (`.bundled_manifest`),
  at gateway start or with `HERMES_HOME=<home> hermes --run-module tools.skills_sync`. User-modified copies keep
  their own (v0.19.1-style, pip-installing) setup.py.
- Tests: `tests/skills/test_google_workspace_setup_sandbox_deps.py` (all 5 fail on the bare script), plus
  `tests/skills/test_google_workspace_setup_deps.py`, `tests/skills/test_google_workspace_setup.py`.

## Skill paths shown to the model are sandbox paths

- `tools/credential_files.py::to_agent_visible_skill_path` / `map_skill_path_to_container` (beside upstream's
  `to_agent_visible_cache_path`): a host path under a skills root (`_skill_dir_roots`: local `skills/`,
  `external_dirs[i]`, project dirs) maps to its mount/sync target: docker/modal `/root/.hermes/skills/...`,
  `/root/.hermes/external_skills/<i>/...`; ssh/daytona/vercel_sandbox `~/.hermes/...`; local, plugin or unknown
  backends and paths under no root stay unchanged. It maps from the SOURCE root, so roots served through the
  sanitized copy (patch above) map to the same container root, and it never builds or refreshes that copy.
- Applied only where text goes to the model: the `skill_view` tool handler (`tools/skills_tool.py::
  _agent_visible_skill_view`, the `skill_dir` field), `${HERMES_SKILL_DIR}` in
  `agent/skill_preprocessing.py::preprocess_skill_content` (skill_view content, cron `skills:` prompt, slash and
  preload messages) and the `[Skill directory: ...]` / `node <dir>/scripts/foo.js` footer in
  `agent/skill_commands.py::_build_skill_message`. Internal `skill_view()` callers keep the host path (they walk
  supporting files and run inline shell on the host); inline `!`cmd`` snippets keep the host path too.
- Why: v0.23.1 dropped `User`/`Home`/`Working directory` from the docker backend probe in the system prompt
  (`agent/prompt_builder.py::_BACKEND_PROBE_CMD`, OS/kernel only), so the model no longer learns `$HOME=/root`
  in the sandbox and trusts `skill_view`'s host `skill_dir` (`/home/<user>/.hermes/profiles/<p>/skills/...`,
  the same value v0.19.1 returned). minhvubot cron `ad46536e64cc` ran
  `python <host skill_dir>/scripts/google_api.py` in the sandbox on 2026-11-16 08:00 and reported "script not in
  the Docker environment"; 11-10..11-14 (v0.19.1) the same job used `/root/.hermes/skills/...`.
- Tests: `tests/tools/test_skill_dir_agent_visible.py` (its 4 boundary tests fail when only the helper is present and the
  consumers are unpatched), plus `tests/agent/test_skill_commands*.py`, `tests/agent/test_skill_inline_shell_*.py`,
  `tests/tools/test_skills_tool*.py`, `tests/tools/test_skill_view_*.py`, `tests/tools/test_credential_files.py`,
  `tests/tools/test_docker_environment.py`, `tests/cron/test_cron_prompt_injection_skill.py`.
- Upstream issue candidate: unchanged up to `v0.23.1+canary.20261114` (skill_dir is host-only for every container backend).

## SKILL.md size cap is configurable and growth-only

- Config `skills.max_content_chars` (default 100000 in `hermes_cli/config_defaults.py` = upstream's fixed
  `MAX_SKILL_CONTENT_CHARS`; `<= 0`, a bool or a non-integer also means 100000), read per write from the
  read-only merged config of the active profile (`tools/skill_manager_tool.py::_skill_md_char_cap`).
- Contract (`_validate_content_size(content, label, before=None)`): a SKILL.md target (label's basename
  `SKILL.md`) over the cap is refused only when it is new (create) or longer than its current text (`before`).
  A write that shrinks or keeps the size of an over-cap SKILL.md always passes, so the background review and
  the curator can trim. Supporting files (`references/` etc.) keep the fixed 100k chars / 1 MiB limits, so the
  fix the refusal message names (move depth into `references/<topic>.md` via `write_file`, leave a pointer)
  always works.
- Where: `_guarded_write` decides it for every SKILL.md writer against the text on disk (create, edit, patch,
  `write_file`/`patch` with `file_path='SKILL.md'`); `_create_skill` and `_patch_skill` also check early (patch
  with `before`); `_edit_skill` and `_write_file` no longer check SKILL.md size before the current text is known.
- Why: the post-turn background review kept patching business skills until SKILL.md hit 100k chars (15 at
  99.6k-100k, 8 at 75-95k on cloudzone 2026-11-16); every `skill_view` of one loads ~36k tokens. The fleet sets
  `skills.max_content_chars: 40000` in every profile's config.yaml.
- Rollback: remove the key from the configs (backups `config.yaml.bak-skillcap-20261116`) for upstream
  behaviour with this code; or reset the branch to the commit before this patch (the key is ignored by older
  code) and restart the gateways.
- Tests: `tests/tools/test_skill_size_cap_config.py` (4 fail with growth-only disabled), plus
  `tests/tools/test_skill_size_limits.py`, `tests/tools/test_skill_manager_tool.py`, `tests/ci/test_docs_drift.py`
  (key documented in `website/docs/user-guide/configuration.md`).

## Docker exec carries the turn's session context (WT121755, D230)

- `tools/environments/docker.py::_run_bash` adds every session var bound in the calling task
  (`gateway/session_context.py::bound_session_env`: the `_VAR_MAP` names whose ContextVar is set here, `""`
  included) to EACH `docker exec`, as name-only `-e NAME` flags with the values in the docker client's env
  (#96268 pattern). Unbound vars are left out and never read from `os.environ` (the gateway's mirror can hold
  another conversation's sender). Parity with `tools/environments/local.py::_inject_session_context_env`, minus
  plugin session env.
- `cron/scheduler.py::_CronRunScope` binds `HERMES_SESSION_CRON_JOB_ID` / `HERMES_SESSION_CRON_JOB_NAME` (job
  name, never the prompt) on enter and resets them on exit; both are in `_VAR_MAP`, so the local backend bridges
  them too, and the `HERMES_SESSION_` prefix keeps them out of the shell snapshot.
- Why: the 4handy erp CLI (>= 0.90.0) builds its audit reason ("<username> (mm:<id>) nhờ, <post link>" /
  "cron <name> (<id>)") from these; without the patch every docker-backend bot sends none.
- Tests: `tests/tools/test_docker_session_env.py` (2 concurrent conversations, stale env mirror, cron scope;
  the 2 exec tests fail without the docker change) and, with `HERMES_TEST_DOCKER_IMAGE`,
  `tests/tools/test_docker_backend_real.py::test_concurrent_turns_each_see_their_own_session_in_one_container`;
  plus `tests/tools/test_docker_environment.py`, `tests/tools/test_snapshot_session_id_leak.py`, `tests/cron`.
- Upstream issue candidate: docker (and ssh/modal/daytona) backends do not bridge `HERMES_SESSION_*` at
  `v0.23.1`.

## Kanban board fence (`kanban.board_fence`, `kanban.chat_command`)

- Why: boards are shared files under one root, so a chat agent with the kanban toolset could read or write any
  board by naming it, and `/kanban` (`!kanban` on Mattermost) ran the whole kanban CLI for every user a bot
  accepts: any board, any assignee, so any staff member could make any profile run a card with its ERP token.
  The agent-box pilot (one board per box, `box-<owner>`) needs a wall. Spec and review: ai-workflow desk
  `specs/B1a-board-fence.md`, `reviews/B1a-board-fence.md`; signed off as decision F20 (2026-11-16).
- Setting, per profile in its own `config.yaml` (defaults in `hermes_cli/config_defaults.py`):
  `kanban.board_fence` = a board slug (fenced), `"*"` (any board, on purpose) or unset;
  `kanban.chat_command` = `false` by default. Read and normalised in one module,
  `hermes_cli/kanban_board_fence.py`. An invalid value fails closed everywhere.
- Rules 0-2, `tools/kanban_tools.py::_apply_board_fence`, called by the `_kanban_handler` wrapper for every kanban
  tool, skipped only for the dispatcher-owned worker (`owned_kanban_task()`; its env pin already ignores an
  explicit board). No setting and not the `default` profile → every tool refused; `"*"` → upstream; fenced → no
  `board` means the fence board (never `HERMES_KANBAN_BOARD` or `kanban/current`), another board is refused with
  a WARNING log line. The caller's fence is the stricter of its loaded config (a managed worker's frozen session
  config) and its live `config.yaml`, so a fence added after a session opened still binds it; a broken live file
  that mentions `board_fence` fails closed.
- Rule 3, `_fence_handoff` in `_handle_create` (assignee, `triage=true`) and `_handle_request_review` (reviewer),
  in every context including dispatched workers: a fenced profile hands a card only to a profile whose own file
  is fenced to the same board, and creates no triage cards. Reviewed list of tool parameters that name a profile:
  `test_schema_guard_every_parameter_that_names_a_profile_was_reviewed` (fails when upstream adds one).
- Rule 4, `gateway/session_kanban.py::build_kanban_policy`: after the claim checks, the owner profile's file
  (`authority.profile_id`/config.yaml) is read; fenced and the card's board differs → `RuntimeStoreError(
  'board_fenced', field='board', detail=...)`, invalid → `board_fence_invalid`. No session, no `owner_admitted`;
  the attempt fails like any owner refusal (`failure_limit` blocks the card). An unparseable file that does not
  mention `board_fence` keeps the upstream path. `hermes_cli/kanban_worker_client.py::_refusal_line` prints the
  owner's `field`/`detail` beside the reason (`Kanban owner refused: board_fenced (board: card on board ...)`),
  which becomes the run's error text; same exit code 1. Other refusals without a detail read as before.
- Rules 5-6, `gateway/slash_commands.py::_handle_kanban_command`: `/kanban` answers only when the bot-owning
  profile (resolved like `slash_access.policy_for_runner_source`: transport profile under multiplex, a served
  profile only when its config is loaded, else the launch home) sets `chat_command: true` and is not fenced.
  Unresolvable owner or unreadable file → refused. **Deploying this turns `!kanban` off on every bot** unless its
  config sets the key. The interactive CLI `/kanban`, the TUI/ACP `slash.exec` read path, `hermes kanban` and
  the dashboard are operator surfaces and unchanged.
- Docs: `website/docs/user-guide/features/kanban.md` "Board fence" (both keys, so `tests/ci/test_docs_drift.py`
  passes) and a note on the `/kanban` paragraph.
- Rollback: with this code, `kanban.board_fence: "*"` gives a profile the upstream tool behaviour (unset is NOT
  upstream: a profile with the kanban toolset and no `board_fence` has its kanban tools refused, rule 0), and
  `kanban.chat_command: true` reopens `/kanban` on a bot that is not fenced. Or reset the branch to the commit
  before this patch (older code ignores both keys) and restart the gateways one at a time.
- Tests: `tests/tools/test_kanban_board_fence.py`, `tests/gateway/test_kanban_board_fence_gateway.py` (87 of 106
  fail on the bare branch; the 19 that pass are the cases that keep upstream behaviour), plus
  `tests/hermes_cli/test_kanban_notify.py` (its two `/kanban create` tests now opt in with `chat_command: true`),
  `tests/ci/test_docs_drift.py` and the kanban suite (`tests/**/test_*kanban*.py`).

## `/usage reset` is admin-only (SEC2)

- `gateway/slash_commands_status.py::_handle_usage_command`: at the top of the `wants_reset` block, a sender who is
  not an admin under `slash_access.policy_for_runner_source` (the bot-owning profile's `allow_admin_from` /
  `group_allow_admin_from`) gets `⛔ /usage reset is admin-only here.` and nothing is redeemed. Why: the slash gate
  checks the command name only, so with `usage` in `user_allowed_commands` any staff user could type
  `!usage reset` and spend the banked Codex rate-limit reset of the one login every bot shares (ai-workflow
  `specs/SEC2-slash-admin-only.md`, decision F26 Q4 B). Plain `/usage` is unchanged. Inert while a scope has no
  admin list (`is_admin` is then true for everyone).
- Rollback: reset the branch to the commit before this patch and restart the gateways.
- Tests: `tests/gateway/test_usage_command.py::TestUsageReset` (admin redeems, non-admin refused with no redeem,
  plain `usage` works for a non-admin; the redeem is always a stub).

## Cron runs declare their own failures; `cron.declared_failure_delivery`

- Hint: the FAILURE clause of `cron/scheduler_prompt.py::_CRON_HINT` now tells the model to put `[CRON_FAILURE]`
  alone on line 1 (literal ASCII, never translated) whenever the run could not produce or deliver its required
  result: a script/command it depends on failed, exited non-zero or timed out; a required file, sheet, report or
  message was not produced or not sent; a delegated child failed. Explanation follows in the job's language. Not
  for "nothing new" (`[SILENT]`) or partial results the task allows. Upstream only mentioned a failed delegated
  child, so models recorded failed runs as `ok`.
- Config `cron.declared_failure_delivery` (`cron/scheduler.py::_declared_failure_delivery_mode`, default
  `notice` in `hermes_cli/config_defaults.py`; unknown values = `notice`): `notice` = upstream
  (`generic_failure_notice` English wrapper + `_failure_streak_nudge`); `verbatim` = deliver the lines after the
  marker as-is. One branch in `_compose_run_delivery`, which both the normal tail (`run_one_job`) and receipt
  recovery (`cron/scheduler_authority.py`) use. A bare marker (no explanation) still gets the notice.
- Unchanged in both modes: `last_status` error, `failure_streak`+1, incident upsert + `alerted` marking, the
  acked/alerted-cooldown withhold (a repeat of the same normalized explanation within
  `cron.failure_repeat_alert_hours`, default 6 h, is not delivered), `failure_deliver` routing, `[SILENT]`,
  marker only on line 1, runtime failures (summarizer copy).
- Fleet: `declared_failure_delivery: verbatim` in every config.yaml (backups `config.yaml.bak-cronfail-20261118`).
- Rollback: remove the key (backups) for upstream delivery with the broadened hint; or reset the branch to the
  commit before this patch and restart the gateways (old code ignores the key).
- Tests: `tests/cron/test_cron_declared_failure_delivery.py` (6 fail with the hint reverted and verbatim
  disabled), plus `tests/cron/` (all), `tests/ci/test_docs_drift.py` (key documented in
  `website/docs/user-guide/features/cron.md`).

## Shared skills read-only for bots (`skills.external_writable`)

- Config `skills.external_writable` (default `true` in `hermes_cli/config_defaults.py` = upstream; strings
  `false/no/off/0` = false), read per call (`tools/skill_manager_tool.py::_external_writable`).
- `false`: `skill_manage` refuses every write to a skill under `skills.external_dirs`
  (`_is_shared_skill_path`: `get_external_skills_dirs()` only; project skill dirs keep upstream behaviour). Guard points:
  `_locate_for_write` (edit, patch incl. supporting files and full rewrite, delete, write_file, remove_file) and
  `_create_skill` (target dir inside a shared dir via `skills.create_dir`; a name taken by a shared skill gets a
  rename hint). Covers foreground turns, the background review fork, the curator's LLM pass, batches
  (`skill_manager_batch` replays ops through `skill_manage`; a refused op rolls the batch back), `/skills approve`
  and dashboard edits. Refusal (`_shared_skill_refusal`) says it is not temporary, the skill is shared and
  read-only for bots, not to retry, and to save the lesson in a local skill (create, or `write_file` a reference).
- Not gated: docker file tools/terminal (skills and external dirs are mounted `:ro` in sandboxes), the default
  profile's local backend (host shell), hub/`hermes skills`/curator CLI (operator), `sync-skills.sh`. Reads
  (`skill_view`, index, cron `skills:`) unchanged. Upstream curator archive/adopt already refuse external dirs.
- Why: bots patched shared `erp-cli` dozens of times in one chat (2026-11-18) and grew shared references that
  every bot loads; edits to the git-synced `skills-4handy` were silently reset every 10 min.
- Fleet: `external_writable: false` in every config.yaml (backups `config.yaml.bak-sharedro-20261119`).
- Rollback: delete the key / restore backups (per call, no restart); or reset to the commit before this patch and
  restart (old code ignores the key).
- Tests: `tests/tools/test_skill_external_readonly.py` (18 fail with the guard forced off), plus the
  skill_manager / skill_usage / curator / background-review suites and `tests/ci/test_docs_drift.py`.

## Cron runs retry a transient error before declaring failure

- Hint: a RETRY clause in `cron/scheduler_prompt.py::_CRON_HINT`, between SILENT and FAILURE: on a transient
  error (timeout, HTTP 429/5xx, `rateLimitExceeded`, connection reset) wait briefly and retry the same call up to
  2 more times before treating it as a failure. Never retry auth/permission errors (401, a 403 that is not a rate
  limit), validation errors or refusals (read-only shared skill, `AGENT_TOKEN_DENIED`). Prompt only, no config.
- Why: 7 cron runs on 2026-11-19/20 ended `[CRON_FAILURE]` after one transient error (e.g. a 90 s query timeout
  at call 22 of 150, a Gmail 403 `rateLimitExceeded`).
- Rollback: reset the branch to the commit before this patch and restart the gateways.
- Tests: `tests/cron/test_cron_retry_hint.py`, plus `tests/cron/` (all).

## Per-job cron toolsets follow a left-core migration's `replaces_toolsets`

- `cron/scheduler.py::_add_migrated_replacements`, called from `_resolve_cron_enabled_toolsets` for a per-job
  `enabled_toolsets` list (before the MCP merge): for every `LEFT_CORE` feature with `replaces_toolsets`, when
  the job names a replaced toolset, the home ran that feature's scope conversion (`_left_core_scoped`) and the
  replacement is on there (not in `agent.disabled_toolsets`, not off for `cron` in `known_plugin_toolsets`),
  append the feature's `toolsets`. Generic; today only camofox (`browser` -> `camofox`).
- Why: the camofox conversion (`_turn_off_replaced`) put `browser` in `agent.disabled_toolsets` in 49 profiles
  and rewrote saved platform lists, but never per-job lists, so a job listing `browser` silently had no browser
  tools. 13 jobs were fixed by hand on 2026-11-19 (backups `jobs.json.bak-camofox-20261119`); this covers jobs
  created or edited later. jobs.json is not rewritten; the replaced name stays (it is disabled anyway).
- Unchanged: a home without the conversion (2 profiles), a job already listing the replacement, an explicit
  empty list, jobs without a per-job list, `_resolve_cron_disabled_toolsets`.
- Rollback: reset the branch to the commit before this patch and restart the gateways.
- Tests: `tests/cron/test_cron_migrated_toolsets.py` (3 fail with the call removed), plus `tests/cron/` (all)
  and `tests/hermes_cli/test_left_core_camofox.py`.

## Lifecycle guard: unscannable data files named in a command no longer fail closed

- `cron/lifecycle_guard.py`: referenced-script candidates carry a kind. `program` = handed to a shell
  (`source x`, `. x`, `bash|sh|... x`, also behind `sudo`/`env`/`timeout`): an unscannable one (over 1 MiB,
  budget) still always blocks, whatever its extension or mode. `exec` = a bare path in command position: an
  unscannable one blocks only when it is executable or its mode is unknown (`_local_data_file`: `stat`, no
  open; `_remote_data_file`: new optional `scan_gateway_lifecycle(is_remote_executable=...)` probe, charged to
  the remote-read budget). Inside a kernel-executed program whose shebang names a non-shell interpreter
  (`_is_non_shell_program_text`), paths are walked as mentions (read and scanned when readable, never a
  fail-closed verdict); the program itself and `bash x` (which ignores the shebang) stay executed.
- `tools/terminal_tool_guards.py`: `_is_executable_for_guard` = `test -x` in the backend only (unknown = `None`),
  passed only for a backend that is not the host (`env_type != "local"`) and only when the loaded guard module
  accepts it (`_lifecycle_scan_kwargs`), so a running gateway that imported the older guard never gets a
  TypeError. With a probe, the backend's mode is the only authority, also for a path the host has too (the host
  may hold a different file, e.g. `/etc/shadow`; review P14 F1); without one (local backend, cron script
  pre-check) the host `stat` decides.
- Why: Python/JS code tokenized as shell (`python - <<'PY'` bodies not masked because the line has `$(`,
  `node -` heredocs, `python -c` inside `$(...)`, a node bundle's path strings) put JSON data paths in command
  position; a >1 MiB file (or the 64 remote reads) failed the guard closed and blocked the whole command. 14
  blocks 15-20/11 (kyduyenbot 10 incl. its e-invoice cron, long_d2_bot 2, baolinhbot 1, huyhoangbot 1).
- Only relaxes verdicts: a readable referenced file is scanned exactly as before; literal lifecycle commands in
  any text still block.
- Rollback: reset the branch to the commit before this patch (cron workers load it from disk at once; chat
  turns after a gateway restart).
- Tests: `tests/cron/test_lifecycle_guard_data_args.py` (33; the 20/11 shapes, programs and bypasses), plus every
  test file naming the guard, `terminal_tool_guards` or `shell_heredoc`. Upstream test changed:
  `test_lifecycle_guard_heredoc_walk.py::test_unquoted_heredoc_body_path_still_walked` now makes its 2 MiB file
  executable (the walk still sees the body and fails closed); its 0644 twin passes as data.

## Dropped at v0.23.1 (upstream has equivalent tested behaviour)

| v0.19.1 local patch | Upstream replacement in v0.23.1 |
|---|---|
| 54638f8892 cross-loop plugin adapter delivery | `tools/send_message_tool.py::_dispatch_on_gateway_loop` runs the live adapter send on the gateway loop; `tests/tools/test_send_message_cross_loop.py` |
| 97a7033d6e prompt probe shares the default terminal env | `agent/prompt_builder.py::_run_backend_probe` uses the shared container-config shaper and force-removes the throwaway `prompt-backend-probe` sandbox (9640f8e1f8); `tests/agent/test_prompt_builder.py::test_probe_remote_backend_tears_down_its_sandbox` and siblings. Difference: one short-lived probe container per (profile, cwd) per process instead of zero |
| 88061bde40 updater rebases instead of reset | upstream merges on a custom branch / parks rescue refs; replaced by the guard above |
| uncommitted `[CRON_FAILURE]` | `cron/scheduler.py::_cron_failure_marker_error` (+ prompt text in `cron/scheduler_prompt.py`); `tests/cron/test_run_one_job.py::test_run_one_job_agent_declared_failure_*`, `test_run_one_job_marker_mentioned_in_report_stays_successful`. Differences: marker must be the literal first line (no leading blank line/spaces), English notice text, repeats of the same evidence are de-duplicated for `cron.failure_repeat_alert_hours` (default 6, `0` = every run) |
| `package-lock.json` churn | dropped: v0.23.1 has no `package-lock.json` (pnpm workspace); the churn was npm adding `"peer": true` flags |
| fork `feat/mattermost-known-command-prefix` | adapter-only; carried by the override plugin (036dcda), nothing in core |
