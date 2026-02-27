# QRing Realtime + TURN Deployment Checklist

This guide is the production baseline for stable messaging, audio, and video across mobile/NAT-heavy networks.

## 1) Provision TURN host

- Create a VM in/near target users (recommended primary: Africa region).
- Assign a static public IPv4 address.
- Create DNS record (example): `turn.useqring.online` -> VM public IP.
- Open firewall/security group:
  - `3478/udp`
  - `3478/tcp`
  - `5349/tcp`
  - `49152-65535/udp` (relay media range)

## 2) Install and configure coturn

Use your distro package or Docker. Start from:

- `backend/infra/coturn/turnserver.conf.example`
- `backend/infra/coturn/docker-compose.turn.yml`
- `backend/infra/coturn/.env.turn.example`

Required replacements:

- `<TURN_PUBLIC_IP>`
- `<TURN_REALM_FQDN>` (example: `turn.useqring.online`)
- `<TURN_USERNAME>`
- `<TURN_PASSWORD>`

Notes:

- Keep `lt-cred-mech` enabled.
- Do not run open relay (never disable auth).
- Keep TLS (`turns:`) configured for reliability where UDP is blocked.

### One-command Docker Compose start

From `backend/infra/coturn`:

```bash
cp .env.turn.example .env.turn
# update TURN_* values and place certs in ./certs/fullchain.pem + ./certs/privkey.pem
docker compose -f docker-compose.turn.yml up -d
```

## 3) Frontend environment (required)

Update `frontend/.env` (and deployment secrets) with ICE servers:

```env
VITE_WEBRTC_ICE_SERVERS=[{"urls":["stun:stun.l.google.com:19302","stun:stun1.l.google.com:19302"]},{"urls":["turn:turn.useqring.online:3478?transport=udp"],"username":"<TURN_USERNAME>","credential":"<TURN_PASSWORD>"},{"urls":["turns:turn.useqring.online:5349?transport=tcp"],"username":"<TURN_USERNAME>","credential":"<TURN_PASSWORD>"}]
```

This is consumed by `env.webRtcIceServers` in:

- `frontend/src/config/env.js`
- `frontend/src/hooks/useSessionRealtime.js`
- `frontend/src/pages/visitor/SessionPage.jsx`

## 4) Backend and socket requirements

- Socket path/namespace must remain reachable over HTTPS/WSS:
  - `/socket.io`
  - `/realtime/signaling`
- If backend runs multiple instances, add Redis pub/sub adapter/backplane so socket rooms fan out across instances.
- Keep websocket transport enabled end-to-end through proxy/load balancer.

## 5) Verification steps (must pass)

1. ICE server test:
   - Open `https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/`
   - Add your `turn:` and `turns:` servers with credentials.
   - Confirm relay candidates appear (`typ relay`).
2. QRing browser test:
   - Open two devices on different networks.
   - Start video call from homeowner.
   - Verify connection succeeds within a few seconds.
3. Failover test:
   - Temporarily block UDP on one client network.
   - Verify call still connects over `turns:...:5349` (TCP/TLS).
4. Messaging persistence test:
   - Send chat message and confirm immediate UI delivery + persisted state.
   - Simulate DB outage and confirm `Not saved` + retry flow works.

## 6) Operational checklist

- Rotate TURN credentials regularly.
- Monitor TURN VM CPU, bandwidth, and packet loss.
- Alert on:
  - spike in call setup failures
  - drop in relay candidate success rate
  - repeated socket reconnect storms
- Keep at least one standby TURN region for failover.

## 7) Quick runbook (incident)

- Symptom: calls ring but never connect.
  - Check TURN ports and firewall first.
  - Check certificate validity for `turns:`.
  - Confirm relay candidates still generated in trickle-ice.
- Symptom: messages delayed.
  - Verify websocket-only transport and signaling server health.
  - Check backend DB latency and async persist failure rate.
