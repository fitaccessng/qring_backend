# Fly.io Deployment Guide for Qring Backend

## Prerequisites

1. **Install Fly CLI**
   ```bash
   # macOS/Linux
   curl -L https://fly.io/install.sh | sh
   
   # Windows (with Scoop)
   scoop install flyctl
   ```

2. **Create Fly.io Account**
   - Visit https://fly.io
   - Sign up and create organization

3. **Authenticate**
   ```bash
   fly auth login
   ```

## Deployment Steps

### Step 1: Prepare Environment Variables

Generate secure keys:
```bash
# Generate JWT secret
openssl rand -hex 32

# For VAPID keys, use: https://web-push-codelab.glitch.me/
```

### Step 2: Create Fly.io App

```bash
# From project root directory
fly app create qring-backend --org personal
```

### Step 3: Set Up Managed Postgres Database

```bash
# Create database cluster
fly postgres create --org personal --region ams --vm-size shared-cpu-1x --initial-cluster-size 1

# This will output connection string - save it!
# Then attach to your app
fly postgres attach qring_db -a qring-backend
```

### Step 4: Set Production Secrets

Set sensitive environment variables (not in fly.toml):

```bash
fly secrets set -a qring-backend \
  JWT_SECRET_KEY="$(openssl rand -hex 32)" \
  PAYSTACK_SECRET_KEY="sk_live_your_key" \
  PAYSTACK_PUBLIC_KEY="pk_live_your_key" \
  VAPID_PUBLIC_KEY="your_vapid_public" \
  VAPID_PRIVATE_KEY="your_vapid_private" \
  ADMIN_SIGNUP_KEY="your_admin_key" \
  CORS_ORIGINS="https://yourdomain.com,https://www.yourdomain.com"
```

### Step 5: Update Configuration

Edit `fly.toml` and verify:

```toml
app = "qring-backend"
primary_region = "ams"

[env]
  ENVIRONMENT = "production"
  DEBUG = "false"
  BACKEND_PORT = "8080"
```

### Step 6: Deploy

```bash
# Initial deployment
fly deploy

# View logs
fly logs -a qring-backend

# SSH into container (if needed)
fly ssh console -a qring-backend
```

### Step 7: Run Database Migrations

```bash
# Connect to app machine
fly ssh console -a qring-backend

# Inside container:
alembic upgrade head
exit
```

### Step 8: Verify Deployment

```bash
# Check app status
fly status -a qring-backend

# Test health endpoint
curl https://qring-backend.fly.dev/health

# View logs
fly logs -a qring-backend
```

## Environment Variables Reference

| Variable | Type | Required | Notes |
|----------|------|----------|-------|
| `ENVIRONMENT` | string | ✅ | Set to `production` |
| `DEBUG` | bool | ✅ | Set to `false` |
| `DATABASE_URL` | string | ✅ | From `fly postgres attach` |
| `JWT_SECRET_KEY` | string | ✅ | Generate with `openssl rand -hex 32` |
| `CORS_ORIGINS` | string | ✅ | Your frontend domain(s) |
| `PAYSTACK_SECRET_KEY` | string | ✅ | From Paystack dashboard |
| `PAYSTACK_PUBLIC_KEY` | string | ✅ | From Paystack dashboard |
| `VAPID_PUBLIC_KEY` | string | ✅ | From web push setup |
| `VAPID_PRIVATE_KEY` | string | ✅ | **Keep secret** |
| `ADMIN_SIGNUP_KEY` | string | ✅ | Admin registration token |
| `FRONTEND_BASE_URL` | string | ✅ | Your frontend URL |

## Scaling & Monitoring

### Add More Machines

```bash
# Scale to 2 machines
fly scale count 2 -a qring-backend

# Increase memory
fly scale memory 512 -a qring-backend
```

### View Metrics

```bash
# CPU/Memory usage
fly stats -a qring-backend

# Full logs with filtering
fly logs --level info -a qring-backend
```

### Custom Domain

```bash
# Add your domain
fly certs create yourdomain.com -a qring-backend

# Update DNS (follow instructions output)
```

## Troubleshooting

### Deployment Failed

```bash
# Check detailed logs
fly logs -a qring-backend
fly logs --instance <instance-id> -a qring-backend

# View recent deployments
fly history -a qring-backend
```

### Database Connection Issue

```bash
# Verify database is running
fly postgres status -a qring_db

# Check connection string in secrets
fly secrets list -a qring-backend
```

### App Crashes on Start

```bash
# SSH into container and check
fly ssh console -a qring-backend
cd /app
pip list  # Verify all packages installed
python -c "import app.main"  # Test imports
```

## Continuous Deployment (Optional)

Set up automatic deployments on push to main:

```bash
# Generate token
fly tokens create deploy -x 720h

# Add to GitHub Secrets as FLY_API_TOKEN

# Create .github/workflows/deploy.yml
# (Example in repository root)
```

## Production Checklist

- [ ] Database connection tested
- [ ] All secrets set securely
- [ ] CORS origins configured
- [ ] Database migrations applied
- [ ] API endpoints responding
- [ ] Health check passing (`/health`)
- [ ] Custom domain configured
- [ ] SSL certificate active
- [ ] Logs being captured
- [ ] Monitoring alerts configured
- [ ] Backup strategy for database
- [ ] API keys rotated after initial deployment

## Resources

- **Fly.io Docs**: https://fly.io/docs/
- **FastAPI Deployment**: https://fly.io/docs/languages-and-frameworks/python/
- **Managed Postgres**: https://fly.io/docs/postgres/
- **Metrics & Monitoring**: https://fly.io/docs/monitoring/
- **CLI Reference**: https://fly.io/docs/flyctl/

## Support

For issues:
1. Check Fly.io status: https://status.fly.io
2. Review logs: `fly logs -a qring-backend`
3. Contact support: https://fly.io/support

---

**Next Steps**: After first deployment, configure CD pipeline and set up monitoring alerts.
