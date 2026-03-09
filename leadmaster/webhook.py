#!/usr/bin/env python3
"""
LeadMaster Webhook — catches GHL workflow webhooks and queues leads for processing.

GHL sends contact fields at top level, attribution under "contact",
calendar data under "calendar", and custom workflow data under "customData".

Run: python3 webhook.py
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="LeadMaster Webhook")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TASK_QUEUE = Path(__file__).parent / "tasks" / "queue.jsonl"


def _parse_ghl(body: dict) -> dict:
    """Extract lead fields from GHL's standard webhook payload.

    GHL puts contact fields at the top level (first_name, last_name, email, phone, etc.)
    and nests calendar info under body["calendar"], location under body["location"],
    attribution under body["contact"], and custom data under body["customData"].
    """
    location = body.get("location", {})
    calendar = body.get("calendar", {})
    custom = body.get("customData", {})
    attribution = body.get("contact", {}).get("attributionSource", {})

    # Determine source URL from attribution
    source_url = attribution.get("url", "")

    # Detect lead type from calendar presence or custom data
    has_calendar = bool(calendar.get("startTime"))
    custom_source = custom.get("source", "")

    if has_calendar or custom_source == "booking":
        lead_type = "appointment"
        source = "masssolar_calendar"
    elif "newenergy" in source_url.lower() or custom_source in ("form", "new-contact", "some-product"):
        lead_type = "form"
        source = "newenergy_form"
    else:
        lead_type = body.get("contact_type", "lead")
        source = body.get("contact_source", custom_source or "unknown")

    lead = {
        "first_name": body.get("first_name", ""),
        "last_name": body.get("last_name", ""),
        "full_name": body.get("full_name", ""),
        "email": body.get("email", ""),
        "phone": body.get("phone", ""),
        "country": body.get("country", ""),
        "timezone": body.get("timezone", ""),
        "city": location.get("city", ""),
        "state": location.get("state", ""),
        "zip": location.get("postalCode", ""),
        "source": source,
        "source_url": source_url,
        "contact_source": body.get("contact_source", ""),
        "tags": body.get("tags", ""),
        "type": lead_type,
        "ghl_contact_id": body.get("contact_id", ""),
        "ghl_location_id": location.get("id", ""),
        "workflow": body.get("workflow", {}).get("name", ""),
        "product_id": custom.get("product_id", custom.get("product", "")),
        "custom_data": custom,
    }

    # Calendar fields (appointment bookings) — convert to ET
    if calendar:
        cal_tz_name = calendar.get("selectedTimezone", "America/New_York")
        et = ZoneInfo("America/New_York")
        for field in ("startTime", "endTime"):
            raw = calendar.get(field, "")
            if raw:
                try:
                    dt = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(cal_tz_name))
                    raw = dt.astimezone(et).strftime("%Y-%m-%d %I:%M %p ET")
                except Exception:
                    pass
            key = "appointment_time" if field == "startTime" else "appointment_end"
            lead[key] = raw
        lead["appointment_status"] = calendar.get("appoinmentStatus", calendar.get("status", ""))
        lead["calendar_name"] = calendar.get("calendarName", "")
        lead["appointment_notes"] = calendar.get("notes", "")

    return lead


def queue_lead(lead_data: dict) -> None:
    """Append a lead to the task queue for LeadMaster to process."""
    TASK_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    task = {
        "type": "new_lead",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": lead_data,
        "status": "pending",
    }
    with open(TASK_QUEUE, "a") as f:
        f.write(json.dumps(task) + "\n")
    log.info(f"Queued lead: {lead_data.get('first_name', '?')} {lead_data.get('last_name', '')} "
             f"from {lead_data.get('source', '?')} [{lead_data.get('type')}]")


@app.post("/lead")
async def receive_lead(request: Request):
    """Receive a lead from GHL webhook (generic — auto-detects type)."""
    body = await request.json()
    log.info(f"RAW LEAD PAYLOAD: {json.dumps(body, indent=2)}")
    lead = _parse_ghl(body)
    queue_lead(lead)
    return {"status": "queued", "source": lead["source"], "type": lead["type"]}


@app.post("/lead/appointment")
async def receive_appointment(request: Request):
    """Dedicated endpoint for calendar bookings — always high intent."""
    body = await request.json()
    log.info(f"RAW APPOINTMENT PAYLOAD: {json.dumps(body, indent=2)}")
    lead = _parse_ghl(body)
    # Force appointment type regardless of detection
    lead["type"] = "appointment"
    lead["source"] = "masssolar_calendar"
    queue_lead(lead)
    return {"status": "queued", "source": "masssolar_calendar", "intent": "hot"}


@app.get("/leads/pending")
async def pending_leads():
    """View pending leads in queue."""
    if not TASK_QUEUE.exists():
        return {"pending": [], "count": 0}
    leads = []
    for line in TASK_QUEUE.read_text().strip().split("\n"):
        if not line:
            continue
        task = json.loads(line)
        if task.get("status") == "pending":
            leads.append(task)
    return {"pending": leads, "count": len(leads)}


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "leadmaster"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
