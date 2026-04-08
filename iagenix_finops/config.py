"""
iagenix_finops.config
=====================

Configurações estáticas e tabela de preços do simulador FinOps.

Este módulo segue o princípio Twelve-Factor de "store config in env":
todas as constantes podem ser sobrescritas via variáveis de ambiente
(ex: `FINOPS_REGION=us-east-1`) sem precisar mexer no código.
"""

import os


# ----------------------------------------------------------------------
# Configurações principais (lidas de env vars com fallback para defaults)
# ----------------------------------------------------------------------
REGION = os.getenv("FINOPS_REGION", "sa-east-1")
LOOKBACK_DAYS = int(os.getenv("FINOPS_LOOKBACK_DAYS", "30"))
UTILIZATION_THRESHOLD = float(os.getenv("FINOPS_UTILIZATION_THRESHOLD", "80.0"))
CPU_IDLE_THRESHOLD = float(os.getenv("FINOPS_CPU_IDLE_THRESHOLD", "5.0"))
CPU_IDLE_MAX_THRESHOLD = float(os.getenv("FINOPS_CPU_IDLE_MAX_THRESHOLD", "20.0"))

# Tags obrigatórias - lista separada por vírgula em FINOPS_REQUIRED_TAGS
_default_tags = "Environment,Project,Owner"
REQUIRED_TAGS = [
    t.strip()
    for t in os.getenv("FINOPS_REQUIRED_TAGS", _default_tags).split(",")
    if t.strip()
]


# ----------------------------------------------------------------------
# Tabela de preços EC2 sa-east-1 (USD/hora On-Demand)
# Fonte: AWS Pricing Calculator. Aproximação para estimativa de custo de
# instâncias ociosas detectadas. Para outras regiões, ajuste esta tabela
# ou use a API oficial AWS Pricing.
# ----------------------------------------------------------------------
EC2_PRICE_SA_EAST_1 = {
    # T3 burstable (general-purpose)
    "t3.nano": 0.0066, "t3.micro": 0.0132, "t3.small": 0.0264, "t3.medium": 0.0528,
    "t3.large": 0.1056, "t3.xlarge": 0.2112, "t3.2xlarge": 0.4224,
    # T3a (AMD)
    "t3a.nano": 0.0059, "t3a.micro": 0.0119, "t3a.small": 0.0238, "t3a.medium": 0.0475,
    "t3a.large": 0.0950, "t3a.xlarge": 0.1901, "t3a.2xlarge": 0.3802,
    # T2 (legacy burstable)
    "t2.nano": 0.0079, "t2.micro": 0.0158, "t2.small": 0.0316, "t2.medium": 0.0632,
    "t2.large": 0.1264, "t2.xlarge": 0.2528, "t2.2xlarge": 0.5056,
    # M5 (general-purpose)
    "m5.large": 0.1200, "m5.xlarge": 0.2400, "m5.2xlarge": 0.4800,
    "m5.4xlarge": 0.9600, "m5.8xlarge": 1.9200, "m5.12xlarge": 2.8800,
    # M5a (AMD)
    "m5a.large": 0.1080, "m5a.xlarge": 0.2160, "m5a.2xlarge": 0.4320,
    # M6i (Intel Ice Lake)
    "m6i.large": 0.1200, "m6i.xlarge": 0.2400, "m6i.2xlarge": 0.4800,
    # C5 (compute-optimized)
    "c5.large": 0.1060, "c5.xlarge": 0.2120, "c5.2xlarge": 0.4240,
    "c5.4xlarge": 0.8480, "c5.9xlarge": 1.9080,
    # R5 (memory-optimized)
    "r5.large": 0.1580, "r5.xlarge": 0.3160, "r5.2xlarge": 0.6320,
    "r5.4xlarge": 1.2640, "r5.8xlarge": 2.5280,
    # R5a (AMD)
    "r5a.large": 0.1422, "r5a.xlarge": 0.2844, "r5a.2xlarge": 0.5688,
}

# Preços fixos auxiliares para sa-east-1 (estimativas)
EBS_GB_MONTH_PRICE = 0.10        # gp3 ~$0.10/GB-mês
EBS_SNAPSHOT_GB_MONTH_PRICE = 0.05
EIP_IDLE_HOURLY_PRICE = 3.60     # ~$0.005/h * 730h
ELB_BASE_MONTHLY_PRICE = 18.40   # ALB/NLB ~$0.0252/h * 730h


def estimate_ec2_monthly_cost(instance_type: str) -> float:
    """
    Estima custo mensal On-Demand para uma instância EC2 em sa-east-1.

    Usa lookup direto na tabela `EC2_PRICE_SA_EAST_1`. Se o tipo não estiver
    mapeado, aplica fallback heurístico baseado no tamanho e número de vCPUs.
    """
    hourly = EC2_PRICE_SA_EAST_1.get(instance_type)
    if hourly is None:
        try:
            _family, size = instance_type.split(".")
            base_per_vcpu = 0.06  # estimativa conservadora sa-east-1
            size_map = {
                "nano": 0.25, "micro": 0.5, "small": 1, "medium": 2,
                "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
                "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96,
            }
            vcpu = size_map.get(size, 2)
            hourly = base_per_vcpu * vcpu
        except Exception:
            hourly = 0.10  # default seguro
    return round(hourly * 730, 2)  # 730 horas/mês = 24h * ~30.4 dias
