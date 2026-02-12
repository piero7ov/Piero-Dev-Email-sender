#!/usr/bin/env python3
import json
import smtplib
import ssl
import mimetypes
import random
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup  # pip install beautifulsoup4

# Timezone (Python 3.9+)
# pip install tzdata
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# QR (pip install qrcode[pil])
try:
    import qrcode
except ImportError:
    qrcode = None


# ============================================================
# BASE DIR (para que funcione sin importar desde dónde ejecutes)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


# ---------- Config & template loading ----------

def load_config(path: str | Path) -> dict:
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_html_template(path: str | Path) -> str:
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------- Logging ----------

def get_log_path(cfg: dict) -> Path:
    email_cfg = cfg.get("email", {})
    log_file = email_cfg.get("log_file", "sent_emails.log")
    p = Path(log_file)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


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


# ---------- Helpers app ----------

def get_app_cfg(cfg: dict) -> dict:
    return cfg.get("app", {})


def now_local(cfg: dict) -> datetime:
    tz_name = get_app_cfg(cfg).get("timezone", "Europe/Madrid")
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo(tz_name))


def get_queue_path(cfg: dict) -> Path:
    qfile = get_app_cfg(cfg).get("queue_file", "queue.json")
    p = Path(qfile)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def load_queue(cfg: dict) -> dict:
    qp = get_queue_path(cfg)
    if not qp.exists():
        return {"jobs": []}
    with open(qp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(cfg: dict, queue_data: dict) -> None:
    qp = get_queue_path(cfg)
    qp.parent.mkdir(parents=True, exist_ok=True)

    tmp = qp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, ensure_ascii=False, indent=2)
    tmp.replace(qp)


def parse_scheduled_for(cfg: dict) -> str:
    """
    Lee email.scheduled_for con formato: "YYYY-MM-DD HH:MM"
    y devuelve ISO con timezone si está disponible.
    """
    email_cfg = cfg.get("email", {})
    raw = (email_cfg.get("scheduled_for") or "").strip()
    if not raw:
        raise ValueError("Falta email.scheduled_for en config (ej: '2026-02-12 19:30').")

    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")

    tz_name = get_app_cfg(cfg).get("timezone", "Europe/Madrid")
    if ZoneInfo is not None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))

    return dt.isoformat()


def new_job_id() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rnd = random.randint(1000, 9999)
    return f"job_{ts}_{rnd}"


def get_recipients(cfg: dict) -> list[str]:
    to_field = cfg["email"]["to"]
    if isinstance(to_field, list):
        return [t.strip() for t in to_field if str(t).strip()]
    return [str(to_field).strip()] if str(to_field).strip() else []


# ---------- PS ----------

def pick_random_ps(cfg: dict) -> str:
    ps_cfg = cfg.get("ps", {})
    if not ps_cfg.get("enabled", False):
        return ""

    phrases = ps_cfg.get("phrases", [])
    if not phrases:
        return ""

    prefix = ps_cfg.get("prefix", "P.D.:")
    phrase = random.choice(phrases).strip()
    if not phrase:
        return ""

    return f"{prefix} {phrase}"


def apply_ps_to_text(body_text: str, ps_line: str, cfg: dict) -> str:
    if not ps_line:
        return body_text

    ps_cfg = cfg.get("ps", {})
    if not ps_cfg.get("add_to_text", True):
        return body_text

    body_text = (body_text or "").rstrip()
    return body_text + "\n\n" + ps_line


def apply_ps_to_html(html: str, ps_line: str, cfg: dict) -> str:
    if not ps_line:
        return html

    ps_cfg = cfg.get("ps", {})
    if not ps_cfg.get("add_to_html", True):
        return html

    if "{{PS}}" in html:
        return html.replace("{{PS}}", ps_line)

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is None:
        return html

    style = ps_cfg.get(
        "html_style",
        "margin:14px 0 0; padding:12px 12px; border-radius:12px; "
        "background:#f8fafc; border:1px solid #e2e8f0; "
        "color:#334155; font-size:11px; line-height:16px;"
    )

    p = soup.new_tag("p")
    p["style"] = style
    p.string = ps_line

    body.append(p)
    return str(soup)


# ---------- QR ----------

