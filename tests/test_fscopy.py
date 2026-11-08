"""copy_file_retry: a sharing violation (WinError 32) is retried with backoff, other errors are not."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fscopy  # noqa: E402
import headless  # noqa: E402
import headless_tools  # noqa: E402
from unittest import mock  # noqa: E402


def sharing(code=32):
    e = PermissionError(13, "The process cannot access the file because it is being used by another process")
    e.winerror = code
    return e


class CopyRetryTests(unittest.TestCase):
    def test_retries_a_sharing_violation_with_growing_pauses(self):
        calls, pauses = [], []

        def copy(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise sharing()
            return dst

        self.assertEqual(fscopy.copy_file_retry("a", "b", sleep=pauses.append, copy=copy), "b")
        self.assertEqual(len(calls), 3)
        self.assertEqual(pauses, [0.1, 0.2])

    def test_other_errors_are_not_retried(self):
        calls = []

        def copy(src, dst):
            calls.append(1)
            raise PermissionError(13, "denied")

        with self.assertRaises(PermissionError):
            fscopy.copy_file_retry("a", "b", sleep=lambda s: None, copy=copy)
        self.assertEqual(len(calls), 1)

    def test_falls_back_to_reading_the_bytes_and_raises_the_violation_when_that_fails_too(self):
        def copy(src, dst):
            raise sharing(33)

        got = []
        self.assertEqual(fscopy.copy_file_retry("a", "b", delays=(0,), sleep=lambda s: None, copy=copy, fallback=lambda s, d: got.append((s, d))), "b")
        self.assertEqual(got, [("a", "b")])

        def failing(s, d):
            raise OSError("still locked")

        with self.assertRaises(PermissionError):
            fscopy.copy_file_retry("a", "b", delays=(0,), sleep=lambda s: None, copy=copy, fallback=failing)

    def test_patience_keeps_polling_after_the_backoff_until_the_file_is_free(self):
        calls, pauses, now = [], [], [0.0]

        def sleep(s):
            pauses.append(s)
            now[0] += s

        def copy(src, dst):
            calls.append(1)
            if len(calls) < 12:
                raise sharing()
            return dst

        self.assertEqual(fscopy.copy_file_retry("a", "b", delays=(0.5,), sleep=sleep, copy=copy, patience=60, clock=lambda: now[0]), "b")
        self.assertEqual(len(calls), 12)
        self.assertEqual(pauses, [0.5] + [fscopy.PATIENCE_POLL] * 10)

    def test_patience_is_bounded_and_the_violation_is_raised_at_the_end(self):
        pauses, now = [], [0.0]

        def sleep(s):
            pauses.append(s)
            now[0] += s

        def copy(src, dst):
            raise sharing()

        def failing(s, d):
            raise OSError("still locked")

        with self.assertRaises(PermissionError):
            fscopy.copy_file_retry("a", "b", delays=(), sleep=sleep, copy=copy, fallback=failing, patience=5, clock=lambda: now[0])
        self.assertEqual(sum(pauses), 5)

    def test_patience_does_not_retry_other_errors(self):
        calls = []

        def copy(src, dst):
            calls.append(1)
            raise PermissionError(13, "denied")

        with self.assertRaises(PermissionError):
            fscopy.copy_file_retry("a", "b", delays=(), sleep=lambda s: None, copy=copy, patience=60)
        self.assertEqual(len(calls), 1)


class StageFileBusyTests(unittest.TestCase):
    def busy(self, pids, **kw):
        kw = kw or {"nothing_changed": True}

        def stuck(src, dst, **k):
            self.assertEqual(k.get("patience"), fscopy.PATIENCE)
            raise sharing()

        with mock.patch.object(fscopy, "copy_file_retry", side_effect=stuck), mock.patch.object(headless_tools, "hidden_pids", return_value=pids):
            with self.assertRaises(headless_tools.StageFileBusy) as cm:
                headless_tools.copy_stage_file("C:/stage/mod/Content/X/BP_A.uasset", "dst", **kw)
        return str(cm.exception)

    def test_nothing_changed_is_only_said_for_the_copies_taken_before_a_change(self):
        msg = self.busy([], nothing_changed=True)
        self.assertIn("Nothing was changed", msg)
        self.assertNotIn("partly restored", msg)

    def test_a_rollback_or_restore_copy_says_the_stage_is_partly_restored_and_names_the_backup(self):
        msg = self.busy([], kept="C:/snapshots/mod-1")
        self.assertNotIn("Nothing was changed", msg)
        self.assertIn("partly restored", msg)
        self.assertIn("C:/snapshots/mod-1", msg)
        no_path = self.busy([], kept=None)
        self.assertNotIn("Nothing was changed", no_path)
        self.assertIn("partly restored", no_path)

    def test_a_hidden_editor_of_a_previous_run_is_named(self):
        msg = self.busy([4711, 4712])
        self.assertIn("BP_A.uasset", msg)
        self.assertIn("previous run (pid 4711, 4712)", msg)
        self.assertIn("run the command again", msg)
        self.assertNotIn("C:/stage", msg)

    def test_without_a_hidden_editor_another_program_is_blamed(self):
        msg = self.busy([])
        self.assertIn("another program", msg)
        self.assertIn("wait a moment", msg)

    def test_the_error_is_a_plain_headless_error_so_the_server_prints_only_the_text(self):
        self.assertTrue(issubclass(headless_tools.StageFileBusy, headless.HeadlessError))

    def test_other_errors_pass_through(self):
        with mock.patch.object(fscopy, "copy_file_retry", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                headless_tools.copy_stage_file("a", "b")

    def test_hidden_pids_keeps_only_our_own_userdir(self):
        with mock.patch.object(headless, "root", return_value=Path("C:/Users/x/AppData/Local/tool/headless")):
            rows = [{"pid": 1, "command_line": 'exe -run=pythonscript -userdir=C:/Users/x/AppData/Local/tool/headless/ud -abslog=l'},
                    {"pid": 2, "command_line": 'exe -Installed -userdir=D:/other/ud'},
                    {"pid": 3, "command_line": None}]
            self.assertEqual(headless_tools.hidden_pids(rows), [1])


if __name__ == "__main__":
    unittest.main()
