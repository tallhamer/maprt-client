"""Pure parsing of MapRT API responses — no Qt, no network, no application state.

Given the text (and status) of an API response, this turns the response into plain Python
and numpy values. Both the QNetworkAccessManager transport (app-side) and the httpx
transport (:mod:`maprt_client.http`) feed their response bodies through here, so the two
cannot diverge on the part that matters — the collision-map wire format most of all, the
clinically critical parser characterized in Phase 0a. It is tested by handing it bytes
rather than by standing up a network or a Qt event loop.
"""

import base64
import json
from http.client import responses

import numpy as np


class MapRTApiError(Exception):
    """A MapRT API call that reported failure.

    Per spec §5.3 errors reach the client two ways: an HTTP 200 whose JSON envelope has
    ``success: false`` with an ``errorCode``/``errorCodeType``, and (once the transports check
    it) an HTTP 4xx/5xx status. `category` classifies the failure so the app can message each
    kind rather than surfacing a raw code:

    - ``auth`` — bad/expired token (``AuthErrorCodes``, e.g. ``InvalidClientToken``)
    - ``license`` — unlicensed feature (``LicenseErrorCodes``, e.g. ``FeatureNotLicensed``)
    - ``server`` — internal server error (HTTP 5xx)
    - ``client`` — rejected/malformed request (HTTP 4xx, or any other reported ``errorCode``)
    """

    _CATEGORY_BY_TYPE = {
        "AuthErrorCodes": "auth",
        "LicenseErrorCodes": "license",
    }

    def __init__(self, error_code, error_code_type, http_status=None):
        self.error_code = error_code
        self.error_code_type = error_code_type
        self.http_status = http_status
        message = f"{error_code_type}: {error_code}"
        if http_status is not None:
            message += f" (HTTP {http_status})"
        super().__init__(message)

    @property
    def category(self):
        by_type = self._CATEGORY_BY_TYPE.get(self.error_code_type)
        if by_type is not None:
            return by_type
        if self.http_status is not None:
            if self.http_status >= 500:
                return "server"
            if self.http_status >= 400:
                return "client"
        return "client"


def raise_for_maprt_error(envelope, error_cls=MapRTApiError):
    """Raise `error_cls` when a MapRT JSON envelope reports a negative result.

    Spec §5.3: a failed computation returns HTTP 200 with ``success: false`` and an
    ``errorCode``/``errorCodeType``. Applied by every JSON-envelope parser so the actual code is
    surfaced instead of a blind ``data['data']`` failing later with a meaningless ``TypeError``.
    A missing ``success`` key is treated as success (the appliance always sends it on real calls).
    """
    if not envelope.get("success", True):
        raise error_cls(envelope.get("errorCode"), envelope.get("errorCodeType"))


def raise_for_http_status(status_code, text):
    """Raise :class:`MapRTApiError` for an HTTP 4xx/5xx response; a no-op below 400.

    Spec §5.3: a malformed/rejected request returns an HTTP 4xx (and 5xx is an internal server
    error). When the error body is the JSON envelope its ``errorCode``/``errorCodeType`` are
    carried through (so a 4xx that also names a code classifies by it); a non-JSON body (HTML,
    empty) still yields a status-classified error.
    """
    if status_code < 400:
        return
    error_code = error_code_type = None
    try:
        envelope = json.loads(text)
        error_code = envelope.get("errorCode")
        error_code_type = envelope.get("errorCodeType")
    except (ValueError, AttributeError):
        pass  # non-JSON error body — classify by status alone
    raise MapRTApiError(error_code, error_code_type, http_status=status_code)


def ping_status(status_code):
    """The human-readable API status line shown after a ping."""
    return f'HTTP Status Code: {status_code} {responses[status_code]}'


def parse_rooms(text):
    """Treatment rooms from a Rooms response, as (name, id, coordinate_system) tuples."""
    data = json.loads(text)
    raise_for_maprt_error(data)
    return [(room['name'], room['id'], room['coordinateSystem']) for room in data['data']]


def parse_surfaces(text):
    """Patient surfaces from a Surfaces response, as (id, timestamp) tuples."""
    data = json.loads(text)
    raise_for_maprt_error(data)
    return [(surface['id'], surface['timeStamp']) for surface in data['data']]


def parse_treatment_room(text):
    """The GetSingleTreatmentRoom envelope, after checking it for a reported error.

    Returns the parsed envelope dict unchanged (its ``data`` is the matching room's GUID, or the
    all-zeros GUID when no room matches); the value is only logged today. This exists so the
    endpoint runs through the shared ``success`` check like every other JSON-envelope call.
    """
    data = json.loads(text)
    raise_for_maprt_error(data)
    return data


def decode_surface(text):
    """The raw surface bytes from a Surface response: the base64 payload, decoded.

    The value is a data URI, so the base64 is the part after the final comma.
    """
    data = json.loads(text)
    raise_for_maprt_error(data)
    base64_data = data['data'].split(',')[-1]
    return base64.b64decode(base64_data)


class BeamDeliveryStatusError(MapRTApiError):
    """A GetBeamDeliveryStatus response reporting a negative result (``success: false``).

    A subclass of :class:`MapRTApiError` so the app's existing beam-specific routing — which
    shows the ``FeatureNotLicensed`` case as a withheld verdict rather than a transport error —
    keeps matching it, while the other endpoints raise the base ``MapRTApiError``.
    """


def parse_beam_delivery_status(text):
    """Beam-delivery verdicts from a GetBeamDeliveryStatus response, as (beam_number, is_safe).

    The response is a normal JSON envelope (unlike GetMap's streaming text): ``data.statuses``
    is a list of ``{"beamNumber": N, "isSafe": bool}``. A negative result comes back as
    ``success: false`` at HTTP 200 (§5.3) — raised as :class:`BeamDeliveryStatusError` so the
    licensed-feature case is distinguishable from an empty result.
    """
    data = json.loads(text)
    raise_for_maprt_error(data, BeamDeliveryStatusError)
    statuses = data['data']['statuses']
    return [(status['beamNumber'], status['isSafe']) for status in statuses]


