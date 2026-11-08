# abf-mod-editor-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI coding assistant (Claude Code, Codex, Cursor, anything that speaks MCP) drive the official **Abiotic Factor Mod Editor** — the free Unreal-based editor Deep Field Games ships through the Epic Games Launcher.

With it, the assistant can open a mod, read and edit item/recipe/buff rows, create new rows copied from base-game ones, paste Blueprint graphs, compile, look at the editor through per-window screenshots, click and type in it, load a map, run Play In Editor, read the player's state, and check a mod against the same Blueprint filter the upload audit enforces — all from a chat.

Pure Python, no packages, Windows only (the editor is Windows only). Not affiliated with Deep Field Games, Playstack or mod.io.

New to this? There's a step-by-step setup and usage guide on mod.io: [Use an AI assistant with the Mod Editor (MCP setup guide)](https://mod.io/g/abiotic-factor/r/use-an-ai-assistant-with-the-mod-editor-mcp-setup-guide).

Changes per version are listed in [CHANGELOG.md](CHANGELOG.md).

## Requirements

- Windows 10/11
- The Abiotic Factor Mod Editor installed from the Epic Games Launcher
- Python 3.9 or newer on the PC (the editor's own Python is used inside the editor; this one runs the server)
- An MCP client. Claude Code is registered automatically; others get a command to paste.

## Install

```bash
git clone https://github.com/EmekC/abf-mod-editor-mcp.git
cd abf-mod-editor-mcp
python install.py
```

`install.py`:

1. Finds the Mod Editor through the Epic Games Launcher's install manifests (every install has a different random folder suffix, so nothing is hardcoded). If it can't, set `ABF_EDITOR_ROOT` or put `{"editor_root": "..."}` in a `config.json` next to `server.py`.
2. Puts a small startup hook in `Documents\UnrealEngine\Python\` (`init_unreal.py` + `abf_mcp_startup.py`). Every Unreal editor runs that file at startup; the hook checks the engine build tag and does nothing outside the Mod Editor. If you already have an `init_unreal.py`, the hook is appended as a marked block and your file is backed up.
3. Registers the server with Claude Code at user scope (through the `claude` CLI when it's on PATH, otherwise by adding it to `~/.claude.json` with a backup). `--no-register` skips this and just prints the command for other clients.

Then **restart the Mod Editor** and start a **new** Claude Code session. Ask it to call `editor_status`.

Remove everything with `python install.py --uninstall`.

## What the hook does

The editor's Python plugin has a "remote execution" mode (UDP discovery on 239.0.0.1:6766, loopback only, then a TCP command channel the editor opens back to the client). It's compiled into the Mod Editor but off by default, and the setting doesn't survive in the editor's config file. The hook switches it on at runtime for that session and also starts a tiny **file bridge** (`%LOCALAPPDATA%\abf-mod-editor-mcp\bridge`) as a fallback for when a modal dialog blocks remote execution or another client holds its connection.

The server uses remote execution first and the bridge second, opens a fresh connection per command (so it can share the editor with other tools), and reports a command that was sent but got no reply instead of retrying it.

## Tools (43)

| Group | Tools |
| --- | --- |
| Status and launch | `editor_status` (call first), `backend_info` (protocol, version, paths, capabilities; no editor needed; capabilities for abfc, see [ABFC.md](ABFC.md)), `launch_editor`, `open_mod` (drives the picker's Browse… dialog, since the command line can't open a mod) |
| Python | `run_python`, `evaluate_python`, `run_python_file` (`args` → `ARGS` inside the script) |
| Mod data | `list_mods`, `list_mod_templates`, `find_assets`, `get_row`, `set_row` (plain values, Python expressions, or whole export text), `create_row_asset` (any of the 72 `*ModRowAsset` classes, optionally copied from a base-game row or another mod row), `delete_asset`, `save_assets`, `compile_blueprint`, `validate_assets` |
| Base-game data | `get_game_row` — list or read any row the game exposes through its `<Kind>HandleFunctionLibrary` (gear_item, weapon_item, recipe, buff_debuff, npc, pet, trait, …) |
| Blueprints | `paste_graph` (clipboard T3D → graph in one step), `inspect_blueprint` (parent, variables, graphs, every reference, errors, nodes and pins), `export_t3d`, `t3d_errors`, `blueprint_filter_check` |
| Headless Blueprints | `staging_sync`, `staging_status`, `bp_apply` (create/edit Blueprints from a JSON spec; `class_manager_row` with an optional `gameplay_tag`), `bp_asset_ops` (rename, delete, reparent, recreate on the staged copy), `bp_read`, `bp_compile_report`, `bp_game_test`, `hidden_editor_shot`, `mod_audit` (the editor's own upload audit), `bp_publish` — all in hidden processes on a staging copy, no mouse, keyboard or focus. See [Hidden Blueprint workflow](#hidden-blueprint-workflow) |
| Window | `editor_windows`, `editor_screenshot` (one editor window, returned as an image, works in the background), `editor_click`, `editor_keys`, `close_editor_window` |
| Map and play | `load_map`, `play_in_editor` start/stop, `pie_info` (players, pawns, locations, chosen pawn properties), `console_command` (editor or game world) |
| Release | `check_upload` (offline pre-upload check), `read_editor_log` |

### Safety rails built in

- Refuses to edit, save, delete or T3D-export anything under `/Game` (base content). Exporting a base-game Blueprint has crashed the editor.
- Screenshots capture one editor window, never the desktop.
- Keys and text are only sent while the editor is the foreground window.
- `delete_asset` deletes one asset per call: a delete loop crashed the editor.
- Nothing uploads to mod.io. You do that in the editor.

### `check_upload` and `blueprint_filter_check`

The editor's **Upload Mod** zips the whole project folder and mod.io rejects some of what ends up inside (for instance any `.md` file, reported only as "Unknown ApiError"). The cloud cook then recompiles Blueprints without the editor-only assets, so a reference to something that exists only in the editor fails there. `check_upload` scans the folder for both, using the file lists in `data/` (names only, from game/editor version 1.5; see `data/make_editor_list.py`).

The upload's Asset Audit checks Blueprint calls and variables against a filter list Deep Field publishes. `blueprint_filter_check` downloads that list (cached a day) and applies the same rules to a mod's Blueprints, naming each blocked call and the node it's on — before you find out one upload at a time.

Neither replaces the editor's own checks; they catch the common failures early. `blueprint_filter_check` has raised false alarms on a PlayerController cast, `Controller.IsLocalController` and `Controller.K2_GetPawn` while the editor's real audit passed those same Blueprints: prefer `mod_audit` (below), which runs that real audit on a staging copy.

## Hidden Blueprint workflow

Nine tools create, edit, wire, compile, test, audit, screenshot and publish Blueprints **without the real desktop**: no mouse, no keyboard, no focus, no clipboard, and no window on your screen. Everything runs in separate Mod Editor processes (a commandlet, a `-game -nullrhi` game, or an editor on a hidden Windows desktop) on a **staging copy** of the mod. Only `bp_publish` writes into the real mod.

What it does not touch: your running editor (unless the mod is open there and you call `bp_publish`, which then releases and reloads only the files it replaces), `/Game`, the kit install, other mods, and the mod folder itself until you publish. Hidden processes always get their own `-userdir` and a redirected `LOCALAPPDATA`.

```
staging_sync(mod_dir)        copy the mod (minus Saved, Intermediate, Binaries, DerivedDataCache, .git)
staging_status(mod)          changed / added / removed / conflicts, and stale (untouched staged files that differ in the real mod)
bp_apply(mod, assets)        create/edit Blueprints from a JSON spec (rolls back on compile errors)
bp_asset_ops(mod, ops)       rename, delete, reparent or recreate staged Blueprints (all-or-nothing)
bp_read / bp_compile_report  read the structure back, compile everything and list errors per asset
bp_game_test(mod, steps)     run steps in a hidden game and assert on state
mod_audit(mod)               the editor's own upload Asset Audit on the staged copy (the real gate)
hidden_editor_shot(...)      screenshots of asset editors on the hidden desktop
bp_publish(mod)              back up, then copy changed Content into the real mod (dry_run to preview;
                             delete_removed also deletes the files that are gone from staging; an open
                             editor first gets the mod's gameplay tags registered in memory)
reflect(..., gameplay_tags)  with ABF_ABFC_TOOLS=1: also list the gameplay tags (see ABFC.md)
```

`bp_read` gets components and variable defaults from the hidden process, because the Blueprint text export omits them. `components` rows are `name, class, parent, inherited, native, default_scene_root`, and they include the default root and inherited ones. `[]` means there are none (a class that is not an Actor); `null` with `components_status: "unavailable"` and `components_error` means they could not be read. `default` is a string; `default_status` is `ok`, `unavailable` (with `default_error`) or `not_applicable`; `defaults_status` summarises.

### Where things live

Under `%LOCALAPPDATA%\abf-mod-editor-mcp\headless`, or `ABF_HEADLESS_DIR` (**no spaces in the path**: the launch arguments are unproven with them; the tools refuse such a root). Outside the repo and outside any mod, because Upload zips the whole mod folder.

| What | Where |
| --- | --- |
| Staged mod | `<root>\stages\<Mod>\mod\<Mod>` (+ `stage.json`: baseline hashes, source folder) |
| One folder per run (job, log, result, exports, `before/` snapshot, screenshots) | `<root>\runs\<time>-<op>-<id>` (newest 20 kept) |
| Backups made by `bp_publish` | `<root>\backups\<Mod>\<time>\` with `manifest.json` (never inside the mod) |
| Hidden editor user dir | `<root>\ud` |

Logs can contain tokens: they stay in the run folders, which `.gitignore` covers, and `scrub` masks the log lines the tools return. Do not paste run folders into issues.

### `bp_apply` spec

`assets` is a list of asset specs (or `spec_file`: a JSON file holding that list or `{"assets": [...]}`). All of an asset's keys are optional except `asset`; groups run in this order, compiling after each, all in memory, then every asset is saved only if all of them applied:

```json
{"asset":"/Mod/Folder/BP_X",
 "create":{"class":"Blueprint","parent":"/Script/Engine.Actor","if_missing":true},
 "variables":[{"name":"Score","type":"int","default":"10","flags":5}],
 "remove_variables":["Old"],
 "variable_defaults":{"Score":"20","Armed":null},
 "components":[{"name":"Box","class":"/Script/Engine.BoxComponent","parent":"Mesh",
                "properties":{"component_tags":["T"]},"expressions":{"box_extent":"unreal.Vector(11,22,33)"}}],
 "dispatchers":[{"name":"OnPing","params":[{"name":"Amount","type":"int"}]}],
 "timelines":[{"name":"T1","length":1.0,"loop":false,"tracks":[{"name":"Val","keys":[[0,0],[1,10]]}]}],
 "widget_tree":{"root":{"class":"VerticalBox","name":"Root","children":[
     {"class":"TextBlock","name":"Title","text":"Hello","slot":{"Padding":"(Left=7.000000)"},"props":{}}]}},
 "animations":[{"name":"FadeIn","fps":20,"end_frame":20,"tracks":[{"widget":"Txt","property":"RenderOpacity","keys":[[0,0.0],[20,1.0]]}]}],
 "macros":[{"name":"M","inputs":[...],"outputs":[...],"nodes":[...],"wires":[...]}],
 "functions":[{"name":"AddScore","inputs":[...],"outputs":[...],"nodes":[...],"wires":[["entry.then","s.execute"]]}],
 "remove_functions":["Old"],
 "interfaces":[{"class":"/Mod/BPI_X.BPI_X_C","functions":[{"name":"Ping","inputs":[...],"outputs":[...],"nodes":[...],"wires":[...]}]}],
 "graphs":[{"graph":"EventGraph","remove":["K2Node_CallFunction_0"],
            "unwire":[["K2Node_A_0.then","K2Node_B_0.execute"]],
            "add":[{"id":"add","k":"call","class":"/Script/Engine.KismetMathLibrary","function":"Add_IntInt","set":{"B":"2"},"objects":{},"pos":[0,0]}],
            "wires":[["add.ReturnValue","K2Node_B_0.Count"]],
            "set":{"K2Node_B_0.Count":"3"},"objects":{"sp.Class":"/Mod/BP_Y.BP_Y_C"},"move":{"K2Node_B_0":[100,200]}}],
 "raw":[{"kind":"graph","graph":"X","text":"..."},{"kind":"tree","root":"R","text":"..."},
        {"kind":"tree_replace","root":"R","replace":[["Size=15.000000","Size=22.000000"]]},
        {"kind":"python","code":"..."}]}
```

- Types: `exec int int64 bool byte name string text real double float`, `object:<class path>`, `class:<path>`, `softobject:<path>`, `softclass:<path>`, `interface:<path>`, `struct:<path>`, `enum:<path>`, `array:<type>`, `map:<key>:<type>` (simple key type: `int int64 bool byte name string text real double float`). Sets, nested containers and a struct with no path are refused.
- Defaults and pin values are raw T3D strings, the same text the engine exports.
- Node ids are yours (no `.`); wires are `["id.pin","id.pin"]`, output first. Existing nodes in `graphs` are addressed by their real name (see `bp_read`). Reserved ids: `entry`, `result` in functions and interfaces; `in`, `out` in macros.
- Generated nodes are placed in columns below everything already in the graph (and the default event bubbles), unless you give `pos`.
- An `override` of an event that already exists as a disabled default node (for example `ReceiveBeginPlay`) reuses that node.
- A `call` without `class` calls the asset's own function.
- Wildcard pins: bp_apply wires by text, so the engine never infers wildcard pin types. Give them with `pin_types: {"<pin>": "<type>"}` on the node (whole pin type: `"TargetArray": "array:int"`, `"Output": "int"`, `"TargetMap": "map:name:int"`). Only wildcard pins accept it and the container must match the pin. The `Object` input of every `cast` is typed automatically.
- Order inside one `graphs` entry: `remove`, `unwire`, `add`, `wires`, `set`, `objects`, `move`.
- Refused before anything launches: asset paths outside `/<Mod>/` (including `/Game`), a `reparent` key, unknown kinds, bad wires or types. Collapsed graphs (`K2Node_Composite`) are refused at edit time.

Node kinds (`k`):

| k | needs | becomes |
| --- | --- | --- |
| `call` | `function`, optional `class` | `K2Node_CallFunction`; no `class` means the asset's own class |
| `get`, `set` | `name`, optional `class`, optional `type` | `K2Node_VariableGet/Set` (`type` declares a variable the scratch Blueprint does not know, e.g. inherited from C++) |
| `event` | `name`, optional `params` | `K2Node_CustomEvent` |
| `override` | `function`, `class` | `K2Node_Event` with `bOverrideFunction` |
| `branch`, `sequence`, `spawn`, `create_widget` | | `K2Node_IfThenElse`, `K2Node_ExecutionSequence`, `K2Node_SpawnActorFromClass`, `K2Node_CreateWidget` |
| `cast` | `target` | `K2Node_DynamicCast` |
| `call_dispatcher`, `bind_dispatcher` | `name`, `params` | `K2Node_CallDelegate`, `K2Node_AddDelegate` |
| `timeline` | `name` (defined in the same batch) | `K2Node_Timeline` |
| `message` | `function`, `interface` | `K2Node_Message` |
| `self` | | `K2Node_Self` |
| `macro` | `name`, `inputs`, `outputs` | `K2Node_MacroInstance` of a macro from the same batch |
| `node` | `class`, `props` | any node class, `props` are raw lines |

How it works, briefly: Python cannot create nodes, so text is imported through a Map import into throwaway carrier objects, and the carrier's arrays are swapped into the real graph in process memory. Pins come from a throwaway scratch Blueprint (never saved) so the engine writes them. Offsets are discovered per build and the tools refuse rather than guess.

### `bp_game_test` steps

A hidden `-game -nullrhi` run of `/Game/Maps/Empty` (or `map`). Python cannot spawn actors or create widgets there, so a harness Blueprint (`Content/_McpTest/BP_McpHarness`, built on demand, never published) does it. Steps run in order and each result is written immediately:

| step | fields |
| --- | --- |
| `new` | `class`, `as`: `new_object` in the level (calls functions, runs no components) |
| `spawn` | `class`, `as`: a real actor through the harness |
| `widget` | `class`, `as`, optional `viewport`: a widget through the harness (`add_to_viewport` when set) |
| `call` | `target` (`$name`), `function`, `args`, `as` |
| `get`, `set` | `target`, `property`, (`value`) |
| `find` | `base` (e.g. `Character`), `class_contains`, `as`, `timeout`: wait for an object |
| `wait` | `seconds` (the game keeps ticking: timelines, animations) |
| `assert` | `value`, one of `equals`, `approx` + `tolerance`, `contains`, `truthy`, `not_none`; a failure is recorded and the run continues |
| `python` | `code` with `unreal`, `ctx` (the names), `level`; set `result` |

Arguments: `"$name"` is a stored value, `{"class": path}` a loaded class, `{"text": "x"}` an `unreal.Text`. `PrintString` is not logged in this build, so read state back with `get` and `assert`.

### Publishing

`bp_publish` copies only `Content/**`, and only files that differ from the baseline taken at `staging_sync`. In order, and it stops at the first refusal with nothing changed: it refuses on **conflicts** (a real file changed since the sync; there is no force option, run `staging_sync` again), on **compile errors** in the changed assets, on **modified assets or Play In Editor** in an open editor, on **an editor that is running but not answering** (a modal dialog, a timeout, the hook missing: its unsaved state cannot be checked, so there is no file copy under it), on **assets that could not be compile-checked** (unknown to the registry or not loadable), and on **locked files**. A real file that changes while the compile check or the editor unload is running is caught again when the backup is made, and refuses. Then it backs up every file it will replace to `<root>\backups\<Mod>\<time>\` (hash-verified), copies each file through `<name>.mcpnew` and `os.replace`, and restores from the backup if anything fails. It works with the editor closed (plain file copy), or open on that mod (release the files through `unload_packages`; packages other loaded objects still use stay loaded, and after the copy they are reloaded in place with `reload_packages`, which repoints references; a file still held after the release refuses as `file locked`; loaded assets of the same mod that reference the published ones are reloaded from their unchanged files in the same call, so they are not recompiled against the old classes, and one with unsaved changes refuses the publish; a reload error restores the old files; Blueprint compile errors the editor logs for the reloaded assets are returned as `compile_errors_after` and do not restore, since the files on disk are the published ones). If a restore itself fails, the report says `restored: false` and lists the files to copy back by hand. Hidden processes run inside a Windows Job Object that kills their whole tree if the server dies. Files you deleted in staging are only reported: delete them in the Content Browser yourself.

To restore: copy the files from `<root>\backups\<Mod>\<time>\` back over the mod's `Content` (`manifest.json` lists them with hashes; added files in the manifest were new and can simply be deleted).

### `hidden_editor_shot` and `mod_audit`

These open a real editor on a hidden desktop. The Mod Browser picker comes first, so the tools write the staged mod as the only recent entry and **post window messages for a click on its first tile** (measured on build 5.4.4-1050002 at 125% DPI: Slate position 946,200). It is fragile: a kit update can move the tile. A failure is detected and reported with a capture (`picker_fail.png` in the run folder); `ABF_PICKER_TILE="x,y"` (window pixels of the tile) overrides the position. The hidden editor gets a private empty Documents folder (`USERPROFILE` redirect; `ABF_PICKER_HOME=0` turns it off) so the picker cannot list your own projects, and the log is checked after the click: if another mod was mounted the run fails with `picker mounted <other>` after one retry. Nothing here uses your cursor, keyboard or foreground window, and screenshots come only from windows enumerated on the hidden desktop.

**Warning:** with the redirect on, `<runtime root>\home\AppData\Local` and `...\Roaming` are *junctions to your real* `%LOCALAPPDATA%` and `%APPDATA%` (the editor hangs at startup without them). The default runtime root is inside `%LOCALAPPDATA%`, so the Local link also forms a directory cycle. Never delete the runtime root or its `home` folder with a recursive delete (PowerShell 5.1 `Remove-Item -Recurse`, `rd /s` via a link-following tool, an old `shutil.rmtree`): first run `rmdir "<root>\home\AppData\Local"` and `rmdir "<root>\home\AppData\Roaming"` (no `/s`; this removes only the link), or call `hiddendesk.remove_home()`.

### Startup hook

The startup hook runs in every Mod Editor process, hidden ones included, and switches on remote execution, so a hidden process could answer another MCP session's discovery pings. The jobs switch it off again at boot; after you re-run `python install.py` the hook skips hidden processes entirely (`ABF_MCP_HIDDEN=1`). Until you do, there is a small window at boot.

## Working notes

- Mod row assets are `PrimaryDataAsset`s; `row_name` defaults to the asset name and the game adds the mod prefix at runtime.
- Python can't create Blueprint nodes in this editor. The preferred route is the headless one (`staging_sync` → `bp_apply` → `bp_compile_report` → `bp_game_test` → `mod_audit` → `bp_publish`, see below): no mouse, keyboard or editor window. `paste_graph` is still there and works on the open editor, but it needs the real desktop: generate clipboard text (T3D) and paste in passes, because custom events must exist and be compiled before anything calls them, or the calls are dropped silently. Use a fresh node-name prefix per paste; deleted nodes linger in exports.
- A new mod project can't be created from Python (the template system has no entry point). Do it in the picker: ITEMS tile → template → name → Create. `editor_screenshot` + `editor_click`/`editor_keys` can drive it.
- Loading a map opens the Message Log over the viewport; `close_editor_window("Message Log")` before Play In Editor.
- The editor runs commands on its game thread: keep scripts short, and never open a modal dialog from one.

## Testing

Unit tests need no editor and touch no mod: `python -m unittest tests.test_unit -v` (stdlib `unittest`, no packages). They use fakes for the editor side, so nothing reaches a running editor.

Integration tests run hidden Mod Editor processes against a fixture mod generated under a temp folder (30+ minutes, kit installed): `ABF_RUN_INTEGRATION=1 ABF_HEADLESS_DIR=<temp folder without spaces> python -m unittest tests.test_integration -v`. They never touch your editor or your mods (`run_json` is a function that raises); the optional last test uses a copy you point `ABF_TEST_REAL_COPY` at. `test_16` really publishes into its temp mod, so it can be left out by naming the test ids you want.

`python test_client.py` speaks MCP to the server over stdio and exercises the read-only tools against the open editor. `--bridge` forces the file-bridge transport. `--row <mod row asset> <mod folder>` also edits and restores a row, creates and deletes a row asset, and runs the two checkers. Use a throwaway copy of a mod for that.

## Limits

- Built against Mod Editor 1.5.0 (engine `5.4.4-1050002+++DF+ABF`, Python 3.11). A future editor update may change the Python API; the file lists in `data/` are versioned and will need regenerating after a game update.
- Windows only.
- Headless Blueprint tools: the Mod Browser tile position used by `hidden_editor_shot` and `mod_audit` is fragile (it follows the picker layout of this build; `ABF_PICKER_TILE` overrides it). The hidden Facility map has a real pawn but no live HUD (`W_PlayerHUD_Main` is not in the viewport), so anything injected there cannot be tested hidden. `PrintString` is not logged in this build: read state back instead. Memory offsets for the Blueprint arrays are discovered per build and the tools refuse rather than guess. Pure functions are not offered (the engine rejected the generated ones). Collapsed graphs are refused, `set` types are refused, and neither component nor Blueprint reparenting is offered. Re-run `install.py` once to get the hook guard that keeps hidden processes from answering remote-execution pings.
- A mod made with these tools still has to pass the editor's Asset Audit and mod.io's cloud cook; the checkers here reduce surprises, they don't guarantee anything.

## Credits

Originally created by [theelites11gamer](https://github.com/theelites11gamer/abf-mod-editor-mcp). The headless Blueprint tools (`staging_sync`, `bp_apply`, `bp_read`, `bp_compile_report`, `bp_game_test`, `mod_audit`, `hidden_editor_shot`, `bp_publish`) were added by DezionDev.

## License

MIT, copyright theelites11gamer and DezionDev. The Blueprint filter list belongs to Deep Field Games and is downloaded from their CDN at run time, not shipped here.
