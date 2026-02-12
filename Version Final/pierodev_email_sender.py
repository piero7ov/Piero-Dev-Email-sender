#!/usr/bin/env python3
# ============================================================
#  SCRIPT PRINCIPAL: Envío de emails (send_now) + Encolado (schedule)
# ------------------------------------------------------------
#  Qué hace este script:
#   - Lee un config fijo en la misma carpeta del script.
#   - Según app.mode:
#       * send_now  -> ENVÍA inmediatamente
#       * schedule  -> NO envía, solo mete jobs en queue.json y sale
#
#  Features incluidas (desde config):
#   1) PS aleatorio: agrega una posdata (P.D.) en texto y/o HTML
#   2) vCard: adjunta un .vcf para que te guarden como contacto
#   3) QR: genera un PNG con QR al portafolio y lo incrusta en la plantilla
#   4) Themes (packs): cambia “skin” del HTML por reemplazos (string replace)
#      - strategy: round_robin | random | by_recipient
#   5) Embedding de imágenes locales como CID (inline)
#      - Convierte <img src="local.png"> en cid:... y adjunta el archivo
#
#  Arquitectura:
#   - create_message_for_recipient() construye el EmailMessage completo
#   - send_now() envía por SMTP
#   - schedule_only() encola jobs (para un worker separado)
#
#  Importante:
#   - BASE_DIR hace que funcione sin importar desde dónde ejecutes el script
#   - Si falta qrcode o tzdata, el script NO revienta (tiene fallbacks)
# ============================================================

import json
import smtplib
import ssl
import mimetypes
import random
import hashlib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup  # pip install beautifulsoup4


# ------------------------------------------------------------
# Timezone (Python 3.9+) + tzdata
# - En Windows a veces falta la base de zonas IANA (Europe/Madrid, etc.)
# - Solución: pip install tzdata
# - Si no está, ZoneInfo puede fallar -> usamos fallback sin timezone.
# ------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ------------------------------------------------------------
# QR (pip install qrcode[pil])
# - Si no está instalado, NO reventamos:
#   solo mostramos WARN y seguimos sin QR.
# ------------------------------------------------------------
try:
    import qrcode
except ImportError:
    qrcode = None


# ============================================================
# BASE DIR
# - Carpeta donde está este script.
# - Todas las rutas relativas (config, plantilla, adjuntos, logs, queue)
#   se resuelven contra BASE_DIR para evitar errores por "cwd".
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# 1) Carga de config y plantilla HTML
# ============================================================

def load_config(path: str | Path) -> dict:
    """
    Lee un archivo JSON y devuelve el config como dict.

    Parámetros:
      path (str | Path): ruta al JSON (ej: config9.json)

    Retorna:
      dict: configuración completa.

    Nota:
      - Se asume que el JSON es válido.
      - Si el archivo no existe, el error lo maneja el caller.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_html_template(path: str | Path) -> str:
    """
    Lee la plantilla HTML (plantilla2.html) y la devuelve como string.

    Parámetros:
      path (str | Path): ruta al HTML

    Retorna:
      str: contenido completo del HTML.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# 2) Logging (registro de envíos)
# ============================================================

def get_log_path(cfg: dict) -> Path:
    """
    Devuelve el path ABSOLUTO del archivo de log de envíos.

    Fuente:
      cfg["email"]["log_file"] (default: sent_emails.log)

    Comportamiento:
      - Si el path es relativo, se interpreta relativo a BASE_DIR.
    """
    email_cfg = cfg.get("email", {})
    log_file = email_cfg.get("log_file", "sent_emails.log")

    p = Path(log_file)
    if not p.is_absolute():
        p = BASE_DIR / p

    return p.resolve()


def log_email_result(cfg: dict, to_addr: str, subject: str, success: bool, message: str = "") -> None:
    """
    Escribe una línea en el log indicando resultado del envío.

    Formato:
      YYYY-MM-DD HH:MM:SS ; destino ; subject ; OK/ERROR ; info

    Parámetros:
      cfg (dict): config para resolver log_file
      to_addr (str): destinatario
      subject (str): asunto
      success (bool): True=OK, False=ERROR
      message (str): texto adicional (PS, theme, error, etc.)

    Nota:
      - Si falla el log, no reventamos el script: solo WARN.
    """
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


