# QRing Call Validation Runbook

## Goal
Run the exact manual checks required for QRing chat, audio, and video across the Visitor -> Gateman -> Homeowner flow.

## Test Environment

### Base URLs
- Production-style frontend: `https://www.useqring.online`
- Backend API health check: `https://qring-backend-1.onrender.com/api/v1/health`

### Seeded QA Accounts
These accounts are created by the backend dev seed path in [main.py](/Users/macbookpro/Documents/qring.io/qring_backend/app/main.py).

- Homeowner
  - Email: `homeowner@useqring.online`
  - Password: `Password123!`
- Gateman / Security
  - Email: `security@useqring.online`
  - Password: `Password123!`
- Estate
  - Email: `estate@useqring.online`
  - Password: `Password123!`
- Admin
  - Email: `admin@useqring.online`
  - Password: `Password123!`

### Required Devices
- Device A: Homeowner browser or phone
- Device B: Visitor browser or phone
- Device C: Gateman browser or phone

Use three separate browsers or browser profiles so sessions stay isolated.

## Before You Start

### 1. Confirm backend is healthy
- Open `https://qring-backend-1.onrender.com/api/v1/health`
- Pass: returns success JSON

### 2. Confirm LiveKit config is present
- Backend env must include:
  - `LIVEKIT_URL`
  - `LIVEKIT_API_KEY`
  - `LIVEKIT_API_SECRET`
- Frontend env must include:
  - `VITE_LIVEKIT_URL`

### 3. Confirm TURN/STUN is configured
- Follow [REALTIME_TURN_DEPLOYMENT.md](/Users/macbookpro/Documents/qring.io/qring_backend/REALTIME_TURN_DEPLOYMENT.md)
- Pass: relay candidates appear in trickle-ice for your TURN server

### 4. Create or identify a live visitor session
You need one active `sessionId`.

Use either flow:
- Scan a QR code as a visitor and proceed until you land on `/session/{sessionId}/message`
- Or open an existing homeowner/security conversation tied to a visitor session

Record:
- `SESSION_ID=________________`

## Common URLs
- Homeowner login: `https://www.useqring.online/login`
- Security login: `https://www.useqring.online/login`
- Security messages: `https://www.useqring.online/dashboard/security/messages?sessionId={SESSION_ID}`
- Visitor message screen: `https://www.useqring.online/session/{SESSION_ID}/message`
- Visitor audio screen: `https://www.useqring.online/session/{SESSION_ID}/audio`
- Visitor video screen: `https://www.useqring.online/session/{SESSION_ID}/video`
- Homeowner visits dashboard: `https://www.useqring.online/dashboard/homeowner/visits`

## What To Watch During Every Scenario
- Browser console should show:
  - `Connected to room`
  - `Participant joined`
  - `Track received`
  - `Call ended`
- QRing in-app debug panel should show recent call events
- Call UI should surface failures visibly, not silently
- Audio and video controls must work:
  - mute/unmute
  - camera on/off
  - end call

## Scenario 1: Homeowner <-> Visitor Call

### Setup
- Device A logs in as homeowner
- Device B opens `https://www.useqring.online/session/{SESSION_ID}/message`

### Steps
1. On Device A, open the visitor session call UI for that `SESSION_ID`
2. Start a video call
3. On Device B, accept the incoming call
4. Verify both devices connect
5. Toggle mute on Device A
6. Toggle camera off and on on Device A
7. End the call from Device B

### Pass Criteria
- Homeowner sees local preview
- Visitor sees incoming call prompt
- Both devices hear each other
- Both devices see video after acceptance
- Mute and camera state changes are reflected
- Ending the call updates both devices
- No blank screen and no silent failure

## Scenario 2: Gateman <-> Homeowner Call

### Setup
- Device A logs in as homeowner
- Device C logs in as `security@useqring.online`
- Device C opens `https://www.useqring.online/dashboard/security/messages?sessionId={SESSION_ID}`

