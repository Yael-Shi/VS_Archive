from __future__ import annotations

import unittest

import aws_cdk as cdk
from vs_archive_infra.worker_scaling import resolve_worker_desired_count


class WorkerDesiredCountContextTests(unittest.TestCase):
    @staticmethod
    def _resolve(context: dict[str, object] | None = None) -> int:
        app = cdk.App(context=context or {})
        stack = cdk.Stack(app, "WorkerDesiredCountContextTest")
        return resolve_worker_desired_count(stack)

    def test_defaults_to_one_worker(self) -> None:
        self.assertEqual(self._resolve(), 1)

    def test_accepts_zero_as_string(self) -> None:
        self.assertEqual(self._resolve({"worker_desired_count": "0"}), 0)

    def test_accepts_zero_as_integer(self) -> None:
        self.assertEqual(self._resolve({"worker_desired_count": 0}), 0)

    def test_accepts_one_as_string(self) -> None:
        self.assertEqual(self._resolve({"worker_desired_count": "1"}), 1)

    def test_rejects_unsupported_values(self) -> None:
        for value in ("2", 2, -1, "invalid", False, True):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    (TypeError, ValueError),
                    "worker_desired_count must be 0 or 1",
                ),
            ):
                self._resolve({"worker_desired_count": value})


if __name__ == "__main__":
    unittest.main()
