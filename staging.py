"""Staging copies of mods and publishing back. Pure file logic: no editor, no window code.

A mod is copied (minus Saved, Intermediate, Binaries, DerivedDataCache, .git) to <root>/stages/<Mod>/mod/<Mod>, edited
there by hidden processes, and published back by copying changed Content files into the real mod. Publish backs up first
(outside the mod), refuses on conflicts, compile errors or modified assets, and never deletes anything in the real mod.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import time
import warnings
from pathlib import Path

import paths


class StagingError(RuntimeError):
    pass


EXCLUDE_TOP = {"Saved", "Intermediate", "Binaries", "DerivedDataCache", ".git"}
TOOLING = ("Content/_McpTest/", "Content/_McpTmp/")


def root():
    r = paths.headless_dir()
    if " " in str(r):
        raise StagingError("the runtime root %r contains a space; -ExecCmds quoting is unproven with spaces. "
                           "Set ABF_HEADLESS_DIR to a path without spaces." % str(r))
    r.mkdir(parents=True, exist_ok=True)
    return r


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_tooling(rel):
    return rel.startswith(TOOLING)


def _is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _rmtree(p):
    def onerror(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    if Path(p).exists():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            shutil.rmtree(p, onerror=onerror)


class Stage:
    def __init__(self, name):
        if not re.match(r"^[A-Za-z0-9_\-]+$", name or ""):
            raise StagingError("mod name %r may only hold letters, digits, '_' and '-'" % name)
        self.name = name
        self.base = root() / "stages" / name
        self.dir = self.base / "mod" / name
        self.meta_path = self.base / "stage.json"
        self.file_count = 0

    @property
    def uplugin_path(self):
        return self.dir / (self.name + ".uplugin")

    def meta(self):
        if not self.meta_path.is_file():
            return {}
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def save_meta(self, meta):
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)


def _content_files(base):
    """Relative posix paths of every file under <base>/Content, minus tooling folders."""
    out = []
    content = Path(base) / "Content"
    if not content.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(content):
        for f in files:
            rel = (Path(dirpath) / f).relative_to(base).as_posix()
            if not _is_tooling(rel):
                out.append(rel)
    return sorted(out)


def sync(mod_dir, discard_changes=False, forbidden=()):
    src = Path(mod_dir).resolve()
    if not src.is_dir():
        raise StagingError("%s is not a folder" % src)
    ups = list(src.glob("*.uplugin"))
    if len(ups) != 1:
        raise StagingError("expected exactly one .uplugin at the top of %s (found %d)" % (src, len(ups)))
    for f in forbidden:
        if f and _is_under(src, f):
            raise StagingError("%s is inside the kit install, which is never staged or edited" % src)
    if _is_under(src, root()):
        raise StagingError("%s is inside the staging root; sync the real mod folder" % src)
    uplugin = json.loads(ups[0].read_text(encoding="utf-8-sig"))
    stage = Stage(ups[0].stem)
    if stage.meta_path.is_file() and stage.dir.is_dir() and not discard_changes:
        d = diff(stage)
        pending = d["changed"] + d["added"]
        if pending:
            raise StagingError("staging holds %d unpublished change(s) (%s). Publish them with bp_publish, or pass discard_changes."
                               % (len(pending), ", ".join(pending[:5])))
    _rmtree(stage.dir)
    stage.dir.parent.mkdir(parents=True, exist_ok=True)

    def ignore(d, names):
        if os.path.normcase(os.path.abspath(d)) == os.path.normcase(str(src)):
            return [n for n in names if n in EXCLUDE_TOP]
        return []

    shutil.copytree(src, stage.dir, ignore=ignore)
    baseline = {}
    count = 0
    for dirpath, _dirs, files in os.walk(stage.dir):
        count += len(files)
    for rel in _content_files(stage.dir):
        p = stage.dir / rel
        baseline[rel] = {"sha256": _sha(p), "size": p.stat().st_size}
    stage.save_meta({"mod": stage.name, "source_dir": str(src), "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "uplugin_original": uplugin, "baseline": baseline})
    stage.file_count = count
    return stage


def load(mod):
    stage = Stage(mod)
    if not stage.meta_path.is_file() or not stage.dir.is_dir():
        raise StagingError("mod %s is not staged: run staging_sync first" % mod)
    return stage


def set_uplugin(stage, mode):
    if mode not in ("mount", "picker"):
        raise StagingError("uplugin mode must be mount or picker")
    d = dict(stage.meta()["uplugin_original"])
    d["FriendlyName"] = "MCPSTAGE" + stage.name
    if mode == "mount":
        d["ExplicitlyLoaded"] = False
        d["EnabledByDefault"] = True
    stage.uplugin_path.write_text(json.dumps(d, indent=2), encoding="utf-8")


def strip_tooling(stage):
    for t in TOOLING:
        _rmtree(stage.dir / t.rstrip("/"))


def strip_scratch(stage):
    """Remove only the scratch folder (Content/_McpTmp); the test harness in _McpTest stays."""
    _rmtree(stage.dir / "Content" / "_McpTmp")


def files_for(stage, stem):
    """Existing files of one asset: every Content/<stem>.* (uasset, uexp, ...)."""
    parent = (stage.dir / stem).parent
    name = Path(stem).name
    if not parent.is_dir():
        return []
    return sorted((parent / f.name).relative_to(stage.dir).as_posix() for f in parent.iterdir()
                  if f.is_file() and os.path.splitext(f.name)[0] == name)


def diff(stage):
    meta = stage.meta()
    base = meta.get("baseline", {})
    src = Path(meta["source_dir"]) if meta.get("source_dir") else None
    now = {rel: None for rel in _content_files(stage.dir)}
    changed, added, removed, conflicts = [], [], [], []
    for rel in now:
        if rel not in base:
            added.append(rel)
            if src and (src / rel).exists():
                conflicts.append({"path": rel, "reason": "a file with this name already exists in the real mod"})
            continue
        p = stage.dir / rel
        if p.stat().st_size != base[rel]["size"] or _sha(p) != base[rel]["sha256"]:
            changed.append(rel)
            real = (src / rel) if src else None
            if real is not None:
                if not real.is_file():
                    conflicts.append({"path": rel, "reason": "the real file was deleted since the last sync"})
                else:
                    try:
                        real_sha = _sha(real)
                    except OSError:
                        real_sha = base[rel]["sha256"]       # locked by an editor: the lock probe in publish decides
                    if real_sha != base[rel]["sha256"]:
                        conflicts.append({"path": rel, "reason": "the real file changed since the last sync; run staging_sync again"})
    for rel in base:
        if rel not in now and not _is_tooling(rel):
            removed.append(rel)
    return {"changed": sorted(changed), "added": sorted(added), "removed": sorted(removed), "conflicts": conflicts}


# ---- publish ------------------------------------------------------------------

def _asset_rel(a, mod):
    a = a.replace("\\", "/")
    prefix = "/%s/" % mod
    if a.startswith(prefix):
        a = "Content/" + a[len(prefix):]
    return os.path.splitext(a)[0] if os.path.splitext(a)[1] else a


def _packages(files, mod):
    out = []
    for rel in files:
        stem, ext = os.path.splitext(rel)
        if ext.lower() in (".uasset", ".umap") and rel.startswith("Content/"):
            pkg = "/%s/%s" % (mod, stem[len("Content/"):])
            if pkg not in out:
                out.append(pkg)
    return out


def _refuse(reason, message, **extra):
    r = {"published": [], "refused": reason, "error": message, "message": message}
    r.update(extra)
    return r


def _restore(src, backup, rels, existed, manifest):
    """Put the real files back from the backup (added files are removed) and VERIFY the result against the manifest.
    Returns the list of paths that could not be restored."""
    failed = []
    for rel in rels:
        dst = src / rel
        try:
            tmp = Path(str(dst) + ".mcpnew")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            failed.append(rel + ".mcpnew")
        try:
            if rel in existed:
                shutil.copyfile(backup / rel, dst)
                if _sha(dst) != manifest.get(rel):
                    failed.append(rel)
            elif dst.exists():
                dst.unlink()
        except OSError:
            failed.append(rel)
    return failed


def _try_reload(editor, packages):
    try:
        return editor.reload(packages)
    except Exception:
        return None


def publish(stage, editor, compile_gate, assets=None, dry_run=False, now=None):
    name = stage.name
    meta = stage.meta()
    src = Path(meta["source_dir"])
    d = diff(stage)
    removed = d["removed"]
    if assets:
        wanted = {_asset_rel(a, name) for a in assets}
        keep = lambda rel: os.path.splitext(rel)[0] in wanted   # noqa: E731
        d = {"changed": [r for r in d["changed"] if keep(r)], "added": [r for r in d["added"] if keep(r)],
             "removed": [r for r in removed if keep(r)],
             "conflicts": [c for c in d["conflicts"] if keep(c["path"])]}
        removed = d["removed"]
    files = d["changed"] + d["added"]
    if not files:
        return {"published": [], "route": None, "backup": None, "removed_in_staging": removed, "editor": None,
                "message": "Nothing to publish: the staged Content matches the real mod."}
    if d["conflicts"]:
        return _refuse("conflicts", "Refused: %d file(s) changed in the real mod since the last sync (%s). Run staging_sync again "
                       "(after publishing or discarding your staged changes); there is no force option."
                       % (len(d["conflicts"]), "; ".join("%s: %s" % (c["path"], c["reason"]) for c in d["conflicts"][:5])),
                       conflicts=d["conflicts"])
    packages = _packages(files, name)
    errors = compile_gate(packages)
    if errors:
        return _refuse("compile errors", "Refused: %d compile error(s) or unchecked asset(s) in the staged files; nothing was changed." % len(errors),
                       compile_errors=errors)
    if dry_run:
        return {"published": [], "dry_run": True, "would_publish": files, "changed": d["changed"], "added": d["added"],
                "removed_in_staging": removed, "editor": None,
                "message": "Dry run: %d file(s) would be copied into %s. Nothing was changed." % (len(files), src)}
    try:
        state = editor.state(name)
    except Exception as e:
        return _refuse("editor unreachable", "Refused: could not determine whether an editor is running (%s). Nothing was changed." % e)
    if state == "unreachable":
        detail = getattr(editor, "detail", "") or ""
        return _refuse("editor unreachable", "Refused: %sAn editor is running but not answering, so its unsaved state cannot be checked and a later "
                       "Save could overwrite this publish. Close the dialog or the editor, then publish again. Nothing was changed."
                       % (detail + " " if detail else ""), editor="unreachable")
    if state not in ("closed", "open", "other"):
        return _refuse("editor state", "Refused: unknown editor state %r." % (state,))
    existing = [rel for rel in files if (src / rel).is_file()]
    if state != "open":
        for rel in existing:
            try:
                open(src / rel, "r+b").close()
            except OSError as e:
                return _refuse("file locked", "Refused: %s is locked (%s). An editor holds it open; close the asset or the mod there, "
                               "or open the mod in the editor the tools can reach." % (rel, e))

    def abort(reason, message, **extra):
        """Every non-success path after prepare: give the user's editor its assets back, then refuse."""
        if state == "open":
            _try_reload(editor, packages)
        return _refuse(reason, message, **extra)

    if state == "open":
        try:
            res = editor.prepare(packages)
        except Exception as e:
            return abort("editor error", "Refused: the open editor failed while preparing (%s); it was asked to reload the assets. "
                         "Nothing was copied." % e)
        if res.get("refused"):
            return _refuse("editor refused", "Refused by the open editor: %s (%s). Save or close those assets, stop Play In Editor, "
                           "then publish again. Nothing was copied." % (", ".join(res["refused"]), res.get("reason", "")),
                           editor_result=res)
    try:
        stamp = now.strftime("%Y%m%d-%H%M%S") if hasattr(now, "strftime") else (str(now) if now else time.strftime("%Y%m%d-%H%M%S"))
        backup = root() / "backups" / name / stamp
        n = 1
        while backup.exists():
            n += 1
            backup = root() / "backups" / name / ("%s-%d" % (stamp, n))
        backup.mkdir(parents=True)
        baseline = meta.get("baseline", {})
        man, mismatched = [], []
        for rel in d["changed"]:
            if rel not in existing:
                mismatched.append(rel + " (deleted in the real mod)")
        for rel in d["added"]:
            if (src / rel).exists():
                mismatched.append(rel + " (now exists in the real mod)")
        for rel in existing:
            if rel in d["added"]:
                continue
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst)
            a, b = _sha(src / rel), _sha(dst)
            if a != b:
                return abort("backup failed", "Refused: the backup of %s did not verify; nothing was copied." % rel)
            if a != baseline.get(rel, {}).get("sha256"):
                mismatched.append(rel + " (changed in the real mod since the last sync)")
            man.append({"path": rel, "sha256": a})
        if mismatched:
            return abort("conflicts", "Refused: the real mod changed while publishing was being prepared (%s). Nothing was copied. "
                         "Run staging_sync again." % "; ".join(mismatched[:5]), conflicts=mismatched)
        (backup / "manifest.json").write_text(json.dumps({"mod": name, "created": stamp, "real_mod": str(src), "files": man,
                                                          "added": [r for r in files if r not in existing]}, indent=2), encoding="utf-8")
    except Exception as e:
        return abort("backup failed", "Refused: could not back up the real files (%s). Nothing was copied." % e)
    manifest = {m["path"]: m["sha256"] for m in man}
    existed = set(manifest)
    done = []
    try:
        for rel in files:
            dst = src / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(dst) + ".mcpnew")
            done.append(rel)                                   # before the copy: a failed copy can leave a .mcpnew behind
            shutil.copyfile(stage.dir / rel, tmp)
            os.replace(tmp, dst)
            if _sha(dst) != _sha(stage.dir / rel):
                raise OSError("copy of %s did not verify" % rel)
    except Exception as e:
        failed = _restore(src, backup, done, existed, manifest)
        if state == "open":
            _try_reload(editor, packages)
        if failed:
            return _refuse("copy failed", "The copy failed (%s) and the restore from the backup FAILED for: %s. The real mod may be "
                           "half-published: copy these back from %s by hand." % (e, ", ".join(failed), backup),
                           restored=False, failed_paths=failed, backup=str(backup))
        return _refuse("copy failed", "The copy failed (%s). The real mod was restored from the backup in %s." % (e, backup),
                       restored=True, backup=str(backup))
    if state == "open":
        try:
            res = editor.reload(packages)
            bad_reload = res.get("missing") or res.get("error")
        except Exception as e:
            bad_reload = str(e)
        if bad_reload:
            failed = _restore(src, backup, files, existed, manifest)
            _try_reload(editor, packages)
            if failed:
                return _refuse("reload failed", "The editor could not load the new files (%s) and the restore from the backup FAILED for: %s. "
                               "Copy them back from %s by hand." % (bad_reload, ", ".join(failed), backup),
                               restored=False, failed_paths=failed, backup=str(backup))
            return _refuse("reload failed", "The editor could not load the new files (%s). The real mod was restored from the backup in %s."
                           % (bad_reload, backup), restored=True, backup=str(backup))
    for rel in files:
        p = stage.dir / rel
        meta["baseline"][rel] = {"sha256": _sha(p), "size": p.stat().st_size}
    stage.save_meta(meta)
    route = "live-reload" if state == "open" else "file-copy"
    msg = "Copied %d file(s) into %s (%s)." % (len(files), src, "the open editor reloaded them" if state == "open"
                                              else "no editor held them; they are on disk")
    msg += " Backup of the previous files: %s." % backup
    if removed:
        msg += " %d file(s) are gone from staging; delete them in the Content Browser yourself, nothing was deleted in the real mod." % len(removed)
    return {"published": files, "route": route, "backup": str(backup), "removed_in_staging": removed, "editor": state, "message": msg}
