# LiveKit Production Notes

Use [livekit.production.yaml](/Users/macbookpro/Documents/qring.io/qring_backend/infra/livekit/livekit.production.yaml) as the QRing baseline for production.

## Required DNS

- `livekit.useqring.online` -> public IP of the LiveKit host
- `turn.useqring.online` -> same public IP, unless TURN is split onto a dedicated host

## Required Ports

- `443/tcp` for TURN over TLS
- `3478/udp` for TURN over UDP
- `7880/tcp` for LiveKit HTTP/WebSocket control plane
- `7881/tcp` for LiveKit ICE over TCP fallback
- `50000-60000/udp` for RTP media

## Frontend Env

Set these in the frontend build:

```env
VITE_LIVEKIT_URL=wss://livekit.useqring.online
VITE_WEBRTC_ICE_SERVERS=[{"urls":["turn:turn.useqring.online:3478?transport=udp"],"username":"TURN_USERNAME","credential":"TURN_PASSWORD"},{"urls":["turns:turn.useqring.online:443?transport=tcp"],"username":"TURN_USERNAME","credential":"TURN_PASSWORD"},{"urls":"stun:stun.l.google.com:19302"}]
VITE_CALL_CONNECT_TIMEOUT_MS=8000
VITE_CALL_RING_TIMEOUT_MS=30000
VITE_PREFER_VOICE_NOTE_FALLBACK=true
```

## Backend Env

Set these on the API server:

```env
LIVEKIT_URL=https://livekit.useqring.online
LIVEKIT_API_KEY=replace-with-livekit-api-key
LIVEKIT_API_SECRET=replace-with-livekit-api-secret
LIVEKIT_WEBHOOK_SECRET=replace-with-livekit-webhook-secret
```

`LIVEKIT_URL` may be stored as `https://...` on the backend. QRing converts it to `wss://...` for the browser token response.

## Coturn

If you terminate TURN outside the built-in LiveKit TURN service, a minimal `coturn` example is:

```ini
listening-port=3478
tls-listening-port=443
fingerprint
use-auth-secret
static-auth-secret=replace-with-turn-shared-secret
realm=turn.useqring.online
total-quota=200
bps-capacity=0
stale-nonce=600
no-cli
no-tlsv1
no-tlsv1_1
cert=/etc/letsencrypt/live/turn.useqring.online/fullchain.pem
pkey=/etc/letsencrypt/live/turn.useqring.online/privkey.pem
external-ip=YOUR_PUBLIC_IP
min-port=50000
max-port=60000
```

For LiveKit-managed TURN, keep the YAML `turn:` block enabled and do not run a second TURN service on the same ports.
