from dataclasses import replace
import sqlite3

from audit.readback import read
from freeze.issue import freeze, freeze_correction
from freeze.store import next_manifest_id
from intake.revisions import record
from lake_db import Conflict, InvalidInput, MaterialMismatch, NotReady, StateError, Unknown
from publish.corrections import publish_correction
from publish.receipts import find_receipt_by_request
from publish.service import publish
from settlement_examples import CASES, submission
from tests.support import LakeTests
from transfer.attempts import get_attempt
from transfer.dispatch import cancel, copy
from transfer.operations import list_operations


class CorrectionPublishTests(LakeTests):
    def stage_chain(self, case_id='partial'):
        """Record, freeze and schedule every version; return request ids and manifests."""
        layouts, allocations = CASES[case_id]
        manifests = []
        first = record(self.source, submission(layouts[0]))
        manifests.append(freeze(self.source, first.day, first.revision))
        for number, values in enumerate(layouts[1:], 1):
            version = record(self.source, submission(values))
            manifests.append(freeze_correction(
                self.source, manifests[-1].manifest_id, version.revision, allocations[number - 1],
            ))
        requests = [f'chain-{number}' for number in range(1, len(manifests) + 1)]
        for request, manifest in zip(requests, manifests, strict=True):
            copy(self.source, self.receiver, self.port, request, manifest.manifest_id)
        return requests, manifests

    def finish_request(self, request):
        operations = list_operations(self.receiver, request)
        for operation in operations:
            self.write(operation)
        for operation in operations:
            self.deliver(operation)
        return operations

    def finish_all(self, requests):
        for request in requests:
            self.finish_request(request)

    def issue_chain(self, requests):
        receipts = [publish(self.receiver, self.port, requests[0]).receipt_id]
        for request in requests[1:]:
            receipts.append(publish_correction(self.receiver, self.port, request, receipts[-1]).receipt_id)
        return receipts

    def test_correction_publish_walks_the_chain(self):
        requests, _ = self.stage_chain()
        self.finish_all(requests)
        self.source.close()
        receipts = self.issue_chain(requests)
        self.assertEqual(receipts, list(range(1, len(requests) + 1)))
        self.reopen_receiver()
        self.assertEqual(self.issue_chain(requests), receipts)
        result = read(self.receiver, receipts[-1])
        self.assertEqual([row.liters for row in result.segments[0].rows], [4])
        self.assertEqual([[row.liters for row in detail.rows] for detail in result.segments[1:]], [[12, 4]])
        self.assertEqual(result.totals.total, 20)

    def test_unknown_parent_and_ordinary_parent_rejection(self):
        requests, _ = self.stage_chain()
        self.finish_all(requests)
        first = publish(self.receiver, self.port, requests[0]).receipt_id
        with self.assertRaises(Unknown):
            publish_correction(self.receiver, self.port, requests[1], first + 500)
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))
        # Version two's manifest is a correction, but a correction request cannot use itself as
        # parent through the ordinary entry; make a separate ordinary copy of revision one only.
        with self.assertRaises(Conflict):
            publish(self.receiver, self.port, requests[1])
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))

    def test_duplicate_freeze_of_parent_cannot_substitute(self):
        requests, manifests = self.stage_chain()
        self.finish_all(requests)
        parent = publish(self.receiver, self.port, requests[0])
        duplicate = freeze(self.source, parent.manifest.day, parent.manifest.revision)
        self.assertNotEqual(duplicate.manifest_id, parent.manifest.manifest_id)
        self.assertEqual(duplicate.entries, parent.manifest.entries)
        copy(self.source, self.receiver, self.port, 'duplicate-parent', duplicate.manifest_id)
        self.finish_request('duplicate-parent')
        duplicate_receipt = publish(self.receiver, self.port, 'duplicate-parent')
        # Same body and revision, but the correction names the original manifest id.
        with self.assertRaises(MaterialMismatch):
            publish_correction(self.receiver, self.port, requests[1], duplicate_receipt.receipt_id)
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))
        self.assertEqual(
            publish_correction(self.receiver, self.port, requests[1], parent.receipt_id).receipt_id,
            3,
        )

    def test_two_receipts_from_one_parent_manifest(self):
        requests, manifests = self.stage_chain()
        self.finish_all(requests)
        parent_manifest = manifests[0]
        copy(self.source, self.receiver, self.port, 'parent-alias', parent_manifest.manifest_id)
        self.finish_request('parent-alias')
        first_parent = publish(self.receiver, self.port, requests[0]).receipt_id
        alias_parent = publish(self.receiver, self.port, 'parent-alias').receipt_id
        self.assertNotEqual(first_parent, alias_parent)
        self.assertEqual(
            self.receiver.execute('SELECT count(*) FROM receipts').fetchone(), (2,),
        )
        # Issue using the alias receipt: the correction records that exact receipt as its parent.
        receipt = publish_correction(self.receiver, self.port, requests[1], alias_parent)
        # Replaying with the other receipt from the same parent manifest is still a conflict.
        with self.assertRaises(Conflict):
            publish_correction(self.receiver, self.port, requests[1], first_parent)
        self.assertEqual(publish_correction(self.receiver, self.port, requests[1], alias_parent), receipt)
        self.assertEqual(
            self.receiver.execute(
                'SELECT parent_receipt_id FROM correction_parents WHERE publication_id = ?',
                (receipt.publication_id,),
            ).fetchone(),
            (alias_parent,),
        )

    def test_cross_entry_conflicts(self):
        requests, _ = self.stage_chain()
        self.finish_all(requests)
        parent = publish(self.receiver, self.port, requests[0])
        with self.assertRaises(Conflict):
            publish(self.receiver, self.port, requests[1])
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))
        correction = publish_correction(self.receiver, self.port, requests[1], parent.receipt_id)
        with self.assertRaises(Conflict):
            publish(self.receiver, self.port, requests[1])
        # The ordinary request refuses the correction entry and vice versa.
        with self.assertRaises(Conflict):
            publish_correction(self.receiver, self.port, requests[0], correction.receipt_id)
        self.assertEqual(publish_correction(self.receiver, self.port, requests[1], parent.receipt_id), correction)
        self.assertEqual(publish(self.receiver, self.port, requests[0]).receipt_id, parent.receipt_id)

    def test_replay_with_other_parent_on_same_chain_conflicts(self):
        requests, _ = self.stage_chain()
        self.finish_all(requests)
        first = publish(self.receiver, self.port, requests[0]).receipt_id
        second = publish_correction(self.receiver, self.port, requests[1], first)
        third = publish_correction(self.receiver, self.port, requests[2], second.receipt_id)
        with self.assertRaises(Conflict):
            publish_correction(self.receiver, self.port, requests[2], first)
        self.assertEqual(publish_correction(self.receiver, self.port, requests[2], second.receipt_id), third)

    def test_missing_child_material_retries_as_not_ready(self):
        requests, _ = self.stage_chain()
        self.finish_request(requests[0])
        self.finish_request(requests[1])
        first = publish(self.receiver, self.port, requests[0]).receipt_id
        second = publish_correction(self.receiver, self.port, requests[1], first).receipt_id
        operations = list_operations(self.receiver, requests[2])
        self.write(operations[0])
        self.write(operations[1])
        self.deliver(operations[0])
        with self.assertRaises(NotReady):
            publish_correction(self.receiver, self.port, requests[2], second)
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[2]))
        self.deliver(operations[1])
        receipt = publish_correction(self.receiver, self.port, requests[2], second)
        self.assertEqual(read(self.receiver, receipt.receipt_id).totals.total, 20)

    def test_unwritten_child_segment_rejects_as_not_ready(self):
        requests, _ = self.stage_chain()
        self.finish_request(requests[0])
        self.finish_request(requests[1])
        first = publish(self.receiver, self.port, requests[0]).receipt_id
        second = publish_correction(self.receiver, self.port, requests[1], first)
        with self.assertRaises(NotReady):
            publish_correction(self.receiver, self.port, requests[2], second.receipt_id)
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[2]))

    def test_cancelled_correction_cannot_issue(self):
        requests, _ = self.stage_chain()
        self.finish_all(requests)
        first = publish(self.receiver, self.port, requests[0]).receipt_id
        cancel(self.receiver, requests[1])
        with self.assertRaises(StateError):
            publish_correction(self.receiver, self.port, requests[1], first)
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))
        self.assertEqual(get_attempt(self.receiver, requests[1]).state, 'cancelled')





