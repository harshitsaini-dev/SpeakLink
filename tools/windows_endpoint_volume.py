"""Windows Core Audio master volume and mute for ONE named endpoint.

WHAT THIS CHANGES ABOUT SPEAKLINK

Until now the Receiver scaled its own decoded PCM and never touched the system
mixer. That had a real safety argument - nothing else on the machine changed,
and a crash could not leave a shop muted - but it also meant a Store user who
muted Windows silenced every announcement while HQ reported "Applied 100%".

This module is the accepted reversal of that decision. HQ's per-Store volume
now sets the WINDOWS ENDPOINT MASTER, so it moves the same slider the Store
user sees in the taskbar. The consequences are accepted and are stated here
rather than discovered later:

  * every other application on that endpoint is affected. Chrome, LinkGuard,
    anything sharing the output gets louder, quieter or muted with SpeakLink.
  * the change is persistent Windows state. It outlives the process, so it
    must be captured before the first mutation and restored afterwards, and a
    crash needs a recovery record. See windows_endpoint_restore.py.
  * it is still not the amplifier. Nothing here controls a physical knob, and
    no result from this module may be read as acoustic evidence.

IDENTITY IS THE DANGEROUS PART

A PortAudio index is NOT an endpoint identity. The same physical output appears
under several host APIs with different indices, and indices renumber when any
device is plugged or removed. Setting a master volume from an index means one
new USB headset can point SpeakLink at the wrong output and mute a shop's till
audio instead of its speakers.

So every mutation here requires a STABLE MMDevice endpoint id - the
``{0.0.0.00000000}.{guid}`` string Windows itself uses - captured once when the
technician selected the device. Nothing in this module will act on a name, an
index, or a default. If the recorded id is not present on the machine, the
operation is refused rather than redirected.
"""

from __future__ import annotations

from dataclasses import dataclass


class EndpointControlError(RuntimeError):
    """Base class, so no caller handles one refusal and misses another."""


class EndpointControlUnavailable(EndpointControlError):
    """Core Audio itself could not be reached.

    A Receiver build without pycaw/comtypes, or a machine where COM refuses.
    Distinct from "the endpoint is missing": one means SpeakLink cannot control
    any endpoint here, the other means it cannot control THIS one.
    """


class EndpointNotFound(EndpointControlError):
    """The recorded endpoint id is not present on this machine.

    Raised rather than falling back to the default device. Falling back is the
    single most dangerous thing this module could do: it would silently move
    the master volume of whatever output happened to be default, on a computer
    nobody is watching.
    """

    def __init__(self, endpoint_id: str) -> None:
        super().__init__(
            "The Windows audio output SpeakLink was configured to control is no "
            "longer present on this computer. No other output has been "
            "changed. Re-select the audio output in Store Setup."
        )
        self.endpoint_id = endpoint_id


class EndpointAmbiguous(EndpointControlError):
    """A playback device could not be matched to exactly one endpoint.

    Only ever raised while SELECTING a device, never while controlling one.
    Two endpoints with the same friendly name cannot be told apart from
    PortAudio's side, and guessing would attach master control to the wrong
    hardware for the life of the installation.
    """


@dataclass(frozen=True, slots=True)
class EndpointState:
    """What an endpoint's master control looked like at one moment."""

    endpoint_id: str
    #: 0-100 whole percent. Windows stores a float scalar; the product speaks
    #: percent, and rounding once here keeps every layer above it in one unit.
    volume_percent: int
    muted: bool

    def as_dict(self) -> dict:
        return {"endpoint_id": self.endpoint_id,
                "volume_percent": self.volume_percent,
                "muted": self.muted}


def _core_audio():
    """pycaw + comtypes, or an honest refusal.

    Imported lazily so that a Receiver running on a machine without them - or
    any non-Windows test host - fails at the point of use with a message about
    audio control, rather than failing to start at all.
    """
    try:
        from comtypes import CLSCTX_ALL, POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as failure:  # pragma: no cover - import shape varies
        raise EndpointControlUnavailable(
            "Windows Core Audio is unavailable in this Receiver build, so the "
            "output's master volume cannot be read or changed."
        ) from failure
    return AudioUtilities, IAudioEndpointVolume, CLSCTX_ALL, POINTER, cast


