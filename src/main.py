#!/usr/bin/env python3
"""
Automated Sales Outreach – AI SDR
Revenue Target: $15K/month

Full pipeline:
  1. Lead sourcing    – Apollo / Hunter / LinkedIn enrichment
  2. AI personalisation – OpenAI GPT-4 hyper-personalised first lines
  3. Email sequencing – 3-5 touch automated sequence with reply detection
  4. Delivery         – SendGrid / Postmark / SMTP
  5. Reply handling   – webhook + Calendly meeting booking
  6. Compliance       – CAN-SPAM / GDPR unsubscribe + consent + review + rate limits
  7. Billing          – Stripe (Starter $299, Pro $599, Agency $1499)
"""

import os
import logging
import uuid
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from src.database import get_db, init_db
from src.models import (
    CampaignDB, ProspectDB, EmailLogDB,
    UnsubscribeDB, SubscriptionDB,
    ConsentDB, ReviewQueueDB, SuppressionDB,
)
from src.ai_service import research_prospect, generate_personalized_email
from src.email_service import send_email
from src import billing
from src.compliance import (
    assert_can_send, queue_for_review, ComplianceBlock,
    record_consent, approve_review,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# FastAPI app                                                                  #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialised")
    yield

app = FastAPI(
    title="Automated Sales Outreach",
    description="AI SDR that sends 1,000+ personalized emails per day",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Pydantic schemas                                                             #
# --------------------------------------------------------------------------- #

class Prospect(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    company: str
    title: str
    linkedin_url: Optional[str] = None


class CampaignCreate(BaseModel):
    name: str
    prospects: List[Prospect]
    template: str = "problem_agitate_solve"
    daily_limit: int = Field(default=500, le=100_000)
    product_description: str = "AI-powered sales outreach that books 30+ meetings/month"


class EmailGenerated(BaseModel):
    prospect_email: str
    subject: str
    body: str
    personalization_score: float


class CheckoutRequest(BaseModel):
    user_email: EmailStr
    plan: str  # starter | pro | agency
    success_url: str = "https://your-app.railway.app/billing/success"
    cancel_url: str = "https://your-app.railway.app/billing/cancel"


class ReplyWebhook(BaseModel):
    from_email: EmailStr
    subject: str
    body: str


class ConsentRequest(BaseModel):
    email: EmailStr
    source: str = "manual"
    proof_url: Optional[str] = None


# --------------------------------------------------------------------------- #
# Background: campaign processing                                              #
# --------------------------------------------------------------------------- #

SEQUENCE_DELAYS = [0, 3, 7, 14, 21]


async def _process_campaign(campaign_id: str, product_description: str):
    """Generate emails and queue for human review. Send only if all compliance gates pass."""
    from src.database import SessionLocal
    db = SessionLocal()
    try:
        campaign = db.query(CampaignDB).filter(CampaignDB.campaign_id == campaign_id).first()
        if not campaign:
            logger.error("Campaign %s not found", campaign_id)
            return

        unsubscribed_emails = {
            r.email for r in db.query(UnsubscribeDB.email).all()
        }

        sent_today = 0
        prospects = (
            db.query(ProspectDB)
            .filter(ProspectDB.campaign_id == campaign_id, ProspectDB.sequence_step == 0)
            .all()
        )

        for prospect in prospects:
            if sent_today >= campaign.daily_limit:
                logger.info("Daily limit (%s) reached for campaign %s", campaign.daily_limit, campaign_id)
                break
            if prospect.email in unsubscribed_emails or prospect.unsubscribed:
                logger.info("Skipping unsubscribed prospect %s", prospect.email)
                continue
            if prospect.replied:
                continue

            try:
                research = await research_prospect(
                    prospect.first_name, prospect.last_name,
                    prospect.company, prospect.title, prospect.linkedin_url,
                )
                email_data = await generate_personalized_email(
                    prospect, research, step=1,
                    product_description=product_description,
                )
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

                success = await send_email(
                    to_email=prospect.email,
                    subject=email_data["subject"],
                    body=email_data["body"],
                    campaign_id=campaign_id,
                )

                log = EmailLogDB(
                    campaign_id=campaign_id,
                    prospect_email=prospect.email,
                    subject=email_data["subject"],
                    body=email_data["body"],
                    step=1,
                    status="sent" if success else "bounced",
                    sent_at=datetime.utcnow() if success else None,
                )
                db.add(log)
                prospect.sequence_step = 1
                prospect.last_contacted = datetime.utcnow()
                db.commit()
                sent_today += 1
            except Exception as exc:
                logger.error("Error processing prospect %s: %s", prospect.email, exc)
                db.rollback()

        campaign.status = "active"
        db.commit()
        logger.info("Campaign %s: processed, sent %s emails", campaign_id, sent_today)
    finally:
        db.close()


async def _process_sequences():
    """Process follow-up sequences. Queue for review and gate every send."""
    from src.database import SessionLocal
    db = SessionLocal()
    try:
        unsubscribed_emails = {r.email for r in db.query(UnsubscribeDB.email).all()}
        now = datetime.utcnow()

        for step_index, delay_days in enumerate(SEQUENCE_DELAYS[1:], start=2):
            cutoff = now - timedelta(days=delay_days)
            prospects = (
                db.query(ProspectDB)
                .filter(
                    ProspectDB.sequence_step == (step_index - 1),
                    ProspectDB.last_contacted <= cutoff,
                    ProspectDB.replied.is_(False),
                    ProspectDB.unsubscribed.is_(False),
                )
                .all()
            )
            for prospect in prospects:
                if prospect.email in unsubscribed_emails:
                    continue
                try:
                    campaign = db.query(CampaignDB).filter(
                        CampaignDB.campaign_id == prospect.campaign_id
                    ).first()
                    product_desc = "AI-powered sales outreach that books 30+ meetings/month"
                    research = await research_prospect(
                        prospect.first_name, prospect.last_name,
                        prospect.company, prospect.title,
                    )
                    email_data = await generate_personalized_email(
                        prospect, research, step=step_index,
                        product_description=product_desc,
                    )
                    review_id = queue_for_review(
                        db, prospect.email, prospect.campaign_id or "",
                        email_data["subject"], email_data["body"], step=step_index,
                    )
                    logger.info("Queued sequence review %s for %s", review_id, prospect.email)
                    try:
                        assert_can_send(db, prospect.email, campaign_id=prospect.campaign_id, review_id=review_id)
                    except ComplianceBlock as block:
                        logger.info("Sequence send blocked for %s: %s", prospect.email, block.reason)
                        continue
                    success = await send_email(
                        to_email=prospect.email,
                        subject=email_data["subject"],
                        body=email_data["body"],
                        campaign_id=prospect.campaign_id,
                    )
                    log = EmailLogDB(
                        campaign_id=prospect.campaign_id,
                        prospect_email=prospect.email,
                        subject=email_data["subject"],
                        body=email_data["body"],
                        step=step_index,
                        status="sent" if success else "bounced",
                        sent_at=datetime.utcnow() if success else None,
                    )
                    db.add(log)
                    prospect.sequence_step = step_index
                    prospect.last_contacted = datetime.utcnow()
                    db.commit()
                except Exception as exc:
                    logger.error("Sequence error for %s (step %s): %s", prospect.email, step_index, exc)
                    db.rollback()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Routes – core                                                                #
# --------------------------------------------------------------------------- #

@app.get("/")
async def root():
    return {
        "service": "Automated Sales Outreach",
        "version": "2.0.0",
        "revenue_target": "$15K/month",
        "capacity": "1,000+ emails/day",
        "pricing": {
            "starter": "$299/month (500 emails/day)",
            "pro": "$599/month (1,000 emails/day)",
            "agency": "$1,499/month (unlimited emails/day)",
        },
        "compliance": "consent + human review + rate limits required before send",
    }


@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    openai_key = os.getenv("OPENAI_API_KEY", "")
    pending = db.query(EmailLogDB).filter(EmailLogDB.status == "pending").count()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "email_queue": pending,
        "openai_configured": bool(openai_key),
        "sendgrid_configured": bool(os.getenv("SENDGRID_API_KEY", "")),
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY", "")),
        "require_consent": os.getenv("REQUIRE_CONSENT", "true"),
        "require_review": os.getenv("REQUIRE_REVIEW", "true"),
    }


# --------------------------------------------------------------------------- #
# Routes – campaigns                                                           #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/campaigns")
@app.post("/api/campaign")
async def create_campaign(
    campaign: CampaignCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create an outreach campaign. Emails are queued for review; nothing sends until gates pass."""
    campaign_id = f"camp_{uuid.uuid4().hex[:12]}"
    logger.info("Creating campaign %s: %s (%s prospects)", campaign_id, campaign.name, len(campaign.prospects))

    db_campaign = CampaignDB(
        campaign_id=campaign_id,
        name=campaign.name,
        template=campaign.template,
        daily_limit=campaign.daily_limit,
        status="pending",
    )
    db.add(db_campaign)
    db.flush()

    for p in campaign.prospects:
        db_prospect = ProspectDB(
            email=str(p.email),
            first_name=p.first_name,
            last_name=p.last_name,
            company=p.company,
            title=p.title,
            linkedin_url=p.linkedin_url,
            campaign_id=campaign_id,
        )
        db.add(db_prospect)

    db.commit()

    background_tasks.add_task(_process_campaign, campaign_id, campaign.product_description)

    return {
        "campaign_id": campaign_id,
        "name": campaign.name,
        "prospects": len(campaign.prospects),
        "status": "processing",
        "note": "Drafts queued for human review. No email leaves until consent + approved review + rate limits pass.",
    }


@app.get("/api/v1/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, db: Session = Depends(get_db)):
    campaign = db.query(CampaignDB).filter(CampaignDB.campaign_id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    total = db.query(ProspectDB).filter(ProspectDB.campaign_id == campaign_id).count()
    sent = db.query(EmailLogDB).filter(
        EmailLogDB.campaign_id == campaign_id, EmailLogDB.status == "sent"
    ).count()
    replied = db.query(ProspectDB).filter(
        ProspectDB.campaign_id == campaign_id, ProspectDB.replied.is_(True)
    ).count()
    meetings = db.query(ProspectDB).filter(
        ProspectDB.campaign_id == campaign_id, ProspectDB.meeting_booked.is_(True)
    ).count()
    return {
        "campaign_id": campaign.campaign_id,
        "name": campaign.name,
        "status": campaign.status,
        "prospects": total,
        "emails_sent": sent,
        "replies": replied,
        "meetings_booked": meetings,
        "created_at": campaign.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Routes – email generation                                                    #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/generate-email", response_model=EmailGenerated)
async def generate_email_endpoint(prospect: Prospect):
    """Generate a single personalised email without sending it."""
    logger.info("Generating email for: %s", prospect.email)
    research = await research_prospect(
        prospect.first_name, prospect.last_name,
        prospect.company, prospect.title, prospect.linkedin_url,
    )
    email_data = await generate_personalized_email(prospect, research, step=1)
    return EmailGenerated(
        prospect_email=str(prospect.email),
        subject=email_data["subject"],
        body=email_data["body"],
        personalization_score=email_data["personalization_score"],
    )


# --------------------------------------------------------------------------- #
# Routes – sequences                                                           #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/process-sequences")
async def trigger_sequences(background_tasks: BackgroundTasks):
    """Trigger follow-up sequence processing (gated)."""
    background_tasks.add_task(_process_sequences)
    return {"status": "sequence processing started"}


# --------------------------------------------------------------------------- #
# Routes – reply handling + Calendly                                           #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/reply-webhook")
async def handle_reply(payload: ReplyWebhook, db: Session = Depends(get_db)):
    email = str(payload.from_email).lower()
    prospect = db.query(ProspectDB).filter(ProspectDB.email == email).first()
    if prospect:
        prospect.replied = True
        log = db.query(EmailLogDB).filter(
            EmailLogDB.prospect_email == email
        ).order_by(EmailLogDB.sent_at.desc()).first()
        if log:
            log.status = "replied"
            log.replied_at = datetime.utcnow()
        db.commit()

    calendly_url = os.getenv("CALENDLY_URL", "")
    return {
        "status": "reply recorded",
        "prospect_email": email,
        "calendly_url": calendly_url or None,
    }


@app.post("/api/v1/meeting-booked")
async def meeting_booked(
    invitee_email: EmailStr,
    db: Session = Depends(get_db),
):
    email = str(invitee_email).lower()
    prospect = db.query(ProspectDB).filter(ProspectDB.email == email).first()
    if prospect:
        prospect.meeting_booked = True
        db.commit()
    return {"status": "meeting recorded", "email": email}


# --------------------------------------------------------------------------- #
# Routes – unsubscribe (CAN-SPAM / GDPR)                                      #
# --------------------------------------------------------------------------- #

@app.get("/unsubscribe")
async def unsubscribe(
    email: str = Query(..., description="Email address to unsubscribe"),
    db: Session = Depends(get_db),
):
    email = email.lower().strip()
    existing = db.query(UnsubscribeDB).filter(UnsubscribeDB.email == email).first()
    if not existing:
        db.add(UnsubscribeDB(email=email, reason="user_request"))
        db.commit()
    db.query(ProspectDB).filter(ProspectDB.email == email).update({"unsubscribed": True})
    db.commit()
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h2>You've been unsubscribed</h2>"
        "<p>You will no longer receive emails from us. "
        "This usually takes effect within 10 business days as required by CAN-SPAM.</p>"
        "</body></html>"
    )


@app.post("/api/v1/unsubscribe")
async def unsubscribe_api(
    email: EmailStr,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
):
    email_str = str(email).lower()
    existing = db.query(UnsubscribeDB).filter(UnsubscribeDB.email == email_str).first()
    if not existing:
        db.add(UnsubscribeDB(email=email_str, reason=reason))
        db.commit()
    db.query(ProspectDB).filter(ProspectDB.email == email_str).update({"unsubscribed": True})
    db.commit()
    return {"status": "unsubscribed", "email": email_str}


# --------------------------------------------------------------------------- #
# Routes – compliance (consent + review)                                       #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/consent")
async def create_consent(req: ConsentRequest, db: Session = Depends(get_db)):
    record_consent(db, str(req.email), req.source)
    return {"status": "consent_recorded", "email": str(req.email).lower()}


@app.get("/api/v1/reviews")
async def list_reviews(status: str = "pending", db: Session = Depends(get_db)):
    q = db.query(ReviewQueueDB)
    if status:
        q = q.filter(ReviewQueueDB.status == status)
    rows = q.order_by(ReviewQueueDB.created_at.desc()).limit(100).all()
    return [{
        "review_id": r.review_id,
        "prospect_email": r.prospect_email,
        "campaign_id": r.campaign_id,
        "subject": r.subject,
        "status": r.status,
        "step": r.step,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@app.post("/api/v1/reviews/{review_id}/approve")
async def approve_review_endpoint(review_id: str, reviewer: str = "operator", db: Session = Depends(get_db)):
    ok = approve_review(db, review_id, reviewer)
    if not ok:
        raise HTTPException(status_code=400, detail="Review not found or not pending")
    return {"status": "approved", "review_id": review_id}


# --------------------------------------------------------------------------- #
# Routes – stats & dashboard (stub preserved)                                  #
# --------------------------------------------------------------------------- #

@app.get("/api/v1/stats")
async def stats(db: Session = Depends(get_db)):
    return {
        "campaigns": db.query(CampaignDB).count(),
        "prospects": db.query(ProspectDB).count(),
        "emails_sent": db.query(EmailLogDB).filter(EmailLogDB.status == "sent").count(),
        "pending_reviews": db.query(ReviewQueueDB).filter(ReviewQueueDB.status == "pending").count(),
        "consents": db.query(ConsentDB).count(),
        "unsubscribes": db.query(UnsubscribeDB).count(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
