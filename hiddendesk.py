"""Mod Editor with UI on a hidden Windows desktop.

The editor runs on a desktop created for the run (CreateDesktopW + CreateProcessW with lpDesktop). Nothing here touches
the user's desktop: windows come only from EnumDesktopWindows on OUR desktop handle, filtered by OUR process id, and the
only messages sent are PostMessageW calls to those windows. The visible desktop is never switched, nothing is brought to
the foreground, and the real cursor and keyboard are never used. Screenshots reuse wininput.capture (PrintWindow) on
those window handles only.

Fragile by nature: the Mod Browser picker opens first and its recent-mod tile has to be clicked. The tile position was
measured on build 5.4.4-1050002; a kit update can move it. ABF_PICKER_TILE="x,y" (window pixels) overrides it.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import secrets
import time
from pathlib import Path

import headless
import staging
import wininput

PICKER_TILE_SLATE = (946, 200)     # first recent-mod tile; measured at 125% DPI in a 1495x1078 px window
PICKER_TILE_RATIO = (1183 / 1495, 250 / 1078)
SETTINGS_SECTION = "[/Script/AbioticFactorEditorCooked.AbioticFactorModEditorSettings]"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.CreateDesktopW.restype = ctypes.c_void_p
user32.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
user32.CloseDesktop.argtypes = [ctypes.c_void_p]
user32.EnumDesktopWindows.argtypes = [ctypes.c_void_p, WNDENUM, wt.LPARAM]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
try:
    user32.GetDpiForWindow.argtypes = [wt.HWND]
    user32.GetDpiForWindow.restype = wt.UINT
except AttributeError:
    pass
kernel32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                    ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wt.UINT]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class _Proc:
    def __init__(self, returncode):
        self.returncode = returncode


def write_recent_mod(userdir, abfmod_path):
    """Make the staged mod the only entry in the Mod Browser's recent list; the rest of the ini is kept."""
    ini = Path(userdir) / "Saved" / "Config" / "WindowsEditor" / "EditorSettings.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    keep, skipping = [], False
    if ini.is_file():
        for line in ini.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.strip().startswith("["):
                skipping = line.strip() == SETTINGS_SECTION
            if not skipping:
                keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    stamp = time.strftime("%Y.%m.%d-%H.%M.%S")
    keep += ["", SETTINGS_SECTION, 'RecentlyOpenedMods=(ProjectName="%s",LastOpenTime=%s)' % (str(abfmod_path).replace("\\", "/"), stamp), ""]
    ini.write_text("\n".join(keep), encoding="utf-8")


