# Compliance Controls — Required Before Live Use

Branch: `compliance-controls-before-use`

## Status

| Gate | Status |
|------|--------|
| Unsubscribe footer + endpoints | Present |
| List-Unsubscribe headers | Present in email_service.py |
| Consent / Review / Suppression models | Present in models.py |
| assert_can_send / queue_for_review | Present in compliance.py |
| Wiring into _process_campaign / _process_sequences | Documented in WIRE_GATES.md (apply next) |
| Operator consent + review endpoints | Documented in WIRE_GATES.md |

## Gates that must pass before any send

1. Consent recorded (source + timestamp)
2. Not unsubscribed or suppressed
3. Human review status = approved
4. Under rate limits (campaign daily + global hourly + domain)
5. Deliverability volume check passes

## Next operator move

Apply the changes in `WIRE_GATES.md` to `src/main.py`, then test with one consented + approved address. Do not run live campaigns until that passes.

Default cash wedge remains LLA-47 ($47 Texas trades first-response map) once controls are live and sequences are human-reviewed.
