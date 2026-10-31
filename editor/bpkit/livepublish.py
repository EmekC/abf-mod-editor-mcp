"""Live publish steps for a mod that is open in an editor. One source, two users: the server sends it to the user's
editor (through run_json, which appends `def _abf(args)`), and the hidden integration test runs it in a hidden editor.
Imports `unreal` only, and never saves, deletes or renames anything.
"""
import unreal


def prepare(args):
    """Make the given /Mod/... packages safe to overwrite on disk. Refuses (and changes nothing) when Play In Editor is
    running or when any of them has unsaved changes. Otherwise closes their asset editors, unloads them and collects
    garbage; no Python reference to them remains after this returns."""
    pkgs = list(args.get("packages") or [])
    ued = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    if ued.get_game_world() is not None:
        return {"refused": pkgs, "reason": "Play In Editor is running"}
    dirty = {p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    hit = [p for p in pkgs if p in dirty]
    if hit:
        return {"refused": hit, "reason": "modified assets (unsaved changes)"}
    sub = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    loaded, names = [], []
    for name in pkgs:
        pkg = unreal.find_package(name)
        if pkg is None:
            continue
        obj = unreal.load_asset(name)
        if obj is not None:
            sub.close_all_editors_for_asset(obj)
        obj = None
        loaded.append(pkg)
        names.append(name)
    result = {"refused": [], "unloaded": 0}
    if loaded:
        ok, msg = unreal.EditorLoadingAndSavingUtils.unload_packages(loaded)
        loaded = None
        pkg = None
        unreal.SystemLibrary.collect_garbage()
        # the emptied UPackage can linger until it is collected: what matters is that the asset object itself is gone
        still = [n for n in names if unreal.find_object(None, "%s.%s" % (n, n.rsplit("/", 1)[-1])) is not None]
        if not ok or str(msg).strip() or still:
            # a partial unload can still report True: treat any message or any package still loaded as a refusal, and
            # load back what did unload so the editor is left as it was (asset editors that were closed stay closed)
            for n in names:
                if n not in still:
                    unreal.load_asset(n)
            return {"refused": still or names,
                    "reason": "unload incomplete (%s); asset editors of these packages may have been closed and the unloaded ones were loaded "
                              "back. Nothing was copied." % (str(msg).strip() or "packages still loaded"),
                    "still_loaded": still}
        result["unloaded"] = len(names)
    loaded = None
    pkg = None
    unreal.SystemLibrary.collect_garbage()
    return result


def reload_assets(args):
    """Rescan the mod's folder, load each package (missing ones are reported) and reload them without asking."""
    root = args["root"]
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    reg.scan_paths_synchronous([root], True)
    loaded, missing, pkgs = [], [], []
    for name in args.get("packages") or []:
        a = unreal.load_asset(name)
        if a is None:
            missing.append(name)
            continue
        pkgs.append(a.get_outermost())
        loaded.append(name)
    res = None
    if pkgs:
        res = unreal.EditorLoadingAndSavingUtils.reload_packages(pkgs, unreal.ReloadPackagesInteractionMode.ASSUME_POSITIVE)
    return {"loaded": loaded, "missing": missing, "result": str(res)[:200]}
