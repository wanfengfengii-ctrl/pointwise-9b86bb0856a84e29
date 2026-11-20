from audit.readback import read, read_settlement
from freeze.issue import freeze, freeze_correction
from intake.revisions import record
from lake_db import InvalidInput, MaterialMismatch, StateError, Unknown
from publish.corrections import publish_correction
from publish.service import publish
from settlement_examples import CASES, ROW_SOURCE, submission
from tests.support import LakeTests


class SettlementTests(LakeTests):
    def build(self, case_id='partial', allocations=None):
        """Publish a full ordinary+correction chain; return receipts from base to target."""
        layouts, hops = CASES[case_id]
        if allocations is not None:
            hops = allocations
        manifests = []
        first = record(self.source, submission(layouts[0]))
        manifests.append(freeze(self.source, first.day, first.revision))
        for number, values in enumerate(layouts[1:], 1):
            version = record(self.source, submission(values))
            manifests.append(freeze_correction(
                self.source, manifests[-1].manifest_id, version.revision, hops[number - 1],
            ))
        requests = [f'settle-{number}' for number in range(1, len(manifests) + 1)]
        for request, manifest in zip(requests, manifests, strict=True):
            self.finish(self.schedule(request, manifest))
        self.source.close()
        receipts = [publish(self.receiver, self.port, requests[0]).receipt_id]
        for request in requests[1:]:
            receipts.append(publish_correction(self.receiver, self.port, request, receipts[-1]).receipt_id)
        return receipts

    def test_partial_case(self):
        receipts = self.build('partial')
        result = read_settlement(self.receiver, receipts[0], 0, 0, receipts[-1])
        self.assertEqual(result['carried'], (6, 10))
        self.assertEqual(result['outstanding'], (0, 4))
        self.assertIs(result['settled'], False)
        self.assertEqual(result['row_provenance'], ROW_SOURCE)
        self.assertEqual((result['segment_position'], result['row_position']), (0, 0))
        self.assertEqual((result['base_receipt_id'], result['target_receipt_id']), (receipts[0], receipts[-1]))
        self.assertIsInstance(result['carried'], tuple)
        self.assertIsInstance(result['outstanding'], tuple)

    def test_complete_case(self):
        receipts = self.build('complete')
        result = read_settlement(self.receiver, receipts[0], 0, 0, receipts[-1])
        self.assertEqual(result['carried'], (10, 10))
        self.assertEqual(result['outstanding'], (0, 0))
        self.assertIs(result['settled'], True)

    def test_settlement_survives_reopen_without_source_or_remote(self):
        receipts = self.build('partial')
        base, target = receipts[0], receipts[-1]
        expected = read_settlement(self.receiver, base, 0, 0, target)
        self.source.close()
        self.receiver.close()
        self.port.close()
        self.reopen_receiver()
        self.assertEqual(read_settlement(self.receiver, base, 0, 0, target), expected)

    def test_same_value_rows_are_distinguished(self):
        layouts, hops = CASES['partial']
        adjusted = list(hops)
        adjusted[0] = ((0, 0, 0, 0, 4), (0, 0, 0, 1, 6))
        receipts = self.build('partial', adjusted)
        first_row = read_settlement(self.receiver, receipts[0], 0, 0, receipts[-1])
        second_row = read_settlement(self.receiver, receipts[0], 0, 1, receipts[-1])
        self.assertEqual(first_row['carried'], (6, 10))
        self.assertEqual(second_row['carried'], (0, 0))
        self.assertEqual(second_row['outstanding'], (10, 10))

    def test_ordinary_receipts_still_show_original_rows(self):
        receipts = self.build('partial')
        first = read(self.receiver, receipts[0])
        self.assertEqual([[row.liters for row in detail.rows] for detail in first.segments], [[10, 10], []])
        self.assertEqual(first.totals.by_segment, (20, 0))
        last = read(self.receiver, receipts[-1])
        self.assertEqual([[row.liters for row in detail.rows] for detail in last.segments], [[4], [12, 4]])

    def test_unknown_and_invalid_positions(self):
        receipts = self.build('partial')
        base, target = receipts[0], receipts[-1]
        with self.assertRaises(Unknown):
            read_settlement(self.receiver, base + 100, 0, 0, target)
        with self.assertRaises(Unknown):
            read_settlement(self.receiver, base, 0, 0, target + 100)
        for segment_position, row_position in ((1, 0), (0, 2), (-1, 0), (0, -1), (True, 0), (0, True)):
            with self.subTest(position=(segment_position, row_position)):
                with self.assertRaises(InvalidInput):
                    read_settlement(self.receiver, base, segment_position, row_position, target)

    def test_target_equal_to_base_rejects(self):
        receipts = self.build('complete')
        with self.assertRaises(StateError):
            read_settlement(self.receiver, receipts[0], 0, 0, receipts[0])

    def test_receipt_from_another_family_is_state_error(self):
        receipts = self.build('partial')
        self.reopen_source()
        version = record(self.source, submission(((10, 10), ())))
        unrelated = freeze(self.source, version.day, version.revision)
        self.finish(self.schedule('unrelated', unrelated))
        unrelated_receipt = publish(self.receiver, self.port, 'unrelated').receipt_id
        with self.assertRaises(StateError):
            read_settlement(self.receiver, unrelated_receipt, 0, 0, receipts[-1])
        with self.assertRaises(StateError):
            read_settlement(self.receiver, receipts[0], 0, 0, unrelated_receipt)

    def test_missing_parent_binding_is_material_mismatch(self):
        receipts = self.build('partial')
        target = receipts[-1]
        self.receiver.execute(
            'DELETE FROM correction_parents WHERE publication_id = '
            '(SELECT publication_id FROM receipts WHERE receipt_id = ?)', (target,),
        )
        with self.assertRaises(MaterialMismatch):
            read_settlement(self.receiver, receipts[0], 0, 0, target)

    def test_corrupted_chain_evidence_is_material_mismatch(self):
        receipts = self.build('partial')
        base, target = receipts[0], receipts[-1]
        self.receiver.execute(
            "UPDATE copy_attempts SET manifest = ? WHERE request_id = 'settle-4'", (b'{}',),
        )
        with self.assertRaises(MaterialMismatch):
            read_settlement(self.receiver, base, 0, 0, target)

    def test_rejecting_query_leaves_no_partial_result(self):
        receipts = self.build('partial')
        before = self.receiver.execute('SELECT count(*) FROM receipts').fetchone()
        with self.assertRaises(StateError):
            read_settlement(self.receiver, receipts[-1], 0, 0, receipts[0])
        self.assertEqual(self.receiver.execute('SELECT count(*) FROM receipts').fetchone(), before)

    def test_mid_chain_correction_can_be_the_base(self):
        receipts = self.build('partial')
        # Base at version two (already a correction); west row 0 holds 4 L there.
        result = read_settlement(self.receiver, receipts[1], 0, 0, receipts[-1])
        self.assertEqual(result['base_receipt_id'], receipts[1])
        self.assertEqual(result['carried'], (4, 4))
        self.assertEqual(result['outstanding'], (0, 0))
        self.assertIs(result['settled'], True)

    def test_target_at_intermediate_version(self):
        receipts = self.build('partial')
        result = read_settlement(self.receiver, receipts[0], 0, 0, receipts[2])
        # By the third version the base 10 L has split into a 4 L west row and a 6 L east row,
        # both of which are still carried, so the whole original survives.
        self.assertEqual(result['carried'], (10, 10))
        self.assertEqual(result['outstanding'], (0, 0))
        self.assertEqual(result['target_receipt_id'], receipts[2])

    def test_single_hop_settlement(self):
        receipts = self.build('partial')
        result = read_settlement(self.receiver, receipts[0], 0, 0, receipts[1])
        # First correction splits 10 L into 4 L and 6 L on the child west segment.
        self.assertEqual(result['carried'], (10, 10))
        self.assertEqual(result['settled'], True)

    def test_corrupt_manifest_identity_is_material_mismatch(self):
        receipts = self.build('partial')
        # Mutate the receipts row so the stored publication points at a foreign manifest id.
        self.receiver.execute(
            'UPDATE publications SET manifest_id = manifest_id + 900 WHERE publication_id = '
            '(SELECT publication_id FROM receipts WHERE receipt_id = ?)', (receipts[-1],),
        )
        with self.assertRaises(MaterialMismatch):
            read_settlement(self.receiver, receipts[0], 0, 0, receipts[-1])
