# QRing 1-on-1 Call Validation

This runbook validates the current production design:

- chat over Socket.IO
- 1-on-1 audio/video over native WebRTC
- STUN + Coturn for NAT traversal

## Before testing

1. Confirm backend health at `https://qring-backend-production.up.railway.app/api/v1/health`.
2. Confirm TURN is configured with relay candidates visible in trickle-ice.
3. Confirm frontend env includes `VITE_WEBRTC_ICE_SERVERS`.
4. Confirm backend env includes `WEBRTC_TURN_URL`, `WEBRTC_TURN_USERNAME`, and `WEBRTC_TURN_CREDENTIAL`.

## Required devices

- Device A: homeowner
- Device B: visitor
- Device C: security

Use separate browsers or phones so sessions stay isolated.

Target real devices for signoff:

- Samsung
- Tecno
- Infinix
- Redmi / Xiaomi

## Core scenarios

### 1. Homeowner <-> Visitor video

1. Visitor opens `/session/{SESSION_ID}/message`.
2. Homeowner starts a video call.
3. Visitor accepts.
4. Confirm connect time is fast and both media directions work.
5. End from either side.

Pass:

- incoming call modal appears
- video renders on both sides
- audio is clear both ways
- ending the call closes both clients

### 2. Security <-> Homeowner audio

1. Security opens the session thread.
2. Security starts audio call.
3. Homeowner accepts.
4. Test mute and reconnect.

Pass:

- audio starts reliably
- TURN fallback still connects if direct path fails
- reconnect after brief network drop keeps the same session alive

### 3. Weak network downgrade

1. Start homeowner <-> visitor video call.
2. Degrade one client to poor LTE or throttled WiFi.
3. Watch diagnostics and logs.

Pass:

- app reports degraded state
- video degrades before audio
- audio remains usable where possible
- chat remains available if media recovery fails

### 4. Network switch recovery

1. Start a live call.
2. Move one device from WiFi to cellular.
3. Resume the app if the OS backgrounded it.
4. Repeat with hotspot scenarios like MTN -> Airtel and Glo -> WiFi.

Pass:

- Socket.IO reconnects
- ICE restarts
- media returns without requiring a new session

## API checks

### Start call

- `POST /api/v1/calls/start`
- expect:
  - `callSessionId`
  - `status`
  - `rtcConfig`

### Join call

- `POST /api/v1/calls/join`
- expect:
  - `callSessionId`
  - `status`
  - `rtcConfig`

### End call

- `POST /api/v1/calls/end`
- expect:
  - `callSessionId`
  - `status`
  - `endedAt`

## Fail the test if

- incoming call never appears
- relay candidates are missing in bad networks
- one-way audio occurs repeatedly
- app stays stuck in connecting after network recovery
- end-call state does not propagate to both peers
