from __future__ import annotations

from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    role: str = "homeowner"
    referralCode: Optional[str] = None
    companyName: Optional[str] = None
    businessEmail: Optional[EmailStr] = None
    phoneNumber: Optional[str] = None
    officeAddress: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    numberOfEmployees: Optional[int] = None

    @field_validator("businessEmail", mode="before")
    @classmethod
    def normalize_optional_business_email(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class AdminSignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    adminKey: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str
    newPassword: str


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str


class RefreshTokenRequest(BaseModel):
    refreshToken: str


class LogoutRequest(BaseModel):
    refreshToken: str


class RequestEmailVerificationRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    token: str


class AuthUser(BaseModel):
    id: str
    fullName: str
    email: EmailStr
    role: str
    referralCode: Optional[str] = None
    referralEarnings: Optional[int] = None


class AuthResponse(BaseModel):
    accessToken: str
    refreshToken: str
    user: AuthUser


class GoogleSigninRequest(BaseModel):
    idToken: str
    email: Optional[EmailStr] = None
    displayName: Optional[str] = None
    photoURL: Optional[str] = None


class GoogleSignupRequest(BaseModel):
    idToken: str
    email: Optional[EmailStr] = None
    displayName: Optional[str] = None
    photoURL: Optional[str] = None
    role: str = "homeowner"
    referralCode: Optional[str] = None
