"""Read-only Windows audio output-device inventory and safe selection.

Why this exists: the installed FFmpeg build has **no audio output muxer** at
all (its ``-devices`` list contains only the ``caca`` video muxer), and
``ffplay`` only exposes DirectShow *capture* device options while playing to
whatever SDL considers the default. Neither can send audio to one explicitly
chosen Windows endpoint, and the pilot must never silently use the default
device. This module therefore wraps PortAudio (via ``sounddevice``) purely to
enumerate output devices and to resolve exactly one of them.

Safety rules enforced here:

- Enumeration is read-only. It never opens a stream, never plays a sound and
  never changes the Windows default device.
- Selection is exact. A stable ``index:N`` selector is preferred; an exact
  device name is accepted only when it matches exactly one output device.
- Partial names, different casing and "first match wins" are all refused. On a
  real Windows machine the same device name commonly appears under several
  host APIs, so guessing would silently pick the wrong endpoint.
- A Bluetooth endpoint may be listed, but it can only ever be reached through
  an explicit selector, never automatically.

Nothing here handles credentials, and a device name is never an identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


class AudioDeviceError(RuntimeError):
    """Base class for controlled, secret-free audio device failures."""


class DeviceEnumerationUnsupportedError(AudioDeviceError):
    """Raised when the audio backend cannot enumerate devices at all."""


class DeviceNotFoundError(AudioDeviceError):
    """Raised when no output device matches the supplied selector exactly."""


class AmbiguousDeviceError(AudioDeviceError):
    """Raised when a selector matches more than one output device."""


INDEX_SELECTOR_PREFIX = "index:"


@dataclass(frozen=True, slots=True)
class OutputDevice:
    """One playback endpoint. Contains no credential and no identity."""

    index: int
    name: str
    host_api: str
    max_output_channels: int
    default_samplerate: int
    is_default: bool

    @property
    def selector(self) -> str:
        """The stable selector an operator should copy."""
        return f"{INDEX_SELECTOR_PREFIX}{self.index}"

    @property
    def looks_like_bluetooth(self) -> bool:
        lowered = self.name.lower()
        return "bluetooth" in lowered or "bthhfenum" in lowered or "hands-free" in lowered

    def as_dict(self) -> dict[str, Any]:
        return {
            "default_samplerate": self.default_samplerate,
            "host_api": self.host_api,
            "index": self.index,
            "is_default": self.is_default,
            "max_output_channels": self.max_output_channels,
            "name": self.name,
            "selector": self.selector,
        }


def _default_backend():
    try:
        import sounddevice
    except Exception as error:  # pragma: no cover - depends on the machine
        raise DeviceEnumerationUnsupportedError(
            "the sounddevice/PortAudio backend is not available, so Windows "
            "output devices cannot be enumerated"
        ) from error
    return sounddevice


def list_output_devices(*, backend: Any | None = None) -> tuple[OutputDevice, ...]:
    """Enumerate playback devices. Opens nothing and changes nothing."""
    audio = backend or _default_backend()
    try:
        hostapis = audio.query_hostapis()
        devices = audio.query_devices()
    except AudioDeviceError:
        raise
    except Exception as error:
        raise DeviceEnumerationUnsupportedError(
            "the audio backend could not enumerate output devices"
        ) from error

    try:
        default_output = audio.default.device[1]
    except Exception:
        default_output = None

    collected: list[OutputDevice] = []
    for index, device in enumerate(devices):
        channels = int(device.get("max_output_channels", 0) or 0)
        if channels <= 0:
            continue  # capture-only endpoint
        host_index = int(device.get("hostapi", 0) or 0)
        try:
            host_api = str(hostapis[host_index]["name"])
        except (IndexError, KeyError, TypeError):
            host_api = "unknown"
        collected.append(
            OutputDevice(
                index=index,
                name=str(device.get("name", "")).strip(),
                host_api=host_api,
                max_output_channels=channels,
                default_samplerate=int(float(device.get("default_samplerate", 0) or 0)),
                is_default=(index == default_output),
            )
        )
    return tuple(collected)


def resolve_output_device(
    selector: str | None,
    *,
    backend: Any | None = None,
    devices: Iterable[OutputDevice] | None = None,
) -> OutputDevice:
    """Resolve exactly one output device. Fails closed on anything ambiguous."""
    if not isinstance(selector, str) or not selector.strip():
        raise AudioDeviceError(
            "an explicit output device selector is required; the pilot never "
            "picks a device for you"
        )
    cleaned = selector.strip()
    available = tuple(devices) if devices is not None else list_output_devices(backend=backend)

    if cleaned.lower().startswith(INDEX_SELECTOR_PREFIX):
        raw_index = cleaned[len(INDEX_SELECTOR_PREFIX):].strip()
        if not raw_index.isdigit():
            raise DeviceNotFoundError(f"{cleaned!r} is not a valid index selector")
        wanted = int(raw_index)
        for device in available:
            if device.index == wanted:
                return device
        raise DeviceNotFoundError(
            f"no output device has index {wanted}; run the device list command again"
        )

    # Exact name match only. No partial matching, no case folding: the same
    # display name legitimately appears under several host APIs.
    matches = [device for device in available if device.name == cleaned]
    if not matches:
        raise DeviceNotFoundError(
            f"no output device is named exactly {cleaned!r}; copy the exact name "
            "or, better, the stable index selector from the device list"
        )
    if len(matches) > 1:
        options = ", ".join(f"{d.selector} ({d.host_api})" for d in matches)
        raise AmbiguousDeviceError(
            f"{cleaned!r} matches {len(matches)} output devices, so it is ambiguous. "
            f"Use one of these stable selectors instead: {options}"
        )
    return matches[0]


def format_device_table(devices: Iterable[OutputDevice]) -> str:
    """Human-readable inventory. Never prints a credential."""
    rows = list(devices)
    lines = [
        "Windows audio OUTPUT devices (read-only; nothing was opened or changed)",
        "",
        f"{'SELECTOR':<12} {'NAME':<46} {'HOST API':<22} {'CH':>3} {'RATE':>7}  FLAGS",
        f"{'-' * 12} {'-' * 46} {'-' * 22} {'-' * 3} {'-' * 7}  {'-' * 20}",
    ]
    for device in rows:
        flags = []
        if device.is_default:
            flags.append("current-default")
        if device.looks_like_bluetooth:
            flags.append("bluetooth")
        lines.append(
            f"{device.selector:<12} {device.name[:46]:<46} {device.host_api[:22]:<22} "
            f"{device.max_output_channels:>3} {device.default_samplerate:>7}  "
            f"{','.join(flags)}"
        )
    if not rows:
        lines.append("(no output devices were reported)")
    lines.extend([
        "",
        "Copy the SELECTOR of the device you want, for example 'index:3'.",
        "Prefer a wired USB audio adapter or a 3.5 mm output.",
        "The pilot never picks a device for you and never changes the Windows default.",
        "A device being listed does not mean an amplifier or speaker is connected.",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        prog="windows_audio_devices",
        description=(
            "List Windows audio output devices. Read-only: it opens no stream, "
            "plays no sound and never changes the Windows default device."
        ),
    )
    parser.add_argument("action", choices=("list",), nargs="?", default="list")
    parser.parse_args(argv)

    try:
        print(format_device_table(list_output_devices()))
    except AudioDeviceError as error:
        import sys

        print(f"Device enumeration refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
