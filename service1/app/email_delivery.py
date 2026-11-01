import json
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol


class Mailer(Protocol):
    available: bool

    def send_registration(self, email: str, full_name: str, verification_link: str | None) -> None: ...
    def send_password_reset(self, email: str, full_name: str, reset_link: str) -> None: ...
    def send_organization_invitation(self, email: str, organization_name: str, invitation_link: str) -> None: ...


class DevelopmentMailbox:
    """Local-only mail sink; stores links in an ignored file for manual development checks."""

    available = True

    def __init__(self, path: Path) -> None:
        self.path = path

    def send_registration(self, email: str, full_name: str, verification_link: str | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        message = {
            "to": email,
            "subject": "Verify your Proofpath email" if verification_link else "Proofpath account request",
            "verification_link": verification_link,
        }
        with self.path.open("a", encoding="utf-8") as mailbox:
            mailbox.write(json.dumps(message) + "\n")

    def send_password_reset(self, email: str, full_name: str, reset_link: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        message = {
            "to": email,
            "subject": "Reset your Proofpath password",
            "password_reset_link": reset_link,
        }
        with self.path.open("a", encoding="utf-8") as mailbox:
            mailbox.write(json.dumps(message) + "\n")

    def send_organization_invitation(self, email: str, organization_name: str, invitation_link: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        message = {
            "to": email,
            "subject": f"Invitation to join {organization_name} on Proofpath",
            "organization_invitation_link": invitation_link,
        }
        with self.path.open("a", encoding="utf-8") as mailbox:
            mailbox.write(json.dumps(message) + "\n")


class SMTPMailer:
    available = True

    def __init__(self, host: str, port: int, sender: str, username: str | None, password: str | None) -> None:
        self.host, self.port, self.sender, self.username, self.password = host, port, sender, username, password

    def send_registration(self, email: str, full_name: str, verification_link: str | None) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = email
        if verification_link:
            message["Subject"] = "Verify your Proofpath email"
            message.set_content(f"Hello {full_name},\n\nVerify your email address: {verification_link}\n")
        else:
            message["Subject"] = "Proofpath account request"
            message.set_content("A registration request was received for this address. If you did not make it, you can ignore this message.\n")
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)

    def send_password_reset(self, email: str, full_name: str, reset_link: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = email
        message["Subject"] = "Reset your Proofpath password"
        message.set_content(f"Hello {full_name},\n\nReset your Proofpath password: {reset_link}\n")
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)

    def send_organization_invitation(self, email: str, organization_name: str, invitation_link: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = email
        message["Subject"] = f"Invitation to join {organization_name} on Proofpath"
        message.set_content(f"You have been invited to join {organization_name} on Proofpath.\n\nAccept the invitation: {invitation_link}\n")
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


def create_mailer(environment: str, *, mailbox_path: Path) -> Mailer:
    import os

    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if host and sender:
        return SMTPMailer(host, int(os.getenv("SMTP_PORT", "587")), sender, username, password)
    if environment == "development":
        return DevelopmentMailbox(mailbox_path)
    raise RuntimeError("SMTP_HOST and SMTP_FROM are required outside development.")
