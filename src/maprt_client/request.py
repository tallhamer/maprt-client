"""Building the MapRT GetMap request — no Qt, no network, no application state.

The mirror of :mod:`maprt_client.wire`: that module turns a response into plain values, this
one turns plain values into the request. It is the clinically meaningful half of a map
request — the isocenter shift applied for an interface correction, and the buffers and ids the
appliance keys the collision map on — kept in one place so the two transports cannot compute it
differently. Given explicit values (never application objects), it returns the metadata string
that tags the request and the two request bodies, low- then high-resolution.
"""


def build_map_request(isocenter, couch_buffer, patient_buffer, surface_id, room_id,
                      room_scale, patient_orientation, x_shift=0, y_shift=0, z_shift=0):
    """Return ``(metadata, [low_res_body, high_res_body])`` for a GetMap call.

    ``isocenter`` is the plan isocenter ``(x, y, z)`` in mm. The shifts are the interface
    surface corrections in cm; each is multiplied by -10 to convert to mm and reverse the
    direction (a surface shift moves the isocenter the opposite way), then added to the
    isocenter. The buffers are in cm and go to the appliance in mm.

    ``metadata`` is ``"[x, y, z];couch_mm;patient_mm;surface_id;room_id;room_scale"`` — the
    shifted isocenter rounded to 2 dp, then the values needed to label and locate the returned
    map. The transport tags the request with it so the same string comes back with the
    response and the map can be filed under a stable label. The two bodies differ only in
    ``HighResolution``; the appliance is asked for both, and the high-resolution map supersedes
    the low-resolution one under the same label.
    """
    x, y, z = isocenter

    # cm surface shift -> mm isocenter shift, direction reversed.
    x = x + x_shift * -10
    y = y + y_shift * -10
    z = z + z_shift * -10

    couch_mm = couch_buffer * 10
    patient_mm = patient_buffer * 10

    shifted_isocenter = [round(x, 2), round(y, 2), round(z, 2)]
    metadata = f"{shifted_isocenter};{couch_mm};{patient_mm};{surface_id};{room_id};{room_scale}"

    bodies = [
        {
            "CouchBuffer": couch_mm,
            "PatientBuffer": patient_mm,
            "HighResolution": high_resolution,
            "PatientPosition": patient_orientation,
            "PatientSurfaceId": f"{surface_id}",
            "TreatmentRoomId": f"{room_id}",
            "Isocenter": {
                "x": x,
                "y": y,
                "z": z,
                "CoordinateSystem": "IEC_61217",
            },
        }
        for high_resolution in (False, True)
    ]

    return metadata, bodies


def beam_entry(beam_number, gantry_couch_angles, coordinate_system="IEC_61217"):
    """One ``BeamList`` entry for a beam-delivery-status request.

    ``gantry_couch_angles`` is an iterable of ``(gantry, couch)`` pairs — one pair for a static
    beam, the swept control points for an arc. Keeps the control-point JSON shape in one place;
    turning a plan's ``BeamInfo`` into these pairs is app-side (maprt-client can't import
    rt-dicom).
    """
    return {
        "beamNumber": beam_number,
        "controlPoints": [
            {"gantryAngle": gantry, "couchAngle": couch, "coordinateSystem": coordinate_system}
            for gantry, couch in gantry_couch_angles
        ],
    }


def build_beam_delivery_status_request(isocenter, couch_buffer, patient_buffer, surface_id,
                                       room_id, patient_orientation, beam_list,
                                       x_shift=0, y_shift=0, z_shift=0):
    """Return ``(metadata, body)`` for a GetBeamDeliveryStatus call.

    The setup half — the surface-shift-corrected isocenter, the cm→mm buffers, the ids and
    patient position — is computed exactly as :func:`build_map_request` computes it, so the two
    calls describe the same physical setup. The differences are GetBeamDeliveryStatus's: no
    ``HighResolution`` flag, a single body (not the low/high pair), and a ``BeamList``.
    ``beam_list`` is already-shaped entries (see :func:`beam_entry`).
    """
    x, y, z = isocenter

    # cm surface shift -> mm isocenter shift, direction reversed — identical to build_map_request.
    x = x + x_shift * -10
    y = y + y_shift * -10
    z = z + z_shift * -10

    couch_mm = couch_buffer * 10
    patient_mm = patient_buffer * 10

    shifted_isocenter = [round(x, 2), round(y, 2), round(z, 2)]
    metadata = f"{shifted_isocenter};{couch_mm};{patient_mm};{surface_id};{room_id}"

    body = {
        "CouchBuffer": couch_mm,
        "PatientBuffer": patient_mm,
        "PatientPosition": patient_orientation,
        "PatientSurfaceId": f"{surface_id}",
        "TreatmentRoomId": f"{room_id}",
        "Isocenter": {
            "x": x,
            "y": y,
            "z": z,
            "CoordinateSystem": "IEC_61217",
        },
        "BeamList": list(beam_list),
    }

    return metadata, body
