"""Live publish steps for a mod that is open in an editor. One source, two users: the server sends it to the user's
editor (through run_json, which appends `def _abf(args)`), and the hidden integration test runs it in a hidden editor.
Imports `unreal` and `re` only, and never saves, deletes or renames anything; `register_tags` changes only the open editor's
in-memory gameplay tag tree.
"""
import re

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


_MAX_REFERENCERS = 200


def _loaded_referencers(root, pkgs):
    """Loaded packages inside `root` (the mod, never /Game) that reference any of `pkgs`, hard or soft, per the asset registry,
    excluding `pkgs` themselves. Transitive: the referencers of a referencer count too (breadth first), because a package
    that only references a referencer still holds pins of the classes that referencer replaces. Returns (reloadable, skipped)
    with `reloadable` in discovery order (the reload order is fixed later, by `_dependency_order`). A loaded map is skipped
    and not expanded, because reloading it would pull the open level out from under the user. At most 200 are returned."""
    found, skipped = [], []
    if not root:
        return found, skipped
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    opts = unreal.AssetRegistryDependencyOptions(include_soft_package_references=True, include_hard_package_references=True)
    seen = set(pkgs)
    frontier = list(pkgs)
    while frontier:
        following = []
        for p in frontier:
            try:
                refs = reg.get_referencers(p, opts)
            except Exception:
                continue
            for r in refs or []:
                n = str(r)
                if n in seen or not n.startswith(root + "/"):
                    continue
                seen.add(n)
                if unreal.find_package(n) is None:
                    continue
                obj = unreal.find_object(None, "%s.%s" % (n, n.rsplit("/", 1)[-1]))
                if obj is None:
                    continue
                if obj.get_class().get_name() == "World":
                    skipped.append(n)
                    continue
                found.append(n)
                following.append(n)
                if len(found) >= _MAX_REFERENCERS:
                    return found, sorted(skipped)
        frontier = following
    return found, sorted(skipped)


def _dependency_order(names):
    """`names` ordered so that every package comes after the packages of `names` it depends on (per the asset registry,
    hard or soft); ties keep the input order. Packages on a dependency cycle, and what is left after one, are appended in
    input order. A registry error counts as no dependencies."""
    names = list(dict.fromkeys(names))
    if len(names) < 2:
        return names
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    opts = unreal.AssetRegistryDependencyOptions(include_soft_package_references=True, include_hard_package_references=True)
    deps = {}
    for n in names:
        try:
            ds = reg.get_dependencies(n, opts)
        except Exception:
            ds = []
        deps[n] = {d for d in (str(x) for x in ds or []) if d != n and d in names}
    ordered, placed, rest = [], set(), names
    while rest:
        ready = next((n for n in rest if deps[n] <= placed), None)
        if ready is None:
            break
        ordered.append(ready)
        placed.add(ready)
        rest = [n for n in rest if n != ready]
    return ordered + rest


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
    the ones that were replaced; `also` are loaded referencers (direct or transitive) that stay unchanged on disk and are
    reloaded in the same call, so they are rebuilt from their files against the new classes and not recompiled against the old.
    All of them are reloaded in dependency order (dependencies first, `_dependency_order`): a package reloaded before a
    package it depends on keeps pins of that package's replaced class and shows compile errors (ABFC7013).
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
    also = [n for n in (args.get("also") or []) if n not in published and n not in skipped_dirty]
    for name in _dependency_order(published + also):
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


def forget(args):
    """After the files of deleted packages are gone from disk: force a rescan of their parent folders so the asset registry
    drops them. `still_listed` names the packages the registry still reports (empty when the rescan cleared them all). Deletes
    nothing itself; the files were removed by the caller."""
    pkgs = list(args.get("packages") or [])
    folders = sorted({p.rsplit("/", 1)[0] for p in pkgs if "/" in p})
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    if folders:
        reg.scan_paths_synchronous(folders, True)
    still = [p for p in pkgs if unreal.EditorAssetLibrary.does_asset_exist("%s.%s" % (p, p.rsplit("/", 1)[-1]))]
    return {"forgotten": [p for p in pkgs if p not in still], "still_listed": still}


def recompile(args):
    """Recompile, in memory and without saving, the given loaded Blueprints in dependency order (dependencies first,
    `_dependency_order`). Used after `reload_assets`: `reload_packages` swaps the classes of the reloaded packages while the
    Blueprints that use them are still loaded, so the editor compiles those against the old and the new class at once and logs
    compile errors that are gone once the swap is over. Compiling them again, dependencies first, shows the state that stays
    (the caller reads the editor log for what this compile reports). Packages that are not Blueprints are left alone."""
    compiled, skipped = [], []
    for name in _dependency_order(list(args.get("packages") or [])):
        a = unreal.find_object(None, "%s.%s" % (name, name.rsplit("/", 1)[-1]))
        if a is None or not isinstance(a, unreal.Blueprint):
            skipped.append(name)
            continue
        unreal.BlueprintEditorLibrary.compile_blueprint(a)
        compiled.append(name)
        a = None
    dirty = sorted(p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
                   if p.get_name().startswith((args.get("root") or "/") + "/"))
    return {"compiled": compiled, "skipped": skipped, "dirty_after": dirty}