# ============================================================
# 3) Helpers de app (modo, timezone y cola)
# ============================================================

def get_app_cfg(cfg: dict) -> dict:
    """
    Devuelve el sub-bloque cfg["app"] o {} si no existe.
    Evita KeyError en configs incompletos.
    """
    return cfg.get("app", {})


def safe_tz(tz_name: str):
    """
    Intenta devolver ZoneInfo(tz_name). Si no se puede, devuelve None.

    Casos:
      - ZoneInfo no existe (Python viejo o import falló) -> None
      - tzdata no instalado / zona no encontrada -> None

    Esto evita que el script reviente solo por timezone.
    """
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def now_local(cfg: dict) -> datetime:
    """
    Devuelve fecha/hora actual en la zona configurada.

    Fuente:
      cfg["app"]["timezone"] (default: Europe/Madrid)

    Retorna:
      - datetime timezone-aware si ZoneInfo funciona
      - datetime naive si no hay timezone disponible
    """
    tz_name = get_app_cfg(cfg).get("timezone", "Europe/Madrid")
    tz = safe_tz(tz_name)
    return datetime.now(tz) if tz else datetime.now()


def get_queue_path(cfg: dict) -> Path:
    """
    Devuelve path ABSOLUTO del archivo de cola.

    Fuente:
      cfg["app"]["queue_file"] (default: queue.json)

    Si es relativo:
      -> BASE_DIR/queue.json
    """
    qfile = get_app_cfg(cfg).get("queue_file", "queue.json")
    p = Path(qfile)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def load_queue(cfg: dict) -> dict:
    """
    Carga queue.json y devuelve dict.

    Si no existe:
      devuelve {"jobs": []}

    Nota:
      - schedule_only() usa esto para no depender de que exista la cola.
    """
    qp = get_queue_path(cfg)
    if not qp.exists():
        return {"jobs": []}
    with open(qp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(cfg: dict, queue_data: dict) -> None:
    """
    Guarda queue.json de manera segura (tmp -> replace).

    Esto reduce la probabilidad de que quede un JSON corrupto
    si el proceso se corta mientras escribe.
    """
    qp = get_queue_path(cfg)
    qp.parent.mkdir(parents=True, exist_ok=True)

    tmp = qp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, ensure_ascii=False, indent=2)
    tmp.replace(qp)


def parse_scheduled_for(cfg: dict) -> str:
    """
    Lee email.scheduled_for en formato:
      "YYYY-MM-DD HH:MM"

    y lo convierte a ISO:
      - con tzinfo si se puede (ZoneInfo OK)
      - sin tzinfo si no se puede

    Ejemplo:
      "2026-02-12 19:30"
        -> "2026-02-12T19:30:00+01:00"   (si tzdata existe)
        -> "2026-02-12T19:30:00"         (si no hay tz)

    Lanza:
      ValueError si scheduled_for está vacío o faltante.
    """
    email_cfg = cfg.get("email", {})
    raw = (email_cfg.get("scheduled_for") or "").strip()
    if not raw:
        raise ValueError("Falta email.scheduled_for en config (ej: '2026-02-12 19:30').")

    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")

    tz_name = get_app_cfg(cfg).get("timezone", "Europe/Madrid")
    tz = safe_tz(tz_name)
    if tz is not None:
        dt = dt.replace(tzinfo=tz)

    return dt.isoformat()


def new_job_id() -> str:
    """
    Genera un ID simple para cada job en la cola.

    Formato:
      job_YYYYMMDDHHMMSS_RAND

    Nota:
      - Suficiente para una cola local.
      - No es UUID, pero evita colisiones “normales”.
    """
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rnd = random.randint(1000, 9999)
    return f"job_{ts}_{rnd}"


