# abfc extensions

Additions for [abfc](https://github.com/EmekC/abfc) (TypeScript to Blueprint compiler). This document describes backend protocol 1 as
of server version 2.4.0 (abfc's minimum). The additions are additive: nothing in the existing tools changes except extra keys in the
results below.

`backend_info` is a core tool (always registered, no editor needed); see the section on it below.

## Opt-in: `ABF_ABFC_TOOLS=1`

Three tools are registered only when the server starts with `ABF_ABFC_TOOLS=1` (abfc sets it when it spawns the server):
`abfc_extensions`, `reflect` and `warm_session`. Without the variable the tool list is exactly the stock one.

### `abfc_extensions` (no arguments, no editor)

```json
{ "extProtocol": 1, "serverVersion": "2.6.0", "python": "3.11.9",
  "features": ["reflect", "compile_node_refs", "bp_read_seconds"] }
```

### `reflect`

`reflect(mod, classes, structs = [], cast_targets = [], meta = true, timeout_seconds = 900)`. Hidden editor on the staging copy, read only.
Paths must be `/Script/...`, `/Game/...` or `/<mod>/...` (anything else is refused on the host before an editor starts).
Classes are loaded with `unreal.load_class` (never `load_asset`, never an export of a `/Game` asset). Returns:

```json
{ "ok": true, "run": "<run folder>", "seconds": 41.2, "file": "<run>/reflect.json",
  "counts": { "classes": 2, "functions": 310, "properties": 120, "structs": 1, "errors": 0 }, "meta": "read",
  "filter": { "content_version": "...", "rules": [["Allow", "<key>"], ["Block", "<key>"]] } }
```

or `{ "error", "run", "log_tail" }`. `<run>/reflect.json`:

```json
{ "engineVersion": "...", "meta": "read",
  "classes": { "<path>": { "meta": "<class of the class object>", "ancestors": ["<native ancestor class paths>"],
                           "functions": [{ "name": "...", "pins": [Pin],
                                           "meta": { "tooltip": "<text or null>", "displayName": "<text or null>" } }],
                           "properties": [{ "name": "...", "pins": [Pin] }],
                           "castPin": "<cast result output pin name or null>" } },
  "structs": { "<path>": { "fields": [Pin] } },
  "errors": ["..."] }
```

A `Pin` is `{ name, dir: "in"|"out", category, subCategory, subCategoryObject (inner path or null), objectMeta, container, hidden, default }`.
Classes that appear only in `cast_targets` get an entry with empty member lists, their `castPin` and `"castOnly": true`.
Per-member failures go to `errors` and do not abort the run. `ExecuteUbergraph_*` and `*__DelegateSignature` functions are skipped.
Blueprint parent chains are not walkable from Python, so `ancestors` lists native wrapper classes only.

**Function metadata and the `meta` switch.** The top-level `meta` of the result and of `reflect.json` is `"read"` or `"skipped"`.
When it is read (the default), each function entry gets `meta: { tooltip, displayName }` from the `ToolTip` and `DisplayName` tags
(`unreal.EditorAssetLibrary.get_metadata_tag`; a tag that is missing or unreadable gives `null`). The read happens after every template
run, because it is the part of `reflect` that once crashed the hidden editor (one access violation, not reproduced). It is skipped with
the argument `meta: false`, or, when the argument is absent, with the environment variable `ABF_REFLECT_META=0` on the server; the
argument wins over the variable. Skipped means every function has `meta: null`.

## `backend_info` (2.4.0)

Core tool, no editor needed, no arguments. Keys:

- `protocol`: backend protocol number (`1`).
- `serverVersion`: this server's version (`"2.6.0"`).
- `engineVersion`: `Major.Minor.Patch` from `<editor root>/Engine/Build/Build.version`, `null` when the file is missing or unreadable.
- `editorRoot`, `headlessDir`, `documentsDir`: the paths the server uses (`editorRoot` is `null` when no editor install was found).
- `capabilities.pinTypes`: `bp_apply` node specs accept `pin_types` (wildcard pin resolution in one pass).
- `capabilities.containerTypes`: `{ array, map, set }`, which container types the type grammar accepts.
- `capabilities.warmSession`: `true` exactly when `ABF_WARM_SESSION=1`.
- `capabilities.stageAudit`: `mod_audit` audits the staged copy.
- `capabilities.reflect`: `true` exactly when `ABF_ABFC_TOOLS=1` (the `reflect` tool is registered).
- `capabilities.compileErrorNodeRefs`: compile messages carry `node` and `graph` (`bp_compile_report`, `bp_apply`).
- `capabilities.functionLocals`: `false`; `bp_apply` cannot add local variables to a function graph.
- `capabilities.userPureFunctions`: `false`; `bp_apply` cannot mark a user function pure.
- `capabilities.createKinds`: the asset kinds `bp_apply` can create (`Blueprint`, `WidgetBlueprint`, `BlueprintInterface`, `UserDefinedStruct`).
- `capabilities.gameTest`: `bp_game_test` is available.
- `capabilities.audit`: `mod_audit` is available.
- `capabilities.publish`: `bp_publish` is available.
- `capabilities.classManagerRows` (2.5.0): `bp_apply` accepts the asset-spec key `class_manager_row` (below).
- `capabilities.gameplayTags` (2.6.0): `class_manager_row` accepts `gameplay_tag` (bp_apply feature `class_manager_row_tag`) and `reflect`
  accepts `gameplay_tags` (below).
- `capabilities.reflectDefaults` (2.6.0): `reflect` function entries carry `meta.defaults` (below).
- `capabilities.assetOps` (2.6.0): the ops `bp_asset_ops` accepts, `["rename", "delete", "reparent", "recreate"]` (below).
- `capabilities.publishDeletes` (2.6.0): `bp_publish` accepts `delete_removed` (below).
- `bpApply.features`, `bpApply.nodeKinds`: the `bp_apply` feature flags and node kinds this build understands.

## Additive keys in existing tools

- `staging_status`: `stale: [rel paths]`, staged files that are untouched in the stage but differ from (or are gone from) the real
  mod, for example saved in the Mod Editor; not conflicts. `staging.diff` has the same key.
- Staged file copies wait up to 60 s for a file in use; a file still locked is a `StageFileBusy` text naming the hidden editor pids.
- `bp_apply` `variables[i].default`: string, number or bool (a bool as `true`/`false`).

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

## Class-manager rows: `class_manager_row` in a `bp_apply` asset spec (2.5.0)

```json
{ "asset": "/ExampleMod/Feature/BPC_Director",
  "class_manager_row": { "asset_path": "/ExampleMod/DataTables/DT_ClassManager/EntityDirector_Feature",
                         "class_path": "/ExampleMod/Feature/BPC_Director.BPC_Director_C" } }
```

- Validation (before anything launches): both values are required strings matching `/Folder/.../Name`; `class_path` ends in
  `.<Name>_C`; both must start with `/<mod>/` (`/Game` and other roots are refused); no other keys; not valid on a
  `UserDefinedStruct` spec.
- Execution: in the same run, after every asset of the batch is applied, compiled and saved, the row asset is loaded or, when
  missing, created as a `ClassManagerModRowAsset` (a data asset; its `row_name` is set to the asset name). Only the class reference
  of the row (`row.object_class`, set to the loaded generated class) is written; every other field keeps its value (a new row has
  the defaults of a fresh `ClassManagerData`: empty gameplay tag, release group `CORE`; `override_existing_row` stays `false`).
  The row is saved only when it was created or its class reference differed.
- Result: `rows: [{ asset, created, changed }]` (in the order of the specs that carry the key; `[]` otherwise). Any failure is an
  error of the whole call, and the staged files, including the row asset, are rolled back like any other failed `bp_apply`.
- A staged row asset is published by `bp_publish` like any other changed or added file when it is named in `assets`.

## 2.6.0: gameplay tags, reflect defaults, asset ops, publish deletions

**Gameplay tag on a class-manager row** (`capabilities.gameplayTags`). `class_manager_row` takes an optional `gameplay_tag` (a
dotted tag such as `Feature.Director`, at most 200 characters, no `/`). Before the run the host adds the tag to the stage file
`Config/Tags/<Mod>Tags.ini` (created with its header when missing; CRLF kept when the file uses it; case-insensitive dedupe) and the
row's tag is set when it differs. The result gains `tags_added` and each `rows[]` entry gains `tag`. A failed apply restores the ini.
`Config/Tags/*.ini` is part of the staged baseline, so `bp_publish` copies a changed ini like any other file (no reload). A running
warm session is stopped when tags were added, so the next run starts with the new tag list.

**`reflect` extras.** `gameplay_tags: true` adds `gameplayTags: { tags: [sorted unique names], sources: { <source>: count | "error: ..." } }`
to `reflect.json` and `gameplayTags` to `counts`. With `meta` read, each function entry's `meta` gains
`defaults: { <input pin>: "<default text>" }` from the pin's metadata, in Unreal's own lookup order (`UEdGraphSchema_K2::FindFunctionParameterDefaultValue`):
the key equal to the parameter name first (`meta=(Duration="0.2")`), then `CPP_Default_<pin>` (`null` when it could not be read; the per-pin
read is the fallback when the bulk read fails).

**`bp_asset_ops(mod, ops, timeout_seconds)`** (`capabilities.assetOps`). Edits the staged mod only (`bp_apply` still refuses these).
At most 20 ops, executed in order, all-or-nothing: on any failure the staged `Content/` and `Config/Tags/` are restored.

- `{ "op": "rename", "from": "/Mod/A", "to": "/Mod/B" }`: Unreal's own rename; refused when the target exists or the source is
  missing; referencers are fixed up (redirector removed) and the changed referencers saved (`touched`).
- `{ "op": "delete", "asset": "/Mod/A" }`: refused, naming them, while an asset outside the call's delete set still references it.
- `{ "op": "reparent", "asset": "/Mod/A", "parent": "/Script/Engine.Actor" }`: same family only (Actor, ActorComponent, UserWidget or other
  objects); compiled and verified.
- `{ "op": "recreate", "asset": "/Mod/A" }`: removes the staged files of the asset so a following `bp_apply` create makes it again.

Paths must be inside the mod (`/Game`, other mods, config assets and tooling folders are refused). Result:
`{ ok, results: [{ op, ok, detail }], touched: [...], run, error }`.

**`bp_publish(..., delete_removed = false)`** (`capabilities.publishDeletes`). With `delete_removed: true`, the files that are gone
from staging (limited to `assets` when given) are deleted from the real mod as part of the publish. Each is backed up first (listed under
`deleted` in the backup's `manifest.json`), and a real file that changed since `staging_sync` is a conflict that refuses. An open editor
releases those packages together with the published ones, and afterwards rescans their folders so its asset registry forgets them; any
failure (including the reload) restores the deleted files from the backup. A publish with only deletions is valid. The result gains
`deleted: [...]` (and `would_delete` in a dry run). Without the flag nothing is deleted and the old note stays.

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

abfc needs protocol >= 1 (a lower number is its error ABFC4001) and warns below server version 2.4.0 (ABFC4014). It reads
`backend_info`: `protocol`, `serverVersion`, `engineVersion`, `capabilities` (`pinTypes`, `containerTypes` for diagnostics,
`warmSession`, `stageAudit`, `compileErrorNodeRefs`, and the other keys above) and `bpApply.features` (`pin_types`, `cast_object_pin`,
`map_types`, `set_types`, `struct_fields`, `event_meta`, `self_event_calls`, `class_manager_row`) and `bpApply.nodeKinds`. `pinTypes` is on only when the
tool lists `pin_types`; otherwise (including a backend without `backend_info`) abfc uses a two-phase route: apply, read the rolled-back
`final_<i>.t3d`, patch the wildcard pins, re-apply as a raw graph. Everything `backend_info` does not report comes from `tools/list`
(for example whether `reflect` is registered), from `abfc_extensions` and from a static `bp_apply` protocol-1 baseline inside abfc. A
backend without `backend_info` is not an error for abfc (warning ABFC4012).
