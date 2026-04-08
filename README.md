# 📊 IAGenix AWS FinOps Simulator

Pacote Python que conecta na sua conta AWS, analisa custos, reservas, recursos ociosos e gera um **relatório HTML completo** com recomendações reais de otimização — orientado por princípios de FinOps.

Pensado para times que querem visibilidade rápida de **onde o dinheiro está indo** e **o que pode ser feito hoje** para reduzir o bill, sem precisar montar um pipeline de dados ou pagar uma ferramenta de terceiros.

---

## 🏗️ Arquitetura

Estrutura em **3 camadas** seguindo princípios do Twelve-Factor App e Single Responsibility:

```
iagenix-finops/
├── finops.py                    # CLI entry point (105 linhas)
├── requirements.txt             # boto3
├── README.md
└── iagenix_finops/              # Python package
    ├── __init__.py              # Re-exports + version
    ├── config.py                # Constantes, EC2 pricing, env vars
    ├── collectors.py            # 🔵 DATA: extração AWS via boto3
    ├── simulator.py             # 🟢 LOGIC: orquestrador FinOps
    └── reporter.py              # 🟣 PRESENTATION: gerador HTML
```

| Camada | Arquivo | Responsabilidade |
|---|---|---|
| 🔵 **Data Extraction** | `collectors.py` | Apenas chamadas boto3 (Cost Explorer, EC2, RDS, OpenSearch, ElastiCache, Redshift, Savings Plans, CloudWatch, ELBv2). Zero regra de negócio. |
| 🟢 **Business Logic** | `simulator.py` | Inicializa sessão AWS, cria clientes, mantém o dict `findings`, orquestra os collectors em sequência via `run()`. |
| 🟣 **Presentation** | `reporter.py` | Função `generate_html_report(findings, output_path)` que opera puramente sobre o dict. Pode ser testada offline com mocks. |

A separação permite:
- **Testar offline** o gerador de HTML (passa um dict de mock)
- **Trocar a apresentação** sem mexer na coleta (ex: gerar PDF, JSON, Slack)
- **Mockear collectors** em testes unitários
- **Escalar** para multi-account no futuro sem refatoração massiva

---

## ✨ O que o relatório gera

- Comparativo entre os **dois últimos meses fechados** + forecast do mês corrente
- **Tendência de 6 meses** (gráfico de linha)
- **Top 10 serviços** + análise de variação serviço a serviço
- **Reserved Instances** com utilização agregada por serviço + detalhamento por RI individual com expiração
- **Savings Plans** com métricas de utilização + tabela de SPs ativos com data de expiração
- **Recursos ociosos**: EBS órfãos, EIPs, snapshots antigos, EC2 com CPU baixa, Load Balancers sem targets
- **Governança e tags** com EC2s sem tags obrigatórias
- **🛒 Recomendações de COMPRA** de SP/RI direto da API do Cost Explorer
- **💡 Recomendações detalhadas** com priorização ALTA/MÉDIA/BAIXA + economia estimada
- **🗺️ Roadmap** de quick wins / médio prazo / longo prazo

---

## 📋 Pré-requisitos

- Python 3.8+
- Credenciais AWS configuradas (via `aws configure`, env vars ou perfil IAM)

### Permissões IAM mínimas

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
python3 -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

---

## 🎯 Como usar

### Como CLI (uso normal)

```bash
# Básico — usa região default sa-east-1
python3 finops.py

# Mudando a região
python3 finops.py --region us-east-1

# Usando perfil AWS específico
python3 finops.py --profile producao --region us-west-2

# Customizando arquivo de saída
python3 finops.py --output relatorio-marco.html

# Customizando tags obrigatórias (recursos sem essas tags são flagados)
python3 finops.py --tag-keys env,projeto,product

# Salvando findings em JSON (para CI/pipelines)
python3 finops.py --json findings.json

# Modo debug do Savings Plans (breakdown raw da API)
python3 finops.py --debug-sp

# Combinando tudo
python3 finops.py \
  --profile prod \
  --region sa-east-1 \
  --tag-keys env,projeto,product \
  --output relatorio-prod-$(date +%Y%m%d).html \
  --json findings-prod.json
```

### Como package Python (uso programático)

```python
from iagenix_finops import FinOpsSimulator, generate_html_report

# Inicializa e roda
sim = FinOpsSimulator(
    region="sa-east-1",
    profile="producao",
    tag_keys=["Environment", "Owner", "CostCenter"],
)
findings = sim.run()

# Gera HTML
generate_html_report(findings, "report.html")

# Ou processa o dict programaticamente
total_idle = sum(r["monthly_cost"] for r in findings["idle_resources"])
print(f"Recursos ociosos: ${total_idle:,.2f}/mês")

# Ou exporta JSON pra outro pipeline
import json
with open("findings.json", "w") as f:
    json.dump(findings, f, indent=2, default=str)
```

### Configuração via variáveis de ambiente (Twelve-Factor)

Todas as constantes em `config.py` podem ser sobrescritas via env vars:

```bash
export FINOPS_REGION=us-east-1
export FINOPS_LOOKBACK_DAYS=60
export FINOPS_UTILIZATION_THRESHOLD=85.0
export FINOPS_CPU_IDLE_THRESHOLD=10.0
export FINOPS_CPU_IDLE_MAX_THRESHOLD=25.0
export FINOPS_REQUIRED_TAGS="Environment,Project,Owner,CostCenter"

python3 finops.py
```

Útil para containers, CI/CD, AWS Lambda, etc.

---

## 📖 Referência completa de CLI

