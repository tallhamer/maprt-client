"""A human-readable ``README.md`` for a recorded session, built from its files alone. No Qt.

A session folder (see :mod:`maprt_client.recording`) holds ``session.json`` and one request and
one response file per call. :func:`summarize_session` reads them back into a
:class:`SessionSummary`; :func:`render_readme` turns that into Markdown, and
:func:`write_session_readme` writes it as ``README.md`` beside them:

- what the session was (id, start, transport, app version, API), and for a Session Capture run
  what was asked for and which DICOM files were copied;
- a table per endpoint: how many calls, how many succeeded and failed, and the time they took;
- every failure, with the reason the appliance or the transport gave;
- every call, in the order it was sent, with a one-line detail of what came back.

Because it reads only the files, it works on any session: a capture run, a Settings recording
when it stops, or one recovered after a crash (``python -m maprt_client.session_readme <folder>``).
A call whose reply was never written shows as "no reply recorded". In a session pack, a body stored
once and referenced by ``body_ref`` (``sessions/<id>/<file>``, from the pack's root) is followed.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

README_NAME = "README.md"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# (path segments with "*" for a value, label), in the order the summary table lists them.
_ROUTES = [
    (("integration", "ping"), "Ping"),
    (("integration", "rooms"), "Get Rooms"),
    (("integration", "rooms", "*"), "Get Room"),
    (("integration", "patients", "*", "surfaces"), "Get Surfaces"),
    (("integration", "surfaces", "*"), "Get Surface"),
    (("integration", "GetMap"), "Get Map"),
    (("integration", "GetBeamDeliveryStatus"), "Check Beams"),
]
_LABEL_ORDER = [label for _segments, label in _ROUTES]


def endpoint_label(url):
    """The endpoint a recorded URL called, e.g. ``"Get Surface"``; its path for anything else."""
    path = unquote(urlsplit(url or "").path)
    segments = tuple(path.strip("/").split("/")) if path.strip("/") else ()
    for pattern, label in _ROUTES:
        if len(pattern) == len(segments) and all(p in ("*", s) for p, s in zip(pattern, segments)):
            return label
    return path or "?"


@dataclass
class CallSummary:
    """One recorded call: when, what, and what came back."""

    sent: str
    method: str
    endpoint: str
    target: str
    ok: bool
    result: str
    detail: str
    status: object
    elapsed_ms: object
    request_file: str


@dataclass
class SessionSummary:
    session: dict
    calls: list = field(default_factory=list)

    @property
    def failures(self):
        return [call for call in self.calls if not call.ok]


def summarize_session(session_dir):
    """Read a session folder into a :class:`SessionSummary`, calls in the order they were sent."""
    session_dir = Path(session_dir)
    session = _read_json(session_dir / "session.json") or {}
    calls = []
    for request_path in sorted(session_dir.glob("*_request.json")):
        request = _read_json(request_path)
        if request is None:
            continue
        response_path = request_path.with_name(request_path.name.replace("_request.json", "_response.json"))
        response = _read_json(response_path)
        calls.append(_summarize_call(session_dir, request_path.name, request, response))
    calls.sort(key=lambda call: call.sent)
    return SessionSummary(session=session, calls=calls)


def _summarize_call(session_dir, request_file, request, response):
    url = request.get("url") or ""
    endpoint = endpoint_label(url)
    target = _target(endpoint, url, request.get("body"))
    base = {"sent": request.get("sent") or "", "method": request.get("method") or "", "endpoint": endpoint,
            "target": target, "request_file": request_file}

    if response is None:
        return CallSummary(**base, ok=False, result="No reply recorded",
                           detail="the reply never arrived, or recording stopped first", status=None,
                           elapsed_ms=None)

    response = _resolve_body_ref(session_dir, response)
    status, elapsed = response.get("status"), response.get("elapsed_ms")
    if response.get("error"):
        return CallSummary(**base, ok=False, result="No reply", detail=response["error"], status=status,
                           elapsed_ms=elapsed)

    body = response.get("body")
    if isinstance(body, dict) and body.get("success") is False:
        code = f"{body.get('errorCodeType')}: {body.get('errorCode')}"
        http = f"HTTP {status}, " if status not in (None, 200) else ""
        return CallSummary(**base, ok=False, result="MapRT error", detail=f"{http}{code}", status=status,
                           elapsed_ms=elapsed)
    if status is not None and status >= 400:
        return CallSummary(**base, ok=False, result=f"HTTP {status}", detail=_size(response.get("body_length")),
                           status=status, elapsed_ms=elapsed)

    detail = _success_detail(session_dir, endpoint, body, response)
    return CallSummary(**base, ok=True, result="OK", detail=detail, status=status, elapsed_ms=elapsed)


def _resolve_body_ref(session_dir, response):
    """A pack's reply whose body is stored in an earlier record, with that body (or its text file)
    put back; any other reply unchanged."""
    ref = response.get("body_ref")
    if not ref or "body" in response or "body_file" in response:
        return response
    target_path = Path(session_dir).parent.parent / ref
    target = _read_json(target_path)
    if target is None:
        return response
    resolved = {**response, "body_length": target.get("body_length", response.get("body_length"))}
    if target.get("body_file"):
        resolved["body_file"] = str(target_path.parent / target["body_file"])
    else:
        resolved["body"] = target.get("body")
    return resolved


def _target(endpoint, url, body):
    """What the call was about: a room name, MRN or surface ID from the path, or a map's setup."""
    segments = unquote(urlsplit(url).path).strip("/").split("/")
    if endpoint == "Get Room":
        return f'"{segments[-1]}"'
    if endpoint == "Get Surfaces":
        return f"patient {segments[2]}"
    if endpoint == "Get Surface":
        return segments[-1]
    if endpoint in ("Get Map", "Check Beams") and isinstance(body, dict):
        parts = []
        if endpoint == "Get Map":
            parts.append("high res" if body.get("HighResolution") else "low res")
        parts.append(f"couch {_mm(body.get('CouchBuffer'))}, patient {_mm(body.get('PatientBuffer'))}")
        parts.append(f"{body.get('PatientPosition')}")
        parts.append(f"room {body.get('TreatmentRoomId')}")
        parts.append(f"surface {body.get('PatientSurfaceId')}")
        if endpoint == "Check Beams":
            parts.append(f"{len(body.get('BeamList') or [])} beams")
        return "; ".join(parts)
    return ""


