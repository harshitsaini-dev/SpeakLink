"""Watch ONE Windows endpoint and report what it is actually doing.

WHY THIS EXISTS

HQ could set a Store's Windows master volume, but not see it. If the person at
the till moved the Windows slider from 80% to 25%, the shop really did change
and the Console went on displaying 80% - confidently wrong about the only
number an operator was looking at.

Core Audio already broadcasts this. ``IAudioEndpointVolumeCallback`` fires for
every master volume or mute change on an endpoint, whoever caused it: EchoCast,
the taskbar, the keyboard volume keys, another application, Sound Settings.
Verified firing against a real endpoint before this module was written, so the
choice of notifications over polling is measured rather than assumed - polling
would have to run fast to feel live and would then spend all day asking a
question whose answer almost never changes.

TWO THINGS THAT ARE NOT THE SAME

    the ORIGINAL pre-broadcast state  - the restoration authority
    the CURRENT endpoint state        - what HQ should display

This module only ever produces the second. Nothing here may touch the
restoration snapshot: a Store user turning the volume down mid-announcement
must not change what gets put back at the end.

THREADING

The callback arrives on a COM thread, not the asyncio loop. It therefore does
the least possible work - records the value, sets an event - and the async side
drains it. Doing real work in a COM callback is how an audio driver ends up
waiting on a WebSocket.
"""

from __future__ import annotations

import gc

import threading
from dataclasses import dataclass


#: Coalescing window for local changes.
#:
#: A Windows slider drag emits a notification per step, so 30->55 can produce
#: a dozen in under a second. 150 ms sits inside the 120 ms cadence the HQ
#: control path already uses, so the two rhythms do not beat against each
#: other, and it is comfortably below what a person reads as lag.
COALESCE_SECONDS = 0.15


class ObserverUnavailable(RuntimeError):
    """Endpoint change notifications cannot be used on this machine.

    A Receiver that cannot observe is still perfectly able to CONTROL the
    endpoint - it simply cannot tell HQ when somebody else changes it. That is
    a degradation worth reporting, not a reason to refuse to broadcast.
    """


@dataclass(frozen=True, slots=True)
class EndpointReading:
    """One observation of what the endpoint is doing now."""

    volume_percent: int
    muted: bool
    #: Monotonic per observer. HQ discards anything older than what it has, so
    #: a delayed notification cannot drag the Console backwards.
    sequence: int


