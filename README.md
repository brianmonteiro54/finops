# 📊 IAGenix AWS FinOps Simulator

Script Python que conecta na sua conta AWS, analisa custos, reservas, recursos ociosos e gera um **relatório HTML completo** com recomendações reais de otimização — tudo orientado por princípios de FinOps.

Pensado para equipes que querem visibilidade rápida de **onde o dinheiro está indo** e **o que pode ser feito hoje** para reduzir o bill, sem precisar montar um pipeline de dados ou pagar uma ferramenta de terceiros.

---

## ✨ O que o relatório gera

- **Comparativo entre os dois últimos meses fechados** + forecast do mês corrente
- **Tendência de custo dos últimos 6 meses** (gráfico de linha)
- **Top 10 serviços** por custo + análise de variação serviço a serviço
- **Inventário detalhado de Reserved Instances** com data de expiração, utilização e ação recomendada
- **Análise consolidada de Savings Plans** com classificação local vs externo (multi-account)
- **Recursos ociosos detectados**: volumes EBS órfãos, EIPs não associados, snapshots antigos, EC2 com CPU baixa, Load Balancers sem targets
- **Governança e tags** com lista de recursos sem tags obrigatórias
- **🛒 Recomendações de COMPRA** de SP/RI direto da API do AWS Cost Explorer (mesmas recomendações que aparecem no console AWS)
- **💡 Recomendações de FinOps detalhadas** com priorização ALTA/MÉDIA/BAIXA, custo atual de cada serviço afetado e economia estimada
- **🗺️ Roadmap de ação** dividido em quick wins / médio prazo / longo prazo

---

## 📋 Pré-requisitos

- Python 3.8+
- `boto3` instalado (`pip install boto3`)
- Credenciais AWS configuradas (via `aws configure`, variáveis de ambiente ou perfil IAM)
- Permissões IAM listadas abaixo

### Permissões IAM mínimas

Anexe esta policy ao usuário/role que vai executar o script:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ce:GetCostAndUsage",
      "ce:GetReservationUtilization",
      "ce:GetSavingsPlansUtilization",
      "ce:GetSavingsPlansPurchaseRecommendation",
      "ce:GetReservationPurchaseRecommendation",
      "ec2:DescribeInstances",
      "ec2:DescribeVolumes",
      "ec2:DescribeAddresses",
      "ec2:DescribeSnapshots",
      "ec2:DescribeReservedInstances",
      "rds:DescribeReservedDBInstances",
      "es:DescribeReservedElasticsearchInstances",
      "es:ListDomainNames",
      "elasticache:DescribeReservedCacheNodes",
      "redshift:DescribeReservedNodes",
      "savingsplans:DescribeSavingsPlans",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
      "cloudwatch:GetMetricStatistics",
      "sts:GetCallerIdentity"
    ],
    "Resource": "*"
  }]
}
```

---

## 🚀 Instalação

```bash
git clone https://github.com/brianmonteiro54/finops.git
cd finops
pip install boto3
```

Recomendado usar virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows
pip install boto3
```

---

## 🎯 Como usar

### Comando básico (região default sa-east-1)

```bash
python3 finops_simulator.py
```

Gera `finops_report.html` no diretório atual usando suas credenciais AWS padrão.

### Mudando a região

```bash
python3 finops_simulator.py --region us-east-1
python3 finops_simulator.py --region eu-west-1
```

### Usando um perfil AWS específico

```bash
python3 finops_simulator.py --profile producao
python3 finops_simulator.py --profile cliente-x --region us-west-2
```

### Customizando o arquivo de saída

```bash
python3 finops_simulator.py --output relatorio-marco.html
python3 finops_simulator.py --output /tmp/finops/conta-prod.html
```

### Customizando as tags obrigatórias

Por padrão o script verifica se EC2s têm pelo menos uma das tags `Environment`, `Project` ou `Owner`. Se sua organização usa outras convenções, passe via `--tag-keys`:

```bash
python3 finops_simulator.py --tag-keys env,projeto,product
python3 finops_simulator.py --tag-keys CostCenter,Application,Team
```

### Salvando os findings em JSON (para pipelines/CI)

```bash
python3 finops_simulator.py --json findings.json
```

Útil para integrar com Slack/Teams notifications ou consumir em outros scripts.

### Modo debug do Savings Plans

Imprime breakdown completo por `RECORD_TYPE` da API Cost Explorer (todos os meses) — útil para validar manualmente contra o console AWS de onde vem cada valor de SP/RI:

```bash
python3 finops_simulator.py --debug-sp
```

### Combinando opções

```bash
python3 finops_simulator.py \
  --profile prod \
  --region sa-east-1 \
  --tag-keys cnj-env,Projeto,cnj-product \
  --output relatorio-prod-$(date +%Y%m%d).html \
  --json findings-prod.json \
  --debug-sp
```

---

## 📖 Referência completa de CLI

| Opção | Default | Descrição |
|---|---|---|
| `--region` | `sa-east-1` | Região AWS para análise (Cost Explorer global, demais APIs por região) |
| `--profile` | (default credentials) | Perfil AWS configurado em `~/.aws/credentials` |
| `--output` | `finops_report.html` | Caminho do arquivo HTML de saída |
| `--json` | (não salva) | Caminho opcional para salvar findings em JSON |
| `--tag-keys` | `Environment,Project,Owner` | Tags obrigatórias separadas por vírgula. Recurso é flagado se NÃO tiver nenhuma delas |
| `--debug-sp` | `false` | Imprime breakdown raw da API Cost Explorer para Savings Plans |
| `-h, --help` | - | Mostra ajuda |

