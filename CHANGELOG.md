# Changelog

All notable changes to abf-mod-editor-mcp. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.5.0] - Unreleased

### Added

- `bp_apply` asset-spec key `class_manager_row: { asset_path, class_path }`: loads or creates the class-manager row asset
  (`ClassManagerModRowAsset`, row name equal to the asset name) and sets its class reference to the generated class of the asset,
  without touching other fields. The result gains `rows: [{ asset, created, changed }]`; a failure rolls back the staged row file
  with the rest of the call. `bpApply.features` lists `class_manager_row`, and `backend_info.capabilities.classManagerRows` is `true`.

### Changed

- `SERVER_VERSION` is `2.5.0`.

## [2.4.0] - 2026-11-07

### Added

- `backend_info` reports `engineVersion` (read from the editor's `Engine/Build/Build.version`, `null` when unavailable) and the
  capability keys `reflect`, `compileErrorNodeRefs`, `functionLocals`, `userPureFunctions`, `createKinds`, `gameTest`, `audit` and
  `publish` next to `pinTypes`, `containerTypes`, `warmSession` and `stageAudit`. See ABFC.md.
- `reflect` takes a `meta` argument; `meta: false` (or the environment variable `ABF_REFLECT_META=0` when the argument is absent)
  skips the function tooltip and display-name read. The result and `reflect.json` carry `meta: "read" | "skipped"`.
- Work merged since 2.3.0 that had no version of its own:
  - wildcard pin types resolved in one `bp_apply` pass (`pin_types`, `cast_object_pin`);
  - opt-in abfc tools (`ABF_ABFC_TOOLS=1`): `reflect`, `abfc_extensions` and `warm_session`;
  - compile errors that point at a node (`node_refs`, `messages[]` in `bp_compile_report`);
  - `mod_audit` on the staged copy, with the picker redirected to a private home;
  - publishing into an open editor (live reload, dependency-ordered referencer reload);
  - map and set container types, and `UserDefinedStruct` authoring through `struct_fields`;
  - audit login fail-fast;
  - readback verification of applied Blueprints;
  - event tooltip, category and keyword metadata in `bp_apply` (`event_meta`);
  - function metadata (tooltip, display name) in `reflect`.

### Changed

- `reflect` reads function metadata after all template runs instead of before them.
- `SERVER_VERSION` is `2.4.0`.

### Fixed

- An intermittent access violation seen once during the function-template chunk of `reflect`: the metadata read now happens after the
  templates and can be skipped with the switch above.
- `bp_apply` judges success by the final compile only.
- The failed-restore test passes on Python 3.11.

## [2.3.0] - 2026-10-31

### Added

- Hidden-process Blueprint tools: `staging_sync`, `bp_apply`, `bp_read`, `bp_compile_report`, `bp_game_test`, `bp_publish`,
  `mod_audit` and `hidden_editor_shot`, which need no editor window, mouse or keyboard.

## [2.2.0] - 2026-10-29

### Added

- Initial release: an MCP server for the Abiotic Factor Mod Editor (status, rows, Python, Blueprint paste, compile, screenshots,
  input, maps, Play In Editor, upload checks).
