"""
AI service: OpenAI-powered email personalisation and prospect research.

Fixes the deprecated openai.ChatCompletion.create() call that broke the
original implementation (openai>=1.0 requires the new client interface).
"""
import os
import logging
from typing import Dict

from openai import OpenAI

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# OpenAI client – uses new v1 client API (fixes deprecated ChatCompletion)    #
# --------------------------------------------------------------------------- #
_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    return _client


# --------------------------------------------------------------------------- #
# Prospect research                                                            #
# --------------------------------------------------------------------------- #

async def research_prospect(
    first_name: str,
    last_name: str,
    company: str,
    title: str,
    linkedin_url: str | None = None,
) -> Dict:
    """
    Gather intelligence about a prospect.

    Production path:  call Apollo / Hunter / LinkedIn APIs using env-configured
    keys.  Falls back gracefully to a sensible stub so the pipeline still works
    during local development or when API keys are not configured.
    """
    import httpx

    research: Dict = {
        "recent_news": f"{company} is growing its team",
        "pain_points": ["manual reporting", "forecast accuracy"],
        "tech_stack": ["Salesforce", "HubSpot"],
        "hiring_for": [],
    }

    # --- Apollo.io (lead enrichment) ----------------------------------------
    apollo_key = os.getenv("APOLLO_API_KEY", "")
    if apollo_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.apollo.io/v1/people/match",
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
                    json={
                        "api_key": apollo_key,
                        "first_name": first_name,
                        "last_name": last_name,
                        "organization_name": company,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json().get("person", {})
                    org = data.get("organization", {})
                    if org.get("short_description"):
                        research["recent_news"] = org["short_description"][:200]
                    if org.get("primary_domain"):
                        research["domain"] = org["primary_domain"]
        except Exception as exc:
            logger.debug("Apollo lookup failed (non-critical): %s", exc)

    # --- Hunter.io (email verification) ------------------------------------
    hunter_key = os.getenv("HUNTER_API_KEY", "")
    if hunter_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                domain = research.get("domain", f"{company.lower().replace(' ', '')}.com")
                resp = await client.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={"domain": domain, "api_key": hunter_key, "limit": 1},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    if data.get("organization"):
                        research["verified_domain"] = data["organization"]
        except Exception as exc:
            logger.debug("Hunter lookup failed (non-critical): %s", exc)

    return research


# --------------------------------------------------------------------------- #
# Email generation                                                             #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = """You are a top-performing B2B sales development rep.
Write personalized cold emails that get 15%+ reply rates.

Guidelines:
- Keep it under 150 words total
- Reference specific research about their company
- One clear value proposition
- Soft CTA (not pushy)
- Professional but casual tone
- No buzzwords or hype
- Format exactly as:
  Subject: <subject line>

  <email body>
"""

_FOLLOW_UP_SYSTEM = """You are a B2B SDR writing a brief follow-up email.
Keep it under 80 words. Reference the prior outreach. Ask one simple question.
Format:
  Subject: Re: <subject>

  <body>
"""

_SEQUENCE_SUBJECTS = [
    "Quick question about {company}",
    "Re: Quick question about {company}",
    "Checking in – {company}",
    "Last try – {company}",
    "{first_name}, did I miss you?",
]


async def generate_personalized_email(
    prospect,
    research: Dict,
    step: int = 1,
    product_description: str = "AI-powered sales outreach that books 30+ meetings/month",
) -> Dict:
    """
    Generate a personalised email for a prospect.

    Args:
        prospect: Prospect pydantic model (or dict-like).
        research:  Dict from research_prospect().
        step:      Sequence step 1–5.
        product_description: What we're selling (configurable per campaign).

    Returns:
        Dict with keys: subject, body, personalization_score.
    """
    first_name = getattr(prospect, "first_name", prospect.get("first_name", ""))
    last_name = getattr(prospect, "last_name", prospect.get("last_name", ""))
    company = getattr(prospect, "company", prospect.get("company", ""))
    title = getattr(prospect, "title", prospect.get("title", ""))

    system = _SYSTEM_PROMPT if step == 1 else _FOLLOW_UP_SYSTEM
    user_prompt = f"""
Prospect: {first_name} {last_name}
Title: {title}
Company: {company}

Recent news: {research.get('recent_news', 'growing their team')}
Pain points: {', '.join(research.get('pain_points', ['scaling outreach']))}
Tech stack: {', '.join(research.get('tech_stack', []))}

Our product: {product_description}

Sequence step: {step} of 5
"""

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=350,
        )
        content = response.choices[0].message.content or ""
        subject, body = _parse_email_content(content, first_name, company, step)
        return {
            "subject": subject,
            "body": body,
            "personalization_score": round(0.85 + (len(research.get("recent_news", "")) / 2000), 2),
        }
    except Exception as exc:
        logger.error("OpenAI email generation failed (step %s): %s", step, exc)
        return _fallback_email(first_name, company, research, step)


def _parse_email_content(content: str, first_name: str, company: str, step: int) -> tuple[str, str]:
    """Parse subject and body from GPT response."""
    lines = [l for l in content.split("\n") if l.strip()]
    subject = ""
    body_lines = []
    in_body = False
    for line in lines:
        stripped = line.strip()
        if not subject and stripped.lower().startswith("subject:"):
            subject = stripped[len("subject:"):].strip()
        elif subject and not in_body and stripped == "":
            in_body = True
        elif subject:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # Fallback subject if parsing failed
    if not subject:
        subject = _SEQUENCE_SUBJECTS[min(step - 1, 4)].format(
            company=company, first_name=first_name
        )
    if not body:
        body = content.strip()
    return subject, body


def _fallback_email(first_name: str, company: str, research: Dict, step: int) -> Dict:
    """Return a sensible fallback email when OpenAI is unavailable."""
    subjects = _SEQUENCE_SUBJECTS
    subject = subjects[min(step - 1, 4)].format(company=company, first_name=first_name)
    if step == 1:
        body = (
            f"Hi {first_name},\n\n"
            f"Saw that {company} recently {research.get('recent_news', 'is growing')}.\n\n"
            "We help sales teams book 30+ qualified meetings per month using AI-powered outreach. "
            "Would it be worth a 15-minute call to see if this fits your team?\n\n"
            "Best,"
        )
    else:
        body = (
            f"Hi {first_name},\n\n"
            "Just wanted to resurface this – happy to share a quick case study "
            "if it'd be helpful.\n\nBest,"
        )
    return {"subject": subject, "body": body, "personalization_score": 0.72}
