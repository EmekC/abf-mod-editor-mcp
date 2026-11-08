"""copy_file_retry: a sharing violation (WinError 32) is retried with backoff, other errors are not."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fscopy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