def ensure_portfolio_qr(cfg: dict, html_base_dir: Path) -> str | None:
    qr_cfg = cfg.get("qr", {})
    if not qr_cfg.get("enabled", False):
        return None

    if qrcode is None:
        print("[WARN] QR enabled pero falta 'qrcode'. Instala: pip install qrcode[pil]")
        return None

    url = (qr_cfg.get("url") or "").strip()
    if not url:
        url = (cfg.get("vcard", {}).get("portfolio") or "").strip()

    if not url:
        print("[WARN] QR enabled pero no hay URL en qr.url ni en vcard.portfolio")
        return None

    out_dir = (qr_cfg.get("output_dir") or "generated").strip()
    filename = (qr_cfg.get("filename") or "qr_portfolio.png").strip()
    box_size = int(qr_cfg.get("box_size", 8))
    border = int(qr_cfg.get("border", 2))

    out_path = (html_base_dir / out_dir / filename).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img.save(out_path)

    rel_src = out_path.relative_to(html_base_dir)
    rel_src = str(rel_src).replace("\\", "/")
    print(f"[OK] QR generado: {rel_src} -> {url}")
    return rel_src


def inject_qr_placeholder(html: str, qr_src: str | None) -> str:
    if "{{QR_PORTFOLIO_SRC}}" not in html:
        return html
    if qr_src:
        return html.replace("{{QR_PORTFOLIO_SRC}}", qr_src)
    return html.replace("{{QR_PORTFOLIO_SRC}}", "https://piero7ov.github.io/Portafolio/")


# ---------- CID embedding ----------

def prepare_html_and_attachments(html: str, base_dir: Path):
    soup = BeautifulSoup(html, "html.parser")
    inline_attachments = []
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
            cid = make_msgid()
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
            inline_attachments.append(info)
            used[src] = info
            print(f"[OK] Embedded image {src} as CID {cid}")

        img["src"] = f"cid:{cid[1:-1]}"

    return str(soup), inline_attachments


def attach_related_images(html_part, attachments):
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


# ---------- Adjuntos ----------

def load_file_attachments_from_config(cfg: dict, adjuntos_dir: Path | None = None):
    email_cfg = cfg.get("email", {})
    filenames = email_cfg.get("attachments", [])
    if not filenames:
        return []

    if adjuntos_dir is None:
        adjuntos_dir = BASE_DIR / "adjuntos"

    file_attachments = []
    for name in filenames:
        path = (adjuntos_dir / name).resolve()
        if not path.exists():
            print(f"[WARN] Attachment not found: {path}")
            continue

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            maintype, subtype = "application", "octet-stream"
        else:
            maintype, subtype = mime_type.split("/", 1)

        file_attachments.append(
            {"path": path, "maintype": maintype, "subtype": subtype, "filename": path.name}
        )
        print(f"[OK] Prepared file attachment: {path.name}")

    return file_attachments


def attach_files(msg: EmailMessage, file_attachments):
    for att in file_attachments:
        with open(att["path"], "rb") as f:
            data = f.read()

        msg.add_attachment(
            data,
            maintype=att["maintype"],
            subtype=att["subtype"],
            filename=att["filename"],
        )


# ---------- vCard ----------

def build_vcard(full_name: str, title: str, email: str, portfolio: str, github: str,
                phone: str | None = None, location: str | None = None) -> bytes:
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{full_name}",
        f"TITLE:{title}",
        f"EMAIL;TYPE=INTERNET:{email}",
        f"URL:{portfolio}",
        f"X-SOCIALPROFILE;TYPE=github:{github}",
    ]
    if phone:
        lines.append(f"TEL;TYPE=CELL:{phone}")
    if location:
        lines.append(f"NOTE:Ubicación - {location}")
    lines.append("END:VCARD")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


def attach_vcard(msg: EmailMessage, cfg: dict) -> None:
    email_cfg = cfg.get("email", {})
    vcfg = cfg.get("vcard", {})

    if not vcfg.get("enabled", True):
        print("[INFO] vCard disabled in config.")
        return

    full_name = vcfg.get("full_name", "Piero Olivares")
    title = vcfg.get("title", "Dev Free Agent")
    email = vcfg.get("email", email_cfg.get("from", ""))
    portfolio = vcfg.get("portfolio", "https://piero7ov.github.io/Portafolio/")
    github = vcfg.get("github", "https://github.com/piero7ov")
    phone = vcfg.get("phone", None)
    location = vcfg.get("location", "Valencia, España")
    filename = vcfg.get("filename", "PIERODEV.vcf")

    if isinstance(phone, str) and phone.strip() == "":
        phone = None

    vcard_bytes = build_vcard(full_name, title, email, portfolio, github, phone, location)
    msg.add_attachment(vcard_bytes, maintype="text", subtype="vcard", filename=filename)
    print(f"[OK] vCard adjunta: {filename}")


