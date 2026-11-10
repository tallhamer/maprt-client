"""Beam-vs-collision-map checking — verdicts and plot geometry, no Qt.

`check_beams` takes the plan's beams and a MapRT collision map and decides, for each beam,
whether every gantry position it occupies is clear. It writes the verdict back into the beam
(element 0) and returns the plain plot coordinates the view draws — no pyqtgraph, no signals.
The app's `DicomPlanContext.validate_beams` is a thin adapter that calls this and emits.
"""

from collections import namedtuple

import numpy as np

# The plain coordinates the view draws — the collision map's index space. The GUI builds the
# pyqtgraph scatter and curve items from these; nothing here imports pyqtgraph.
StaticBeamPlot = namedtuple('StaticBeamPlot', 'couch gantry is_setup')
ArcSegment = namedtuple('ArcSegment', 'couch start stop')


def _angle_key(angle, row_count=360):
    """Collision-map lookup key for a gantry or couch angle.

    The angle arrives as a DS string on the IEC 61217 0-360 scale — the nominal value,
    with any extended-range designation already dropped by the planning system, so `180E`
    reaches here as `180`. Gantry runs 0..359.9 and couch 270..359.9 and 0..90.0; in both
    cases 360 denotes the same position as 0.

    Only one input needs adjusting, and it is one this function creates: rounding
    359.5..359.9 produces 360, which is not a key. That wraps to 0.

    Anything else is rounded to the nearest position and left to stand or fail on its
    own. A value off the scale therefore fails the lookup rather than being reduced onto
    a row that would pass for real — a gantry angle of 545 must not quietly become 185 in
    a map that decides whether a beam is called safe.
    """
    value = float(angle)

    if 0 <= value <= row_count:
        return str(round(value) % row_count)

    return str(round(value))


def _arc_span_rows(start_angle, stop_angle, rotation_direction, row_count):
    """How many collision-map rows an arc sweeps, taken from the unrounded angles.

    The map holds one row per degree. Measuring the span from the *rounded* endpoints
    instead collapses any arc shorter than a degree onto a single row, which is then
    indistinguishable from a full rotation — an arc through the extended range, say
    179.9 -> 180.1, would be checked against all 360 rows rather than the one it
    occupies.

    A start angle equal to its stop is a genuine full rotation. Anything else sweeps at
    least the row it sits on, however short it is.
    """
    step = -1 if rotation_direction == 'CC' else 1
    span = (step * (stop_angle - start_angle)) % row_count

    if span == 0:
        return row_count

    return max(1, round(span))


def _swept_arc_rows(start_idx, span_rows, rotation_direction, resolution, row_count):
    """Collision-map rows a gantry arc sweeps, in travel order.

    The map's rows run gantry 180..359 then 0..179, so an arc crossing gantry 180 wraps
    around the end of the array. Indices are therefore stepped modulo `row_count` rather
    than sliced: a slice whose stop precedes its start is empty, and `np.all` reports an
    empty selection as safe.

    A full rotation is checked at every row regardless of `resolution`.
    """
    if span_rows >= row_count:
        return np.arange(row_count)

    step = -1 if rotation_direction == 'CC' else 1
    return (start_idx + step * np.arange(0, span_rows, resolution)) % row_count


def _arc_plot_segments(start_idx, span_rows, rotation_direction, row_count):
    """(start, stop) row pairs to draw for an arc.

    An arc that wraps needs two line segments, one either side of the seam; drawing a
    single line from start to stop would run backwards through the rows the arc never
    visits.
    """
    if span_rows >= row_count:
        return [(0, row_count - 1)]

    step = -1 if rotation_direction == 'CC' else 1
    stop_idx = start_idx + step * span_rows

    if 0 <= stop_idx <= row_count - 1:
        return [(start_idx, stop_idx)]

    seam_edge, seam_far = (row_count - 1, 0) if step == 1 else (0, row_count - 1)
    return [(start_idx, seam_edge), (seam_far, stop_idx % row_count)]


def check_beams(beams, map_data, arc_check_resolution):
    """Check every beam against the collision map.

    Each beam is a `BeamInfo`-like record — anything carrying `couch`, `gantry_start`,
    `gantry_stop`, `gantry_rotation` and `delivery_type` fields and a writable `status` — so
    this module needs no import from rt-dicom, the producer. The verdict is written back into
    `beam.status` (`True`/`False`) in place. `map_data` is `(collision_map, x_ticks, y_ticks)`;
    `arc_check_resolution` is the gantry-row stride for arc sweeps.

    Returns `(beams, arc_plots, static_plots)` — the same beam list (mutated), and the plot
    geometry as `ArcSegment` / `StaticBeamPlot` tuples in the map's index space.
    """
    collision_map, x_ticks, y_ticks = map_data

    # construct couch index positions for resampled map
    c0 = np.arange(270, 360, 1)
    c1 = np.arange(0, 91, 1)
    couch_values = np.hstack((c0, c1))
    i_idx = np.arange(len(couch_values))

    # construct gantry index positions for resampled map
    g0 = np.arange(180, 360, 1)
    g1 = np.arange(0, 180, 1)
    gantry_values = np.hstack((g0, g1))
    j_idx = np.arange(len(gantry_values))

    x_map = dict([(str(couch_values[x]), int(i_idx[x])) for x in range(len(i_idx))])
    y_map = dict([(str(gantry_values[y]), int(j_idx[y])) for y in range(len(j_idx))])

    arc_plots = []
    static_plots = []

    for beam in beams:
        if beam.gantry_rotation == 'NONE':

            g_start_idx = y_map[_angle_key(beam.gantry_start)]
            c_pos_idx = x_map[_angle_key(beam.couch)]
            is_ok = np.all(collision_map[g_start_idx, c_pos_idx])
            beam.status = True if is_ok else False

            static_plots.append(StaticBeamPlot(c_pos_idx, g_start_idx, beam.delivery_type == 'SETUP'))

        else:
            g_start_idx = y_map[_angle_key(beam.gantry_start)]
            c_pos_idx = x_map[_angle_key(beam.couch)]

            # The stop angle needs no map lookup: the span is measured from the
            # angles themselves, and normalization leaves no gantry value that could
            # fail one.
            span_rows = _arc_span_rows(float(beam.gantry_start),
                                       float(beam.gantry_stop),
                                       beam.gantry_rotation,
                                       len(j_idx)
                                       )

            swept_rows = _swept_arc_rows(g_start_idx,
                                         span_rows,
                                         beam.gantry_rotation,
                                         arc_check_resolution,
                                         len(j_idx)
                                         )

            is_ok = np.all(collision_map[swept_rows, c_pos_idx])
            beam.status = True if is_ok else False

            for segment_start, segment_stop in _arc_plot_segments(g_start_idx,
                                                                  span_rows,
                                                                  beam.gantry_rotation,
                                                                  len(j_idx)
                                                                  ):
                arc_plots.append(ArcSegment(c_pos_idx, segment_start, segment_stop))

    return beams, arc_plots, static_plots
