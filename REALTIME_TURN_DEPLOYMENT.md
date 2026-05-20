# QRing WebRTC + TURN Deployment

This is the active production topology:

`React + Capacitor app -> Socket.IO signaling on Render -> Coturn on VPS -> direct WebRTC media`

## Core rules

- Do not host TURN on Render.
- Do not route media through the backend.
- Do not add group-call or SFU logic to this stack.
- Keep this path 1-on-1 only.

## Recommended hosting

- Signaling/API: Render
- TURN: Hetzner Germany, Contabo Germany, or a UK VPS
- STUN: `stun:stun.l.google.com:19302`

## Ubuntu TURN deployment

1. Provision Ubuntu 22.04 or 24.04 on a Germany VPS.
2. Point `turn.yourdomain.com` to the VPS public IPv4.
3. Install Coturn and certificates:

```bash
sudo apt update
sudo apt install -y coturn certbot
sudo systemctl enable coturn
```

4. Issue TLS certs:

```bash
sudo certbot certonly --standalone -d turn.yourdomain.com
```

5. Copy the QRing baseline from:

- [turnserver.conf.example](/Users/macbookpro/Documents/qring/backend/infra/coturn/turnserver.conf.example)

6. Replace:

- `<TURN_PUBLIC_IP>`
- `<TURN_REALM_FQDN>`
- `<TURN_USERNAME>`
- `<TURN_PASSWORD>`

7. Restart Coturn:

```bash
sudo systemctl restart coturn
sudo systemctl status coturn
```

## Required ports on the TURN VPS

- `3478/udp`
- `3478/tcp`
- `5349/tcp` or `443/tcp` for `turns:`
- `50000-60000/udp`

## Frontend env

```env
VITE_SOCKET_URL=https://qring-backend-1.onrender.com
VITE_SOCKET_PATH=/socket.io
VITE_SIGNALING_NAMESPACE=/realtime/signaling
VITE_WEBRTC_ICE_SERVERS=[{"urls":"stun:stun.l.google.com:19302"},{"urls":["turn:turn.example.com:3478?transport=udp","turn:turn.example.com:3478?transport=tcp"],"username":"TURN_USER","credential":"TURN_PASSWORD"},{"urls":"turns:turn.example.com:5349?transport=tcp","username":"TURN_USER","credential":"TURN_PASSWORD"}]
VITE_CALL_CONNECT_TIMEOUT_MS=8000
VITE_CALL_RING_TIMEOUT_MS=30000
VITE_RTC_MONITORING_URL=https://YOUR_MONITORING_ENDPOINT/rtc
```

## Backend env

```env
WEBRTC_STUN_URL=stun:stun.l.google.com:19302
WEBRTC_TURN_URL=turn:turn.yourdomain.com:3478
WEBRTC_TURN_TLS_URL=turns:turn.yourdomain.com:5349
WEBRTC_TURN_USERNAME=user
WEBRTC_TURN_CREDENTIAL=password
SOCKET_PATH=/socket.io
SIGNALING_NAMESPACE=/realtime/signaling
```

## Coturn baseline

Use:

- [turnserver.conf.example](/Users/macbookpro/Documents/qring/backend/infra/coturn/turnserver.conf.example)
- [docker-compose.turn.yml](/Users/macbookpro/Documents/qring/backend/infra/coturn/docker-compose.turn.yml)

Production notes:

- Keep long-term credentials enabled.
- Keep both UDP and TCP TURN listeners.
- Prefer `turns:` for office WiFi and restrictive carrier paths.
- Keep TLS TURN on `5349` or `443` and test both direct carrier data and estate WiFi.
- Rotate TURN credentials regularly.
- Monitor bandwidth, CPU, packet loss, and relay success rate.

## QRing call tuning

- Default video target: `640x360 @ 24fps`
- Prefer VP8 for video
- Prefer Opus for audio
- Preserve audio first when bandwidth drops
- Use TURN relay retry when direct ICE is unstable
- Re-run ICE after app resume or network switching

## Verification

1. Use the WebRTC trickle-ice sample and confirm relay candidates appear.
2. Confirm both `turn:` and `turns:` produce relay candidates.
3. Confirm call setup usually completes in 1-3 seconds on healthy networks.
4. Test homeowner <-> visitor on different networks.
5. Test homeowner <-> security on different networks.
6. Switch one device from WiFi to mobile data during a live call.
7. Lock and unlock Android during a live call.
8. Confirm audio survives weak-network conditions before video does.
9. Confirm chat still works even if media recovery fails.

## Incident checklist

- Calls ring but do not connect:
  - verify TURN ports
  - verify public IP / DNS on Coturn
  - verify relay candidates still appear
- One-way audio or black video:
  - verify TURN credentials
  - verify ICE candidates are exchanged both ways
  - verify app is not stuck on a stale peer connection after resume
- Reconnect storms:
  - verify Socket.IO health on Render
  - verify Redis/socket fanout if multiple backend instances are running