| Opção | Default | Descrição |
|---|---|---|
| `--region` | `sa-east-1` | Região AWS para análise |
| `--profile` | (default credentials) | Perfil AWS de `~/.aws/credentials` |
| `--output` | `finops_report.html` | Caminho do arquivo HTML de saída |
| `--json` | (não salva) | Caminho opcional para salvar findings em JSON |
| `--tag-keys` | `Environment,Project,Owner` | Tags obrigatórias separadas por vírgula |
| `--debug-sp` | `false` | Imprime breakdown raw da API CE para Savings Plans |
| `-h, --help` | - | Mostra ajuda |

---

## 🔧 Estendendo o simulator

A arquitetura em camadas facilita extensões. Alguns exemplos:

### Adicionar um novo collector

Edite `iagenix_finops/collectors.py` e adicione um método ao `FinOpsCollectorMixin`:

```python
def fetch_lambda_idle(self):
    """Detecta funções Lambda sem invocações nos últimos 30 dias."""
    print("[8/8] Procurando Lambdas ociosas...")
    lambda_client = self.session.client("lambda", region_name=self.region)
    # ... lógica boto3 ...
    self.findings["idle_lambdas"] = idle_list
```

E chame no `run()` em `simulator.py`:

```python
def run(self):
    # ... etapas existentes ...
    self.fetch_lambda_idle()
    return self.findings
```

### Trocar a apresentação por PDF

Crie `iagenix_finops/pdf_reporter.py`:

```python
def generate_pdf_report(findings, output_path):
    # Usa weasyprint, reportlab, etc — opera sobre o mesmo dict findings
    ...
```

E importe no seu script:

```python
from iagenix_finops import FinOpsSimulator
from iagenix_finops.pdf_reporter import generate_pdf_report

findings = FinOpsSimulator(region="sa-east-1").run()
generate_pdf_report(findings, "report.pdf")
```

A camada de coleta e o dict de findings ficam intactos.

### Notificar via Slack após rodar

```python
import requests
from iagenix_finops import FinOpsSimulator

findings = FinOpsSimulator(region="sa-east-1").run()
critical = [r for r in findings["recommendations"] if r.get("priority") == "ALTA"]

if critical:
    msg = f"⚠️ {len(critical)} recomendações críticas encontradas. "
    msg += f"Economia potencial: ${findings['total_potential_savings']:,.2f}/mês"
    requests.post("https://hooks.slack.com/services/...", json={"text": msg})
```

---

## 🔧 Troubleshooting

### `DataUnavailableException` no Savings Plans

Acontece quando a conta não tem nenhum SP ativo, ou os dados são muito recentes (<24h). O script tem 3 fallbacks (MONTHLY → DAILY → últimos 30d). Se as 3 falharem, simplesmente não mostra a seção e segue.

### `AccessDenied` em `savingsplans:DescribeSavingsPlans`

Adicione a permissão `savingsplans:DescribeSavingsPlans` à sua role. Sem ela, o script não consegue listar SPs ativos (mesmo se aparecem no console).

### Erro de credenciais

```bash
aws sts get-caller-identity   # verifica se as credenciais funcionam
aws configure list            # lista perfis disponíveis
```

### Custo $0 em EC2 com CPU baixa

A tabela de preços em `config.py` cobre os tipos mais comuns de sa-east-1 (t2/t3/t3a/m5/m5a/m6i/c5/r5/r5a). Para tipos exóticos há fallback heurístico baseado em vCPU. Para outras regiões, ajuste a constante `EC2_PRICE_SA_EAST_1`.

---

## 🛠️ Decisões técnicas relevantes

- **Cost Explorer global** (`us-east-1`) — todas as queries de custo passam por lá independente da `--region` passada
- **Demais APIs por região** — `ec2`, `rds`, `es`, `elasticache`, `redshift`, `cloudwatch`, `elbv2` usam a região do `--region`
- **End-exclusive nos períodos** — a API AWS usa `End` exclusivo (ex: `2026-04-01` para incluir 31/mar). O script trata isso e exibe datas inclusivas
- **AmortizedCost para SPs** — economia de Savings Plans usa `AmortizedCost` (não `UnblendedCost`), para refletir corretamente All Upfront / Partial Upfront / No Upfront
- **CPU idle requer 2 sinais** — EC2 só é considerada ociosa se CPU **média** < 5% E **pico** < 20% nos últimos 30 dias (evita falsos positivos em workloads com picos)
- **Mixin pattern** — `FinOpsSimulator` herda de `FinOpsCollectorMixin` em vez de composição, mantendo a interface pública limpa e o `self.findings` acessível por todos os métodos sem proxies

---

## 🤝 Contribuindo

Pull requests bem-vindos. Áreas que podem ser melhoradas:
- Suporte multi-region em uma única execução
- Integração com AWS Organizations (escaneio multi-account)
- Exportação para PDF
- Histórico comparativo entre execuções (snapshot diff)
- Notificações via Slack/Teams quando rodado em CI
- Suporte a SageMaker Savings Plans
- Detecção de Lambdas/Step Functions ociosas

---

## 📄 Licença

MIT — use à vontade em ambientes corporativos.

---

## ⚠️ Disclaimer

Os preços de EC2 hardcoded são estimativas para `sa-east-1`. Para análise financeira oficial, sempre confirme com o AWS Pricing Calculator e a fatura real.

As recomendações de SP/RI vêm direto do motor de ML do AWS Cost Explorer — são as mesmas que aparecem em **Cost Explorer → Recommendations**.

Este script é uma ferramenta de **diagnóstico** e **suporte à decisão**. Toda compra de RI ou SP deve passar por revisão humana e validação contra o uso histórico real.