def _success_detail(session_dir, endpoint, body, response):
    data = body.get("data") if isinstance(body, dict) else None
    if endpoint == "Ping":
        return f"API {data}" if data is not None else ""
    if endpoint == "Get Rooms" and isinstance(data, list):
        return _count(len(data), "room")
    if endpoint == "Get Room":
        return "no matching room (all-zeros GUID)" if data == ZERO_GUID else f"room {data}"
    if endpoint == "Get Surfaces" and isinstance(data, list):
        return _count(len(data), "surface")
    if endpoint == "Get Surface":
        return f"surface data, {_size(response.get('body_length'))}"
    if endpoint == "Get Map":
        return _map_detail(session_dir, response)
    if endpoint == "Check Beams" and isinstance(data, dict):
        statuses = data.get("statuses") or []
        unsafe = sum(1 for status in statuses if not status.get("isSafe"))
        return f"{_count(len(statuses), 'beam')}, {unsafe} unsafe"
    return _size(response.get("body_length"))


def _map_detail(session_dir, response):
    """A map's grid size from its header line (``couch,gantry``) and its size."""
    body_file = response.get("body_file")
    size = _size(response.get("body_length"))
    if not body_file:
        return size
    try:
        with open(session_dir / body_file, encoding="utf-8", errors="replace") as text:
            header = text.readline().strip()
    except OSError:
        return f"map, {size} (its .txt is missing)"
    return f"map {header.replace(',', ' × ')}, {size}"


