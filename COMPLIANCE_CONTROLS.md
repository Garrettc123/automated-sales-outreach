# Compliance Controls — Wired and Ready for Test

Branch: `compliance-controls-before-use`

## Status (complete)

| Gate | Status |
|------|--------|
| Unsubscribe footer + endpoints | Present |
| List-Unsubscribe / List-Unsubscribe-Post headers | Present in email_service.py |
| Consent / Review / Suppression models | Present in models.py |
| assert_can_send / queue_for_review | Present in compliance.py |
| Wiring into _process_campaign / _process_sequences | **Done** in main.py |
| Operator consent + review endpoints | **Done** in main.py |

## Behavior

- Creating a campaign queues every draft for human review. No email leaves until:
  1. Consent is recorded (`POST /api/v1/consent`)
  2. Review is approved (`POST /api/v1/reviews/{id}/approve`)
  3. Rate limits and suppression checks pass
- `REQUIRE_CONSENT=true` and `REQUIRE_REVIEW=true` by default.

## Test sequence

1. Create a campaign with one test prospect → confirm entry in `review_queue` (status=pending) and zero emails sent.
2. `POST /api/v1/consent` with the test email and source="manual".
3. `GET /api/v1/reviews?status=pending` → copy a review_id.
4. `POST /api/v1/reviews/{review_id}/approve`.
5. Re-trigger processing (or create a new campaign) → only the approved + consented address can send.
6. Confirm unsubscribe and rate-limit still block.

## Cash path

Once the single test passes, sequences can point at the live LLA-47 link ($47 Texas trades first-response map) with human review required on every send. No bulk send until that test is green.

Env vars:
```
REQUIRE_CONSENT=true
REQUIRE_REVIEW=true
GLOBAL_HOURLY_LIMIT=50
DOMAIN_DAILY_LIMIT=100
UNSUBSCRIBE_BASE_URL=https://your-domain
FROM_EMAIL=...@your-verified-domain
```
