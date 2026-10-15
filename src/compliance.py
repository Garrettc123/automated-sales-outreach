"""
Compliance gates for Automated Sales Outreach.

Required before any live send:
  - Consent recorded (source + timestamp)
  - Not on unsubscribe or suppression list
  - Under rate limits (campaign daily + global hourly + domain)
  - Human review approved (status = approved)
  - Deliverability pre-checks pass (no hard bounce history, volume within warmup)

Import and call assert_can_send() before every send_email call.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Tunables via env
GLOBAL_HOURLY_LIMIT = int(os.getenv("GLOBAL_HOURLY_LIMIT", "50"))
DOMAIN_DAILY_LIMIT = int(os.getenv("DOMAIN_DAILY_LIMIT", "100"))
REQUIRE_REVIEW = os.getenv("REQUIRE_REVIEW", "true").lower() == "true"
REQUIRE_CONSENT = os.getenv("REQUIRE_CONSENT", "true").lower() == "true"


class ComplianceBlock(Exception):
    def __init__(self, reason: str, code: str = "blocked"):
        self.reason = reason
        self.code = code
        super().__init__(reason)


def assert_can_send(
    db: Session,
    prospect_email: str,
    campaign_id: Optional[str] = None,
    review_id: Optional[str] = None,
) -> None:
    """Raise ComplianceBlock if any gate fails. Call before send_email."""
    email = prospect_email.lower().strip()

    # 1. Unsubscribe
    from src.models import UnsubscribeDB, ProspectDB
    if db.query(UnsubscribeDB).filter(UnsubscribeDB.email == email).first():
        raise ComplianceBlock("Recipient is unsubscribed", "unsubscribe")
    prospect = db.query(ProspectDB).filter(ProspectDB.email == email).first()
    if prospect and prospect.unsubscribed:
        raise ComplianceBlock("Prospect marked unsubscribed", "unsubscribe")

    # 2. Consent
    if REQUIRE_CONSENT:
        from src.models import ConsentDB
        consent = db.query(ConsentDB).filter(ConsentDB.email == email).first()
        if not consent or not consent.consented_at:
            raise ComplianceBlock("No recorded consent for recipient", "consent")

    # 3. Suppression (hard bounces, complaints)
    from src.models import SuppressionDB
    if db.query(SuppressionDB).filter(SuppressionDB.email == email).first():
        raise ComplianceBlock("Recipient on suppression list", "suppression")

    # 4. Human review
    if REQUIRE_REVIEW:
        from src.models import ReviewQueueDB
        if review_id:
            review = db.query(ReviewQueueDB).filter(ReviewQueueDB.review_id == review_id).first()
            if not review or review.status != "approved":
                raise ComplianceBlock("Review not approved", "review")
        else:
            # No review_id means we require an approved review record for this email+campaign
            q = db.query(ReviewQueueDB).filter(
                ReviewQueueDB.prospect_email == email,
                ReviewQueueDB.status == "approved",
            )
            if campaign_id:
                q = q.filter(ReviewQueueDB.campaign_id == campaign_id)
            if not q.first():
                raise ComplianceBlock("No approved human review for this send", "review")

    # 5. Rate limits
    _check_rate_limits(db, email, campaign_id)

    # 6. Deliverability volume (simple warmup gate)
    _check_deliverability_volume(db, email)


def _check_rate_limits(db: Session, email: str, campaign_id: Optional[str]) -> None:
    from src.models import EmailLogDB, CampaignDB
    now = datetime.utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)

    # Global hourly
    global_recent = db.query(EmailLogDB).filter(
        EmailLogDB.sent_at >= hour_ago,
        EmailLogDB.status == "sent",
    ).count()
    if global_recent >= GLOBAL_HOURLY_LIMIT:
        raise ComplianceBlock(
            f"Global hourly limit reached ({GLOBAL_HOURLY_LIMIT})",
            "rate_limit",
        )

    # Campaign daily (if campaign exists)
    if campaign_id:
        campaign = db.query(CampaignDB).filter(CampaignDB.campaign_id == campaign_id).first()
        if campaign:
            campaign_today = db.query(EmailLogDB).filter(
                EmailLogDB.campaign_id == campaign_id,
                EmailLogDB.sent_at >= day_ago,
                EmailLogDB.status == "sent",
            ).count()
            if campaign_today >= campaign.daily_limit:
                raise ComplianceBlock(
                    f"Campaign daily limit reached ({campaign.daily_limit})",
                    "rate_limit",
                )

    # Domain daily (simple: from address domain)
    from_domain = os.getenv("FROM_EMAIL", "outreach@example.com").split("@")[-1]
    domain_today = db.query(EmailLogDB).filter(
        EmailLogDB.sent_at >= day_ago,
        EmailLogDB.status == "sent",
    ).count()  # approximate; refine with per-domain log if needed
    if domain_today >= DOMAIN_DAILY_LIMIT:
        raise ComplianceBlock(
            f"Domain daily volume limit reached ({DOMAIN_DAILY_LIMIT}) for {from_domain}",
            "deliverability",
        )


def _check_deliverability_volume(db: Session, email: str) -> None:
    """Block if recent hard bounces or complaints exceed threshold."""
    from src.models import SuppressionDB
    recent_suppressions = db.query(SuppressionDB).filter(
        SuppressionDB.created_at >= datetime.utcnow() - timedelta(days=7)
    ).count()
    if recent_suppressions > 20:
        raise ComplianceBlock(
            "Too many recent suppressions; pause for deliverability review",
            "deliverability",
        )


def record_consent(
    db: Session,
    email: str,
    source: str,
    consented_at: Optional[datetime] = None,
) -> None:
    """Record explicit consent. Source examples: form, imported_with_proof, double_opt_in."""
    from src.models import ConsentDB
    email = email.lower().strip()
    existing = db.query(ConsentDB).filter(ConsentDB.email == email).first()
    if existing:
        existing.source = source
        existing.consented_at = consented_at or datetime.utcnow()
    else:
        db.add(ConsentDB(
            email=email,
            source=source,
            consented_at=consented_at or datetime.utcnow(),
        ))
    db.commit()


def queue_for_review(
    db: Session,
    prospect_email: str,
    campaign_id: str,
    subject: str,
    body: str,
    step: int = 1,
) -> str:
    """Create a pending review record. Returns review_id."""
    import uuid
    from src.models import ReviewQueueDB
    review_id = f"rev_{uuid.uuid4().hex[:12]}"
    db.add(ReviewQueueDB(
        review_id=review_id,
        prospect_email=prospect_email.lower().strip(),
        campaign_id=campaign_id,
        subject=subject,
        body=body,
        step=step,
        status="pending",
    ))
    db.commit()
    return review_id


def approve_review(db: Session, review_id: str, reviewer: str = "operator") -> bool:
    from src.models import ReviewQueueDB
    review = db.query(ReviewQueueDB).filter(ReviewQueueDB.review_id == review_id).first()
    if not review or review.status != "pending":
        return False
    review.status = "approved"
    review.reviewed_by = reviewer
    review.reviewed_at = datetime.utcnow()
    db.commit()
    return True
