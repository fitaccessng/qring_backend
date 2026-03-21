# QRing Call Tester Sheet

## Test Setup

- [ ] Open `https://qring-backend-1.onrender.com/api/v1/health` and confirm it loads
- [ ] Use 3 separate devices or browser profiles
- [ ] Record `SESSION_ID: ________________________`

## Login Details

- [ ] Homeowner account
  - Email: `homeowner@useqring.online`
  - Password: `Password123!`
- [ ] Gateman account
  - Email: `security@useqring.online`
  - Password: `Password123!`

## Useful Pages

- [ ] Homeowner login: `https://www.useqring.online/login`
- [ ] Visitor page: `https://www.useqring.online/session/{SESSION_ID}/message`
- [ ] Gateman messages: `https://www.useqring.online/dashboard/security/messages?sessionId={SESSION_ID}`

## Scenario 1: Homeowner <-> Visitor

- [ ] Homeowner logs in
- [ ] Visitor opens session message page
- [ ] Homeowner starts a video call
- [ ] Visitor sees incoming call popup
- [ ] Visitor accepts call
- [ ] Both can hear each other
- [ ] Both can see video
- [ ] Mute works
- [ ] Camera off/on works
- [ ] Ending the call closes it for both people
- [ ] Mark result: PASS / FAIL
- [ ] Notes: ______________________________________

## Scenario 2: Gateman <-> Homeowner

- [ ] Gateman logs in
- [ ] Gateman opens security messages for the same `SESSION_ID`
- [ ] Gateman starts an audio call
- [ ] Homeowner sees incoming call popup
- [ ] Homeowner accepts call
- [ ] Both can hear each other
- [ ] Retry as video call
- [ ] Video works
- [ ] Ending the call closes it for both people
- [ ] Mark result: PASS / FAIL
- [ ] Notes: ______________________________________

## Scenario 3: Gateman Acting For Visitor

- [ ] Do not use visitor device at first
- [ ] Gateman starts the call from the visitor thread
- [ ] Homeowner accepts call
- [ ] Gateman stays in the call as the fallback participant
- [ ] Audio works
- [ ] Video works
- [ ] Ending the call closes it cleanly
- [ ] Mark result: PASS / FAIL
- [ ] Notes: ______________________________________

## Scenario 4: Poor Network

- [ ] Start a homeowner <-> visitor video call
- [ ] Slow down the visitor network
- [ ] Video quality drops instead of freezing
- [ ] Audio keeps working if video becomes unstable
- [ ] If media fails badly, chat still works
- [ ] The app shows an error or reconnect message
- [ ] No blank screen or silent failure
- [ ] Mark result: PASS / FAIL
- [ ] Notes: ______________________________________

## Scenario 5: Three Participants

- [ ] Homeowner joins call
- [ ] Gateman joins same call
- [ ] Visitor joins same call
- [ ] All 3 remain connected
- [ ] Audio is heard from all participants
- [ ] Video appears for at least 2 participants
- [ ] Ending the call closes it for all participants
- [ ] Mark result: PASS / FAIL
- [ ] Notes: ______________________________________

## Fail Immediately If Any Of These Happen

- [ ] Incoming call never appears
- [ ] Audio never starts
- [ ] Video never appears after acceptance
- [ ] One participant is kicked out when another joins
- [ ] Ending on one device leaves others stuck in the call
- [ ] The app fails silently without showing an error

## Final Summary

- [ ] Homeowner <-> Visitor: PASS / FAIL
- [ ] Gateman <-> Homeowner: PASS / FAIL
- [ ] Gateman as Visitor Fallback: PASS / FAIL
- [ ] Poor Network: PASS / FAIL
- [ ] Three Participants: PASS / FAIL
- [ ] Overall: PASS / FAIL
- [ ] Tester Name: ______________________
- [ ] Date: ______________________
