"""Tests for best-effort PHOTO S3 cleanup after database deletion."""

from unittest.mock import call, patch

from botocore.exceptions import ClientError
from django.db import transaction
from django.test import TestCase

from documents.services.photo_s3_cleanup import (
    delete_photo_s3_objects_best_effort,
    schedule_photo_s3_cleanup_after_commit,
)


class PhotoS3CleanupTests(TestCase):
    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_cleanup_deletes_original_and_thumbnail(self, mock_delete_s3_object):
        delete_photo_s3_objects_best_effort(
            bucket="bucket",
            original_file_key="photos/1/original.jpg",
            thumbnail_file_key="photos/1/thumb_400.jpg",
            photo_content_id=1,
        )

        self.assertEqual(
            mock_delete_s3_object.call_args_list,
            [
                call("bucket", "photos/1/original.jpg"),
                call("bucket", "photos/1/thumb_400.jpg"),
            ],
        )

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_cleanup_ignores_blank_and_duplicate_keys(self, mock_delete_s3_object):
        delete_photo_s3_objects_best_effort(
            bucket="bucket",
            original_file_key=" photos/1/shared.jpg ",
            thumbnail_file_key="photos/1/shared.jpg",
            photo_content_id=1,
        )

        mock_delete_s3_object.assert_called_once_with(
            "bucket",
            "photos/1/shared.jpg",
        )

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_cleanup_skips_all_objects_when_bucket_is_missing(
        self, mock_delete_s3_object
    ):
        with self.assertLogs(
            "documents.services.photo_s3_cleanup",
            level="WARNING",
        ):
            delete_photo_s3_objects_best_effort(
                bucket=" ",
                original_file_key="photos/1/original.jpg",
                thumbnail_file_key="photos/1/thumb_400.jpg",
                photo_content_id=1,
            )

        mock_delete_s3_object.assert_not_called()

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_cleanup_continues_after_one_s3_delete_failure(self, mock_delete_s3_object):
        mock_delete_s3_object.side_effect = [
            ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "denied",
                    }
                },
                "DeleteObject",
            ),
            None,
        ]

        with self.assertLogs(
            "documents.services.photo_s3_cleanup",
            level="ERROR",
        ):
            delete_photo_s3_objects_best_effort(
                bucket="bucket",
                original_file_key="photos/1/original.jpg",
                thumbnail_file_key="photos/1/thumb_400.jpg",
                photo_content_id=1,
            )

        self.assertEqual(
            mock_delete_s3_object.call_args_list,
            [
                call("bucket", "photos/1/original.jpg"),
                call("bucket", "photos/1/thumb_400.jpg"),
            ],
        )

    @patch("documents.services.photo_s3_cleanup.delete_photo_s3_objects_best_effort")
    def test_rollback_discards_scheduled_cleanup(self, mock_cleanup):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    schedule_photo_s3_cleanup_after_commit(
                        bucket="bucket",
                        original_file_key="photos/1/original.jpg",
                        thumbnail_file_key="photos/1/thumb_400.jpg",
                        photo_content_id=1,
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        self.assertEqual(callbacks, [])
        mock_cleanup.assert_not_called()

    @patch("documents.services.photo_s3_cleanup.delete_photo_s3_objects_best_effort")
    def test_schedule_runs_cleanup_only_when_commit_callback_executes(
        self, mock_cleanup
    ):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_photo_s3_cleanup_after_commit(
                bucket="bucket",
                original_file_key="photos/1/original.jpg",
                thumbnail_file_key="photos/1/thumb_400.jpg",
                photo_content_id=1,
            )

        self.assertEqual(len(callbacks), 1)
        mock_cleanup.assert_not_called()

        callbacks[0]()

        mock_cleanup.assert_called_once_with(
            bucket="bucket",
            original_file_key="photos/1/original.jpg",
            thumbnail_file_key="photos/1/thumb_400.jpg",
            photo_content_id=1,
        )
