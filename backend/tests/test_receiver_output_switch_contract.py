"""Switching a Store's speaker, against the sink the Receiver really has.

WHY THIS FILE EXISTS

Remote speaker selection shipped calling `WindowsPcmSink.start()`. That method
does not exist - the class has `open()` - so every change failed at its first
line with an AttributeError, and HQ reported it exactly as it was told:

    the last change was refused by the Store
    (that speaker could not be opened: 'WindowsPcmSink' object has no
     attribute 'start')

A correct message about a broken call. The tests that existed drove the HQ
half and handed the Receiver half a hand-written fake, and a hand-written fake
answers to whatever it is asked - `start()` included. So the double here is
built with `create_autospec`, which has exactly the surface of the real class
and raises on anything else, and one test drives the real code path with it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.audio_receiver_pilot import (  # noqa: E402
    SINK_MODE_WINDOWS, WindowsPcmSink,
)


class Device:
    """The resolved endpoint HQ asked for."""
    verified_selector = "index:3@Speakers (Realtek(R) Audio)"
    index = 3
    name = "Speakers (Realtek(R) Audio)"

    def as_dict(self):
        return {"index": self.index, "name": self.name,
                "verified_selector": self.verified_selector}


def test_the_switch_only_calls_methods_the_real_sink_has(monkeypatch):
    """The double has the real class's surface. Calling anything else raises,
    which is what should have happened in this test rather than in a shop."""
    from tools import audio_receiver_pilot as pilot

    opened = create_autospec(WindowsPcmSink, instance=True)
    monkeypatch.setattr(pilot, "WindowsPcmSink", lambda *a, **k: opened)

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = None
    receiver.sink = None
    receiver._announcement = None
    receiver.config_path = None
    receiver._audio_backend = None

    receiver._switch_output_device(Device())

    # It opened the new speaker...
    opened.open.assert_called_once_with()
    # ...and never asked for a method the class does not have. (autospec would
    # have raised on the way in; this is the assertion in words.)
    assert not hasattr(WindowsPcmSink, "start"), (
        "if the sink grows a start(), decide which one the switch means")


def test_the_previous_speaker_is_closed_not_left_open(monkeypatch):
    """Two open endpoints is two things writing to a sound card, and the shop
    hears whichever wins."""
    from tools import audio_receiver_pilot as pilot

    previous = create_autospec(WindowsPcmSink, instance=True)
    opened = create_autospec(WindowsPcmSink, instance=True)
    monkeypatch.setattr(pilot, "WindowsPcmSink", lambda *a, **k: opened)

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = previous
    receiver.sink = None
    receiver._announcement = None
    receiver.config_path = None
    receiver._audio_backend = None

    receiver._switch_output_device(Device())

    previous.close.assert_called_once_with()
    assert receiver.pcm_sink is opened


def test_an_announcement_follows_the_shop_to_the_new_speaker(monkeypatch):
    """Changing the speaker mid-campaign must not silence it: the recording
    keeps playing, through the new endpoint."""
    from tools import audio_receiver_pilot as pilot

    opened = create_autospec(WindowsPcmSink, instance=True)
    monkeypatch.setattr(pilot, "WindowsPcmSink", lambda *a, **k: opened)

    class Playing:
        _sink = object()

    playing = Playing()
    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = None
    receiver.sink = None
    receiver._announcement = playing
    receiver.config_path = None
    receiver._audio_backend = None

    receiver._switch_output_device(Device())

    assert playing._sink is opened
