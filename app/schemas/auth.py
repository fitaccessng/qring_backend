from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    role: str = "homeowner"


class AdminSignupRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str
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
