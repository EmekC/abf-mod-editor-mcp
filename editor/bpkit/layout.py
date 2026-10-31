"""Placement of generated Blueprint nodes. Pure arithmetic, no `unreal`.

New nodes go in columns by longest path from a source, below everything that is already in the graph,
so they never overlap existing nodes or the default (ghost) event bubbles.
"""

COL_W = 420
NODE_W = 320
ROW_GAP = 48
BELOW_GAP = 200


def node_size(pin_count_in, pin_count_out):
    return 320, 60 + 26 * max(pin_count_in, pin_count_out)


def overlaps(a, b):
    """a and b are (x, y, w, h)."""
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def place(new, wires, occupied):
    """new: [(id, w, h)], wires: [(from_id, to_id)] among new ids, occupied: [(x, y, w, h)].
    Returns {id: (x, y)}."""
    ids = [n[0] for n in new]
    size = {n[0]: (n[1], n[2]) for n in new}
    preds = {i: [] for i in ids}
    for a, b in wires:
        if a in preds and b in preds and a != b:
            preds[b].append(a)

    col = {}

    def depth(i, stack):
        if i in col:
            return col[i]
        stack.add(i)
        d = 0
        for p in preds[i]:
            if p not in stack:
                d = max(d, depth(p, stack) + 1)
        stack.discard(i)
        col[i] = d
        return d

    for i in ids:
        depth(i, set())
    base_x = min((o[0] for o in occupied), default=0)
    base_y = max((o[1] + o[3] for o in occupied), default=None)
    base_y = 0 if base_y is None else base_y + BELOW_GAP
    next_y = {}
    out = {}
    for i in ids:
        c = col[i]
        y = next_y.get(c, base_y)
        out[i] = (base_x + c * COL_W, y)
        next_y[c] = y + size[i][1] + ROW_GAP
    return out
