from audit.segments import read_segments
from freeze.allocations import rows_of, validate_allocations
from freeze.payloads import decode_segment
from lake_db import Conflict, MaterialMismatch, NotReady, StateError, require_id, require_label
from publish.catalog import commit_correction_publication, get_parent_receipt_id
from publish.receipts import find_receipt_by_request, get_receipt
from publish.verify import verify_materials
from transfer.attempts import get_attempt
from transfer.completions import list_completions
from transfer.operations import list_operations


def publish_correction(receiver, port, request_id, parent_receipt_id):
    require_label(request_id, 'request ID')
    require_id(parent_receipt_id, 'parent receipt ID')
    attempt = get_attempt(receiver, request_id)
    if attempt.manifest.parent_manifest_id is None:
        raise Conflict(f'request {request_id!r} is not a correction request')
    if attempt.state == 'cancelled':
        raise StateError(f'request {request_id!r} was cancelled')
    parent = get_receipt(receiver, parent_receipt_id)
    if attempt.state == 'issued':
        existing = find_receipt_by_request(receiver, request_id)
        if existing is None:
            raise StateError(f'issued request {request_id!r} has no receipt')
        bound = get_parent_receipt_id(receiver, existing.publication_id)
        if bound is None:
            raise Conflict(f'request {request_id!r} was issued through the ordinary entry')
        if bound != parent_receipt_id:
            raise Conflict(f'request {request_id!r} is bound to another parent receipt')
        return existing
    if attempt.state != 'accepted':
        raise NotReady(f'request {request_id!r} awaits remote acceptance')
    manifest = attempt.manifest
    if parent.manifest.manifest_id != manifest.parent_manifest_id:
        raise MaterialMismatch('parent receipt does not carry the correction parent manifest')
    if (parent.manifest.day, parent.manifest.revision) != (manifest.day, manifest.revision - 1):
        raise MaterialMismatch('parent receipt revision does not immediately precede the correction')
    parent_segments = read_segments(receiver, parent)
    materials = verify_materials(
        attempt, list_operations(receiver, request_id), list_completions(receiver, request_id), port,
    )
    child_segments = tuple(decode_segment(material.payload) for material in materials)
    validate_allocations(
        manifest.allocations, rows_of(parent_segments), rows_of(child_segments), MaterialMismatch,
    )
    return commit_correction_publication(receiver, attempt, materials, parent_receipt_id)