_SETTINGS_PATH = "/Script/GameplayTags.Default__GameplayTagsSettings"
_TAG_LIST_CLASS = "/Script/GameplayTags.GameplayTagsList"
_TAG_EXPORT_RX = re.compile(r'TagName\s*=\s*"?([^",)]*)"?')


def _tag_known(name):
    """True when the open editor's gameplay tag tree knows `name`: a GameplayTag struct imported from text keeps the name only
    for a registered tag (an unknown one exports as None). Any error counts as unknown."""
    try:
        t = unreal.GameplayTag()
        t.import_text('(TagName="%s")' % name)
        m = _TAG_EXPORT_RX.search(str(t.export_text()))
        return bool(m) and m.group(1) == name
    except Exception:
        return False


def _row_tag(row):
    """The tag name of a GameplayTagTableRow ("" when unset)."""
    try:
        v = row.get_editor_property("tag")
    except Exception:
        v = row.get_editor_property("Tag")
    v = str(v)
    return "" if v == "None" else v


def _new_row(name):
    r = unreal.GameplayTagTableRow()
    try:
        r.set_editor_property("tag", unreal.Name(name))
    except Exception:
        r.set_editor_property("Tag", unreal.Name(name))
    return r


def _mod_source(mod):
    """The in-memory source object of the mod's tag ini (`<Mod>Tags.ini`, owned by the tags manager), or None. The editor's
    Gameplay Tag Manager rewrites the file from this object when a tag is added there."""
    want = ("%sTags.ini" % mod).lower()
    cls = unreal.load_class(None, _TAG_LIST_CLASS)
    if cls is None:
        return None
    for o in unreal.ObjectIterator(cls):
        try:
            if o.get_name().lower() != want:
                continue
            outer = o.get_outer()
            if outer is not None and outer.get_class().get_name() == "GameplayTagsManager":
                return o
        except Exception:
            continue
    return None


def register_tags(args):
    """Make the open editor know the gameplay tags `args["tags"]` (the tags of the mod's Config/Tags ini), in memory only.
    The editor reads tag ini files at start, so a tag added to the file later is unknown to it and a row that uses it loads
    with an invalid tag. Strategies, each verified by reading every tag back (`_tag_known`):
      refresh   re-apply the tag settings with change notification, which makes the editor rebuild its tag tree from the
                tag search paths (finds an ini the editor did not read yet);
      settings  add the missing tags to the settings' tag list with notification (the tree is rebuilt from that list), then
                put the original list back without notification (the tree keeps the tags; nothing can be saved into the game's
                tag config later).
    Then the tags the file holds and the mod's in-memory source object does not are added to that object without notification,
    so a tag added later through the Gameplay Tag Manager (which rewrites the file from that object) cannot drop them.
    Returns wanted, registered (made known now), unregistered (still unknown), strategy (None when nothing was needed or
    nothing worked), source_added, error. Always returns a dict."""
    tags = list(dict.fromkeys(t for t in (args.get("tags") or []) if isinstance(t, str) and t))
    mod = args.get("mod") or ""
    always = unreal.PropertyAccessChangeNotifyMode.ALWAYS
    never = unreal.PropertyAccessChangeNotifyMode.NEVER
    strategy, error, source_added = None, None, []
    missing, left = [], []
    try:
        missing = [t for t in tags if not _tag_known(t)]
        left = list(missing)
    except Exception as e:
        error = "check: %s" % e
    if missing:
        settings = None
        try:
            settings = unreal.find_object(None, _SETTINGS_PATH)
        except Exception as e:
            error = "settings: %s" % e
        if settings is None:
            error = error or "GameplayTagsSettings not found"
        else:
            try:
                settings.set_editor_property("ImportTagsFromConfig", settings.get_editor_property("ImportTagsFromConfig"),
                                             notify_mode=always)
            except Exception as e:
                error = "refresh: %s" % e
            left = [t for t in tags if not _tag_known(t)]
            if not left:
                strategy = "refresh"
            else:
                try:
                    original = list(settings.get_editor_property("GameplayTagList"))
                except Exception as e:
                    original = None
                    error = "settings: %s" % e
                if original is not None:
                    have = {_row_tag(r).lower() for r in original}
                    try:
                        settings.set_editor_property("GameplayTagList", original + [_new_row(t) for t in left if t.lower() not in have],
                                                     notify_mode=always)
                        left = [t for t in tags if not _tag_known(t)]
                    except Exception as e:
                        error = "settings: %s" % e
                    finally:
                        try:
                            settings.set_editor_property("GameplayTagList", original, notify_mode=never)
                        except Exception as e:
                            error = "%s; restore: %s" % (error, e) if error else "restore: %s" % e
                    if not left:
                        strategy = "settings"
    try:
        src = _mod_source(mod) if tags and mod else None
        if src is not None:
            rows = list(src.get_editor_property("GameplayTagList"))
            have = {_row_tag(r).lower() for r in rows}
            add = [t for t in tags if t.lower() not in have]
            if add:
                src.set_editor_property("GameplayTagList", rows + [_new_row(t) for t in add], notify_mode=never)
                source_added = add
    except Exception as e:
        error = "%s; source: %s" % (error, e) if error else "source: %s" % e
    return {"wanted": tags, "registered": [t for t in missing if t not in left], "unregistered": left, "strategy": strategy,
            "source_added": source_added, "error": error}
