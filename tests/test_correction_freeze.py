from freeze.allocations import Allocation
from freeze.issue import freeze, freeze_correction
from freeze.store import load_manifest, next_manifest_id
from intake.revisions import record
from intake.rows import ObservationRow, RevisionInput, SourceSegment
from lake_db import InvalidInput, Unknown
from settlement_examples import CASES, DAY, ROW_SOURCE, submission
from tests.support import LakeTests


def layout(*segments):
    """Build a submission from per-segment liter tuples using the shared row source."""
    names = ('west', 'east', 'north', 'south')
    return RevisionInput(DAY, tuple(
        SourceSegment(names[index], f'{names[index]}/field-sheet',
                      tuple(ObservationRow(value, ROW_SOURCE) for value in values))
        for index, values in enumerate(segments)
    ))


class CorrectionFreezeTests(LakeTests):
    def seed_chain(self, case_id):
        layouts, allocations = CASES[case_id]
        first = record(self.source, submission(layouts[0]))
        manifests = [freeze(self.source, first.day, first.revision)]
        for number, values in enumerate(layouts[1:], 1):
            version = record(self.source, submission(values))
            manifests.append(
                freeze_correction(self.source, manifests[-1].manifest_id, version.revision,
                                  allocations[number - 1])
            )
        return layouts, allocations, manifests

    def test_correction_manifest_binds_parent_and_allocations(self):
        _, allocations, manifests = self.seed_chain('partial')
        for parent, child, hops in zip(manifests, manifests[1:], allocations):
            self.assertEqual(child.parent_manifest_id, parent.manifest_id)
            self.assertEqual(child.revision, parent.revision + 1)
            self.assertEqual(child.day, parent.day)
            self.assertEqual(child.allocations, tuple(Allocation(*item) for item in hops))
            self.assertEqual(load_manifest(self.source, child.manifest_id), child)
        self.assertIsNone(manifests[0].parent_manifest_id)

    def test_empty_allocation_is_allowed(self):
        a = record(self.source, layout((10,), (5,)))
        b = record(self.source, layout((10,), (5,)))
        parent = freeze(self.source, a.day, a.revision)
        child = freeze_correction(self.source, parent.manifest_id, b.revision, [])
        self.assertEqual(child.allocations, ())
        self.assertEqual(child.parent_manifest_id, parent.manifest_id)

    def test_allocations_must_be_positive_and_well_formed(self):
        a = record(self.source, layout((10,), ()))
        b = record(self.source, layout((10,), ()))
        parent = freeze(self.source, a.day, a.revision)
        stored = next_manifest_id(self.source)
        for bad in (
            ((0, 0, 0, 0),),
            ((0, 0, 0, 0, 0),),
            ((0, 0, 0, 0, -1),),
            ((0, 0, 0, 0, True),),
            ((0, 0, 0, 0, 5), (0, 0, 0, 0, 5)),
            ((0, 0, 0, 1, 5),),
            ((0, 1, 0, 0, 5),),
            ((-1, 0, 0, 0, 5),),
            'not a sequence',
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInput):
                    freeze_correction(self.source, parent.manifest_id, b.revision, bad)
        self.assertEqual(next_manifest_id(self.source), stored)

    def test_oversized_transfer_rejects_both_directions(self):
        a = record(self.source, layout((10,),))
        b = record(self.source, layout((10,),))
        parent = freeze(self.source, a.day, a.revision)
        stored = next_manifest_id(self.source)
        with self.assertRaises(InvalidInput):
            freeze_correction(self.source, parent.manifest_id, b.revision, ((0, 0, 0, 0, 11),))
        self.assertEqual(next_manifest_id(self.source), stored)

        c = record(self.source, layout((12,),))
        with self.assertRaises(InvalidInput):
            freeze_correction(
                self.source, parent.manifest_id, c.revision, ((0, 0, 0, 0, 10), (0, 0, 0, 1, 1)),
            )
        self.assertEqual(next_manifest_id(self.source), stored)

    def test_negative_parent_and_child_rows_reject(self):
        a = record(self.source, layout((10,),))
        parent = freeze(self.source, a.day, a.revision)
        sink = record(self.source, RevisionInput(a.day, (
            SourceSegment('west', 'west/field', (ObservationRow(10, 'x'), ObservationRow(-1, 'y'))),
        )))
        stored = next_manifest_id(self.source)
        with self.assertRaises(InvalidInput):
            freeze_correction(self.source, parent.manifest_id, sink.revision, ())
        self.assertEqual(next_manifest_id(self.source), stored)

    def test_revision_relationship_and_unknown_parent(self):
        a = record(self.source, layout((10,),))
        c = record(self.source, layout((10,),))
        d = record(self.source, layout((10,),))
        parent = freeze(self.source, a.day, a.revision)
        stored = next_manifest_id(self.source)
        with self.assertRaises(InvalidInput):
            freeze_correction(self.source, parent.manifest_id, d.revision, ())
        with self.assertRaises(Unknown):
            freeze_correction(self.source, parent.manifest_id + 50, c.revision, ())
        with self.assertRaises(Unknown):
            freeze_correction(self.source, parent.manifest_id, 99, ())
        self.assertEqual(next_manifest_id(self.source), stored)
        child = freeze_correction(self.source, parent.manifest_id, c.revision, ())
        self.assertEqual(child.parent_manifest_id, parent.manifest_id)

    def test_signed_parent_rows_cannot_be_corrected(self):
        a = record(self.source, layout((10, -1)))
        b = record(self.source, layout((10,)))
        parent = freeze(self.source, a.day, a.revision)
        with self.assertRaises(InvalidInput):
            freeze_correction(self.source, parent.manifest_id, b.revision, ())