def get_recipients(cfg: dict) -> list[str]:
    """
    Normaliza email.to a lista SIEMPRE.

    Casos:
      - email.to es list: limpia strings vacíos y strip()
      - email.to es string: lo mete en lista si no está vacío

    Retorna:
      list[str] con 0..N destinatarios.
    """
    to_field = cfg["email"]["to"]
    if isinstance(to_field, list):
        return [t.strip() for t in to_field if str(t).strip()]
    return [str(to_field).strip()] if str(to_field).strip() else []


# ============================================================
# 4) THEMES (packs) por reemplazos de string
# ============================================================

def get_templates_cfg(cfg: dict) -> dict:
    """Shortcut para cfg["templates"] o {}."""
    return cfg.get("templates", {})


def templates_enabled(cfg: dict) -> bool:
    """
    Indica si el sistema de themes está activo.

    Fuente:
      cfg["templates"]["enabled"]
    """
    tcfg = get_templates_cfg(cfg)
    return bool(tcfg.get("enabled", False))


def get_themes(cfg: dict) -> list[dict]:
    """
    Devuelve la lista de themes:
      cfg["templates"]["themes"]

    Si no es lista:
      devuelve []
    """
    tcfg = get_templates_cfg(cfg)
    themes = tcfg.get("themes", [])
    if isinstance(themes, list):
        return themes
    return []


def get_templates_state_path(cfg: dict) -> Path:
    """
    Ruta ABSOLUTA donde guardamos el estado del round_robin.

    Fuente:
      cfg["templates"]["state_file"] (default: templates_state.json)

    Nota:
      - Es un archivo separado de queue.json.
      - Solo guarda rr_next (índice próximo).
    """
    tcfg = get_templates_cfg(cfg)
    state_file = tcfg.get("state_file", "templates_state.json")
    p = Path(state_file)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def load_templates_state(cfg: dict) -> dict:
    """
    Carga templates_state.json y devuelve dict con rr_next.

    Si no existe o está corrupto:
      devuelve {"rr_next": 0}

    rr_next:
      - índice a usar en el próximo correo (round_robin).
    """
    sp = get_templates_state_path(cfg)
    if not sp.exists():
        return {"rr_next": 0}

    try:
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("rr_next", 0)
            return data
    except Exception:
        pass

    return {"rr_next": 0}


def save_templates_state(cfg: dict, state: dict) -> None:
    """
    Guarda templates_state.json de forma segura (tmp -> replace).

    Así evitamos:
      - archivos a medias si se corta el proceso.
    """
    sp = get_templates_state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)

    tmp = sp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(sp)


def pick_theme_index(cfg: dict, recipient: str | None = None) -> int:
    """
    Elige qué theme usar según cfg["templates"]["strategy"]:

    strategies:
      1) round_robin (default)
         - rota entre themes
         - persiste rr_next en templates_state.json

      2) random
         - elige uno aleatorio en cada envío

      3) by_recipient
         - estable por destinatario (hash del email)
         - el mismo email siempre cae en el mismo theme

    Retorna:
      int: índice 0..len(themes)-1
    """
    themes = get_themes(cfg)
    if not themes:
        return 0

    tcfg = get_templates_cfg(cfg)
    strategy = str(tcfg.get("strategy", "round_robin")).strip().lower()
    n = len(themes)

    if strategy == "random":
        return random.randrange(n)

    if strategy == "by_recipient":
        base = (recipient or "").strip().lower()
        if not base:
            return 0
        h = hashlib.sha256(base.encode("utf-8")).hexdigest()
        return int(h, 16) % n

    # round_robin (default)
    st = load_templates_state(cfg)
    idx = int(st.get("rr_next", 0)) % n
    st["rr_next"] = (idx + 1) % n
    save_templates_state(cfg, st)
    return idx


