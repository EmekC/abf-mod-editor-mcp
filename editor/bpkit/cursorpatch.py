"""Cursor patch for editors on a hidden desktop. Runs only inside such a process.

GetCursorPos fails (error 5) on a hidden desktop, which leaves Slate without a pointer position. The patch replaces
its first bytes with a jump to a tiny stub that returns fixed coordinates the host can change through cursor.json.
install() refuses unless ABF_MCP_HIDDEN=1, so it can never patch the user's own editor.
"""
import ctypes
import os
import struct

_k = ctypes.WinDLL("kernel32")
_k.VirtualAlloc.restype = ctypes.c_void_p
_k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint]
_k.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
_k.GetProcAddress.restype = ctypes.c_void_p
_k.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
_k.GetModuleHandleW.restype = ctypes.c_void_p
_k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]


def install():
    if os.environ.get("ABF_MCP_HIDDEN") != "1":
        raise RuntimeError("cursorpatch refuses to run outside a hidden MCP process (ABF_MCP_HIDDEN is not 1)")
    mem = _k.VirtualAlloc(None, 4096, 0x3000, 0x40)
    if not mem:
        raise RuntimeError("VirtualAlloc failed")
    data = mem + 0x80
    code = bytearray()

    def load(off):
        ip = mem + len(code) + 6
        code.extend(b"\x8b\x05" + struct.pack("<i", data + off - ip))

    # mov eax,[x]; mov [rcx],eax; mov eax,[y]; mov [rcx+4],eax; mov eax,1; ret
    load(0)
    code.extend(b"\x89\x01")
    load(4)
    code.extend(b"\x89\x41\x04\xb8\x01\x00\x00\x00\xc3")
    ctypes.memmove(mem, bytes(code), len(code))
    fn = _k.GetProcAddress(_k.GetModuleHandleW("user32.dll"), b"GetCursorPos")
    if not fn:
        raise RuntimeError("GetCursorPos not found")
    old = ctypes.c_uint()
    _k.VirtualProtect(fn, 14, 0x40, ctypes.byref(old))
    ctypes.memmove(fn, b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", mem), 14)
    _k.VirtualProtect(fn, 14, old.value, ctypes.byref(old))
    return data


def setpos(data, x, y):
    ctypes.memmove(data, struct.pack("<ii", int(x), int(y)), 8)
