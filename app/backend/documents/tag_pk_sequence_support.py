"""Test-only Tag PK sequence support.

Not a pytest test module (name does not match ``test_*.py`` / ``*_test.py``).
Do not import from production code.
"""

from __future__ import annotations

import uuid

from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, router
from django.db.models import Model

from documents.historical_person_tag_map import (
    historical_person_name_tag_ids,
    is_retired_historical_person_tag_name,
)
from documents.models import Tag

# Per-invocation names use this prefix plus a uuid. Never delete by prefix:
# a test could legally create a Tag whose name starts with it.
SENTINEL_TAG_NAME_PREFIX = "__vs_archive_tag_seq_"


def _write_alias(model: type[Model], using: str | None) -> str:
    if using is not None:
        return using
    return router.db_for_write(model) or DEFAULT_DB_ALIAS


def _is_tag_table(model: type[Model]) -> bool:
    meta = getattr(model, "_meta", None)
    return meta is not None and meta.db_table == Tag._meta.db_table


def _new_sentinel_name() -> str:
    name = f"{SENTINEL_TAG_NAME_PREFIX}{uuid.uuid4().hex}"
    if len(name) > 64 or is_retired_historical_person_tag_name(name):
        raise RuntimeError("sentinel Tag name is invalid or retired")
    return name


def ensure_tag_pk_sequence_past_historical_ids(
    *,
    using: str | None = None,
    tag_model: type[Model] | None = None,
) -> None:
    """Ensure the next auto Tag PK is greater than max(frozen historical ids).

    Inserts a temporary sentinel row at a PK above both the frozen ceiling and
    current ``MAX(pk)`` on ``tag_model`` (default: live ``Tag``), runs Django
    ``sequence_reset_sql`` on that same alias/model/table, then deletes only
    that sentinel row. PostgreSQL ``sequence_reset_sql`` uses ``MAX(pk)``.
    SQLite's ``sequence_reset_sql`` is a no-op; Django SQLite AutoField uses
    ``AUTOINCREMENT``, so the explicit insert updates ``sqlite_sequence`` and
    delete does not rewind it.

    ``using`` selects the database alias for ORM, MAX, sequence SQL, and
    cleanup. Historical ``apps.get_model`` Tag classes must be passed as
    ``tag_model`` so inserts match that schema; this function does not
    substitute the live ``Tag`` model in that case.
    """
    model = tag_model if tag_model is not None else Tag
    alias = _write_alias(model, using)
    manager = model.objects.using(alias)
    conn = connections[alias]

    frozen_max = max(historical_person_name_tag_ids())
    current_max = manager.order_by("-pk").values_list("pk", flat=True).first() or 0
    sentinel_pk = max(frozen_max, current_max) + 1
    occupied = set(manager.filter(pk__gte=sentinel_pk).values_list("pk", flat=True))
    while sentinel_pk in occupied:
        sentinel_pk += 1

    sentinel_name = _new_sentinel_name()
    created_pk: int | None = None
    caught: BaseException | None = None
    try:
        manager.create(pk=sentinel_pk, name=sentinel_name)
        created_pk = sentinel_pk
        sql_statements = conn.ops.sequence_reset_sql(no_style(), [model])
        if sql_statements:
            with conn.cursor() as cursor:
                for sql in sql_statements:
                    cursor.execute(sql)
    except BaseException as exc:
        caught = exc
        raise
    finally:
        if created_pk is not None:
            try:
                manager.filter(pk=created_pk, name=sentinel_name).delete()
            except Exception:
                if caught is None:
                    raise


def reset_pk_sequence(model: type[Model], *, using: str | None = None) -> None:
    """Reset ``model`` PK sequence from existing rows on its write alias.

    Tag-table models also advance past frozen historical ids using the
    provided model class (including historical migration-state Tag models).
    """
    alias = _write_alias(model, using)
    if _is_tag_table(model):
        ensure_tag_pk_sequence_past_historical_ids(using=alias, tag_model=model)
        return
    conn = connections[alias]
    sql_statements = conn.ops.sequence_reset_sql(no_style(), [model])
    with conn.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)
