"""Request and response bodies for authentication."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # No minimum here: a too-short password is just a wrong password (401), not a 400 that
    # tells the caller something about the password policy.
    password: Annotated[str, Field(min_length=1, max_length=128)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 (OAuth2 token type, not a secret)
