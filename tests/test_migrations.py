from __future__ import annotations

import ast
from pathlib import Path


def _migration_values() -> list[tuple[str, str | None]]:
    values: list[tuple[str, str | None]] = []
    for path in sorted(Path("migrations/versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assignments: dict[str, str | None] = {}
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            if node.target.id not in {"revision", "down_revision"}:
                continue
            if isinstance(node.value, ast.Constant):
                assignments[node.target.id] = node.value.value
        if "revision" in assignments:
            values.append((assignments["revision"] or "", assignments.get("down_revision")))
    return values


def test_migration_revision_ids_fit_alembic_version_column() -> None:
    values = _migration_values()
    assert values
    assert all(len(revision) <= 32 for revision, _ in values)


def test_migration_chain_references_known_revisions() -> None:
    values = _migration_values()
    revisions = {revision for revision, _ in values}
    assert all(down_revision is None or down_revision in revisions for _, down_revision in values)
