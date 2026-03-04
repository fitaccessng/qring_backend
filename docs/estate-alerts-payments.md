# Estate Alerts & In-App Payments

## Summary
This module lets estate managers create alerts for homeowners and collect payments for `payment_request` alerts through Paystack.

## Database Tables
- `estate_alerts`
- `homeowner_payments`

Both tables are created automatically at startup via `Base.metadata.create_all`.

## API Routes
- `POST /api/v1/estate/alerts`
- `GET /api/v1/estate/{estate_id}/alerts`
- `GET /api/v1/estate/alerts/me`
- `GET /api/v1/estate/{estate_id}/alerts/payments`
- `POST /api/v1/alert/{alert_id}/pay`
- `POST /api/v1/payment/paystack/webhook`

## Auth Rules
- Estate users create alerts and view estate payment overview.
- Homeowners only view/pay alerts in estates where they are linked via `homes.homeowner_id`.
- Frontend cannot directly mark payments as paid; payment status updates happen in webhook processing.

## WebSocket Events
Namespace: `settings.DASHBOARD_NAMESPACE`

Room pattern: `estate:{estate_id}:alerts`

Events:
- `ALERT_CREATED`
- `PAYMENT_STATUS_UPDATED`

## Paystack Metadata
Alert payment initialize payload includes:
- `payment_kind=estate_alert`
- `estate_alert_id`
- `estate_id`
- `homeowner_id`

Webhook handler routes by `payment_kind`.

## Receipt Storage
Receipt URL is stored in `homeowner_payments.receipt_url` and returned to both estate and homeowner APIs.
