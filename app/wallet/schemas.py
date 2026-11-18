"""Request and response bodies for the wallet API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.wallet.phone import normalize_phone

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Phone = Annotated[str, Field(max_length=32), AfterValidator(normalize_phone)]
Password = Annotated[str, Field(min_length=8, max_length=128)]


class CreateUserRequest(BaseModel):
    # forbid: unknown fields are a 400, so e.g. {"role": "admin"} can't be smuggled in.
    model_config = ConfigDict(extra="forbid")

    name: Name
    email: EmailStr
    phone: Phone
    password: Password


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    role: Literal["user", "admin"]
    kyc_status: Literal["pending", "verified", "rejected"]
    created_at: datetime
