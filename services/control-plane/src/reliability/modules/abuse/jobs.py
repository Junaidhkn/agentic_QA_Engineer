from __future__ import annotations

import uuid
from typing import Any

from reliability.modules.abuse import service
from reliability.modules.notifications.service import get_email_sender
from reliability.platform.jobs import job
from reliability.platform.settings import get_settings

MESSAGES = {
    "opened": (
        "Your load test is waiting for a short safety review",
        "Hi,\n\nA load test in your organization was held for a quick safety review, which we do for new "
        "organizations' first large load tests against a new host. It usually takes less than one business "
        "day; we will email you when it is done. Smaller tests and sandbox runs keep working meanwhile.\n\n"
        "If the host you are testing is not yours, please delete the target.\n\n— Reliability Engineer",
    ),
    "cleared": (
        "Safety review complete: load tests are enabled",
        "Hi,\n\nThe safety review of your organization is complete and load tests are enabled. Re-submit the "
        "held run from the dashboard.\n\n— Reliability Engineer",
    ),
    "suspended": (
        "Your organization has been suspended",
        "Hi,\n\nAfter review, your organization was suspended under our Acceptable Use Policy and no tests "
        "will run. Reply to this email if you believe this is a mistake.\n\n— Reliability Engineer",
    ),
}


@job("abuse.notify")
async def notify(payload: dict[str, Any]) -> None:
    review = await service.get(uuid.UUID(payload["review_id"]))
    subject, body = MESSAGES[payload["event"]]
    sender = get_email_sender()
    link = f"\n\n{get_settings().web_base_url}/settings"
    for email in await service.owner_emails(review.org_id):
        sender.send(email, subject, body + link)
