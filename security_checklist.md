# security_checklist.md — Checklist Antes de Capital Real

Complete **todos** os itens antes de colocar qualquer valor real no bot.
Marque cada item após verificação pessoal.

---

## 🔐 Segurança de Credenciais

- [ ] Criei uma carteira dedicada separada da minha carteira principal
- [ ] A carteira dedicada tem no máximo US$ 100-300 depositados
- [ ] A chave privada está APENAS no arquivo `.env` local
- [ ] Verifiquei que `.env` está listado no `.gitignore`
- [ ] Executei `git status` e confirmei que `.env` não aparece como arquivo rastreado
- [ ] Não há nenhuma chave ou senha hardcoded em nenhum arquivo `.py`
- [ ] Fiz backup offline da chave privada (papel ou hardware wallet)
- [ ] As credenciais da API (key, secret, passphrase) foram geradas e testadas

## 🔍 Auditoria de Dependências

- [ ] Executei `pip-audit -r requirements.txt` e não há vulnerabilidades críticas
- [ ] Executei `safety check -r requirements.txt` sem alertas críticos
- [ ] Verifiquei o repositório do `py-clob-client` no GitHub (é oficial da Polymarket)
- [ ] Verifiquei a data de criação de cada pacote no PyPI (todos > 1 ano)
- [ ] Revisei o código de autenticação do `py-clob-client` brevemente

## 🏗️ Ambiente e Configuração

- [ ] Estou usando Python 3.10 ou superior (`python --version`)
- [ ] Estou usando um ambiente virtual isolado (`which python` aponta para `.venv`)
- [ ] O arquivo `.env` foi criado a partir do `.env.example` e preenchido corretamente
- [ ] Testei a conexão com a API com `python -c "from py_clob_client.client import ClobClient; print('OK')"`
- [ ] O `CHAIN_ID` está correto (137 para mainnet, 80002 para testnet)
- [ ] O `CLOB_HOST` está correto (`https://clob.polymarket.com`)

## 📊 Backtesting

- [ ] Baixei dados históricos do poly_data para o mercado que vou negociar
- [ ] Executei o backtest com pelo menos 3 meses de dados históricos
- [ ] O backtest mostra resultado positivo após custos (taxas estimadas)
- [ ] Analisei o drawdown máximo no backtest e estou confortável com ele
- [ ] Entendo que backtest não garante resultados futuros
- [ ] Testei todas as 4 estratégias no backtest e escolhi a mais adequada

## 📝 Paper Trading

- [ ] Rodei paper trading por pelo menos **14 dias corridos**
- [ ] O paper trading mostra resultado consistente (não apenas sorte)
- [ ] Analisei os trade logs do paper trading e entendo cada operação
- [ ] O RiskManager funcionou corretamente no paper trading (stop-loss, limites)
- [ ] O modo de emergência foi testado (drawdown artificial de 20%)
- [ ] Nenhum trade foi executado com market order (apenas limit orders)

## 🛡️ Controles de Risco

- [ ] `MAX_TRADE_SIZE` configurado em no máximo 10% do capital
- [ ] `MAX_LOSS_PER_TRADE` configurado em 10% do tamanho da posição
- [ ] `MAX_TRADES_PER_DAY` configurado em valor conservador (≤ 5)
- [ ] `EMERGENCY_DRAWDOWN` configurado em 20%
- [ ] Testei o modo de emergência: o bot para todas as operações corretamente
- [ ] Tenho um plano de contingência manual (como cancelar ordens no site)

## 🌐 Polymarket e Aprovações

- [ ] Acessei https://revoke.cash com a carteira dedicada
- [ ] Revoguei todas as aprovações desnecessárias de USDC
- [ ] Limitei o allowance de USDC ao valor mínimo necessário
- [ ] Entendo como cancelar ordens manualmente na interface do Polymarket
- [ ] Conheço o mercado que vou negociar (li a descrição do contrato)
- [ ] Entendo os riscos específicos de mercados de predição

## 🧠 Conhecimento e Psicologia

- [ ] Li o arquivo `security.md` completamente
- [ ] Entendo que mais de **90% dos traders perdem dinheiro**
- [ ] Estou disposto a perder **todo** o capital alocado sem impacto financeiro sério
- [ ] Não vou aumentar o capital por pelo menos 1 mês após iniciar live
- [ ] Tenho horários definidos para monitorar o bot (não deixar rodar sem supervisão)
- [ ] Sei como desligar o bot em emergência (`Ctrl+C` + cancelar ordens no site)

---

## ✅ Assinatura

Ao completar este checklist, você confirma que:
1. Leu e entendeu todos os riscos envolvidos
2. Tomou todas as precauções de segurança listadas
3. Está usando capital que pode perder totalmente
4. Não está investindo dinheiro de terceiros

**Data de conclusão**: _______________
**Capital alocado**: US$ _______________
**Estratégia escolhida**: _______________
**Mercado escolhido**: _______________

---

> Revise este checklist mensalmente ou sempre que mudar o capital alocado.
