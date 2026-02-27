# Qring Backend

A production-ready FastAPI backend for the Qring property access management system. Provides comprehensive APIs for user authentication, QR-code-based visitor access, real-time notifications, and dashboard analytics.

## 🚀 Features

- **FastAPI REST API** - Modern, high-performance `/api/v1/*` endpoints
- **JWT Authentication** - Secure token-based auth with refresh token rotation
- **Role-Based Access Control** - Three roles: `admin`, `estate`, `homeowner`
- **Real-Time WebSocket** - Socket.IO for live dashboard updates and WebRTC signaling
- **QR Code Management** - Generate, resolve, and track visitor access via QR codes
- **Visitor Session Manager** - Track guest entry/exit with timestamps and logs
- **Payment Integration** - Paystack integration for subscription & transactions
- **Database Migrations** - Alembic-managed schema versioning
- **Comprehensive Logging** - Audit trails for all critical operations
- **Push Notifications** - VAPID-based web push notifications

## 📋 Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI 0.115+ |
| Server | Uvicorn (ASGI) |
| Database | PostgreSQL (prod) / SQLite (dev) |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic |
| Real-Time | Socket.IO |
| Auth | JWT (python-jose) |
| Password | bcrypt |
| Validation | Pydantic |

## 🔧 Setup

### Prerequisites
- Python 3.10+
- PostgreSQL 12+ (production)
- pip

### Local Development

```bash
# 1. Clone the repository
git clone https://github.com/fitaccessng/qring_backend.git
cd qring_backend

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your local database & API keys

# 5. Run database migrations
alembic upgrade head

# 6. Start the development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Environment Variables

| Variable | Example | Notes |
|----------|---------|-------|
| `ENVIRONMENT` | `production` | Set to `production` for prod |
| `DEBUG` | `false` | Disable in production |
| `DATABASE_URL` | `postgresql://user:pass@localhost/qring` | Must use PostgreSQL in prod |
| `JWT_SECRET_KEY` | Generate with `openssl rand -hex 32` | **Keep secret** |
| `CORS_ORIGINS` | `https://yourdomain.com` | Separate multiple with commas |
| `PAYSTACK_SECRET_KEY` | `sk_live_...` | From Paystack dashboard |
| `PAYSTACK_PUBLIC_KEY` | `pk_live_...` | From Paystack dashboard |
| `VAPID_PUBLIC_KEY` | Web push key | From web push service |
| `VAPID_PRIVATE_KEY` | Web push key | **Keep secret** |
| `FRONTEND_BASE_URL` | `https://yourdomain.com` | Frontend URL |

See [.env.example](.env.example) for all available options.

## 📡 API Endpoints

### Authentication
```
POST   /api/v1/auth/login              # Login with email/password
POST   /api/v1/auth/signup             # Register new user
POST   /api/v1/auth/refresh-token      # Refresh JWT access token
POST   /api/v1/auth/logout             # Logout user
```

### Dashboard
```
GET    /api/v1/dashboard/overview      # Get dashboard stats
GET    /api/v1/dashboard/visitors      # List recent visitors
GET    /api/v1/dashboard/analytics     # Access analytics
```

### QR Codes
```
POST   /api/v1/qr/generate             # Create new QR code
GET    /api/v1/qr/resolve/{qr_id}      # Resolve QR code
GET    /api/v1/qr/list                 # List all QR codes
DELETE /api/v1/qr/{qr_id}              # Revoke QR code
```

### Visitors
```
POST   /api/v1/visitor/request         # Request visitor access
GET    /api/v1/visitor/sessions        # View visitor sessions
POST   /api/v1/visitor/{id}/approve    # Approve visitor request
POST   /api/v1/visitor/{id}/reject     # Reject visitor request
```

### Payment
```
GET    /api/v1/payment/transactions    # List transactions
POST   /api/v1/payment/verify          # Verify Paystack payment
```

### Admin
```
GET    /api/v1/admin/users             # List all users
GET    /api/v1/admin/audit-log         # View audit log
POST   /api/v1/admin/settings          # Update system settings
```

### WebSocket (Real-Time)
```
Path: /socket.io
Namespaces:
  /realtime/dashboard    # Live dashboard updates
  /realtime/signaling    # WebRTC peer signaling
```

TURN deployment checklist:
- See `REALTIME_TURN_DEPLOYMENT.md`

## 🏗️ Architecture

```
app/
├── api/                    # API routes & endpoints
│   ├── routes/             # Grouped route modules
│   └── deps.py             # Dependency injection
├── core/                   # Core configuration
│   ├── config.py           # Settings management
│   ├── security.py         # JWT & auth logic
│   └── exceptions.py       # Custom exceptions
├── db/                     # Database layer
│   ├── models/             # SQLAlchemy ORM models
│   ├── session.py          # Database session
│   └── base.py             # Base model
├── services/               # Business logic
│   ├── auth_service.py     # Authentication
│   ├── qr_service.py       # QR code handling
│   ├── visitor_service.py  # Visitor management
│   └── ...                 # Other services
├── schemas/                # Pydantic request/response models
├── socket/                 # WebSocket configuration
├── middleware/             # Request middleware
└── main.py                 # FastAPI app entry point
```

## 🗄️ Database Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "describe your changes"

# Run migrations
alembic upgrade head

# Rollback one version
alembic downgrade -1

# View migration history
alembic history
```

## 🚢 Deployment

### Docker

```bash
# Build image
docker build -t qring-backend:latest .

# Run container
docker run -p 8000:8000 \
  --env-file .env \
  qring-backend:latest
```

### Production Checklist

- [ ] Set `ENVIRONMENT=production` and `DEBUG=false`
- [ ] Use PostgreSQL (not SQLite)
- [ ] Generate strong `JWT_SECRET_KEY` (`openssl rand -hex 32`)
- [ ] Configure real CORS origins (not `*`)
- [ ] Set up SSL/TLS (reverse proxy with nginx)
- [ ] Configure logging to persistent storage
- [ ] Set up database backups
- [ ] Enable HTTPS only
- [ ] Rotate Paystack & VAPID keys securely
- [ ] Configure monitoring & alerting

### Health Check

```bash
curl http://localhost:8000/health
# Response: {"status": "healthy"}
```

## 📝 Demo Accounts (Development Only)

Remove these before production deployment:

```
Email: admin@useqring.online
Password: Password123!
Role: admin

Email: homeowner@useqring.online
Password: Password123!
Role: homeowner

Email: estate@useqring.online
Password: Password123!
Role: estate
```

## 🔒 Security Notes

⚠️ **IMPORTANT**
- Never commit `.env` file to version control
- Regenerate all API keys before production
- Use HTTPS in production
- Implement rate limiting on public endpoints
- Keep dependencies updated regularly
- Use environment-specific secrets management (AWS Secrets Manager, Azure Key Vault, etc.)

## 📚 API Documentation

Once the server is running, view interactive API docs:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 🤝 Contributing

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Commit changes: `git commit -am 'Add your feature'`
3. Push to branch: `git push origin feature/your-feature`
4. Submit a pull request

## 📄 License

Proprietary - All rights reserved © 2026 Qring

## 👥 Support

For issues or questions, contact: support@useqring.online