# ---------- Crear mensaje + Enviar ----------

def create_message_for_recipient(cfg: dict, recipient: str) -> EmailMessage:
    email_cfg = cfg["email"]
    ps_line = pick_random_ps(cfg)

    msg = EmailMessage()
    msg["From"] = email_cfg["from"]
    msg["To"] = recipient
    msg["X-Original-To"] = recipient
    msg["Subject"] = email_cfg["subject"]

    body_text = email_cfg.get("body_text", "This email contains HTML content.")
    body_text = apply_ps_to_text(body_text, ps_line, cfg)
    msg.set_content(body_text)

    html_template_path = Path(email_cfg["html_template"])
    if not html_template_path.is_absolute():
        html_template_path = (BASE_DIR / html_template_path)
    html_template_path = html_template_path.resolve()

    base_dir = html_template_path.parent

    html_raw = load_html_template(html_template_path)
    html_raw = apply_ps_to_html(html_raw, ps_line, cfg)

    # QR
    qr_src = ensure_portfolio_qr(cfg, base_dir)
    html_raw = inject_qr_placeholder(html_raw, qr_src)

    # CID
    html_final, inline_attachments = prepare_html_and_attachments(html_raw, base_dir)
    msg.add_alternative(html_final, subtype="html")
    html_part = msg.get_payload()[-1]
    attach_related_images(html_part, inline_attachments)

    # Adjuntos
    file_attachments = load_file_attachments_from_config(cfg, BASE_DIR / "adjuntos")
    attach_files(msg, file_attachments)

    # vCard
    attach_vcard(msg, cfg)

    # Header debug PS
    if ps_line:
        msg["X-PS-Line"] = ps_line

    return msg


def send_now(cfg: dict) -> None:
    smtp_cfg = cfg["smtp"]
    recipients = get_recipients(cfg)
    if not recipients:
        print("[ERROR] No hay destinatario en email.to")
        return

    host = smtp_cfg["host"]
    port = int(smtp_cfg["port"])
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_tls = smtp_cfg.get("use_tls", True)

    # Envío 1 por 1 (privacidad, y escalable a cola más adelante)
    for recipient in recipients:
        msg = create_message_for_recipient(cfg, recipient)
        subject_for_log = msg["Subject"]
        ps_for_log = msg.get("X-PS-Line", "")

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

            print(f"[OK] Email enviado a {recipient}")
            extra = "Sent successfully (with vCard + QR)"
            if ps_for_log:
                extra += f" | PS={ps_for_log}"
            log_email_result(cfg, recipient, subject_for_log, True, extra)

        except Exception as e:
            error_text = str(e)
            print(f"[ERROR] Sending email failed to {recipient}: {error_text}")
            log_email_result(cfg, recipient, subject_for_log, False, error_text)


# ---------- Schedule (solo encola y sale) ----------

def schedule_only(cfg: dict) -> None:
    recipients = get_recipients(cfg)
    if not recipients:
        print("[ERROR] No hay destinatario en email.to")
        return

    scheduled_iso = parse_scheduled_for(cfg)

    q = load_queue(cfg)
    jobs = q.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []

    for r in recipients:
        job = {
            "id": new_job_id(),
            "to": r,
            "scheduled_for": scheduled_iso,
            "status": "pending",
            "created_at": now_local(cfg).isoformat(),
            "subject": cfg.get("email", {}).get("subject", ""),
            "template": cfg.get("email", {}).get("html_template", ""),
            "note": "Encolado desde config8.json (modo schedule)"
        }
        jobs.append(job)
        print(f"[OK] Encolado: {r} @ {scheduled_iso}")

    q["jobs"] = jobs
    save_queue(cfg, q)
    print(f"[INFO] Cola guardada en: {get_queue_path(cfg)}")
    print("[INFO] (modo schedule) No se envió ningún correo. Solo se encoló.")


# ---------- Main (FIJO: config8.json) ----------

if __name__ == "__main__":
    config_path = (BASE_DIR / "config8.json").resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"No existe config8.json en: {config_path}")

    cfg = load_config(config_path)
    mode = get_app_cfg(cfg).get("mode", "send_now").strip().lower()

    if mode == "send_now":
        send_now(cfg)
    elif mode == "schedule":
        schedule_only(cfg)
    else:
        print(f"[ERROR] app.mode inválido: {mode}. Usa: send_now | schedule")
