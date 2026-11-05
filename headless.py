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
    prepare(run) is called with the new run folder before the process starts."""
    return _launch(stage, job, "cmd", None, timeout, prepare)


def run_game(stage, job, map_path="/Game/Maps/Empty", timeout=180):
    if not re.match(r"^/(Game|%s)/[A-Za-z0-9_/]+$" % re.escape(stage.name), map_path or ""):
        raise HeadlessError("map %r is not a plain /Game or /<Mod> package path" % (map_path,))
    return _launch(stage, job, "game", map_path, timeout)
