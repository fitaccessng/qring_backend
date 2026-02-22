from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    role: str = "homeowner"
    referralCode: str | None = None


class AdminSignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    newPassword: str


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str


class RefreshTokenRequest(BaseModel):
    refreshToken: str


class LogoutRequest(BaseModel):
    refreshToken: str


class AuthUser(BaseModel):
    id: str
    fullName: str
    email: EmailStr
    role: str
    referralCode: str | None = None
    referralEarnings: int | None = None


class AuthResponse(BaseModel):
    accessToken: str
    refreshToken: str
    user: AuthUser


class GoogleSigninRequest(BaseModel):
    idToken: str
    email: EmailStr | None = None
    displayName: str | None = None
    photoURL: str | None = None


class GoogleSignupRequest(BaseModel):
    idToken: str
    email: EmailStr | None = None
    displayName: str | None = None
    photoURL: str | None = None
    role: str = "homeowner"
    referralCode: str | None = None
