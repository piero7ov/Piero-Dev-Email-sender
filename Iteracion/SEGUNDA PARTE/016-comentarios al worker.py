#!/usr/bin/env python3
# ============================================================
#  WORKER DE COLA (queue.json)
# ------------------------------------------------------------
#  Qué hace este script:
#   1) Lee queue.json cada X segundos (tick_seconds)
#   2) Busca jobs con status="pending" cuyo scheduled_for ya venció
#   3) Para cada job vencido (due):
#       - Construye el email usando el script 010 como módulo
#       - Envía por SMTP
#       - Marca el job como "sent" o programa reintento
#   4) Aplica rate_limit_seconds entre envíos (para no saturar Gmail)
#
#  Diseño (idea clave):
#   - El script 013 es el "constructor" del email (QR, PS, vCard, themes, CID...).
#   - Este worker es solo el "reloj" que ejecuta envíos cuando toca.
# ============================================================

import json
import time
import ssl
import smtplib
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta

# ------------------------------------------------------------
# Timezone (Python 3.9+) + tzdata
# - En Windows a veces falta la base de zonas IANA.
# - Solución: pip install tzdata
# - Si ZoneInfo no está disponible, trabajamos sin timezone.
# ------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================
# BASE DIR
# - Para que rutas relativas funcionen aunque ejecutes el worker
#   desde otra carpeta.
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# Cargar tu script 010 como módulo (aunque tenga espacios)
# ------------------------------------------------------------
# ¿Por qué lo hacemos así?
#  - Tu script 010 ya sabe construir el EmailMessage completo
#    (PS + QR + vCard + themes + CID + adjuntos).
#  - El worker NO reimplementa eso: solo lo reutiliza.
#
# Importante:
#  - Esto permite mantener una sola fuente de verdad del email.
# ============================================================
SENDER_SCRIPT = BASE_DIR / "010- programamos envio o envio inmediato.py"

if not SENDER_SCRIPT.exists():
    raise FileNotFoundError(f"No existe el script 010 en: {SENDER_SCRIPT}")

spec = importlib.util.spec_from_file_location("sender010", str(SENDER_SCRIPT))
sender = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sender)


# ============================================================
# Helpers: timezone y fecha local
# ============================================================

def safe_tz(tz_name: str):
    """
    Intenta devolver ZoneInfo(tz_name).
    Si no hay ZoneInfo o no existe la zona -> None.
    """
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def now_local(cfg: dict) -> datetime:
    """
    Devuelve datetime.now() en timezone del config:
      cfg["app"]["timezone"] (por defecto "Europe/Madrid")

    Si no se puede usar timezone (ZoneInfo/tzdata), devuelve naive datetime.
    """
    tz_name = cfg.get("app", {}).get("timezone", "Europe/Madrid")
    tz = safe_tz(tz_name)
    return datetime.now(tz) if tz else datetime.now()


# ============================================================
# Helpers: ruta, lectura y guardado de queue.json
# ============================================================

