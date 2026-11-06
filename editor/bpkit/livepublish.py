"""Live publish steps for a mod that is open in an editor. One source, two users: the server sends it to the user's
editor (through run_json, which appends `def _abf(args)`), and the hidden integration test runs it in a hidden editor.
Imports `unreal` only, and never saves, deletes or renames anything.
"""
import unreal


def _call_problem(what, res):
    """None when the result of reload_packages/unload_packages shows no failure, else a short text. The Python binding may
    return a bool, or a tuple of (bools, error text) for the out parameters; a non-empty error text or a plain False is a
    failure. A tuple of bools alone is not judged (the meaning of its flags is not documented): it is logged in the report."""
    items = list(res) if isinstance(res, (tuple, list)) else [res]
    for it in items:
        if it is None or isinstance(it, (bool, int, float)):
            continue
        text = str(it).strip()
        if text:
            return "%s: %s" % (what, text[:300])
    if not isinstance(res, (tuple, list)) and res is False:
        return "%s returned False" % what
    return None


def _loaded_referencers(root, pkgs):
    """Loaded packages inside `root` (the mod, never /Game) that reference any of `pkgs`, hard or soft, per the asset registry,
    excluding `pkgs` themselves. Direct referencers only. Returns (reloadable, skipped); a loaded map is skipped, because
    reloading it would pull the open level out from under the user."""
    found, skipped = [], []
    if not root:
        return found, skipped
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    opts = unreal.AssetRegistryDependencyOptions(include_soft_package_references=True, include_hard_package_references=True)
    for p in pkgs:
        try:
            refs = reg.get_referencers(p, opts)
        except Exception:
            continue
        for r in refs or []:
            n = str(r)
            if n in pkgs or n in found or n in skipped or not n.startswith(root + "/"):
                continue
            if unreal.find_package(n) is None:
                continue
            obj = unreal.find_object(None, "%s.%s" % (n, n.rsplit("/", 1)[-1]))
            if obj is None:
                continue
            (skipped if obj.get_class().get_name() == "World" else found).append(n)
    return sorted(found), sorted(skipped)


def prepare(args):
    """Make the given /Mod/... packages safe to overwrite on disk. Refuses (and changes nothing) when Play In Editor is
    running or when any of them, or any loaded package of the same mod that references them (`referencers`), has unsaved
    changes: reloading would discard those edits. Otherwise closes their asset editors and calls unload_packages, which
    releases the files; packages other loaded objects still reference stay loaded (`kept_loaded`) and are reloaded in place
    after the copy, together with the referencers (which are not touched on disk; reloading them from their files rebuilds
    them against the new classes). Collects garbage; no Python reference to them remains after this returns."""
    pkgs = list(args.get("packages") or [])
    refs, skipped_refs = _loaded_referencers(args.get("root"), pkgs)
    ued = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    if ued.get_game_world() is not None:
        return {"refused": pkgs, "reason": "Play In Editor is running"}
    dirty = {p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    hit = [p for p in pkgs if p in dirty]
    hit_refs = [p for p in refs if p in dirty]
    if hit or hit_refs:
        return {"refused": hit + hit_refs, "refused_referencers": hit_refs, "reason": "modified assets (unsaved changes)"}
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
    for name in refs:
        obj = unreal.load_asset(name)
        if obj is not None:
            sub.close_all_editors_for_asset(obj)
        obj = None
    result = {"refused": [], "unloaded": [], "kept_loaded": [], "unload_message": "", "editors_closed": list(refs),
              "referencers": refs, "skipped_referencers": skipped_refs}
    if loaded:
        ok, msg = unreal.EditorLoadingAndSavingUtils.unload_packages(loaded)
        loaded = None
        pkg = None
        unreal.SystemLibrary.collect_garbage()
        # the emptied UPackage can linger until it is collected: what matters is that the asset object itself is gone
        still = [n for n in names if unreal.find_object(None, "%s.%s" % (n, n.rsplit("/", 1)[-1])) is not None]
        result = {"refused": [], "unloaded": [n for n in names if n not in still], "kept_loaded": still,
                  "unload_message": str(msg).strip()[:500], "editors_closed": names + refs, "referencers": refs,
                  "skipped_referencers": skipped_refs}
    loaded = None
    pkg = None
    unreal.SystemLibrary.collect_garbage()
    return result


def reload_assets(args):
    """Rescan the mod's folder, load each package (missing ones are reported) and reload them without asking. `packages` are
    the ones that were replaced; `also` are loaded referencers that stay unchanged on disk and are reloaded in the same call,
    after the replaced ones, so they are rebuilt from their files against the new classes and not recompiled against the old.
    `prepare` checked for unsaved changes before the copy, but the editor stays editable during it, so the dirty set is read
    again right before the reload: a dirty `also` package is left out (`skipped_dirty`) so its edit is not thrown away; a dirty
    replaced package is still reloaded, since its file on disk has already changed (`dirty_published`)."""
    root = args["root"]
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    reg.scan_paths_synchronous([root], True)
    dirty_before = {p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    published = list(args.get("packages") or [])
    dirty_published = [n for n in published if n in dirty_before]
    skipped_dirty = [n for n in (args.get("also") or []) if n not in published and n in dirty_before]
    loaded, missing, pkgs = [], [], []
    for name in published + [n for n in (args.get("also") or []) if n not in published and n not in skipped_dirty]:
        a = unreal.load_asset(name)
        if a is None:
            missing.append(name)
            continue
        pkgs.append(a.get_outermost())
        loaded.append(name)
    res = None
    if pkgs:
        res = unreal.EditorLoadingAndSavingUtils.reload_packages(pkgs, unreal.ReloadPackagesInteractionMode.ASSUME_POSITIVE)
    problem = _call_problem("reload_packages", res) if pkgs else None
    for n in loaded:
        if unreal.find_object(None, "%s.%s" % (n, n.rsplit("/", 1)[-1])) is None:
            missing.append(n)
    dirty_after = sorted(p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
                         if p.get_name().startswith(root + "/"))
    out = {"loaded": loaded, "missing": missing, "result": str(res)[:200], "dirty_after": dirty_after,
           "reloaded_referencers": [n for n in loaded if n in (args.get("also") or []) and n not in (args.get("packages") or [])],
           "skipped_dirty": skipped_dirty, "dirty_published": dirty_published}
    if problem:
        out["error"] = problem
    return out
