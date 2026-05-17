# QRing RTC Production Audit

Superseded by the LiveKit Cloud migration plan for public launch. Keep this document as historical context for the former self-hosted risk profile.

This app was originally structured for **self-hosted LiveKit**, not LiveKit Cloud.

Evidence in-repo:

- `backend/infra/livekit/livekit.production.yaml`
- `backend/infra/coturn/*`
- previous deployment docs and Coturn templates in `backend/infra/*`

## Executive Summary

Main production risks before release:

1. TURN deployment mode is ambiguous.
2. Coturn examples were inconsistent with frontend/docs on TLS port and relay range.
3. Static TURN credentials are documented in frontend env examples.
4. Self-hosted LiveKit means QRing owns TURN, firewall, NAT, scaling, and failover.
5. Real-world mobile-network testing is not represented in automated verification yet.
6. Monitoring exists only at app-log level unless external sinks are configured.

## Exact Issues Found

### 1. TURN mode conflict risk

The repo documents both:

- LiveKit built-in TURN via `backend/infra/livekit/livekit.production.yaml`
- standalone Coturn via `backend/infra/coturn/*`

These must not terminate on the same ports at the same time unless you intentionally split hosts/IPs.

### 2. Coturn TLS port mismatch

Before this audit:

- frontend/docs previously expected a self-hosted `turns:` endpoint on `443/tcp`
- coturn examples listened on `5349`

That would cause TLS TURN fallback to fail even though signaling still works.

### 3. Relay port range mismatch

Before this audit:

- LiveKit docs/firewall examples used `50000-60000/udp`
- coturn examples used `49152-65535`

That means relay allocation could fail under production firewall rules.

### 4. Static TURN credentials in frontend env

The docs currently show embedding TURN username/password in frontend build-time env.

That is workable for testing but weak for production:

- credentials become extractable from the shipped client
- abusive relay usage becomes easier
- rotation is harder

### 5. Self-hosted operational burden

With self-hosted LiveKit, QRing is responsible for:

- TURN reachability
- NAT traversal
- firewall correctness
- packet-loss resilience
- regional placement
- failover capacity

### 6. Monitoring gap

The app had detailed local RTC logs, but no clear production sink.

## Fixes Applied In Repo

### Coturn consistency

Updated:

- `backend/infra/coturn/turnserver.conf.example`
- `backend/infra/coturn/docker-compose.turn.yml`

Changes:

- TURN/TLS aligned to `443`
- relay media range aligned to `50000-60000`
- added warning not to co-run Coturn and LiveKit TURN on the same exposed ports without explicit design

### Android permissions

Updated:

- `frontend/android/app/src/main/AndroidManifest.xml`

Added:

- `FOREGROUND_SERVICE`

### Monitoring hook

Added:

- `frontend/src/services/rtcMonitoring.js`

Integrated into:

- `frontend/src/hooks/useSessionRealtime.js`

Behavior:

- emits browser event `qring:rtc-monitor`
- forwards warn/error events to global Sentry if present
- optionally sends beacons to `VITE_RTC_MONITORING_URL`

## Required Production Decisions

## Option A: LiveKit Cloud

Recommended if the team is not deeply experienced in WebRTC networking.

Benefits:

- TURN handled for you
- better default global routing
- less NAT/firewall burden
- lower ops risk during stabilization

Suggested path:

1. move media to LiveKit Cloud
2. keep existing app/session logic
3. remove custom static TURN credentials from frontend env
4. keep voice-note pipeline separate

## Option B: Self-hosted LiveKit

If you stay self-hosted:

1. choose one TURN strategy
2. document host/IP ownership explicitly
3. open firewall exactly for the selected strategy
4. verify relay candidates from real devices and real networks
5. add monitoring/alerting before broad release

## TURN Strategy Guidance

### Preferred self-hosted baseline

Use **LiveKit built-in TURN** first unless you have a strong reason to run standalone Coturn.

Why:

- fewer moving parts
- tighter integration with LiveKit
- less duplicate configuration

If using built-in TURN:

- keep `turn.enabled: true`
- keep `rtc.use_external_ip: true`
- open:
  - `3478/udp`
  - `3478/tcp`
  - `443/tcp` for TURN/TLS if terminated directly by LiveKit
  - `7881/tcp`
  - `50000-60000/udp`

If using standalone Coturn:

- disable or avoid overlapping LiveKit TURN exposure
- keep Coturn on dedicated host or dedicated public IP if possible
- align frontend ICE URLs to the actual TURN listener ports

## Security Considerations

1. TURN credentials should be short-lived where possible.
2. Do not run open relay.
3. Keep `lt-cred-mech` or shared-secret auth enabled.
4. Rotate TURN credentials regularly.
5. Keep LiveKit tokens short-lived.
6. Keep room join authorization bound to session/call identity.
7. Ensure voice-note uploads validate:
   - file size
   - allowed extension
   - content type
8. Signed media URLs should expire.

## Real-World Failure Modes To Expect

Media can still fail while signaling succeeds when:

1. TURN/TLS port is closed.
2. UDP relay ports are blocked.
3. external IP is wrong on LiveKit or Coturn.
4. client gets only host/srflx candidates, no relay candidates.
5. mobile carrier blocks/rewrites UDP aggressively.
6. network switches between Wi-Fi and cellular during ICE establishment.
7. packet loss triggers transport degradation before media fully stabilizes.

## Test Matrix

Run all of these before production:

1. home Wi-Fi to home Wi-Fi
2. home Wi-Fi to 4G/5G
3. MTN Nigeria
4. Airtel Nigeria
5. Glo Nigeria
6. 9mobile
7. one side on unstable hotspot
8. one side with UDP blocked
9. one side switching Wi-Fi to cellular mid-call
10. degraded bandwidth + packet loss

## Packet Loss / Reconnect Expectations

For Nigerian networks, expect:

- intermittent RTT spikes
- cellular NAT rebinding
- unstable upstream bandwidth

Recommendations:

1. prefer audio-first fallback under weak network
2. keep TURN/TLS over 443 available
3. keep reconnect attempts bounded and observable
4. track relay-vs-direct candidate usage in diagnostics
5. treat room-connected and media-connected as separate states

## Scalability Concerns

Self-hosted TURN cost scales with:

- egress bandwidth
- concurrent relay sessions
- region count

Watch for:

- TURN CPU saturation
- relay egress bills
- lack of regional TURN nodes for African traffic
- single-host TURN as a SPOF

## Minimum Release Recommendation

Before broad release, QRing should have:

1. one chosen TURN topology
2. verified relay candidate generation
3. real mobile-network call tests
4. monitoring sink wired for RTC failures
5. alerts on repeated reconnects and connect timeouts
6. explicit rollback path to audio-only or voice-note fallback