def resolve_theme(cfg: dict, recipient: str | None, theme_index_override: int | None = None):
    """
    Decide el theme final para un destinatario.

    Parámetros:
      recipient (str | None):
        - usado por by_recipient (hash)
      theme_index_override (int | None):
        - si viene (por ejemplo desde la cola), lo respetamos
        - útil para que el worker envíe “con el mismo theme” que se encoló

    Retorna:
      (idx, theme)
        - si themes desactivado o no hay themes: (None, None)
        - si override válido: (override, themes[override])
        - si no: (idx_elegido, theme_elegido)
    """
    if not templates_enabled(cfg):
        return None, None

    themes = get_themes(cfg)
    if not themes:
        return None, None

    if theme_index_override is not None:
        try:
            idx = int(theme_index_override)
            if 0 <= idx < len(themes):
                return idx, themes[idx]
        except Exception:
            pass

    idx = pick_theme_index(cfg, recipient)
    return idx, themes[idx]


def apply_theme_to_html(html: str, theme: dict | None) -> str:
    """
    Aplica un theme al HTML mediante reemplazos de string.

    theme esperado:
      {
        "name": "...",
        "replace": {
          "#1e3a8a": "#0f172a",
          ...
        }
      }

    Retorna:
      html modificado

    Nota:
      - Es un sistema simple pero potente: puedes cambiar colores y textos.
      - Si necesitas cambios estructurales, ahí ya sería “otra plantilla HTML”.
    """
    if not theme:
        return html

    rep = theme.get("replace", {})
    if not isinstance(rep, dict) or not rep:
        return html

    for old, new in rep.items():
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        if not old or old == new:
            continue
        html = html.replace(old, new)

    return html


# ============================================================
# 5) PS aleatorio (P.D.) en texto + HTML
# ============================================================

def pick_random_ps(cfg: dict) -> str:
    """
    Devuelve una línea de PS aleatoria si está activado.

    Fuente:
      cfg["ps"]["enabled"]
      cfg["ps"]["phrases"] (list)
      cfg["ps"]["prefix"] (default: "P.D.:")

    Retorna:
      str (ej: "P.D.: ...") o "" si no aplica.
    """
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
    """
    Agrega el PS al texto plano (si cfg["ps"]["add_to_text"] == True).

    Retorna:
      str: texto plano final para msg.set_content(...)
    """
    if not ps_line:
        return body_text

    ps_cfg = cfg.get("ps", {})
    if not ps_cfg.get("add_to_text", True):
        return body_text

    body_text = (body_text or "").rstrip()
    return body_text + "\n\n" + ps_line


def apply_ps_to_html(html: str, ps_line: str, cfg: dict) -> str:
    """
    Inserta el PS en el HTML (si cfg["ps"]["add_to_html"] == True).

    Reglas:
      1) Si el HTML contiene {{PS}} -> reemplaza ese marcador.
      2) Si no contiene marcador -> inserta un <p> al final del <body>.

    Ventaja:
      - No te obliga a editar la plantilla cada vez; funciona por defecto.
    """
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


# ============================================================
# 6) QR: generar PNG y reemplazar placeholder del HTML
# ============================================================

def ensure_portfolio_qr(cfg: dict, html_base_dir: Path) -> str | None:
    """
    Genera un QR PNG a una URL (normalmente tu portafolio).

    Fuente:
      cfg["qr"]["enabled"]
      cfg["qr"]["url"] (si no, usa cfg["vcard"]["portfolio"])
      cfg["qr"]["output_dir"], cfg["qr"]["filename"]

    Parámetros:
      html_base_dir (Path):
        - carpeta donde vive la plantilla2.html
        - el QR se genera relativo a esa carpeta para que el <img src="..."> funcione

    Retorna:
      str | None:
        - devuelve la ruta relativa (ej: "generated/qr_portfolio.png")
        - o None si no se pudo generar
    """
    qr_cfg = cfg.get("qr", {})
    if not qr_cfg.get("enabled", False):
        return None

    if qrcode is None:
        print("[WARN] QR enabled pero falta 'qrcode'. Instala: pip install qrcode[pil]")
        return None

    # Prioridad de URL: qr.url -> vcard.portfolio
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

    # Devolvemos la ruta relativa para usarla en el HTML
    rel_src = out_path.relative_to(html_base_dir)
    rel_src = str(rel_src).replace("\\", "/")
    print(f"[OK] QR generado: {rel_src} -> {url}")
    return rel_src


