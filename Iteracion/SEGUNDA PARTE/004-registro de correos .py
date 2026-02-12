import json
import smtplib
import ssl
import mimetypes
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup  # pip install beautifulsoup4


# ---------- Config & template loading ----------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_html_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------- Logging ----------

def get_log_path(cfg: dict) -> Path:
    email_cfg = cfg.get("email", {})
    log_file = email_cfg.get("log_file", "sent_emails.log")
    return Path(log_file).resolve()


def log_email_result(cfg: dict, to_addr: str, subject: str, success: bool, message: str = "") -> None:
    log_path = get_log_path(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if success else "ERROR"
    line = f"{timestamp} ; {to_addr} ; {subject} ; {status} ; {message}\n"

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[WARN] Could not write log file {log_path}: {e}")


# ---------- Scan HTML and prepare CIDs ----------

def prepare_html_and_attachments(html: str, base_dir: Path):
    """
    - Parses HTML.
    - For each local <img src="...">:
        * generates a CID
        * replaces src with cid:...
        * records file path + mime + cid
    Returns:
        html_final (str), attachments (list of dicts)
    """
    soup = BeautifulSoup(html, "html.parser")
    attachments = []
    used = {}

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        if src.startswith(("http://", "https://", "cid:", "data:")):
            continue

        img_path = Path(src)
        if not img_path.is_absolute():
            img_path = (base_dir / src).resolve()

        if not img_path.exists():
            print(f"[WARN] Image not found, leaving as is: {src}")
            continue

        if src in used:
            cid = used[src]["cid"]
        else:
            cid = make_msgid()  # with < >
            mime_type, _ = mimetypes.guess_type(str(img_path))
            if mime_type is None:
                maintype, subtype = "application", "octet-stream"
            else:
                maintype, subtype = mime_type.split("/", 1)

            info = {
                "cid": cid,
                "path": img_path,
                "maintype": maintype,
                "subtype": subtype,
                "filename": img_path.name,
            }
            attachments.append(info)
            used[src] = info

            print(f"[OK] Embedded image {src} as CID {cid}")

        img["src"] = f"cid:{cid[1:-1]}"  # remove < >

    return str(soup), attachments


def attach_related_images(html_part, attachments):
    """
    Attaches files in `attachments` as inline related parts to `html_part`.
    """
    for att in attachments:
        with open(att["path"], "rb") as f:
            data = f.read()

        html_part.add_related(
            data,
            maintype=att["maintype"],
            subtype=att["subtype"],
            cid=att["cid"],
            filename=att["filename"],
        )


# ---------- Message creation ----------

def create_message(cfg: dict) -> EmailMessage:
    email_cfg = cfg["email"]

    msg = EmailMessage()
    msg["From"] = email_cfg["from"]

    to_field = email_cfg["to"]
    if isinstance(to_field, list):
        msg["To"] = ", ".join(to_field)
        to_str = ", ".join(to_field)
    else:
        msg["To"] = to_field
        to_str = to_field

    msg["X-Original-To"] = to_str
    msg["Subject"] = email_cfg["subject"]

    body_text = email_cfg.get("body_text", "This email contains HTML content.")
    msg.set_content(body_text)

    html_file = Path(email_cfg["html_template"]).resolve()
    html_raw = load_html_template(str(html_file))
    base_dir = html_file.parent

    # 1) Prepare HTML (with cid:...) and list of attachments
    html_final, attachments = prepare_html_and_attachments(html_raw, base_dir)

    # 2) Add HTML alternative with final HTML
    msg.add_alternative(html_final, subtype="html")

    # 3) Get the HTML part and attach images as related
    html_part = msg.get_payload()[-1]  # text/html part
    attach_related_images(html_part, attachments)

    return msg


# ---------- Sending ----------

def send_email(cfg: dict) -> None:
    smtp_cfg = cfg["smtp"]
    msg = create_message(cfg)

    host = smtp_cfg["host"]
    port = smtp_cfg["port"]
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_tls = smtp_cfg.get("use_tls", True)

    to_for_log = msg.get("X-Original-To", msg["To"])
    subject_for_log = msg["Subject"]

    try:
        if use_tls and port == 587:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
        elif port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as server:
                server.login(user, password)
                server.send_message(msg)

        print("Email enviado.")
        log_email_result(cfg, to_for_log, subject_for_log, True, "Sent successfully")

    except Exception as e:
        error_text = str(e)
        print(f"[ERROR] Sending email failed: {error_text}")
        log_email_result(cfg, to_for_log, subject_for_log, False, error_text)


if __name__ == "__main__":
    config_path = Path("config2.json")
    cfg = load_config(config_path)
    send_email(cfg)

