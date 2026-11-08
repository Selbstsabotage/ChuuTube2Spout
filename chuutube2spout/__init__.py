# SPDX-License-Identifier: GPL-3.0-or-later
"""ChuuTube2Spout - YouTube (and any mpv-playable URL) to Spout, for VJ use.

Copyright (C) 2026 Djinn

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.
"""

LICENSE = "GPL-3.0-or-later"

APP_NAME = "ChuuTube2Spout"

# Renumbered ahead of the first public release: 1.0 is the version that gets
# published, so everything before it counts up through 0.9.x. The 3.x numbers
# in ROADMAP.md are the internal ones this replaced - they are left as written
# because they say when a change happened, and rewriting them would make every
# note in the file disagree with the log it came from.
#
# 0.9.9 is therefore the last three-part number available, and it is not a
# deadline. If 0.9.9 comes and goes without the release actually being
# uploaded, the numbers grow a fourth part - 0.9.9.1, 0.9.9.2, and on - rather
# than rolling into 1.0. 1.0 means "published", and a 1.0 sitting on a hard
# disk unpublished would make the version number lie about the one thing it is
# for. There is no shortage of fourth parts.
#
# PUBLISHED is what turns that from a note into a rule: while it is False the
# test suite refuses any version at or above 1.0, and build.ps1 runs the tests
# before it builds. Flip it in the same commit that tags and uploads 1.0.
PUBLISHED = True

__version__ = "1.0"


def version_tuple(version: str = "") -> tuple[int, ...]:
    """"0.9.9.1" -> (0, 9, 9, 1), so the four-part numbers sort where they read.

    Plain string comparison gets this wrong in the one place it matters:
    "0.9.9.1" < "0.9.9" is False by luck, but "0.9.10" < "0.9.9" is True, and a
    tuple of ints is the cheapest way not to have to think about it again.
    """
    return tuple(int(part) for part in (version or __version__).split("."))
