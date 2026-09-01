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
    """Construct the ELD provider for a company from config secrets.

    Both keys are required: the API rejects a request that is missing either
    header. Missing keys are reported as ELDError here — callers already
    isolate that per company, whereas a None slipped into a request header
    surfaces as a TypeError from deep inside urllib and kills the whole cycle.
    """
    cls = PROVIDER_CLASSES.get(company.provider)
    if cls is None:
        raise ELDError(f"Unknown provider: {company.provider!r}")
    provider_key = secrets.provider_key_for(company.provider)
    if not provider_key:
        raise ELDError(
            f"{company.name}: no Provider API key configured for platform "
            f"{company.provider!r} (X-API-Provider-Key)"
        )
    if not company.company_key:
        raise ELDError(
            f"{company.name}: no Company API key configured "
            f"(X-API-Company-Key, from {company.company_key_env or 'company_key_env'})"
        )
    return cls(
        provider_key=provider_key,
        company_key=company.company_key,
        base_url=secrets.base_url_for(company.provider),
    )


__all__ = ["build_provider", "ELDProvider", "ELDError"]
