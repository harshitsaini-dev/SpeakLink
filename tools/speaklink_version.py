"""The one place a SpeakLink Store build says what it is.

WHY THIS FILE EXISTS

A build was published as 1.6.3 whose payload was 1.6.2, and the Store machine
was right: it opened a wizard from the older build. Three separate steps -
the wizard, the kit package, the installer filename - each carried a version
somebody typed by hand, and they drifted apart at the first opportunity.

Everything that names a version now reads it from here. A build that ships the
wrong code can still happen; a build that ships the wrong NUMBER for the code
it shipped cannot, because there is only one number.
"""

#: Bumped by hand, deliberately - a version is a claim a person makes about a
#: build, not something a script should invent from a date.
STORE_KIT_VERSION = "1.7.6"

#: A string the build verifies is present in the FROZEN wizard before the
#: installer is allowed to ship. PyInstaller reuses cached modules when it
#: believes the inputs are unchanged, and it was wrong once: an installer went
#: out containing a wizard built an hour earlier, from source that had since
#: been fixed. The build now reads this constant back out of the packaged
#: bytecode, so "it compiled" and "it contains what I just wrote" stop being
#: the same claim.
BUILD_MARKER = f"speaklink-store-kit {STORE_KIT_VERSION}"
