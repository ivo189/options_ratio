# Reglas de operación automática — Ratio Call Spreads

> Documento de diseño para el bot de ejecución automática.
> Implementación en `scanner_bot.py` / `bot_config.py`.
> Los parámetros configurables se editan desde la pestaña **Bot Scanner** del dashboard.

---

## Estado de implementación

| Función | Estado |
|---------|--------|
| Screener manual | ✅ Operativo |
| Scanner periódico (watchlist) | ✅ Operativo |
| Parámetros configurables (UI) | ✅ Operativo |
| Apertura automática de posiciones | 🔜 Próximo paso |
| Notificaciones Telegram (BE breach) | 🔜 Próximo paso |
| Roll manual + alerta | 🔜 Próximo paso |
| Roll automático | ⏳ Futuro |
| Cierre automático de vencimientos | ⏳ Futuro |

---

## 1. Estrategia base

Venta de ratio call spreads **1:2** sobre acciones de alta liquidez.

- **Leg largo**: 1 call comprado (protección)
- **Leg corto**: 2 calls vendidos a un strike superior
- **Objetivo**: cobrar crédito neto; el subyacente no sube hasta los strikes antes del vencimiento
- **DTE target**: 5–21 días (preferido ~10–14)
- **Resultado ideal**: todas las opciones expiran sin valor; crédito cobrado = ganancia neta

---

## 2. Filtros de apertura (pre-condiciones)

Antes de evaluar cualquier spread se verifican estos filtros. Si alguno falla, **no se opera**.

### 2.1 Earnings antes del vencimiento ❌
Si el activo tiene una fecha de earnings o conference call **antes** del vencimiento de la opción elegida,
descartar completamente ese símbolo para esa expiración.

Razón: los resultados generan saltos bruscos que invalidan los supuestos del spread.

> **Implementación pendiente**: consultar earnings date vía `yfinance` (`Ticker.calendar`)
> y comparar contra la expiración elegida. Campo en `BotConfig`: `check_earnings: bool = True`.

### 2.2 Liquidez mínima
- Bid/ask válidos en ambas patas (ya filtrado por el screener actual)
- Open interest mínimo (umbral a definir)

### 2.3 Horario de mercado
Solo operar en sesión regular NYSE (09:30–16:00 ET, lunes a viernes).
El bot ya respeta esto vía `_market_is_open()` en `scanner_bot.py`.

---

## 3. Condición de apertura automática

Un spread se abre automáticamente si cumple **todas** estas condiciones:

### 3.1 Strike largo suficientemente OTM
```
long_strike ≥ spot_price × (1 + min_otm_pct / 100)
```
Parámetro configurable: **`min_otm_pct`** (default 10%).

Ejemplo: spot $14.00 → long strike debe ser ≥ $15.40.

Razón: cuanto más fuera del dinero estén los strikes, menor la probabilidad de que el subyacente
los alcance antes del vencimiento, y el crédito cobrado es prácticamente seguro.

### 3.2 Ratio 1:2
Solo operar spreads con ratio 1:2 en el bot automático.
Parámetro: **`target_ratio`** (default 2, hardcodeado en la primera versión).

### 3.3 Crédito neto positivo
```
net_credit_per_share > min_net_credit   (después de comisiones IBKR)
```
Parámetro configurable: **`min_net_credit`** (default 0.0 $/sh — cualquier crédito positivo).

---

## 4. Gestión de posiciones abiertas

### 4.1 Posición ganadora (precio ≤ short strike)
**No hacer nada.** Se deja expirar sin valor.

### 4.2 Precio supera el upper BE → alerta Telegram
Cuando el subyacente cruza el upper break-even de una posición activa, el bot envía
una notificación inmediata al canal de Telegram configurado.

El usuario decide si quiere ejecutar el roll manualmente desde el dashboard.

Parámetro: **`notify_be_breach`** (default true).

### 4.3 Roll (manual por ahora)
**No cerrar la posición cuando se torna negativa. Rollear a base más alta (pirámide).**

La lógica ya está implementada en `roll_advisor.py`:
- Vender el leg largo original
- Transformar el leg corto en nuevo leg largo
- Vender nuevos legs cortos a strike superior (mantiene ratio 1:M)
- El crédito acumulado incluye entry + roll
- La nueva posición tiene upper BE más alto y el doble de lotes

> **Futuro**: activar roll automático cuando `auto_roll = True` en `BotConfig`.

### 4.4 Vencimiento
Al llegar a DTE = 0, el usuario registra el resultado manualmente via el botón
**"Registrar vencimiento"** en la tab Positions. El precio histórico de cierre
se sugiere automáticamente vía Yahoo Finance.

> **Futuro**: `auto_close_expired = True` → registrar automáticamente a las 16:05 ET.

---

## 5. Notificaciones Telegram

Configuración: token del bot + chat ID, editables desde la UI del dashboard.

| Evento | Mensaje |
|--------|---------|
| Apertura automática | `✅ Abierto NU 15.50/17.00 (1:2) · crédito +$0.06/sh · vto 2025-02-21` |
| Precio > upper BE | `⚠️ NU superó upper BE $16.80 · precio actual $17.10 · revisar roll` |
| Roll recomendado (futuro) | `🔄 Roll sugerido NU → 17.00/19.00 · crédito acum. +$0.14 · nuevo BE $20.40` |

---

## 6. Parámetros configurables

Todos los parámetros se guardan en `bot_config.json` y se editan desde la
sección **"Apertura automática"** y **"Notificaciones Telegram"** del sidebar del Bot Scanner.

| Parámetro | UI | Descripción | Default |
|-----------|-----|-------------|---------|
| `auto_open` | checkbox Habilitada | Activar apertura automática | `false` |
| `min_otm_pct` | Strike largo ≥ ATM + N% | % mínimo OTM del leg largo | `10` |
| `target_ratio` | — | Ratio del spread (1:N) | `2` |
| `min_net_credit` | Crédito neto mínimo | $/sh mínimo después de comisiones | `0.0` |
| `telegram_token` | Bot token | Token del bot de Telegram | `""` |
| `telegram_chat_id` | Chat ID | ID del chat destinatario | `""` |
| `notify_be_breach` | Alertar si precio > upper BE | Notificar cuando BE es superado | `true` |
| `notify_open` | Notificar apertura automática | Notificar cuando el bot abre | `true` |
| `auto_roll` | — (futuro) | Roll automático al superar BE | `false` |
| `auto_close_expired` | — (futuro) | Registrar vencimientos a las 16:05 ET | `false` |

---

## 7. Checklist de implementación

- [ ] Integración de reglas de apertura en `scanner_bot.py` (leer `BotConfig`, evaluar condiciones)
- [ ] Ejecución de órdenes via IBKR (requiere permisos trading + manejo de fills)
- [ ] Filtro de earnings (`yfinance Ticker.calendar` o API de eventos corporativos)
- [ ] Envío de notificaciones Telegram (`python-telegram-bot` o requests directo a la API)
- [ ] Monitoreo de BE en posiciones activas → trigger Telegram
- [ ] Roll automático (cuando `auto_roll = True`)
- [ ] Cierre automático de vencimientos (cuando `auto_close_expired = True`)
- [ ] Backtesting de las reglas sobre historial antes de activar ejecución real
