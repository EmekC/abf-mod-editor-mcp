"""Host runner for hidden Unreal processes: commandlet and game launches, run folders, the one-at-a-time lock,
log parsers. No imports from server.py.

Every launch passes -userdir (a private one under the runtime root) and a redirected LOCALAPPDATA, runs on a staged copy
of the mod, and is judged by the result file the job writes: the commandlet exits non-zero even on success.
"""
import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import paths
import staging


class HeadlessError(RuntimeError):
    pass


KEEP_RUNS = 20


def root():
    r = paths.headless_dir()
    if " " in str(r):
        raise HeadlessError("the runtime root %r contains a space; -ExecCmds quoting is unproven with spaces. "
                            "Set ABF_HEADLESS_DIR to a path without spaces." % str(r))
    r.mkdir(parents=True, exist_ok=True)
    return r


def _fwd(p):
    return str(p).replace("\\", "/")


def new_run(op):
    runs = root() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run = runs / ("%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), op, secrets.token_hex(2)))
    run.mkdir()
    shutil.copyfile(paths.HERE / "editor" / "abf_job.py", run / "abf_job.py")
    old = sorted((p for p in runs.iterdir() if p.is_dir() and p != run), key=lambda p: (p.stat().st_mtime, p.name))
    for p in old[:max(0, len(old) + 1 - KEEP_RUNS)]:
        shutil.rmtree(p, ignore_errors=True)
    return run


def _pid_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5         # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunLock:
    """One hidden run at a time: they share one -userdir. A lock held by a dead pid is replaced."""

    def __init__(self):
        self.path = root() / "run.lock"
        self.fd = None

    def __enter__(self):
        for _ in range(3):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and _pid_alive(pid):
                    raise HeadlessError("another hidden run is in progress (pid %d holds %s)" % (pid, self.path))
                try:
                    self.path.unlink()
                except OSError:
                    raise HeadlessError("cannot replace the stale lock %s" % self.path)
        raise HeadlessError("could not take the run lock %s" % self.path)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


class KillOnCloseJob:
    """A Windows Job Object with KILL_ON_JOB_CLOSE. Processes we start are assigned to it; closing it (explicitly, or when
    this server dies and Windows closes the handle) kills the whole tree of OUR hidden process, including a
    CrashReportClient it spawned. Only processes assigned here are ever affected."""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes.wintypes as wt

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        h = k.CreateJobObjectW(None, None)
        if not h:
            raise HeadlessError("CreateJobObjectW failed (error %d)" % ctypes.get_last_error())
        info = EXT()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(h)
            raise HeadlessError("SetInformationJobObject failed (error %d)" % ctypes.get_last_error())
        self.handle = h

    def assign(self, process_handle):
        """process_handle: a Windows process handle (int) of a process this server started."""
        if self.handle is None:
            return False
        return bool(self._k.AssignProcessToJobObject(self.handle, int(process_handle)))

    def close(self):
        if self.handle is not None:
            self._k.CloseHandle(self.handle)
            self.handle = None


def child_env(run, job_path):
    env = dict(os.environ)
    la = root() / "la"
    la.mkdir(parents=True, exist_ok=True)
    env["LOCALAPPDATA"] = str(la)
    env["ABF_MCP_HIDDEN"] = "1"
    env["ABF_MCP_KIT"] = _fwd(paths.HERE / "editor")
    env["ABF_MCP_JOB"] = _fwd(job_path)
    return env


def scrub(line):
    return re.sub(r"(?i)(token|authorization|bearer|api[_-]?key|secret)[=: ]+(?:bearer\s+)?\S+", lambda m: m.group(1) + "=***", line)


# ---- log parsers ------------------------------------------------------------------

_COMPILE = re.compile(r"LogBlueprint: (?P<sev>Error|Warning): (?:\[AssetLog\] (?P<where>.+?): )?\[Compiler\] (?P<msg>.*?)(?: from Source: (?P<src>\S+))?\s*$")


def _asset_of(src, where, mod=None):
    if src:
        return src.split(".")[0]
    if where:
        if where.startswith("/"):
            return where.split(".")[0]
        w = where.replace("\\", "/")
        i = w.lower().rfind("/content/")
        if i >= 0:
            rest = os.path.splitext(w[i + len("/content/"):])[0]
            m = mod or (w[:i].rsplit("/", 1)[-1])
            return "/%s/%s" % (m, rest)
        return where
    return None


def parse_compile_log(text, mod=None):
    out = {"errors": [], "warnings": []}
    seen = set()
    for line in text.splitlines():
        m = _COMPILE.search(line)
        if not m:
            continue
        sev = m.group("sev")
        src = m.group("src")
        asset = _asset_of(src, m.group("where"), mod)
        msg = m.group("msg").strip()
        key = (sev, asset, msg, src)
        if key in seen:
            continue
        seen.add(key)
        entry = {"asset": asset, "message": msg}
        if src:
            # src like /Mod/X/BP.BP:EventGraph.K2Node_CallFunction_12 -> graph EventGraph, node K2Node_CallFunction_12
            where = src.split(":", 1)[1] if ":" in src else ""
            graph, _dot, node = where.rpartition(".")
            entry.update({"source": src, "graph": graph or None, "node": node or None})
        out["errors" if sev == "Error" else "warnings"].append(entry)
    return out


APPLY_FINAL_MARK = "ABFMCP_APPLY_FINAL"     # logged by editor/bpkit/apply.py right before the compile of the finished asset


def final_compile_errors(text, mod, assets):
    """Compile errors of `assets` that stand after the apply: bp_apply compiles an asset many times (before the edit, after each graph),
    and an asset that was left broken by an earlier failed build reports its old errors on the first of them even when the edit
    repairs it. Only what the last compile (after the ABFMCP_APPLY_FINAL <asset> line) reports counts. A log without the line
    (an older kit) is read whole."""
    lines = text.splitlines()
    out = []
    for asset in assets:
        at = -1
        for i, line in enumerate(lines):
            if line.rstrip().endswith(APPLY_FINAL_MARK + " " + asset):
                at = i
        seg = "\n".join(lines[at + 1:]) if at >= 0 else text
        out.extend(e for e in parse_compile_log(seg, mod)["errors"] if e["asset"] == asset and "/_McpTmp/" not in (e["asset"] or ""))
    return out


def _norm_msg(text):
    """Compares messages by their words. Node messages arrive unescaped (bpscan.compiler_node_errors); log lines carry no escapes."""
    return " ".join(str(text or "").split())


def attach_node_refs(entries, nodes_by_asset):
    """The Blueprint compile log names no node (`from Source: <asset>` only), but the compiler stores each message on its node
    (ErrorMsg=), which a post-compile T3D export shows. Give every entry of parse_compile_log that has no node the node whose
    stored message equals the entry's message (or contains it, or is contained in it: ErrorMsg may be truncated). Each node is
    used once, in export order, so two identical messages on two nodes get two nodes. Entries that match nothing stay as they were.
    entries: dicts with asset and message (changed in place); nodes_by_asset: {asset: [(graph, node, message)]}.
    Returns the number of entries that got a node."""
    used = set()
    n = 0
    for e in entries:
        if e.get("node") or e.get("asset") not in nodes_by_asset:
            continue
        want = _norm_msg(e.get("message"))
        cands = [(i, g, nd, _norm_msg(m)) for i, (g, nd, m) in enumerate(nodes_by_asset[e["asset"]]) if (e["asset"], i) not in used]
        hit = next((c for c in cands if c[3] == want), None)
        if hit is None and len(want) >= 12:
            hit = next((c for c in cands if len(c[3]) >= 12 and (c[3] in want or want in c[3])), None)
        if hit is None:
            continue
        used.add((e["asset"], hit[0]))
        e.update({"graph": hit[1] or None, "node": hit[2], "node_from": "export"})
        n += 1
    return n


_AUDIT_DONE = re.compile(r"Asset audit complete: (\d+) Error\(s\), (\d+) Warning\(s\)")
_AUDITING = re.compile(r"Auditing (/\S+?)(?:\.\S+)? \(")


def parse_audit_log(text):
    r = {"completed": False, "errors": 0, "warnings": 0, "lines": [], "audited": []}
    for line in text.splitlines():
        if "LogAbioticAuditModAssets" in line or "AssetAudit" in line:
            r["lines"].append(scrub(line.strip()))
        m = _AUDITING.search(line)
        if m and m.group(1) not in r["audited"]:
            r["audited"].append(m.group(1))
        d = _AUDIT_DONE.search(line)
        if d:
            r["completed"] = True
            r["errors"], r["warnings"] = int(d.group(1)), int(d.group(2))
    return r


def parse_runtime_log(text):
    out, seen = [], set()
    for line in text.splitlines():
        if re.search(r"LogScript: (Error|Warning)", line) or "Blueprint Runtime Error" in line:
            s = scrub(re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", line).strip())
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


# ---- launching ----------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _finish(run, job_path, p, timed_out, seconds, extra=None):
    result_path = run / "result.json"
    res = None
    if result_path.is_file():
        try:
            res = json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError:
            res = None
    log = run / "editor.log"
    out = {"ok": bool(res and res.get("ok")), "result": res.get("result") if res else None,
           "error": res.get("error") if res else None, "exit_code": p.returncode if p else None,
           "timed_out": timed_out, "log": str(log), "run": str(run), "seconds": round(seconds, 1)}
    if res is None:
        out["error"] = ("hidden process timed out and was killed" if timed_out else
                        "no result file: the hidden process crashed or exited before the job finished")
        lines = [scrub(l) for l in _read(log).splitlines() if re.search(r"Error|Fatal|Assertion|Crash|Warning: Failed", l)]
        out["log_tail"] = lines[-30:]
    if extra:
        out.update(extra)
    return out


def _spawn_quiet(args, env):
    """Popen without a console window and without taking focus on the user's desktop."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7                                   # SW_SHOWMINNOACTIVATE
    return subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            startupinfo=si, creationflags=0x08000000)       # CREATE_NO_WINDOW


def _exe():
    exe = paths.editor_exe()
    if exe is None or not Path(exe).is_file():
        raise HeadlessError("No Mod Editor install found; set ABF_EDITOR_ROOT.")
    return str(exe)


def _launch(stage, job, kind, map_path, timeout, prepare=None):
    exe = _exe()
    WARMS.stop_all()            # a warm session holds the shared -userdir: a cold run needs it free
    with RunLock():
        run = new_run(job["op"])
        if prepare is not None:
            prepare(run)            # e.g. snapshot the files a job is about to change into <run>/before
        job_path = run / "job.json"
        job = dict(job, mod=stage.name, run=_fwd(run), log=str(run / "editor.log"))
        job_path.write_text(json.dumps(job), encoding="utf-8")
        staging.set_uplugin(stage, "mount")
        uplugin = _fwd(stage.uplugin_path)
        userdir = _fwd(root() / "ud")
        log = str(run / "editor.log")
        if kind == "cmd":
            args = [exe, "-run=pythonscript", "-script=" + str(run / "abf_job.py"), "-unattended", "-nosplash", "-nullrhi",
                    "-nopause", "-plugin=" + uplugin, "-userdir=" + userdir, "-abslog=" + log]
        else:
            args = [exe, map_path, "-game", "-nullrhi", "-unattended", "-nosplash", "-nosound", "-nopause", "-plugin=" + uplugin,
                    "-userdir=" + userdir, "-ExecCmds=py " + _fwd(run / "abf_job.py"), "-abslog=" + log]
        t0 = time.time()
        job_obj = KillOnCloseJob()
        p = None
        timed_out = False
        try:
            p = _spawn_quiet(args, child_env(run, job_path))
            job_obj.assign(int(p._handle))                  # our own child (and anything it starts) dies with the job
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            if p is not None and p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            job_obj.close()                                 # kills a leftover tree (CrashReportClient, shader workers)
        return _finish(run, job_path, p, timed_out, time.time() - t0)


def run_commandlet(stage, job, timeout=300, prepare=None):
    """-run=pythonscript about 10 s. Exit code is ignored: the result file decides.
    prepare(run) is called with the new run folder before the process starts.
    With ABF_WARM_SESSION=1 the ops in WARM_OPS go to the warm session first (result key warm: true); when the warm session
    cannot run a job, it runs cold (warm: false, warm_fallback: reason)."""
    reason = None
    if warm_enabled() and job.get("op") in WARM_OPS:
        res, reason = WARMS.get(stage).submit(job, timeout, prepare)
        if res is not None:
            return res
    res = _launch(stage, job, "cmd", None, timeout, prepare)
    res["warm"] = False
    if reason:
        res["warm_fallback"] = reason
    return res


def run_game(stage, job, map_path="/Game/Maps/Empty", timeout=180):
    if not re.match(r"^/(Game|%s)/[A-Za-z0-9_/]+$" % re.escape(stage.name), map_path or ""):
        raise HeadlessError("map %r is not a plain /Game or /<Mod> package path" % (map_path,))
    return _launch(stage, job, "game", map_path, timeout)


# ---- warm session ----------------------------------------------------------------------

WARM_OPS = ("apply", "export", "compile", "reflect", "catalog")     # commandlet ops; bp_game_test (-game) and mod_audit (editor) stay cold
WARM_READY_TIMEOUT = 600.0       # seconds to wait for the warm process to report ready
WARM_END_WAIT = 10.0             # seconds to wait for the job's end marker to reach the log file
WARM_SLACK = 10.0                # a warm job may run this much longer than its timeout before the session is killed
WARM_IDLE_DEFAULT = 600.0
RELOAD_STALE_MARKERS = ("is not compatible with", "doesn't match the property")     # expected while reloaded Blueprints compile
RELOAD_FAILED = "ABFMCP_WARM_RELOAD_FAILED"      # result error prefix written by editor/bpkit/warm.py when the reload failed


class WarmError(HeadlessError):
    pass


def warm_enabled():
    """Opt-in: the warm session is used only when the server was started with ABF_WARM_SESSION=1."""
    return os.environ.get("ABF_WARM_SESSION") == "1"


class WarmSession:
    """One long-lived hidden commandlet per staged mod (editor/bpkit/warm.py is its loop). Queue protocol, all under
    <runtime root>/warm/<mod>/: the host writes queue/<seq>-<id>.json = {"id", "job", "run"}; the loop writes <run>/result.json
    and then <run>/warm.done; ready.json appears once the loop runs; stop.json asks it to exit. Every call is serialized by the
    session mutex plus the shared RunLock (the session uses the same -userdir as cold runs).

    submit() returns (result, None) when the warm session ran the job, or (None, reason) when the caller must run it cold.
    Whenever the warm process is dead, silent past job timeout + 10 s, or fails to start, the process tree is killed and the job
    falls back to cold, except an `apply` job that was already handed to a dying process: its files may be half written, so that
    one is reported as failed (warm: true) and the caller's own rollback restores the stage."""

    def __init__(self, stage, spawn=None, poll=0.1, ready_timeout=None):
        self.stage = stage
        self.name = stage.name
        self.poll = poll
        self.ready_timeout = WARM_READY_TIMEOUT if ready_timeout is None else ready_timeout
        self._spawn = spawn or _spawn_quiet
        self._mu = threading.RLock()
        self.proc = None
        self.job_obj = None
        self.dir = None
        self.jobs = 0
        self.seq = 0
        self.started_at = None

    # -- state
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        a = self.alive()
        return {"mod": self.name, "running": a, "pid": getattr(self.proc, "pid", None) if a else None, "jobs": self.jobs,
                "startedAt": self.started_at if a else None}

    def _session_dir(self):
        return root() / "warm" / self.name

    def _kill_locked(self):
        p, self.proc = self.proc, None
        jo, self.job_obj = self.job_obj, None
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        if jo is not None:
            jo.close()                                    # kills a leftover tree (CrashReportClient, shader workers)

    # -- start / stop
    def _start_locked(self):
        """Start the process when it is not alive. Returns the seconds it took (0.0 when it was already running)."""
        if self.alive():
            return 0.0
        self._kill_locked()
        exe = _exe()
        d = self._session_dir()
        shutil.rmtree(d, ignore_errors=True)
        (d / "queue").mkdir(parents=True)
        shutil.copyfile(paths.HERE / "editor" / "warm_loop.py", d / "warm_loop.py")
        staging.set_uplugin(self.stage, "mount")
        env = child_env(d, d / "warm.json")
        env["ABF_MCP_WARM_DIR"] = _fwd(d)
        env["ABF_MCP_STAGE_DIR"] = _fwd(self.stage.dir)
        env.setdefault("ABF_WARM_IDLE_SECONDS", str(int(WARM_IDLE_DEFAULT)))
        args = [exe, "-run=pythonscript", "-script=" + str(d / "warm_loop.py"), "-unattended", "-nosplash", "-nullrhi", "-nopause",
                "-plugin=" + _fwd(self.stage.uplugin_path), "-userdir=" + _fwd(root() / "ud"), "-abslog=" + str(d / "editor.log")]
        t0 = time.time()
        self.dir = d
        self.job_obj = KillOnCloseJob()
        try:
            self.proc = self._spawn(args, env)
        except OSError as e:
            self._kill_locked()
            raise WarmError("could not start the warm session: %s" % e)
        handle = getattr(self.proc, "_handle", None)
        if handle is not None:
            self.job_obj.assign(int(handle))
        while not (d / "ready.json").is_file():
            if not self.alive():
                self._kill_locked()
                raise WarmError("the warm process exited before it was ready (see %s)" % (d / "editor.log"))
            if time.time() - t0 > self.ready_timeout:
                self._kill_locked()
                raise WarmError("the warm process was not ready after %.0f s (see %s)" % (self.ready_timeout, d / "editor.log"))
            time.sleep(self.poll)
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.jobs = 0
        return time.time() - t0

    def start(self):
        """Start (or confirm) the session. Takes the run lock because the process uses the shared -userdir."""
        with self._mu:
            if self.alive():
                return self.status()
            with RunLock():
                self._start_locked()
            return self.status()

    def stop(self, grace=20.0):
        """Ask the loop to exit, kill it after `grace` seconds. Returns True when a live process was stopped."""
        with self._mu:
            was = self.alive()
            if was and self.dir is not None:
                try:
                    (self.dir / "stop.json").write_text("{}", encoding="utf-8")
                except OSError:
                    pass
                t0 = time.time()
                while self.alive() and time.time() - t0 < grace:
                    time.sleep(self.poll)
            self._kill_locked()
            return was

    # -- jobs
    def _log_from(self, offset):
        try:
            with open(self.dir / "editor.log", "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _log_size(self):
        try:
            return (self.dir / "editor.log").stat().st_size
        except OSError:
            return 0

    def _cut_log(self, run, jid, offset):
        """Copy this job's part of the session log (BEGIN..END marker lines) to <run>/editor.log. The log reaches the file
        asynchronously, so wait (bounded) for the END marker. Returns (end_found, reload_errors): without the END marker the copy
        may be cut off and must not be trusted; reload_errors are the reload-phase error lines that are not stale-class noise."""
        end = "ABFMCP_WARM_END " + jid
        t0 = time.time()
        text = self._log_from(offset)
        while end not in text and time.time() - t0 < WARM_END_WAIT and self.alive():
            time.sleep(self.poll)
            text = self._log_from(offset)
        found = end in text
        reload_errors = []
        lines = text.splitlines()
        begin = "ABFMCP_WARM_BEGIN " + jid
        i = next((k for k, l in enumerate(lines) if begin in l), 0)
        j = next((k for k in range(i, len(lines)) if end in lines[k]), len(lines) - 1)
        # What the editor logs while it reloads the staged packages (warm.reload_mod) is not the job's output: a reloaded
        # Blueprint compiles against the class of a package that is replaced a moment later and logs stale-class errors
        # ("X is not compatible with X", "doesn't match the property") that never occur in a cold process. The job's own
        # compile runs after the reload and logs its real result, so only the lines from the reload marker on are the
        # job's log. The reload part is kept apart for diagnosis.
        r = next((k for k in range(i, j + 1) if "ABFMCP_WARM_RELOAD " in lines[k]), None)
        if r is not None and r > i + 1:
            (run / "reload.log").write_text("\n".join(lines[i + 1:r]) + "\n", encoding="utf-8")
            # errors of the reload phase that are not the known stale-class noise are real (e.g. a package that failed to load)
            reload_errors = [scrub(re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", l).strip()) for l in lines[i + 1:r]
                             if re.search(r"\bError\b", l) and not any(k in l for k in RELOAD_STALE_MARKERS)][:20]
            lines = lines[:i + 1] + lines[r:]
            j -= r - i - 1
        (run / "editor.log").write_text("\n".join(lines[i:j + 1]) + "\n", encoding="utf-8")
        return found, reload_errors

    def submit(self, job, timeout, prepare=None):
        with self._mu:
            with RunLock():
                t_start = time.time()
                try:
                    start_seconds = self._start_locked()
                except WarmError as e:
                    return None, str(e)
                run = new_run(job["op"])
                if prepare is not None:
                    prepare(run)
                jid = run.name
                job_path = run / "job.json"
                job = dict(job, mod=self.name, run=_fwd(run), log=str(run / "editor.log"))
                job_path.write_text(json.dumps(job), encoding="utf-8")
                offset = self._log_size()
                self.seq += 1
                entry = self.dir / "queue" / ("%06d-%s.json" % (self.seq, jid))
                tmp = entry.with_suffix(".tmp")
                tmp.write_text(json.dumps({"id": jid, "job": _fwd(job_path), "run": _fwd(run)}), encoding="utf-8")
                os.replace(str(tmp), str(entry))
                t0 = time.time()
                deadline = t0 + timeout + WARM_SLACK
                state = "done"
                while not (run / "warm.done").is_file():
                    if not self.alive():
                        state = "died"
                        break
                    if time.time() > deadline:
                        state = "timeout"
                        break
                    time.sleep(self.poll)
                if state == "done":
                    found, reload_errors = self._cut_log(run, jid, offset)
                    self.jobs += 1
                    extra = {"warm": True, "total_seconds": round(time.time() - t_start, 1)}
                    if start_seconds:
                        extra["warm_start_seconds"] = round(start_seconds, 1)
                    if reload_errors:
                        extra["reload_errors"] = reload_errors
                    res = _finish(run, job_path, None, False, time.time() - t0, extra)
                    if str(res.get("error") or "").startswith(RELOAD_FAILED):
                        # the packages could not be reloaded and the job did not run: the process holds stale state, so it
                        # goes and the job runs cold (nothing was written, even for an apply)
                        self._kill_locked()
                        return None, "warm reload failed: %s" % str(res["error"])[len(RELOAD_FAILED):].lstrip(": ")[:300]
                    if not found:
                        # the editor.log copy may be cut off, so parse_compile_log could miss errors and a run pass wrongly
                        self._kill_locked()
                        if job["op"] != "apply":
                            return None, "warm log incomplete (no end marker after %.0f s)" % WARM_END_WAIT
                        res["ok"] = False
                        res["warm_failed"] = "warm log incomplete"
                        res["error"] = ("warm log incomplete: the job's end marker never reached the log; the staged files may be "
                                        "changed (the caller restores them)")
                    return res, None
                # the process is dead or stuck: nothing more can be learned from it
                self._kill_locked()
                self._cut_log(run, jid, offset)
                reason = ("the warm process died during the job" if state == "died"
                          else "the warm job exceeded %.0f s (timeout + %.0f s)" % (timeout + WARM_SLACK, WARM_SLACK))
                have_result = (run / "result.json").is_file()
                if have_result or job["op"] == "apply":
                    # a result exists (export writes one early), or an apply may have half-written files: report, never rerun
                    res = _finish(run, job_path, None, state == "timeout", time.time() - t0,
                                  {"warm": True, "warm_failed": reason})
                    if not have_result:
                        res["error"] = "%s; the staged files may be partly written (the caller restores them)" % reason
                    return res, None
                return None, reason


class WarmRegistry:
    """The warm sessions of this server process, one per staged mod."""

    def __init__(self):
        self.sessions = {}
        self.factory = WarmSession

    def get(self, stage):
        s = self.sessions.get(stage.name)
        if s is None:
            s = self.factory(stage)
            self.sessions[stage.name] = s
        s.stage = stage
        return s

    def stop_all(self):
        """Stop every live session; True when one was stopped."""
        any_ = False
        for s in list(self.sessions.values()):
            if s.alive():
                any_ = s.stop() or any_
        return any_

    def status(self, mod=None):
        rows = [s.status() for n, s in sorted(self.sessions.items()) if mod in (None, n)]
        first = next((r for r in rows if r["running"]), rows[0] if rows else None)
        base = first or {"running": False, "pid": None, "jobs": 0, "startedAt": None, "mod": mod}
        return dict(base, sessions=rows)


WARMS = WarmRegistry()
