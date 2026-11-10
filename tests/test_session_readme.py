"""The session README: a human summary of a recorded session, built from its files alone.

Sessions are written by the real SessionRecorder through MapRTClient against an
httpx2.MockTransport appliance, so the README is tested on exactly the files a session holds.
"""

import datetime as dt
import json

import httpx2
import pytest

from maprt_client import MapRTApiError, MapRTClient, SessionRecorder
from maprt_client.session_readme import (
    README_NAME,
    _size,
    endpoint_label,
    main,
    render_readme,
    summarize_session,
    write_session_readme,
)

API_URL = "https://appliance.example"
ZERO = "00000000-0000-0000-0000-000000000000"
#: A complete low-resolution map: 37 couch x 36 gantry triples, then DONE.
MAP_TEXT = ("37,36\r\n" + "".join(f"{c},{g},1\r\n" for c in [*range(270, 360, 5), *range(0, 91, 5)]
                                   for g in range(0, 360, 10)) + "DONE\r\n").encode()
DUPLICATE = {"errorData": None, "success": False, "errorCode": "DuplicateKey",
             "errorCodeType": "TreatmentRoomAdministrationErrorCodes"}


def ok(data):
    return httpx2.Response(200, json={"data": data, "success": True, "errorCode": None,
                                     "errorCodeType": "NoErrorCodes"})


def appliance(request):
    path = request.url.path
    if path == "/integration/ping":
        return ok("2.0.0")
    if path == "/integration/rooms":
        return ok([{"name": "Room A", "id": "room-1", "machineName": "Truebeam", "coordinateSystem": "IEC_61217"}])
    if path == "/integration/rooms/Truebeam":
        return httpx2.Response(200, json=DUPLICATE)
    if path == "/integration/rooms/Edge":
        return ok(ZERO)
    if path == "/integration/patients/000008/surfaces":
        return ok([{"id": "surface-a", "timeStamp": "2026-10-21T22:32:51.1", "timeZone": "Mountain Standard Time"}])
    if path == "/integration/surfaces/surface-a":
        return ok("data:application/octet-stream;base64,dg==")
    if path == "/integration/GetMap":
        return httpx2.Response(200, content=MAP_TEXT)
    if path == "/integration/GetBeamDeliveryStatus":
        return ok({"statuses": [{"beamNumber": 1, "isSafe": True}, {"beamNumber": 2, "isSafe": False}]})
    if path == "/integration/boom":
        raise httpx2.ConnectError("connection refused")
    return httpx2.Response(500, text="Internal Server Error")


class Clock:
    """A clock that ticks 10 ms per reading, so every file has its own time and order."""

    def __init__(self):
        self.now = dt.datetime(2026, 10, 27, 9, 0, 0)  # noqa: DTZ001 — recordings use local time

    def __call__(self):
        self.now += dt.timedelta(milliseconds=10)
        return self.now


@pytest.fixture
def session(tmp_path):
    recorder = SessionRecorder(tmp_path, "session-1", dt.date(2026, 10, 27), clock=Clock(),
                               info={"transport": "httpx", "app_version": "0.1.0", "api_url": API_URL})
    client = MapRTClient(API_URL, "token", "VisionRT.Integration.Saturn/2.0.0",
                         transport=httpx2.MockTransport(appliance))
    client.recorder = recorder
    return client, recorder


def run_everything(client):
    client.get_status()
    client.get_treatment_rooms()
    with pytest.raises(MapRTApiError):
        client.get_treatment_room("Truebeam")
    client.get_treatment_room("Edge")
    client.get_patient_surfaces("000008")
    client.get_surface("surface-a")
    client.get_map({"CouchBuffer": 20.0, "PatientBuffer": 30.0, "HighResolution": False, "PatientPosition": "HFS",
                    "PatientSurfaceId": "surface-a", "TreatmentRoomId": "room-1"})
    client.get_beam_delivery_status({"CouchBuffer": 20.0, "PatientBuffer": 30.0, "PatientPosition": "HFS",
                                     "PatientSurfaceId": "surface-a", "TreatmentRoomId": "room-1",
                                     "BeamList": [{"beamNumber": "1"}, {"beamNumber": "2"}]})