class IncompleteMapError(ValueError):
    """A GetMap reply that stopped early: no closing ``DONE``, or fewer angle triples than its
    header announces. A dropped connection looks like this; parsing it anyway would leave the
    missing angles as collisions without a word, so it is refused instead."""


def parse_collision_map(text):
    """The collision map from a GetMap response, as (array, x_ticks, y_ticks).

    The wire format is a whitespace-separated token stream: the first token is
    `couch_samples,gantry_samples`, then one `couch,gantry,isOK` triple per angle pair, then
    `DONE`. A reply without `DONE`, or with fewer triples than `couch_samples * gantry_samples`,
    raises :class:`IncompleteMapError` (every recorded appliance reply is complete).

    The array is always **360 × 181** at one-degree resolution (rows gantry 180° round to 179°,
    columns couch 270° through 0° to 90°), the index space the beam check works in. A coarse
    map is upsampled to it: each sample covers its own angle and the ones up to the next
    sample, and the last couch sample (90°) stops at 90°.
    """
    lst = text.split()
    if not lst or lst[-1] != 'DONE':
        raise IncompleteMapError('The GetMap reply has no closing DONE: the map is incomplete '
                                 '(the connection may have dropped).')

    # Determine the sampling for the gantry and couch axes
    couch_samples, gantry_samples = lst[0].split(',')
    expected = int(couch_samples) * int(gantry_samples)
    if len(lst) - 2 != expected:
        raise IncompleteMapError(f'The GetMap reply has {len(lst) - 2} angle pairs, not the {expected} '
                                 f'its header announces ({couch_samples} couch x {gantry_samples} gantry).')
    gantry_step_size = 360 / int(gantry_samples)
    couch_step_size = 180 / (int(couch_samples) - 1)

    # Construct csv string and read into array using numpy
    new_str = ','.join(lst[1:-1])
    a = np.fromstring(new_str, dtype=int, sep=',')
    a = a.reshape((int(len(a) / 3), 3))

    # Grab the couch and gantry position arrays along with the safe flag
    couch, gantry, isOK = a.T

    # Construct a unique set of gantry and couch positions used to construct the 2D image
    unique_couch = np.unique(couch)
    unique_gantry = np.unique(gantry)

    # Order them to match the MapRT collision map display settings
    gantry_idx = np.hstack((unique_gantry[np.where(unique_gantry >= 180)],
                            unique_gantry[np.where(unique_gantry < 180)]
                            )
                           )

    couch_idx = np.hstack((unique_couch[np.where(unique_couch >= 180)],
                           unique_couch[np.where(unique_couch <= 90)]
                           )
                          )

    # Construct mappings from gantry and couch readouts to array index positions
    x_map = dict([(str(couch_idx[i]), i) for i in range(len(couch_idx))])
    y_map = dict([(str(gantry_idx[i]), i) for i in range(len(gantry_idx))])

    # Construct the initial 2D collision map image and assign the values to each pixel
    _collision_map = np.zeros((len(gantry_idx), len(couch_idx)), dtype=int)
    for j in range(len(couch)):
        _collision_map[y_map[str(gantry[j])], x_map[str(couch[j])]] = isOK[j]

    collision_map = None

    # If the gantry and couch are low resolution upsample to get consistent indices
    if not gantry_step_size == 1 or not couch_step_size == 1:
        collision_map = np.repeat(np.repeat(_collision_map,
                                            int(gantry_step_size),
                                            axis=0
                                            ),
                                  int(couch_step_size),
                                  axis=1
                                  )
    else:
        collision_map = _collision_map

    # Repeating each sample overshoots the couch axis by one step less a degree: 37 low-resolution
    # couch samples x 5 = 185 columns, the extra 4 past couch 90. Every column at or below 90
    # (index 180) already holds its value; trim to the one-degree grid, 360 x 181.
    collision_map = collision_map[:360, :181]

    # construct couch index positions for resampled map
    c0 = np.arange(270, 360, 1)
    c1 = np.arange(0, 91, 1)
    couch_values = np.hstack((c0, c1))

    # construct gantry index positions for resampled map
    g0 = np.arange(180, 360, 1)
    g1 = np.arange(0, 180, 1)
    gantry_values = np.hstack((g0, g1))

    # construct the mappings for the tick labels for the couch and gantry axes
    x_labels = [(int(x), str(couch_values[x])) for x in np.arange(0, len(couch_values), 10)]
    x_ticks = [x_labels]

    y_labels = [(int(y), str(gantry_values[y])) for y in np.arange(0, len(gantry_values), 10)]
    y_ticks = [y_labels]

    return collision_map, x_ticks, y_ticks


def parse_map_response(text):
    """Parse a GetMap response, tolerating the error case.

    GetMap is the one endpoint whose *success* body is streaming text (a ``couch,gantry`` header,
    ``couch,gantry,isOK`` triples, then ``DONE``) rather than the JSON envelope. An error still
    arrives as the envelope, so detect a JSON body first and run it through the shared
    ``success`` check (raising :class:`MapRTApiError`); otherwise parse the stream. Both
    transports use this so they agree on the error case as well as the map format.
    """
    if text.lstrip().startswith("{"):
        raise_for_maprt_error(json.loads(text))
        # A JSON body with success:true is not a valid map; GetMap never sends that, so fall
        # through to parse_collision_map, which will fail loudly if it somehow happens.
    return parse_collision_map(text)
