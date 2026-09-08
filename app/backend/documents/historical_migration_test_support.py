"""Test-only helpers for historical migration TransactionTestCase cleanup.

Not a pytest test module (name does not match ``test_*.py`` / ``*_test.py``).
Do not import from production code.

Historical migration tests migrate ``documents`` backward on the shared
pytest-django database. Cleanup must restore that app to the current
migration graph leaf, not to the migration under test.
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

DOCUMENTS_APP_LABEL = "documents"


def documents_migration_leaf_targets() -> list[tuple[str, str]]:
    """Return current ``documents`` leaf node(s) from the on-disk graph.

    Resolves the leaf from Django's migration graph so cleanup stays correct
    when later ``documents`` migrations are added. Does not hard-code a
    migration name.
    """
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    leaves = executor.loader.graph.leaf_nodes(DOCUMENTS_APP_LABEL)
    if not leaves:
        raise RuntimeError("documents migration graph has no leaf nodes")
    return leaves
