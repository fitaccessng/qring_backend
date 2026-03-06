# Calls E2E Test Scenario

## Goal
Validate full call coordination from visitor to homeowner with backend-managed call sessions.

## Preconditions
- Backend is running with valid LiveKit env (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`).
- Frontend and mobile builds are deployed.
- At least one valid visitor session or appointment exists.

## End-to-End Flow
1. Visitor arrives and opens session.
2. Visitor triggers call start.
3. Backend creates/returns call session (`status=ringing`) and emits realtime `incoming-call`.
4. Homeowner sees incoming call popup immediately (no polling).
5. Homeowner accepts call and joins room.
6. Backend issues LiveKit token and transitions call session to `active`.
7. Both participants publish/subscribe tracks.
8. Either participant ends call.
9. Backend sets call session to `ended` and cleans up LiveKit room.

## Assertions
- `POST /api/v1/calls/start` returns `callSessionId`, `roomName`, `status=ringing`.
- `POST /api/v1/calls/join` returns `token`, `roomName`.
- `POST /api/v1/calls/end` returns `status=ended`.
- Realtime event `incoming-call` is received by homeowner client.
- LiveKit webhook events (`participant_joined`, `participant_left`, `room_finished`) are accepted by `/api/v1/webhooks/livekit`.

## Failure Cases
- Invalid UUID IDs should return `422`.
- Unauthorized call join/end should return `401/403`.
- Missing media permissions should surface `Camera/Microphone access required for calls`.
- Network drop should transition UI state to `failed` and allow retry.
