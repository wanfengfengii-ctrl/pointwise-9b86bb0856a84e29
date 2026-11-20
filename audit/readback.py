from dataclasses import dataclass

from audit.segments import SegmentDetail, read_segments
from audit.settlement import read_settlement
from audit.totals import LiterTotals, calculate_liters
from publish.receipts import Receipt, get_receipt

__all__ = ['ReadbackResult', 'read', 'read_settlement', 'get_receipt']


@dataclass(frozen=True, slots=True)
class ReadbackResult:
    receipt: Receipt
    segments: tuple[SegmentDetail, ...]
    totals: LiterTotals


def read(receiver, receipt_id):
    receipt = get_receipt(receiver, receipt_id)
    segments = read_segments(receiver, receipt)
    return ReadbackResult(receipt, segments, calculate_liters(segments))
