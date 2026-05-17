# QRing LiveKit Cloud Launch Migration

This is the active production migration path for QRing's public launch.

## Target Architecture

Keep self-hosted:

- QRing backend API
- QRing Socket.IO signaling/business logic
- QRing chat persistence
- QRing voice-note upload/storage pipeline
- frontend/mobile apps

Move to LiveKit Cloud:

- realtime media plane
- TURN/TLS fallback
- NAT traversal handling
- global edge connectivity
- LiveKit room hosting

Remove from active production topology:

- standalone Coturn as primary path
- self-hosted LiveKit server as primary media path
- frontend dependency on custom TURN credentials

## Required Production Secrets

Backend:

```env
LIVEKIT_URL=wss://qring-yovnizqn.livekit.cloud
LIVEKIT_API_KEY=YOUR_LIVEKIT_CLOUD_API_KEY
LIVEKIT_API_SECRET=YOUR_LIVEKIT_CLOUD_API_SECRET
LIVEKIT_WEBHOOK_SECRET=YOUR_LIVEKIT_CLOUD_WEBHOOK_SECRET
```

Frontend:

```env
VITE_LIVEKIT_CLOUD=true
VITE_LIVEKIT_URL=wss://qring-yovnizqn.livekit.cloud
VITE_RTC_MONITORING_URL=https://YOUR_MONITORING_ENDPOINT/rtc
```

Do not set `VITE_WEBRTC_ICE_SERVERS` for the Cloud migration unless explicitly instructed by LiveKit support.

## LiveKit CLI Workflow

Official commands from current docs:

```bash
brew install livekit-cli
lk cloud auth
lk project list
lk project set-default "<project-name>"
```

Useful verification commands:

```bash
lk token create \
  --api-key "$LIVEKIT_API_KEY" \
  --api-secret "$LIVEKIT_API_SECRET" \
  --identity smoke-test-user \
  --room smoke-test-room \
  --join \
  --valid-for 1h
```

Notes:

- `lk cloud auth` is browser-based and requires interactive account access.
- QRing does not currently contain a LiveKit agent runtime, so `lk agent create` is not applicable to the existing RTC stack without introducing a new service.

## Code-Level Migration Notes

1. The frontend now defaults to `LiveKit Cloud` behavior through `VITE_LIVEKIT_CLOUD=true`.
2. When Cloud mode is enabled, the frontend avoids injecting custom ICE server lists by default.
3. Existing signaling/business logic remains self-hosted and unchanged in principle.
4. Voice notes remain a separate subsystem and are not migrated to LiveKit.

## Verification Checklist

1. Backend health returns `livekitConfigured=true`.
2. Frontend build contains `VITE_LIVEKIT_CLOUD=true`.
3. `VITE_LIVEKIT_URL` uses `wss://`.
4. No active production env references the previous self-hosted TURN hostname.
5. No active production env references the previous self-hosted LiveKit hostname.
6. Calls connect with LiveKit Cloud and timer starts only after remote media.
7. Remote audio renders on both audio and video screens.
8. Voice-note upload returns 200 and playback works.
9. Mobile permissions remain present on Android/iOS.
10. RTC warnings/errors are visible in monitoring.

## Rollback Strategy

If Cloud migration fails before launch:

1. Restore backend `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `LIVEKIT_WEBHOOK_SECRET` to the self-hosted values.
2. Restore frontend `VITE_LIVEKIT_URL` to the self-hosted websocket URL.
3. Remove `VITE_LIVEKIT_CLOUD=true`.
4. Restore `VITE_WEBRTC_ICE_SERVERS` only if returning to self-hosted TURN.
5. Re-enable/validate Coturn and self-hosted firewall paths before reopening traffic.

## Launch Sequence

1. Create/import the LiveKit Cloud project with the CLI.
2. Store Cloud API key/secret/webhook secret in backend production secrets.
3. Update frontend production env to Cloud mode and Cloud websocket URL.
4. Deploy backend.
5. Deploy frontend/web.
6. Build and test Android/iOS against Cloud.
7. Run cross-network smoke tests.
8. Monitor error rate and reconnect rate before broad user announcement.

## Blockers To Clear Before Public Launch

1. LiveKit Cloud project URL must be confirmed.
2. LiveKit Cloud API secret must be stored in backend secrets.
3. CLI/browser auth must be completed on a workstation with account access.
4. Real device tests on production networks must pass.
