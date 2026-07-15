from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from documents.models import ArchiveItem, PhotoContent
from documents.s3 import S3DeleteObjectResult
from documents.services.photo_s3_orphan_cleanup import (
    PHOTO_S3_REFERENCE_FIELDS,
    S3ListedObject,
    collect_referenced_photo_s3_keys,
    normalize_and_validate_s3_prefix,
)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class CleanupPhotoS3OrphansCommandTests(TestCase):
    SERVICE_MODULE = "documents.services.photo_s3_orphan_cleanup"

    def setUp(self):
        self.now = timezone.now()
        self.stale_modified = self.now - timedelta(hours=30)
        self.recent_modified = self.now - timedelta(hours=2)

    def _create_photo(
        self,
        *,
        original_file_key: str = "",
        thumbnail_file_key: str = "",
    ) -> PhotoContent:
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Test photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        return PhotoContent.objects.create(
            archive_item=item,
            original_file_key=original_file_key,
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            thumbnail_file_key=thumbnail_file_key,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            upload_error="",
        )

    def _listed_object(
        self,
        key: str,
        *,
        last_modified: datetime | None = None,
        size: int = 1024,
    ) -> S3ListedObject:
        return S3ListedObject(
            key=key,
            last_modified=last_modified or self.stale_modified,
            size=size,
        )

    def _mock_list_objects(self, objects: list[S3ListedObject]):
        return patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            return_value=objects,
        )

    def test_reference_fields_constant_contains_original_and_thumbnail(self):
        self.assertEqual(
            PHOTO_S3_REFERENCE_FIELDS,
            (
                "original_file_key",
                "thumbnail_file_key",
            ),
        )

    def test_collect_referenced_keys_includes_original_and_thumbnail(self):
        photo = self._create_photo(
            original_file_key="photos/10/original.jpg",
            thumbnail_file_key="photos/10/thumb_400.jpg",
        )

        referenced = collect_referenced_photo_s3_keys()

        self.assertIn(photo.original_file_key, referenced)
        self.assertIn(photo.thumbnail_file_key, referenced)

    def test_referenced_objects_are_excluded_from_candidates(self):
        photo = self._create_photo(
            original_file_key="photos/20/original.jpg",
            thumbnail_file_key="photos/20/thumb_400.jpg",
        )
        orphan_key = "photos/20/orphan.jpg"
        objects = [
            self._listed_object(photo.original_file_key),
            self._listed_object(photo.thumbnail_file_key),
            self._listed_object(orphan_key),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_photo_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn(orphan_key, output)
        self.assertNotIn(photo.original_file_key, output)
        self.assertNotIn(photo.thumbnail_file_key, output)
        self.assertIn("Candidates: 1", output)

    def test_old_orphan_is_reported_in_dry_run(self):
        orphan_key = "photos/30/orphan-old.jpg"

        stdout = StringIO()
        with self._mock_list_objects([self._listed_object(orphan_key, size=2048)]):
            call_command(
                "cleanup_photo_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertIn(f"key={orphan_key}", output)
        self.assertIn("size: 2048", output)
        self.assertIn("Candidates: 1", output)

    def test_recent_object_is_excluded(self):
        objects = [
            self._listed_object(
                "photos/31/recent.jpg",
                last_modified=self.recent_modified,
            )
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_photo_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("Candidates: 0", output)
        self.assertIn("No orphaned photo S3 objects found.", output)

    def test_dry_run_never_deletes(self):
        objects = [self._listed_object("photos/40/orphan.jpg")]

        with (
            self._mock_list_objects(objects),
            patch(f"{self.SERVICE_MODULE}.delete_s3_object") as mock_delete,
        ):
            call_command(
                "cleanup_photo_s3_orphans",
                "--older-than-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()

    def test_commit_deletes_candidate(self):
        orphan_key = "photos/50/orphan.jpg"

        with (
            self._mock_list_objects([self._listed_object(orphan_key)]),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ) as mock_delete,
        ):
            call_command(
                "cleanup_photo_s3_orphans",
                "--commit",
                "--older-than-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_called_once_with("test-uploads-bucket", orphan_key)

    def test_missing_object_is_treated_as_success(self):
        orphan_key = "photos/51/gone.jpg"
        stdout = StringIO()

        with (
            self._mock_list_objects([self._listed_object(orphan_key)]),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(
                    deleted=False,
                    not_found=True,
                ),
            ),
        ):
            call_command(
                "cleanup_photo_s3_orphans",
                "--commit",
                stdout=stdout,
            )

        self.assertIn("s3_keys_not_found: 1", stdout.getvalue())

    def test_one_delete_failure_does_not_stop_later_deletion(self):
        first_key = "photos/60/fail.jpg"
        second_key = "photos/60/ok.jpg"
        objects = [
            self._listed_object(
                first_key,
                last_modified=self.stale_modified,
            ),
            self._listed_object(
                second_key,
                last_modified=self.stale_modified + timedelta(minutes=1),
            ),
        ]

        def delete_side_effect(_bucket, key):
            if key == first_key:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "AccessDenied",
                            "Message": "denied",
                        }
                    },
                    "DeleteObject",
                )
            return S3DeleteObjectResult(deleted=True)

        stderr = StringIO()
        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                side_effect=delete_side_effect,
            ) as mock_delete,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_photo_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                    stderr=stderr,
                )

        self.assertIn(first_key, stderr.getvalue())
        self.assertIn("AccessDenied", stderr.getvalue())
        mock_delete.assert_any_call("test-uploads-bucket", second_key)

    def test_prefix_filter_limits_scan(self):
        all_objects = [
            self._listed_object("photos/70/a/orphan.jpg"),
            self._listed_object("photos/71/b/orphan.jpg"),
        ]

        def list_side_effect(_bucket, prefix):
            return [obj for obj in all_objects if obj.key.startswith(prefix)]

        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            side_effect=list_side_effect,
        ) as mock_list:
            call_command(
                "cleanup_photo_s3_orphans",
                "--prefix=photos/70/",
                stdout=stdout,
            )

        mock_list.assert_called_once_with(
            "test-uploads-bucket",
            "photos/70/",
        )
        output = stdout.getvalue()
        self.assertIn("photos/70/a/orphan.jpg", output)
        self.assertNotIn("photos/71/b/orphan.jpg", output)
        self.assertIn("Candidates: 1", output)

    def test_unsafe_prefixes_are_rejected(self):
        unsafe_prefixes = [
            "",
            "   ",
            "documents/1/",
            "uploads/1/",
            "photos/../",
            "photos/foo/../bar/",
        ]

        for prefix in unsafe_prefixes:
            with self.subTest(prefix=prefix):
                with self.assertRaises(CommandError):
                    call_command(
                        "cleanup_photo_s3_orphans",
                        f"--prefix={prefix}",
                        stdout=StringIO(),
                    )

    def test_prefix_normalization(self):
        self.assertEqual(
            normalize_and_validate_s3_prefix("/photos/99"),
            "photos/99/",
        )

    def test_limit_restricts_candidate_count(self):
        objects = [
            self._listed_object(
                "photos/80/a.jpg",
                last_modified=self.stale_modified,
            ),
            self._listed_object(
                "photos/80/b.jpg",
                last_modified=self.stale_modified + timedelta(minutes=1),
            ),
            self._listed_object(
                "photos/80/c.jpg",
                last_modified=self.stale_modified + timedelta(minutes=2),
            ),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_photo_s3_orphans",
                "--limit=2",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("Candidates: 2", output)
        self.assertIn("photos/80/a.jpg", output)
        self.assertIn("photos/80/b.jpg", output)
        self.assertNotIn("photos/80/c.jpg", output)

    def test_json_output_includes_candidates_and_commit_results(self):
        orphan_key = "photos/90/orphan.jpg"
        stdout = StringIO()

        with (
            self._mock_list_objects([self._listed_object(orphan_key, size=333)]),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ),
        ):
            call_command(
                "cleanup_photo_s3_orphans",
                "--json",
                "--commit",
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "commit")
        self.assertEqual(payload["prefix"], "photos/")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["key"], orphan_key)
        self.assertEqual(payload["candidates"][0]["size"], 333)
        self.assertEqual(payload["commit"]["s3_keys_deleted"], 1)

    def test_json_mode_reports_delete_failure_and_raises(self):
        orphan_key = "photos/95/fail.jpg"
        stdout = StringIO()

        with (
            self._mock_list_objects([self._listed_object(orphan_key)]),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                side_effect=ClientError(
                    {
                        "Error": {
                            "Code": "AccessDenied",
                            "Message": "denied",
                        }
                    },
                    "DeleteObject",
                ),
            ),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_photo_s3_orphans",
                    "--json",
                    "--commit",
                    stdout=stdout,
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "commit")
        self.assertEqual(payload["commit"]["s3_keys_deleted"], 0)
        self.assertEqual(
            payload["commit"]["s3_delete_failures"][0]["s3_key"],
            orphan_key,
        )
        self.assertIn(
            "AccessDenied",
            payload["commit"]["s3_delete_failures"][0]["error"],
        )

    def test_deterministic_ordering(self):
        objects = [
            self._listed_object(
                "photos/100/z.jpg",
                last_modified=self.stale_modified + timedelta(minutes=5),
            ),
            self._listed_object(
                "photos/100/a.jpg",
                last_modified=self.stale_modified,
            ),
            self._listed_object(
                "photos/100/b.jpg",
                last_modified=self.stale_modified,
            ),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_photo_s3_orphans",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertLess(
            output.index("photos/100/a.jpg"),
            output.index("photos/100/b.jpg"),
        )
        self.assertLess(
            output.index("photos/100/b.jpg"),
            output.index("photos/100/z.jpg"),
        )

    def test_invalid_arguments_are_rejected(self):
        invalid_args = (
            "--older-than-hours=0",
            "--limit=0",
            "--limit=-1",
        )

        for argument in invalid_args:
            with self.subTest(argument=argument):
                with self.assertRaises(CommandError):
                    call_command(
                        "cleanup_photo_s3_orphans",
                        argument,
                        stdout=StringIO(),
                    )

    def test_missing_bucket_raises_command_error(self):
        with (
            override_settings(UPLOADS_BUCKET_NAME=""),
            self._mock_list_objects([]),
        ):
            with self.assertRaises(CommandError) as dry_run_context:
                call_command(
                    "cleanup_photo_s3_orphans",
                    stdout=StringIO(),
                )

            with self.assertRaises(CommandError) as commit_context:
                call_command(
                    "cleanup_photo_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                )

        self.assertIn(
            "UPLOADS_BUCKET_NAME",
            str(dry_run_context.exception),
        )
        self.assertIn(
            "UPLOADS_BUCKET_NAME",
            str(commit_context.exception),
        )

    def test_unexpected_delete_result_fails_command(self):
        orphan_key = "photos/115/unexpected.jpg"
        stderr = StringIO()

        with (
            self._mock_list_objects([self._listed_object(orphan_key)]),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(
                    deleted=False,
                    not_found=False,
                ),
            ),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_photo_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                    stderr=stderr,
                )

        self.assertIn(orphan_key, stderr.getvalue())
        self.assertIn(
            "unexpected delete result",
            stderr.getvalue().lower(),
        )

    def test_whitespace_only_reference_keys_are_ignored(self):
        self._create_photo(
            original_file_key="   ",
            thumbnail_file_key=" \t ",
        )
        orphan_key = "photos/130/orphan.jpg"

        stdout = StringIO()
        with self._mock_list_objects([self._listed_object(orphan_key)]):
            call_command(
                "cleanup_photo_s3_orphans",
                stdout=stdout,
            )

        self.assertIn(orphan_key, stdout.getvalue())
        self.assertEqual(collect_referenced_photo_s3_keys(), set())

    def test_naive_last_modified_is_supported(self):
        naive_modified = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(
            hours=30
        )

        stdout = StringIO()
        with self._mock_list_objects(
            [
                self._listed_object(
                    "photos/140/naive.jpg",
                    last_modified=naive_modified,
                )
            ]
        ):
            call_command(
                "cleanup_photo_s3_orphans",
                stdout=stdout,
            )

        self.assertIn("photos/140/naive.jpg", stdout.getvalue())
        self.assertIn("Candidates: 1", stdout.getvalue())