def list_endpoints(*, backend=None) -> list[dict]:
    """Every ACTIVE output endpoint, with the id used for control.

    Inactive, unplugged and not-present endpoints are excluded: offering a
    technician a device that is not there produces a configuration that cannot
    work and a support call that starts a week later.
    """
    if backend is not None:
        return backend.list_endpoints()
    AudioUtilities, _, _, _, _ = _core_audio()
    endpoints = []
    for device in AudioUtilities.GetAllDevices():
        state = str(getattr(device, "state", ""))
        if "Active" not in state:
            continue
        endpoints.append({"endpoint_id": str(device.id),
                          "name": str(device.FriendlyName)})
    return endpoints


def resolve_endpoint_for_playback_device(playback_name: str, *,
                                         backend=None) -> str:
    """Map a selected PortAudio device to exactly ONE endpoint id.

    Run once, while the technician is choosing the output and can see what
    happened - never during a broadcast. The match is on the endpoint's
    friendly name, which is the only thing the two APIs share, and it is
    required to be UNIQUE. Two endpoints with the same name is an ambiguity
    this refuses rather than resolves, because a wrong answer here is
    permanent and silent.
    """
    wanted = _normalise(playback_name)
    if not wanted:
        raise EndpointAmbiguous("The selected playback device has no name to match on.")

    matches = [e for e in list_endpoints(backend=backend)
               if _normalise(e["name"]) == wanted]
    if len(matches) == 1:
        return matches[0]["endpoint_id"]

    if not matches:
        # PortAudio decorates names ("Speakers (2- Logi USB Headset H340)")
        # and truncates them at 31 characters under MME, so an exact match can
        # legitimately fail. Containment is tried second, and still has to be
        # unique.
        matches = [e for e in list_endpoints(backend=backend)
                   if _normalise(e["name"]) in wanted or wanted in _normalise(e["name"])]
    if len(matches) == 1:
        return matches[0]["endpoint_id"]
    if not matches:
        raise EndpointAmbiguous(
            f"No Windows audio endpoint matches the selected output "
            f"'{playback_name}'. Choose the output again in Store Setup.")
    raise EndpointAmbiguous(
        f"{len(matches)} Windows audio endpoints match '{playback_name}', so "
        "SpeakLink cannot tell which one to control. Rename one of them in "
        "Windows Sound settings, or select a different output.")


def _normalise(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _endpoint_volume(endpoint_id: str, *, backend=None):
    """The IAudioEndpointVolume for exactly this id, or EndpointNotFound."""
    if backend is not None:
        return backend.controller(endpoint_id)
    AudioUtilities, IAudioEndpointVolume, CLSCTX_ALL, POINTER, cast = _core_audio()
    for device in AudioUtilities.GetAllDevices():
        if str(device.id) != endpoint_id:
            continue
        if "Active" not in str(getattr(device, "state", "")):
            # Present but unusable - treated as missing rather than forced.
            raise EndpointNotFound(endpoint_id)
        interface = device._dev.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))
    raise EndpointNotFound(endpoint_id)


def read_state(endpoint_id: str, *, backend=None) -> EndpointState:
    """What this endpoint's master control is right now."""
    control = _endpoint_volume(endpoint_id, backend=backend)
    scalar = float(control.GetMasterVolumeLevelScalar())
    return EndpointState(
        endpoint_id=endpoint_id,
        volume_percent=max(0, min(100, round(scalar * 100))),
        muted=bool(control.GetMute()),
    )


def apply_state(endpoint_id: str, *, volume_percent: int | None = None,
                muted: bool | None = None, backend=None) -> EndpointState:
    """Set master volume and/or mute, then READ BACK what actually happened.

    The read-back is the point. Windows can clamp, a policy can override, and
    a device can vanish between the set and the next instant - so "applied"
    means the endpoint reported the value afterwards, not that a call
    returned without raising. Everything above this reports the value that
    comes back from here, never the one it asked for.
    """
    if volume_percent is not None:
        if isinstance(volume_percent, bool) or not isinstance(volume_percent, int):
            raise EndpointControlError("volume_percent must be a whole number")
        if not 0 <= volume_percent <= 100:
            raise EndpointControlError("volume_percent must be between 0 and 100")
    if muted is not None and not isinstance(muted, bool):
        raise EndpointControlError("muted must be true or false")

    control = _endpoint_volume(endpoint_id, backend=backend)
    if volume_percent is not None:
        control.SetMasterVolumeLevelScalar(volume_percent / 100.0, None)
    if muted is not None:
        control.SetMute(bool(muted), None)
    return read_state(endpoint_id, backend=backend)
