# Remaining wiring (do this before any live send)

Models and email_service List-Unsubscribe headers are already on this branch.

## 1. Update imports in src/main.py

```python
from src.models import (
    CampaignDB, ProspectDB, EmailLogDB,
    UnsubscribeDB, SubscriptionDB,
    ConsentDB, ReviewQueueDB, SuppressionDB,
)
from src.compliance import (
    assert_can_send, queue_for_review, ComplianceBlock,
    record_consent, approve_review,
)
```

## 2. Replace the send block inside `_process_campaign`

After generating `email_data`, insert:

```python
review_id = queue_for_review(
    db, prospect.email, campaign_id,
    email_data["subject"], email_data["body"], step=1,
)
logger.info("Queued review %s for %s", review_id, prospect.email)
try:
    assert_can_send(db, prospect.email, campaign_id=campaign_id, review_id=review_id)
except ComplianceBlock as block:
    logger.info("Send blocked for %s: %s", prospect.email, block.reason)
    continue
# then existing send_email + log
```

Do the same in `_process_sequences`.

## 3. Add operator endpoints (after the unsubscribe routes)

```python
class ConsentRequest(BaseModel):
    email: EmailStr
    source: str = "manual"

@app.post("/api/v1/consent")
async def create_consent(req: ConsentRequest, db: Session = Depends(get_db)):
    record_consent(db, str(req.email), req.source)
    return {"status": "consent_recorded", "email": str(req.email).lower()}

@app.get("/api/v1/reviews")
async def list_reviews(status: str = "pending", db: Session = Depends(get_db)):
    rows = db.query(ReviewQueueDB).filter(ReviewQueueDB.status == status).limit(100).all()
    return [{"review_id": r.review_id, "prospect_email": r.prospect_email, "status": r.status, "subject": r.subject} for r in rows]

@app.post("/api/v1/reviews/{review_id}/approve")
async def approve_review_endpoint(review_id: str, reviewer: str = "operator", db: Session = Depends(get_db)):
    if not approve_review(db, review_id, reviewer):
        raise HTTPException(status_code=400, detail="Review not found or not pending")
    return {"status": "approved", "review_id": review_id}
```

## 4. Env

```
REQUIRE_CONSENT=true
REQUIRE_REVIEW=true
GLOBAL_HOURLY_LIMIT=50
DOMAIN_DAILY_LIMIT=100
```

## 5. Test

1. Create campaign → drafts land in review_queue, no email leaves.
2. POST /api/v1/consent for a test address.
3. POST /api/v1/reviews/{id}/approve.
4. Re-trigger processing → only that approved + consented address can send.
5. Confirm unsubscribe and rate-limit still block.

Default cash wedge: LLA-47 once the above is live and sequences stay human-reviewed.
No live bulk send until this is merged and tested.
