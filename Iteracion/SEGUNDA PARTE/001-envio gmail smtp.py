import json
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def create_message(cfg: dict) -> EmailMessage:
    email_cfg = cfg["email"]

    msg = EmailMessage()
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(email_cfg["to"])
    msg["Subject"] = email_cfg["subject"]
    msg.set_content(email_cfg["body"])

    return msg


def send_email(cfg: dict) -> None:
    smtp_cfg = cfg["smtp"]
    msg = create_message(cfg)

    host = smtp_cfg["host"]
    port = smtp_cfg["port"]
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_tls = smtp_cfg.get("use_tls", True)

    if use_tls and port == 587:
        # STARTTLS
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
    elif port == 465:
        # SSL
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        # Plain (no TLS) – only for testing in trusted environments
        with smtplib.SMTP(host, port) as server:
            server.login(user, password)
            server.send_message(msg)

    print("Email sent.")


if __name__ == "__main__":
    config_path = Path("config.json")
    cfg = load_config(config_path)
    send_email(cfg)

