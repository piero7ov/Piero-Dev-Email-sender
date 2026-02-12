# Cómo usar

## Elegir estrategia de rotación

En config8.json:

```json
"strategy": "round_robin"
```

## Opciones:

- "round_robin" → v1, v2, v3, v4… (equilibrado)
- "random" → aleatorio
- "by_recipient" → estable por destinatario (misma persona = mismo tema siempre)

📌 Con round_robin te crea/actualiza templates_state.json.