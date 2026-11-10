"""MapRTClient with a SessionRecorder attached: every call writes its request and reply.

Driven through httpx2.MockTransport, so no socket is opened. What's pinned is the hook, not the
recorder's own rules (test_recording.py covers those): that the recording holds what httpx
actually sent and received, that a 4xx/5xx and a transport exception are still recorded, and
that no recorder means no files.
"""

import datetime as dt
import json

import httpx2
import pytest

from maprt_client import MapRTApiError, MapRTClient, SessionRecorder
from maprt_client.http import DEFAULT_TIMEOUT, client_timeout
from maprt_client.recording import REDACTED_TOKEN

API_URL = "https://appliance.example"
TOKEN = "secret-token"
USER_AGENT = "sgrt-tools-test"
MAP_TEXT = b"0 0 1\n0 1 1\n"


def make_client(tmp_path, responder):
    client = MapRTClient(API_URL, TOKEN, USER_AGENT, transport=httpx2.MockTransport(responder))
    client.recorder = SessionRecorder(tmp_path, "session-1", dt.date(2026, 10, 22))
    return client


def session_dir(tmp_path):
    return tmp_path / "2026-10-22" / "session-1"


def records(tmp_path, kind):
    """The {request,response} records written so far, oldest first."""
    paths = sorted(session_dir(tmp_path).glob(f"*_{kind}.json"))
    return [(p, json.loads(p.read_text(encoding="utf-8"))) for p in paths]


def only(tmp_path, kind):
    found = records(tmp_path, kind)
    assert len(found) == 1, [p.name for p, _ in found]
    return found[0]


class TestRecordedCalls:
    def test_get_writes_a_request_and_a_json_response(self, tmp_path):
        client = make_client(tmp_path, lambda r: httpx2.Response(200, json={"success": True}))
        assert client.get_status() == "HTTP Status Code: 200 OK"

        path, request = only(tmp_path, "request")
        assert path.name.startswith("integration_ping_")
        assert request["method"] == "GET"
        assert request["url"] == f"{API_URL}/integration/ping"
        assert request["body"] is None

        path, response = only(tmp_path, "response")
        assert response["request_file"] == path.name.replace("_response", "_request")
        assert response["status"] == 200
        assert response["body"] == {"success": True}
        assert response["headers"]["content-type"] == "application/json"

    def test_request_headers_are_the_ones_sent_with_the_token_redacted(self, tmp_path):
        sent = []

        def responder(request):
            sent.append(request)
            return httpx2.Response(200)

        client = make_client(tmp_path, responder)
        client.get_status()

        headers = only(tmp_path, "request")[1]["headers"]
        assert headers["authorization"] == REDACTED_TOKEN
        assert headers["user-agent"] == USER_AGENT
        # Everything httpx put on the wire, not just the three the client sets.
        assert set(headers) == {k.lower() for k in sent[0].headers}
        assert TOKEN not in json.dumps(headers)

    def test_post_body_is_the_json_that_was_sent(self, tmp_path):
        sent = []

        def responder(request):
            sent.append(request)
            return httpx2.Response(200, text=MAP_TEXT.decode())

        client = make_client(tmp_path, responder)
        body = {"SurfaceId": "surface-1", "HighResolution": True, "Isocenter": [1.5, -2.0, 30.25]}
        client._request("POST", "/integration/GetMap", body)

        request = only(tmp_path, "request")[1]
        assert request["body"] == body
        assert request["body_length"] == len(sent[0].content)

    def test_map_text_reply_is_kept_byte_for_byte(self, tmp_path):
        client = make_client(tmp_path, lambda r: httpx2.Response(200, content=MAP_TEXT))
        client._request("POST", "/integration/GetMap", {"HighResolution": False})

        response = only(tmp_path, "response")[1]
        assert (session_dir(tmp_path) / response["body_file"]).read_bytes() == MAP_TEXT

    def test_patient_surfaces_endpoint_names_the_file(self, tmp_path):
        client = make_client(tmp_path, lambda r: httpx2.Response(200, json=[]))
        client._request("GET", "/integration/patients/123456789/surfaces")
        assert only(tmp_path, "request")[0].name.startswith("integration_patients_123456789_surfaces_")


    def test_send_returns_the_reply_unparsed_and_records_it(self, tmp_path):
        failure = {"success": False, "errorCode": "DuplicateKey", "errorCodeType": "X"}
        client = make_client(tmp_path, lambda r: httpx2.Response(200, json=failure))
        response = client.send("GET", "/integration/rooms/Truebeam")
        assert response.json() == failure  # a success:false envelope is not raised
        _path, record = only(tmp_path, "response")
        assert record["body"] == failure

    def test_send_still_raises_an_http_error(self, tmp_path):
        client = make_client(tmp_path, lambda r: httpx2.Response(500, text="boom"))
        with pytest.raises(MapRTApiError):
            client.send("POST", "/integration/GetMap", {"HighResolution": False})


