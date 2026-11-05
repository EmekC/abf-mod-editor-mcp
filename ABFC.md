# abfc extensions

Additions for [abfc](https://github.com/EmekC/abfc) (TypeScript to Blueprint compiler), protocol 1. They are additive: nothing
in the existing tools changes except extra keys in three results (below).

## Opt-in: `ABF_ABFC_TOOLS=1`

Two tools are registered only when the server starts with `ABF_ABFC_TOOLS=1` (abfc sets it when it spawns the server).
Without the variable the tool list is exactly the stock one. Neither tool is named `backend_info`; that tool belongs to the
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
  `node` and `graph` come from `from Source: /Mod/X/BP.BP:<Graph>.<Node>` in the compile log and are `null` without a source.
- Compile entries (`headless.parse_compile_log`, also in `bp_apply` and `mod_audit` results): an entry with a source is
  `{ asset, message, source, graph, node }`; without a source it stays exactly `{ asset, message }`. Messages are deduplicated by
  severity, asset, text and source, so the same text on two different nodes is kept twice.

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
