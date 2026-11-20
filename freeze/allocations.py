"""Exact integer allocation primitives shared by freezing, issuing and audit."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Allocation:
    parent_segment: int
    parent_row: int
    child_segment: int
    child_row: int
    liters: int


def coerce_allocations(items, error):
    """Validate the wire-level shape of an allocation sequence."""
    if not isinstance(items, (list, tuple)):
        raise error('allocations must be an ordered sequence')
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 5:
            raise error('each allocation must contain five integers')
        values = []
        for value in item:
            if type(value) is not int:
                raise error('allocation positions and liters must be integers')
            values.append(value)
        parent_segment, parent_row, child_segment, child_row, liters = values
        if parent_segment < 0 or parent_row < 0 or child_segment < 0 or child_row < 0:
            raise error('allocation positions must be non-negative')
        if liters <= 0:
            raise error('allocation liters must be positive')
        key = (parent_segment, parent_row, child_segment, child_row)
        if key in seen:
            raise error('duplicate parent/child position pair')
        seen.add(key)
        result.append(Allocation(*values))
    return tuple(result)


def encode_allocations(allocations):
    return [
        [item.parent_segment, item.parent_row, item.child_segment, item.child_row, item.liters]
        for item in allocations
    ]


def rows_of(segments):
    return tuple(tuple(row.liters for row in segment.rows) for segment in segments)


def require_nonnegative_rows(rows, error):
    for segment in rows:
        for value in segment:
            if type(value) is not int or value < 0:
                raise error('correction rows must be non-negative integers')


def validate_allocations(allocations, parent_rows, child_rows, error):
    """Check positions, uniqueness and transfer capacity against actual rows."""
    seen = set()
    outgoing = {}
    incoming = {}
    for item in allocations:
        if not (
            0 <= item.parent_segment < len(parent_rows)
            and 0 <= item.parent_row < len(parent_rows[item.parent_segment])
        ):
            raise error('allocation names a missing parent row')
        if not (
            0 <= item.child_segment < len(child_rows)
            and 0 <= item.child_row < len(child_rows[item.child_segment])
        ):
            raise error('allocation names a missing child row')
        key = (item.parent_segment, item.parent_row, item.child_segment, item.child_row)
        if key in seen:
            raise error('duplicate parent/child position pair')
        seen.add(key)
        outgoing[key[:2]] = outgoing.get(key[:2], 0) + item.liters
        incoming[key[2:]] = incoming.get(key[2:], 0) + item.liters
    for (segment, row), total in outgoing.items():
        if total > parent_rows[segment][row]:
            raise error('outgoing allocation exceeds its parent row')
    for (segment, row), total in incoming.items():
        if total > child_rows[segment][row]:
            raise error('incoming allocation exceeds its child row')
