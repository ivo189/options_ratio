# Reglas de operación automática — Ratio Call Spreads

> Documento de diseño para el bot de ejecución automática.
> Las reglas aquí definidas son las que eventualmente se implementarán en `scanner_bot.py`.
> Estado actual: **solo documentación**, sin ejecución automática.

---

## 1. Estrategia base

Venta de ratio call spreads (1:2 ó 1:3) sobre acciones de alta liquidez.

- **Leg largo**: 1 call comprado (protección)
- **Leg corto**: 2 ó 3 calls vendidos a strike superior
- **Objetivo**: cobrar crédito neto o deuda mínima; el subyacente no sube hasta los strikes
- **DTE target**: 5–21 días (preferido ~10–14)
- **Resultado ideal**: todas las opciones expiran sin valor

---

## 2. Filtros de apertura (pre-condiciones)

Antes de evaluar cualquier spread, se verifican estos filtros. Si alguno falla, **no se opera**.

### 2.1 Earnings antes del vencimiento ❌
Si el activo tiene una fecha de earnings/conference call **antes** del vencimiento de la opción elegida,
descartar completamente ese símbolo para esa expiración.

Razón: los resultados generan saltos bruscos que invalidan los supuestos del spread.

> **Implementación futura**: consultar earnings date vía yfinance (`Ticker.calendar`) o
> una API de eventos corporativos, y comparar contra la expiración elegida.

### 2.2 Liquidez mínima
- Bid/ask válidos en ambas patas (ya filtrado por el screener actual)
- Open interest > N contratos (umbral a definir)

### 2.3 Horario de mercado
Solo operar en sesión regular NYSE (09:30–16:00 ET, lunes a viernes).
El bot ya respeta esto vía `_market_is_open()` en `scanner_bot.py`.

---

## 3. Condición de apertura automática

Un spread se abre automáticamente si cumple **todas** estas condiciones:

### 3.1 Bases muy fuera del dinero (Far OTM)
Ambos strikes deben estar **significativamente por encima** del precio actual del subyacente.

Razonamiento: cuanto más lejos estén los strikes del precio actual, menor la probabilidad de
que el subyacente los alcance antes del vencimiento. Eso convierte a la posición en casi
inalcanzable y el crédito cobrado es prácticamente ganancia asegurada.

> **Umbral sugerido (a calibrar)**: el strike largo debe estar ≥ X% por encima del precio actual.
> Ejemplo: subyacente en $14.00 → strike largo ≥ $16.50 (+18%), strike corto más arriba todavía.

### 3.2 Crédito neto positivo
`net_credit_per_share > 0` después de comisiones IBKR estimadas.

No se abre si la posición tiene deuda neta, incluso si el spread es muy OTM.

### 3.3 Score mínimo
El score del screener (`−net_debit / spread_width`, mayor es mejor) debe superar un umbral mínimo
(a definir empíricamente según historial).

---

## 4. Gestión de posiciones abiertas

### 4.1 Posición ganadora (precio ≤ short strike)
No hacer nada. Se deja expirar sin valor.

### 4.2 Posición en zona de alerta (precio > short strike pero ≤ upper BE)
Alerta visual en el dashboard (`alert_level = "warning"`).
**No cerrar**. Evaluar si conviene rollear.

### 4.3 Posición comprometida (precio > upper BE)
**No cerrar la posición**. En su lugar, **rollear a una base más alta** (pirámide).

La lógica de roll está implementada en `roll_advisor.py`:
- Se vende el leg largo original
- Se transforma el leg corto original en nuevo leg largo
- Se venden nuevos legs cortos a un strike superior
- Se mantiene el ratio 1:M
- El crédito acumulado se recalcula incluyendo el crédito del roll

La posición resultante tiene un nuevo upper BE más alto y mayor tamaño (2x los lotes originales).

> **Condición para roll automático (a definir)**:
> - Precio > upper_BE por más de N minutos consecutivos, O
> - Precio > upper_BE al cierre de la sesión

### 4.4 Vencimiento
Al llegar a DTE = 0, el bot no hace nada automáticamente.
El usuario registra el resultado manualmente via el botón "Registrar vencimiento"
(con precio histórico sugerido por Yahoo Finance).

> **Futuro**: podría automatizarse registrando el precio de cierre del día de vencimiento
> via Yahoo Finance a las 16:05 ET y cerrando la posición automáticamente en el sistema.

---

## 5. Notificaciones Telegram (futuro)

Cuando el bot ejecute cualquiera de estas acciones, enviará un mensaje al chat configurado:

| Evento | Mensaje ejemplo |
|--------|----------------|
| Apertura automática | `✅ Abierto NU 16.5/18.0 (1:3) · crédito neto +$0.08 · vto 2025-01-17` |
| Roll ejecutado | `🔄 Roll NU → 18.0/20.0 (1:3) · crédito acum. +$0.12 · nuevo BE $21.40` |
| Alerta warning | `⚠️ NU cruzó el short strike · precio $15.20 > $15.00 · upper BE $16.80` |
| Alerta crítica | `🚨 NU superó el upper BE · precio $17.10 > $16.80 · evaluando roll` |
| Vencimiento pendiente | `📋 NU venció ayer · registrar resultado en el dashboard` |

---

## 6. Parámetros configurables (a implementar en la UI o config file)

| Parámetro | Descripción | Valor por defecto sugerido |
|-----------|-------------|---------------------------|
| `min_otm_pct` | % mínimo que el strike largo debe estar por encima del spot | 15% |
| `min_score` | Score mínimo del screener para apertura automática | TBD |
| `roll_trigger` | Condición para disparar roll (precio > BE por N min) | 15 min |
| `max_roll_count` | Máximo de rolls permitidos por posición | 2 |
| `telegram_chat_id` | ID del chat de Telegram para notificaciones | — |
| `bot_interval_min` | Intervalo de escaneo del bot en minutos | 5 |
| `target_dte_min` / `max` | Rango de DTE aceptable para aperturas | 5 – 21 |

---

## 7. Lo que falta implementar

- [ ] Consulta de earnings date por símbolo (pre-filtro)
- [ ] Umbral de OTM configurable para apertura automática
- [ ] Ejecución de órdenes via IBKR (requiere permisos de trading en la cuenta)
- [ ] Manejo de fills parciales y órdenes limit vs market
- [ ] Integración Telegram (`python-telegram-bot` o webhook)
- [ ] Roll automático cuando se dispara la condición
- [ ] Registro automático de vencimientos a las 16:05 ET
- [ ] Backtesting de las reglas sobre histórico antes de activar ejecución real