def queue_path(cfg: dict) -> Path:
    """
    Determina dónde está queue.json.
      cfg["app"]["queue_file"] (default "queue.json")
    Si el path es relativo, lo resuelve con BASE_DIR.
    """
    qfile = cfg.get("app", {}).get("queue_file", "queue.json")
    p = Path(qfile)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def load_queue(qp: Path) -> dict:
    """
    Carga la cola desde qp.
    Si no existe, devuelve {"jobs": []}.
    """
    if not qp.exists():
        return {"jobs": []}
    with open(qp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(qp: Path, data: dict) -> None:
    """
    Guarda la cola de forma segura:
      - escribe en un .tmp
      - luego reemplaza el archivo real
    Esto reduce el riesgo de queue.json corrupto.
    """
    qp.parent.mkdir(parents=True, exist_ok=True)
    tmp = qp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(qp)


# ============================================================
# Helpers: parse de scheduled_for y comparación "due"
# ============================================================

def parse_job_dt(raw: str) -> datetime | None:
    """
    Convierte job["scheduled_for"] a datetime.

    Acepta:
      - ISO con offset: 2026-02-12T02:48:00+01:00
      - ISO naive:      2026-02-12T02:48:00
      - formato simple: 2026-02-12 02:48

    Devuelve:
      - datetime si pudo parsear
      - None si es inválido
    """
    if not raw:
        return None

    raw = raw.strip()

    # 1) ISO (soporta offsets)
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass

    # 2) Formato simple
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except Exception:
        return None


def is_due(job_dt: datetime, now_dt: datetime) -> bool:
    """
    Decide si un job está vencido (job_dt <= now_dt).

    Considera mezcla tz-aware / naive:
      - Si uno tiene tz y el otro no, compara en naive para evitar TypeError.
    """
    if job_dt.tzinfo is None and now_dt.tzinfo is not None:
        return job_dt <= now_dt.replace(tzinfo=None)
    if job_dt.tzinfo is not None and now_dt.tzinfo is None:
        return job_dt.replace(tzinfo=None) <= now_dt
    return job_dt <= now_dt


# ============================================================
# Reintentos (retry)
# ============================================================

def bump_retry(cfg: dict, job: dict, err: str) -> None:
    """
    Aplica política simple de reintento:

    Config:
      app.max_retries (default 2)
      app.retry_delay_seconds (default 300)

    Comportamiento:
      - attempts++ y last_error = err
      - si attempts > max_retries:
          status=failed + failed_at
      - si no:
          reprograma scheduled_for = now + retry_delay
          status vuelve a pending
    """
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


# ============================================================
# Enviar un job
# ============================================================

def send_job(
    cfg: dict,
    to_addr: str,
    subject_override: str | None = None,
    template_override: str | None = None,
    theme_index_override: int | None = None
) -> None:
    """
    Envía 1 correo a 1 destinatario usando el constructor del 010.

    Overriding:
      - subject_override: fuerza un subject específico desde el job
      - template_override: fuerza una plantilla específica desde el job
      - theme_index_override: asegura que se use el mismo theme elegido al encolar

    Nota importante:
      - Modificamos cfg["email"] temporalmente y lo restauramos al final.
      - Esto evita que un job afecte el siguiente.
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
        # Construcción del mensaje completa la hace el 010
        msg = sender.create_message_for_recipient(
            cfg,
            to_addr,
            theme_index_override=theme_index_override
        )

        # Envío SMTP
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

        # Log de éxito usando el logger del 010 (consistencia de logs)
        extra = "Sent from worker (queue) | with vCard + QR"
        theme = msg.get("X-Theme-Name", "")
        if theme:
            extra += f" | THEME={theme}"
        ps = msg.get("X-PS-Line", "")
        if ps:
            extra += f" | PS={ps}"

        sender.log_email_result(cfg, to_addr, msg["Subject"], True, extra)

    finally:
        # Restaurar valores originales para no contaminar próximos envíos
        email_cfg["subject"] = old_subject
        email_cfg["html_template"] = old_template


# ============================================================
# MAIN Worker Loop
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # CONFIG FIJO:
    # Ya migraste a config9.json, así que aquí lo apuntamos directo.
    # --------------------------------------------------------
    cfg_path = BASE_DIR / "config9.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No existe config9.json en: {cfg_path}")

    # Cargar config con la función del 010 (evita duplicar loaders)
    cfg = sender.load_config(cfg_path)

    # Cola y parámetros del worker
    qp = queue_path(cfg)
    tick = int(cfg.get("app", {}).get("tick_seconds", 5))                 # cada cuánto revisa la cola
    rate_limit = int(cfg.get("app", {}).get("rate_limit_seconds", 15))    # pausa entre envíos

    print(f"[INFO] Worker activo. Cola: {qp}")
    print(f"[INFO] tick={tick}s | rate_limit={rate_limit}s | Ctrl+C para parar")

    # Loop infinito del worker
    while True:
        # 1) Cargar jobs
        q = load_queue(qp)
        jobs = q.get("jobs", [])
        if not isinstance(jobs, list):
            jobs = []

        now_dt = now_local(cfg)
        changed = False  # si hubo cambios, guardamos al final

        # 2) Ordenar jobs por scheduled_for (más antiguo primero)
        def job_sort_key(j):
            dt = parse_job_dt(j.get("scheduled_for", ""))
            return dt or datetime.max

        jobs.sort(key=job_sort_key)

        # 3) Revisar jobs pendientes
        for job in jobs:
            if job.get("status", "pending") != "pending":
                continue

            # 3.1) Validar destinatario
            to_addr = (job.get("to") or "").strip()
            if not to_addr:
                job["status"] = "failed"
                job["last_error"] = "Missing 'to'"
                changed = True
                continue

            # 3.2) Validar scheduled_for
            job_dt = parse_job_dt(job.get("scheduled_for", ""))
            if job_dt is None:
                job["status"] = "failed"
                job["last_error"] = "Invalid 'scheduled_for'"
                changed = True
                continue

            # 3.3) Si aún no toca, saltamos
            if not is_due(job_dt, now_dt):
                continue

            # 3.4) Overrides guardados en el job
            subject_override = job.get("subject") or None
            template_override = job.get("template") or None
            theme_index_override = job.get("theme_index", None)

            print(f"[DUE] {to_addr} (job={job.get('id')}) -> enviando...")

            try:
                # Intentar envío
                send_job(cfg, to_addr, subject_override, template_override, theme_index_override)

                # Marcar como enviado
                job["status"] = "sent"
                job["sent_at"] = now_local(cfg).isoformat()
                job["last_error"] = ""
                changed = True

                print(f"[OK] Enviado a {to_addr} | THEME={job.get('theme_name','')}")

            except Exception as e:
                # Si falla: reintento o failed definitivo
                err = str(e)
                print(f"[ERROR] Falló {to_addr}: {err}")
                bump_retry(cfg, job, err)
                changed = True

            # Guardado inmediato tras cada intento:
            # - si el worker se cierra/crashea, no pierdes progreso
            save_queue(qp, {"jobs": jobs})

            # Rate-limit entre envíos
            time.sleep(rate_limit)

        # 4) Guardar si hubo cambios generales
        if changed:
            save_queue(qp, {"jobs": jobs})

        # 5) Esperar antes del siguiente ciclo
        time.sleep(tick)