def inject_qr_placeholder(html: str, qr_src: str | None) -> str:
    """
    Rellena el placeholder {{QR_PORTFOLIO_SRC}} si existe en el HTML.

    Si no existe el placeholder:
      - no toca nada (la plantilla puede usar directamente generated/qr_portfolio.png)

    Si existe:
      - usa qr_src si se generó
      - si no se generó, mete un fallback a la URL del portafolio (no rompe el HTML)
    """
    if "{{QR_PORTFOLIO_SRC}}" not in html:
        return html
    if qr_src:
        return html.replace("{{QR_PORTFOLIO_SRC}}", qr_src)
    return html.replace("{{QR_PORTFOLIO_SRC}}", "https://piero7ov.github.io/Portafolio/")


# ============================================================
# 7) CID: imágenes locales embebidas en el email
# ============================================================

def prepare_html_and_attachments(html: str, base_dir: Path):
    """
    Convierte imágenes locales del HTML a imágenes inline (CID):

    - Busca <img src="...">
    - Si src empieza con http/https/cid/data: -> NO se toca
    - Si src es local -> se reemplaza por cid:xxxx
    - Devuelve:
        html_final (str)
        inline_attachments (list[dict]) con:
            path, maintype, subtype, filename, cid

    Parámetros:
      base_dir (Path):
        - carpeta base para resolver rutas relativas de imágenes.
        - normalmente es la carpeta donde está la plantilla HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    inline_attachments = []
    used = {}  # evita adjuntar duplicados si la misma imagen se repite

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        # Ya está remoto o ya es inline cid o base64 -> se deja como está
        if src.startswith(("http://", "https://", "cid:", "data:")):
            continue

        img_path = Path(src)
        if not img_path.is_absolute():
            img_path = (base_dir / src).resolve()

        if not img_path.exists():
            print(f"[WARN] Image not found, leaving as is: {src}")
            continue

        # Reutilizar CID si la imagen aparece varias veces
        if src in used:
            cid = used[src]["cid"]
        else:
            cid = make_msgid()  # retorna "<...>"
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

        # EmailMessage espera cid sin "<>"
        img["src"] = f"cid:{cid[1:-1]}"

    return str(soup), inline_attachments


def attach_related_images(html_part, attachments):
    """
    Adjunta cada imagen inline como "related" del HTML part.

    Esto es lo que hace que en Gmail se vea la imagen dentro del email
    sin depender de links externos.
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


# ============================================================
# 8) Adjuntos normales (PDFs, etc.) desde carpeta "adjuntos/"
# ============================================================

def load_file_attachments_from_config(cfg: dict, adjuntos_dir: Path | None = None):
    """
    Prepara adjuntos “normales” (no inline) desde cfg["email"]["attachments"].

    - Busca archivos dentro de la carpeta adjuntos/
    - Si alguno no existe, avisa y lo salta
    - Devuelve una lista con metadatos (mime, filename, path)
    """
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
    """
    Adjunta archivos como attachments normales al EmailMessage.
    Ejemplo: tu CV en PDF.
    """
    for att in file_attachments:
        with open(att["path"], "rb") as f:
            data = f.read()

        msg.add_attachment(
            data,
            maintype=att["maintype"],
            subtype=att["subtype"],
            filename=att["filename"],
        )


# ============================================================
# 9) vCard (.vcf)
# ============================================================

