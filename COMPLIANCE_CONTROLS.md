# Compliance Controls — Required Before Live Use

Added on branch `compliance-controls-before-use`.

## Gates (must all pass before any send)

1. **Consent**  
   Explicit record in `consents` table (source + timestamp).  
   Set `REQUIRE_CONSENT=false` only for internal testing.

2. **Unsubscribe**  
   Existing `UnsubscribeDB` + prospect.unsubscribed flag.  
   Every email already carries footer.  
   List-Unsubscribe header recommended in email_service.py (next patch).

3. **Rate limits**  
   - Campaign `daily_limit` (already present)  
   - Global hourly (`GLOBAL_HOURLY_LIMIT`, default 50)  
   - Domain daily (`DOMAIN_DAILY_LIMIT`, default 100)  

4. **Human review**  
   Drafts go to `review_queue` with status `pending`.  
   `REQUIRE_REVIEW=true` (default) blocks send until an approved review_id exists.  
   Approve via `approve_review()` or new `/api/v1/reviews/{id}/approve` endpoint.

5. **Deliverability**  
   Suppression list for hard bounces and complaints.  
   Volume pause if recent suppressions exceed threshold.  
   Warm-up and multi-domain rotation still need explicit implementation before high volume.

## Integration points

In `_process_campaign` and `_process_sequences` (src/main.py), before `send_email`:

```python
from src.compliance import assert_can_send, queue_for_review, ComplianceBlock

# Option A: require pre-approved review
try:
    assert_can_send(db, prospect.email, campaign_id=campaign_id)
except ComplianceBlock as e:
    logger.info("Blocked %s: %s", prospect.email, e.reason)
    continue

# Option B: queue for review instead of auto-send
review_id = queue_for_review(db, prospect.email, campaign_id, subject, body, step=1)
# later, after human approve:
# assert_can_send(db, ..., review_id=review_id)
```

## Models to merge

Copy classes from `src/models_compliance_additions.py` into `src/models.py` and run `init_db()` / Alembic migration.

## Env vars

```
REQUIRE_CONSENT=true
REQUIRE_REVIEW=true
GLOBAL_HOURLY_LIMIT=50
DOMAIN_DAILY_LIMIT=100
UNSUBSCRIBE_BASE_URL=https://your-domain
FROM_EMAIL=...@your-verified-domain
```

## Status

- Unsubscribe: present in code
- Consent / review / suppression / rate gates: added in this branch
- Full List-Unsubscribe header + domain rotation: follow-up
- Do not run live campaigns until gates are wired into the send path and tested

Default cash wedge remains LLA-47 ($47 Texas trades first-response map) once controls are live and sequences are human-reviewed.
