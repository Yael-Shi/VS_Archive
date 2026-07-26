"""Focused tests for sync_transkribus_corrected_current management command."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from documents.models import (
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusTranscriptSnapshot,
)
from documents.services.transkribus_corrected_current_sync import (
    CorrectedCurrentSyncError,
    CorrectedCurrentSyncFailureCode,
    CorrectedCurrentSyncResult,
)
from documents.services.transkribus_snapshot_storage import SnapshotStorageOutcome

User = get_user_model()

_CMD = "sync_transkribus_corrected_current"
_SERVICE = (
    "documents.management.commands.sync_transkribus_corrected_current"
    ".run_corrected_current_transkribus_sync"
)
_CREDS = {
    "TRANSKRIBUS_USERNAME": "trp-user",
    "TRANSKRIBUS_PASSWORD": "trp-secret-password",
    "TRANSKRIBUS_API_TOKEN": "trp-secret-token",
}


def _staff_user(**kwargs):
    defaults = dict(
        username="cc_sync_cmd_staff",
        password="x",
        is_staff=True,
        is_active=True,
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


class SyncTranskribusCorrectedCurrentCommandTests(TestCase):
    def test_missing_required_arguments_raises(self):
        with self.assertRaises(CommandError):
            call_command(_CMD)

    def test_non_positive_document_id_raises(self):
        staff = _staff_user()
        with self.assertRaises(CommandError) as ctx:
            call_command(
                _CMD,
                "--document-id=0",
                f"--initiated-by-user-id={staff.pk}",
            )
        self.assertIn("--document-id must be a positive integer", str(ctx.exception))

    def test_non_positive_initiated_by_user_id_raises(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                _CMD,
                "--document-id=1",
                "--initiated-by-user-id=-3",
            )
        self.assertIn(
            "--initiated-by-user-id must be a positive integer",
            str(ctx.exception),
        )

    def test_missing_user_raises_before_service(self):
        with patch(_SERVICE) as mock_sync:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    _CMD,
                    "--document-id=1",
                    "--initiated-by-user-id=999999",
                )
        self.assertIn("does not exist", str(ctx.exception))
        mock_sync.assert_not_called()

    def test_inactive_user_raises_before_service(self):
        user = _staff_user(username="cc_sync_inactive", is_active=False)
        with patch(_SERVICE) as mock_sync:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    _CMD,
                    "--document-id=1",
                    f"--initiated-by-user-id={user.pk}",
                )
        self.assertIn("is inactive", str(ctx.exception))
        mock_sync.assert_not_called()

    def test_non_staff_user_raises_before_service(self):
        user = User.objects.create_user(
            username="cc_sync_non_staff",
            password="x",
            is_staff=False,
            is_active=True,
        )
        with patch(_SERVICE) as mock_sync:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    _CMD,
                    "--document-id=1",
                    f"--initiated-by-user-id={user.pk}",
                )
        self.assertIn("is not staff", str(ctx.exception))
        mock_sync.assert_not_called()

    def test_missing_credentials_raises_before_service(self):
        staff = _staff_user(username="cc_sync_creds")
        with patch.dict("os.environ", {}, clear=True):
            with patch(_SERVICE) as mock_sync:
                with self.assertRaises(CommandError) as ctx:
                    call_command(
                        _CMD,
                        "--document-id=1",
                        f"--initiated-by-user-id={staff.pk}",
                    )
        self.assertIn("TRANSKRIBUS_USERNAME", str(ctx.exception))
        mock_sync.assert_not_called()

    def test_missing_bearer_token_raises_before_service(self):
        staff = _staff_user(username="cc_sync_token")
        env = {
            "TRANSKRIBUS_USERNAME": "trp-user",
            "TRANSKRIBUS_PASSWORD": "trp-pass",
            "TRANSKRIBUS_API_TOKEN": "",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch(_SERVICE) as mock_sync:
                with self.assertRaises(CommandError) as ctx:
                    call_command(
                        _CMD,
                        "--document-id=1",
                        f"--initiated-by-user-id={staff.pk}",
                    )
        self.assertIn("TRANSKRIBUS_API_TOKEN", str(ctx.exception))
        mock_sync.assert_not_called()

    @patch.dict("os.environ", _CREDS, clear=False)
    @patch(_SERVICE)
    def test_completed_result_prints_safe_fields_and_passes_args(self, mock_sync):
        staff = _staff_user(username="cc_sync_completed")
        attempt = cast(
            TranskribusCorrectedCurrentSyncAttempt,
            SimpleNamespace(
                pk=41,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            ),
        )
        snapshot = cast(
            TranskribusTranscriptSnapshot,
            SimpleNamespace(pk=77),
        )
        mock_sync.return_value = CorrectedCurrentSyncResult(
            attempt=attempt,
            refused=False,
            snapshot=snapshot,
            storage_outcome=SnapshotStorageOutcome.CREATED,
        )
        stdout = StringIO()
        call_command(
            _CMD,
            "--document-id=55",
            f"--initiated-by-user-id={staff.pk}",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("attempt_id=41", output)
        self.assertIn("status=COMPLETED", output)
        self.assertIn("resolved_snapshot_id=77", output)
        self.assertIn("storage_outcome=CREATED", output)
        self.assertNotIn(_CREDS["TRANSKRIBUS_PASSWORD"], output)
        self.assertNotIn(_CREDS["TRANSKRIBUS_API_TOKEN"], output)
        self.assertNotIn("https://", output)

        mock_sync.assert_called_once()
        kwargs = mock_sync.call_args.kwargs
        self.assertEqual(kwargs["document_id"], 55)
        self.assertEqual(kwargs["initiated_by"].pk, staff.pk)
        self.assertTrue(kwargs["initiated_by"].is_staff)
        self.assertEqual(kwargs["username"], _CREDS["TRANSKRIBUS_USERNAME"])
        self.assertEqual(kwargs["password"], _CREDS["TRANSKRIBUS_PASSWORD"])
        self.assertEqual(kwargs["bearer_token"], _CREDS["TRANSKRIBUS_API_TOKEN"])
        # Management-command path must not supply request/lease correlation.
        self.assertNotIn("sync_request_id", kwargs)
        self.assertNotIn("lease_token", kwargs)

    @patch.dict("os.environ", _CREDS, clear=False)
    @patch(_SERVICE)
    def test_refused_result_prints_status_without_snapshot_fields(self, mock_sync):
        staff = _staff_user(username="cc_sync_refused")
        attempt = cast(
            TranskribusCorrectedCurrentSyncAttempt,
            SimpleNamespace(
                pk=42,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
            ),
        )
        mock_sync.return_value = CorrectedCurrentSyncResult(
            attempt=attempt,
            refused=True,
            snapshot=None,
            storage_outcome=None,
        )
        stdout = StringIO()
        call_command(
            _CMD,
            "--document-id=9",
            f"--initiated-by-user-id={staff.pk}",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("attempt_id=42", output)
        self.assertIn("status=REFUSED", output)
        self.assertNotIn("resolved_snapshot_id=", output)
        self.assertNotIn("storage_outcome=", output)
        self.assertNotIn(_CREDS["TRANSKRIBUS_PASSWORD"], output)

    @patch.dict("os.environ", _CREDS, clear=False)
    @patch(_SERVICE)
    def test_corrected_current_sync_error_maps_to_safe_command_error(self, mock_sync):
        staff = _staff_user(username="cc_sync_error")
        unsafe = (
            "https://transkribus.eu/secret-path token=leak "
            f"password={_CREDS['TRANSKRIBUS_PASSWORD']}"
        )
        sync_error = CorrectedCurrentSyncError(
            "Transkribus login or pages metadata request failed.",
            attempt_id=88,
            failure_code=CorrectedCurrentSyncFailureCode.HTTP_METADATA,
        )
        sync_error.__cause__ = RuntimeError(unsafe)
        mock_sync.side_effect = sync_error

        with self.assertRaises(CommandError) as ctx:
            call_command(
                _CMD,
                "--document-id=3",
                f"--initiated-by-user-id={staff.pk}",
            )
        message = str(ctx.exception)
        self.assertIn("Transkribus login or pages metadata request failed.", message)
        self.assertIn("attempt_id=88", message)
        self.assertIn(
            f"failure_code={CorrectedCurrentSyncFailureCode.HTTP_METADATA}",
            message,
        )
        self.assertNotIn("https://", message)
        self.assertNotIn("token=leak", message)
        self.assertNotIn("secret-path", message)
        self.assertNotIn(_CREDS["TRANSKRIBUS_PASSWORD"], message)
        self.assertNotIn(_CREDS["TRANSKRIBUS_API_TOKEN"], message)
        self.assertIsNone(ctx.exception.__cause__)

    @patch.dict("os.environ", _CREDS, clear=False)
    @patch(_SERVICE)
    def test_sync_error_without_attempt_id_omits_attempt_field(self, mock_sync):
        staff = _staff_user(username="cc_sync_run_resolution")
        mock_sync.side_effect = CorrectedCurrentSyncError(
            "Corrected/current sync could not resolve a trusted Transkribus run.",
            attempt_id=None,
            failure_code=CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
        )
        with self.assertRaises(CommandError) as ctx:
            call_command(
                _CMD,
                "--document-id=3",
                f"--initiated-by-user-id={staff.pk}",
            )
        message = str(ctx.exception)
        self.assertIn("could not resolve a trusted Transkribus run", message)
        self.assertIn(
            f"failure_code={CorrectedCurrentSyncFailureCode.RUN_RESOLUTION}",
            message,
        )
        self.assertNotIn("attempt_id=", message)
