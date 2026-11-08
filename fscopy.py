"""File copy that survives a file another process holds open without sharing (Windows WinError 32/33)."""
import shutil
import time

SHARING_WINERRORS = (32, 33)    # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
COPY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6, 3.2)
PATIENCE = 60.0        # seconds a caller that cannot go on without the copy keeps trying after the backoff
PATIENCE_POLL = 1.0


def _copy_bytes(src, dst):
    with open(src, "rb") as a:
        data = a.read()
    with open(dst, "wb") as b:
        b.write(data)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass


def copy_file_retry(src, dst, delays=None, sleep=time.sleep, copy=shutil.copy2, fallback=_copy_bytes, patience=0.0, clock=time.monotonic):
    """shutil.copy2 retried with a growing pause on a sharing violation (a hidden editor still shutting down, an antivirus scan,
    the indexer), then every PATIENCE_POLL seconds until `patience` seconds have passed since the first try (0 = not at all),
    then once more by reading the bytes (a plain open shares more than CopyFile2). Any other error is raised at once; the last
    sharing violation is raised when everything failed."""
    last = None
    start = clock()
    for pause in list(COPY_DELAYS if delays is None else delays) + [None]:
        try:
            return copy(src, dst)
        except PermissionError as e:
            if getattr(e, "winerror", None) not in SHARING_WINERRORS:
                raise
            last = e
        if pause is not None:
            sleep(pause)
    while patience and clock() - start < patience:
        sleep(PATIENCE_POLL)
        try:
            return copy(src, dst)
        except PermissionError as e:
            if getattr(e, "winerror", None) not in SHARING_WINERRORS:
                raise
            last = e
    try:
        fallback(src, dst)
        return dst
    except OSError:
        raise last
