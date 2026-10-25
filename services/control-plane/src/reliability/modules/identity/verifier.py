"""Identity verification adapters (ADR-0018).

* :class:`OidcVerifier` — validates RS256/ES256 JWTs from any OIDC provider (Clerk by default)
  against its JWKS, pinning algorithms and checking issuer, audience and expiry.
* Dev tokens ``dev:<email>`` are accepted only when ``RE_ENV`` is local/test.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt

from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    email: str
    name: str | None
    mfa: bool


class OidcVerifier:
    ALGORITHMS = ("RS256", "ES256")

    def __init__(self, issuer: str, audience: str | None, jwks_url: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> VerifiedIdentity:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=list(self.ALGORITHMS),
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub"], "verify_aud": self.audience is not None},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise AppError("UNAUTHENTICATED", "invalid or expired token") from exc
        email = claims.get("email") or claims.get("primary_email")
        if not email:
            raise AppError("UNAUTHENTICATED", "token has no email claim")
        amr = claims.get("amr") or []
        mfa = bool(claims.get("mfa") or claims.get("two_factor_enabled") or ("mfa" in amr) or ("otp" in amr))
        return VerifiedIdentity(subject=str(claims["sub"]), email=str(email), name=claims.get("name"), mfa=mfa)


@lru_cache
def get_oidc_verifier() -> OidcVerifier | None:
    s = get_settings()
    if not (s.idp_issuer and s.idp_jwks_url):
        return None
    return OidcVerifier(s.idp_issuer, s.idp_audience, s.idp_jwks_url)


def verify_dev_token(token: str) -> VerifiedIdentity | None:
    if not token.startswith("dev:"):
        return None
    if not get_settings().dev_auth_active:
        raise AppError("UNAUTHENTICATED", "dev sign-in is disabled")
    email = token[4:].strip().lower()
    if "@" not in email or len(email) > 254:
        raise AppError("UNAUTHENTICATED", "invalid dev token")
    return VerifiedIdentity(subject=f"dev:{email}", email=email, name=email.split("@")[0], mfa=True)
