# security.md — Regras de Segurança

## Regras Não Negociáveis

### 1. Nunca Use Sua Carteira Principal

Crie uma carteira **exclusivamente** para este bot:
- Metamask: Criar nova conta → exportar chave privada → colocar no `.env`
- Deposite no máximo **US$ 100-300** para iniciar
- Mantenha a carteira principal desconectada deste projeto

### 2. Proteja Suas Chaves Privadas

```
❌ NUNCA faça isso:
   - git commit com chave privada
   - Compartilhar .env com alguém
   - Colocar chave em variável de código
   - Subir chave para qualquer serviço de nuvem

✅ SEMPRE faça isso:
   - Chave apenas no arquivo .env local
   - .env está no .gitignore (verifique!)
   - Backup offline em papel ou hardware wallet
   - Rotacione credenciais da API periodicamente
```

### 3. Limite Aprovações de USDC — Use Revoke.cash

Antes de interagir com Polymarket, acesse https://revoke.cash e:
1. Conecte sua carteira dedicada
2. Revogue aprovações desnecessárias
3. Limite o allowance de USDC ao valor necessário
4. Repita esta verificação mensalmente

### 4. Auditoria de Dependências

Execute **obrigatoriamente** antes de qualquer deploy:

```bash
pip install pip-audit
pip-audit -r requirements.txt

# Alternativa com safety:
pip install safety
safety check -r requirements.txt
```

Para cada pacote, verifique manualmente:
- Idade do repositório no GitHub (prefira > 1 ano)
- Número de stars e downloads no PyPI
- Quem são os mantenedores
- Histórico de CVEs (vulnerabilidades)

### 5. Auditoria dos Pacotes Principais

| Pacote | GitHub | PyPI | Observação |
|--------|--------|------|------------|
| py-clob-client | github.com/Polymarket/py-clob-client | ✅ Oficial | Leia o código de autenticação |
| pandas | github.com/pandas-dev/pandas | ✅ Estável | Projeto maduro |
| numpy | github.com/numpy/numpy | ✅ Estável | Projeto maduro |
| ta | github.com/bukosabino/ta | ✅ Estável | Biblioteca simples |
| python-dotenv | github.com/theskumar/python-dotenv | ✅ Estável | Amplamente usado |
| requests | github.com/psf/requests | ✅ Estável | Padrão da indústria |

### 6. Variáveis de Ambiente

```bash
# Verifique que .env está no .gitignore ANTES de qualquer commit
cat .gitignore | grep ".env"

# Verifique que não há chaves em nenhum arquivo Python
grep -r "PRIVATE_KEY\|api_secret\|passphrase" *.py strategies/ bot/ deploy/
# Resultado esperado: nenhuma ocorrência de valores hardcoded
```

### 7. Comece Pequeno — Escale Gradualmente

```
Semana 1-2:  Paper trading (zero capital)
Semana 3-4:  Live com $10-20 (aprendizado)
Mês 2:       Live com $50-100 (validação)
Mês 3+:      Decisão informada sobre escalar
```

**Nunca** aumente o capital antes de entender completamente:
- Por que o backtest funciona (overfitting?)
- Por que o paper trading funciona
- Qual é o drawdown máximo histórico
- Qual é a taxa de acerto e risk/reward real

### 8. Monitore Ativamente

- **Não deixe o bot rodar sem supervisão** nas primeiras semanas
- Configure alertas de email/SMS para trades executados
- Verifique o portfolio na Polymarket diariamente
- Implemente alertas de drawdown no RiskManager

### 9. Gestão de Incidentes

Se algo der errado:
1. `Ctrl+C` para parar o bot imediatamente
2. Acesse https://polymarket.com e verifique posições abertas
3. Cancele todas as ordens manualmente se necessário
4. Acesse Revoke.cash e revogue todas as aprovações
5. Mova fundos para outra carteira se comprometida

### 10. Dependências Transitivas

O `pip-audit` verifica dependências diretas e transitivas. Porém, sempre verifique:

```bash
# Ver todas as dependências instaladas
pip list

# Ver árvore de dependências
pip install pipdeptree
pipdeptree --packages py-clob-client,pandas,ta
```

---

## Histórico de Vulnerabilidades Conhecidas

Mantenha este arquivo atualizado com CVEs relevantes:

| Data | Pacote | CVE | Versão Afetada | Ação |
|------|--------|-----|----------------|------|
| — | — | — | — | Monitorar |

Acompanhe: https://pypi.org/security/ e https://github.com/advisories
