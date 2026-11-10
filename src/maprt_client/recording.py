"""Session recording for the MapRT API — every request and reply written to a folder. No Qt.

A :class:`SessionRecorder` writes one file per request and one per reply, so the exact traffic
between the app and the appliance can be inspected after a session. Both transports (the app's
``QNetworkAccessManager`` one and :class:`~maprt_client.http.MapRTClient`) report what they sent
and what came back through the same two calls; the recorder owns everything else — the folder
layout, the file names, the token redaction and the JSON/text split — so the two transports
cannot write different files for the same call.

Layout: ``<folder>/<YYYY-MM-DD>/<session id>/`` holding ``session.json`` plus, per call::

    integration_ping_20261022-143512.123456_request.json
    integration_ping_20261022-143512.123456_response.json
    integration_GetMap_20261022-143513.000001_response.txt   # a non-JSON body, byte for byte

The reply takes its request's timestamp, so a pair sorts together; the time it actually arrived
is inside the file. A JSON body sits inline in the record; any other body goes to a ``.txt``
next to it, named in the record's ``body_file``.

Recording never breaks a call: once the session folder exists, a failed write is logged (the
first time) and dropped. Creating the folder is the one step that raises, so the app can tell
the user recording could not start.
"""

import datetime as dt
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from maprt_client.session_readme import write_session_readme

REDACTED_TOKEN = "Bearer ***redacted***"

# The recording format, written first in every ``session.json``. Bump the number when a reader
# written for the old layout would misread the new one; adding a field does not need a bump.
RECORDING_FORMAT = "maprt-recording/1"

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S.%f"

# Characters no filename can hold on one platform or another (Windows is the strictest), plus
# whitespace and control characters, which are legal but awkward in a file listing.
_UNSAFE = re.compile(r'[<>:"/\\|?*\s\x00-\x1f]')


def endpoint_name(url):
    """The file-name stem for a URL: its path with the slashes turned into ``_``.

    ``https://host:5000/integration/ping`` -> ``integration_ping``. The path is unquoted first,
    so a room name sent percent-encoded by one transport and in clear by the other names the
    file the same way; anything a filename can't hold then becomes ``_``.
    """
    path = unquote(urlsplit(url).path).strip("/")
    name = "_".join(_UNSAFE.sub("_", part) for part in path.split("/"))
    return name or "root"


@dataclass(frozen=True)
class RecordHandle:
    """Pairs a reply with its request: the shared file stem and when the request was sent."""

    stem: str
    sent: dt.datetime


