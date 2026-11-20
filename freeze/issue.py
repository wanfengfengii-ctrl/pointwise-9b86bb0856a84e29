from freeze.allocations import (
    coerce_allocations,
    require_nonnegative_rows,
    rows_of,
    validate_allocations,
)
from freeze.manifest import build_correction_manifest, build_manifest
from freeze.store import load_manifest, next_manifest_id, save_manifest
from intake.revisions import get_revision
from lake_db import InvalidInput, MaterialMismatch, require_id, write_transaction


def freeze(source, day, revision):
    selected = get_revision(source, day, revision)
    with write_transaction(source):
        manifest = build_manifest(selected, next_manifest_id(source))
        save_manifest(source, manifest)
    return manifest


def freeze_correction(source, parent_manifest_id, child_revision, allocations):
    require_id(parent_manifest_id, 'parent manifest ID')
    require_id(child_revision, 'revision')
    parent = load_manifest(source, parent_manifest_id)
    child = get_revision(source, parent.day, child_revision)
    parent_revision = get_revision(source, parent.day, parent.revision)
    if child_revision != parent.revision + 1:
        raise InvalidInput('corrected revision must immediately follow its parent revision')
    if build_manifest(parent_revision, parent.manifest_id).entries != parent.entries:
        raise MaterialMismatch('parent manifest no longer matches its recorded revision')
    items = coerce_allocations(allocations, InvalidInput)
    parent_rows = rows_of(parent_revision.segments)
    child_rows = rows_of(child.segments)
    require_nonnegative_rows(parent_rows, InvalidInput)
    require_nonnegative_rows(child_rows, InvalidInput)
    validate_allocations(items, parent_rows, child_rows, InvalidInput)
    with write_transaction(source):
        manifest = build_correction_manifest(child, next_manifest_id(source), parent_manifest_id, items)
        save_manifest(source, manifest)
    return manifest
