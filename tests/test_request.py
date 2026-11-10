"""build_map_request is pure: no Qt, no context, no network — the request-side mirror of
test_maprt_wire. These pin the clinical maths that used to be buried in the QNAM transport's
get_map, where nothing could reach it: the surface-shift-to-isocenter conversion, the rounding
that goes into the label, the cm-to-mm buffers, and that two bodies come back differing only in
resolution while the metadata is shared."""

from maprt_client import build_map_request


def build(**overrides):
    kwargs = {
        "isocenter": (10.0, 20.0, 30.0),
        "couch_buffer": 2.0,
        "patient_buffer": 3.0,
        "surface_id": "surface-1",
        "room_id": "room-1",
        "room_scale": "1.0",
        "patient_orientation": "HFS",
    }
    kwargs.update(overrides)
    return build_map_request(**kwargs)


class TestBodies:
    def test_two_bodies_low_then_high_resolution(self):
        _, bodies = build()
        assert [b["HighResolution"] for b in bodies] == [False, True]

    def test_buffers_are_converted_cm_to_mm(self):
        _, bodies = build(couch_buffer=2.0, patient_buffer=3.0)
        assert bodies[0]["CouchBuffer"] == 20.0
        assert bodies[0]["PatientBuffer"] == 30.0

    def test_ids_and_orientation_are_carried_through(self):
        _, bodies = build(surface_id="s9", room_id="r9", patient_orientation="FFP")
        body = bodies[0]
        assert body["PatientSurfaceId"] == "s9"
        assert body["TreatmentRoomId"] == "r9"
        assert body["PatientPosition"] == "FFP"
        assert body["Isocenter"]["CoordinateSystem"] == "IEC_61217"

    def test_bodies_differ_only_in_resolution(self):
        _, bodies = build()
        low, high = bodies
        assert {k: v for k, v in low.items() if k != "HighResolution"} == \
               {k: v for k, v in high.items() if k != "HighResolution"}


class TestIsocenterShift:
    def test_no_shift_leaves_the_isocenter_in_place(self):
        _, bodies = build(isocenter=(10.0, 20.0, 30.0))
        iso = bodies[0]["Isocenter"]
        assert (iso["x"], iso["y"], iso["z"]) == (10.0, 20.0, 30.0)

    def test_a_surface_shift_moves_the_isocenter_by_minus_ten_times(self):
        # 1 cm of surface shift is 10 mm of isocenter travel, in the opposite direction.
        _, bodies = build(isocenter=(0.0, 0.0, 0.0), x_shift=1, y_shift=2, z_shift=-3)
        iso = bodies[0]["Isocenter"]
        assert (iso["x"], iso["y"], iso["z"]) == (-10.0, -20.0, 30.0)


class TestMetadata:
    def test_carries_the_shifted_isocenter_rounded_and_the_buffers_and_ids(self):
        metadata, _ = build(
            isocenter=(1.234, 2.0, 3.0),
            couch_buffer=2.0,
            patient_buffer=3.0,
            surface_id="surface-1",
            room_id="room-1",
            room_scale="1.0",
        )
        assert metadata == "[1.23, 2.0, 3.0];20.0;30.0;surface-1;room-1;1.0"

    def test_splits_into_the_six_fields_handle_api_results_reads(self):
        # _apply_result does `args.split(';')` into exactly six fields; guard that shape.
        metadata, _ = build()
        assert len(metadata.split(";")) == 6


# -- GetBeamDeliveryStatus request -----------------------------------------------------------

from maprt_client import beam_entry, build_beam_delivery_status_request


class TestBeamEntry:
    def test_shapes_control_points_from_gantry_couch_pairs(self):
        assert beam_entry(3, [(0, 0), (10, 20)]) == {
            "beamNumber": 3,
            "controlPoints": [
                {"gantryAngle": 0, "couchAngle": 0, "coordinateSystem": "IEC_61217"},
                {"gantryAngle": 10, "couchAngle": 20, "coordinateSystem": "IEC_61217"},
            ],
        }


class TestBeamDeliveryStatusRequest:
    def build_bds(self, **overrides):
        kwargs = {
            "isocenter": (10.0, 20.0, 30.0),
            "couch_buffer": 2.0,
            "patient_buffer": 3.0,
            "surface_id": "surface-1",
            "room_id": "room-1",
            "patient_orientation": "HFS",
            "beam_list": [beam_entry(1, [(0, 0)])],
        }
        kwargs.update(overrides)
        return build_beam_delivery_status_request(**kwargs)

    def test_single_body_with_no_high_resolution_flag(self):
        # Unlike GetMap (a low/high pair), GetBeamDeliveryStatus is one body, no HighResolution.
        _, body = self.build_bds()
        assert isinstance(body, dict)
        assert "HighResolution" not in body

    def test_carries_the_beam_list(self):
        _, body = self.build_bds()
        assert body["BeamList"] == [beam_entry(1, [(0, 0)])]

    def test_buffers_and_isocenter_shift_match_get_map(self):
        _, body = self.build_bds(couch_buffer=2.0, patient_buffer=3.0, x_shift=1.0)
        assert body["CouchBuffer"] == 20
        assert body["PatientBuffer"] == 30
        # 1 cm surface shift -> -10 mm isocenter shift, direction reversed, as in build_map_request.
        assert body["Isocenter"]["x"] == 10.0 + 1.0 * -10

    def test_ids_and_patient_position_carried(self):
        _, body = self.build_bds(surface_id="s9", room_id="r9", patient_orientation="HFP")
        assert body["PatientSurfaceId"] == "s9"
        assert body["TreatmentRoomId"] == "r9"
        assert body["PatientPosition"] == "HFP"
