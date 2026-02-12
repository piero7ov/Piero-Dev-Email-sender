import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_html_template(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def minify_html_safe(html: str) -> str:
    """
    Minificado suave (seguro para emails):
    - Quita líneas vacías
    - Recorta espacios al inicio/fin de cada línea
    - Reduce múltiples espacios/tabulaciones a 1
    """
    lines = [line.strip() for line in html.splitlines() if line.strip()]
    html = "\n".join(lines)
    html = re.sub(r"[ \t]{2,}", " ", html)
    return html


def create_message(cfg: dict) -> EmailMessage:
    email_cfg = cfg["email"]

    msg = EmailMessage()
    msg["From"] = email_cfg["from"]

    to_value = email_cfg["to"]
    msg["To"] = ", ".join(to_value) if isinstance(to_value, list) else to_value

    msg["Subject"] = email_cfg["subject"]

    # Texto plano corto (fallback)
    msg.set_content(email_cfg.get("body_text", "Este correo contiene contenido HTML."))

    # HTML
    html_path = Path(email_cfg["html_template"]).resolve()
    html_raw = load_html_template(html_path)

    # IMPORTANTE: no base64, y minificado suave para evitar “clip” en Gmail
    html_final = minify_html_safe(html_raw)

    msg.add_alternative(html_final, subtype="html")

    # Debug útil (puedes borrarlo luego)
    print("HTML bytes:", len(html_final.encode("utf-8")))
    print("Email total bytes:", len(msg.as_bytes()))

    return msg


def send_email(cfg: dict) -> None:
    smtp_cfg = cfg["smtp"]
    host = smtp_cfg["host"]
    port = smtp_cfg["port"]
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_tls = smtp_cfg.get("use_tls", True)

    msg = create_message(cfg)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        if use_tls:
            server.starttls(context=context)
            server.ehlo()
        server.login(user, password)
        server.send_message(msg)

    print("Correo enviado.")


if __name__ == "__main__":
    config = load_config("config2.json")
    send_email(config)
