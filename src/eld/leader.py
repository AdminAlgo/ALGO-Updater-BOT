"""Leader ELD provider client.

Leader ELD is a white-label of the DriveHOS platform (same API, base URL, and
partner/provider key as Factor). So the client is identical to the Factor one —
only the config's company key differs per company. If Leader ever diverges onto
its own API, override the fetch methods here.
"""

from __future__ import annotations

from .factor import FactorELD


class LeaderELD(FactorELD):
    name = "leader"
