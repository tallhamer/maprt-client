"""The shared MapRT error check: every JSON-envelope parser surfaces a reported error.

Spec §5.3: a failed call returns HTTP 200 with ``success: false`` and an ``errorCode`` /
``errorCodeType``. `raise_for_maprt_error` turns that into a typed `MapRTApiError` (a
`BeamDeliveryStatusError` for the beam endpoint), classified by `category`, so the app can
message auth / license / server / client failures instead of a blind ``data['data']`` blowing
up later with a meaningless TypeError.
"""

import json

import pytest

from maprt_client import wire
from maprt_client.wire import (
    BeamDeliveryStatusError,
    MapRTApiError,
    raise_for_maprt_error,
)


def _envelope(error_code, error_code_type):
    return json.dumps({
        "data": None,
        "success": False,
        "errorCode": error_code,
        "errorCodeType": error_code_type,
    })


class TestRaiseForMapRTError:
    def test_raises_on_success_false_carrying_the_code(self):
        with pytest.raises(MapRTApiError) as exc:
            raise_for_maprt_error(json.loads(_envelope("InvalidClientToken", "AuthErrorCodes")))
        assert exc.value.error_code == "InvalidClientToken"
        assert exc.value.error_code_type == "AuthErrorCodes"

    def test_passes_through_on_success_true(self):
        raise_for_maprt_error({"data": [], "success": True})  # must not raise

    def test_passes_through_when_success_is_absent(self):
        raise_for_maprt_error({})  # the appliance always sends success on real calls

    def test_uses_the_given_error_class(self):
        with pytest.raises(BeamDeliveryStatusError):
            raise_for_maprt_error(json.loads(_envelope("FeatureNotLicensed", "LicenseErrorCodes")),
                                  BeamDeliveryStatusError)


class TestCategory:
    @pytest.mark.parametrize("error_code_type, expected", [
        ("AuthErrorCodes", "auth"),
        ("LicenseErrorCodes", "license"),
        ("TreatmentRoomAdministrationErrorCodes", "client"),  # any other reported code
    ])
    def test_from_error_code_type(self, error_code_type, expected):
        assert MapRTApiError("code", error_code_type).category == expected

    @pytest.mark.parametrize("status, expected", [(503, "server"), (500, "server"), (400, "client"),
                                                  (404, "client")])
    def test_from_http_status_when_no_type_match(self, status, expected):
        assert MapRTApiError(None, None, http_status=status).category == expected

    def test_type_takes_precedence_over_status(self):
        assert MapRTApiError("x", "AuthErrorCodes", http_status=500).category == "auth"


class TestParsersSurfaceTheError:
    # A success:false envelope for each JSON-envelope parser raises MapRTApiError with the code,
    # rather than the old blind data['data'] TypeError/AttributeError.
    @pytest.mark.parametrize("parser", [wire.parse_rooms, wire.parse_surfaces,
                                        wire.parse_treatment_room, wire.decode_surface])
    def test_generic_parsers_raise_maprt_api_error(self, parser):
        with pytest.raises(MapRTApiError) as exc:
            parser(_envelope("FeatureNotLicensed", "LicenseErrorCodes"))
        assert exc.value.category == "license"

    def test_beam_status_raises_the_beam_subclass(self):
        with pytest.raises(BeamDeliveryStatusError) as exc:
            wire.parse_beam_delivery_status(_envelope("FeatureNotLicensed", "LicenseErrorCodes"))
        assert isinstance(exc.value, MapRTApiError)      # subclass, so general routing still matches
        assert exc.value.category == "license"


class TestRaiseForHttpStatus:
    def test_no_op_below_400(self):
        wire.raise_for_http_status(200, "anything")  # must not raise

    @pytest.mark.parametrize("status, category", [(400, "client"), (404, "client"),
                                                  (500, "server"), (503, "server")])
    def test_raises_classified_by_status_for_a_non_json_body(self, status, category):
        with pytest.raises(MapRTApiError) as exc:
            wire.raise_for_http_status(status, "<html>Bad Request</html>")
        assert exc.value.http_status == status
        assert exc.value.category == category

    def test_carries_the_error_code_from_a_json_error_body(self):
        with pytest.raises(MapRTApiError) as exc:
            wire.raise_for_http_status(400, _envelope("InvalidClientToken", "AuthErrorCodes"))
        assert exc.value.error_code == "InvalidClientToken"
        assert exc.value.category == "auth"          # type wins over the 4xx status


class TestParseMapResponse:
    def test_parses_a_streaming_map_body(self):
        # header "couch,gantry" = 2,2, then 2*2 triples, then a discarded trailing token —
        # a non-JSON body must route to parse_collision_map, not the error path.
        text = "2,2 270,180,1 270,0,1 0,180,1 0,0,1 END"
        image, _x, _y = wire.parse_map_response(text)
        assert image.shape == wire.parse_collision_map(text)[0].shape

    def test_raises_on_a_json_error_envelope_instead_of_a_stream(self):
        with pytest.raises(MapRTApiError) as exc:
            wire.parse_map_response(_envelope("FeatureNotLicensed", "LicenseErrorCodes"))
        assert exc.value.category == "license"


class TestSuccessStillParses:
    def test_rooms_success_passes_through(self):
        text = json.dumps({"data": [{"name": "R", "id": "i", "coordinateSystem": "IEC_61217"}],
                           "success": True})
        assert wire.parse_rooms(text) == [("R", "i", "IEC_61217")]

    def test_beam_status_success_passes_through(self):
        text = json.dumps({"data": {"statuses": [{"beamNumber": 1, "isSafe": True}]},
                           "success": True})
        assert wire.parse_beam_delivery_status(text) == [(1, True)]
