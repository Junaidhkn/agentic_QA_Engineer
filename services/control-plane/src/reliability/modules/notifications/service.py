"""Outbound email behind an adapter (log driver locally, SES in AWS)."""

from __future__ import annotations

import logging
from typing import Protocol

from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class LogEmailSender:
    sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        LogEmailSender.sent.append((to, subject, body))
        log.info("email (log driver)", extra={"to_domain": to.split("@")[-1], "subject": subject})


class SesEmailSender:
    def __init__(self, sender: str, region: str) -> None:
        import boto3

        self._client = boto3.client("ses", region_name=region)
        self._from = sender

    def send(self, to: str, subject: str, body: str) -> None:
        self._client.send_email(
            Source=self._from,
            Destination={"ToAddresses": [to]},
            Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
        )


def get_email_sender() -> EmailSender:
    s = get_settings()
    if s.email_provider == "ses":
        return SesEmailSender(s.email_from, s.s3_region)
    return LogEmailSender()
