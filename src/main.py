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
  6. Compliance       – CAN-SPAM / GDPR unsubscribe
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
)
from src.ai_service import research_prospect, generate_personalized_email
from src.email_service import send_email
from src import billing

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


# --------------------------------------------------------------------------- #
# Background: campaign processing                                              #
# --------------------------------------------------------------------------- #

# Sequence delays in days: touch 1 immediate, touch 2 after 3d, etc.
SEQUENCE_DELAYS = [0, 3, 7, 14, 21]


async def _process_campaign(campaign_id: str, product_description: str):
    """Background task: generate + send step-1 emails for every prospect."""
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
        logger.info("Campaign %s: sent %s emails", campaign_id, sent_today)
    finally:
        db.close()


async def _process_sequences():
    """
    Process follow-up sequences for all active campaigns.
    Should be called periodically (e.g. via /api/v1/process-sequences or a cron job).
    """
    from src.database import SessionLocal
    db = SessionLocal()
    try:
        unsubscribed_emails = {r.email for r in db.query(UnsubscribeDB.email).all()}
        now = datetime.utcnow()

        # Find prospects eligible for the next touch
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
    }


# --------------------------------------------------------------------------- #
# Routes – campaigns                                                           #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/campaigns")
@app.post("/api/campaign")          # alias requested in problem statement
async def create_campaign(
    campaign: CampaignCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create an outreach campaign and immediately kick off step-1 sending."""
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
        "estimated_completion": "2 hours",
        "estimated_meetings": int(len(campaign.prospects) * 0.18 * 0.3),
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
    """
    Trigger follow-up sequence processing.
    Call this endpoint from a Railway / GitHub Actions cron job.
    """
    background_tasks.add_task(_process_sequences)
    return {"status": "sequence processing started"}


# --------------------------------------------------------------------------- #
# Routes – reply handling + Calendly                                           #
# --------------------------------------------------------------------------- #

@app.post("/api/v1/reply-webhook")
async def handle_reply(payload: ReplyWebhook, db: Session = Depends(get_db)):
    """
    Webhook called by email provider when a reply is received.
    Marks the prospect as replied and, if a Calendly link is configured,
    returns it so a follow-up can be sent with the booking link.
    """
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
    """Calendly webhook: mark prospect as meeting_booked."""
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
    """
    One-click unsubscribe link (CAN-SPAM § 5, GDPR Art. 21).
    Linked from every email footer.
    """
    email = email.lower().strip()
    # Add to global unsubscribe list
    existing = db.query(UnsubscribeDB).filter(UnsubscribeDB.email == email).first()
    if not existing:
        db.add(UnsubscribeDB(email=email, reason="user_request"))
        db.commit()
    # Also mark any prospect records
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
    """Programmatic unsubscribe (for GDPR deletion requests)."""
    email_str = str(email).lower()
    existing = db.query(UnsubscribeDB).filter(UnsubscribeDB.email == email_str).first()
    if not existing:
        db.add(UnsubscribeDB(email=email_str, reason=reason))
        db.commit()
    db.query(ProspectDB).filter(ProspectDB.email == email_str).update({"unsubscribed": True})
    db.commit()
    return {"status": "unsubscribed", "email": email_str}


# --------------------------------------------------------------------------- #
# Routes – stats & dashboard                                                   #
# --------------------------------------------------------------------------- #

@app.get("/api/v1/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Return real-time statistics from the database."""
    today_start = datetime.combine(date.today(), datetime.min.time())

    emails_sent_today = db.query(EmailLogDB).filter(
        EmailLogDB.sent_at >= today_start,
        EmailLogDB.status.in_(["sent", "opened", "replied"]),
    ).count()

    replies_today = db.query(EmailLogDB).filter(
        EmailLogDB.replied_at >= today_start,
    ).count()

    meetings_today = db.query(ProspectDB).filter(
        ProspectDB.meeting_booked.is_(True),
        ProspectDB.last_contacted >= today_start,
    ).count()

    active_campaigns = db.query(CampaignDB).filter(CampaignDB.status == "active").count()

    total_sent = db.query(EmailLogDB).filter(
        EmailLogDB.status.in_(["sent", "opened", "replied"])
    ).count()
    total_replied = db.query(EmailLogDB).filter(EmailLogDB.status == "replied").count()
    reply_rate = round(total_replied / total_sent * 100, 1) if total_sent else 0.0

    monthly_revenue = db.query(SubscriptionDB).filter(
        SubscriptionDB.status == "active"
    ).with_entities(
        SubscriptionDB.monthly_revenue
    ).all()
    monthly_revenue_total = sum(r[0] for r in monthly_revenue)

    return {
        "emails_sent_today": emails_sent_today,
        "replies_today": replies_today,
        "reply_rate": f"{reply_rate}%",
        "meetings_booked_today": meetings_today,
        "active_campaigns": active_campaigns,
        "monthly_revenue": monthly_revenue_total,
        "total_emails_sent": total_sent,
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(db: Session = Depends(get_db)):
    """Simple HTML dashboard showing key metrics."""
    stats = await get_stats(db)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Outreach Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f172a; color: #e2e8f0; padding: 40px 20px; }}
    h1 {{ font-size: 2rem; margin-bottom: 8px; color: #38bdf8; }}
    .sub {{ color: #94a3b8; margin-bottom: 40px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 24px; }}
    .card {{ background: #1e293b; border-radius: 16px; padding: 28px 24px;
             border: 1px solid #334155; }}
    .card .label {{ font-size: 0.85rem; color: #64748b; text-transform: uppercase;
                    letter-spacing: .05em; margin-bottom: 10px; }}
    .card .value {{ font-size: 2.5rem; font-weight: 700; color: #38bdf8; }}
    .card .unit  {{ font-size: 1rem; color: #94a3b8; margin-left: 4px; }}
    .revenue .value {{ color: #4ade80; }}
    footer {{ margin-top: 48px; color: #475569; font-size: 0.8rem; text-align: center; }}
  </style>
</head>
<body>
  <h1>📊 Outreach Dashboard</h1>
  <p class="sub">Real-time metrics — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
  <div class="grid">
    <div class="card">
      <div class="label">Emails Sent Today</div>
      <div class="value">{stats['emails_sent_today']:,}</div>
    </div>
    <div class="card">
      <div class="label">Open Rate</div>
      <div class="value">—</div>
    </div>
    <div class="card">
      <div class="label">Reply Rate</div>
      <div class="value">{stats['reply_rate']}</div>
    </div>
    <div class="card">
      <div class="label">Meetings Booked Today</div>
      <div class="value">{stats['meetings_booked_today']}</div>
    </div>
    <div class="card">
      <div class="label">Active Campaigns</div>
      <div class="value">{stats['active_campaigns']}</div>
    </div>
    <div class="card revenue">
      <div class="label">Monthly Revenue</div>
      <div class="value">${stats['monthly_revenue']:,.0f}</div>
    </div>
  </div>
  <footer>Automated Sales Outreach v2.0 · CAN-SPAM &amp; GDPR compliant</footer>
</body>
</html>"""
    return HTMLResponse(content=html)


# --------------------------------------------------------------------------- #
# Routes – billing (Stripe)                                                    #
# --------------------------------------------------------------------------- #

@app.get("/api/v1/billing/plans")
async def list_plans():
    return {
        plan: {
            "display": info["display"],
            "daily_limit": info["daily_limit"],
            "monthly_price": info["monthly_price"],
        }
        for plan, info in billing.PLANS.items()
    }


@app.post("/api/v1/billing/checkout")
async def create_checkout(req: CheckoutRequest, db: Session = Depends(get_db)):
    """Create a Stripe Checkout session and return the redirect URL."""
    try:
        result = billing.create_checkout_session(
            user_email=str(req.user_email),
            plan=req.plan,
            success_url=req.success_url,
            cancel_url=req.cancel_url,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/v1/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe sends subscription lifecycle events here."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = billing.handle_webhook(payload, sig_header)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        plan = data.get("metadata", {}).get("plan", "starter")
        user_email = data.get("customer_email", "")
        plan_info = billing.PLANS.get(plan, billing.PLANS["starter"])

        existing = db.query(SubscriptionDB).filter(SubscriptionDB.user_email == user_email).first()
        if existing:
            existing.plan = plan
            existing.daily_limit = plan_info["daily_limit"]
            existing.monthly_revenue = plan_info["monthly_price"]
            existing.status = "active"
            existing.stripe_subscription_id = data.get("subscription")
        else:
            db.add(SubscriptionDB(
                user_email=user_email,
                stripe_customer_id=data.get("customer"),
                stripe_subscription_id=data.get("subscription"),
                plan=plan,
                daily_limit=plan_info["daily_limit"],
                monthly_revenue=plan_info["monthly_price"],
                status="active",
            ))
        db.commit()

    elif event_type in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub_id = data.get("id")
        sub_record = db.query(SubscriptionDB).filter(
            SubscriptionDB.stripe_subscription_id == sub_id
        ).first()
        if sub_record:
            sub_record.status = data.get("status", "canceled")
            db.commit()

    return {"received": True}


@app.post("/api/v1/billing/cancel")
async def cancel_subscription(
    user_email: EmailStr,
    db: Session = Depends(get_db),
):
    """Cancel a user's Stripe subscription."""
    record = db.query(SubscriptionDB).filter(
        SubscriptionDB.user_email == str(user_email)
    ).first()
    if not record or not record.stripe_subscription_id:
        raise HTTPException(status_code=404, detail="No active subscription found")
    try:
        billing.cancel_subscription(record.stripe_subscription_id)
        record.status = "canceled"
        db.commit()
        return {"status": "canceled", "email": str(user_email)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
