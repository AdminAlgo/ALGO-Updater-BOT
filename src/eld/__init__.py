"""ELD provider integrations (Factor ELD / DriveHOS, Leader ELD).

`build_provider` is the factory the rest of the app uses: given a Company and
the loaded Secrets, it returns the right ELDProvider instance authenticated
with that platform's Provider key plus the company's own Company key.
"""

from __future__ import annotations

from .base import ELDError, ELDProvider
from .factor import FactorELD
from .leader import LeaderELD

PROVIDER_CLASSES = {"factor": FactorELD, "leader": LeaderELD}


def build_provider(company, secrets) -> ELDProvider:
    """Construct the ELD provider for a company from config secrets."""
    cls = PROVIDER_CLASSES.get(company.provider)
    if cls is None:
        raise ELDError(f"Unknown provider: {company.provider!r}")
    return cls(
        provider_key=secrets.provider_key_for(company.provider),
        company_key=company.company_key,
        base_url=secrets.base_url_for(company.provider),
    )


__all__ = ["build_provider", "ELDProvider", "ELDError"]
