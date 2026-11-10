from dataclasses import dataclass

import numpy as np

from maprt_client import ArcSegment, StaticBeamPlot, check_beams


@dataclass
class Beam:
    """A stand-in for rt-dicom's `BeamInfo` — the duck-typed record `check_beams` reads.
    maprt-client does not depend on rt-dicom (siblings), so the contract is restated here:
    the four angle/type fields it reads, and the `status` it writes the verdict into.
    """

    couch: str
    gantry_start: str
    gantry_stop: str
    gantry_rotation: str
    delivery_type: str
    status: object = None

# The axes check_beams rebuilds internally, mirrored here to place cells independently.
GANTRY = np.hstack((np.arange(180, 360), np.arange(0, 180)))  # 360 rows, gantry 180..179
COUCH = np.hstack((np.arange(270, 360), np.arange(0, 91)))    # 181 cols, couch 270..90


def clear_map():
    return np.ones((len(GANTRY), len(COUCH)), dtype=bool)


def row(gantry):
    return int(np.flatnonzero(GANTRY == gantry)[0])


def col(couch):
    return int(np.flatnonzero(COUCH == couch)[0])


def beam(couch, gantry_start, gantry_stop=None, rotation="NONE", beam_type="TREATMENT"):
    stop = gantry_start if gantry_stop is None else gantry_stop
    return Beam(str(couch), str(gantry_start), str(stop), rotation, beam_type)


def check(beams, image=None, resolution=1):
    image = clear_map() if image is None else image
    return check_beams(beams, (image, None, None), resolution)


class TestStaticBeams:
    def test_clear_map_passes(self):
        beams, _, _ = check([beam(270, 180)])
        assert beams[0].status is True

    def test_blocked_cell_fails_the_beam(self):
        image = clear_map()
        image[row(180), col(270)] = False
        beams, _, _ = check([beam(270, 180)], image)
        assert beams[0].status is False

    def test_geometry_is_placed_at_the_beam_index(self):
        _, _, statics = check([beam(270, 180)])
        assert statics == [StaticBeamPlot(col(270), row(180), False)]

    def test_setup_flag_is_carried(self):
        _, _, statics = check([beam(270, 180, beam_type="SETUP")])
        assert statics[0].is_setup is True

    def test_verdict_is_written_in_place(self):
        b = beam(270, 180)
        returned, _, _ = check([b])
        assert returned[0] is b        # same object
        assert b.status is True        # mutated in place


class TestArcs:
    def test_clear_arc_passes_and_returns_a_segment(self):
        beams, arcs, _ = check([beam(0, 181, 179, rotation="CW")])
        assert beams[0].status is True
        assert len(arcs) >= 1
        assert all(isinstance(a, ArcSegment) for a in arcs)

    def test_blocked_cell_inside_the_arc_fails_it(self):
        image = clear_map()
        image[row(200), col(0)] = False  # a gantry the 181->179 CW sweep passes through
        beams, _, _ = check([beam(0, 181, 179, rotation="CW")], image)
        assert beams[0].status is False
