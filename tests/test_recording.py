"""SessionRecorder: the folder layout, file names, redaction and JSON/text split of a session
recording. Everything here is plain files under tmp_path — no network, no Qt."""

import datetime as dt
import json
import logging
import threading

import pytest

from maprt_client import SessionRecorder
from maprt_client.recording import RECORDING_FORMAT, REDACTED_TOKEN, endpoint_name

API = "https://maprt.local:5000"
RUN_DATE = dt.date(2026, 10, 22)
SESSION_ID = "0c6a4e3e-6c35-4f43-9a25-7f6f3c1b2d10"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer s3cret-token",
    "User-Agent": "VisionRT.Integration.Saturn/1.2.8",
}
MAP_TEXT = b"0 0 1\n0 1 1\n1 0 0\n"


class FakeClock:
    """Returns 2026-10-22 14:35:12.123456, then steps forward `step` per call."""

    def __init__(self, step=dt.timedelta(milliseconds=250), start=None):
        self.now = start or dt.datetime(2026, 10, 22, 14, 35, 12, 123456)
        self.step = step

    def __call__(self):
        current = self.now
        self.now += self.step
        return current


def make_recorder(tmp_path, clock=None, info=None):
    return SessionRecorder(tmp_path, SESSION_ID, RUN_DATE, info=info, clock=clock or FakeClock())


def session_dir(tmp_path):
    return tmp_path / "2026-10-22" / SESSION_ID


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestEndpointName:
    @pytest.mark.parametrize("url, expected", [
        (f"{API}/integration/ping", "integration_ping"),
        (f"{API}/integration/rooms", "integration_rooms"),
        (f"{API}/integration/patients/123456789/surfaces", "integration_patients_123456789_surfaces"),
        (f"{API}/integration/surfaces/abc-123", "integration_surfaces_abc-123"),
        (f"{API}/integration/GetMap", "integration_GetMap"),
        (f"{API}/integration/GetBeamDeliveryStatus", "integration_GetBeamDeliveryStatus"),
        (f"{API}/", "root"),
    ])
    def test_path_slashes_become_underscores(self, url, expected):
        assert endpoint_name(url) == expected

    def test_room_name_unsafe_characters(self):
        assert endpoint_name(f"{API}/integration/rooms/TrueBeam 1: A|B*") == "integration_rooms_TrueBeam_1__A_B_"

    def test_percent_encoded_and_clear_paths_name_alike(self):
        # QNAM sends a space as %20; the name must not depend on the transport.
        assert endpoint_name(f"{API}/integration/rooms/TrueBeam%201") == endpoint_name(
            f"{API}/integration/rooms/TrueBeam 1")


class TestSessionFolder:
    def test_layout_is_folder_date_session(self, tmp_path):
        recorder = make_recorder(tmp_path)
        assert recorder.session_dir == session_dir(tmp_path)
        assert recorder.session_dir.is_dir()

    def test_session_json_holds_start_and_info(self, tmp_path):
        make_recorder(tmp_path, info={"transport": "QNAM", "api_url": API, "app_version": "0.1.0"})
        record = read_json(session_dir(tmp_path) / "session.json")
        assert record == {
            "format": "maprt-recording/1",
            "session": SESSION_ID,
            "started": "2026-10-22T14:35:12.123456",
            "transport": "QNAM",
            "api_url": API,
            "app_version": "0.1.0",
        }

    def test_format_comes_first_and_info_cannot_override_it(self, tmp_path):
        make_recorder(tmp_path, info={"format": "something-else/9"})
        record = read_json(session_dir(tmp_path) / "session.json")
        assert record["format"] == RECORDING_FORMAT == "maprt-recording/1"
        assert next(iter(record)) == "format"

    def test_off_on_reuses_folder_and_keeps_first_session_json(self, tmp_path):
        make_recorder(tmp_path, info={"transport": "QNAM"})
        later = FakeClock(start=dt.datetime(2026, 10, 22, 18, 0, 0))
        make_recorder(tmp_path, clock=later, info={"transport": "QNAM"})
        assert read_json(session_dir(tmp_path) / "session.json")["started"] == "2026-10-22T14:35:12.123456"
        assert [p.name for p in (tmp_path / "2026-10-22").iterdir()] == [SESSION_ID]

    def test_run_crossing_midnight_stays_in_its_start_date(self, tmp_path):
        after_midnight = FakeClock(start=dt.datetime(2026, 10, 23, 0, 5, 0))
        recorder = make_recorder(tmp_path, clock=after_midnight)
        recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        assert (session_dir(tmp_path) / "integration_ping_20261023-000500.250000_request.json").exists()

    def test_uncreatable_folder_raises(self, tmp_path):
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(OSError):
            SessionRecorder(blocker, SESSION_ID, RUN_DATE)


