from __future__ import annotations

from pydantic import BaseModel, EmailStr
from typing import Optional


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    role: str = "resident"
    referralCode: Optional[str] = None


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
    role: str = "resident"
    referralCode: Optional[str] = None