class CorrectionJointTests(LakeTests):
    """Joint verification coverage called out by SETTLEMENT.md."""

    def chain(self, layouts, hops):
        manifests = []
        first = record(self.source, submission(layouts[0]))
        manifests.append(freeze(self.source, first.day, first.revision))
        for number, values in enumerate(layouts[1:], 1):
            version = record(self.source, submission(values))
            manifests.append(freeze_correction(
                self.source, manifests[-1].manifest_id, version.revision, hops[number - 1],
            ))
        return manifests

    def test_six_liter_row_over_transfer_rejects_at_freeze(self):
        first = record(self.source, submission(((6,), ())))
        parent = freeze(self.source, first.day, first.revision)
        child = record(self.source, submission(((4,), (3,))))
        stored = next_manifest_id(self.source)
        with self.assertRaises(InvalidInput):
            freeze_correction(self.source, parent.manifest_id, child.revision, ((0, 0, 0, 0, 7),))
        self.assertEqual(next_manifest_id(self.source), stored)

    def test_six_liter_row_over_transfer_rejects_at_publish(self):
        from freeze.manifest import decode_manifest, encode_manifest

        layouts = (((6,), ()), ((4,), (2,)))
        manifests = self.chain(layouts, (((0, 0, 0, 0, 4), (0, 0, 1, 0, 2)),))
        requests = ['six-parent', 'six-child']
        for request, manifest in zip(requests, manifests, strict=True):
            self.finish(self.schedule(request, manifest))
        parent = publish(self.receiver, self.port, requests[0])
        copied, = self.receiver.execute(
            "SELECT manifest FROM copy_attempts WHERE request_id = 'six-child'",
        ).fetchone()
        forged = decode_manifest(copied)
        tampered = replace(forged, allocations=tuple(
            replace(item, liters=7) for item in forged.allocations
        ))
        self.receiver.execute(
            "UPDATE copy_attempts SET manifest = ? WHERE request_id = 'six-child'",
            (encode_manifest(tampered),),
        )
        with self.assertRaises(MaterialMismatch):
            publish_correction(self.receiver, self.port, 'six-child', parent.receipt_id)
        self.assertIsNone(find_receipt_by_request(self.receiver, 'six-child'))

    def test_landed_child_segment_must_match_manifest(self):
        layouts, hops = CASES['partial']
        manifests = self.chain(layouts, hops)
        parent_request, child_request = 'landed-parent', 'landed-child'
        self.finish(self.schedule(parent_request, manifests[0]))
        parent = publish(self.receiver, self.port, parent_request)
        submission_two = submission(layouts[1])
        foreign = replace(submission_two.segments[0], provenance='foreign/landing')
        operations = self.accepted_fixture(
            child_request, manifests[1], (foreign, submission_two.segments[1]),
        )
        for operation in operations:
            self.write(operation)
            self.deliver(operation)
        self.source.close()
        with self.assertRaises(MaterialMismatch):
            publish_correction(self.receiver, self.port, child_request, parent.receipt_id)
        self.assertIsNone(find_receipt_by_request(self.receiver, child_request))

    def test_correction_commit_failure_leaves_no_receipt_and_retries(self):
        layouts, hops = CASES['partial']
        manifests = self.chain(layouts, hops)
        requests = ['commit-1', 'commit-2']
        for request, manifest in zip(requests, manifests[:2], strict=True):
            self.finish(self.schedule(request, manifest))
        parent = publish(self.receiver, self.port, requests[0])
        self.receiver.executescript('''
            CREATE TABLE fault_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE fault_child (
                parent INTEGER REFERENCES fault_parent (id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER fail_correction_commit AFTER UPDATE OF state ON copy_attempts
            WHEN NEW.request_id = 'commit-2' AND NEW.state = 'issued'
            BEGIN INSERT INTO fault_child VALUES (99); END;
        ''')
        with self.assertRaises(sqlite3.IntegrityError):
            publish_correction(self.receiver, self.port, requests[1], parent.receipt_id)
        self.assertFalse(self.receiver.in_transaction)
        self.reopen_receiver()
        self.assertIsNone(find_receipt_by_request(self.receiver, requests[1]))
        self.assertEqual(get_attempt(self.receiver, requests[1]).state, 'accepted')
        self.assertEqual(self.receiver.execute('SELECT count(*) FROM correction_parents').fetchone(), (0,))
        self.receiver.execute('DROP TRIGGER fail_correction_commit')
        receipt = publish_correction(self.receiver, self.port, requests[1], parent.receipt_id)
        self.assertEqual(publish_correction(self.receiver, self.port, requests[1], parent.receipt_id), receipt)
        self.assertEqual(
            self.receiver.execute('SELECT count(*) FROM correction_parents').fetchone(), (1,),
        )