def render_readme(summary):
    """The Markdown for a :class:`SessionSummary`."""
    session = summary.session
    calls = summary.calls
    lines = [f"# MapRT session {session.get('session', '')}".rstrip(), ""]

    ok = len(calls) - len(summary.failures)
    lines.append(f"**{_count(len(calls), 'call')}: {ok} succeeded, {len(summary.failures)} failed.**")
    lines.append("")
    about = [["Started", session.get("started")], ["Transport", session.get("transport")],
             ["App version", session.get("app_version")], ["API", session.get("api_url")],
             ["Recording format", session.get("format")]]
    lines += _table(["", ""], [row for row in about if row[1]])

    capture = session.get("capture")
    if capture:
        lines += ["", "## What was asked for", "",
                  "This session was recorded by MapApp's Session Capture (File → Export → Session Recording…).", ""]
        lines += _table(["Setting", "Value"], [[key.replace("_", " "), _value(value)] for key, value in capture.items()])

    dicom = session.get("dicom")
    if dicom:
        lines += ["", "## DICOM copied into this folder", ""]
        lines += _table(["Kind", "Files", "Folder"],
                        [[kind, str(len(files)), f"dicom/{kind}/"] for kind, files in dicom.items() if files])

    lines += ["", "## Summary by endpoint", ""]
    rows = []
    labels = sorted({call.endpoint for call in calls},
                    key=lambda label: (_LABEL_ORDER.index(label) if label in _LABEL_ORDER else len(_LABEL_ORDER), label))
    for label in labels:
        group = [call for call in calls if call.endpoint == label]
        failed = sum(1 for call in group if not call.ok)
        seconds = sum(call.elapsed_ms or 0 for call in group) / 1000
        rows.append([label, str(len(group)), str(len(group) - failed), str(failed), f"{seconds:.1f} s"])
    lines += _table(["Endpoint", "Calls", "Succeeded", "Failed", "Time"], rows) if rows else ["No calls were recorded."]

    lines += ["", "## Failures", ""]
    if summary.failures:
        lines += _table(["Sent", "Endpoint", "Target", "Result", "Detail", "Request file"],
                        [[call.sent, call.endpoint, call.target, call.result, call.detail, f"`{call.request_file}`"]
                         for call in summary.failures])
    else:
        lines.append("None.")

    lines += ["", "## Every call", ""]
    if calls:
        lines += _table(["#", "Sent", "Endpoint", "Target", "Result", "Detail", "ms"],
                        [[str(n), call.sent, call.endpoint, call.target, "✅ OK" if call.ok else f"❌ {call.result}",
                          call.detail, "" if call.elapsed_ms is None else f"{call.elapsed_ms:.0f}"]
                         for n, call in enumerate(calls, start=1)])
    else:
        lines.append("No calls were recorded.")

    lines += ["", "---", "",
              ("Written from the files in this folder. Each call's `_request.json` and `_response.json` "
               "(and `_response.txt` for a map) hold exactly what was sent and received."), ""]
    return "\n".join(lines)


def write_session_readme(session_dir):
    """Write ``README.md`` into ``session_dir`` from its files; returns its path. Raises OSError."""
    path = Path(session_dir) / README_NAME
    path.write_text(render_readme(summarize_session(session_dir)), encoding="utf-8")
    return path


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def _cell(text):
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _value(value):
    if isinstance(value, dict):
        return ", ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def _count(n, noun):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _mm(value):
    return "?" if value is None else f"{value:g} mm"


def _size(length):
    if not length:
        return "empty"
    for unit, scale in (("MB", 1_000_000), ("kB", 1_000)):
        if length >= scale:
            return f"{length / scale:.1f} {unit}"
    return f"{length} B"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None):
    """``python -m maprt_client.session_readme <session folder>...``: (re)write each README."""
    folders = sys.argv[1:] if argv is None else argv
    if not folders:
        print("usage: python -m maprt_client.session_readme <session folder>...", file=sys.stderr)
        return 2
    for folder in folders:
        print(write_session_readme(folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
