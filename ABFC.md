# abfc extensions

Additions for [abfc](https://github.com/EmekC/abfc) (TypeScript to Blueprint compiler), protocol 1. They are additive: nothing
in the existing tools changes except extra keys in three results (below).

## Opt-in: `ABF_ABFC_TOOLS=1`

Three tools are registered only when the server starts with `ABF_ABFC_TOOLS=1` (abfc sets it when it spawns the server).
Without the variable the tool list is exactly the stock one. None of them is named `backend_info`; that tool belongs to the
wildcard-pins branch and is read by abfc when present.

### `abfc_extensions` (no arguments, no editor)

```json
{ "extProtocol": 1, "serverVersion": "2.3.0", "python": "3.11.9",
  "features": ["reflect", "compile_node_refs", "bp_read_seconds"] }
```

### `reflect`

`reflect(mod, classes, structs = [], cast_targets = [], timeout_seconds = 900)`. Hidden editor on the staging copy, read only.
Paths must be `/Script/...`, `/Game/...` or `/<mod>/...` (anything else is refused on the host before an editor starts).
Classes are loaded with `unreal.load_class` (never `load_asset`, never an export of a `/Game` asset). Returns:

```json
{ "ok": true, "run": "<run folder>", "seconds": 41.2, "file": "<run>/reflect.json",
  "counts": { "classes": 2, "functions": 310, "properties": 120, "structs": 1, "errors": 0 },
  "filter": { "content_version": "...", "rules": [["Allow", "<key>"], ["Block", "<key>"]] } }
```

or `{ "error", "run", "log_tail" }`. `<run>/reflect.json`:

```json
{ "engineVersion": "...",
  "classes": { "<path>": { "meta": "<class of the class object>", "ancestors": ["<native ancestor class paths>"],
                           "functions": [{ "name": "...", "pins": [Pin] }],
                           "properties": [{ "name": "...", "pins": [Pin] }],
                           "castPin": "<cast result output pin name or null>" } },
  "structs": { "<path>": { "fields": [Pin] } },
  "errors": ["..."] }
```

A `Pin` is `{ name, dir: "in"|"out", category, subCategory, subCategoryObject (inner path or null), objectMeta, container, hidden, default }`.
Classes that appear only in `cast_targets` get an entry with empty member lists, their `castPin` and `"castOnly": true`.
Per-member failures go to `errors` and do not abort the run. `ExecuteUbergraph_*` and `*__DelegateSignature` functions are skipped.
Blueprint parent chains are not walkable from Python, so `ancestors` lists native wrapper classes only.

## Additive keys in existing tools

- `bp_read`: `seconds` (wall time of the export run).
- `bp_compile_report`: `messages: [{ severity: "error"|"warning", asset, message, node, graph }]` next to the existing keys.
  `node` and `graph` come from `from Source: /Mod/X/BP.BP:<Graph>.<Node>` in the compile log when the log has it. Observed on the
  5.4.4 kit: the log ends in `from Source: <asset path>` only, so the node is taken from the compiled asset instead: the compiler stores
  each message on its node (`ErrorMsg=` in a T3D export), and the host matches the log message to such a node (equal text after
  whitespace normalization, else one contains the other; every node is used once, in export order; no match leaves `node`/`graph`
  `null`, and the entry gains `node_from: "export"` when it matched). `bp_compile_report` has the compile job export every compiled
  Blueprint to `<run>/compile_<i>.t3d` for this (argument `node_refs`, default true, `false` skips the exports); `bp_apply` reads the
  `final_<i>.t3d` it writes anyway. Unreal compile errors thus point at a node in `compile.errors[]` of `bp_apply` as well.
- Compile entries (`headless.parse_compile_log`, also in `bp_apply` and `mod_audit` results): an entry with a source is
  `{ asset, message, source, graph, node }`; without a source it stays exactly `{ asset, message }`. Messages are deduplicated by
  severity, asset, text and source, so the same text on two different nodes is kept twice.

## `mod_audit` on the stage (`backend_info.capabilities.stageAudit`)

`mod_audit` always audits the staged copy (`audited_copy: "stage"`; the optional `stage` argument is accepted and ignored, there is no
audit of the real mod). Failed first runs showed the Mod Browser picker listing the user's own projects from
`Documents\Abiotic Factor Mods` next to our recent entry, so the first-tile click mounted another mod (the log then names
`Registered mod plugin '<other>'` and the job waits for its mod until timeout). Now:

- The hidden editor gets `USERPROFILE` pointing at `<runtime root>/home` (Windows resolves Documents from `%USERPROFILE%`), with an empty
  `Documents\Abiotic Factor Mods`, so the picker can only offer the staged mod written as the only recent entry. `ABF_PICKER_HOME=0`
  switches the redirect off. The user's `-userdir` is unchanged (still the private one).
- **`<runtime root>/home/AppData/Local` and `.../Roaming` are junctions to your REAL `%LOCALAPPDATA%` and `%APPDATA%`.** Without them
  the editor stalls at startup (nothing logged after the Zen line; the hang was in the DDC/Zen local path init). Never delete the
  runtime root or its `home` folder recursively (PowerShell 5.1 `Remove-Item -Recurse`, old Python `shutil.rmtree`, `rd /s` through a
  tool that follows links): it can wipe your real AppData. Remove the two links first (`rmdir "<root>\home\AppData\Local"`, same for
  `Roaming`: `rmdir` without `/s` only removes the link) or call `hiddendesk.remove_home()`. The default runtime root lives inside
  `%LOCALAPPDATA%`, so the Local link also makes a directory cycle (`Local\...\home\AppData\Local\...`): any tool that walks the
  runtime root and follows junctions loops (`os.walk` does not treat a junction as a symlink on Windows, so it may follow it). The
  backend itself never walks the runtime root (it lists `runs/` and walks stage and mod folders only); `redirect_home` creates the
  links, nothing deletes them but `remove_home`.
  Only the whole-folder links are proven (one probe, run-log step 20); narrowing them to the subfolders the editor needs
  (for example `AppData\Local\UnrealEngine`) is untested and needs a live check.
- After each click the editor log is checked: a `Registered mod plugin` line naming another mod ends the attempt at once with
  `error: "picker mounted <other> instead of <mod>"`; the whole run is retried once, then fails with that reason.
- Success results carry `mounted: "<mod>"` (also from `hidden_editor_shot`'s runner).

## Warm session (`ABF_WARM_SESSION=1`)

Opt-in, off by default. With `ABF_WARM_SESSION=1` the server keeps one hidden `-run=pythonscript` commandlet alive per staged mod
(`editor/bpkit/warm.py`, started through `editor/warm_loop.py`) and sends it the commandlet ops `apply`, `export` (`bp_read`),
`compile` (`bp_compile_report`, the `bp_publish` gate) and `reflect`. This saves the editor start of every call. `bp_game_test`
(`-game`) and `mod_audit` (hidden editor) always run cold.

- Protocol (all under `<runtime root>/warm/<mod>/`): the host writes `queue/<seq>-<id>.json` = `{id, job, run}`, where `job` is the
  same `job.json` a cold run reads. The loop reloads the mod's loaded packages from disk first (it rescans `/<Mod>`, reloads every
  loaded package with `EditorLoadingAndSavingUtils.reload_packages(..., ASSUME_POSITIVE)` and unloads the ones whose file is gone;
  `_McpTmp` is left alone), runs the job through the cold dispatcher, writes `<run>/result.json`, then `<run>/warm.done`, then removes
  the queue entry. `ready.json` marks a started loop, `stop.json` asks it to exit; it also exits after `ABF_WARM_IDLE_SECONDS` (default 600)
  without a job. The session log is shared; each job is bracketed by `ABFMCP_WARM_BEGIN/END <id>` lines and the host copies that part
  to `<run>/editor.log`, so log parsers see the same text as for a cold run. The text from the BEGIN marker up to the `ABFMCP_WARM_RELOAD`
  marker (the reload phase, which logs stale-class errors a cold run never has) goes to `<run>/reload.log` instead; its error lines that
  are not the known stale-class messages ("is not compatible with", "doesn't match the property") are reported as `reload_errors` in the result.
- The reload result is checked before the job runs: a non-empty error text, a plain `False` or an exception from `reload_packages` /
  `unload_packages` fails the job with `ABFMCP_WARM_RELOAD_FAILED`, and the host kills the session and runs the job cold
  (`warm_fallback: "warm reload failed: ..."`), because a compile of stale in-memory packages could report clean. If the END marker never
  reaches the log within 10 s the log copy may be cut off: a read-only op runs cold (`warm log incomplete`), an `apply` is reported as
  failed (`warm_failed: "warm log incomplete"`) and the caller restores the stage.
- Same `-userdir` as cold runs, every call serialized (session mutex plus the run lock). A cold launch (`bp_game_test`, a cold fallback),
  `mod_audit`, `hidden_editor_shot` (both through `hiddendesk.run_editor`, before the run lock) and `staging_sync` stop the session first,
  because it holds the user dir and the staged packages.
- Fallback per job: if the process fails to start, is dead, or stays silent for job timeout + 10 s, the process tree is killed and the
  job runs cold (result `warm: false`, `warm_fallback: "<reason>"`). Exception: an `apply` that was already handed to a process that then
  died or hung is reported as failed (`warm: true`, `warm_failed`), never rerun, because its files may be half written; the caller's
  rollback restores them.
- Results of `bp_apply`, `bp_read` and `bp_compile_report` gain `warm: bool`. A warm job also reports `warm_start_seconds` (when it had to
  start the process) and `total_seconds` in the internal result.
- `backend_info.capabilities.warmSession` is `true` exactly when `ABF_WARM_SESSION=1`.
- Tool `warm_session` (registered with the other opt-in tools): `{action: "start"|"stop"|"status", mod?}` returns
  `{running, pid, jobs, startedAt, mod, sessions: [...]}`; `start` needs `mod`; `stop` also reports `stopped`. Without
  `ABF_WARM_SESSION=1` it returns an error.

## Backend contract abfc relies on (summary)

abfc reads `backend_info` when the backend has it (the wildcard-pins branch): `protocol`, `capabilities.pinTypes`,
`capabilities.containerTypes` (kept for diagnostics only), `bpApply.features` (`pin_types`, `cast_object_pin`, `map_types`) and
`bpApply.nodeKinds`. `pinTypes` is on only when the tool lists `pin_types`; otherwise (including a backend without `backend_info`)
abfc uses a two-phase route: apply, read the rolled-back `final_<i>.t3d`, patch the wildcard pins, re-apply as a raw graph.
Everything `backend_info` does not report comes from `tools/list`, from `abfc_extensions` and from a static bp_apply protocol-1
baseline inside abfc. A backend without `backend_info` is not an error for abfc (warning ABFC4012).

## Applying together with the wildcard-pins branch

This branch edits `server.py` (one block after `TOOLS.extend(headless_tools.build(...))`), `editor/bpkit/jobs.py`, `headless.py` and
`headless_tools.py`, and adds `abfc_tools.py`, `editor/bpkit/reflect.py`, `editor/bpkit/reflectparse.py`, `tests/test_abfc_backend.py`
and this file. The wildcard-pins branch edits `README.md`, `editor/bpkit/graphs.py`, `editor/bpkit/specs.py`, `editor/bpkit/t3d.py`,
`server.py` and `tests/test_unit.py`. Only `server.py` overlaps, in separate hunks, so both apply with plain `git apply` in either order.
The wildcard branch's smoke test asserts the stock tool count, which is why the tools here are opt-in.
