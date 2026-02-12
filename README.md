# PIERODEV Email Sender (Python)
Envío de emails profesionales con **HTML**, **adjuntos**, **vCard**, **QR automático**, **themes/packs**, y **programación vía cola**.

Este proyecto está pensado para enviar correos tipo “presentación / CV / portafolio” con una plantilla HTML cuidada, y permite:

- Enviar **ahora mismo** (modo `send_now`)
- O **programar** (modo `schedule`) creando jobs en `queue.json`
- Y procesarlos luego con un **worker** (`worker_sender.py`)

---

## ✅ Componentes del repo

### 1) `pierodev_email_sender.py` (script principal)
Hace dos cosas según `app.mode`:

- **`send_now`** → envía inmediatamente usando SMTP (Gmail típico: STARTTLS 587).
- **`schedule`** → NO envía; solo encola en `queue.json` con fecha/hora y sale.

Incluye extras:
- **PS aleatorio** (P.D.) desde `config.json`
- **vCard (.vcf)** adjunta (para guardar contacto en 1 clic)
- **QR automático** al portafolio (genera PNG y lo embebe en el email)
- **Themes/packs** que aplican reemplazos en el HTML con estrategia:
  - `round_robin`, `random`, `by_recipient`
- **Imágenes locales inline (CID)**: si el HTML apunta a imágenes locales, las embebe dentro del correo

---

### 2) `worker_sender.py` (worker)
Procesa la cola `queue.json`:

- Revisa jobs `pending`
- Si la hora ya llegó (job “due”), envía el correo
- Marca el job como `sent`
- Si falla, aplica **reintentos** y, si supera el máximo, marca `failed`
- Aplica **rate limit** entre envíos (para no saturar Gmail)

> Importante: el worker usa el mismo constructor del script principal para generar el email (incluyendo QR, vCard, PS y theme).

---

### 3) `config.json`
Configura:
- SMTP (Gmail)
- Email (from/to/subject/body/html_template/attachments/scheduled_for)
- App (mode/timezone/queue_file)
- PS (enabled/phrases)
- vCard (datos de contacto)
- QR (url, output_dir, filename)
- Themes (enabled/strategy/themes/state_file)

---

## 📁 Estructura recomendada

.
├─ pierodev_email_sender.py
├─ worker_sender.py
├─ config.json
├─ plantilla2.html
├─ adjuntos/
│   └─ CV_....pdf
├─ generated/               # se crea solo (QR)
├─ queue.json               # se crea solo (schedule)
├─ templates_state.json     # se crea solo (round_robin)
└─ sent_emails.log          # se crea solo


---

## 🧩 Requisitos

- Python 3.9+ (recomendado 3.11+)

Instala dependencias:

```bash
pip install beautifulsoup4
pip install qrcode[pil]
pip install tzdata
````

Notas:

* En Windows, `tzdata` evita errores con zonas horarias tipo `Europe/Madrid`.
* Si no instalas `qrcode[pil]`, el script avisa y continúa (sin QR).

---

## ⚙️ Configuración rápida (SMTP Gmail)

En `config.json`, rellena:

```json
"smtp": {
  "host": "smtp.gmail.com",
  "port": 587,
  "user": "TU_CORREO@gmail.com",
  "password": "TU_APP_PASSWORD",
  "use_tls": true
}
```

✅ Recomendación para Gmail: usar **App Password** (no tu contraseña normal).

---

## 🚀 Uso

### A) Enviar ahora (modo `send_now`)

En `config.json`:

```json
"app": { "mode": "send_now" }
```

Ejecuta:

```bash
python pierodev_email_sender.py
```

Resultado:

* Envía el correo a `email.to`
* Registra el resultado en `sent_emails.log`

---

### B) Programar envío (modo `schedule` → encola y sale)

En `config.json`:

```json
"app": { "mode": "schedule" }
```

Y define fecha/hora:

```json
"email": { "scheduled_for": "2026-02-12 19:30" }
```

Ejecuta:

```bash
python pierodev_email_sender.py
```

Resultado:

* NO envía nada
* Guarda jobs en `queue.json` con estado `pending`

---

## 🛠️ Ejecutar el worker (procesar la cola)

Abre otra terminal y ejecuta:

```bash
python worker_sender.py
```

El worker:

* Lee `queue.json`
* Envía cuando corresponde
* Respeta `rate_limit_seconds` entre envíos
* Reintenta si falla y marca `failed` si se exceden los intentos

> Para detenerlo: **Ctrl + C**

---

## 🎨 Themes (packs) y estrategias

Los themes funcionan como “skins” aplicando reemplazos al HTML.

Ejemplo:

```json
"templates": {
  "enabled": true,
  "strategy": "round_robin",
  "state_file": "templates_state.json",
  "themes": [
    { "name": "ocean-default", "replace": {} },
    {
      "name": "midnight-cyan",
      "replace": {
        "#1e3a8a": "#0f172a",
        "#0ea5e9": "#22d3ee"
      }
    }
  ]
}
```

Estrategias:

* **round_robin**: rota themes y guarda estado en `templates_state.json`
* **random**: elige un theme aleatorio por email
* **by_recipient**: theme estable por destinatario (mismo email → mismo theme)

✅ Cuando encolas en `schedule`, se guarda `theme_index` en el job para que el worker use el mismo theme al enviar.

---

## 🧾 Logs

* `sent_emails.log` guarda:

  * fecha/hora
  * destinatario
  * asunto
  * OK/ERROR
  * info extra (THEME, PS, etc.)

---

## 🐞 Troubleshooting

### Error de timezone (Europe/Madrid)

```bash
pip install tzdata
```

### No se genera el QR

```bash
pip install qrcode[pil]
```

y revisa que `qr.enabled` esté en `true`.

### Adjuntos no encontrados

* Confirma que el PDF existe en `adjuntos/`
* Confirma que el nombre coincide exactamente con `email.attachments`

---

## ✅ Flujo recomendado

1. Ajusta `config.json` (SMTP, destinatario, asunto, adjuntos)
2. Prueba en `send_now` para validar que llega bien
3. Cambia a `schedule` para encolar envíos
4. Ejecuta el worker para procesarlos