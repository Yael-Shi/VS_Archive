from __future__ import annotations

import unittest

import aws_cdk as cdk
from vs_archive_infra.image_tags import resolve_image_tags


class ImageTagContextTests(unittest.TestCase):
    @staticmethod
    def _resolve(context: dict[str, str] | None = None) -> tuple[str, str]:
        app = cdk.App(context=context or {})
        stack = cdk.Stack(app, "ImageTagContextTest")
        return resolve_image_tags(stack)

    def test_defaults_both_services_to_dev(self) -> None:
        self.assertEqual(
            self._resolve(),
            ("dev", "dev"),
        )

    def test_legacy_image_tag_remains_shared(self) -> None:
        self.assertEqual(
            self._resolve({"image_tag": "shared-release"}),
            ("shared-release", "shared-release"),
        )

    def test_service_specific_tags_override_shared_tag(self) -> None:
        self.assertEqual(
            self._resolve(
                {
                    "image_tag": "shared-release",
                    "web_image_tag": "web-release",
                    "worker_image_tag": "worker-release",
                }
            ),
            ("web-release", "worker-release"),
        )

    def test_missing_service_override_uses_shared_tag(self) -> None:
        self.assertEqual(
            self._resolve(
                {
                    "image_tag": "shared-release",
                    "worker_image_tag": "worker-release",
                }
            ),
            ("shared-release", "worker-release"),
        )


if __name__ == "__main__":
    unittest.main()
