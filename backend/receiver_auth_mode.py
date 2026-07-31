"""Decide how a Receiver may authenticate, and keep deciding.

THE DEFECT THIS FIXES

``build_receiver_runtime_authenticator()`` ran ONCE - at import and again in
``startup_event`` - and its answer was frozen into the app for the life of the
process. It degrades to the legacy Store-token authenticator when either the
phase-one Device tables or the Receiver key container is absent *at that moment*.

On the real Store PC that produced:

    finished state=AUTHENTICATION_REFUSED attempts=1
    authentication refused: HQ refused this Device credential.

The Device was fine. Device 3b1ff11f, Store 31 (Bindapur, active), enabled, its
credential present and not revoked. HQ refused it because the backend had started
BEFORE ``run_receiver_credential_phase_one`` created the Device tables at
07:57 UTC, so it was serving the legacy-only authenticator - and would have gone
on doing so until somebody restarted it.

The failure mode is nasty because HALF the system kept working. Enrolment is an
HTTP route with its own database session, so codes were redeemed and Device rows
were written, exactly as an operator expects. Only the WebSocket handshake - the
one path that consults this frozen object - said no. The HQ page therefore showed
"code redeemed: yes, device connected: no" and nothing anywhere explained why.

THE FIX

Re-evaluate instead of freezing. When the current mode is legacy-only, the next
authentication attempt re-checks the two preconditions and upgrades itself if
they now hold. A migration that runs under a live backend, or a key container
minted after start-up, stops being a silent outage that needs a restart nobody
knows to perform.

It never downgrades. Once Device credentials are being accepted, a transient
database hiccup must not quietly return the fleet to shared Store tokens - that
would be a security regression triggered by a blip.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("echocast.receiver.auth")

#: Seconds between re-checks while degraded. The check is two cheap reads, but a
#: refused Receiver retries, and re-running them per attempt would turn a bad
#: credential into database load.
RECHECK_SECONDS = 5.0

DEVICE_TABLES = frozenset({"receiver_devices", "receiver_credentials"})


class ReceiverAuthMode:
    """Holds the current authenticator and upgrades it when it can.

    ``build`` returns either a device-capable authenticator or ``None`` when the
    preconditions are not met. It is injected so this class can be tested without
    a database, a key container or a server.
    """

    def __init__(self, *, build, legacy, clock=None, recheck_seconds=RECHECK_SECONDS):
        self._build = build
        self._legacy = legacy
        self._clock = clock or time.monotonic
        self._recheck_seconds = recheck_seconds
        self._current = None
        self._last_check = None
        self._upgraded = False
        self.refresh()

    # -- state ------------------------------------------------------------
    @property
    def device_credentials_accepted(self) -> bool:
        return self._upgraded

    @property
    def current(self):
        return self._current if self._current is not None else self._legacy

    def describe(self) -> str:
        return ("device credentials + legacy store tokens" if self._upgraded
                else "legacy store tokens only")

    # -- upgrading --------------------------------------------------------
    def refresh(self) -> bool:
        """Try to obtain a device-capable authenticator. Never downgrades."""
        self._last_check = self._clock()
        if self._upgraded:
            return True
        try:
            built = self._build()
        except Exception:
            # A probe failure must never take Receivers offline: the legacy
            # authenticator stays in place and this is tried again later.
            logger.warning("Receiver authentication probe failed; staying on "
                           "legacy store tokens for now", exc_info=False)
            return False
        if built is None:
            return False
        self._current = built
        self._upgraded = True
        logger.info("Receiver authentication upgraded to device credentials")
        return True

    def _maybe_refresh(self) -> None:
        if self._upgraded:
            return
        now = self._clock()
        if self._last_check is None or (now - self._last_check) >= self._recheck_seconds:
            self.refresh()

    # -- the authenticator interface --------------------------------------
    def authenticate(self, *args, **kwargs):
        """Authenticate one connection, re-checking first if still degraded.

        This is what turns "restart HQ and hope" into "it works": the Store PC
        that was refused at 10:49 would be accepted on its next attempt once the
        tables exist, with nobody having to know that a restart was the remedy.
        """
        self._maybe_refresh()
        return self.current.authenticate(*args, **kwargs)

    def __getattr__(self, name):
        # Anything else the runtime asks of an authenticator is forwarded, so
        # this wrapper stays a drop-in for whatever the interface grows.
        return getattr(self.current, name)