class SessionRecorder:
    """Writes MapRT requests and replies into ``<folder>/<run date>/<session id>/``.

    ``session_id`` and ``run_date`` belong to the recording session, not to the recorder: the app
    picks them when recording is turned on and passes the same two again if it re-points the
    session to another folder (so a session that crosses midnight stays in the date folder it
    started in). ``info`` goes into ``session.json`` after :data:`RECORDING_FORMAT`, which ``info``
    can't override; the file is written once per folder. ``clock`` returns the local time; tests
    inject a fixed one.

    Raises ``OSError`` if the session folder can't be created. After that, nothing it does
    raises. It is safe to call from several threads at once.
    """

    def __init__(self, folder, session_id, run_date, info=None, clock=None):
        self.logger = logging.getLogger('maprt_client.recording.SessionRecorder')
        self._clock = clock or dt.datetime.now
        self._lock = threading.Lock()
        self._stems = set()
        self._write_failed = False

        self.session_id = str(session_id)
        self.session_dir = Path(folder) / run_date.isoformat() / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        session_file = self.session_dir / "session.json"
        if not session_file.exists():
            record = {"format": RECORDING_FORMAT, "session": self.session_id,
                      "started": self._clock().isoformat(), **(info or {})}
            record["format"] = RECORDING_FORMAT
            self._write_json(session_file, record)
        self.logger.info(f'Recording MapRT API traffic to "{self.session_dir}"')

    def record_request(self, method, url, headers, body=None):
        """Write ``<endpoint>_<ts>_request.json`` and return the handle its reply needs.

        ``headers`` is a mapping or a sequence of (name, value) pairs, as str or bytes.
        ``body`` is ``None``, a JSON-able object, or the raw bytes/str that were sent.
        """
        sent = self._clock()
        stem = self._allocate_stem(f"{endpoint_name(url)}_{sent.strftime(TIMESTAMP_FORMAT)}")
        record = {
            "session": self.session_id,
            "method": method,
            "url": url,
            "sent": sent.isoformat(),
            "headers": _header_dict(headers),
        }
        record.update(self._body_fields(f"{stem}_request", body))
        self._write_json(self.session_dir / f"{stem}_request.json", record)
        return RecordHandle(stem, sent)

    def record_response(self, handle, status, headers, body=None, error=None):
        """Write ``<stem>_response.json`` (and ``<stem>_response.txt`` for a non-JSON body).

        ``status`` may be ``None`` and ``headers`` empty when the call failed before a reply
        arrived; ``error`` (an exception or a message) then says why.
        """
        received = self._clock()
        record = {
            "session": self.session_id,
            "request_file": f"{handle.stem}_request.json",
            "received": received.isoformat(),
            "elapsed_ms": round((received - handle.sent).total_seconds() * 1000, 3),
            "status": status,
            "headers": _header_dict(headers),
        }
        record.update(self._body_fields(f"{handle.stem}_response", body))
        if error is not None:
            # Named by type: some transport exceptions (an httpx timeout) carry no message.
            record["error"] = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        self._write_json(self.session_dir / f"{handle.stem}_response.json", record)

    def write_readme(self):
        """Write the session's ``README.md`` (:mod:`maprt_client.session_readme`) from its files.

        Called when a session ends. Returns the path, or ``None`` if it couldn't be written (logged);
        like every other write here, it never raises.
        """
        try:
            path = write_session_readme(self.session_dir)
        except OSError as exc:
            self.logger.warning(f'Session README could not be written in "{self.session_dir}": {exc}')
            return None
        self.logger.info(f'Session README written to "{path}"')
        return path

    def _allocate_stem(self, stem):
        # Two calls in the same microsecond (or a stem left by an earlier recorder in this
        # folder) get _2, _3, ... — checked against disk too, since an off/on cycle starts a
        # fresh recorder on the same folder.
        with self._lock:
            candidate, n = stem, 1
            while candidate in self._stems or (self.session_dir / f"{candidate}_request.json").exists():
                n += 1
                candidate = f"{stem}_{n}"
            self._stems.add(candidate)
            return candidate

    def _body_fields(self, name, body):
        if body is None:
            return {"body": None, "body_length": 0}
        if not isinstance(body, (bytes, bytearray, str)):
            # Already an object (a request body before encoding): inline it as is.
            return {"body": body}

        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        if not raw:
            return {"body": None, "body_length": 0}
        try:
            return {"body": json.loads(raw), "body_length": len(raw)}
        except ValueError:
            # Not JSON (GetMap's map text, an HTML error page): keep it byte for byte.
            body_file = f"{name}.txt"
            self._write_bytes(self.session_dir / body_file, raw)
            return {"body_file": body_file, "body_length": len(raw)}

    def _write_json(self, path, record):
        try:
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except OSError as exc:
            self._report_write_failure(path, exc)

    def _write_bytes(self, path, data):
        try:
            path.write_bytes(data)
        except OSError as exc:
            self._report_write_failure(path, exc)

    def _report_write_failure(self, path, exc):
        if not self._write_failed:
            self._write_failed = True
            self.logger.warning(f'Session recording failed to write "{path}" (further failures not logged): {exc}')


def _header_dict(headers):
    """Headers as a {name: value} dict of str, with the token redacted.

    A repeated header is joined with ", " (how HTTP itself folds them), so no value is lost.
    """
    pairs = headers.items() if hasattr(headers, "items") else (headers or ())
    result = {}
    for name, value in pairs:
        name, value = _text(name), _text(value)
        if name.lower() == "authorization":
            value = REDACTED_TOKEN
        result[name] = f"{result[name]}, {value}" if name in result else value
    return result


def _text(value):
    # Header bytes are ISO-8859-1 by the HTTP spec, which also decodes any byte without error.
    return value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)
