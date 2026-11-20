from lake_db import Conflict, MaterialMismatch, StateError, write_transaction
from publish.materials import initialize_materials, store_materials
from publish.receipts import find_receipt_by_request, get_receipt, initialize_receipts, insert_receipt
from transfer.attempts import get_attempt, mark_issued


def initialize_catalog(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS publications (
            publication_id INTEGER PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE REFERENCES copy_attempts (request_id),
            manifest_id INTEGER NOT NULL
        )
    ''')
    initialize_materials(db)
    initialize_receipts(db)
    db.execute('''
        CREATE TABLE IF NOT EXISTS correction_parents (
            publication_id INTEGER PRIMARY KEY REFERENCES publications (publication_id),
            parent_receipt_id INTEGER NOT NULL REFERENCES receipts (receipt_id)
        )
    ''')


def get_parent_receipt_id(db, publication_id):
    row = db.execute('''
        SELECT parent_receipt_id FROM correction_parents WHERE publication_id = ?
    ''', (publication_id,)).fetchone()
    return None if row is None else row[0]


def _check_material_set(selected, current, materials):
    if current.manifest != selected.manifest or tuple(item.position for item in materials) != tuple(
        entry.position for entry in current.manifest.entries
    ):
        raise MaterialMismatch('verified material set differs from the selected manifest')


def commit_publication(db, selected, materials):
    with write_transaction(db):
        existing = find_receipt_by_request(db, selected.request_id)
        if existing is not None:
            if get_parent_receipt_id(db, existing.publication_id) is not None:
                raise Conflict(f'request {selected.request_id!r} was issued through the correction entry')
            return existing
        current = get_attempt(db, selected.request_id)
        if current.manifest.parent_manifest_id is not None:
            raise Conflict(f'request {selected.request_id!r} needs the correction entry')
        if current.state != 'accepted':
            raise StateError(f'request {selected.request_id!r} cannot be issued from {current.state}')
        _check_material_set(selected, current, materials)
        publication_id = db.execute('''
            INSERT INTO publications (request_id, manifest_id) VALUES (?, ?)
        ''', (current.request_id, current.manifest.manifest_id)).lastrowid
        store_materials(db, publication_id, materials)
        receipt_id = insert_receipt(db, publication_id)
        mark_issued(db, current.request_id)
        return get_receipt(db, receipt_id)


def commit_correction_publication(db, selected, materials, parent_receipt_id):
    with write_transaction(db):
        existing = find_receipt_by_request(db, selected.request_id)
        if existing is not None:
            bound = get_parent_receipt_id(db, existing.publication_id)
            if bound is None:
                raise Conflict(f'request {selected.request_id!r} was issued through the ordinary entry')
            if bound != parent_receipt_id:
                raise Conflict(f'request {selected.request_id!r} is bound to another parent receipt')
            return existing
        current = get_attempt(db, selected.request_id)
        if current.manifest.parent_manifest_id is None:
            raise Conflict(f'request {selected.request_id!r} needs the ordinary entry')
        if current.state != 'accepted':
            raise StateError(f'request {selected.request_id!r} cannot be issued from {current.state}')
        _check_material_set(selected, current, materials)
        publication_id = db.execute('''
            INSERT INTO publications (request_id, manifest_id) VALUES (?, ?)
        ''', (current.request_id, current.manifest.manifest_id)).lastrowid
        store_materials(db, publication_id, materials)
        db.execute('''
            INSERT INTO correction_parents (publication_id, parent_receipt_id) VALUES (?, ?)
        ''', (publication_id, parent_receipt_id))
        receipt_id = insert_receipt(db, publication_id)
        mark_issued(db, current.request_id)
        return get_receipt(db, receipt_id)