def build_vcard(
    full_name: str,
    title: str,
    email: str,
    portfolio: str,
    github: str,
    phone: str | None = None,
    location: str | None = None
) -> bytes:
    """
    Construye una vCard 3.0 (como bytes) usando CRLF para compatibilidad.

    Retorna:
      bytes listos para msg.add_attachment(...)

    Nota:
      - La vCard te permite que el receptor te guarde como contacto en 1 click.
    """
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
    """
    Adjunta la vCard al email si cfg["vcard"]["enabled"] es True.

    Usa valores desde cfg["vcard"] y algunos fallbacks del cfg["email"].
    """
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

    # si viene "", lo tratamos como None para no ensuciar la vCard
    if isinstance(phone, str) and phone.strip() == "":
        phone = None

    vcard_bytes = build_vcard(full_name, title, email, portfolio, github, phone, location)
    msg.add_attachment(vcard_bytes, maintype="text", subtype="vcard", filename=filename)
    print(f"[OK] vCard adjunta: {filename}")


# ============================================================
# 10) Construcción del EmailMessage final (por destinatario)
# ============================================================

def create_message_for_recipient(cfg: dict, recipient: str, theme_index_override: int | None = None) -> EmailMessage:
    """
    Construye el EmailMessage completo para 1 destinatario.

    Incluye:
      - Headers (From/To/Subject)
      - Texto plano (y PS opcional)
      - HTML (PS + QR + Theme + CID inline)
      - Adjuntos normales (PDF)
      - vCard

    Parámetros:
      recipient (str): email destino
      theme_index_override (int|None):
        - si viene (por cola), fuerza theme estable y no cambia por strategy

    Retorna:
      EmailMessage listo para send_message(...)
    """
    email_cfg = cfg["email"]

    # 1) Elegimos un PS por correo (si está activado)
    ps_line = pick_random_ps(cfg)

    # 2) Elegimos theme según config (o override si viene de queue)
    theme_idx, theme = resolve_theme(cfg, recipient, theme_index_override)

    # 3) EmailMessage base
    msg = EmailMessage()
    msg["From"] = email_cfg["from"]
    msg["To"] = recipient
    msg["X-Original-To"] = recipient  # útil para logs/depuración
    msg["Subject"] = email_cfg["subject"]

    # Headers de depuración: saber qué theme se aplicó realmente
    if theme is not None:
        msg["X-Theme-Name"] = str(theme.get("name", ""))
        msg["X-Theme-Index"] = str(theme_idx if theme_idx is not None else "")

    # ---------------------------
    # 1) TEXTO PLANO
    # ---------------------------
    body_text = email_cfg.get("body_text", "This email contains HTML content.")
    body_text = apply_ps_to_text(body_text, ps_line, cfg)
    msg.set_content(body_text)

    # ---------------------------
    # 2) HTML TEMPLATE
    # ---------------------------
    # Resolvemos la plantilla relativa a BASE_DIR para que funcione desde cualquier cwd.
    html_template_path = Path(email_cfg["html_template"])
    if not html_template_path.is_absolute():
        html_template_path = (BASE_DIR / html_template_path)
    html_template_path = html_template_path.resolve()

    # base_dir es la carpeta de la plantilla (para resolver imágenes/QR)
    base_dir = html_template_path.parent

    html_raw = load_html_template(html_template_path)

    # 2.1) Insertar PS al HTML (si está habilitado)
    html_raw = apply_ps_to_html(html_raw, ps_line, cfg)

    # 2.2) Generar QR + rellenar placeholder si existe
    qr_src = ensure_portfolio_qr(cfg, base_dir)
    html_raw = inject_qr_placeholder(html_raw, qr_src)

    # 2.3) Aplicar theme (reemplazos de strings)
    html_raw = apply_theme_to_html(html_raw, theme)

    # 2.4) Convertir imágenes locales a CID (inline)
    html_final, inline_attachments = prepare_html_and_attachments(html_raw, base_dir)

    # ---------------------------
    # 3) MULTIPART: HTML alternative
    # ---------------------------
    msg.add_alternative(html_final, subtype="html")

    # el html_part suele ser el último payload
    html_part = msg.get_payload()[-1]
    attach_related_images(html_part, inline_attachments)

    # ---------------------------
    # 4) Adjuntos normales (PDFs)
    # ---------------------------
    file_attachments = load_file_attachments_from_config(cfg, BASE_DIR / "adjuntos")
    attach_files(msg, file_attachments)

    # ---------------------------
    # 5) vCard
    # ---------------------------
    attach_vcard(msg, cfg)

    # Header extra para depurar el PS que tocó (si existe)
    if ps_line:
        msg["X-PS-Line"] = ps_line

    return msg