class EndpointObserver:
    """Reports master volume/mute changes for one stable endpoint id.

    Deliberately NOT given the ability to find an endpoint by name or to fall
    back to the default device. It observes exactly the id it was constructed
    with, or it fails - the same rule the control path follows, for the same
    reason: acting on the wrong output is worse than not acting.
    """

    def __init__(self, endpoint_id: str, *, backend=None) -> None:
        self.endpoint_id = endpoint_id
        self._backend = backend
        self._lock = threading.Lock()
        self._sequence = 0
        self._pending: EndpointReading | None = None
        self._changed = threading.Event()
        self._registration = None
        self._control = None
        self._started = False

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Attach to the endpoint. Raises ObserverUnavailable if it cannot."""
        if self._started:
            return
        if self._backend is not None:
            self._backend.register(self.endpoint_id, self._on_change)
            self._started = True
            return

        try:
            from comtypes import CLSCTX_ALL, POINTER, cast
            from pycaw.callbacks import AudioEndpointVolumeCallback
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except Exception as failure:  # pragma: no cover - import shape varies
            raise ObserverUnavailable(
                "Windows Core Audio change notifications are unavailable in "
                "this Receiver build.") from failure

        device = None
        for candidate in AudioUtilities.GetAllDevices():
            if str(candidate.id) == self.endpoint_id:
                device = candidate
                break
        if device is None:
            raise ObserverUnavailable(
                "The configured Windows audio output is not present, so its "
                "volume cannot be watched.")

        observer = self

        class _Callback(AudioEndpointVolumeCallback):
            def on_notify(self, new_volume, new_mute, event_context,
                          channels, channel_volumes):
                # COM thread. Record and return - nothing that can block.
                observer._on_change(
                    max(0, min(100, round(float(new_volume) * 100))),
                    bool(new_mute))

        try:
            self._control = cast(
                device._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                POINTER(IAudioEndpointVolume))
            self._registration = _Callback()
            self._control.RegisterControlChangeNotify(self._registration)
        except Exception as failure:
            self._registration = None
            self._control = None
            raise ObserverUnavailable(
                "Windows refused to report changes for the configured audio "
                "output.") from failure
        self._started = True

    def stop(self) -> None:
        """Detach. Safe to call twice, and never raises.

        Unregistering matters: a COM callback left attached after a broadcast
        would keep a dead session's object alive and could report into the
        next one.
        """
        if self._backend is not None:
            try:
                self._backend.unregister(self.endpoint_id)
            except Exception:
                pass
        elif self._control is not None and self._registration is not None:
            try:
                self._control.UnregisterControlChangeNotify(self._registration)
            except Exception:
                pass
        self._registration = None
        self._control = None
        self._started = False
        self._pending = None
        self._changed.set()          # release anybody waiting

    @property
    def started(self) -> bool:
        return self._started

    # ---- the callback ------------------------------------------------------
    def _on_change(self, volume_percent: int, muted: bool) -> None:
        """Called from the COM thread, and from tests directly."""
        with self._lock:
            self._sequence += 1
            self._pending = EndpointReading(
                volume_percent=volume_percent, muted=muted,
                sequence=self._sequence)
        self._changed.set()

    # ---- draining ----------------------------------------------------------
    def take(self) -> EndpointReading | None:
        """The LATEST reading, or None. Intermediates are dropped on purpose.

        A drag from 30 to 55 produces every step in between, and none of them
        is worth a WebSocket frame: only where the slider came to rest is true
        by the time HQ draws it. Keeping one slot rather than a queue is also
        what makes this unable to grow without bound, however noisy one Store
        gets.
        """
        with self._lock:
            reading, self._pending = self._pending, None
            self._changed.clear()
            return reading

    def wait_for_change(self, timeout: float) -> bool:
        """Block until something changes, or the timeout expires."""
        return self._changed.wait(timeout)

    def read_now(self):
        """Current state, read directly rather than waited for.

        Used once after preparation so HQ starts from the truth instead of
        from whatever it last commanded.
        """
        from tools import windows_endpoint_volume

        return windows_endpoint_volume.read_state(self.endpoint_id,
                                                  backend=self._backend)


def notifications_supported(*, backend=None) -> tuple[bool, str]:
    """Can this build register an endpoint change callback at all?

    Registers against the default endpoint and immediately unregisters,
    CHANGING NOTHING. It exists because ``comtypes`` builds COM objects at
    runtime and that is exactly what tends to break inside a PyInstaller
    bundle - so "the module is packaged" proves nothing, and a technician
    deserves an answer that came from this machine rather than from a
    changelog.
    """
    if backend is not None:
        return True, "test backend"
    try:
        from comtypes import CLSCTX_ALL, POINTER, cast
        from pycaw.callbacks import AudioEndpointVolumeCallback
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as failure:
        return False, f"Core Audio callbacks unavailable ({failure.__class__.__name__})"

    class _Probe(AudioEndpointVolumeCallback):
        def on_notify(self, *args):
            return None

    control = None
    probe = None
    try:
        speakers = AudioUtilities.GetSpeakers()
        control = cast(
            speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
            POINTER(IAudioEndpointVolume))
        probe = _Probe()
        control.RegisterControlChangeNotify(probe)
        return True, "endpoint change notifications supported"
    except Exception as failure:
        return False, f"could not register for changes ({failure.__class__.__name__})"
    finally:
        if control is not None and probe is not None:
            try:
                control.UnregisterControlChangeNotify(probe)
            except Exception:
                pass
        # Release the COM pointers HERE, deterministically, instead of leaving
        # them to whenever the garbage collector next runs.
        #
        # comtypes releases an interface from __del__. If that runs during a
        # later collection - on another thread, or after COM has been torn
        # down - the process dies with an access violation rather than an
        # exception, and the traceback points at whatever unrelated code
        # happened to allocate at that moment. That is exactly what this
        # diagnostic caused when it was added: a crash during an import in a
        # completely different test file.
        #
        # Dropping the references and collecting inside the function keeps the
        # release in the same call, on the same thread, while COM is still up.
        del control
        del probe
        gc.collect()
