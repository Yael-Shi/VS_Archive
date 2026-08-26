"""Pytest hooks for VS-Archive backend tests.

Keeps auto-created Tag PKs above frozen historical person-name Tag ids after
migrate, explicit Tag sequence reset, and ``TransactionTestCase.reset_sequences``.

Verified Django 6.0.1 / pytest-django ordering used here:

* Function-scoped fixtures wrap unittest ``TestCase.__call__`` /
  ``_setup_and_call``, so they run before per-test ``_pre_setup`` (TestCase
  ``_enter_atomics``) and after ``_post_teardown`` (TestCase rollback, or
  TransactionTestCase ``flush(..., reset_sequences=False)``).
* ``TransactionTestCase.setUpClass`` (non-``TestCase`` subclasses) calls
  ``_pre_setup`` eagerly, including ``_fixture_setup`` → ``_reset_sequences``,
  before any function-scoped fixture. The wrap is installed in
  ``pytest_configure`` and in a class-scoped autouse fixture (pytest runs
  those before unittest ``setUpClass``). The one-shot post-migrate Tag
  advance is requested from that same class-scoped fixture for classes with
  a truthy ``databases`` (not from a per-test insert/delete).
* Django's ``TransactionTestCase._reset_sequences`` is a private staticmethod
  (``django.test.testcases``). We wrap only that hook, preserve ``(db_name)``
  via ``functools.wraps``, and mark the wrapper so install is idempotent.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import wraps

import pytest

_TAG_PK_SEQUENCE_GUARD_ATTR = "_vs_archive_tag_pk_guard"


def _django_db_test_requested(request: pytest.FixtureRequest) -> bool:
    if request.node.get_closest_marker("django_db"):
        return True
    cls = getattr(request, "cls", None)
    if cls is None:
        return False
    return bool(getattr(cls, "databases", None))


def _install_transaction_testcase_tag_pk_guard() -> None:
    """Re-advance Tag PKs on the same alias Django just reset to 1."""
    from django.db import DEFAULT_DB_ALIAS, router
    from django.test import TransactionTestCase

    from documents.models import Tag

    current = TransactionTestCase._reset_sequences
    if getattr(current, _TAG_PK_SEQUENCE_GUARD_ATTR, False):
        return

    original = current

    @wraps(original)
    def _reset_sequences(db_name: str) -> None:
        original(db_name)
        tag_alias = router.db_for_write(Tag) or DEFAULT_DB_ALIAS
        if db_name != tag_alias:
            return
        # Call-time import: tests can patch the helper; the wrap closes over
        # Django's original ``_reset_sequences``, not this wrapper.
        from documents.tag_pk_sequence_support import (
            ensure_tag_pk_sequence_past_historical_ids as advance_tag_pk,
        )

        advance_tag_pk(using=db_name, tag_model=Tag)

    setattr(_reset_sequences, _TAG_PK_SEQUENCE_GUARD_ATTR, True)
    TransactionTestCase._reset_sequences = staticmethod(_reset_sequences)


def pytest_configure() -> None:
    from django.conf import settings
    from django.core.exceptions import AppRegistryNotReady, ImproperlyConfigured

    if not settings.configured:
        return
    try:
        _install_transaction_testcase_tag_pk_guard()
    except (AppRegistryNotReady, ImproperlyConfigured):
        return


@pytest.fixture(scope="class", autouse=True)
def _install_tag_pk_guard_before_django_setup_class(
    request: pytest.FixtureRequest,
) -> None:
    """Install the wrap and session Tag advance before unittest setUpClass.

    pytest-django class fixtures run before ``TransactionTestCase.setUpClass``,
    which eagerly calls ``_pre_setup`` / ``_reset_sequences``. Function-scoped
    fixtures wrap ``TestCase._setup_and_call``, so they cannot intercept that
    first eager reset.
    """
    from django.conf import settings
    from django.core.exceptions import AppRegistryNotReady, ImproperlyConfigured

    if not settings.configured:
        return
    try:
        _install_transaction_testcase_tag_pk_guard()
    except (AppRegistryNotReady, ImproperlyConfigured):
        return
    cls = getattr(request, "cls", None)
    if cls is not None and bool(getattr(cls, "databases", None)):
        request.getfixturevalue("_tag_pk_sequence_ready")


@pytest.fixture(scope="session")
def _tag_pk_sequence_ready(django_db_setup, django_db_blocker) -> None:
    """Advance Tag PK sequence once after migrate / test DB creation."""
    if django_db_blocker is None:
        return
    from django.db import DEFAULT_DB_ALIAS, router

    from documents.models import Tag
    from documents.tag_pk_sequence_support import (
        ensure_tag_pk_sequence_past_historical_ids,
    )

    with django_db_blocker.unblock():
        using = router.db_for_write(Tag) or DEFAULT_DB_ALIAS
        ensure_tag_pk_sequence_past_historical_ids(using=using, tag_model=Tag)


@pytest.fixture(autouse=True)
def _ensure_tag_pk_sequence_past_historical_ids(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Request the one-shot session advance for DB tests only.

    Does not insert/delete Tags around every test. TestCase rollback leaves
    sequences high; a per-test empty-table sentinel would rewind them via
    ``sequence_reset_sql`` MAX(pk). ``reset_sequences=True`` is handled by
    the ``_reset_sequences`` wrap. Explicit Tag resets use
    ``reset_pk_sequence``.
    """
    if not _django_db_test_requested(request):
        yield
        return
    request.getfixturevalue("_tag_pk_sequence_ready")
    _install_transaction_testcase_tag_pk_guard()
    yield
