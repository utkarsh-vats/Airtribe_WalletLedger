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


KycStatus = Literal["pending", "verified", "rejected"]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class CreateWalletRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Label = "main"


class WalletResponse(BaseModel):
    """Amounts in paise. available = balance - held_amount (never negative, by CHECK)."""

    id: int
    user_id: int
    label: str
    currency: str
    balance: int
    held_amount: int
    available: int
    created_at: datetime


class BalanceResponse(BaseModel):
    """Amounts in paise, all read from one row at one moment."""

    wallet_id: int
    currency: str
    balance: int
    held_amount: int
    available: int


class SetKycRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kyc_status: KycStatus


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    role: Literal["user", "admin"]
    kyc_status: KycStatus
    created_at: datetime
