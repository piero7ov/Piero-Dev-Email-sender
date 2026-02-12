#!/usr/bin/env python3
import json
import time
import ssl
import smtplib
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta

# Timezone (Python 3.9+) + tzdata
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================
# BASE DIR
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# Cargar tu script 010 como módulo (aunque tenga espacios)
# ============================================================
SENDER_SCRIPT = BASE_DIR / "010- programamos envio o envio inmediato.py"

if not SENDER_SCRIPT.exists():
    raise FileNotFoundError(f"No existe el script 010 en: {SENDER_SCRIPT}")

spec = importlib.util.spec_from_file_location("sender010", str(SENDER_SCRIPT))
sender = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sender)


# ============================================================
# Helpers worker
# ============================================================

def safe_tz(tz_name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def now_local(cfg: dict) -> datetime:
    tz_name = cfg.get("app", {}).get("timezone", "Europe/Madrid")
    tz = safe_tz(tz_name)
    return datetime.now(tz) if tz else datetime.now()


def queue_path(cfg: dict) -> Path:
    qfile = cfg.get("app", {}).get("queue_file", "queue.json")
    p = Path(qfile)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def load_queue(qp: Path) -> dict:
    if not qp.exists():
        return {"jobs": []}
    with open(qp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(qp: Path, data: dict) -> None:
    qp.parent.mkdir(parents=True, exist_ok=True)
    tmp = qp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(qp)


def parse_job_dt(raw: str) -> datetime | None:
    """
    Acepta:
    - ISO con offset: 2026-02-12T02:48:00+01:00
    - ISO naive:      2026-02-12T02:48:00
    - formato:        2026-02-12 02:48
    """
    if not raw:
        return None

    raw = raw.strip()

    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass

    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except Exception:
        return None


def is_due(job_dt: datetime, now_dt: datetime) -> bool:
    if job_dt.tzinfo is None and now_dt.tzinfo is not None:
        return job_dt <= now_dt.replace(tzinfo=None)
    if job_dt.tzinfo is not None and now_dt.tzinfo is None:
        return job_dt.replace(tzinfo=None) <= now_dt
    return job_dt <= now_dt


def bump_retry(cfg: dict, job: dict, err: str) -> None:
    app = cfg.get("app", {})
    max_retries = int(app.get("max_retries", 2))
    retry_delay = int(app.get("retry_delay_seconds", 300))

    attempts = int(job.get("attempts", 0)) + 1
    job["attempts"] = attempts
    job["last_error"] = err

    if attempts > max_retries:
        job["status"] = "failed"
        job["failed_at"] = now_local(cfg).isoformat()
        return

    nxt = now_local(cfg) + timedelta(seconds=retry_delay)
    job["scheduled_for"] = nxt.isoformat()
    job["status"] = "pending"


def send_job(cfg: dict, to_addr: str, subject_override: str | None = None, template_override: str | None = None,
             theme_index_override: int | None = None) -> None:
    """
    Envía 1 correo a 1 destinatario usando el constructor del 010.
    Respeta theme_index del job para que el tema sea consistente.
    """
    smtp_cfg = cfg["smtp"]
    host = smtp_cfg["host"]
    port = int(smtp_cfg["port"])
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_tls = smtp_cfg.get("use_tls", True)

    email_cfg = cfg.get("email", {})
    old_subject = email_cfg.get("subject", "")
    old_template = email_cfg.get("html_template", "")

    if subject_override:
        email_cfg["subject"] = subject_override
    if template_override:
        email_cfg["html_template"] = template_override

    try:
        msg = sender.create_message_for_recipient(cfg, to_addr, theme_index_override=theme_index_override)

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

        extra = "Sent from worker (queue) | with vCard + QR"
        theme = msg.get("X-Theme-Name", "")
        if theme:
            extra += f" | THEME={theme}"
        ps = msg.get("X-PS-Line", "")
        if ps:
            extra += f" | PS={ps}"

        sender.log_email_result(cfg, to_addr, msg["Subject"], True, extra)

    finally:
        email_cfg["subject"] = old_subject
        email_cfg["html_template"] = old_template


# ============================================================
# MAIN Worker Loop
# ============================================================

if __name__ == "__main__":
    cfg_path = BASE_DIR / "config8.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No existe config8.json en: {cfg_path}")

    cfg = sender.load_config(cfg_path)

    qp = queue_path(cfg)
    tick = int(cfg.get("app", {}).get("tick_seconds", 5))
    rate_limit = int(cfg.get("app", {}).get("rate_limit_seconds", 15))

    print(f"[INFO] Worker activo. Cola: {qp}")
    print(f"[INFO] tick={tick}s | rate_limit={rate_limit}s | Ctrl+C para parar")

    while True:
        q = load_queue(qp)
        jobs = q.get("jobs", [])
        if not isinstance(jobs, list):
            jobs = []

        now_dt = now_local(cfg)
        changed = False

        def job_sort_key(j):
            dt = parse_job_dt(j.get("scheduled_for", ""))
            return dt or datetime.max

        jobs.sort(key=job_sort_key)

        for job in jobs:
            if job.get("status", "pending") != "pending":
                continue

            to_addr = (job.get("to") or "").strip()
            if not to_addr:
                job["status"] = "failed"
                job["last_error"] = "Missing 'to'"
                changed = True
                continue

            job_dt = parse_job_dt(job.get("scheduled_for", ""))
            if job_dt is None:
                job["status"] = "failed"
                job["last_error"] = "Invalid 'scheduled_for'"
                changed = True
                continue

            if not is_due(job_dt, now_dt):
                continue

            subject_override = job.get("subject") or None
            template_override = job.get("template") or None
            theme_index_override = job.get("theme_index", None)

            print(f"[DUE] {to_addr} (job={job.get('id')}) -> enviando...")

            try:
                send_job(cfg, to_addr, subject_override, template_override, theme_index_override)
                job["status"] = "sent"
                job["sent_at"] = now_local(cfg).isoformat()
                job["last_error"] = ""
                changed = True
                print(f"[OK] Enviado a {to_addr} | THEME={job.get('theme_name','')}")

            except Exception as e:
                err = str(e)
                print(f"[ERROR] Falló {to_addr}: {err}")
                bump_retry(cfg, job, err)
                changed = True

            save_queue(qp, {"jobs": jobs})
            time.sleep(rate_limit)

        if changed:
            save_queue(qp, {"jobs": jobs})

        time.sleep(tick)
