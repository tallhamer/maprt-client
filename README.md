# maprt-client

A small Python client for the Vision RT **MapRT API 2.0**: it builds the requests, sends them,
and turns the replies into plain Python and numpy values. It also checks a plan's beams against a
collision map and can record every call to a folder.

> **Not a medical device. Teaching, research and testing use only.** Not affiliated with or
> endorsed by Vision RT. A collision map or beam verdict from this library is not a clinical
> decision.

It depends only on **numpy** and **httpx2**, so it installs in seconds anywhere, including Binder
and Colab. There is no Qt and no VTK.

## Install

```console
pip install maprt-client
```

Python 3.12 or newer.

## Try it without an appliance

The [MapRT Emulator](https://github.com/tallhamer/maprt-emulator) answers the same API on your
own machine, from a built-in catalog or from sessions recorded on a real appliance:

```console
pip install maprt-emulator
python get_packs.py --release packs-v1 hfdr-phantom     # get_packs.py: see the emulator's README
maprt-emulator --packs-dir packs                        # prints the API token it accepts
```

## Use it

```python
from maprt_client import MapRTClient, MapRTApiError, build_map_request

client = MapRTClient(
    api_url="http://127.0.0.1:8000",                 # your appliance, or the emulator
    token="<the API token>",
    user_agent="VisionRT.Integration.Saturn/2.0.0",  # the API checks it
)

print(client.get_status())                           # 'HTTP Status Code: 200 OK'
rooms = client.get_treatment_rooms()                 # [(name, id, coordinate_system), ...]
surfaces = client.get_patient_surfaces("ANON-005")   # [(surface_id, timestamp), ...]

name, room_id, coordinate_system = rooms[0]
surface_id, _ = surfaces[0]

# The two GetMap bodies (low- and high-resolution) for one setup. Buffers in cm,
# isocenter in mm (IEC 61217).
label, (low_res, high_res) = build_map_request(
    isocenter=(-141.71, -41.8, -2.4),
    couch_buffer=2, patient_buffer=2,
    surface_id=surface_id, room_id=room_id, room_scale=coordinate_system,
    patient_orientation="HFDR",
)
collision_map, couch_ticks, gantry_ticks = client.get_map(high_res)
print(collision_map.shape)                           # (360, 181): gantry × couch, 1° steps
```

The examples below use the same open `client`. Call `client.close()` when you're done, or create
it with `with MapRTClient(...) as client:` to close it at the end of the block.

Every call raises `MapRTApiError` when the API reports a failure (a bad token, an unknown
patient, an unlicensed feature), with `error_code`, `error_code_type` and a coarse `category`
(`auth`, `license`, `client`, `server`) to act on:

```python
try:
    client.get_patient_surfaces("NO-SUCH-PATIENT")
except MapRTApiError as error:
    print(error.category, error.error_code)  # client PatientNotFoundInDatabase
```

## Checking beams

Two ways:

**Locally, against a collision map**, with `check_beams`. A beam is any object with `couch`,
`gantry_start`, `gantry_stop`, `gantry_rotation` (`"NONE"`, `"CW"` or `"CC"`), `delivery_type`
and a writable `status`; each beam's `status` is set to `True` (clear) or `False`:

```python
from types import SimpleNamespace
from maprt_client import check_beams

beams = [
    SimpleNamespace(couch=0, gantry_start=181, gantry_stop=179, gantry_rotation="CW",
                    delivery_type="TREATMENT", status=None),  # a full arc
    SimpleNamespace(couch=90, gantry_start=0, gantry_stop=0, gantry_rotation="NONE",
                    delivery_type="TREATMENT", status=None),  # a static beam
]
check_beams(beams, (collision_map, couch_ticks, gantry_ticks), arc_check_resolution=1)
print([beam.status for beam in beams])
```

`check_beams` also returns plain coordinates for drawing the beams over the map
(`StaticBeamPlot`, `ArcSegment`).

**On the appliance**, with GetBeamDeliveryStatus (a licensed feature):

```python
from maprt_client import beam_entry, build_beam_delivery_status_request

beam_list = [
    beam_entry(1, [(181, 0), (270, 0), (0, 0), (90, 0), (179, 0)]),  # (gantry, couch) points
    beam_entry(2, [(0, 90)]),
]
_, body = build_beam_delivery_status_request(
    isocenter=(-141.71, -41.8, -2.4), couch_buffer=2, patient_buffer=2,
    surface_id=surface_id, room_id=room_id, patient_orientation="HFDR", beam_list=beam_list,
)
print(client.get_beam_delivery_status(body))                         # [(beam_number, is_safe), ...]
```

## What's inside

| Name | What it does |
|---|---|
| `MapRTClient` | The HTTP client: `get_status`, `get_treatment_rooms`, `get_treatment_room`, `get_patient_surfaces`, `get_surface`, `get_map`, `get_beam_delivery_status`, and `send` for a raw call |
| `build_map_request`, `build_beam_delivery_status_request`, `beam_entry` | Request bodies from plain values. Optional `x_shift`/`y_shift`/`z_shift` (cm) apply a surface correction to the isocenter |
| `wire` | The reply parsers the client uses, for text you already have (the collision-map stream, rooms, surfaces, beam verdicts) |
| `check_beams` | Static beams and arcs checked against a collision map |
| `MapRTApiError`, `BeamDeliveryStatusError` | The typed errors |
| `IncompleteMapError` | Raised by `get_map` (and the `wire` parsers) when a map reply was cut off: no closing `DONE`, or fewer angle pairs than its header announces. A partial map is never returned |
| `SessionRecorder` | Set `client.recorder` to one to write every request and reply to a folder (the format the emulator's packs are built from) |

Some details that matter with a real appliance:

- **Timeouts:** 5 s to connect, and 120 s per read. The appliance can be silent for several
  seconds while it computes a beam check or a high-resolution map. Pass `timeout=` to change it.
- **TLS:** an appliance presents a self-signed certificate, so the client does not verify it.
  Use it only on the network the appliance lives on.
- **The token is a secret:** the client never logs it, and a `SessionRecorder` redacts it.
- `get_surface` returns the surface's raw `.obj` bytes. Turning them into a mesh is up to you
  (any OBJ reader works).

## Changes

* **0.1.1:** a map reply cut off before `DONE` (or missing angle pairs) raises `IncompleteMapError`
  instead of parsing into a partial array. A low-resolution map now has the same 360 × 181 shape
  as a high-resolution one (it was 185 columns wide; the four extra columns past couch 90° held no
  new information and nothing could address them). Every value at couch 270°–90° is unchanged.
* **0.1.0:** first release.

## Licence and source

MIT ([LICENSE](LICENSE)). This repository is a read-only mirror: the package is developed in the
private `sgrt-tools` monorepo and published here one release at a time, so pull requests can't be
merged here. Issues are welcome.
