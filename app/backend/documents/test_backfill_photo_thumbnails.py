"""Management command tests for backfill_photo_thumbnails."""

from __future__ import annotations

import json
from io import BytesIO, StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from PIL import Image

from documents.models import ArchiveItem, PhotoContent
from documents.s3 import build_photo_thumbnail_s3_key
from documents.services.photo_thumbnail import THUMBNAIL_JPEG_MIME


def _create_photo_archive_item(*, title: str = "Family photo") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _solid_jpeg_bytes(
    width: int,
    height: int,
    *,
    color: tuple[int, int, int] = (10, 20, 30),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _create_uploaded_photo(
    *,
    title: str = "Backfill photo",
    original_file_key: str | None = None,
    thumbnail_file_key: str = "",
    upload_status: str = PhotoContent.UploadStatus.UPLOADED,
) -> PhotoContent:
    item = _create_photo_archive_item(title=title)
    photo = PhotoContent.objects.create(
        archive_item=item,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=4096,
        upload_status=upload_status,
        upload_error="",
        thumbnail_file_key=thumbnail_file_key,
    )
    key = original_file_key or f"photos/{photo.id}/original.jpg"
    photo.original_file_key = key
    photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class BackfillPhotoThumbnailsCommandTests(TestCase):
    def test_dry_run_finds_candidates_without_s3_calls_or_db_changes(self):
        candidate = _create_uploaded_photo(title="Needs thumbnail")
        completed = _create_uploaded_photo(
            title="Already done",
            thumbnail_file_key=build_photo_thumbnail_s3_key(999),
        )
        completed.thumbnail_file_key = build_photo_thumbnail_s3_key(completed.id)
        completed.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        with (
            patch(
                "documents.services.photo_thumbnail_backfill.generate_and_persist_photo_thumbnail"
            ) as mock_generate,
            patch("documents.services.photo_thumbnail.get_object_bytes") as mock_get,
            patch("documents.services.photo_thumbnail.put_object_bytes") as mock_put,
        ):
            call_command("backfill_photo_thumbnails", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertIn(f"photo_content_id={candidate.id}", output)
        self.assertNotIn(f"photo_content_id={completed.id}", output)
        mock_generate.assert_not_called()
        mock_get.assert_not_called()
        mock_put.assert_not_called()

        candidate.refresh_from_db()
        self.assertEqual(candidate.thumbnail_file_key, "")
        completed.refresh_from_db()
        self.assertEqual(
            completed.thumbnail_file_key,
            build_photo_thumbnail_s3_key(completed.id),
        )

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=12345)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(1200, 800), "image/jpeg"),
    )
    def test_commit_successfully_generates_thumbnail(self, _mock_get, _mock_put):
        photo = _create_uploaded_photo(title="Commit candidate")

        call_command("backfill_photo_thumbnails", "--commit", stdout=StringIO())

        photo.refresh_from_db()
        self.assertEqual(photo.width, 1200)
        self.assertEqual(photo.height, 800)
        self.assertEqual(
            photo.thumbnail_file_key,
            build_photo_thumbnail_s3_key(photo.id),
        )
        self.assertEqual(photo.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(photo.thumbnail_size_bytes, 12345)

    def test_already_completed_photos_are_skipped(self):
        photo = _create_uploaded_photo(title="Completed")
        thumb_key = build_photo_thumbnail_s3_key(photo.id)
        photo.thumbnail_file_key = thumb_key
        photo.thumbnail_mime_type = THUMBNAIL_JPEG_MIME
        photo.thumbnail_size_bytes = 999
        photo.width = 100
        photo.height = 200
        photo.save()

        stdout = StringIO()
        with patch(
            "documents.services.photo_thumbnail_backfill.generate_and_persist_photo_thumbnail"
        ) as mock_generate:
            call_command(
                "backfill_photo_thumbnails",
                f"--photo-id={photo.id}",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("already has thumbnail_file_key", output)
        mock_generate.assert_not_called()

    def test_pending_and_failed_photos_are_excluded(self):
        pending = _create_uploaded_photo(
            title="Pending",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        failed = _create_uploaded_photo(
            title="Failed",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )

        stdout = StringIO()
        call_command("backfill_photo_thumbnails", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("No photo thumbnail backfill candidates found.", output)
        self.assertNotIn(f"photo_content_id={pending.id}", output)
        self.assertNotIn(f"photo_content_id={failed.id}", output)

    def test_missing_original_key_is_excluded(self):
        photo = _create_uploaded_photo(title="Missing original")
        photo.original_file_key = ""
        photo.save(update_fields=["original_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("missing original_file_key", output)

    def test_photo_id_targeting_candidate(self):
        photo = _create_uploaded_photo(title="Targeted")

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"photo_content_id={photo.id}", output)
        self.assertIn("candidate_count: 1", output)

    def test_photo_id_not_found(self):
        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            "--photo-id=999999",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("PhotoContent id=999999 does not exist.", output)
        self.assertIn("candidate_count: 0", output)

    def test_limit_caps_candidates(self):
        first = _create_uploaded_photo(title="First")
        second = _create_uploaded_photo(title="Second")

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            "--limit=1",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"photo_content_id={first.id}", output)
        self.assertNotIn(f"photo_content_id={second.id}", output)
        self.assertIn("candidate_count: 1", output)

    def test_limit_selects_first_n_candidates_by_pk(self):
        first = _create_uploaded_photo(title="Limit first")
        second = _create_uploaded_photo(title="Limit second")
        third = _create_uploaded_photo(title="Limit third")

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            "--limit=2",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["candidate_count"], 2)
        selected_ids = [row["photo_content_id"] for row in payload["candidates"]]
        self.assertEqual(selected_ids, [first.id, second.id])
        self.assertNotIn(third.id, selected_ids)

    def test_whitespace_only_thumbnail_key_is_treated_as_missing(self):
        photo = _create_uploaded_photo(title="Whitespace thumbnail")
        photo.thumbnail_file_key = "   "
        photo.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"photo_content_id={photo.id}", output)
        self.assertIn("candidate_count: 1", output)
        self.assertNotIn("already has thumbnail_file_key", output)

    def test_whitespace_only_thumbnail_key_included_in_bulk_scan(self):
        photo = _create_uploaded_photo(title="Bulk whitespace thumbnail")
        photo.thumbnail_file_key = " \t "
        photo.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["photo_content_id"],
            photo.id,
        )

    def test_whitespace_only_original_key_is_excluded(self):
        photo = _create_uploaded_photo(title="Whitespace original")
        photo.original_file_key = "   "
        photo.save(update_fields=["original_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("missing original_file_key", output)
        self.assertIn("candidate_count: 0", output)

    def test_whitespace_only_original_key_excluded_from_bulk_scan(self):
        photo = _create_uploaded_photo(title="Bulk whitespace original")
        photo.original_file_key = " \t "
        photo.save(update_fields=["original_file_key", "updated_at"])

        stdout = StringIO()
        call_command("backfill_photo_thumbnails", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("No photo thumbnail backfill candidates found.", output)
        self.assertNotIn(f"photo_content_id={photo.id}", output)

    def test_invalid_limit_raises_command_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("backfill_photo_thumbnails", "--limit=0", stdout=StringIO())
        self.assertIn("positive integer", str(ctx.exception).lower())

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=1111)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        side_effect=lambda _bucket, key: (
            (_solid_jpeg_bytes(640, 480), "image/jpeg")
            if key.endswith("/original.jpg")
            else (_ for _ in ()).throw(
                ClientError(
                    {"Error": {"Code": "500", "Message": "fail"}},
                    "GetObject",
                )
            )
        ),
    )
    def test_one_failure_does_not_stop_later_candidates(self, _mock_get, _mock_put):
        failing = _create_uploaded_photo(title="Failing")
        succeeding = _create_uploaded_photo(title="Succeeding")
        failing.original_file_key = f"photos/{failing.id}/missing.jpg"
        failing.save(update_fields=["original_file_key", "updated_at"])

        stdout = StringIO()
        call_command("backfill_photo_thumbnails", "--commit", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("failed_count: 1", output)
        self.assertIn("succeeded_count: 1", output)
        self.assertIn(f"photo_content_id={failing.id}", output)
        self.assertIn(f"photo_content_id={succeeding.id}", output)

        failing.refresh_from_db()
        succeeding.refresh_from_db()
        self.assertEqual(failing.thumbnail_file_key, "")
        self.assertEqual(
            succeeding.thumbnail_file_key,
            build_photo_thumbnail_s3_key(succeeding.id),
        )

    @override_settings(UPLOADS_BUCKET_NAME="")
    def test_missing_bucket_on_commit_raises_command_error(self):
        _create_uploaded_photo(title="Needs bucket")

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "backfill_photo_thumbnails",
                "--commit",
                stdout=StringIO(),
            )
        self.assertIn("UPLOADS_BUCKET_NAME is not configured", str(ctx.exception))

    def test_json_output_includes_candidates_and_commit_results(self):
        photo = _create_uploaded_photo(title="JSON photo")

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            "--json",
            stdout=stdout,
        )
        dry_payload = json.loads(stdout.getvalue())
        self.assertEqual(dry_payload["mode"], "dry-run")
        self.assertEqual(dry_payload["candidate_count"], 1)
        self.assertEqual(
            dry_payload["candidates"][0]["photo_content_id"],
            photo.id,
        )
        self.assertIsNone(dry_payload["commit"])

        with (
            patch(
                "documents.services.photo_thumbnail.get_object_bytes",
                return_value=(_solid_jpeg_bytes(800, 600), "image/jpeg"),
            ),
            patch(
                "documents.services.photo_thumbnail.put_object_bytes",
                return_value=2222,
            ),
        ):
            stdout = StringIO()
            call_command(
                "backfill_photo_thumbnails",
                "--commit",
                "--json",
                stdout=stdout,
            )

        commit_payload = json.loads(stdout.getvalue())
        self.assertEqual(commit_payload["mode"], "commit")
        self.assertEqual(commit_payload["commit"]["processed_count"], 1)
        self.assertEqual(commit_payload["commit"]["succeeded_count"], 1)
        self.assertEqual(commit_payload["commit"]["failed_count"], 0)
        self.assertEqual(len(commit_payload["commit"]["results"]), 1)
        self.assertEqual(
            commit_payload["commit"]["results"][0]["outcome"],
            "succeeded",
        )

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=3333)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(500, 500), "image/jpeg"),
    )
    def test_repeated_execution_is_idempotent(self, _mock_get, _mock_put):
        photo = _create_uploaded_photo(title="Idempotent")

        call_command("backfill_photo_thumbnails", "--commit", stdout=StringIO())
        photo.refresh_from_db()
        first_key = photo.thumbnail_file_key
        first_size = photo.thumbnail_size_bytes

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("already has thumbnail_file_key", output)

        with patch(
            "documents.services.photo_thumbnail_backfill.generate_and_persist_photo_thumbnail"
        ) as mock_generate:
            call_command("backfill_photo_thumbnails", "--commit", stdout=StringIO())
            mock_generate.assert_not_called()

        photo.refresh_from_db()
        self.assertEqual(photo.thumbnail_file_key, first_key)
        self.assertEqual(photo.thumbnail_size_bytes, first_size)

    def test_json_target_disposition_for_ineligible_photo_id(self):
        photo = _create_uploaded_photo(
            title="Pending target",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )

        stdout = StringIO()
        call_command(
            "backfill_photo_thumbnails",
            f"--photo-id={photo.id}",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["target"]["disposition"], "ineligible")
        self.assertEqual(payload["candidate_count"], 0)
