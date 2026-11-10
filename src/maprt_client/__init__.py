"""maprt-client: MapRT API wire parsing, request building, and the httpx2 transport. No Qt, no VTK.

The .obj surface decode (``MapRTSurface``), which needs VTK, is in the ``maprt-surface`` package.
"""

from maprt_client import wire
from maprt_client.beams import ArcSegment, StaticBeamPlot, check_beams
from maprt_client.http import MapRTClient
from maprt_client.recording import SessionRecorder
from maprt_client.request import beam_entry, build_beam_delivery_status_request, build_map_request
from maprt_client.wire import BeamDeliveryStatusError, MapRTApiError, raise_for_maprt_error

__all__ = [
    "wire",
    "MapRTClient",
    "SessionRecorder",
    "build_map_request",
    "build_beam_delivery_status_request",
    "beam_entry",
    "BeamDeliveryStatusError",
    "MapRTApiError",
    "raise_for_maprt_error",
    "check_beams",
    "ArcSegment",
    "StaticBeamPlot",
]
