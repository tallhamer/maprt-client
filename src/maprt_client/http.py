"""httpx2 transport for the MapRT API — Qt-free, importable headless.

Mirrors the request surface of the ``QNetworkAccessManager`` transport in the app's
``models.maprt`` — the same endpoints, the same headers, the same disabled peer verification
the appliance's self-signed certificate needs — but it is synchronous and returns the parsed
value directly instead of dispatching it through a Qt signal. Both transports feed their
response bodies through :mod:`maprt_client.wire`, so the clinically critical parsing cannot
diverge between them.

Being free of Qt, this path can be driven from a plain script, a notebook, or a batch QA job
with no event loop. The app threads it (``QThreadPool``) so the GUI stays responsive and
selects the transport at launch.
"""

import logging

import httpx2

from maprt_client import wire

# A wrong host or port still fails fast, but a reply may be long coming: the appliance is silent
# while it checks beams (4–8 s recorded) and sometimes before a high-res map's first byte (over
# 6 s), and the emulator sends nothing until it has computed the whole map.
DEFAULT_CONNECT_TIMEOUT = 5.0  # seconds
DEFAULT_READ_TIMEOUT = 120.0  # seconds


def client_timeout(connect=DEFAULT_CONNECT_TIMEOUT, read=DEFAULT_READ_TIMEOUT):
    """The client's timeout: ``connect`` seconds to connect, write or get a pooled connection,
    ``read`` seconds to wait for each read (between bytes, not for the whole reply)."""
    return httpx2.Timeout(connect, read=read)


DEFAULT_TIMEOUT = client_timeout()


class MapRTClient:
    """Synchronous MapRT API client over httpx2.

    ``api_url``, ``token`` and ``user_agent`` are read fresh on every request, so the app can
    update them (from the settings dialog) between calls without rebuilding the client. Pass
    ``transport`` to inject an :class:`httpx2.MockTransport` in tests; when it is given httpx
    ignores ``verify``.

    Set ``recorder`` to a :class:`~maprt_client.recording.SessionRecorder` to write every
    request and reply to a session folder; ``None`` (the default) records nothing.

    ``timeout`` (seconds, or an :class:`httpx2.Timeout`) defaults to :data:`DEFAULT_TIMEOUT`: 5 s to
    connect, write or get a pooled connection, and 120 s to wait for each read. httpx's own 5 s
    read limit dropped beam checks and maps the appliance was still computing.
    """

    def __init__(self, api_url=None, token=None, user_agent=None, transport=None, timeout=None):
        self.logger = logging.getLogger('maprt_client.http.MapRTClient')
        self.logger.debug('Initializing attributes for MapRTClient object')

        self._api_url = api_url
        self._token = token
        self._user_agent = user_agent

        # verify=False mirrors the QNetworkAccessManager transport's PeerVerifyMode.VerifyNone:
        # the MapRT appliance presents a self-signed certificate. A MockTransport supersedes it
        # in tests.
        self._client = httpx2.Client(verify=False, transport=transport,
                                    timeout=DEFAULT_TIMEOUT if timeout is None else timeout)
        self.recorder = None

    @property
    def timeout(self):
        """The :class:`httpx2.Timeout` every request uses; settable between calls (seconds, or a
        Timeout), as the app's settings dialog does."""
        return self._client.timeout

    @timeout.setter
    def timeout(self, value):
        self.logger.debug(f'Setting timeout to {value} in MapRTClient')
        self._client.timeout = value

    @property
    def api_url(self):
        return self._api_url

    @api_url.setter
    def api_url(self, value):
        self.logger.debug(f'Setting api_url to {value} in MapRTClient')
        self._api_url = str(value)

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        # The token is a secret; log that it changed, never the value.
        self.logger.debug('Setting token in MapRTClient')
        self._token = str(value)

    @property
    def user_agent(self):
        return self._user_agent

    @user_agent.setter
    def user_agent(self, value):
        self.logger.debug(f'Setting user_agent to {value} in MapRTClient')
        self._user_agent = str(value)

    @property
    def headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
        }

    def _request(self, method, endpoint, body=None):
        # Single point where a request is built and sent, mirroring MapRTAPIManager._request.
        # Every call shares the same headers; the verb, path and (for POST) JSON body vary.
        url = self._api_url + endpoint
        self.logger.debug(f'{method} call made to the "{endpoint}" API endpoint in MapRTClient')
        if body is None:
            request = self._client.build_request(method, url, headers=self.headers)
        else:
            request = self._client.build_request(method, url, headers=self.headers, json=body)

        # Read once: the worker thread may see the app swap or clear it mid-call.
        recorder = self.recorder
        if recorder is None:
            response = self._client.send(request)
        else:
            # Built first so the recording holds exactly the headers and body bytes httpx sends.
            handle = recorder.record_request(method, url, request.headers.multi_items(), request.content)
            try:
                response = self._client.send(request)
            except Exception as exc:
                recorder.record_response(handle, None, None, None, error=exc)
                raise
            recorder.record_response(handle, response.status_code, response.headers.multi_items(),
                                     response.content)
        # Turn an HTTP 4xx/5xx into a typed MapRTApiError here — the QNAM transport gets this for
        # free from QNetworkReply.error(), so both surface HTTP failures the same way instead of
        # feeding an error body to the parsers. text is read only on the error path.
        if response.status_code >= 400:
            wire.raise_for_http_status(response.status_code, response.text)
        return response

    def send(self, method, endpoint, body=None):
        """One call, unparsed: the :class:`httpx2.Response`, recorded like every other call.

        For callers that want the reply as it came (a session capture), not a parsed value. An
        HTTP 4xx/5xx still raises :class:`~maprt_client.wire.MapRTApiError`; a ``success: false``
        envelope does not, since nothing here parses the body.
        """
        return self._request(method, endpoint, body)

    def get_status(self):
        response = self._request("GET", "/integration/ping")
        return wire.ping_status(response.status_code)

    def get_treatment_rooms(self):
        response = self._request("GET", "/integration/rooms")
        return wire.parse_rooms(response.text)

    def get_treatment_room(self, room_name):
        response = self._request("GET", f"/integration/rooms/{room_name}")
        return wire.parse_treatment_room(response.text)

    def get_patient_surfaces(self, patient_id):
        response = self._request("GET", f"/integration/patients/{patient_id}/surfaces")
        return wire.parse_surfaces(response.text)

    def get_surface(self, surface_id):
        response = self._request("GET", f"/integration/surfaces/{surface_id}")
        return wire.decode_surface(response.text)

    def get_map(self, body):
        # One request, one map. The QNAM transport builds the isocenter-shifted body and fires
        # this twice (low- and high-resolution); that orchestration is app logic and stays with
        # the caller, so at the transport level a map request takes a ready body.
        response = self._request("POST", "/integration/GetMap", body)
        return wire.parse_map_response(response.text)

    def get_beam_delivery_status(self, body):
        # Server-side assessment of whether the beams in `body`'s BeamList can be safely
        # delivered. `body` comes ready from request.build_beam_delivery_status_request. A
        # negative/unlicensed result surfaces as wire.BeamDeliveryStatusError, not an HTTP error.
        response = self._request("POST", "/integration/GetBeamDeliveryStatus", body)
        return wire.parse_beam_delivery_status(response.text)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