class TestTimeout:
    def test_the_default_connects_fast_and_waits_long_for_a_reply(self):
        timeout = MapRTClient("https://x")._client.timeout
        assert (timeout.connect, timeout.write, timeout.pool) == (5.0, 5.0, 5.0)
        # The appliance took up to 8 s to answer a beam check; httpx's own 5 s dropped it.
        assert timeout.read == 120.0

    def test_a_longer_one_can_be_given(self):
        assert MapRTClient("https://x", timeout=60.0)._client.timeout == httpx2.Timeout(60.0)

    def test_client_timeout_sets_connect_write_and_pool_together(self):
        assert client_timeout(8.0, 300.0) == httpx2.Timeout(8.0, read=300.0)
        assert client_timeout() == DEFAULT_TIMEOUT == httpx2.Timeout(5.0, read=120.0)

    def test_it_can_be_changed_between_calls(self, tmp_path):
        seen = []

        def responder(request):
            seen.append(request.extensions["timeout"])
            return httpx2.Response(200, json={"success": True})

        client = make_client(tmp_path, responder)
        client.timeout = client_timeout(2.0, 30.0)
        assert client.timeout == httpx2.Timeout(2.0, read=30.0)
        client.get_status()
        assert seen[-1] == {"connect": 2.0, "read": 30.0, "write": 2.0, "pool": 2.0}


class TestFailures:
    def test_http_error_is_recorded_before_it_raises(self, tmp_path):
        envelope = {"success": False, "errorCode": "Unauthorized"}
        client = make_client(tmp_path, lambda r: httpx2.Response(401, json=envelope))
        with pytest.raises(MapRTApiError):
            client.get_treatment_rooms()

        response = only(tmp_path, "response")[1]
        assert response["status"] == 401
        assert response["body"] == envelope
        assert "error" not in response

    def test_transport_exception_writes_an_error_response_and_still_raises(self, tmp_path):
        def responder(request):
            raise httpx2.ConnectError("Connection refused", request=request)

        client = make_client(tmp_path, responder)
        with pytest.raises(httpx2.ConnectError):
            client.get_status()

        only(tmp_path, "request")
        response = only(tmp_path, "response")[1]
        assert response["status"] is None
        assert response["headers"] == {}
        assert response["error"] == "ConnectError: Connection refused"


class TestRecorderSwitch:
    def test_no_recorder_writes_nothing(self, tmp_path):
        client = MapRTClient(API_URL, TOKEN, USER_AGENT,
                             transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
        assert client.recorder is None
        client.get_status()
        assert not any(tmp_path.iterdir())

    def test_clearing_the_recorder_stops_recording(self, tmp_path):
        client = make_client(tmp_path, lambda r: httpx2.Response(200))
        client.get_status()
        client.recorder = None
        client.get_status()
        assert len(records(tmp_path, "request")) == 1

    def test_unrecorded_call_behaves_the_same(self, tmp_path):
        # The recorder only watches: the parsed result and the request on the wire are unchanged.
        sent = {}

        def responder(request):
            sent.setdefault("with" if "with" not in sent else "without", request)
            return httpx2.Response(200, json={"success": True})

        client = make_client(tmp_path, responder)
        recorded = client.get_status()
        client.recorder = None
        plain = client.get_status()
        assert recorded == plain
        assert sent["with"].headers == sent["without"].headers
        assert sent["with"].url == sent["without"].url