class TestRequest:
    def test_get_request_record(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        assert handle.stem == "integration_ping_20261022-143512.373456"
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_request.json")
        assert record == {
            "session": SESSION_ID,
            "method": "GET",
            "url": f"{API}/integration/ping",
            "sent": "2026-10-22T14:35:12.373456",
            "headers": {**HEADERS, "Authorization": REDACTED_TOKEN},
            "body": None,
            "body_length": 0,
        }

    def test_post_json_object_body_is_inline(self, tmp_path):
        body = {"SurfaceId": "abc", "HighResolution": True, "Isocenter": [1.5, -2.0, 30.25]}
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, body)
        assert read_json(session_dir(tmp_path) / f"{handle.stem}_request.json")["body"] == body

    def test_post_encoded_json_body_is_inline(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, b'{"a": 1}')
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_request.json")
        assert record["body"] == {"a": 1}
        assert record["body_length"] == 8

    @pytest.mark.parametrize("name", ["Authorization", "authorization", "AUTHORIZATION"])
    def test_token_redacted_in_any_casing(self, tmp_path, name):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", {name: "Bearer s3cret-token"})
        text = (session_dir(tmp_path) / f"{handle.stem}_request.json").read_text(encoding="utf-8")
        assert "s3cret-token" not in text
        assert json.loads(text)["headers"] == {name: REDACTED_TOKEN}

    def test_header_pairs_as_bytes(self, tmp_path):
        # QNAM hands back headers as (bytes, bytes) pairs.
        pairs = [(b"Content-Type", b"application/json"), (b"Authorization", b"Bearer s3cret-token")]
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", pairs)
        headers = read_json(session_dir(tmp_path) / f"{handle.stem}_request.json")["headers"]
        assert headers == {"Content-Type": "application/json", "Authorization": REDACTED_TOKEN}

    def test_repeated_header_values_are_joined(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", [("Accept", "a"), ("Accept", "b")])
        assert read_json(session_dir(tmp_path) / f"{handle.stem}_request.json")["headers"] == {"Accept": "a, b"}


class TestResponse:
    def test_json_reply_is_inline_and_paired(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/rooms", HEADERS)
        reply = b'{"success": true, "rooms": [{"name": "TB1"}]}'
        recorder.record_response(handle, 200, {"Content-Type": "application/json"}, reply)

        path = session_dir(tmp_path) / f"{handle.stem}_response.json"
        assert path.name == "integration_rooms_20261022-143512.373456_response.json"
        assert read_json(path) == {
            "session": SESSION_ID,
            "request_file": f"{handle.stem}_request.json",
            "received": "2026-10-22T14:35:12.623456",
            "elapsed_ms": 250.0,
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": {"success": True, "rooms": [{"name": "TB1"}]},
            "body_length": len(reply),
        }

    def test_text_reply_goes_to_txt_byte_for_byte(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, {"HighResolution": True})
        recorder.record_response(handle, 200, {"Content-Type": "text/plain"}, MAP_TEXT)

        record = read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")
        assert "body" not in record
        assert record["body_file"] == f"{handle.stem}_response.txt"
        assert record["body_length"] == len(MAP_TEXT)
        assert (session_dir(tmp_path) / record["body_file"]).read_bytes() == MAP_TEXT

    def test_text_reply_keeps_crlf_and_non_utf8_bytes(self, tmp_path):
        raw = b"0 0 1\r\n\xff\xfe 1 0\r\n"
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, {})
        recorder.record_response(handle, 200, {}, raw)
        assert (session_dir(tmp_path) / f"{handle.stem}_response.txt").read_bytes() == raw

    def test_empty_reply_body(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        recorder.record_response(handle, 200, {}, b"")
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")
        assert record["body"] is None and record["body_length"] == 0
        assert not list(session_dir(tmp_path).glob("*.txt"))

    def test_error_envelope_is_recorded_like_any_json_reply(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/rooms", HEADERS)
        recorder.record_response(handle, 401, {}, b'{"success": false, "errorCode": "Unauthorized"}')
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")
        assert record["status"] == 401
        assert record["body"] == {"success": False, "errorCode": "Unauthorized"}

    def test_failed_call_writes_an_error_response(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        recorder.record_response(handle, None, None, None, error=ConnectionError("Connection refused"))
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")
        assert record["status"] is None
        assert record["headers"] == {}
        assert record["body"] is None
        assert record["error"] == "ConnectionError: Connection refused"

    def test_error_given_as_a_message(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        recorder.record_response(handle, 404, {}, b"", error="Error transferring ... - server replied: Not Found")
        record = read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")
        assert record["error"] == "Error transferring ... - server replied: Not Found"

    def test_no_error_key_on_success(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
        recorder.record_response(handle, 200, {}, b"{}")
        assert "error" not in read_json(session_dir(tmp_path) / f"{handle.stem}_response.json")


class TestNaming:
    def test_getmap_pair_in_the_same_instant_gets_a_suffix(self, tmp_path):
        recorder = make_recorder(tmp_path, clock=FakeClock(step=dt.timedelta(0)))
        low = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, {"HighResolution": False})
        high = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, {"HighResolution": True})
        assert low.stem == "integration_GetMap_20261022-143512.123456"
        assert high.stem == "integration_GetMap_20261022-143512.123456_2"
        recorder.record_response(high, 200, {}, MAP_TEXT)
        recorder.record_response(low, 200, {}, MAP_TEXT)
        names = sorted(p.name for p in session_dir(tmp_path).iterdir() if p.name != "session.json")
        assert names == [
            "integration_GetMap_20261022-143512.123456_2_request.json",
            "integration_GetMap_20261022-143512.123456_2_response.json",
            "integration_GetMap_20261022-143512.123456_2_response.txt",
            "integration_GetMap_20261022-143512.123456_request.json",
            "integration_GetMap_20261022-143512.123456_response.json",
            "integration_GetMap_20261022-143512.123456_response.txt",
        ]

    def test_clash_with_a_file_left_by_an_earlier_recorder(self, tmp_path):
        first = make_recorder(tmp_path, clock=FakeClock(step=dt.timedelta(0)))
        first.record_request("GET", f"{API}/integration/ping", HEADERS)
        second = make_recorder(tmp_path, clock=FakeClock(step=dt.timedelta(0)))
        assert second.record_request("GET", f"{API}/integration/ping", HEADERS).stem.endswith("_2")

    def test_patient_id_in_the_name(self, tmp_path):
        recorder = make_recorder(tmp_path)
        handle = recorder.record_request("GET", f"{API}/integration/patients/123456789/surfaces", HEADERS)
        assert handle.stem.startswith("integration_patients_123456789_surfaces_")


class TestRobustness:
    def test_concurrent_calls_get_unique_files(self, tmp_path):
        recorder = make_recorder(tmp_path, clock=FakeClock(step=dt.timedelta(0)))
        handles, lock = [], threading.Lock()

        def call():
            for _ in range(20):
                handle = recorder.record_request("POST", f"{API}/integration/GetMap", HEADERS, {})
                recorder.record_response(handle, 200, {}, MAP_TEXT)
                with lock:
                    handles.append(handle)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({h.stem for h in handles}) == 160
        assert len(list(session_dir(tmp_path).glob("*_request.json"))) == 160
        assert len(list(session_dir(tmp_path).glob("*_response.txt"))) == 160

    def test_write_failure_is_logged_once_and_never_raised(self, tmp_path, caplog):
        recorder = make_recorder(tmp_path)
        # The folder vanishing under the recorder (a removed drive, a user tidying up).
        for path in session_dir(tmp_path).iterdir():
            path.unlink()
        session_dir(tmp_path).rmdir()

        with caplog.at_level(logging.WARNING, logger="maprt_client.recording.SessionRecorder"):
            handle = recorder.record_request("GET", f"{API}/integration/ping", HEADERS)
            recorder.record_response(handle, 200, {}, MAP_TEXT)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Session recording failed to write" in warnings[0].getMessage()
