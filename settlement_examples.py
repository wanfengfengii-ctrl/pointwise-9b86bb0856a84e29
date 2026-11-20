"""Two four-version worked examples for correction settlement."""

import json
from contextlib import closing
from pathlib import Path

from audit.readback import read, read_settlement
from freeze.issue import freeze, freeze_correction
from intake.revisions import record
from intake.rows import ObservationRow, RevisionInput, SourceSegment
from lake_db import open_receiver, open_source
from publish.corrections import publish_correction
from publish.service import publish
from transfer.completions import event
from transfer.dispatch import copy
from transfer.operations import list_operations
from transfer.port import CopyPort
from tempfile import TemporaryDirectory

DAY = "2026-04-20"
ROW_SOURCE = "station-sheet/shared-line"
FIRST_VERSIONS = (((10, 10), ()), ((4, 6), (10,)), ((4,), (16,)))
FIRST_ALLOCATIONS = (
    ((0, 0, 0, 0, 4), (0, 0, 0, 1, 6), (0, 1, 1, 0, 10)),
    ((0, 0, 0, 0, 4), (0, 1, 1, 0, 6), (1, 0, 1, 0, 10)),
)
CASES = {
    "partial": (
        FIRST_VERSIONS + (((4,), (12, 4)),),
        FIRST_ALLOCATIONS + (((0, 0, 0, 0, 4), (1, 0, 1, 0, 12)),),
    ),
    "complete": (
        FIRST_VERSIONS + (((4,), (8, 8)),),
        FIRST_ALLOCATIONS + (((0, 0, 0, 0, 4), (1, 0, 1, 0, 8), (1, 0, 1, 1, 8)),),
    ),
}
EXPECTED = {
    "partial": {"carried": (6, 10), "outstanding": (0, 4), "settled": False},
    "complete": {"carried": (10, 10), "outstanding": (0, 0), "settled": True},
}


def submission(layout):
    return RevisionInput(DAY, tuple(
        SourceSegment(name, f"{name}/field-sheet", tuple(ObservationRow(liters, ROW_SOURCE) for liters in rows))
        for name, rows in zip(("west", "east"), layout, strict=True)
    ))


def run_v1(root, case_id):
    layouts, _ = CASES[case_id]
    receiver_path = root / "receiver.sqlite3"
    with closing(open_receiver(receiver_path)) as receiver, CopyPort(root / "remote.sqlite3") as port:
        requests = []
        with closing(open_source(root / "source.sqlite3")) as source:
            for number, layout in enumerate(layouts, 1):
                version = record(source, submission(layout))
                manifest = freeze(source, version.day, version.revision)
                request = f"ordinary-{number}"
                copy(source, receiver, port, request, manifest.manifest_id)
                requests.append(request)
        receipts = []
        for request in requests:
            operations = list_operations(receiver, request)
            for operation in operations:
                port.execute(operation.operation_id)
            for operation in reversed(operations):
                port.deliver(operation.operation_id, lambda notice: event(receiver, port, notice))
            receipts.append(publish(receiver, port, request).receipt_id)
    with closing(open_receiver(receiver_path)) as receiver:
        ledger = []
        for number, (receipt_id, layout) in enumerate(zip(receipts, layouts, strict=True), 1):
            result = read(receiver, receipt_id)
            expected = submission(layout).segments
            observed = tuple((detail.provenance.segment_id, detail.provenance.segment_source, detail.rows)
                             for detail in result.segments)
            if (observed != tuple((segment.segment_id, segment.provenance, segment.rows) for segment in expected)
                    or result.totals.total != 20 or result.receipt.manifest.revision != number):
                raise AssertionError(f"{case_id}: ordinary receipt {receipt_id} does not match revision {number}")
            ledger.append({"receipt_id": receipt_id, "revision": number, "segments": layout,
                           "total_liters": result.totals.total})
    return {"case": case_id, "ordinary_ledger": ledger}


def run_correction(root, case_id):
    layouts, allocations = CASES[case_id]
    receiver_path = root / "receiver.sqlite3"
    with closing(open_receiver(receiver_path)) as receiver, CopyPort(root / "remote.sqlite3") as port:
        with closing(open_source(root / "source.sqlite3")) as source:
            first = record(source, submission(layouts[0]))
            manifests = [freeze(source, first.day, first.revision)]
            for number, layout in enumerate(layouts[1:], 1):
                version = record(source, submission(layout))
                manifests.append(freeze_correction(
                    source, manifests[-1].manifest_id, version.revision, allocations[number - 1],
                ))
            requests = [f"ordinary-{number}" for number in range(1, len(manifests) + 1)]
            for request, manifest in zip(requests, manifests, strict=True):
                copy(source, receiver, port, request, manifest.manifest_id)
            for request in requests:
                operations = list_operations(receiver, request)
                for operation in operations:
                    port.execute(operation.operation_id)
                for operation in reversed(operations):
                    port.deliver(operation.operation_id, lambda notice: event(receiver, port, notice))
        receipts = [publish(receiver, port, requests[0]).receipt_id]
        parent_receipts = list(receipts)
        for request in requests[1:]:
            receipts.append(publish_correction(receiver, port, request, parent_receipts[-1]).receipt_id)
            parent_receipts.append(receipts[-1])
    with closing(open_receiver(receiver_path)) as receiver:
        settlement = read_settlement(receiver, receipts[0], 0, 0, receipts[-1])
        expected = EXPECTED[case_id]
        if {key: settlement[key] for key in expected} != expected:
            raise AssertionError(f"{case_id}: settlement {settlement} does not match {expected}")
        first_view = read(receiver, receipts[0])
        if [row.liters for row in first_view.segments[0].rows] != [10, 10]:
            raise AssertionError(f"{case_id}: ordinary receipt no longer shows the two 10 L rows")
        if first_view.segments[1].rows:
            raise AssertionError(f"{case_id}: ordinary receipt should show an empty east segment")
    return {"case": case_id, "settlement": settlement}


def main():
    for case_id in CASES:
        with TemporaryDirectory() as directory:
            print(json.dumps(run_v1(Path(directory), case_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
