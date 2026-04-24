# CLAUDE.md — Polymarket Trading Bot
# Este arquivo é lido automaticamente pelo Claude Code a cada sessão.

## Visão Geral do Projeto

Bot de trading para Polymarket usando Python. Negocia mercados de predição
via CLOB (Central Limit Order Book) com estratégias técnicas.

## Estrutura de Pastas

```
Trader Joe/
├── strategies/          # 4 estratégias de trading
│   ├── base_strategy.py     # Classe abstrata base
│   ├── macd_strategy.py     # MACD via biblioteca `ta`
│   ├── rsi_strategy.py      # RSI via biblioteca `ta`
│   ├── cvd_strategy.py      # CVD (Cumulative Volume Delta) custom
│   └── fvg_multitf_strategy.py  # FVG multi-timeframe 4H+15M
├── backtesting/         # Engine de backtesting
│   ├── engine.py            # Motor principal com suporte multi-TF
│   └── metrics.py           # Métricas de performance
├── bot/                 # Lógica de execução
│   ├── trader.py            # Trader (py-clob-client, limit orders)
│   └── risk_manager.py      # Controle de risco (stop, drawdown, daily limit)
├── deploy/              # Scripts de execução
│   ├── main.py              # Ponto de entrada principal
│   ├── paper_trading.py     # Simulação 100% offline
│   └── live_trading.py      # Trading real (use com cautela)
├── .env.example         # Template de variáveis de ambiente
├── requirements.txt     # Dependências fixas
├── README.md            # Documentação completa
├── security.md          # Regras de segurança
└── security_checklist.md # Checklist antes de capital real
```

## Decisões Técnicas Importantes

### SDK e API
- **Exclusivamente py-clob-client** para toda comunicação com Polymarket
- **Apenas limit orders** — nunca market orders (risco de slippage)
- Autenticação via chave privada + API credentials no `.env`

### Dados
- Dados históricos via **poly_data** (warproxxx/poly_data no GitHub)
- Candles 15M e 4H construídos via `pandas.resample()` a partir de tick data
- Paper trading usa dados locais — **zero conexão API**

### Estratégias
- `MACDStrategy`: MACD(12,26,9) — sinal de cruzamento com filtro de tendência
- `RSIStrategy`: RSI(14) — oversold/overbought com confirmação de volume
- `CVDStrategy`: CVD custom — acumulação/distribuição inteligente
- `FVGMultiTFStrategy`: FVG 4H (bias) + FVG/fill 15M (entrada) — a mais complexa

### FVG (Fair Value Gap)
- **Bullish FVG**: `candle[i].low > candle[i-2].high` com body size > 50% do range
- **Bearish FVG**: `candle[i].high < candle[i-2].low` com body size > 50% do range
- FVG inválido se > 10 candles de idade
- 4H define bias; 15M confirma entrada

### Risk Manager
- Stop-loss máximo: 10% do tamanho da posição
- Limite diário: configurável via `MAX_TRADES_PER_DAY`
- Modo emergência: para tudo se drawdown >= 20%
- Todas as ordens são limit (nunca market)

## Fluxo de Desenvolvimento Recomendado

1. `python deploy/main.py --mode backtest` → valide a estratégia
2. `python deploy/main.py --mode paper` → simule por 2+ semanas
3. `python deploy/main.py --mode live` → apenas com capital pequeno ($100)

## Dependências Principais

```
py-clob-client==0.18.0  # SDK Polymarket
pandas==2.2.2            # Dados e resampling
numpy==1.26.4            # Cálculos numéricos
ta==0.11.0               # MACD e RSI
python-dotenv==1.0.1     # Variáveis de ambiente
requests==2.32.3         # HTTP
```

## Avisos Críticos

- **NUNCA** use a carteira principal com este bot
- **NUNCA** commite `.env` ou chaves privadas
- **SEMPRE** teste paper trading por 2 semanas antes de capital real
- Mais de 90% dos traders de predição perdem dinheiro
- Execute `pip-audit -r requirements.txt` antes de cada deploy

## Recursos Externos

- poly_data: https://github.com/warproxxx/poly_data (dados históricos, nota 9/10)
- polyterm: https://github.com/NYTEMODEONLY/polyterm (explorar mercados, nota 8/10)
- agents: https://github.com/polymarket/agents (referência oficial, nota 8/10)
- Docs CLOB: https://docs.polymarket.com
- Revoke.cash: https://revoke.cash (limitar aprovações USDC)