# ============================================================
# 11) Envío inmediato (send_now)
# ============================================================

def send_now(cfg: dict) -> None:
    """
    Envía inmediatamente a todos los destinatarios en email.to.

    Nota:
      - Si email.to es lista, manda N correos (1 por destinatario).
      - Si email.to es string, manda 1 correo.
    """
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

    for recipient in recipients:
        # Construimos el mensaje final para este destinatario
        msg = create_message_for_recipient(cfg, recipient)

        # Datos útiles para el log
        subject_for_log = msg["Subject"]
        ps_for_log = msg.get("X-PS-Line", "")
        theme_for_log = msg.get("X-Theme-Name", "")

        try:
            # Gmail típico: 587 + STARTTLS
            if use_tls and port == 587:
                context = ssl.create_default_context()
                with smtplib.SMTP(host, port) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(user, password)
                    server.send_message(msg)

            # Gmail SSL directo: 465
            elif port == 465:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(host, port, context=context) as server:
                    server.login(user, password)
                    server.send_message(msg)

            # Fallback: SMTP “simple”
            else:
                with smtplib.SMTP(host, port) as server:
                    server.login(user, password)
                    server.send_message(msg)

            print(f"[OK] Email enviado a {recipient}")

            extra = "Sent successfully (with vCard + QR)"
            if theme_for_log:
                extra += f" | THEME={theme_for_log}"
            if ps_for_log:
                extra += f" | PS={ps_for_log}"

            log_email_result(cfg, recipient, subject_for_log, True, extra)

        except Exception as e:
            err = str(e)
            print(f"[ERROR] Sending email failed to {recipient}: {err}")
            log_email_result(cfg, recipient, subject_for_log, False, err)


# ============================================================
# 12) Schedule (solo encola y sale)
# ============================================================

def schedule_only(cfg: dict) -> None:
    """
    NO envía correos.

    Solo encola jobs en queue.json con:
      - to
      - scheduled_for (ISO)
      - subject / template
      - theme_index/theme_name (para envío consistente desde worker)

    Esto permite:
      - Preparar correos con hora
      - Dejar el envío al worker (011-worker)
    """
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
        # Elegimos theme en el momento de encolar (para que luego sea consistente)
        theme_idx, theme = resolve_theme(cfg, r, None)
        theme_name = theme.get("name", "") if theme else ""

        job = {
            "id": new_job_id(),
            "to": r,
            "scheduled_for": scheduled_iso,
            "status": "pending",
            "created_at": now_local(cfg).isoformat(),
            "subject": cfg.get("email", {}).get("subject", ""),
            "template": cfg.get("email", {}).get("html_template", ""),
            "theme_index": theme_idx,
            "theme_name": theme_name,
            "note": "Encolado desde config (modo schedule)"
        }

        jobs.append(job)
        print(f"[OK] Encolado: {r} @ {scheduled_iso} | THEME={theme_name}")

    q["jobs"] = jobs
    save_queue(cfg, q)

    print(f"[INFO] Cola guardada en: {get_queue_path(cfg)}")
    (print("[INFO] (modo schedule) No se envió ningún correo. Solo se encoló."))


# ============================================================
# 13) MAIN: carga config.json y ejecuta modo
# ============================================================

if __name__ == "__main__":

    # Config FIJO
    config_path = (BASE_DIR / "config.json").resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"No existe config.json en: {config_path}")

    cfg = load_config(config_path)

    # app.mode: "send_now" | "schedule"
    mode = get_app_cfg(cfg).get("mode", "send_now").strip().lower()

    if mode == "send_now":
        send_now(cfg)
    elif mode == "schedule":
        schedule_only(cfg)
    else:
        print(f"[ERROR] app.mode inválido: {mode}. Usa: send_now | schedule")
