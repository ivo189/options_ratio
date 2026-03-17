# Options Ratio Screener — Dashboard

Web dashboard para screenear y gestionar **ratio call spreads** en Interactive Brokers.

Conecta a TWS / IB Gateway via la API oficial de IBKR, obtiene cadenas de opciones en
tiempo real y rankea los mejores spreads por crédito neto, upper BE y score.

---

## Inicio rápido (local con TWS)

### 1. Clonar el repositorio

```bash
git clone https://github.com/ivo189/options_ratio.git
cd options_ratio
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar la conexión

```bash
cp .env.example .env
```

Editar `.env` con tus datos:

```env
IBKR_HOST=127.0.0.1
IBKR_PORT=7497          # TWS paper → 7497 | TWS live → 7496
IBKR_CLIENT_ID=1
```

> Asegúrate de tener TWS o IB Gateway corriendo con la API habilitada:
> *Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients*

### 4. Levantar el dashboard

```bash
python3 app.py
```

Abrir `http://localhost:5000` en el browser.

---

## Tabs del dashboard

| Tab | Descripción |
|-----|-------------|
| **Manual** | Screenear un símbolo/expiración manualmente |
| **Bot Scanner** | Watchlist automático con scan periódico + configuración del bot |
| **Positions** | Registro de posiciones abiertas, cerradas y vencidas |

---

## Bot Scanner — Configuración

En el sidebar del Bot Scanner hay dos secciones configurables que persisten en `bot_config.json`:

### Apertura automática

| Parámetro | Descripción | Default |
|-----------|-------------|---------|
| Habilitada | Activa la apertura automática de posiciones | off |
| Strike largo ≥ ATM + N% | El leg largo debe estar al menos N% por encima del precio actual | 10% |
| Crédito neto mínimo | Crédito neto mínimo en $/sh (después de comisiones) | 0.00 |

### Notificaciones Telegram

| Campo | Descripción |
|-------|-------------|
| Bot token | Token obtenido de @BotFather |
| Chat ID | ID del chat/grupo destinatario (usar @userinfobot para obtenerlo) |
| Alertar si precio > upper BE | Envía alerta cuando el subyacente supera el break-even superior |
| Notificar apertura automática | Envía mensaje cuando el bot abre una posición |

> `bot_config.json` está en `.gitignore` — el token de Telegram nunca se sube al repo.

---

## Estructura del proyecto

```
app.py              Flask server + endpoints de la API
bot_config.py       Configuración persistente del bot (dataclass)
scanner_bot.py      Bot de scan periódico (watchlist)
chain_fetcher.py    Obtención de cadenas de opciones vía IBKR
ratio_analyzer.py   Análisis y ranking de ratio spreads
roll_advisor.py     Análisis de escenarios de roll
positions.py        Gestión de posiciones (CRUD, JSON)
watchlist.py        Gestión de la watchlist del bot
screener.py         CLI standalone (uso sin dashboard)
templates/          Frontend (HTML/CSS/JS en un solo archivo)
BOT_RULES.md        Reglas de operación automática y roadmap
```

---

## Columnas del screener

| Columna | Significado |
|---------|-------------|
| Long K | Strike del leg largo (call comprado) |
| Short K | Strike del leg corto (calls vendidos) |
| Ratio | 1:N (1:2 = vender 2, comprar 1) |
| Net $/sh | Crédito neto por acción (positivo = cobras) |
| Lower BE | Breakeven inferior |
| Upper BE | Breakeven superior (donde vuelven las pérdidas) |
| Max profit | P&L máximo por lote al vencimiento en el short strike |
| Score | Mayor es mejor: `−(net_debit / spread_width)` |

---

## Prerrequisitos

- Python 3.10+
- Interactive Brokers account con suscripciones de datos de opciones
- TWS o IB Gateway corriendo con API habilitada

---

## Risk Warning

Los ratio spreads con N > 1 tienen **riesgo ilimitado al alza** más allá del short strike.
Entendé bien el riesgo antes de operar.
