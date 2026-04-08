"""
iagenix_finops.simulator
========================

Camada de orquestração / regra de negócio.

A classe `FinOpsSimulator` é responsável por:

  1. Inicializar a sessão AWS e os clientes boto3 necessários
  2. Manter a estrutura `findings` que acumula todos os achados
  3. Coordenar a execução dos collectors em sequência (`run()`)
  4. Aplicar pós-processamento agregado (somar economia total, etc.)

A extração de dados em si (chamadas boto3) está em `collectors.py` —
esta classe herda do `FinOpsCollectorMixin` e expõe os métodos via
herança múltipla padrão Python (mixin pattern).
"""

import sys
from datetime import datetime

import boto3
from botocore.exceptions import NoCredentialsError, ProfileNotFound

from .collectors import FinOpsCollectorMixin
from .config import REGION, REQUIRED_TAGS


class FinOpsSimulator(FinOpsCollectorMixin):
    """
    Orquestrador principal da análise FinOps de uma conta AWS.

    Args:
        region: Região AWS para análise (default: sa-east-1)
        profile: Nome do perfil AWS em ~/.aws/credentials (opcional)
        tag_keys: Lista de tags obrigatórias a verificar
                  (default: Environment, Project, Owner)
        debug_sp: Se True, imprime breakdown raw do Cost Explorer
                  para Savings Plans (útil para troubleshooting)

    Uso:
        sim = FinOpsSimulator(region="sa-east-1", profile="prod")
        findings = sim.run()
    """

    def __init__(self, region=REGION, profile=None, tag_keys=None, debug_sp=False):
        self.region = region
        self.required_tags = tag_keys if tag_keys else REQUIRED_TAGS
        self.debug_sp = debug_sp

        # ----- Inicialização da sessão AWS e clientes boto3 -----
        try:
            session_args = {"region_name": region}
            if profile:
                session_args["profile_name"] = profile
            self.session = boto3.Session(**session_args)

            # Cost Explorer SEMPRE em us-east-1 (endpoint global)
            self.ce = self.session.client("ce", region_name="us-east-1")
            self.ec2 = self.session.client("ec2", region_name=region)
            self.rds = self.session.client("rds", region_name=region)
            self.es = self.session.client("es", region_name=region)
            self.elasticache = self.session.client("elasticache", region_name=region)
            self.redshift = self.session.client("redshift", region_name=region)
            self.savingsplans = self.session.client("savingsplans", region_name="us-east-1")
            self.cloudwatch = self.session.client("cloudwatch", region_name=region)
            self.elbv2 = self.session.client("elbv2", region_name=region)
        except (NoCredentialsError, ProfileNotFound) as e:
            print(f"ERRO de credenciais AWS: {e}")
            sys.exit(1)

        # ----- Estrutura de findings (alimentada pelos collectors) -----
        self.findings = {
            "account_id": self._get_account_id(),
            "region": region,
            "generated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "required_tags": list(self.required_tags),
            "cost_summary": {},
            "reserved_utilization": [],
            "savings_plans_utilization": [],
            "ri_inventory": {},
            "ri_details": [],
            "sp_details": [],
            "purchase_recommendations": {
                "savings_plans": [],
                "reserved_instances": [],
                "total_sp_savings": 0,
                "total_ri_savings": 0,
            },
            "idle_resources": [],
            "untagged_resources": [],
            "recommendations": [],
            "total_potential_savings": 0.0,
        }

    def _get_account_id(self):
        """Retorna o Account ID via STS GetCallerIdentity."""
        try:
            return self.session.client("sts").get_caller_identity()["Account"]
        except Exception:
            return "unknown"

    def run(self):
        """
        Executa toda a análise FinOps em sequência.

        Cada etapa é um collector que popula uma seção do `self.findings`.
        Ao final, retorna o dict de findings consolidado pronto para ser
        passado ao `generate_html_report()`.
        """
        print(f"\n{'='*60}\n IAGenix FinOps Simulator - Região: {self.region}\n{'='*60}")
        self.fetch_cost_summary()
        self.fetch_ri_utilization()
        self.fetch_savings_plans_utilization()
        self.inventory_reserved_resources()
        self.find_idle_resources()
        self.find_untagged_resources()
        self.fetch_purchase_recommendations()
        self.findings["total_potential_savings"] = round(
            self.findings["total_potential_savings"], 2
        )
        print(
            f"\n💰 Economia potencial total estimada: "
            f"${self.findings['total_potential_savings']:,.2f}/mês\n"
        )
        return self.findings
