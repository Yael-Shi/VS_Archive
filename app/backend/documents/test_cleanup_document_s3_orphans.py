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

from documents.models import Document, DocumentSourceFile
from documents.s3 import S3DeleteObjectResult
from documents.services.archive_items import create_ocr_document
from documents.services.document_s3_orphan_cleanup import (
    DOCUMENT_S3_REFERENCE_FIELDS,
    S3ListedObject,
    collect_referenced_document_s3_keys,
    normalize_and_validate_s3_prefix,
)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class CleanupDocumentS3OrphansCommandTests(TestCase):
    SERVICE_MODULE = "documents.services.document_s3_orphan_cleanup"

    def setUp(self):
        self.now = timezone.now()
        self.stale_modified = self.now - timedelta(hours=30)
        self.recent_modified = self.now - timedelta(hours=2)

    def _create_document(self, *, file_s3_key: str = "") -> Document:
        return create_ocr_document(
            title="Test document",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key=file_s3_key,
        )

    def _add_source_file(
        self,
        document: Document,
        *,
        order_index: int = 0,
        file_s3_key: str | None = None,
    ) -> DocumentSourceFile:
        key = file_s3_key or f"documents/{document.pk}/source/{order_index}.jpeg"
        return DocumentSourceFile.objects.create(
            document=document,
            order_index=order_index,
            file_s3_key=key,
            file_original_name=f"page-{order_index}.jpg",
            mime_type="image/jpeg",
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
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

    def test_reference_fields_constant_documents_both_s3_key_fields(self):
        self.assertEqual(
            DOCUMENT_S3_REFERENCE_FIELDS,
            (
                ("Document", "file_s3_key"),
                ("DocumentSourceFile", "file_s3_key"),
            ),
        )

    def test_collect_referenced_keys_includes_document_and_source_file_fields(self):
        doc = self._create_document(file_s3_key="documents/10/original.jpg")
        source = self._add_source_file(
            doc,
            order_index=0,
            file_s3_key="documents/10/source/0.jpeg",
        )
        self._add_source_file(
            doc,
            order_index=1,
            file_s3_key="documents/10/source/1.jpeg",
        )

        referenced = collect_referenced_document_s3_keys()

        self.assertIn(doc.file_s3_key, referenced)
        self.assertIn(source.file_s3_key, referenced)
        self.assertIn("documents/10/source/1.jpeg", referenced)

    def test_referenced_objects_are_excluded_from_candidates(self):
        doc = self._create_document(file_s3_key="documents/20/original.jpg")
        source = self._add_source_file(doc)

        objects = [
            self._listed_object(doc.file_s3_key),
            self._listed_object(source.file_s3_key),
            self._listed_object("documents/20/orphan.jpg"),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("documents/20/orphan.jpg", output)
        self.assertNotIn(doc.file_s3_key, output)
        self.assertNotIn(source.file_s3_key, output)
        self.assertIn("Candidates: 1", output)

    def test_orphan_objects_older_than_threshold_are_reported(self):
        orphan_key = "documents/30/orphan-old.jpg"
        objects = [self._listed_object(orphan_key, size=2048)]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertIn(f"key={orphan_key}", output)
        self.assertIn("last_modified:", output)
        self.assertIn("size: 2048", output)
        self.assertIn("Candidates: 1", output)

    def test_recent_objects_are_excluded(self):
        objects = [
            self._listed_object(
                "documents/31/recent.jpg",
                last_modified=self.recent_modified,
            )
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("Candidates: 0", output)
        self.assertIn("No orphaned document S3 objects found.", output)

    def test_dry_run_never_deletes(self):
        objects = [self._listed_object("documents/40/orphan.jpg")]

        with (
            self._mock_list_objects(objects),
            patch(f"{self.SERVICE_MODULE}.delete_s3_object") as mock_delete,
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()

    def test_commit_deletes_candidates(self):
        orphan_key = "documents/50/orphan.jpg"
        objects = [self._listed_object(orphan_key)]

        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ) as mock_delete,
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--commit",
                "--older-than-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_called_once_with("test-uploads-bucket", orphan_key)

    def test_missing_objects_are_treated_as_success(self):
        orphan_key = "documents/51/gone.jpg"
        objects = [self._listed_object(orphan_key)]

        stdout = StringIO()
        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=False, not_found=True),
            ),
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--commit",
                stdout=stdout,
            )

        self.assertIn("s3_keys_not_found: 1", stdout.getvalue())

    def test_one_deletion_failure_does_not_stop_later_deletions_and_fails_command(
        self,
    ):
        first_key = "documents/60/fail.jpg"
        second_key = "documents/60/ok.jpg"
        objects = [
            self._listed_object(first_key, last_modified=self.stale_modified),
            self._listed_object(
                second_key,
                last_modified=self.stale_modified + timedelta(minutes=1),
            ),
        ]

        def _delete_side_effect(_bucket, key):
            if key == first_key:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DeleteObject",
                )
            return S3DeleteObjectResult(deleted=True)

        stderr = StringIO()
        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                side_effect=_delete_side_effect,
            ) as mock_delete,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_document_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                    stderr=stderr,
                )

        stderr_text = stderr.getvalue()
        self.assertIn(first_key, stderr_text)
        self.assertIn("AccessDenied", stderr_text)
        mock_delete.assert_any_call("test-uploads-bucket", second_key)

    def test_prefix_filtering_limits_scan_results(self):
        all_objects = [
            self._listed_object("documents/70/a/orphan.jpg"),
            self._listed_object("documents/71/b/orphan.jpg"),
        ]

        def _list_side_effect(_bucket, prefix):
            return [obj for obj in all_objects if obj.key.startswith(prefix)]

        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            side_effect=_list_side_effect,
        ) as mock_list:
            call_command(
                "cleanup_document_s3_orphans",
                "--prefix=documents/70/",
                stdout=stdout,
            )

        mock_list.assert_called_once_with("test-uploads-bucket", "documents/70/")
        output = stdout.getvalue()
        self.assertIn("documents/70/a/orphan.jpg", output)
        self.assertNotIn("documents/71/b/orphan.jpg", output)
        self.assertIn("Candidates: 1", output)

    def test_unsafe_prefixes_are_rejected(self):
        unsafe_prefixes = [
            "",
            "   ",
            "photos/1/",
            "uploads/1/",
            "documents/../",
            "documents/foo/../bar/",
        ]
        for prefix in unsafe_prefixes:
            with self.subTest(prefix=prefix):
                with self.assertRaises(CommandError):
                    call_command(
                        "cleanup_document_s3_orphans",
                        f"--prefix={prefix}",
                        stdout=StringIO(),
                    )

    def test_prefix_normalization_adds_trailing_slash_and_strips_leading_slash(self):
        self.assertEqual(
            normalize_and_validate_s3_prefix("/documents/99"),
            "documents/99/",
        )

    def test_limit_restricts_candidate_count(self):
        objects = [
            self._listed_object(
                "documents/80/a.jpg",
                last_modified=self.stale_modified,
            ),
            self._listed_object(
                "documents/80/b.jpg",
                last_modified=self.stale_modified + timedelta(minutes=1),
            ),
            self._listed_object(
                "documents/80/c.jpg",
                last_modified=self.stale_modified + timedelta(minutes=2),
            ),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                "--limit=2",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("Candidates: 2", output)
        self.assertIn("documents/80/a.jpg", output)
        self.assertIn("documents/80/b.jpg", output)
        self.assertNotIn("documents/80/c.jpg", output)

    def test_json_output_includes_candidates_and_commit_results(self):
        orphan_key = "documents/90/orphan.jpg"
        objects = [self._listed_object(orphan_key, size=333)]

        stdout = StringIO()
        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ),
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--json",
                "--commit",
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "commit")
        self.assertEqual(payload["prefix"], "documents/")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["key"], orphan_key)
        self.assertEqual(payload["candidates"][0]["size"], 333)
        self.assertEqual(payload["commit"]["s3_keys_deleted"], 1)

    def test_deterministic_ordering_by_last_modified_then_key(self):
        objects = [
            self._listed_object(
                "documents/100/z.jpg",
                last_modified=self.stale_modified + timedelta(minutes=5),
            ),
            self._listed_object(
                "documents/100/a.jpg",
                last_modified=self.stale_modified,
            ),
            self._listed_object(
                "documents/100/b.jpg",
                last_modified=self.stale_modified,
            ),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                stdout=stdout,
            )

        output = stdout.getvalue()
        a_pos = output.index("documents/100/a.jpg")
        b_pos = output.index("documents/100/b.jpg")
        z_pos = output.index("documents/100/z.jpg")
        self.assertLess(a_pos, b_pos)
        self.assertLess(b_pos, z_pos)

    def test_invalid_argument_validation(self):
        with self.assertRaises(CommandError):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=0",
                stdout=StringIO(),
            )

        with self.assertRaises(CommandError):
            call_command(
                "cleanup_document_s3_orphans",
                "--limit=0",
                stdout=StringIO(),
            )

        with self.assertRaises(CommandError):
            call_command(
                "cleanup_document_s3_orphans",
                "--limit=-1",
                stdout=StringIO(),
            )

    def test_missing_bucket_raises_command_error_in_dry_run_and_commit(self):
        objects = [self._listed_object("documents/110/orphan.jpg")]

        with (
            override_settings(UPLOADS_BUCKET_NAME=""),
            self._mock_list_objects(objects),
        ):
            with self.assertRaises(CommandError) as dry_run_ctx:
                call_command(
                    "cleanup_document_s3_orphans",
                    stdout=StringIO(),
                )
            self.assertIn("UPLOADS_BUCKET_NAME", str(dry_run_ctx.exception))

            with self.assertRaises(CommandError) as commit_ctx:
                call_command(
                    "cleanup_document_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                )
            self.assertIn("UPLOADS_BUCKET_NAME", str(commit_ctx.exception))

    def test_unexpected_delete_result_is_reported_and_fails_command(self):
        orphan_key = "documents/115/unexpected.jpg"
        objects = [self._listed_object(orphan_key)]

        stderr = StringIO()
        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=False, not_found=False),
            ),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_document_s3_orphans",
                    "--commit",
                    stdout=StringIO(),
                    stderr=stderr,
                )

        self.assertIn(orphan_key, stderr.getvalue())
        self.assertIn("unexpected delete result", stderr.getvalue().lower())

    def test_mixed_aware_and_naive_timestamps_sort_without_error(self):
        aware_modified = self.stale_modified
        naive_modified = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(
            hours=31
        )
        objects = [
            self._listed_object(
                "documents/150/aware.jpg",
                last_modified=aware_modified,
            ),
            self._listed_object(
                "documents/150/naive.jpg",
                last_modified=naive_modified,
            ),
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("Candidates: 2", output)
        naive_pos = output.index("documents/150/naive.jpg")
        aware_pos = output.index("documents/150/aware.jpg")
        self.assertLess(naive_pos, aware_pos)

    def test_json_mode_reports_delete_failures_and_raises(self):
        orphan_key = "documents/120/fail.jpg"
        objects = [self._listed_object(orphan_key)]

        with (
            self._mock_list_objects(objects),
            patch(
                f"{self.SERVICE_MODULE}.delete_s3_object",
                side_effect=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DeleteObject",
                ),
            ),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_document_s3_orphans",
                    "--json",
                    "--commit",
                    stdout=StringIO(),
                )

    def test_whitespace_only_reference_keys_are_ignored(self):
        doc = self._create_document(file_s3_key="   ")
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="  ",
            file_original_name="blank.jpg",
            mime_type="image/jpeg",
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )
        orphan_key = "documents/130/orphan.jpg"
        objects = [self._listed_object(orphan_key)]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                stdout=stdout,
            )

        self.assertIn(orphan_key, stdout.getvalue())
        self.assertEqual(collect_referenced_document_s3_keys(), set())

    def test_naive_s3_last_modified_is_handled(self):
        naive_modified = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(
            hours=30
        )
        objects = [
            self._listed_object(
                "documents/140/naive.jpg",
                last_modified=naive_modified,
            )
        ]

        stdout = StringIO()
        with self._mock_list_objects(objects):
            call_command(
                "cleanup_document_s3_orphans",
                stdout=stdout,
            )

        self.assertIn("documents/140/naive.jpg", stdout.getvalue())
        self.assertIn("Candidates: 1", stdout.getvalue())