### Steps
1. On Device C, click audio call
2. Confirm Device C is redirected into the shared call UI
3. On Device A, accept the incoming call
4. Verify audio path first
5. Retry as video call

### Pass Criteria
- Security can initiate the call from dashboard
- Homeowner receives incoming call UI
- Both can join the same room
- Audio works in both directions
- Video works when requested
- Call state moves through `ringing` -> `ongoing` -> `ended`

## Scenario 3: Gateman Acting On Behalf Of Visitor

### Setup
- Device A logs in as homeowner
- Device C logs in as security
- Device B is not used initially

### Steps
1. On Device C, open the visitor thread for `SESSION_ID`
2. Start a video call from security
3. Accept on Device A
4. Keep Device C active as the visitor-side fallback participant
5. End from Device C

### Pass Criteria
- Security can stand in for the visitor without backend rejection
- Homeowner can still receive and join
- Media works between homeowner and security
- Call UI stays stable even though the visitor device is absent
- Ending the call closes the room cleanly

## Scenario 4: Poor Network / Low Bandwidth

### Recommended Network Shaping
Use one of:
- macOS Network Link Conditioner
- Chrome DevTools network throttling plus mobile hotspot switch
- Android emulator network throttling

### Suggested bad network target
- Downlink: `<= 700 kbps`
- Uplink: `<= 300 kbps`
- Latency: `>= 300 ms`

### Steps
1. Start a video call between Device A and Device B
2. Degrade Device B network during the call
3. Watch QRing debug panel and console logs
4. If video drops, keep the call alive and test audio
5. If audio also fails, confirm user is still able to continue with chat

### Pass Criteria
- App logs visible reconnect / degradation state
- Video downgrades before total failure
- Audio remains available if possible
- If media becomes unusable, the session still continues in chat
- No frozen UI and no invisible error state

## Scenario 5: Three Participants In One Room

### Setup
- Device A: homeowner
- Device B: visitor
- Device C: security

### Steps
1. Start the call from Device C or Device A
2. Join second participant from Device A or Device C
3. Join the visitor from Device B using the same `SESSION_ID`
4. Speak from each device in turn
5. Toggle video on at least two participants
6. End from one participant and confirm all others close cleanly

### Pass Criteria
- All three devices can join the same visitor-request room
- Remote media is received after each participant joins
- Existing participants are not dropped when the third joins
- Ending the call propagates to all participants

## API Checks During Validation

### Start Call
- Endpoint: `POST /api/v1/calls/start`
- Expect:
  - `callSessionId`
  - `roomName`
  - `status=ringing`

### Join Call
- Endpoint: `POST /api/v1/calls/join`
- Expect:
  - `token`
  - `roomName`
  - `url`

### Direct LiveKit Token
- Endpoint: `POST /api/v1/get-livekit-token`
- Payload:
```json
{
  "user_id": "<user-id>",
  "role": "homeowner|security|visitor",
  "visitor_request_id": "<visitor-request-id>"
}
```
- Expect:
  - `token`
  - `roomName`
  - `url`
  - `expiresIn`

## Call-State Checks
- `ringing`: call created, waiting for join
- `ongoing`: at least one participant has joined
- `ended`: connected call finished normally
- `missed`: ringing call ended before answer

## Fail The Scenario If Any Of These Happen
- Incoming call UI never appears
- Camera or microphone failure is not shown to the user
- Tracks subscribe in console but no media renders
- One participant joins and another is silently kicked
- Ending from one device leaves others hanging in-call
- Poor network causes blank UI without fallback to audio or chat

## Test Record Sheet

| Scenario | Result | Notes |
| --- | --- | --- |
| Homeowner <-> Visitor | PASS / FAIL | |
| Gateman <-> Homeowner | PASS / FAIL | |
| Gateman as Visitor Fallback | PASS / FAIL | |
| Poor Network | PASS / FAIL | |
| Three Participants | PASS / FAIL | |