def _windows(desk, pid):
    """Visible windows of our process on OUR desktop: [(hwnd, title, left, top, w, h)]."""
    out = []

    def cb(hw, _lp):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hw, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hw):
            b = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hw, b, 256)
            r = wt.RECT()
            user32.GetWindowRect(hw, ctypes.byref(r))
            out.append((hw, b.value, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    fn = WNDENUM(cb)
    user32.EnumDesktopWindows(desk, fn, 0)
    return out


def _capture(hwnd, path, max_width):
    png, _f, _size = wininput.capture({"hwnd": hwnd}, max_width)
    Path(path).write_bytes(png)
    return str(path)


def _picker_candidates(win):
    _hw, _t, left, top, w, h = win[:6]
    cands = []
    env = os.environ.get("ABF_PICKER_TILE")
    if env:
        try:
            x, y = (float(v) for v in env.split(","))
            cands.append((left + x, top + y))
        except ValueError:
            pass
    try:
        s = user32.GetDpiForWindow(win[0]) / 96.0
    except Exception:
        s = 1.0
    cands.append((left + PICKER_TILE_SLATE[0] * s, top + PICKER_TILE_SLATE[1] * s))
    cands.append((left + PICKER_TILE_RATIO[0] * w, top + PICKER_TILE_RATIO[1] * h))
    return [(int(x), int(y)) for x, y in cands]


def _post_click_sequence(hwnd, wx, wy):
    """Window messages only: nothing reaches the real input queue. (wx, wy) is the point in window pixels; the gaps are
    slow on purpose: 50 ms gaps were not enough for the picker to register the click."""
    lp = ((int(wy) & 0xFFFF) << 16) | (int(wx) & 0xFFFF)
    user32.PostMessageW(hwnd, 0x001C, 1, 0)        # WM_ACTIVATEAPP
    user32.PostMessageW(hwnd, 0x0006, 1, 0)        # WM_ACTIVATE
    for _ in range(3):
        user32.PostMessageW(hwnd, 0x0200, 0, lp)   # WM_MOUSEMOVE
        time.sleep(0.2)
    time.sleep(1.0)
    for msg, wp in ((0x0201, 1), (0x0202, 0), (0x0203, 1), (0x0202, 0)):   # down, up, double-click, up
        user32.PostMessageW(hwnd, msg, wp, lp)
        time.sleep(0.4)


def run_editor(stage, job, timeout=420):
    """Run job['op'] (shots, audit, live_publish_test) in an editor on a hidden desktop. Same shape as
    headless.run_commandlet, plus "shots": [png paths]."""
    exe = headless._exe()
    abfmods = sorted(stage.dir.glob("*.abfmod"))
    if not abfmods:
        raise headless.HeadlessError("the staged mod has no .abfmod file; the Mod Browser picker needs one to open it")
    with headless.RunLock():
        run = headless.new_run(job["op"])
        job_path = run / "job.json"
        log = run / "editor.log"
        job = dict(job, mod=stage.name, run=str(run).replace("\\", "/"), log=str(log))
        job_path.write_text(json.dumps(job), encoding="utf-8")
        staging.set_uplugin(stage, "picker")
        userdir = str(headless.root() / "ud").replace("\\", "/")
        write_recent_mod(headless.root() / "ud", abfmods[0])
        env = headless.child_env(run, job_path)
        env_block = "".join("%s=%s\0" % (k, v) for k, v in env.items()) + "\0"
        cmd = '"%s" -userdir=%s -nosplash -nopause "-ExecCmds=py %s" -abslog=%s' % (
            exe, userdir, str(run / "abf_job.py").replace("\\", "/"), log)
        desk_name = "ABFMCP_%d_%s" % (os.getpid(), secrets.token_hex(3))
        job_obj = headless.KillOnCloseJob()
        desk = user32.CreateDesktopW(desk_name, None, None, 0, 0x000F01FF, None)
        if not desk:
            job_obj.close()
            raise headless.HeadlessError("CreateDesktopW failed (error %d)" % ctypes.get_last_error())
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = "WinSta0\\" + desk_name
        pi = PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmd)
        t0 = time.time()
        shots, done_tags = [], set()
        code = wt.DWORD(1)
        timed_out, picker_error, picker_done = False, None, False
        try:
            ok = kernel32.CreateProcessW(None, buf, None, None, False, 0x400, ctypes.create_unicode_buffer(env_block), None,
                                         ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise headless.HeadlessError("CreateProcessW failed (error %d)" % ctypes.get_last_error())
            job_obj.assign(pi.hProcess)                    # the editor and anything it starts (CrashReportClient) die with the job
            deadline = t0 + timeout
            while True:
                if (run / "result.json").is_file():
                    kernel32.WaitForSingleObject(pi.hProcess, 10000)        # let it exit by itself so the log tail is flushed
                    break
                if kernel32.WaitForSingleObject(pi.hProcess, 0) == 0:
                    break
                if time.time() > deadline:
                    timed_out = True
                    break
                wins = _windows(desk, pi.dwProcessId)
                if not picker_done and not (run / "mounted").exists():
                    pk = [w for w in wins if "Mod Browser" in w[1]]
                    if pk:
                        picker_done = True
                        (run / "picker_windows.json").write_text(json.dumps([[str(w[0]), w[1], w[2], w[3], w[4], w[5]] for w in wins]), encoding="utf-8")
                        time.sleep(float(os.environ.get("ABF_PICKER_SETTLE", "8")))   # the tile list fills in after the window appears
                        for x, y in _picker_candidates(pk[0]):
                            if (run / "cursor_ok").exists():
                                (run / "cursor_ok").unlink()
                            (run / "cursor.json").write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
                            for _ in range(100):
                                if (run / "cursor_ok").exists() or kernel32.WaitForSingleObject(pi.hProcess, 0) == 0:
                                    break
                                time.sleep(0.1)
                            _post_click_sequence(pk[0][0], x - pk[0][2], y - pk[0][3])
                            for _ in range(200):
                                if (run / "mounted").exists() or kernel32.WaitForSingleObject(pi.hProcess, 0) == 0:
                                    break
                                time.sleep(0.1)
                            if (run / "mounted").exists():
                                break
                        if not (run / "mounted").exists() and kernel32.WaitForSingleObject(pi.hProcess, 0) != 0:
                            fail = run / "picker_fail.png"
                            try:
                                _capture(pk[0][0], fail, int(job.get("max_width", 1600)))
                            except Exception:
                                pass
                            picker_error = ("the Mod Browser tile did not open the staged mod (tried %s). Screenshot of the picker: %s. "
                                            "The tile position depends on the picker layout of this editor build; set ABF_PICKER_TILE=\"x,y\" "
                                            "(window pixels of the first recent-mod tile) to override it." % (_picker_candidates(pk[0]), fail))
                            break
                for req in sorted(run.glob("shotreq_*")):
                    tag = req.name[len("shotreq_"):]
                    if tag in done_tags:
                        continue
                    done_tags.add(tag)
                    big = sorted((w for w in wins if w[4] > 200 and w[5] > 200), key=lambda w: -w[4] * w[5])[:2]
                    for n, w in enumerate(big):
                        try:
                            shots.append(_capture(w[0], run / ("shot_%s_%d.png" % (tag, n)), int(job.get("max_width", 1600))))
                        except Exception as e:
                            shots.append("capture failed: %s" % e)
                    (run / ("shotdone_" + tag)).write_text("", encoding="utf-8")
                time.sleep(0.4)
        finally:
            if pi.hProcess and kernel32.WaitForSingleObject(pi.hProcess, 0) != 0:
                kernel32.TerminateProcess(pi.hProcess, 1)          # our own child only
                kernel32.WaitForSingleObject(pi.hProcess, 5000)
            if pi.hProcess:
                kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            job_obj.close()                                # kills leftovers so no process keeps the desktop alive
            if pi.hProcess:
                kernel32.CloseHandle(pi.hProcess)
            if pi.hThread:
                kernel32.CloseHandle(pi.hThread)
            user32.CloseDesktop(desk)
        out = headless._finish(run, job_path, _Proc(code.value), timed_out, time.time() - t0, {"shots": shots})
        if picker_error and not out["ok"]:
            out["error"] = picker_error
        return out
