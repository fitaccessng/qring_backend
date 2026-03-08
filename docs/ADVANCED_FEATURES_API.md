# Qring Advanced Features API

This document covers the modular advanced feature baseline added under `/api/v1/advanced`.

## Security & Compliance Notes
- All endpoints require authenticated access token unless noted.
- Visitor media is stored under `MEDIA_STORAGE_PATH` (or backend local fallback) and referenced by audit records.
- Recognition stores only hashed visitor keys by default (`sha256(identifier)`), with optional encrypted template payload.
- Multi-channel notifications are synchronized through `notifications` table and socket events.

## REST Endpoints

### Real-Time Visitor Features
- `GET /api/v1/advanced/visitor/queue?limit=100`
  - Returns newest-first live queue entries for homeowner.
- `POST /api/v1/advanced/visitor/snapshots` (multipart)
  - Form fields: `homeownerId`, `mediaType`, `visitorSessionId?`, `appointmentId?`, `source?`, file: `media`
  - Persists secure audit record and emits socket event.
- `GET /api/v1/advanced/visitor/snapshots/{snapshot_id}`
  - Returns encoded file payload (hex + media type) for authorized requester.
- `POST /api/v1/advanced/visitor/recognition`
  - Body: `homeownerId`, `displayName`, `identifier`, `encryptedTemplate?`
  - Returns returning-visitor result and skip-approval suggestion.

### Financial & Payment Integration
- `POST /api/v1/advanced/split-bills`
  - Create shared bill with participants and pledged amounts.
- `GET /api/v1/advanced/split-bills/{bill_id}`
  - Current contribution and remaining balance snapshot.
- `POST /api/v1/advanced/split-bills/{bill_id}/contribute`
  - Mark participant contribution in real-time.
- `POST /api/v1/advanced/receipts`
  - Persist digital receipt entry.
- `GET /api/v1/advanced/receipts`
  - List receipts for current user.
- `GET /api/v1/advanced/receipts/{receipt_id}/pdf`
  - Generates downloadable receipt PDF.

### Safety & Security
- `POST /api/v1/advanced/security/threat-alert`
  - Logs AI/rule-based threat alert and notifies homeowner.
- `GET /api/v1/advanced/security/threat-alerts`
  - List threat logs.
- `POST /api/v1/advanced/security/geofence-check`
  - Stateless geofence validation helper.
- `POST /api/v1/advanced/security/emergency`
  - Triggers emergency signal and multi-channel fanout.

### Community & Engagement
- `POST /api/v1/advanced/community/posts`
  - Create community post/event/notice.
- `GET /api/v1/advanced/community/posts?scope=estate&limit=100`
  - List posts with read state for requester.
- `POST /api/v1/advanced/community/posts/{post_id}/read`
  - Mark post read.

### Summaries
- `GET /api/v1/advanced/summaries/weekly?weekStartIso=...`
  - Generates and stores weekly summary snapshot (visitors, payments, pending alerts).

## WebSocket Events

- `visitor.snapshot`
  - emitted after snapshot upload.
- `payments.split.updated`
  - emitted on split-bill create/contribution updates.
- `security.threat_alert`
  - emitted when threat alert is logged.
- `security.emergency`
  - emitted when emergency signal is triggered.
- `community.post.created`
  - emitted after community post creation.

## Notification Triggers

- `visitor.snapshot` on media upload
- `payment.split_due` on split bill participant creation
- `security.threat_alert` on threat creation
- `security.geofence_violation` on geofence-check outside boundary
- `security.emergency` on emergency trigger
- `summary.weekly` on weekly summary generation

## Environment Variables

Add these to backend `.env`:

- `MEDIA_STORAGE_PATH=/absolute/path/for/secure/media`
- `SMTP_HOST=...`
- `SMTP_PORT=587`
- `SMTP_USERNAME=...`
- `SMTP_PASSWORD=...`
- `SMTP_FROM_EMAIL=no-reply@yourdomain.com`
- `SMS_PROVIDER_API_KEY=...`
- `SMS_PROVIDER_BASE_URL=...`
- `SMS_PROVIDER_SENDER_ID=Qring`
- `FACE_RECOGNITION_API_URL=...`
- `FACE_RECOGNITION_API_KEY=...`
- `FIREBASE_PROJECT_ID=...`
- `FIREBASE_SERVICE_ACCOUNT_JSON=...` or `FIREBASE_SERVICE_ACCOUNT_BASE64=...`

## Frontend Sample Components

Sample components added for integration:

- `src/components/advanced/LiveVisitorFeedSample.jsx`
- `src/components/advanced/PaymentLogsSample.jsx`
- `src/components/advanced/CommunityBoardSample.jsx`
- API client: `src/services/advancedService.js`

## Testing

Added tests in:
- `backend/tests/test_advanced_service.py`

Covers:
- live queue ordering
- split-bill contribution updates
- weekly summary metrics
- threat alert logging
