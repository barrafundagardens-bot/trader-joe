# Polymarket Trading Bot

Bot de trading técnico para [Polymarket](https://polymarket.com) escrito em Python.
Opera via CLOB (Central Limit Order Book) com estratégias baseadas em análise técnica.

---

## ⚠️ AVISO IMPORTANTE — LEIA ANTES DE QUALQUER COISA

> **Mais de 90% dos traders de mercados de predição perdem dinheiro.**
>
> Este bot é uma ferramenta educacional. Não é conselho financeiro.
> Comece **sempre** com paper trading, teste por no mínimo **2 semanas**,
> e nunca coloque mais do que você pode perder totalmente.
> Use uma **carteira dedicada** com no máximo **US$ 100-300** no início.

---

## Para Iniciantes — Comece Aqui

Antes de rodar qualquer código, conheça estas ferramentas essenciais:

### 1. poly_data — Dados Históricos
- **Repositório**: https://github.com/warproxxx/poly_data
- **Valor**: ⭐⭐⭐⭐⭐ (9/10) — Dados OHLCV históricos do Polymarket
- **Dificuldade**: 🟢 Fácil (3/10)
- **Use para**: Baixar dados históricos e alimentar o backtesting

```bash
git clone https://github.com/warproxxx/poly_data
cd poly_data
pip install -r requirements.txt
python download.py  # baixa dados para ./data/
```

### 2. polyterm — Terminal Interativo
- **Repositório**: https://github.com/NYTEMODEONLY/polyterm
- **Valor**: ⭐⭐⭐⭐ (8/10) — Interface de terminal para explorar mercados
- **Dificuldade**: 🟢 Muito Fácil (2/10)
- **Use para**: Encontrar market IDs, ver orderbook, explorar mercados

### 3. Polymarket Agents — Referência Oficial
- **Repositório**: https://github.com/polymarket/agents
- **Valor**: ⭐⭐⭐⭐ (8/10) — Exemplo oficial de bot com AI
- **Dificuldade**: 🟡 Médio (4/10)
- **Use para**: Entender o padrão de integração com py-clob-client

---

## Arquitetura e Pipeline

```
poly_data (dados brutos)
    │
    ▼
pandas.resample() → candles 15M e 4H
    │
    ├── MACDStrategy    (ta.trend.MACD)
    ├── RSIStrategy     (ta.momentum.RSIIndicator)
    ├── CVDStrategy     (lógica custom em pandas)
    └── FVGMultiTFStrategy  (FVG 4H bias + 15M confirmação)
         │
         ▼
    BacktestingEngine (multi-timeframe)
         │
         ▼
    RiskManager (stop-loss, drawdown, limite diário)
         │
         ▼
    Trader → py-clob-client → Polymarket CLOB
         │
    ┌────┴────┐
    │         │
paper_trading  live_trading
(100% offline)  (capital real)
```

---

## Como Gerar Candles 15M e 4H via pandas resample

O Polymarket não fornece candles diretamente. Construímos a partir de dados tick/minuto:

```python
import pandas as pd

# df tem colunas: timestamp (index), open, high, low, close, volume
df.index = pd.to_datetime(df.index)

# Candles de 15 minutos
df_15m = df.resample('15min').agg({
    'open':   'first',
    'high':   'max',
    'low':    'min',
    'close':  'last',
    'volume': 'sum',
}).dropna()

# Candles de 4 horas
df_4h = df.resample('4h').agg({
    'open':   'first',
    'high':   'max',
    'low':    'min',
    'close':  'last',
    'volume': 'sum',
}).dropna()
```

---

## Instalação

### Pré-requisitos
- Python 3.10 ou superior
- Conta na Polygon Network com carteira **dedicada** (não use a principal)

### Setup

```bash
# 1. Clone o repositório
git clone <seu-repositorio>
cd "Trader Joe"

# 2. Crie ambiente virtual
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 3. Instale dependências
pip install -r requirements.txt

# 4. Auditoria de segurança (OBRIGATÓRIO)
pip install pip-audit
pip-audit -r requirements.txt

# 5. Configure variáveis de ambiente
cp .env.example .env
# Edite .env com seus valores
nano .env

# 6. Crie diretório de dados
mkdir -p data logs results
```

---

## Fluxo Recomendado: Backtest → Paper → Live

### Etapa 1: Backtesting

```bash
# Certifique-se de ter dados do poly_data em ./data/poly_data.csv
python deploy/main.py --mode backtest --strategy fvg_multitf
python deploy/main.py --mode backtest --strategy macd
python deploy/main.py --mode backtest --strategy rsi
python deploy/main.py --mode backtest --strategy cvd
```

### Etapa 2: Paper Trading (mínimo 2 semanas)

```bash
# Simula execuções 100% offline — sem API real
python deploy/main.py --mode paper --strategy fvg_multitf
# ou diretamente:
python deploy/paper_trading.py
```

### Etapa 3: Live Trading (apenas após 2 semanas de paper)

```bash
# NUNCA pule as etapas anteriores
# Configure TRADING_MODE=live no .env
python deploy/main.py --mode live --strategy fvg_multitf
# ou:
python deploy/live_trading.py
```

---

## Estratégias Disponíveis

### MACD Strategy (`macd_strategy.py`)
- **Indicador**: MACD(12, 26, 9) via biblioteca `ta`
- **Sinal BUY**: MACD line cruza acima da signal line + histograma positivo
- **Sinal SELL**: MACD line cruza abaixo da signal line + histograma negativo
- **Filtro**: Tendência de médio prazo confirmada

### RSI Strategy (`rsi_strategy.py`)
- **Indicador**: RSI(14) via biblioteca `ta`
- **Sinal BUY**: RSI < 30 (oversold) + divergência bullish
- **Sinal SELL**: RSI > 70 (overbought) + divergência bearish
- **Filtro**: Volume acima da média

### CVD Strategy (`cvd_strategy.py`)
- **Indicador**: Cumulative Volume Delta (lógica custom em pandas)
- **CVD = Σ(volume × direção)** — positivo para candles de alta, negativo para baixa
- **Sinal BUY**: CVD cruza acima de zero + momentum positivo
- **Sinal SELL**: CVD cruza abaixo de zero + momentum negativo

### FVG MultiTF Strategy (`fvg_multitf_strategy.py`) ⭐ Principal
- **Timeframe 4H**: Detecta Fair Value Gaps para definir bias direcional
- **Timeframe 15M**: Confirma entrada quando preço testa FVG ou cria novo alinhado
- **FVG Bullish**: `candle[i].low > candle[i-2].high` com body > 50% do range
- **FVG Bearish**: `candle[i].high < candle[i-2].low` com body > 50% do range
- **Validade**: FVG expira após 10 candles no timeframe respectivo

---

## Risk Manager

| Parâmetro | Valor Padrão | Descrição |
|-----------|-------------|-----------|
| `MAX_LOSS_PER_TRADE` | 10% | Stop-loss máximo por trade |
| `MAX_TRADES_PER_DAY` | 5 | Limite de trades diários |
| `EMERGENCY_DRAWDOWN` | 20% | Para tudo se drawdown atingir este nível |
| `MAX_TRADE_SIZE` | $10 | Tamanho máximo por ordem |

---

## Segurança

Leia os arquivos `security.md` e `security_checklist.md` antes de qualquer trade real.

### Resumo Rápido de Segurança

1. **Carteira dedicada**: Nunca use sua carteira principal
2. **Capital mínimo**: Comece com no máximo US$ 100-300
3. **Revoke.cash**: Limite aprovações de USDC em https://revoke.cash
4. **Nunca commite `.env`**: Chaves privadas ficam apenas no `.env` local
5. **Auditoria**: Execute `pip-audit -r requirements.txt` antes de cada deploy
6. **Backups**: Mantenha backups da sua chave privada em local seguro offline

---

## Estrutura do Projeto

```
Trader Joe/
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py         # Classe abstrata
│   ├── macd_strategy.py         # Estratégia MACD
│   ├── rsi_strategy.py          # Estratégia RSI
│   ├── cvd_strategy.py          # Estratégia CVD
│   └── fvg_multitf_strategy.py  # FVG Multi-Timeframe
├── backtesting/
│   ├── __init__.py
│   ├── engine.py                # Motor de backtesting
│   └── metrics.py               # Métricas de performance
├── bot/
│   ├── __init__.py
│   ├── trader.py                # Execução de ordens
│   └── risk_manager.py          # Controle de risco
├── deploy/
│   ├── main.py                  # Ponto de entrada
│   ├── paper_trading.py         # Simulação offline
│   └── live_trading.py          # Trading real
├── data/                        # Dados locais (gitignored)
├── logs/                        # Logs de execução (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── security.md
├── security_checklist.md
└── CLAUDE.md
```

---

## Disclaimer Legal

Este software é fornecido **"como está"**, sem garantias de qualquer tipo.
Trading de ativos financeiros envolve risco substancial de perda.
Os autores não são responsáveis por perdas financeiras resultantes do uso deste software.
Este não é um conselho de investimento.