---

## 📊 O que cada seção do relatório significa

### 1️⃣ Resumo Executivo
Cards com saúde geral da conta (semáforo), economia potencial mensal/anual, total de RIs, recursos ociosos detectados e quantidade de recomendações.

### 2️⃣ Comparativo entre meses fechados
Compara os DOIS últimos meses completos (não meses parciais). Se hoje for 7 de abril, compara fevereiro inteiro vs março inteiro. Mostra também o forecast do mês corrente baseado em extrapolação linear.

### 3️⃣ Tendência de 6 meses
Gráfico de linha do custo total mês a mês. Linha plana ou descendente é saudável.

### 4️⃣ Reserved Instances - Detalhamento Completo
Tabela com cada RI ativa: ID, tipo, quantidade, pagamento, **data de expiração**, **dias restantes**, **utilização agregada**, **desperdício** e **ação recomendada** (combina expiração + utilização inteligentemente). Ordenado por urgência.

### 5️⃣ Savings Plans - Utilização & Expiração
Métricas de utilização (% de uso, compromisso, economia líquida) + tabela de SPs ativos com data de expiração. Inclui detecção de SPs locais vs externos (multi-account).

### 6️⃣ Recursos Ociosos
Quick wins agrupados por tipo: volumes EBS não anexados, EIPs órfãos, snapshots antigos, EC2 com CPU baixa (média + pico) e Load Balancers sem targets healthy. Cada item com custo mensal estimado.

### 7️⃣ Governança & Tags
Lista de EC2s sem nenhuma das tags obrigatórias.

### 8️⃣ 🛒 Recomendações de Compra (SP + RI)
Análise gerada pelo motor de ML do AWS Cost Explorer (lookback de 60 dias):
- **Análise do workload de compute** (mín/médio/pico horário, classificação de estabilidade)
- **Comparação lado-a-lado** de todas as combinações de SP (1y/3y × Sem/Parcial/Total upfront)
- **Tabela de RIs por serviço** com instância exata, quantidade recomendada, payback, upfront e economia mensal/anual
- **Guia de decisão** adaptado à estabilidade detectada do workload

### 9️⃣ 💡 Recomendações Detalhadas
Combina **itens detectados** na sua conta + **boas práticas universais** de FinOps. Cada recomendação tem:
- Prioridade (ALTA/MÉDIA/BAIXA)
- Custo atual do serviço afetado (puxado do Cost Explorer)
- Economia estimada em valores absolutos ($/mês e $/ano)
- Ação concreta com caminho exato no console AWS
- Esforço estimado

### 🗺️ Roadmap
Quick wins (esta semana) → Médio prazo (este mês) → Longo prazo (próximo trimestre).

---

## 🔧 Troubleshooting



### "100% de economia" em meses sem SP local

Esse caso é tratado e indica que o SP está em outra conta da Organization (geralmente a payer). O relatório mostra um badge "🔗 SP externo" e uma nota explicando. Use `--debug-sp` para confirmar.

### Erro de credenciais

```bash
aws sts get-caller-identity   # verifica se as credenciais funcionam
aws configure list            # lista perfis disponíveis
```

### Custo $0 em EC2 com CPU baixa

A tabela de preços hardcoded cobre os tipos mais comuns de sa-east-1 (t2/t3/t3a/m5/m5a/m6i/c5/r5/r5a). Para tipos exóticos, há um fallback heurístico baseado em vCPU. Para outras regiões, ajuste a constante `EC2_PRICE_SA_EAST_1` no topo do arquivo.

---

## 🛠️ Como o script foi pensado

- **Cost Explorer endpoint global** (`us-east-1`) — todas as queries de custo passam por lá
- **Demais APIs** (`ec2`, `rds`, `es`, `elasticache`, `redshift`, `cloudwatch`, `elbv2`) usam a região passada via `--region`
- **End-exclusive nos períodos**: a API AWS usa `End` exclusivo (ex: `2026-04-01` para incluir dia 31 de março). O script trata isso e mostra datas inclusivas na exibição
- **AmortizedCost para SPs**: a economia de Savings Plans é calculada usando `AmortizedCost` (não `UnblendedCost`), o que permite refletir corretamente All Upfront / Partial Upfront / No Upfront
- **Detecção de RECORD_TYPE para SP local vs externo**: usa `SavingsPlanRecurringFee.Unblended > 0` como sinal definitivo de "esta conta paga o SP"

---

## 🤝 Contribuindo

Pull requests são bem-vindos. Áreas que podem ser melhoradas:

- Suporte a múltiplas regiões em um único relatório
- Integração com AWS Organizations (escaneio multi-account)
- Exportação para PDF
- Histórico comparativo entre execuções (snapshot diff)
- Notificações via Slack/Teams quando rodado em CI
- Suporte a cobertura de SageMaker SP

---

## 📄 Licença

MIT — use à vontade em ambientes corporativos, modifique como quiser.

---

## ⚠️ Disclaimer

Os preços de EC2 hardcoded são estimativas para `sa-east-1` em valores aproximados de On-Demand. Para análise financeira oficial, sempre confirme com o AWS Pricing Calculator e a fatura real.

As recomendações de SP/RI vêm direto do motor de ML da AWS Cost Explorer — são as mesmas que aparecem no console em **Cost Explorer → Recommendations**.

Este script é uma ferramenta de **diagnóstico** e **suporte à decisão**. Toda compra de RI ou SP deve passar por revisão humana e validação contra o uso histórico real.