@pytest.mark.parametrize("url, label", [
    ("https://x/integration/ping", "Ping"),
    ("/integration/rooms", "Get Rooms"),
    ("/integration/rooms/Truebeam%20", "Get Room"),
    ("/integration/patients/000008/surfaces", "Get Surfaces"),
    ("/integration/surfaces/abc", "Get Surface"),
    ("/integration/GetMap", "Get Map"),
    ("/integration/GetBeamDeliveryStatus", "Check Beams"),
    ("/integration/other/thing", "/integration/other/thing"),
])
def test_endpoint_labels(url, label):
    assert endpoint_label(url) == label


class TestSummary:
    def test_every_call_in_the_order_sent_with_what_came_back(self, session):
        client, recorder = session
        run_everything(client)
        calls = summarize_session(recorder.session_dir).calls
        assert [(c.endpoint, c.target, c.ok, c.detail) for c in calls] == [
            ("Ping", "", True, "API 2.0.0"),
            ("Get Rooms", "", True, "1 room"),
            ("Get Room", '"Truebeam"', False, "TreatmentRoomAdministrationErrorCodes: DuplicateKey"),
            ("Get Room", '"Edge"', True, "no matching room (all-zeros GUID)"),
            ("Get Surfaces", "patient 000008", True, "1 surface"),
            ("Get Surface", "surface-a", True, "surface data, 115 B"),
            ("Get Map", "low res; couch 20 mm, patient 30 mm; HFS; room room-1; surface surface-a", True,
             f"map 37 × 36, {_size(len(MAP_TEXT))}"),
            ("Check Beams", "couch 20 mm, patient 30 mm; HFS; room room-1; surface surface-a; 2 beams", True,
             "2 beams, 1 unsafe"),
        ]
        assert calls[2].result == "MapRT error"

    def test_a_transport_failure_and_an_http_error_are_failures(self, session):
        client, recorder = session
        with pytest.raises(httpx2.ConnectError):
            client._request("GET", "/integration/boom")
        with pytest.raises(MapRTApiError):
            client._request("GET", "/integration/unknown")
        failures = summarize_session(recorder.session_dir).failures
        assert [(f.result, f.detail) for f in failures] == [
            ("No reply", "ConnectError: connection refused"), ("HTTP 500", "21 B")]

    def test_a_request_with_no_reply_file_is_a_failure(self, session):
        _client, recorder = session
        recorder.record_request("GET", f"{API_URL}/integration/ping", {})
        (call,) = summarize_session(recorder.session_dir).calls
        assert (call.ok, call.result) == (False, "No reply recorded")

    def test_a_missing_map_text_is_said_rather_than_raised(self, session):
        client, recorder = session
        client.get_map({"HighResolution": True})
        next(recorder.session_dir.glob("*_response.txt")).unlink()
        (call,) = summarize_session(recorder.session_dir).calls
        assert call.detail == f"map, {_size(len(MAP_TEXT))} (its .txt is missing)"


    def test_a_pack_body_ref_is_followed(self, session):
        # In a pack a repeated body is stored once: a later reply holds only body_ref, relative to
        # the pack's root (two folders above the session).
        client, recorder = session
        client.get_surface("surface-a")
        client.get_surface("surface-a")
        client.get_map({"HighResolution": False})
        client.get_map({"HighResolution": True})
        responses = sorted(recorder.session_dir.glob("*_response.json"))
        root = recorder.session_dir.parent.parent
        for later in (responses[1], responses[3]):
            first = responses[0] if later is responses[1] else responses[2]
            record = json.loads(later.read_text(encoding="utf-8"))
            record.pop("body", None)
            if record.pop("body_file", None):
                next(recorder.session_dir.glob(later.name.replace(".json", ".txt"))).unlink()
            record["body_ref"] = first.relative_to(root).as_posix()
            later.write_text(json.dumps(record), encoding="utf-8")
        details = [call.detail for call in summarize_session(recorder.session_dir).calls]
        assert details == ["surface data, 115 B", "surface data, 115 B",
                           f"map 37 × 36, {_size(len(MAP_TEXT))}", f"map 37 × 36, {_size(len(MAP_TEXT))}"]

    def test_a_body_ref_to_nothing_is_left_as_it_is(self, session):
        client, recorder = session
        client.get_status()
        path = next(recorder.session_dir.glob("*_response.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("body")
        record["body_ref"] = "sessions/gone/x_response.json"  # audit: allow path-construction - a pack reference, "/" on every OS
        path.write_text(json.dumps(record), encoding="utf-8")
        (call,) = summarize_session(recorder.session_dir).calls
        assert call.ok and call.detail == ""


class TestReadme:
    def test_the_readme_summarizes_and_lists_the_failures(self, session):
        client, recorder = session
        run_everything(client)
        text = render_readme(summarize_session(recorder.session_dir))
        assert text.startswith("# MapRT session session-1\n")
        assert "**8 calls: 7 succeeded, 1 failed.**" in text
        assert "| Get Room | 2 | 1 | 1 |" in text
        assert "| Get Map | 1 | 1 | 0 |" in text
        failures = text.split("## Failures")[1].split("## Every call")[0]
        assert "DuplicateKey" in failures and "Edge" not in failures
        assert "| 3 | " in text and "❌ MapRT error" in text and "✅ OK" in text
        assert "| Transport | httpx |" in text and "| Recording format | maprt-recording/1 |" in text

    def test_no_failures_says_none(self, session):
        client, recorder = session
        client.get_status()
        assert "## Failures\n\nNone." in render_readme(summarize_session(recorder.session_dir))

    def test_an_empty_session(self, session):
        _client, recorder = session
        text = render_readme(summarize_session(recorder.session_dir))
        assert "**0 calls: 0 succeeded, 0 failed.**" in text
        assert "No calls were recorded." in text

    def test_capture_settings_and_dicom_are_shown_when_present(self, tmp_path):
        info = {"capture": {"calls": ["Ping", "Get Map"], "couch_buffer_cm": {"start": 0, "stop": 2, "step": 1}},
                "dicom": {"plan": ["RP.dcm"], "struct": [], "ct": ["CT1.dcm", "CT2.dcm"]}}
        recorder = SessionRecorder(tmp_path, "s", dt.date(2026, 10, 27), info=info)
        text = render_readme(summarize_session(recorder.session_dir))
        assert "| calls | Ping, Get Map |" in text
        assert "| couch buffer cm | start 0, stop 2, step 1 |" in text
        assert "| plan | 1 | dicom/plan/ |" in text and "| ct | 2 | dicom/ct/ |" in text
        assert "| struct |" not in text

    def test_a_pipe_in_a_value_does_not_break_the_table(self, session):
        client, recorder = session
        with pytest.raises(MapRTApiError):
            client.get_treatment_room("A|B")
        assert '"A\\|B"' in render_readme(summarize_session(recorder.session_dir))


class TestWriting:
    def test_write_session_readme_and_the_recorder_method(self, session):
        client, recorder = session
        client.get_status()
        path = recorder.write_readme()
        assert path == recorder.session_dir / README_NAME
        assert path.read_text(encoding="utf-8") == render_readme(summarize_session(recorder.session_dir))

    def test_the_readme_is_not_read_as_a_call(self, session):
        client, recorder = session
        client.get_status()
        write_session_readme(recorder.session_dir)
        assert len(summarize_session(recorder.session_dir).calls) == 1

    def test_the_recorder_method_never_raises(self, session, monkeypatch):
        _client, recorder = session

        def fail(_folder):
            raise PermissionError("read-only")

        monkeypatch.setattr("maprt_client.recording.write_session_readme", fail)
        assert recorder.write_readme() is None

    def test_the_command_line(self, session, capsys):
        client, recorder = session
        client.get_status()
        assert main([str(recorder.session_dir)]) == 0
        assert (recorder.session_dir / README_NAME).exists()
        assert main([]) == 2

    def test_session_json_is_unchanged(self, session):
        _client, recorder = session
        before = (recorder.session_dir / "session.json").read_text(encoding="utf-8")
        recorder.write_readme()
        assert json.loads((recorder.session_dir / "session.json").read_text(encoding="utf-8")) == json.loads(before)
