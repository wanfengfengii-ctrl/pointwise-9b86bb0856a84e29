import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.readback import read
from freeze.issue import freeze
from intake.revisions import record
from intake.rows import ObservationRow, RevisionInput, SourceSegment
from lake_db import open_receiver, open_source
from publish.service import publish
from transfer.completions import event
from transfer.dispatch import copy
from transfer.operations import list_operations
from transfer.port import CopyPort

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


def main():
    for case_id in CASES:
        with TemporaryDirectory() as directory:
            print(json.dumps(run_v1(Path(directory), case_id), sort_keys=True))


if __name__ == "__main__":
    main()
