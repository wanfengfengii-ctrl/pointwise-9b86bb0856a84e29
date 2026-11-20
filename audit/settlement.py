"""Cross-receipt settlement audit over an actual frozen correction chain."""

from collections import deque

from audit.segments import read_segments
from freeze.allocations import rows_of, validate_allocations
from lake_db import InvalidInput, MaterialMismatch, StateError, Unknown
from publish.catalog import get_parent_receipt_id
from publish.receipts import get_receipt


def require_position(value, field):
    if type(value) is not int or value < 0:
        raise InvalidInput(f'{field} must be a non-negative integer')
    return value


class _Flow:
    """Integral max-flow network with Dinic level graphs."""

    def __init__(self):
        self.graph = {}
        self.capacity = {}

    def add_node(self, node):
        self.graph.setdefault(node, [])

    def add_edge(self, source, sink, capacity):
        if capacity <= 0:
            return
        self.add_node(source)
        self.add_node(sink)
        self.graph[source].append(sink)
        self.graph[sink].append(source)
        self.capacity[(source, sink)] = self.capacity.get((source, sink), 0) + capacity
        self.capacity.setdefault((sink, source), 0)

    def max_flow(self, source, sink):
        self.add_node(source)
        self.add_node(sink)
        limit = sum(self.capacity.values()) + 1
        total = 0
        while True:
            level = {source: 0}
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for neighbour in self.graph.get(node, ()):
                    if neighbour not in level and self.capacity.get((node, neighbour), 0) > 0:
                        level[neighbour] = level[node] + 1
                        queue.append(neighbour)
            if sink not in level:
                return total
            progress = {node: 0 for node in self.graph}

            def augment(node, available):
                if node == sink:
                    return available
                edges = self.graph.get(node, ())
                while progress[node] < len(edges):
                    neighbour = edges[progress[node]]
                    residual = self.capacity.get((node, neighbour), 0)
                    if residual > 0 and level.get(neighbour) == level[node] + 1:
                        pushed = augment(neighbour, min(available, residual))
                        if pushed:
                            self.capacity[(node, neighbour)] -= pushed
                            self.capacity[(neighbour, node)] += pushed
                            return pushed
                    progress[node] += 1
                return 0

            while True:
                pushed = augment(source, limit)
                if not pushed:
                    break
                total += pushed


def _chain(receiver, base, target):
    """Return the receipts from the base receipt up to the target receipt."""
    path = [target]
    seen = {target.receipt_id}
    current = target
    while current.receipt_id != base.receipt_id:
        manifest = current.manifest
        bound = get_parent_receipt_id(receiver, current.publication_id)
        if manifest.parent_manifest_id is None:
            if bound is not None:
                raise MaterialMismatch('ordinary receipt carries a correction parent binding')
            raise StateError('target receipt does not descend from the base receipt')
        if bound is None:
            raise MaterialMismatch('correction receipt lacks its parent receipt binding')
        if bound in seen:
            raise MaterialMismatch('parent chain repeats a receipt')
        try:
            parent = get_receipt(receiver, bound)
        except Unknown as exc:
            raise MaterialMismatch('parent receipt referenced by the chain is missing') from exc
        path.append(parent)
        seen.add(parent.receipt_id)
        current = parent
    path.reverse()
    return path


def _verify_chain(receiver, path):
    segments = [read_segments(receiver, receipt) for receipt in path]
    for index in range(1, len(path)):
        parent, child = path[index - 1], path[index]
        correction = child.manifest
        if correction.parent_manifest_id != parent.manifest.manifest_id:
            raise MaterialMismatch('correction does not name its exact parent manifest')
        if correction.day != parent.manifest.day or correction.revision != parent.manifest.revision + 1:
            raise MaterialMismatch('correction revision does not immediately follow its parent')
        after = rows_of(segments[index])
        if any(value < 0 for rows in after for value in rows):
            raise MaterialMismatch('correction versions must not carry negative rows')
        validate_allocations(correction.allocations, rows_of(segments[index - 1]), after, MaterialMismatch)
    return segments


def _flow_limits(segments, receipts, base_row, retain_at_target):
    """Max liters of the selected base origin that can reach the requested boundary."""
    rows = [rows_of(actual) for actual in segments]
    last = len(rows) - 1
    base_segment, base_index = base_row
    retained = rows[0][base_segment][base_index]

    source, sink = ('source',), ('sink',)
    flow = _Flow()
    for version, layout in enumerate(rows):
        for segment, values in enumerate(layout):
            for row in range(len(values)):
                flow.add_node((version, segment, row))
    flow.add_edge(source, (0, base_segment, base_index), retained)

    outgoing = {}
    for hop in range(last):
        for item in receipts[hop + 1].manifest.allocations:
            parent_node = (hop, item.parent_segment, item.parent_row)
            child_node = (hop + 1, item.child_segment, item.child_row)
            flow.add_edge(parent_node, child_node, item.liters)
            outgoing[parent_node] = outgoing.get(parent_node, 0) + item.liters

    for node in list(flow.graph):
        if not (isinstance(node[0], int) and node[0] < last):
            continue
        version, segment, row = node
        if retain_at_target:
            continue
        remaining = rows[version][segment][row] - outgoing.get(node, 0)
        if remaining < 0:
            raise MaterialMismatch('allocations exceed a row value in the chain')
        flow.add_edge(node, sink, remaining)
    if retain_at_target:
        for node in list(flow.graph):
            if isinstance(node[0], int) and node[0] == last:
                version, segment, row = node
                flow.add_edge(node, sink, rows[version][segment][row])
    return flow.max_flow(source, sink)


def read_settlement(receiver, base_receipt_id, segment_position, row_position, target_receipt_id):
    require_position(segment_position, 'segment position')
    require_position(row_position, 'row position')
    base = get_receipt(receiver, base_receipt_id)
    target = get_receipt(receiver, target_receipt_id)
    if base.receipt_id == target.receipt_id:
        raise StateError('a target receipt cannot settle against itself')
    path = _chain(receiver, base, target)
    segments = _verify_chain(receiver, path)

    if segment_position >= len(segments[0]):
        raise InvalidInput('base receipt has no such segment position')
    base_segment = segments[0][segment_position]
    if row_position >= len(base_segment.rows):
        raise InvalidInput('base segment has no such row position')
    base_row_model = base_segment.rows[row_position]
    if base_row_model.liters < 0:
        raise InvalidInput('a signed base reading cannot be settled as an original volume')

    carried_max = _flow_limits(segments, path, (segment_position, row_position), retain_at_target=True)
    exited_max = _flow_limits(segments, path, (segment_position, row_position), retain_at_target=False)
    carried_min = base_row_model.liters - exited_max
    if not 0 <= carried_min <= carried_max <= base_row_model.liters:
        raise MaterialMismatch('settlement bounds are inconsistent with the selected row')
    return {
        'base_receipt_id': base.receipt_id,
        'segment_position': segment_position,
        'row_position': row_position,
        'row_provenance': base_row_model.provenance,
        'target_receipt_id': target.receipt_id,
        'carried': (carried_min, carried_max),
        'outstanding': (base_row_model.liters - carried_max, base_row_model.liters - carried_min),
        'settled': carried_min == base_row_model.liters,
    }
