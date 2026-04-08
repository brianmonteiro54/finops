#!/usr/bin/env python3
"""
========================================================================
 IAGenix Cloud - AWS FinOps Simulator & Reserved Capacity Verifier
========================================================================
 Objetivo:
   1) Simular oportunidades de melhoria FinOps na conta AWS
   2) Verificar se os recursos RESERVADOS (RIs e Savings Plans) estão
      realmente sendo utilizados (incluindo Amazon OpenSearch)
   3) Gerar relatório HTML no mesmo estilo do modelo IAGenix
   4) Foco na região sa-east-1 (São Paulo)

 Serviços analisados:
   - EC2  (Reserved Instances + Savings Plans)
   - RDS  (Reserved Instances)
   - ElastiCache (Reserved Nodes)
   - Amazon OpenSearch Service (Reserved Instances)
   - Redshift (Reserved Nodes)
   - Recursos ociosos (volumes não anexados, EIPs livres, etc.)
   - Recursos sem tags (governança)

 Requisitos:
   pip install boto3
   AWS credentials configuradas (~/.aws/credentials, env vars ou IAM Role)
   Permissões: ce:*, ec2:Describe*, rds:Describe*, es:Describe*,
               elasticache:Describe*, redshift:Describe*, savingsplans:*,
               cloudwatch:GetMetricStatistics
========================================================================
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
except ImportError:
    print("ERRO: boto3 não instalado. Execute: pip install boto3")
    sys.exit(1)


# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
REGION = "sa-east-1"   # São Paulo
LOOKBACK_DAYS = 30     # Janela para análise de utilização
UTILIZATION_THRESHOLD = 80.0   # % abaixo disso = subutilizado
CPU_IDLE_THRESHOLD = 5.0       # CPU médio < 5% = candidato a desligamento
CPU_IDLE_MAX_THRESHOLD = 20.0  # Pico (Max) precisa também estar abaixo disso
REQUIRED_TAGS = ["Environment", "Project", "Owner"]  # Tags obrigatórias (configurável via --tag-keys)

# Tabela de preços EC2 sa-east-1 (USD/hora On-Demand) - aproximação para estimativa de custo
EC2_PRICE_SA_EAST_1 = {
    "t3.nano": 0.0066, "t3.micro": 0.0132, "t3.small": 0.0264, "t3.medium": 0.0528,
    "t3.large": 0.1056, "t3.xlarge": 0.2112, "t3.2xlarge": 0.4224,
    "t3a.nano": 0.0059, "t3a.micro": 0.0119, "t3a.small": 0.0238, "t3a.medium": 0.0475,
    "t3a.large": 0.0950, "t3a.xlarge": 0.1901, "t3a.2xlarge": 0.3802,
    "t2.nano": 0.0079, "t2.micro": 0.0158, "t2.small": 0.0316, "t2.medium": 0.0632,
    "t2.large": 0.1264, "t2.xlarge": 0.2528, "t2.2xlarge": 0.5056,
    "m5.large": 0.1200, "m5.xlarge": 0.2400, "m5.2xlarge": 0.4800,
    "m5.4xlarge": 0.9600, "m5.8xlarge": 1.9200, "m5.12xlarge": 2.8800,
    "m5a.large": 0.1080, "m5a.xlarge": 0.2160, "m5a.2xlarge": 0.4320,
    "m6i.large": 0.1200, "m6i.xlarge": 0.2400, "m6i.2xlarge": 0.4800,
    "c5.large": 0.1060, "c5.xlarge": 0.2120, "c5.2xlarge": 0.4240,
    "c5.4xlarge": 0.8480, "c5.9xlarge": 1.9080,
    "r5.large": 0.1580, "r5.xlarge": 0.3160, "r5.2xlarge": 0.6320,
    "r5.4xlarge": 1.2640, "r5.8xlarge": 2.5280,
    "r5a.large": 0.1422, "r5a.xlarge": 0.2844, "r5a.2xlarge": 0.5688,
}

def estimate_ec2_monthly_cost(instance_type):
    """Estima custo mensal On-Demand para uma instância EC2 em sa-east-1."""
    hourly = EC2_PRICE_SA_EAST_1.get(instance_type)
    if hourly is None:
        # Fallback heurístico baseado em família e size
        try:
            family, size = instance_type.split(".")
            base_per_vcpu = 0.06  # estimativa conservadora sa-east-1 ~$0.06/vCPU/h
            size_map = {"nano": 0.25, "micro": 0.5, "small": 1, "medium": 2, "large": 2,
                        "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "8xlarge": 32, "12xlarge": 48,
                        "16xlarge": 64, "24xlarge": 96}
            vcpu = size_map.get(size, 2)
            hourly = base_per_vcpu * vcpu
        except Exception:
            hourly = 0.10  # default seguro
    return round(hourly * 730, 2)  # 730 horas/mês


# ============================================================================
# CLASSE PRINCIPAL DO SIMULADOR FINOPS
# ============================================================================
class FinOpsSimulator:
    def __init__(self, region=REGION, profile=None, tag_keys=None, debug_sp=False):
        self.region = region
        self.required_tags = tag_keys if tag_keys else REQUIRED_TAGS
        self.debug_sp = debug_sp
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

        # Estrutura para armazenar todos os achados
        self.findings = {
            "account_id": self._get_account_id(),
            "region": region,
            "generated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "required_tags": list(self.required_tags),
            "cost_summary": {},
            "reserved_utilization": [],
            "savings_plans_utilization": [],
            "savings_plans_history": {"months": [], "total_savings": 0},
            "ri_inventory": {},
            "ri_details": [],
            "sp_details": [],
            "purchase_recommendations": {"savings_plans": [], "reserved_instances": [], "total_sp_savings": 0, "total_ri_savings": 0},
            "idle_resources": [],
            "untagged_resources": [],
            "recommendations": [],
            "total_potential_savings": 0.0,
        }

    def _get_account_id(self):
        try:
            return self.session.client("sts").get_caller_identity()["Account"]
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # 1) RESUMO DE CUSTOS (Cost Explorer)
    # ------------------------------------------------------------------
    def fetch_cost_summary(self):
        """Compara os DOIS últimos meses COMPLETOS (ignora mês corrente parcial)."""
        print("[1/7] Buscando resumo de custos (comparativo de meses completos)...")
        today = datetime.now().date()
        first_of_current_month = today.replace(day=1)

        # "Mês mais recente fechado" = mês passado completo
        #   ex: hoje=07/abr → recent = 01/mar a 01/abr
        recent_end = first_of_current_month
        recent_start = (first_of_current_month - timedelta(days=1)).replace(day=1)

        # "Mês anterior fechado" = dois meses atrás completo
        #   ex: hoje=07/abr → previous = 01/fev a 01/mar
        previous_end = recent_start
        previous_start = (recent_start - timedelta(days=1)).replace(day=1)

        # Mês corrente parcial (informativo apenas)
        partial_start = first_of_current_month
        partial_end = today
        partial_days = (today - first_of_current_month).days + 1

        # Últimos 12 meses para média (até início do mês corrente)
        annual_start = (first_of_current_month - timedelta(days=365)).replace(day=1)

        region_filter = {"Dimensions": {"Key": "REGION", "Values": [self.region]}}

        def _fetch_by_service(start, end):
            try:
                resp = self.ce.get_cost_and_usage(
                    TimePeriod={"Start": str(start), "End": str(end)},
                    Granularity="MONTHLY",
                    Metrics=["UnblendedCost"],
                    GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
                    Filter=region_filter,
                )
                agg = {}
                for result in resp.get("ResultsByTime", []):
                    for grp in result.get("Groups", []):
                        name = grp["Keys"][0]
                        amount = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                        agg[name] = agg.get(name, 0) + amount
                return agg
            except ClientError as e:
                print(f"      ⚠ Cost Explorer erro: {e.response['Error']['Code']}")
                return {}

        # Mês mais recente FECHADO (este vai ser tratado como "current" no resto do código)
        current = _fetch_by_service(recent_start, recent_end)
        # Mês anterior FECHADO
        previous = _fetch_by_service(previous_start, previous_end)
        # Mês corrente parcial (informativo)
        partial = _fetch_by_service(partial_start, partial_end) if partial_days > 0 else {}
        # Média anual
        annual_total = _fetch_by_service(annual_start, first_of_current_month)
        annual_avg = {k: v / 12.0 for k, v in annual_total.items()}

        # Histórico 6 meses para gráfico de tendência
        trend_history = self._fetch_monthly_trend(first_of_current_month)

        all_services = set(current.keys()) | set(previous.keys()) | set(annual_avg.keys())
        services_detail = []
        for name in all_services:
            c = current.get(name, 0)
            p = previous.get(name, 0)
            a = annual_avg.get(name, 0)
            diff = c - p
            pct = ((c - p) / p * 100) if p > 0 else (100 if c > 0 else 0)
            vs_avg = ((c - a) / a * 100) if a > 0 else 0
            services_detail.append({
                "name": name,
                "current": round(c, 2),
                "previous": round(p, 2),
                "annual_avg": round(a, 2),
                "diff": round(diff, 2),
                "pct_change": round(pct, 2),
                "vs_annual_pct": round(vs_avg, 1),
            })
        services_detail.sort(key=lambda x: x["current"], reverse=True)

        total_current = sum(s["current"] for s in services_detail)
        total_previous = sum(s["previous"] for s in services_detail)
        total_partial = sum(partial.values())
        total_variation = total_current - total_previous
        total_variation_pct = ((total_current - total_previous) / total_previous * 100) if total_previous > 0 else 0

        # Forecast do mês corrente (extrapolação linear baseada nos dias decorridos)
        days_in_current_month = ((first_of_current_month.replace(day=28) + timedelta(days=4)).replace(day=1) - first_of_current_month).days
        if partial_days > 0 and total_partial > 0:
            forecast_current_month = total_partial * (days_in_current_month / partial_days)
        else:
            forecast_current_month = 0

        # Breakdown por tag - usa mês mais recente FECHADO
        tag_breakdown = self._fetch_tag_breakdown(recent_start, recent_end)

        # Datas inclusivas para EXIBIÇÃO (a API usa end-exclusive, mas mostrar
        # "01/03 a 01/04" confunde — humanos esperam "01/03 a 31/03")
        recent_end_inclusive = recent_end - timedelta(days=1)
        previous_end_inclusive = previous_end - timedelta(days=1)

        self.findings["cost_summary"] = {
            "current_period": f"{recent_start} a {recent_end_inclusive}",
            "previous_period": f"{previous_start} a {previous_end_inclusive}",
            "partial_period": f"{partial_start} a {partial_end} ({partial_days} dias)",
            "total": round(total_current, 2),
            "total_current": round(total_current, 2),
            "total_previous": round(total_previous, 2),
            "total_partial": round(total_partial, 2),
            "forecast_current_month": round(forecast_current_month, 2),
            "total_variation": round(total_variation, 2),
            "total_variation_pct": round(total_variation_pct, 2),
            "services": [{"name": s["name"], "cost": s["current"]} for s in services_detail if s["current"] > 0],
            "services_detail": services_detail,
            "tag_breakdown": tag_breakdown,
            "trend_history": trend_history,
        }
        print(f"      → {recent_start.strftime('%b/%Y')} (fechado): ${total_current:,.2f}")
        print(f"      → {previous_start.strftime('%b/%Y')} (fechado): ${total_previous:,.2f}")
        print(f"      → Variação: {total_variation_pct:+.1f}%")
        print(f"      → Mês corrente parcial ({partial_days}d): ${total_partial:,.2f} | Forecast: ${forecast_current_month:,.2f}")

    def _fetch_monthly_trend(self, end_date, months=6):
        """Busca custo total por mês dos últimos N meses para gráfico de tendência."""
        try:
            start = end_date
            for _ in range(months):
                start = (start - timedelta(days=1)).replace(day=1)
            resp = self.ce.get_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end_date)},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "REGION", "Values": [self.region]}},
            )
            history = []
            for result in resp.get("ResultsByTime", []):
                period_start = result["TimePeriod"]["Start"]
                amount = float(result["Total"]["UnblendedCost"]["Amount"])
                history.append({"month": period_start[:7], "cost": round(amount, 2)})
            return history
        except ClientError as e:
            print(f"      ⚠ Trend history: {e.response['Error']['Code']}")
            return []

    def _fetch_tag_breakdown(self, start, end):
        """Agrupa custos por tag 'Produto' (ou Project) para mostrar COM tags vs SEM tags"""
        region_filter = {"Dimensions": {"Key": "REGION", "Values": [self.region]}}
        for tag_key in ["Produto", "Project", "Product", "Environment"]:
            try:
                resp = self.ce.get_cost_and_usage(
                    TimePeriod={"Start": str(start), "End": str(end)},
                    Granularity="MONTHLY",
                    Metrics=["UnblendedCost"],
                    GroupBy=[{"Type": "TAG", "Key": tag_key}],
                    Filter=region_filter,
                )
                products = {}
                untagged = 0.0
                for result in resp.get("ResultsByTime", []):
                    for grp in result.get("Groups", []):
                        key = grp["Keys"][0]  # formato: "Produto$valor" ou "Produto$"
                        amount = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                        # Valor após o $
                        tag_value = key.split("$", 1)[1] if "$" in key else ""
                        if not tag_value:
                            untagged += amount
                        else:
                            products[tag_value] = products.get(tag_value, 0) + amount
                if products or untagged > 0:
                    total = sum(products.values()) + untagged
                    product_list = [
                        {"name": k, "cost": round(v, 2), "pct": round(v / total * 100, 1) if total > 0 else 0}
                        for k, v in sorted(products.items(), key=lambda x: -x[1])
                    ]
                    return {
                        "tag_key": tag_key,
                        "tagged_total": round(sum(products.values()), 2),
                        "untagged_total": round(untagged, 2),
                        "products": product_list,
                        "tag_rate_pct": round(sum(products.values()) / total * 100, 1) if total > 0 else 0,
                    }
            except ClientError:
                continue
        return {"tag_key": None, "tagged_total": 0, "untagged_total": 0, "products": [], "tag_rate_pct": 0}

    # ------------------------------------------------------------------
    # 2) UTILIZAÇÃO DE RESERVED INSTANCES (EC2, RDS, ES, EC, Redshift)
    # ------------------------------------------------------------------
    def fetch_ri_utilization(self):
        print("[2/7] Verificando utilização de Reserved Instances...")
        end = datetime.now().date()
        start = end - timedelta(days=LOOKBACK_DAYS)
        services_map = {
            "Amazon Elastic Compute Cloud - Compute": "EC2",
            "Amazon Relational Database Service": "RDS",
            "Amazon ElastiCache": "ElastiCache",
            "Amazon OpenSearch Service": "OpenSearch",
            "Amazon Redshift": "Redshift",
        }
        for service_name, short in services_map.items():
            try:
                resp = self.ce.get_reservation_utilization(
                    TimePeriod={"Start": str(start), "End": str(end)},
                    Granularity="MONTHLY",
                    Filter={"Dimensions": {"Key": "SERVICE", "Values": [service_name]}},
                )
                total_data = resp.get("Total", {})
                if not total_data or float(total_data.get("PurchasedHours", 0)) == 0:
                    continue
                utilization = float(total_data.get("UtilizationPercentage", 0))
                purchased = float(total_data.get("PurchasedHours", 0))
                used = float(total_data.get("TotalActualHours", 0))
                unused_cost = float(total_data.get("UnusedHours", 0)) * \
                              (float(total_data.get("AmortizedRecurringFee", 0)) /
                               max(purchased, 1))
                status = "✅ OK" if utilization >= UTILIZATION_THRESHOLD else "⚠️ SUBUTILIZADO"
                self.findings["reserved_utilization"].append({
                    "service": short,
                    "utilization_pct": round(utilization, 2),
                    "purchased_hours": round(purchased, 1),
                    "used_hours": round(used, 1),
                    "unused_hours": round(purchased - used, 1),
                    "estimated_waste": round(unused_cost, 2),
                    "status": status,
                })
                if utilization < UTILIZATION_THRESHOLD:
                    self.findings["recommendations"].append({
                        "type": "RI Subutilizada",
                        "service": short,
                        "description": f"{short}: RIs com {utilization:.1f}% de uso. "
                                       f"Considere modificar/vender no Marketplace.",
                        "potential_savings": round(unused_cost, 2),
                    })
                    self.findings["total_potential_savings"] += unused_cost
                print(f"      → {short}: {utilization:.1f}% utilização {status}")
            except ClientError as e:
                err = e.response["Error"]["Code"]
                if err not in ("DataUnavailableException", "ValidationException"):
                    print(f"      ⚠ {short}: {err}")

    # ------------------------------------------------------------------
    # 3) UTILIZAÇÃO DE SAVINGS PLANS
    # ------------------------------------------------------------------
    def fetch_savings_plans_utilization(self):
        print("[3/7] Verificando utilização de Savings Plans...")
        today = datetime.now().date()
        first_of_current = today.replace(day=1)
        # Último mês fechado: 1º do mês anterior até 1º do mês atual
        # (MONTHLY granularity exige limites de mês)
        last_month_end = first_of_current
        last_month_start = (first_of_current - timedelta(days=1)).replace(day=1)
        # Mês corrente parcial (para fallback)
        current_partial_start = first_of_current
        current_partial_end = today

        def _try_fetch(start, end, granularity, label):
            try:
                resp = self.ce.get_savings_plans_utilization(
                    TimePeriod={"Start": str(start), "End": str(end)},
                    Granularity=granularity,
                )
                total = resp.get("Total", {})
                util = total.get("Utilization", {}) if total else {}
                if util and float(util.get("TotalCommitment", 0)) > 0:
                    return total, label
            except ClientError as e:
                code = e.response['Error']['Code']
                if code != "DataUnavailableException":
                    print(f"      ⚠ SP utilization ({label}): {code}")
            return None, None

        # Tentativa 1: último mês fechado, MONTHLY (caminho ideal)
        total, source = _try_fetch(last_month_start, last_month_end, "MONTHLY", f"{last_month_start.strftime('%b/%Y')} fechado")
        # Tentativa 2: mês corrente parcial, DAILY
        if total is None and current_partial_end > current_partial_start:
            total, source = _try_fetch(current_partial_start, current_partial_end, "DAILY", f"{current_partial_start.strftime('%b/%Y')} parcial")
        # Tentativa 3: últimos 30 dias, DAILY (último recurso)
        if total is None:
            total, source = _try_fetch(today - timedelta(days=30), today, "DAILY", "últimos 30 dias")

        if total is None:
            print("      → Nenhum Savings Plan ativo nos períodos consultados (ou sem dados disponíveis)")
            self._fetch_savings_plans_history()
            return

        util = total.get("Utilization", {})
        utilization_pct = float(util.get("UtilizationPercentage", 0))
        used = float(util.get("UsedCommitment", 0))
        unused = float(util.get("UnusedCommitment", 0))
        total_commit = float(util.get("TotalCommitment", 0))
        savings = float(total.get("Savings", {}).get("NetSavings", 0))
        status = "✅ OK" if utilization_pct >= UTILIZATION_THRESHOLD else "⚠️ SUBUTILIZADO"
        self.findings["savings_plans_utilization"].append({
            "utilization_pct": round(utilization_pct, 2),
            "total_commitment": round(total_commit, 2),
            "used_commitment": round(used, 2),
            "unused_commitment": round(unused, 2),
            "net_savings": round(savings, 2),
            "status": status,
            "source_period": source,
        })
        if utilization_pct < UTILIZATION_THRESHOLD:
            self.findings["recommendations"].append({
                "type": "Savings Plan Subutilizado",
                "service": "Compute",
                "description": f"Savings Plan com {utilization_pct:.1f}% de uso ({source}). "
                               f"${unused:,.2f} comprometido sem uso.",
                "potential_savings": round(unused, 2),
            })
            self.findings["total_potential_savings"] += unused
        print(f"      → Savings Plans: {utilization_pct:.1f}% {status} ({source}, economia líquida: ${savings:,.2f})")
        self._fetch_savings_plans_history()

    def _fetch_savings_plans_history(self):
        """Busca histórico mês a mês de economia REAL com Savings Plans (últimos 6 meses).

        Usa o padrão correto AWS:
        - Filter: RECORD_TYPE = SavingsPlanCoveredUsage (só linhas cobertas por SP)
        - UnblendedCost = valor que seria pago on-demand (custo "fantasma")
        - AmortizedCost = custo real do SP alocado a essa usage (já com upfront amortizado)
        - Economia = Unblended - Amortized

        Importante: NÃO inclui Reserved Instances. RIs têm RECORD_TYPE = DiscountedUsage,
        que é filtrado fora. A coluna 'Custo com SP' representa o valor amortizado real
        do SP (incluindo a parcela mensal do upfront em planos All/Partial Upfront)."""
        today = datetime.now().date()
        current_start = today.replace(day=1)
        # 6 meses atrás
        year = current_start.year
        month = current_start.month - 6
        while month <= 0:
            month += 12
            year -= 1
        history_start = current_start.replace(year=year, month=month)

        # MODO DEBUG: imprime breakdown completo por RECORD_TYPE pra comparar com console
        if self.debug_sp:
            print("\n" + "=" * 70)
            print(" 🔍 DEBUG MODE - Savings Plans History")
            print(" Janela: {} a {}".format(history_start, today))
            print(" Filtro: RECORD_TYPE = SavingsPlanCoveredUsage")
            print(" Métricas: UnblendedCost + AmortizedCost")
            print("=" * 70)
            try:
                # Query 1: Breakdown completo por RECORD_TYPE (sem filtro) pra ver TUDO
                resp_all = self.ce.get_cost_and_usage(
                    TimePeriod={"Start": str(history_start), "End": str(today)},
                    Granularity="MONTHLY",
                    Metrics=["UnblendedCost", "AmortizedCost"],
                    GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
                )
                print("\n📋 BREAKDOWN POR RECORD_TYPE (sem filtro):")
                print("-" * 70)
                for result in resp_all.get("ResultsByTime", []):
                    period = result["TimePeriod"]["Start"][:7]
                    print(f"\n  {period}:")
                    for grp in result.get("Groups", []):
                        rt = grp["Keys"][0]
                        unb = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                        amo = float(grp["Metrics"]["AmortizedCost"]["Amount"])
                        # Marca records relacionados a SP/RI
                        marker = ""
                        if "SavingsPlan" in rt:
                            marker = "  ← SP"
                        elif rt in ("DiscountedUsage", "RIFee"):
                            marker = "  ← RI"
                        print(f"    {rt:35s}  Unblended: ${unb:>14,.2f}  Amortized: ${amo:>14,.2f}{marker}")
            except ClientError as e:
                print(f"  ⚠ Erro no debug: {e.response['Error']['Code']}")
            print("\n" + "=" * 70 + "\n")

        try:
            # Query única com GroupBy por RECORD_TYPE - assim conseguimos ver
            # CoveredUsage (Unblended e Amortized) E RecurringFee (Unblended)
            # numa só chamada, e classificar mês a mês.
            resp = self.ce.get_cost_and_usage(
                TimePeriod={"Start": str(history_start), "End": str(today)},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "AmortizedCost"],
                Filter={
                    "Dimensions": {
                        "Key": "RECORD_TYPE",
                        # Inclui tanto CoveredUsage (showback) quanto RecurringFee (cobrança real local)
                        "Values": ["SavingsPlanCoveredUsage", "SavingsPlanRecurringFee", "SavingsPlanUpfrontFee"],
                    }
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
            )
            monthly = []
            total_on_demand = 0
            total_sp_cost = 0
            for result in resp.get("ResultsByTime", []):
                period_start = result["TimePeriod"]["Start"]
                period_label = datetime.strptime(period_start, "%Y-%m-%d").strftime("%b/%Y")
                # Lê cada record type
                covered_unblended = 0  # On-demand equivalente (showback)
                covered_amortized = 0  # SP cost amortizado alocado a essa usage
                recurring_fee_local = 0  # Fee de SP que ESTA conta efetivamente pagou
                upfront_fee_local = 0  # Upfront pago por esta conta (geralmente só no mês da compra)
                for grp in result.get("Groups", []):
                    rt = grp["Keys"][0]
                    unb = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                    amo = float(grp["Metrics"]["AmortizedCost"]["Amount"])
                    if rt == "SavingsPlanCoveredUsage":
                        covered_unblended = unb
                        covered_amortized = amo
                    elif rt == "SavingsPlanRecurringFee":
                        recurring_fee_local = unb
                    elif rt == "SavingsPlanUpfrontFee":
                        upfront_fee_local = unb
                # Classificação local vs externo:
                # - Local: esta conta pagou alguma fee de SP esse mês (recurring ou upfront)
                # - Externo: tem cobertura mas zero pagamento local → SP em outra conta (payer)
                local_sp_paid = recurring_fee_local + upfront_fee_local
                is_local = local_sp_paid > 0
                # Economia SEMPRE calculada como Unblended - Amortized
                # (representa a economia REAL do consumo, não importa quem pagou o SP)
                savings = covered_unblended - covered_amortized
                savings_pct = (savings / covered_unblended * 100) if covered_unblended > 0 else 0
                if covered_unblended > 0 or covered_amortized > 0:
                    monthly.append({
                        "period": period_label,
                        "on_demand_cost": round(covered_unblended, 2),
                        "sp_cost": round(covered_amortized, 2),
                        "savings": round(savings, 2),
                        "savings_pct": round(savings_pct, 1),
                        "local_sp_paid": round(local_sp_paid, 2),
                        "is_local": is_local,
                    })
                    total_on_demand += covered_unblended
                    total_sp_cost += covered_amortized
            total_savings = total_on_demand - total_sp_cost
            self.findings["savings_plans_history"] = {
                "months": monthly,
                "total_on_demand": round(total_on_demand, 2),
                "total_sp_cost": round(total_sp_cost, 2),
                "total_savings": round(total_savings, 2),
                "total_savings_pct": round(total_savings / total_on_demand * 100, 1) if total_on_demand > 0 else 0,
            }
            if monthly:
                avg_pct = (total_savings / total_on_demand * 100) if total_on_demand > 0 else 0
                local_n = sum(1 for m in monthly if m["is_local"])
                ext_n = len(monthly) - local_n
                print(f"      → Histórico SP: {len(monthly)} meses ({local_n} local + {ext_n} externo), economia ${total_savings:,.2f} ({avg_pct:.1f}%)")
        except ClientError as e:
            print(f"      ⚠ Histórico SP indisponível: {e.response['Error']['Code']}")
            self.findings["savings_plans_history"] = {"months": [], "total_savings": 0}

    # ------------------------------------------------------------------
    # 4) RESERVED INSTANCES POR SERVIÇO (inventário detalhado)
    # ------------------------------------------------------------------
    def inventory_reserved_resources(self):
        print("[4/7] Inventariando reservas ativas por serviço...")
        ri_details = []  # detalhes com data de expiração

        def _days_until(end_dt):
            if end_dt is None:
                return None
            try:
                delta = end_dt.date() - datetime.now(timezone.utc).date()
                return delta.days
            except Exception:
                return None

        def _expiry_status(days):
            if days is None:
                return "?"
            if days < 0:
                return "EXPIRADA"
            if days < 30:
                return "EXPIRA EM BREVE"
            if days < 90:
                return "ATENÇÃO"
            return "OK"

        # EC2 RIs
        try:
            ec2_ris = self.ec2.describe_reserved_instances(
                Filters=[{"Name": "state", "Values": ["active"]}]
            ).get("ReservedInstances", [])
            for r in ec2_ris:
                end_dt = r.get("End")
                days = _days_until(end_dt)
                ri_details.append({
                    "service": "EC2",
                    "id": r.get("ReservedInstancesId", "?"),
                    "instance_type": r.get("InstanceType", "?"),
                    "count": r.get("InstanceCount", 0),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                    "offering": r.get("OfferingType", ""),
                })
            print(f"      → EC2: {len(ec2_ris)} RIs ativas")
        except ClientError:
            ec2_ris = []

        # RDS RIs - StartTime + Duration(seg) = End
        try:
            rds_ris = self.rds.describe_reserved_db_instances().get("ReservedDBInstances", [])
            rds_ris = [r for r in rds_ris if r.get("State") == "active"]
            for r in rds_ris:
                start_dt = r.get("StartTime")
                duration = r.get("Duration", 0)
                end_dt = start_dt + timedelta(seconds=duration) if start_dt else None
                days = _days_until(end_dt)
                ri_details.append({
                    "service": "RDS",
                    "id": r.get("ReservedDBInstanceId", "?"),
                    "instance_type": r.get("DBInstanceClass", "?"),
                    "count": r.get("DBInstanceCount", 0),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                    "offering": r.get("OfferingType", ""),
                })
            print(f"      → RDS: {len(rds_ris)} RIs ativas")
        except ClientError:
            rds_ris = []

        # OpenSearch RIs
        try:
            os_ris = self.es.describe_reserved_elasticsearch_instances().get(
                "ReservedElasticsearchInstances", []
            )
            os_ris = [r for r in os_ris if r.get("State") == "active"]
            for r in os_ris:
                start_dt = r.get("StartTime")
                duration = r.get("Duration", 0)
                end_dt = start_dt + timedelta(seconds=duration) if start_dt else None
                days = _days_until(end_dt)
                ri_details.append({
                    "service": "OpenSearch",
                    "id": r.get("ReservedElasticsearchInstanceId", "?"),
                    "instance_type": r.get("ElasticsearchInstanceType", "?"),
                    "count": r.get("ElasticsearchInstanceCount", 0),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                    "offering": r.get("PaymentOption", ""),
                })
            print(f"      → OpenSearch: {len(os_ris)} RIs ativas")
            domains = self.es.list_domain_names().get("DomainNames", [])
            print(f"      → OpenSearch: {len(domains)} domínios em execução")
            if os_ris and not domains:
                self.findings["recommendations"].append({
                    "type": "RI Órfã - OpenSearch",
                    "service": "OpenSearch",
                    "description": f"{len(os_ris)} RI(s) de OpenSearch ativas mas NENHUM domínio em execução. Desperdício total!",
                    "potential_savings": sum(float(r.get("FixedPrice", 0)) for r in os_ris),
                })
        except ClientError as e:
            os_ris = []
            print(f"      ⚠ OpenSearch: {e.response['Error']['Code']}")

        # ElastiCache RIs
        try:
            ec_ris = self.elasticache.describe_reserved_cache_nodes().get("ReservedCacheNodes", [])
            ec_ris = [r for r in ec_ris if r.get("State") == "active"]
            for r in ec_ris:
                start_dt = r.get("StartTime")
                duration = r.get("Duration", 0)
                end_dt = start_dt + timedelta(seconds=duration) if start_dt else None
                days = _days_until(end_dt)
                ri_details.append({
                    "service": "ElastiCache",
                    "id": r.get("ReservedCacheNodeId", "?"),
                    "instance_type": r.get("CacheNodeType", "?"),
                    "count": r.get("CacheNodeCount", 0),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                    "offering": r.get("OfferingType", ""),
                })
            print(f"      → ElastiCache: {len(ec_ris)} RIs ativas")
        except ClientError:
            ec_ris = []

        # Redshift RIs
        try:
            rs_ris = self.redshift.describe_reserved_nodes().get("ReservedNodes", [])
            rs_ris = [r for r in rs_ris if r.get("State") == "active"]
            for r in rs_ris:
                start_dt = r.get("StartTime")
                duration = r.get("Duration", 0)
                end_dt = start_dt + timedelta(seconds=duration) if start_dt else None
                days = _days_until(end_dt)
                ri_details.append({
                    "service": "Redshift",
                    "id": r.get("ReservedNodeId", "?"),
                    "instance_type": r.get("NodeType", "?"),
                    "count": r.get("NodeCount", 0),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                    "offering": r.get("OfferingType", ""),
                })
            print(f"      → Redshift: {len(rs_ris)} nodes reservados ativos")
        except ClientError:
            rs_ris = []

        self.findings["ri_inventory"] = {
            "EC2": len(ec2_ris),
            "RDS": len(rds_ris),
            "OpenSearch": len(os_ris),
            "ElastiCache": len(ec_ris),
            "Redshift": len(rs_ris),
        }
        self.findings["ri_details"] = ri_details

        # Alerta proativo: RIs expirando em < 30 dias
        expiring_soon = [r for r in ri_details if r["days_remaining"] is not None and 0 <= r["days_remaining"] < 30]
        if expiring_soon:
            self.findings["recommendations"].append({
                "type": "RI Expirando em Breve",
                "service": "Múltiplos",
                "description": f"{len(expiring_soon)} Reserved Instance(s) expira(m) nos próximos 30 dias. Avalie renovação ou substituição por Savings Plans.",
                "potential_savings": 0,
            })

        # Também busca Savings Plans com data de expiração
        try:
            sp_resp = self.savingsplans.describe_savings_plans(states=["active"]).get("savingsPlans", [])
            sp_details = []
            for sp in sp_resp:
                end_str = sp.get("end")  # ISO string
                end_dt = None
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                days = _days_until(end_dt)
                sp_details.append({
                    "id": sp.get("savingsPlanId", "?"),
                    "type": sp.get("savingsPlanType", "?"),  # Compute/EC2Instance/SageMaker
                    "payment": sp.get("paymentOption", "?"),
                    "term_years": sp.get("termDurationInSeconds", 0) // (365*24*3600),
                    "commitment_hourly": sp.get("commitment", "?"),
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                })
            self.findings["sp_details"] = sp_details
            print(f"      → Savings Plans ativos: {len(sp_details)}")
            sp_expiring = [s for s in sp_details if s["days_remaining"] is not None and 0 <= s["days_remaining"] < 30]
            if sp_expiring:
                self.findings["recommendations"].append({
                    "type": "Savings Plan Expirando em Breve",
                    "service": "Compute/EC2/SageMaker",
                    "description": f"{len(sp_expiring)} Savings Plan(s) expira(m) nos próximos 30 dias. Avalie renovação baseada no uso histórico.",
                    "potential_savings": 0,
                })
        except ClientError as e:
            print(f"      ⚠ Savings Plans details: {e.response['Error']['Code']}")
            self.findings["sp_details"] = []

    # ------------------------------------------------------------------
    # 5) RECURSOS OCIOSOS (oportunidades clássicas de FinOps)
    # ------------------------------------------------------------------
    def find_idle_resources(self):
        print("[5/7] Procurando recursos ociosos...")

        # Volumes EBS não anexados
        try:
            volumes = self.ec2.describe_volumes(
                Filters=[{"Name": "status", "Values": ["available"]}]
            ).get("Volumes", [])
            for v in volumes:
                size_gb = v["Size"]
                # gp3 sa-east-1 ~$0.10/GB-mês
                est_cost = size_gb * 0.10
                self.findings["idle_resources"].append({
                    "type": "EBS Volume não anexado",
                    "id": v["VolumeId"],
                    "details": f"{size_gb} GB ({v['VolumeType']})",
                    "monthly_cost": round(est_cost, 2),
                })
                self.findings["total_potential_savings"] += est_cost
            print(f"      → {len(volumes)} volumes EBS órfãos")
        except ClientError as e:
            print(f"      ⚠ EBS: {e.response['Error']['Code']}")

        # Elastic IPs não associados
        try:
            addrs = self.ec2.describe_addresses().get("Addresses", [])
            unused_eips = [a for a in addrs if "AssociationId" not in a]
            for a in unused_eips:
                # EIP não associado: ~$3.60/mês em sa-east-1
                self.findings["idle_resources"].append({
                    "type": "Elastic IP não associado",
                    "id": a.get("PublicIp", "unknown"),
                    "details": "Cobrança por hora ociosa",
                    "monthly_cost": 3.60,
                })
                self.findings["total_potential_savings"] += 3.60
            print(f"      → {len(unused_eips)} EIPs órfãos")
        except ClientError as e:
            print(f"      ⚠ EIP: {e.response['Error']['Code']}")

        # Snapshots antigos (>90 dias)
        try:
            snaps = self.ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", [])
            old_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            old_snaps = [s for s in snaps if s["StartTime"] < old_cutoff]
            if old_snaps:
                # Snapshot ~$0.05/GB-mês em sa-east-1
                total_size = sum(s["VolumeSize"] for s in old_snaps)
                est_cost = total_size * 0.05
                self.findings["idle_resources"].append({
                    "type": "Snapshots EBS antigos (>90 dias)",
                    "id": f"{len(old_snaps)} snapshots",
                    "details": f"{total_size} GB total",
                    "monthly_cost": round(est_cost, 2),
                })
                self.findings["total_potential_savings"] += est_cost
            print(f"      → {len(old_snaps)} snapshots antigos")
        except ClientError as e:
            print(f"      ⚠ Snapshots: {e.response['Error']['Code']}")

        # Instâncias EC2 com baixo uso de CPU (avg E max baixos = realmente ocioso)
        try:
            instances = []
            paginator = self.ec2.get_paginator("describe_instances")
            for page in paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            ):
                for res in page["Reservations"]:
                    instances.extend(res["Instances"])

            low_cpu_count = 0
            for inst in instances[:100]:  # limita para não estourar
                iid = inst["InstanceId"]
                cpu_stats = self._get_cpu_stats(iid)
                if cpu_stats is None:
                    continue
                avg_cpu, max_cpu = cpu_stats
                # Só considera ocioso se MÉDIA E PICO estiverem baixos
                # (instâncias com picos ocasionais NÃO são ociosas)
                if avg_cpu < CPU_IDLE_THRESHOLD and max_cpu < CPU_IDLE_MAX_THRESHOLD:
                    low_cpu_count += 1
                    instance_type = inst.get("InstanceType", "unknown")
                    est_cost = estimate_ec2_monthly_cost(instance_type)
                    name = next(
                        (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                        "(sem nome)",
                    )
                    self.findings["idle_resources"].append({
                        "type": "EC2 com CPU baixa (média e pico)",
                        "id": iid,
                        "details": f"{instance_type} | {name} | CPU avg {avg_cpu:.1f}% / max {max_cpu:.1f}% (30 dias)",
                        "monthly_cost": est_cost,
                    })
                    self.findings["total_potential_savings"] += est_cost
            print(f"      → {low_cpu_count} instâncias EC2 ociosas (avg<{CPU_IDLE_THRESHOLD}% E max<{CPU_IDLE_MAX_THRESHOLD}%)")
        except ClientError as e:
            print(f"      ⚠ EC2: {e.response['Error']['Code']}")

        # Load Balancers (ALB/NLB) órfãos - sem target groups saudáveis
        try:
            ELB_BASE_COST = 18.40  # ~$0.0252/h * 730h sa-east-1
            orphan_elbs = 0
            paginator = self.elbv2.get_paginator("describe_load_balancers")
            for page in paginator.paginate():
                for lb in page.get("LoadBalancers", []):
                    lb_arn = lb["LoadBalancerArn"]
                    lb_name = lb["LoadBalancerName"]
                    lb_type = lb["Type"]  # application, network, gateway
                    # Verifica target groups associados
                    try:
                        tgs = self.elbv2.describe_target_groups(LoadBalancerArn=lb_arn).get("TargetGroups", [])
                    except ClientError:
                        tgs = []
                    healthy_total = 0
                    for tg in tgs:
                        try:
                            health = self.elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                            for t in health.get("TargetHealthDescriptions", []):
                                if t.get("TargetHealth", {}).get("State") == "healthy":
                                    healthy_total += 1
                        except ClientError:
                            pass
                    if healthy_total == 0:
                        orphan_elbs += 1
                        self.findings["idle_resources"].append({
                            "type": f"Load Balancer órfão ({lb_type})",
                            "id": lb_name,
                            "details": f"{len(tgs)} target group(s), 0 targets healthy",
                            "monthly_cost": ELB_BASE_COST,
                        })
                        self.findings["total_potential_savings"] += ELB_BASE_COST
            print(f"      → {orphan_elbs} Load Balancers sem targets healthy")
        except ClientError as e:
            print(f"      ⚠ ELB: {e.response['Error']['Code']}")

    def _get_cpu_stats(self, instance_id):
        """Retorna (avg, max) de CPU dos últimos 30 dias, ou None se sem dados."""
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=30)  # janela de 30 dias = mês inteiro
            resp = self.cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start,
                EndTime=end,
                Period=3600,  # granularidade de 1 hora (capta picos diários)
                Statistics=["Average", "Maximum"],
            )
            points = resp.get("Datapoints", [])
            if not points:
                return None
            avg = sum(p["Average"] for p in points) / len(points)
            mx = max(p["Maximum"] for p in points)
            return (avg, mx)
        except ClientError:
            return None

    # ------------------------------------------------------------------
    # 6) RECURSOS SEM TAGS (governança/alocação de custo)
    # ------------------------------------------------------------------
    def find_untagged_resources(self):
        print("[6/7] Verificando recursos sem tags...")
        required = set(self.required_tags)
        print(f"      → Tags procuradas (precisa ter pelo menos UMA): {', '.join(sorted(required))}")
        try:
            untagged = 0
            paginator = self.ec2.get_paginator("describe_instances")
            for page in paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            ):
                for res in page["Reservations"]:
                    for inst in res["Instances"]:
                        tags = inst.get("Tags", [])
                        tag_keys = {t["Key"] for t in tags}
                        # Só considera "sem tag" se NÃO TEM NENHUMA das tags obrigatórias
                        # (resource sem qualquer uma das tags procuradas)
                        if not (tag_keys & required):
                            untagged += 1
                            self.findings["untagged_resources"].append({
                                "type": "EC2",
                                "id": inst["InstanceId"],
                                "name": next(
                                    (t["Value"] for t in tags if t["Key"] == "Name"),
                                    "(sem nome)",
                                ),
                                "missing_tags": sorted(required),  # todas, já que não tem nenhuma
                            })
            print(f"      → {untagged} EC2s sem nenhuma das tags obrigatórias")
            if untagged > 0:
                self.findings["recommendations"].append({
                    "type": "Governança - Tags",
                    "service": "EC2",
                    "description": f"{untagged} instâncias EC2 sem nenhuma das tags obrigatórias "
                                   f"({', '.join(sorted(required))}) — impede alocação de custos",
                    "potential_savings": 0,
                })
        except ClientError as e:
            print(f"      ⚠ Tags: {e.response['Error']['Code']}")

    # ------------------------------------------------------------------
    # ORQUESTRAÇÃO + GERAÇÃO DO RELATÓRIO
    # ------------------------------------------------------------------
    def fetch_purchase_recommendations(self):
        """Busca recomendações de compra de SP e RIs direto do AWS Cost Explorer
        baseadas em ML do uso histórico (60 dias). Estas são as MESMAS recomendações
        que aparecem no console AWS em Cost Explorer → Recommendations."""
        print("[7/7] Buscando recomendações de compra (SP + RIs)...")
        recommendations = {"savings_plans": [], "reserved_instances": []}

        # Savings Plans purchase recommendation - testa as 3 combinações mais úteis + EC2 SP
        sp_combos = [
            ("COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT"),
            ("COMPUTE_SP", "ONE_YEAR", "PARTIAL_UPFRONT"),
            ("COMPUTE_SP", "ONE_YEAR", "ALL_UPFRONT"),
            ("COMPUTE_SP", "THREE_YEARS", "NO_UPFRONT"),
            ("COMPUTE_SP", "THREE_YEARS", "PARTIAL_UPFRONT"),
            ("COMPUTE_SP", "THREE_YEARS", "ALL_UPFRONT"),
            ("EC2_INSTANCE_SP", "ONE_YEAR", "NO_UPFRONT"),
        ]
        for sp_type, term, payment in sp_combos:
            try:
                resp = self.ce.get_savings_plans_purchase_recommendation(
                    SavingsPlansType=sp_type,
                    TermInYears=term,
                    PaymentOption=payment,
                    LookbackPeriodInDays="SIXTY_DAYS",
                )
                rec = resp.get("SavingsPlansPurchaseRecommendation", {})
                summary = rec.get("SavingsPlansPurchaseRecommendationSummary", {})
                details = rec.get("SavingsPlansPurchaseRecommendationDetails", [])
                if not summary:
                    continue
                est_monthly_savings = float(summary.get("EstimatedMonthlySavingsAmount", 0) or 0)
                if est_monthly_savings <= 0:
                    continue
                hourly_commit = float(summary.get("HourlyCommitmentToPurchase", 0) or 0)
                # Pega min/avg/max do detalhe agregando todos os accounts
                min_hourly = max_hourly = avg_hourly = est_util = upfront_total = 0.0
                if details:
                    mins, maxs, avgs, utils, upfronts = [], [], [], [], []
                    for d in details:
                        try:
                            mins.append(float(d.get("CurrentMinimumHourlyOnDemandSpend", 0) or 0))
                            maxs.append(float(d.get("CurrentMaximumHourlyOnDemandSpend", 0) or 0))
                            avgs.append(float(d.get("CurrentAverageHourlyOnDemandSpend", 0) or 0))
                            utils.append(float(d.get("EstimatedAverageUtilization", 0) or 0))
                            upfronts.append(float(d.get("UpfrontCost", 0) or 0))
                        except (ValueError, TypeError):
                            pass
                    if mins:
                        min_hourly = sum(mins)
                        max_hourly = sum(maxs)
                        avg_hourly = sum(avgs)
                        est_util = sum(utils) / len(utils) if utils else 0
                        upfront_total = sum(upfronts)
                # Estabilidade do workload: usa razão avg/max (mais robusta que max/min,
                # que quebra quando min=0 — comum quando há momentos de cobertura total).
                # avg/max alto (>0.7) = workload estável (média próxima do pico)
                # avg/max baixo (<0.4) = workload variável (média muito menor que pico)
                if max_hourly > 0:
                    avg_to_max = avg_hourly / max_hourly
                    stability_ratio = round(avg_to_max, 2)
                    if avg_to_max >= 0.85:
                        stability_label = "MUITO ESTÁVEL"
                    elif avg_to_max >= 0.65:
                        stability_label = "ESTÁVEL"
                    elif avg_to_max >= 0.45:
                        stability_label = "VARIÁVEL"
                    else:
                        stability_label = "MUITO VARIÁVEL"
                else:
                    stability_ratio = 0
                    stability_label = "?"

                recommendations["savings_plans"].append({
                    "type": sp_type,
                    "term": term,
                    "payment": payment,
                    "hourly_commitment": round(hourly_commit, 4),
                    "monthly_commitment": round(hourly_commit * 730, 2),
                    "estimated_monthly_savings": round(est_monthly_savings, 2),
                    "estimated_annual_savings": round(est_monthly_savings * 12, 2),
                    "savings_pct": round(float(summary.get("EstimatedSavingsPercentage", 0) or 0), 1),
                    "estimated_roi": round(float(summary.get("EstimatedROI", 0) or 0), 1),
                    "current_on_demand_spend": round(float(summary.get("CurrentOnDemandSpend", 0) or 0), 2),
                    "rec_count": int(summary.get("TotalRecommendationCount", 0) or 0),
                    # NOVOS campos detalhados
                    "min_hourly_spend": round(min_hourly, 4),
                    "avg_hourly_spend": round(avg_hourly, 4),
                    "max_hourly_spend": round(max_hourly, 4),
                    "estimated_utilization": round(est_util, 1),
                    "upfront_cost": round(upfront_total, 2),
                    "stability_label": stability_label,
                    "stability_ratio": round(stability_ratio, 2),
                })
                print(f"      → SP {sp_type} {term} {payment}: economia ~${est_monthly_savings:,.2f}/mês")
            except ClientError as e:
                code = e.response['Error']['Code']
                if code != "DataUnavailableException":
                    print(f"      ⚠ SP rec ({sp_type}/{term}/{payment}): {code}")

        # Reserved Instance purchase recommendations
        ri_services = [
            ("Amazon Elastic Compute Cloud - Compute", "EC2"),
            ("Amazon Relational Database Service", "RDS"),
            ("Amazon ElastiCache", "ElastiCache"),
            ("Amazon Redshift", "Redshift"),
            ("Amazon OpenSearch Service", "OpenSearch"),
        ]
        for service_full, service_short in ri_services:
            for term, payment in [("ONE_YEAR", "NO_UPFRONT"), ("ONE_YEAR", "PARTIAL_UPFRONT")]:
                try:
                    kwargs = {
                        "Service": service_full,
                        "LookbackPeriodInDays": "SIXTY_DAYS",
                        "TermInYears": term,
                        "PaymentOption": payment,
                    }
                    # EC2 exige ServiceSpecification
                    if service_short == "EC2":
                        kwargs["ServiceSpecification"] = {"EC2Specification": {"OfferingClass": "STANDARD"}}
                    resp = self.ce.get_reservation_purchase_recommendation(**kwargs)
                    recs_list = resp.get("Recommendations", [])
                    for rec in recs_list:
                        for detail in rec.get("RecommendationDetails", []):
                            est_monthly = float(detail.get("EstimatedMonthlySavingsAmount", 0) or 0)
                            if est_monthly <= 0:
                                continue
                            inst_details = detail.get("InstanceDetails", {})
                            # Extrai info da instância (estrutura varia por serviço)
                            inst_info = ""
                            family = ""
                            for k, v in inst_details.items():
                                if isinstance(v, dict):
                                    inst_type = v.get("InstanceType") or v.get("NodeType") or v.get("CacheNodeType") or v.get("InstanceClass") or "?"
                                    family = v.get("Family", "")
                                    region = v.get("Region", "")
                                    inst_info = f"{inst_type}"
                                    if region:
                                        inst_info += f" ({region})"
                            recommendations["reserved_instances"].append({
                                "service": service_short,
                                "instance_info": inst_info,
                                "family": family,
                                "term": term,
                                "payment": payment,
                                "qty_recommended": int(float(detail.get("RecommendedNumberOfInstancesToPurchase", 0) or 0)),
                                "avg_used_per_hour": round(float(detail.get("AverageNumberOfInstancesUsedPerHour", 0) or 0), 2),
                                "max_used_per_hour": round(float(detail.get("MaximumNumberOfInstancesUsedPerHour", 0) or 0), 2),
                                "avg_utilization": round(float(detail.get("AverageUtilization", 0) or 0), 1),
                                "estimated_monthly_savings": round(est_monthly, 2),
                                "estimated_annual_savings": round(est_monthly * 12, 2),
                                "savings_pct": round(float(detail.get("EstimatedMonthlySavingsPercentage", 0) or 0), 1),
                                "estimated_break_even_months": round(float(detail.get("EstimatedBreakEvenInMonths", 0) or 0), 1),
                                "upfront_cost": round(float(detail.get("UpfrontCost", 0) or 0), 2),
                                "monthly_on_demand": round(float(detail.get("EstimatedMonthlyOnDemandCost", 0) or 0), 2),
                            })
                except ClientError as e:
                    code = e.response['Error']['Code']
                    if code not in ("DataUnavailableException", "ValidationException"):
                        print(f"      ⚠ RI rec ({service_short}/{term}/{payment}): {code}")

            count = len([r for r in recommendations["reserved_instances"] if r["service"] == service_short])
            if count > 0:
                print(f"      → {service_short}: {count} recomendação(ões) de RI")

        # Ordena RIs por economia (maior primeiro)
        recommendations["reserved_instances"].sort(key=lambda x: -x["estimated_monthly_savings"])
        recommendations["savings_plans"].sort(key=lambda x: -x["estimated_monthly_savings"])

        # Totais
        total_sp_savings = sum(s["estimated_monthly_savings"] for s in recommendations["savings_plans"])
        total_ri_savings = sum(r["estimated_monthly_savings"] for r in recommendations["reserved_instances"])
        recommendations["total_sp_savings"] = round(total_sp_savings, 2)
        recommendations["total_ri_savings"] = round(total_ri_savings, 2)
        print(f"      → Economia potencial SP: ${total_sp_savings:,.2f}/mês | RI: ${total_ri_savings:,.2f}/mês")

        self.findings["purchase_recommendations"] = recommendations

    def run(self):
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
        print(f"\n💰 Economia potencial total estimada: "
              f"${self.findings['total_potential_savings']:,.2f}/mês\n")
        return self.findings


# ============================================================================
# GERADOR DE RELATÓRIO HTML - Estilo IAGenix completo
# ============================================================================
def generate_html_report(findings, output_path):
    import json as _json

    cs = findings.get("cost_summary", {})
    services = cs.get("services", [])
    services_detail = cs.get("services_detail", [])
    total_current = cs.get("total_current", cs.get("total", 0))
    total_previous = cs.get("total_previous", 0)
    total_partial = cs.get("total_partial", 0)
    forecast_current_month = cs.get("forecast_current_month", 0)
    total_variation = cs.get("total_variation", 0)
    total_variation_pct = cs.get("total_variation_pct", 0)
    current_period = cs.get("current_period", "N/A")
    previous_period = cs.get("previous_period", "N/A")
    partial_period = cs.get("partial_period", "N/A")
    trend_history = cs.get("trend_history", [])
    tag_breakdown = cs.get("tag_breakdown", {"products": [], "tagged_total": 0, "untagged_total": 0, "tag_rate_pct": 0, "tag_key": None})

    # Helper para extrair label legível do mês a partir do period string "YYYY-MM-DD a YYYY-MM-DD"
    def _month_label(period_str):
        try:
            start = period_str.split(" a ")[0]
            d = datetime.strptime(start, "%Y-%m-%d")
            meses_pt = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
            return f"{meses_pt[d.month-1]}/{d.year}"
        except Exception:
            return period_str
    current_month_label = _month_label(current_period)
    previous_month_label = _month_label(previous_period)

    ri_util = findings.get("reserved_utilization", [])
    sp_util = findings.get("savings_plans_utilization", [])
    sp_history = findings.get("savings_plans_history", {"months": [], "total_savings": 0})
    idle = findings.get("idle_resources", [])
    untagged = findings.get("untagged_resources", [])
    recs = findings.get("recommendations", [])
    ri_inv = findings.get("ri_inventory", {})
    savings = findings.get("total_potential_savings", 0)

    # -------- métricas derivadas --------
    pct_savings = (savings / total_current * 100) if total_current > 0 else 0
    annual_savings = savings * 12
    total_idle_cost = sum(r.get("monthly_cost", 0) for r in idle)
    total_ris = sum(ri_inv.values()) if ri_inv else 0

    if pct_savings < 5:
        health_status, health_color, health_emoji = "EXCELENTE", "#00e68a", "🟢"
    elif pct_savings < 15:
        health_status, health_color, health_emoji = "BOM", "#ffd93d", "🟡"
    elif pct_savings < 30:
        health_status, health_color, health_emoji = "ATENÇÃO", "#ffa657", "🟠"
    else:
        health_status, health_color, health_emoji = "CRÍTICO", "#f85149", "🔴"

    # -------- Tag breakdown --------
    tagged_total = tag_breakdown.get("tagged_total", 0)
    untagged_total = tag_breakdown.get("untagged_total", 0)
    tag_rate = tag_breakdown.get("tag_rate_pct", 0)
    products_list = tag_breakdown.get("products", [])
    tag_key = tag_breakdown.get("tag_key") or "Produto"

    # -------- Variation colors --------
    var_color = "#f85149" if total_variation > 0 else "#00e68a"
    var_sign = "+" if total_variation >= 0 else ""

    # -------- JSON para JS (charts + tabela) --------
    services_json = _json.dumps(services_detail, ensure_ascii=False)
    products_json = _json.dumps(products_list, ensure_ascii=False)
    trend_json = _json.dumps(trend_history, ensure_ascii=False)

    # -------- Cards de inventário de RI --------
    ri_descriptions = {
        "EC2": "Reserved Instances de computação",
        "RDS": "Reserved DB Instances",
        "OpenSearch": "Reserved OpenSearch/Elasticsearch",
        "ElastiCache": "Reserved Cache Nodes",
        "Redshift": "Reserved Nodes Data Warehouse",
    }
    ri_inv_html = ""
    for service, count in (ri_inv.items() if ri_inv else []):
        color = "#00e68a" if count > 0 else "#8b949e"
        emoji = "✅" if count > 0 else "⭕"
        desc = ri_descriptions.get(service, "")
        ri_inv_html += f"""
        <div class="card">
          <h3>{emoji} {service}</h3>
          <div class="value" style="color:{color}">{count}</div>
          <div style="font-size:.75em;color:#8b949e;margin-top:5px">{desc}</div>
        </div>"""
    if not ri_inv_html:
        ri_inv_html = '<p style="grid-column:1/-1;text-align:center;color:#8b949e;padding:20px">Sem reservas ativas detectadas</p>'

    # -------- Tabela CONSOLIDADA de RIs (expiração + utilização) --------
    ri_details = findings.get("ri_details", [])
    sp_details = findings.get("sp_details", [])

    def _expiry_color(status):
        return {
            "EXPIRADA": "#f85149",
            "EXPIRA EM BREVE": "#ffa657",
            "ATENÇÃO": "#ffd93d",
            "OK": "#00e68a",
        }.get(status, "#8b949e")

    # Mapeia utilização AGREGADA por serviço (do ri_util)
    # IMPORTANTE: a API get_reservation_utilization retorna utilização CONSOLIDADA
    # por serviço, não por RI individual. Por isso mostramos isso numa tabela
    # SEPARADA acima da tabela de RIs individuais.
    util_by_service = {r["service"]: r for r in ri_util}

    # Tabela de utilização AGREGADA (uma linha por serviço)
    ri_util_summary_html = ""
    if ri_util:
        util_rows = ""
        for r in ri_util:
            is_ok = "OK" in r["status"]
            util_color = "#00e68a" if is_ok else "#f85149"
            badge_class = "down" if is_ok else "up"
            recommendation = "✅ Manter" if is_ok else "⚠️ Modificar/Vender Marketplace"
            util_rows += f"""
            <tr>
              <td><strong>{r['service']}</strong></td>
              <td><strong style="color:{util_color}">{r['utilization_pct']:.1f}%</strong></td>
              <td>{r['purchased_hours']:.0f}h</td>
              <td>{r['used_hours']:.0f}h</td>
              <td style="color:#ffa657">{r['unused_hours']:.0f}h</td>
              <td style="color:#f85149">${r['estimated_waste']:,.2f}</td>
              <td><span class="badge {badge_class}">{r['status']}</span></td>
              <td style="font-size:.85em;color:#8b949e">{recommendation}</td>
            </tr>"""
        ri_util_summary_html = f"""
        <div class="table-scroll">
          <table>
            <thead><tr><th>Serviço</th><th>Util. Agregada</th><th>Compradas (h)</th><th>Usadas (h)</th><th>Não Usadas (h)</th><th>Desperdício/mês</th><th>Status</th><th>Ação</th></tr></thead>
            <tbody>{util_rows}</tbody>
          </table>
        </div>"""
    else:
        ri_util_summary_html = '<p style="text-align:center;color:#8b949e;padding:20px">⭕ Sem dados de utilização agregada disponíveis</p>'

    ri_consolidated_html = ""
    if ri_details:
        sorted_ris = sorted(ri_details, key=lambda x: (x["days_remaining"] if x["days_remaining"] is not None else 99999))
        rows = ""
        for r in sorted_ris:
            exp_color = _expiry_color(r["status"])
            days_str = (
                f"<strong style='color:{exp_color}'>EXPIRADA há {abs(r['days_remaining'])}d</strong>"
                if r["days_remaining"] is not None and r["days_remaining"] < 0
                else f"<strong style='color:{exp_color}'>{r['days_remaining']}d</strong>"
                if r["days_remaining"] is not None
                else "?"
            )
            # Verifica se o SERVIÇO (não a RI individual) tem boa utilização agregada
            # — usado apenas para a coluna "Ação", não para mostrar % por RI
            util_data = util_by_service.get(r["service"])
            service_util_ok = True
            if util_data:
                service_util_ok = "OK" in util_data["status"]
            # Ação combinada (prioriza expiração)
            if r["status"] == "EXPIRADA":
                action = "🔴 Renovar URGENTE"
                action_color = "#f85149"
            elif r["status"] == "EXPIRA EM BREVE":
                action = "🟠 Avaliar renovação"
                action_color = "#ffa657"
            elif not service_util_ok:
                action = "⚠️ Serviço subutilizado"
                action_color = "#f85149"
            elif r["status"] == "ATENÇÃO":
                action = "🟡 Planejar renovação"
                action_color = "#ffd93d"
            else:
                action = "✅ Manter"
                action_color = "#00e68a"

            rows += f"""
            <tr>
              <td><strong>{r['service']}</strong></td>
              <td><code style="font-size:.78em">{r['id'][:30]}</code></td>
              <td>{r['instance_type']}</td>
              <td style="text-align:center">{r['count']}</td>
              <td style="font-size:.85em">{r['offering']}</td>
              <td>{r['end_date']}</td>
              <td>{days_str}</td>
              <td><span class="badge" style="background:rgba({int(exp_color[1:3],16)},{int(exp_color[3:5],16)},{int(exp_color[5:7],16)},0.2);color:{exp_color}">{r['status']}</span></td>
              <td style="font-size:.82em;color:{action_color}">{action}</td>
            </tr>"""
        ri_consolidated_html = f"""
        <div class="table-scroll">
          <table>
            <thead><tr><th>Serviço</th><th>ID</th><th>Tipo</th><th>Qtd</th><th>Pagamento</th><th>Expira em</th><th>Restante</th><th>Status Exp.</th><th>Ação</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""
    else:
        ri_consolidated_html = '<p style="text-align:center;color:#8b949e;padding:30px">⭕ Nenhuma Reserved Instance ativa detectada na região</p>'

    # -------- Savings Plans CONSOLIDADO (utilização + expiração em uma seção) --------
    sp_monthly_total = 0
    has_sp_util = bool(sp_util)
    has_sp_details = bool(sp_details)

    sp_consolidated_html = ""
    if has_sp_util:
        sp = sp_util[0]
        sp_pct = sp['utilization_pct']
        sp_color = "#00e68a" if sp_pct >= 80 else "#ffa657" if sp_pct >= 50 else "#f85149"
        sp_interpret = '✅ Savings Plan bem dimensionado.' if sp_pct >= 80 else '⚠️ Compromisso maior que uso real — considere não renovar ou reduzir.'
        source_note = f" <span style='color:#8b949e;font-size:.85em'>(fonte: {sp.get('source_period', 'N/A')})</span>" if sp.get('source_period') else ""
        sp_monthly_total = sp.get('net_savings', 0)
        sp_consolidated_html += f"""
        <h3 style="color:#00e68a;margin-bottom:15px">📊 Métricas Agregadas (todos os SPs ativos somados){source_note}</h3>
        <p style="color:#8b949e;font-size:.85em;margin:-8px 0 12px 0">Estes números são o <strong>total consolidado</strong> de todos os Savings Plans ativos. Detalhes de cada SP individual estão na tabela abaixo.</p>
        <div class="sp-summary">
          <div class="sp-metric"><div class="sp-label">Utilização</div><div class="sp-value" style="color:{sp_color}">{sp_pct:.1f}%</div></div>
          <div class="sp-metric"><div class="sp-label">Compromisso</div><div class="sp-value">${sp['total_commitment']:,.2f}</div></div>
          <div class="sp-metric"><div class="sp-label">Usado</div><div class="sp-value" style="color:#00e68a">${sp['used_commitment']:,.2f}</div></div>
          <div class="sp-metric"><div class="sp-label">Não Usado</div><div class="sp-value" style="color:#f85149">${sp['unused_commitment']:,.2f}</div></div>
          <div class="sp-metric"><div class="sp-label">💰 Economia Líquida</div><div class="sp-value" style="color:#00e68a">${sp['net_savings']:,.2f}</div></div>
        </div>
        <div class="progress-bar" style="margin-top:20px;height:25px"><div class="progress-fill" style="width:{sp_pct}%;background:linear-gradient(90deg,{sp_color},#b388ff)"></div></div>
        <p style="margin-top:15px;color:#8b949e;font-size:.9em">{sp_interpret}</p>"""

    if has_sp_details:
        sorted_sps = sorted(sp_details, key=lambda x: (x["days_remaining"] if x["days_remaining"] is not None else 99999))
        sp_rows = ""
        for s in sorted_sps:
            color = _expiry_color(s["status"])
            days_str = (
                f"<strong style='color:{color}'>EXPIRADO há {abs(s['days_remaining'])}d</strong>"
                if s["days_remaining"] is not None and s["days_remaining"] < 0
                else f"<strong style='color:{color}'>{s['days_remaining']}d</strong>"
                if s["days_remaining"] is not None
                else "?"
            )
            if s["status"] == "EXPIRADA":
                action = "🔴 Renovar URGENTE"
                action_color = "#f85149"
            elif s["status"] == "EXPIRA EM BREVE":
                action = "🟠 Avaliar renovação"
                action_color = "#ffa657"
            elif s["status"] == "ATENÇÃO":
                action = "🟡 Planejar renovação"
                action_color = "#ffd93d"
            else:
                action = "✅ Manter"
                action_color = "#00e68a"
            sp_rows += f"""
            <tr>
              <td><code style="font-size:.78em">{s['id'][:30]}</code></td>
              <td><strong>{s['type']}</strong></td>
              <td style="font-size:.85em">{s['payment']}</td>
              <td style="text-align:center">{s['term_years']}y</td>
              <td>${s['commitment_hourly']}/h</td>
              <td>{s['end_date']}</td>
              <td>{days_str}</td>
              <td><span class="badge" style="background:rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.2);color:{color}">{s['status']}</span></td>
              <td style="font-size:.82em;color:{action_color}">{action}</td>
            </tr>"""
        sp_consolidated_html += f"""
        <h3 style="color:#00e68a;margin:25px 0 15px">📋 Compromissos Ativos (com data de expiração)</h3>
        <div class="table-scroll">
          <table>
            <thead><tr><th>ID</th><th>Tipo</th><th>Pagamento</th><th>Termo</th><th>Compromisso</th><th>Expira em</th><th>Restante</th><th>Status Exp.</th><th>Ação</th></tr></thead>
            <tbody>{sp_rows}</tbody>
          </table>
        </div>"""

    if not has_sp_util and not has_sp_details:
        sp_consolidated_html = """
        <div style="text-align:center;padding:30px">
          <p style="color:#8b949e;font-size:1.1em;margin-bottom:15px">⭕ Nenhum Savings Plan ativo detectado</p>
          <p style="color:#ffa657">💡 <strong>Oportunidade:</strong> Compute Savings Plans podem reduzir até 66% vs On-Demand para workloads EC2/Fargate/Lambda estáveis.</p>
        </div>"""

    # -------- Extrato histórico Savings Plans (cards mensais) --------
    sp_months = sp_history.get("months", [])
    # A classificação local vs externo agora vem direto da API:
    # - is_local=True quando esta conta pagou SavingsPlanRecurringFee/UpfrontFee no mês
    # - is_local=False quando há cobertura mas zero pagamento local (SP em conta payer/org)

    # Verifica se algum mês tem cobertura mas SEM pagamento local detectado (= origem externa)
    has_external_origin = any(
        m["on_demand_cost"] > 0 and not m.get("is_local", False)
        for m in sp_months
    )

    sp_history_intro = ""
    if has_external_origin and sp_months:
        sp_history_intro = """
        <details class="expandable" style="margin-bottom:15px">
          <summary style="color:#ffa657">🔗 Cobertura externa detectada — clique para entender</summary>
          <div style="padding:14px 18px;font-size:.92em;line-height:1.6;color:#c9d1d9">
            Os meses marcados com <span class="origin-badge external">🔗 SP externo</span> são cobertos por um Savings Plan em <strong>outra conta</strong> (geralmente a payer da Organization). Esta conta pagou <strong>$0 de fee de SP</strong> nesses meses — a "economia" mostrada é apenas <strong>showback contábil</strong>, não dinheiro que entrou aqui. Pode desaparecer se a payer não renovar o SP.
          </div>
        </details>"""

    sp_history_html = sp_history_intro
    for m in sp_months:
        is_local = m.get("is_local", False)
        local_paid = m.get("local_sp_paid", 0)
        if is_local:
            badge_html = f'<span class="origin-badge local">✅ SP nesta conta (paguei ${local_paid:,.2f})</span>'
        else:
            badge_html = '<span class="origin-badge external">🔗 SP externo (paguei $0)</span>'
        border_color = "#b388ff" if is_local else "#ffa657"
        sp_history_html += f"""
        <div class="sp-month-card" style="border-left-color:{border_color}">
          <div class="sp-month-header">
            <h3>{m['period']}</h3>
            {badge_html}
          </div>
          <div class="cost-line"><span>Custo On-Demand (sem SP):</span><span>${m['on_demand_cost']:,.2f}</span></div>
          <div class="cost-line"><span>Custo com Savings Plans (amortizado):</span><span style="color:#b388ff">${m['sp_cost']:,.2f}</span></div>
          <div class="cost-line cost-line-total"><span><strong>ECONOMIA:</strong></span><span style="color:#00e68a"><strong>${m['savings']:,.2f} ({m['savings_pct']:.1f}%)</strong></span></div>
        </div>"""
    if sp_months:
        total_od = sp_history.get('total_on_demand', 0)
        total_sp = sp_history.get('total_sp_cost', 0)
        total_sv = sp_history.get('total_savings', 0)
        total_pct = sp_history.get('total_savings_pct', 0)
        local_count = sum(1 for m in sp_months if m.get("is_local", False))
        external_count = len(sp_months) - local_count
        local_savings = sum(m["savings"] for m in sp_months if m.get("is_local", False))
        external_savings = sum(m["savings"] for m in sp_months if not m.get("is_local", False))
        composition = ""
        if external_count > 0 and local_count > 0:
            composition = f"""
            <div style='margin-top:12px;padding:10px;background:rgba(0,0,0,0.2);border-radius:6px;font-size:.88em;line-height:1.7'>
              <strong style='color:#00e68a'>✅ Sua economia REAL ({local_count} mês{'es' if local_count > 1 else ''}):</strong> ${local_savings:,.2f}<br>
              <strong style='color:#ffa657'>🔗 Showback de SP externo ({external_count} mês{'es' if external_count > 1 else ''}):</strong> ${external_savings:,.2f}
              <span style='color:#8b949e;font-size:.92em'>(não conte como sua)</span>
            </div>"""
        elif external_count > 0:
            composition = f"""
            <div style='margin-top:12px;padding:10px;background:rgba(255,166,87,0.1);border-left:3px solid #ffa657;border-radius:6px;font-size:.88em;line-height:1.7'>
              <strong style='color:#ffa657'>⚠️ TODA a economia mostrada é showback de SP em outra conta</strong> — você efetivamente pagou <strong>$0.00 de fee de SP</strong> nesses {external_count} meses.
              A "economia" de ${total_sv:,.2f} é alocação contábil da AWS, não dinheiro que entrou no seu bolso.
            </div>"""
        elif local_count > 0:
            composition = f"<div style='margin-top:8px;font-size:.88em;color:#00e68a'>✅ Toda a economia veio de SPs comprados nesta conta — economia REAL.</div>"
        sp_history_html += f"""
        <div class="sp-month-card sp-total">
          <h3>💰 TOTAL ACUMULADO ({len(sp_months)} meses)</h3>
          <div class="cost-line"><span>Custo On-Demand (sem SP):</span><span>${total_od:,.2f}</span></div>
          <div class="cost-line"><span>Custo com Savings Plans (amortizado):</span><span style="color:#b388ff">${total_sp:,.2f}</span></div>
          <div class="cost-line cost-line-total"><span style="font-size:1.2em"><strong>ECONOMIA TOTAL:</strong></span><span style="color:#00e68a;font-size:1.2em"><strong>${total_sv:,.2f} ({total_pct:.1f}%)</strong></span></div>
          {composition}
        </div>"""
    else:
        sp_history_html = '<p style="color:#8b949e;text-align:center;padding:20px">Sem histórico de Savings Plans disponível nos últimos 6 meses.</p>'

    # -------- Recursos ociosos agrupados --------
    idle_by_type = {}
    for r in idle:
        t = r['type']
        if t not in idle_by_type:
            idle_by_type[t] = {"items": [], "total_cost": 0}
        idle_by_type[t]["items"].append(r)
        idle_by_type[t]["total_cost"] += r.get('monthly_cost', 0)
    idle_html = ""
    for t, data in sorted(idle_by_type.items(), key=lambda x: -x[1]["total_cost"]):
        count = len(data["items"])
        # Primeiras 10 linhas visíveis sempre
        visible_rows = ""
        for item in data["items"][:10]:
            visible_rows += f"""<tr><td><code>{item['id']}</code></td><td>{item['details']}</td><td style="color:#f85149">${item['monthly_cost']:,.2f}</td></tr>"""
        # Resto em <details> expansível
        extra_block = ""
        if count > 10:
            extra_rows = ""
            for item in data["items"][10:]:
                extra_rows += f"""<tr><td><code>{item['id']}</code></td><td>{item['details']}</td><td style="color:#f85149">${item['monthly_cost']:,.2f}</td></tr>"""
            extra_block = f"""
            <details class="expandable">
              <summary>🔽 Mostrar +{count-10} recurso(s) restantes</summary>
              <table style="margin-top:10px"><thead><tr><th>ID</th><th>Detalhes</th><th>Custo</th></tr></thead><tbody>{extra_rows}</tbody></table>
            </details>"""
        idle_html += f"""
        <div class="idle-group">
          <div class="idle-header">
            <div><strong style="color:#ffa657;font-size:1.1em">{t}</strong><span style="color:#8b949e;margin-left:10px">{count} recurso(s)</span></div>
            <div style="color:#f85149;font-weight:700">${data['total_cost']:,.2f}/mês</div>
          </div>
          <table><thead><tr><th>ID</th><th>Detalhes</th><th>Custo</th></tr></thead><tbody>{visible_rows}</tbody></table>
          {extra_block}
        </div>"""
    if not idle_html:
        idle_html = '<p style="text-align:center;color:#00e68a;padding:30px;font-size:1.1em">✅ Nenhum recurso ocioso detectado!</p>'

    # -------- Recomendações combinadas (detectadas + boas práticas com CUSTO REAL) --------
    # Helper: procura custo de serviço por palavras-chave no breakdown real
    def service_cost(*keywords):
        total = 0.0
        for s in services:
            name_lower = s["name"].lower()
            if any(k.lower() in name_lower for k in keywords):
                total += s["cost"]
        return total

    # Custos detectados na conta
    ec2_cost = service_cost("Elastic Compute Cloud", "EC2")
    rds_cost = service_cost("Relational Database", "RDS")
    lambda_cost = service_cost("Lambda")
    s3_cost = service_cost("Simple Storage", "S3")
    cloudwatch_cost = service_cost("CloudWatch")
    opensearch_cost = service_cost("OpenSearch", "Elasticsearch")
    data_transfer_cost = service_cost("Data Transfer")
    nat_cost = service_cost("NAT Gateway")
    compute_total = ec2_cost + rds_cost + lambda_cost

    def fmt_cost_line(label, cost, saving_low, saving_high):
        """Formata linha 'custo atual → economia estimada'."""
        if cost <= 0:
            return f"<em style='color:#8b949e'>⚠️ Nenhum custo de {label} detectado — recomendação informativa.</em>"
        low = cost * saving_low / 100
        high = cost * saving_high / 100
        return (f"<strong style='color:#ffd93d'>Custo atual de {label}: ${cost:,.2f}/mês</strong> "
                f"→ economia estimada <span style='color:#00e68a'><strong>${low:,.2f} a ${high:,.2f}/mês</strong></span> "
                f"(<strong>${low*12:,.0f} a ${high*12:,.0f}/ano</strong>)")

    best_practices = []

    # Compute Optimizer — aplica a EC2+RDS+Lambda
    best_practices.append({
        "priority": "ALTA" if compute_total > 500 else "MÉDIA",
        "type": "Compute Optimizer (Rightsizing)",
        "service": "EC2/RDS/Lambda",
        "description": "Habilite o AWS Compute Optimizer para receber recomendações automáticas de rightsizing baseadas em ML. Ele analisa CPU/memória/rede e sugere tamanhos menores sem perda de performance.",
        "cost_detail": fmt_cost_line("compute (EC2+RDS+Lambda)", compute_total, 10, 25),
        "action": "Console AWS → Compute Optimizer → Opt-in (grátis) → aguardar 14 dias de análise → aplicar recomendações",
        "effort": "5 min setup + 1-2 semanas para aplicar sugestões",
    })

    # Cost Anomaly Detection — sempre aplica
    best_practices.append({
        "priority": "ALTA",
        "type": "Cost Anomaly Detection",
        "service": "Conta toda",
        "description": "Configure alertas automáticos de anomalias de custo para detectar gastos inesperados em tempo real (ex: instância esquecida ligada, loop de Lambda, ataque DDoS consumindo Data Transfer).",
        "cost_detail": f"<strong style='color:#ffd93d'>Gasto mensal monitorado: ${total_current:,.2f}</strong> → previne picos que historicamente causam <strong style='color:#f85149'>+20% a +100% de surpresa na fatura</strong>. Serviço <strong>gratuito</strong>.",
        "action": "Cost Explorer → Cost Anomaly Detection → Create monitor (tipo: AWS services) → configurar SNS com e-mail",
        "effort": "10 minutos",
    })

    # Tagging
    untagged_count = len(untagged)
    tagging_cost_detail = (
        f"<strong style='color:#f85149'>{untagged_count} recurso(s) sem tags obrigatórias</strong> → impede alocação correta de custos por time/projeto. "
        f"Sem tags, <strong>100% do custo (${total_current:,.2f}/mês)</strong> fica no bucket 'não alocado'."
        if untagged_count > 0 else
        f"<strong style='color:#00e68a'>✅ Recursos analisados estão com tags.</strong> Mantenha política de tags obrigatórias para garantir 100% de alocação do custo (${total_current:,.2f}/mês)."
    )
    best_practices.append({
        "priority": "ALTA" if untagged_count > 0 else "MÉDIA",
        "type": "Tagging Strategy Obrigatória",
        "service": "Conta toda",
        "description": "Implemente tags obrigatórias (Environment, Project, Owner, CostCenter) via Service Control Policies e Tag Policies. Sem tags, relatórios de chargeback/showback não funcionam.",
        "cost_detail": tagging_cost_detail,
        "action": "AWS Organizations → Tag Policies + SCPs que bloqueiem criação sem tags + Cost Allocation Tags ativadas",
        "effort": "1-2 dias para setup + remediação de recursos existentes",
    })

    # S3 Intelligent-Tiering
    if s3_cost > 0:
        best_practices.append({
            "priority": "ALTA" if s3_cost > 300 else "MÉDIA",
            "type": "S3 Intelligent-Tiering & Lifecycle",
            "service": "Amazon S3",
            "description": "Mova buckets com padrão de acesso desconhecido para Intelligent-Tiering (AWS move automaticamente entre camadas). Configure lifecycle para Glacier/Deep Archive em dados frios.",
            "cost_detail": fmt_cost_line("S3", s3_cost, 30, 70),
            "action": "S3 → Bucket → Management → Lifecycle rule → Intelligent-Tiering para prefixos sem padrão claro + Glacier para backups >90 dias",
            "effort": "30 min por bucket (pode ser automatizado via AWS Config)",
        })

    # Graviton
    if compute_total > 200:
        best_practices.append({
            "priority": "MÉDIA",
            "type": "Migração para Graviton (ARM)",
            "service": "EC2/RDS/Lambda",
            "description": "Migre workloads compatíveis para instâncias Graviton (famílias t4g, m7g, r7g, c7g). Mesma performance a até 40% menos custo. Lambda ARM: grátis para migrar.",
            "cost_detail": fmt_cost_line("compute elegível a ARM", compute_total, 15, 30),
            "action": "1) Lambda → mudar arquitetura para arm64 (imediato). 2) EC2/RDS → testar em dev, validar compatibilidade de libs, migrar production gradualmente",
            "effort": "Lambda: minutos. EC2/RDS: dias a semanas por workload",
        })

    # Spot Instances
    if ec2_cost > 300:
        best_practices.append({
            "priority": "MÉDIA",
            "type": "Spot Instances para workloads tolerantes",
            "service": "Amazon EC2",
            "description": "Use Spot Instances para workloads que toleram interrupção: batch, CI/CD, processamento assíncrono, dev/staging, treinamento de ML, workers de fila.",
            "cost_detail": fmt_cost_line("EC2", ec2_cost, 40, 70),
            "action": "Auto Scaling Groups → Mixed Instances Policy (mix On-Demand + Spot) + EC2 Fleet + Capacity Rebalancing habilitado",
            "effort": "1-2 dias por workload (depende de idempotência do código)",
        })

    # RDS
    if rds_cost > 100:
        best_practices.append({
            "priority": "MÉDIA",
            "type": "RDS Storage Auto Scaling + Graviton + Aurora Serverless v2",
            "service": "Amazon RDS",
            "description": "1) Habilite Storage Auto Scaling (evita sobredimensionar). 2) Migre para db.r7g (Graviton). 3) Considere Aurora Serverless v2 para workloads com picos.",
            "cost_detail": fmt_cost_line("RDS", rds_cost, 15, 35),
            "action": "RDS → Modify → Storage autoscaling ON + trocar classe de instância para Graviton + avaliar Aurora Serverless v2",
            "effort": "5 min por instância (auto scaling) a 1 dia (migração de classe)",
        })

    # OpenSearch
    if opensearch_cost > 100:
        best_practices.append({
            "priority": "MÉDIA",
            "type": "OpenSearch - UltraWarm & Cold Storage",
            "service": "Amazon OpenSearch",
            "description": "Para clusters grandes, mova índices antigos para UltraWarm (80% mais barato) e Cold Storage (90% mais barato). Reserved Instances oferecem até 50% de desconto para workloads estáveis.",
            "cost_detail": fmt_cost_line("OpenSearch", opensearch_cost, 30, 60),
            "action": "OpenSearch → Index State Management (ISM) policies → move hot→warm→cold baseado em idade",
            "effort": "2-4 horas para setup de ISM policies",
        })

    # CloudWatch Logs — ESTE É O QUE O USUÁRIO PEDIU COMO EXEMPLO
    if cloudwatch_cost > 0:
        best_practices.append({
            "priority": "ALTA" if cloudwatch_cost > 200 else "MÉDIA",
            "type": "CloudWatch Logs Retention & Log Class",
            "service": "Amazon CloudWatch",
            "description": "Por padrão, Log Groups ficam 'Never Expire' = custo infinito crescente. Defina retenção (7/30/90 dias conforme compliance) e use Infrequent Access Log Class para logs raramente consultados (50% mais barato).",
            "cost_detail": fmt_cost_line("CloudWatch (Logs + Metrics)", cloudwatch_cost, 30, 60),
            "action": (
                "1) Script: <code>aws logs describe-log-groups</code> → identificar grupos sem retenção. "
                "2) <code>aws logs put-retention-policy --log-group-name X --retention-in-days 30</code>. "
                "3) Mover para Infrequent Access Log Class logs de auditoria. "
                "4) Avaliar exportação para S3 + Athena em vez de manter no CW Logs."
            ),
            "effort": "30 min (script bulk) ou 1 min por log group manual",
        })

    # NAT Gateway / Data Transfer
    if nat_cost > 0 or data_transfer_cost > 50:
        dt_total = nat_cost + data_transfer_cost
        best_practices.append({
            "priority": "MÉDIA" if dt_total > 200 else "BAIXA",
            "type": "VPC Endpoints (substituir NAT Gateway)",
            "service": "VPC / Data Transfer",
            "description": "Tráfego de serviços AWS via NAT Gateway é caro ($0.045/GB + $0.045/hora). Gateway VPC Endpoints (S3, DynamoDB) são GRÁTIS. Interface Endpoints (outros serviços) custam bem menos.",
            "cost_detail": fmt_cost_line("NAT Gateway + Data Transfer", dt_total, 30, 60),
            "action": "VPC → Endpoints → Create Gateway Endpoints (S3, DynamoDB) → atualizar route tables. Depois, Interface Endpoints para SSM, ECR, CloudWatch, etc.",
            "effort": "1-2 horas (Gateway endpoints) + teste de conectividade",
        })

    # Lambda
    if lambda_cost > 50:
        best_practices.append({
            "priority": "BAIXA",
            "type": "Lambda - Memory Tuning & ARM",
            "service": "AWS Lambda",
            "description": "Use AWS Lambda Power Tuning para encontrar o ponto ótimo memória×custo. Migre para arm64 (Graviton) — 20% mais barato sem mudança de código Python/Node.",
            "cost_detail": fmt_cost_line("Lambda", lambda_cost, 20, 40),
            "action": "1) Deploy Lambda Power Tuning Step Function. 2) Rodar em funções top-10 por custo. 3) Ajustar memory + mudar architecture para arm64",
            "effort": "1 dia inicial + 15 min por função",
        })

    # Savings Plans / RIs — recomendação sempre útil
    if compute_total > 1000 and not sp_util:
        best_practices.append({
            "priority": "ALTA",
            "type": "Comprar Compute Savings Plan",
            "service": "EC2/Fargate/Lambda",
            "description": f"Com ${compute_total:,.0f}/mês em compute estável, um Compute Savings Plan de 1 ano (pagamento parcial) oferece ~27% de desconto automaticamente em EC2, Fargate e Lambda — sem precisar escolher família/região.",
            "cost_detail": fmt_cost_line("compute estável", compute_total * 0.7, 20, 27),
            "action": "1) Cost Explorer → Savings Plans → Recommendations → seguir sugestão conservadora (70% da baseline). 2) Comprar 1yr No Upfront primeiro (menor risco)",
            "effort": "1 hora para análise + compra",
        })

    # Monta lista final (detectados primeiro)
    all_recs = []
    for r in recs:
        all_recs.append({
            "priority": "ALTA",
            "type": r["type"],
            "service": r["service"],
            "description": r["description"],
            "cost_detail": f"<strong style='color:#00e68a'>💰 Economia detectada: ${r['potential_savings']:,.2f}/mês (${r['potential_savings']*12:,.2f}/ano)</strong>",
            "action": "Ver detalhes nas seções de RIs/Savings Plans/Recursos Ociosos acima",
            "effort": "Variável",
            "is_detected": True,
        })
    for bp in best_practices:
        bp["is_detected"] = False
        all_recs.append(bp)
    priority_order = {"ALTA": 0, "MÉDIA": 1, "BAIXA": 2}
    all_recs.sort(key=lambda x: priority_order.get(x["priority"], 99))

    # -------- Recomendações de COMPRA (SP/RIs) do AWS Cost Explorer --------
    purchase_recs = findings.get("purchase_recommendations", {})
    sp_recs = purchase_recs.get("savings_plans", [])
    ri_recs = purchase_recs.get("reserved_instances", [])
    total_sp_purchase_savings = purchase_recs.get("total_sp_savings", 0)
    total_ri_purchase_savings = purchase_recs.get("total_ri_savings", 0)

    purchase_html = ""

    # Análise contextualizada de Savings Plans + comparação lado-a-lado
    sp_purchase_cards = ""
    if sp_recs:
        # Pega o primeiro recomendado para extrair contexto do workload (todos compartilham o mesmo)
        sample = sp_recs[0]
        avg_h = sample.get("avg_hourly_spend", 0)
        min_h = sample.get("min_hourly_spend", 0)
        max_h = sample.get("max_hourly_spend", 0)
        stability = sample.get("stability_label", "?")
        stability_color = {
            "MUITO ESTÁVEL": "#00e68a",
            "ESTÁVEL": "#7ee787",
            "VARIÁVEL": "#ffa657",
            "MUITO VARIÁVEL": "#f85149",
            "?": "#8b949e",
        }.get(stability, "#8b949e")

        # Identifica o melhor SP (maior economia mensal) e o melhor ROI
        best_savings = max(sp_recs, key=lambda x: x["estimated_monthly_savings"])
        best_roi = max(sp_recs, key=lambda x: x["estimated_roi"])

        # Lista de famílias EC2 que aparecem nas recomendações de RI (contexto)
        ec2_families_covered = sorted(set(
            r["instance_info"].split(" ")[0] for r in ri_recs
            if r["service"] in ("EC2", "RDS", "ElastiCache")
        ))[:8]
        families_str = ", ".join(f"<code>{f}</code>" for f in ec2_families_covered) if ec2_families_covered else "EC2/Fargate/Lambda em geral"

        # Box de contexto do workload
        sp_purchase_cards += f"""
        <div class="sp-context-box">
          <h3 style="color:#00e68a;margin-bottom:12px">🔍 Análise do Seu Workload de Compute</h3>
          <div class="sp-context-grid">
            <div class="sp-context-metric">
              <div class="sp-context-label">Spend on-demand médio/h</div>
              <div class="sp-context-value">${avg_h:,.4f}</div>
              <div class="sp-context-sub">${avg_h*730:,.2f}/mês equivalente</div>
            </div>
            <div class="sp-context-metric">
              <div class="sp-context-label">Mínimo (vale do uso)</div>
              <div class="sp-context-value" style="color:#58a6ff">${min_h:,.4f}/h</div>
              <div class="sp-context-sub">menor consumo na janela</div>
            </div>
            <div class="sp-context-metric">
              <div class="sp-context-label">Pico (topo do uso)</div>
              <div class="sp-context-value" style="color:#ffa657">${max_h:,.4f}/h</div>
              <div class="sp-context-sub">maior consumo na janela</div>
            </div>
            <div class="sp-context-metric">
              <div class="sp-context-label">Estabilidade do workload</div>
              <div class="sp-context-value" style="color:{stability_color}">{stability}</div>
              <div class="sp-context-sub">média/pico: {sample.get('stability_ratio', 0)*100:.0f}% (quanto maior, mais estável)</div>
            </div>
          </div>
          <div class="sp-interpretation">
            <strong>📖 O que isso significa:</strong> Sua conta gasta em média <strong>${avg_h:,.4f}/h</strong> em compute on-demand,
            variando de <strong>${min_h:,.4f}</strong> (vale) até <strong>${max_h:,.4f}</strong> (pico). O AWS recomenda um compromisso
            de <strong>${best_savings['hourly_commitment']:,.4f}/h</strong> — esse valor cobre a "base" estável do seu uso, sem desperdiçar capacidade
            comprometida nos vales. Workloads classificados como <strong style="color:{stability_color}">{stability}</strong>
            {'são candidatos IDEAIS a Savings Plans (alta utilização garantida).' if stability in ('MUITO ESTÁVEL', 'ESTÁVEL') else 'exigem cautela — comece com compromisso menor e aumente gradualmente conforme observa o uso real.'}
            <br><br>
            <strong>🎯 Esse SP cobriria workloads como:</strong> {families_str}
          </div>
        </div>"""

        # Tabela de comparação lado-a-lado
        rows = ""
        for s in sorted(sp_recs, key=lambda x: -x["estimated_monthly_savings"]):
            term_label = "1 ano" if s["term"] == "ONE_YEAR" else "3 anos"
            payment_label = {"NO_UPFRONT": "Sem entrada", "PARTIAL_UPFRONT": "Parcial", "ALL_UPFRONT": "Total"}.get(s["payment"], s["payment"])
            sp_type_label = {"COMPUTE_SP": "Compute SP", "EC2_INSTANCE_SP": "EC2 Instance SP", "SAGEMAKER_SP": "SageMaker SP"}.get(s["type"], s["type"])
            is_best_savings = s is best_savings
            is_best_roi = s is best_roi
            highlight = ""
            badges = ""
            if is_best_savings:
                highlight = "background:rgba(0,230,138,0.06)"
                badges += '<span class="best-tag" style="background:#00e68a;color:#0a0e1a">🏆 MAIOR ECONOMIA</span>'
            if is_best_roi and not is_best_savings:
                badges += '<span class="best-tag" style="background:#b388ff;color:#0a0e1a">⚡ MELHOR ROI</span>'
            payback_str = f"{(s['upfront_cost'] / s['estimated_monthly_savings']):.1f}m" if s['upfront_cost'] > 0 and s['estimated_monthly_savings'] > 0 else "imediato"
            rows += f"""
            <tr style="{highlight}">
              <td><strong>{sp_type_label}</strong>{badges}</td>
              <td style="text-align:center">{term_label}</td>
              <td style="text-align:center">{payment_label}</td>
              <td>${s['hourly_commitment']:.4f}/h</td>
              <td>${s['monthly_commitment']:,.2f}</td>
              <td style="color:#ffa657">${s['upfront_cost']:,.2f}</td>
              <td style="color:#00e68a;font-weight:700">${s['estimated_monthly_savings']:,.2f}</td>
              <td style="color:#00e68a">${s['estimated_annual_savings']:,.2f}</td>
              <td><strong style="color:#00e68a">{s['savings_pct']:.1f}%</strong></td>
              <td>{s['estimated_roi']:.1f}%</td>
              <td>{s['estimated_utilization']:.0f}%</td>
              <td style="font-size:.85em">{payback_str}</td>
            </tr>"""

        sp_purchase_cards += f"""
        <h3 style="color:#00e68a;margin:25px 0 12px;font-size:1.15em">📊 Comparação Lado-a-Lado de Todas as Opções</h3>
        <p style="color:#8b949e;font-size:.9em;margin-bottom:12px">Compare termos (1y/3y) e formas de pagamento. Linha destacada em verde = maior economia absoluta. Tag roxa = melhor ROI.</p>
        <div class="table-scroll">
          <table style="font-size:.88em">
            <thead><tr><th>Tipo</th><th>Termo</th><th>Pagamento</th><th>Compromisso/h</th><th>Compromisso/mês</th><th>Upfront</th><th>Economia/mês</th><th>Economia/ano</th><th>Desconto</th><th>ROI</th><th>Util. esperada</th><th>Payback</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>

        <div class="sp-decision-guide">
          <h3 style="color:#00e68a;margin-bottom:10px">🧭 Como Escolher</h3>
          <ul style="margin:0;padding-left:20px;line-height:1.8;color:#c9d1d9">
            <li><strong>Quer máxima economia e tem caixa para upfront?</strong> Vá de <code>3 anos All Upfront</code> (maior desconto, mas trava 3 anos)</li>
            <li><strong>Quer economia sem amarrar caixa?</strong> Vá de <code>1 ano No Upfront</code> (menor risco, fácil de testar)</li>
            <li><strong>Workload {stability}?</strong> {'Pode arriscar termo de 3 anos com segurança.' if stability in ('MUITO ESTÁVEL', 'ESTÁVEL') else 'Comece com 1 ano para validar antes de comprometer 3 anos.'}</li>
            <li><strong>Não sabe?</strong> A regra de ouro: <code>1 ano No Upfront</code> com compromisso = <strong>70% do uso atual</strong> (deixa margem para variação)</li>
          </ul>
        </div>

        <div class="purchase-action">
          📋 <strong>Como comprar:</strong> AWS Console → Cost Explorer → Savings Plans → <strong>Recommendations</strong> → escolha a linha desejada da tabela acima → ajuste o "Hourly commitment" conforme sua tolerância a risco → <strong>Add to cart</strong> → revisar e comprar.
        </div>"""
    else:
        sp_purchase_cards = '<p style="color:#8b949e;text-align:center;padding:25px">⭕ Nenhuma recomendação de Savings Plan disponível. Pode ser que você já tenha boa cobertura, ou que o uso histórico não justifique a compra (lookback de 60 dias).</p>'

    # Cards de RIs recomendados (agrupado por serviço)
    ri_purchase_cards = ""
    if ri_recs:
        # Agrupa por serviço para mostrar resumo
        by_service = {}
        for r in ri_recs:
            svc = r["service"]
            if svc not in by_service:
                by_service[svc] = []
            by_service[svc].append(r)

        for svc, items in sorted(by_service.items(), key=lambda x: -sum(i["estimated_monthly_savings"] for i in x[1])):
            service_total = sum(i["estimated_monthly_savings"] for i in items)
            rows = ""
            for r in items[:5]:  # top 5 por serviço
                term_label = "1y" if r["term"] == "ONE_YEAR" else "3y"
                payment_label = {"NO_UPFRONT": "Sem entrada", "PARTIAL_UPFRONT": "Entrada parcial", "ALL_UPFRONT": "Entrada total"}.get(r["payment"], r["payment"])
                rows += f"""
                <tr>
                  <td><strong>{r['instance_info']}</strong></td>
                  <td style="text-align:center">{r['qty_recommended']}</td>
                  <td style="text-align:center">{r['avg_used_per_hour']:.1f}</td>
                  <td style="text-align:center">{r['avg_utilization']:.0f}%</td>
                  <td>{term_label} {payment_label}</td>
                  <td style="color:#ffd93d">${r['monthly_on_demand']:,.2f}</td>
                  <td style="color:#00e68a;font-weight:700">${r['estimated_monthly_savings']:,.2f}</td>
                  <td>{r['savings_pct']:.0f}%</td>
                  <td>{r['estimated_break_even_months']:.1f}m</td>
                  <td>${r['upfront_cost']:,.2f}</td>
                </tr>"""
            ri_purchase_cards += f"""
            <div class="purchase-card ri-card">
              <div class="purchase-card-header">
                <div>
                  <strong style="font-size:1.1em;color:#b388ff">📦 Reserved Instances - {svc}</strong>
                  <span class="purchase-tag">{len(items)} recomendação(ões)</span>
                </div>
                <div style="text-align:right">
                  <div style="font-size:1.3em;font-weight:700;color:#00e68a">${service_total:,.2f}/mês</div>
                  <div style="font-size:.82em;color:#8b949e">${service_total*12:,.2f}/ano</div>
                </div>
              </div>
              <div class="table-scroll">
                <table style="font-size:.88em">
                  <thead><tr><th>Instância</th><th>Qtd</th><th>Uso médio/h</th><th>Util.%</th><th>Termo/Pgto</th><th>On-Demand atual</th><th>Economia/mês</th><th>%</th><th>Payback</th><th>Upfront</th></tr></thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
              <div class="purchase-action">
                📋 <strong>Como comprar:</strong> AWS Console → Cost Explorer → Reservations → Recommendations → filtre por <code>{svc}</code> → revise as instâncias acima → Add to cart
              </div>
            </div>"""
    else:
        ri_purchase_cards = '<p style="color:#8b949e;text-align:center;padding:25px">⭕ Nenhuma recomendação de Reserved Instance disponível. Pode ser que você já tenha boa cobertura via RIs/SPs, ou que o uso seja muito variável para justificar reservas.</p>'

    purchase_total = total_sp_purchase_savings + total_ri_purchase_savings

    rec_cards_html = ""
    for r in all_recs:
        prio_color = {"ALTA": "#f85149", "MÉDIA": "#ffa657", "BAIXA": "#ffd93d"}[r["priority"]]
        badge = '<span class="detected-badge">🔍 DETECTADO NA SUA CONTA</span>' if r.get("is_detected") else '<span class="bp-badge">💡 BOA PRÁTICA</span>'
        rec_cards_html += f"""
        <div class="rec-card" style="border-left:4px solid {prio_color}">
          <div class="rec-header">
            <div><span class="prio-badge" style="background:{prio_color}">{r['priority']}</span>{badge}<strong style="margin-left:10px;font-size:1.1em">{r['type']}</strong><span style="color:#8b949e;margin-left:8px">({r['service']})</span></div>
          </div>
          <p class="rec-desc">{r['description']}</p>
          <div class="rec-cost-box">{r['cost_detail']}</div>
          <div class="rec-meta">
            <div><strong>📋 Ação:</strong> {r['action']}</div>
            <div><strong>⏱️ Esforço:</strong> {r['effort']}</div>
          </div>
        </div>"""

    # -------- Roadmap --------
    quick_wins = ["Deletar volumes EBS órfãos","Liberar Elastic IPs não associados","Limpar snapshots EBS >90 dias","Habilitar Cost Anomaly Detection","Habilitar Compute Optimizer"]
    medium_term = ["Implementar tag policies obrigatórias","Ajustar Reserved Instances subutilizadas","Lifecycle policies em S3","Retention em CloudWatch Logs","Avaliar migração Graviton"]
    long_term = ["Migrar workloads batch para Spot","Comprar Savings Plans baseado em histórico","Reestruturar para Serverless onde aplicável","Cultura FinOps (treinamento + dashboards por time)","Negociar Enterprise Discount Program"]
    render_list = lambda items: "".join(f'<li>{i}</li>' for i in items)

    # -------- Untagged list (expansível) --------
    required_tags_list = findings.get("required_tags", ["Environment", "Project", "Owner"])
    required_tags_str = ", ".join(required_tags_list)
    untagged_html = ""
    if untagged:
        visible_u = ""
        for u in untagged[:15]:
            missing = ", ".join(u.get("missing_tags", [])) or "—"
            visible_u += f"<tr><td>{u['type']}</td><td><code>{u['id']}</code></td><td>{u.get('name','N/A')}</td><td style='color:#f85149'>{missing}</td></tr>"
        untagged_html = visible_u
        if len(untagged) > 15:
            extra_u = ""
            for u in untagged[15:]:
                missing = ", ".join(u.get("missing_tags", [])) or "—"
                extra_u += f"<tr><td>{u['type']}</td><td><code>{u['id']}</code></td><td>{u.get('name','N/A')}</td><td style='color:#f85149'>{missing}</td></tr>"
            untagged_extra_html = f"""
            <details class="expandable">
              <summary>🔽 Mostrar +{len(untagged)-15} recurso(s) sem tags</summary>
              <table style="margin-top:10px"><thead><tr><th>Tipo</th><th>ID</th><th>Nome</th><th>Tags Faltando</th></tr></thead><tbody>{extra_u}</tbody></table>
            </details>"""
        else:
            untagged_extra_html = ""
    else:
        untagged_html = '<tr><td colspan="4" style="text-align:center;color:#00e68a;padding:20px">✅ Todos os recursos estão tagueados!</td></tr>'
        untagged_extra_html = ""

    # ============================================================
    # HTML COMPLETO
    # ============================================================
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWS FinOps Report - IAGenix Cloud</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background:#0a0e1a;color:#c9d1d9;padding:20px;line-height:1.5}}
.container{{max-width:1600px;margin:0 auto}}
.header{{text-align:center;margin-bottom:30px;padding:30px;background:linear-gradient(135deg,#111827 0%,#1a2332 100%);border-radius:15px;border:1px solid #1e2a3a}}
h1{{color:#00e68a;margin-bottom:10px;font-size:2.2em;text-shadow:0 0 20px rgba(0,230,138,.5)}}
.subtitle{{color:#8b949e;margin-bottom:10px;font-size:1.15em}}
.account-info{{color:#b388ff;font-size:1em;margin-bottom:10px}}
.health-banner{{display:inline-block;padding:10px 25px;border-radius:25px;font-weight:700;font-size:1.1em;margin-top:15px}}

.summary-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:25px}}
.card{{background:#111827;border-radius:12px;padding:20px;border:1px solid #1e2a3a;text-align:center;transition:all .3s}}
.card:hover{{transform:translateY(-3px);box-shadow:0 10px 30px rgba(0,230,138,.15)}}
.card h3{{color:#8b949e;font-size:.85em;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px}}
.card .value{{font-size:1.9em;font-weight:700;color:#00e68a}}
.card .value.red{{color:#f85149}}
.card .value.green{{color:#00e68a}}
.card .value.blue{{color:#58a6ff}}
.card .value.orange{{color:#ffa657}}

.section-title{{text-align:center;margin:45px 0 15px;font-size:1.6em;font-weight:700;color:#00e68a;text-shadow:0 0 15px rgba(0,230,138,.4)}}
.section-subtitle{{text-align:center;color:#8b949e;font-size:.95em;margin-bottom:25px;max-width:900px;margin-left:auto;margin-right:auto}}

.executive-summary{{background:#111827;border-radius:15px;padding:25px;border:1px solid #1e2a3a;margin-bottom:25px}}
.executive-summary h2{{color:#00e68a;margin-bottom:20px;font-size:1.3em}}
.cost-breakdown{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:15px}}
.period-costs{{background:#1a2332;padding:18px;border-radius:10px}}
.period-costs h3{{color:#b388ff;margin-bottom:12px;font-size:1.05em}}
.cost-line{{display:flex;justify-content:space-between;padding:8px 0;color:#c9d1d9;font-size:.95em}}
.cost-line-total{{border-top:2px solid #1e2a3a;padding-top:12px;margin-top:8px}}
.cost-note{{color:#8b949e;font-size:.82em;margin-top:15px;line-height:1.7}}

/* Produtos grid */
.products-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:20px;margin-top:15px}}
.product-card{{background:rgba(255,255,255,.04);border-radius:12px;padding:22px;border:1px solid rgba(0,230,138,.2);transition:all .3s}}
.product-card:hover{{border-color:rgba(0,230,138,.5);transform:translateY(-2px)}}
.product-card.waste-card{{border-color:rgba(248,81,73,.3);background:rgba(248,81,73,.03)}}
.product-card.sp-card{{border-color:rgba(179,136,255,.3);background:rgba(179,136,255,.03)}}
.product-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;flex-wrap:wrap;gap:8px}}
.product-name{{color:#00e68a;font-size:1.1em;font-weight:700}}
.product-total{{color:#ffd93d;font-size:1.2em;font-weight:700}}
.percentage{{color:#8b949e;font-size:.8em;margin-left:5px}}
.progress-bar{{width:100%;height:8px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden;margin:10px 0 15px}}
.progress-fill{{height:100%;background:linear-gradient(90deg,#00e68a,#b388ff);border-radius:4px;transition:width .8s ease}}
.services-list{{margin-top:10px}}
.service-item{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:.9em}}
.service-name{{color:#c9d1d9}}
.service-cost{{color:#8b949e}}

/* Top changes */
.top-changes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:20px;margin-bottom:25px}}
.change-list{{background:#111827;padding:20px;border-radius:12px;border:1px solid #1e2a3a}}
.change-list h2{{font-size:1.1em;margin-bottom:15px}}
.change-list.increases h2{{color:#f85149}}
.change-list.decreases h2{{color:#00e68a}}
.change-item{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:.9em;gap:10px}}
.change-item .service{{color:#c9d1d9;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.change-item .amount{{font-weight:700;white-space:nowrap}}
.change-item .amount.increase{{color:#f85149}}
.change-item .amount.decrease{{color:#00e68a}}

/* Charts */
.charts-row{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:25px}}
.chart-container{{background:#111827;border-radius:12px;padding:20px;border:1px solid #1e2a3a}}
.chart-container.full-width{{grid-column:1/-1}}
.chart-container h2{{color:#00e68a;margin-bottom:15px;font-size:1.1em}}
@media (max-width:900px){{.charts-row{{grid-template-columns:1fr}}}}

/* Tables */
.table-scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #1e2a3a;font-size:.9em}}
th{{background:#1a2332;color:#00e68a;font-weight:600;font-size:.85em;text-transform:uppercase;letter-spacing:.5px}}
tr:hover{{background:rgba(26,35,50,.5)}}
.badge{{padding:4px 10px;border-radius:4px;font-size:.75em;font-weight:600;white-space:nowrap}}
.badge.up{{background:rgba(248,81,73,.2);color:#f85149}}
.badge.down{{background:rgba(0,230,138,.2);color:#00e68a}}
.badge.same{{background:rgba(139,148,158,.2);color:#8b949e}}
.increase{{color:#f85149}}
.decrease{{color:#00e68a}}
.var-pill{{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:14px;font-weight:700;font-size:.95em;white-space:nowrap}}
.var-arrow{{font-size:1.1em;line-height:1}}
.big-arrow{{display:inline-block;font-size:1.2em;margin-right:6px;vertical-align:middle}}
.change-item .amount{{font-size:1em;font-weight:700;display:inline-flex;align-items:center}}
.neutral{{color:#8b949e}}

/* Savings Plans */
.sp-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px}}
.sp-metric{{background:#1a2332;padding:15px;border-radius:10px;text-align:center}}
.sp-label{{color:#8b949e;font-size:.82em;margin-bottom:5px}}
.sp-value{{font-size:1.3em;font-weight:700}}
.sp-history-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:15px;margin-top:15px}}
.sp-month-card{{background:#1a2332;border-radius:10px;padding:18px;border-left:4px solid #b388ff}}
.sp-month-card h3{{color:#b388ff;margin-bottom:12px;font-size:1em}}
.sp-month-header{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
.sp-month-header h3{{margin-bottom:0}}
.origin-badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.72em;font-weight:700;text-transform:uppercase;letter-spacing:.3px}}
.origin-badge.local{{background:rgba(0,230,138,0.18);color:#00e68a;border:1px solid rgba(0,230,138,0.4)}}
.origin-badge.external{{background:rgba(255,166,87,0.18);color:#ffa657;border:1px solid rgba(255,166,87,0.4)}}
.sp-month-card.sp-total{{border-left-color:#00e68a;background:rgba(0,230,138,.05);grid-column:1/-1}}
.sp-month-card.sp-total h3{{color:#00e68a}}

/* Idle resources */
.idle-group{{background:#1a2332;border-radius:10px;padding:20px;margin-bottom:15px;border-left:4px solid #ffa657}}
.idle-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;padding-bottom:10px;border-bottom:1px solid #1e2a3a}}
code{{background:#0a0e1a;padding:4px 10px;border-radius:5px;font-size:0.9em;color:#ffd93d;font-weight:600;font-family:'JetBrains Mono','Fira Code','SF Mono',Monaco,Menlo,Consolas,monospace;letter-spacing:.3px}}

/* Recommendations */
.rec-card{{background:#1a2332;padding:20px;border-radius:10px;margin-bottom:15px;transition:all .3s}}
.purchase-card{{background:#1a2332;padding:22px;border-radius:12px;margin-bottom:18px;border:1px solid #1e2a3a;transition:all .3s}}
.purchase-card.sp-card{{border-left:4px solid #00e68a}}
.purchase-card.ri-card{{border-left:4px solid #b388ff}}
.purchase-card:hover{{transform:translateX(5px);box-shadow:0 5px 20px rgba(0,230,138,0.1)}}
.purchase-card-header{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:15px;padding-bottom:12px;border-bottom:1px solid #1e2a3a}}
.purchase-tag{{display:inline-block;background:rgba(179,136,255,0.15);color:#b388ff;padding:3px 10px;border-radius:12px;font-size:.78em;margin-left:6px;font-weight:600}}
.purchase-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:12px 0;font-size:.9em;color:#c9d1d9}}
.purchase-grid div{{padding:6px 0}}
.purchase-action{{margin-top:15px;padding:12px 15px;background:rgba(0,230,138,0.06);border-left:3px solid #00e68a;border-radius:6px;font-size:.88em;color:#c9d1d9;line-height:1.6}}
.purchase-action code{{background:#0a0e1a;padding:2px 7px;border-radius:3px;color:#ffd93d;font-size:0.9em;font-weight:600;font-family:'JetBrains Mono','Fira Code','SF Mono',Monaco,Menlo,Consolas,monospace;letter-spacing:.3px}}
.sp-context-box{{background:linear-gradient(135deg,rgba(0,230,138,0.06) 0%,rgba(179,136,255,0.04) 100%);border:1px solid rgba(0,230,138,0.25);border-radius:12px;padding:22px;margin-bottom:20px}}
.sp-context-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:18px}}
.sp-context-metric{{background:#1a2332;padding:14px 16px;border-radius:8px;border-left:3px solid #00e68a}}
.sp-context-label{{color:#8b949e;font-size:.78em;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px}}
.sp-context-value{{font-size:1.4em;font-weight:700;color:#00e68a;margin-bottom:3px}}
.sp-context-sub{{color:#8b949e;font-size:.78em}}
.sp-interpretation{{background:rgba(255,217,61,0.05);border-left:3px solid #ffd93d;padding:14px 16px;border-radius:6px;font-size:.92em;line-height:1.7;color:#c9d1d9}}
.sp-interpretation code{{background:#0a0e1a;padding:1px 6px;border-radius:3px;color:#ffd93d;font-size:0.9em;font-weight:600;font-family:'JetBrains Mono','Fira Code','SF Mono',Monaco,Menlo,Consolas,monospace;letter-spacing:.3px}}
.sp-decision-guide{{background:rgba(179,136,255,0.06);border:1px solid rgba(179,136,255,0.25);border-radius:10px;padding:18px;margin:18px 0}}
.sp-decision-guide code{{background:#0a0e1a;padding:1px 6px;border-radius:3px;color:#ffd93d;font-size:0.9em;font-weight:600;font-family:'JetBrains Mono','Fira Code','SF Mono',Monaco,Menlo,Consolas,monospace;letter-spacing:.3px}}
.best-tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7em;font-weight:700;margin-left:8px}}
.rec-card:hover{{transform:translateX(5px);background:#1f2937}}
.rec-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:10px}}
.prio-badge{{padding:4px 10px;border-radius:4px;font-size:.75em;font-weight:700;color:white}}
.detected-badge{{padding:4px 10px;border-radius:4px;font-size:.75em;font-weight:700;background:rgba(248,81,73,.2);color:#f85149;margin-left:8px}}
.bp-badge{{padding:4px 10px;border-radius:4px;font-size:.75em;font-weight:700;background:rgba(179,136,255,.2);color:#b388ff;margin-left:8px}}
.rec-desc{{color:#c9d1d9;margin:10px 0;line-height:1.6}}
.rec-cost-box{{background:rgba(255,217,61,0.08);border-left:3px solid #ffd93d;padding:12px 15px;margin:12px 0;border-radius:6px;font-size:.92em;line-height:1.7}}
.rec-cost-box code{{background:#0a0e1a;padding:2px 6px;border-radius:3px;font-size:0.9em;color:#ffd93d;font-weight:600;font-family:'JetBrains Mono','Fira Code','SF Mono',Monaco,Menlo,Consolas,monospace;letter-spacing:.3px}}
.rec-meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid #1e2a3a;font-size:.88em;color:#8b949e}}
details.expandable{{margin-top:12px;background:#0f1624;border-radius:8px;border:1px solid #1e2a3a;overflow:hidden}}
details.expandable summary{{cursor:pointer;padding:12px 16px;color:#58a6ff;font-weight:600;user-select:none;transition:background .2s;list-style:none}}
details.expandable summary::-webkit-details-marker{{display:none}}
details.expandable summary:hover{{background:#1a2332;color:#00e68a}}
details.expandable[open] summary{{border-bottom:1px solid #1e2a3a;background:#1a2332}}
details.expandable table{{margin:0;padding:0 15px 15px 15px}}

/* Roadmap */
.roadmap{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-top:20px}}
.roadmap-col{{background:#1a2332;border-radius:12px;padding:20px;border-top:4px solid #00e68a}}
.roadmap-col.medium{{border-top-color:#ffa657}}
.roadmap-col.long{{border-top-color:#b388ff}}
.roadmap-col h3{{color:#00e68a;margin-bottom:15px;font-size:1.1em}}
.roadmap-col.medium h3{{color:#ffa657}}
.roadmap-col.long h3{{color:#b388ff}}
.roadmap-col ul{{list-style:none;padding:0}}
.roadmap-col li{{padding:10px 0;border-bottom:1px solid #1e2a3a;padding-left:25px;position:relative;color:#c9d1d9;font-size:.9em}}
.roadmap-col li:before{{content:'▸';position:absolute;left:5px;color:#00e68a;font-weight:700}}
.roadmap-col.medium li:before{{color:#ffa657}}
.roadmap-col.long li:before{{color:#b388ff}}

.edu-box{{background:linear-gradient(135deg,rgba(179,136,255,.1) 0%,rgba(0,230,138,.05) 100%);border:1px solid rgba(179,136,255,.3);border-radius:10px;padding:20px;margin:15px 0}}
.edu-box strong{{color:#b388ff}}
.footer{{text-align:center;margin-top:50px;padding:30px;color:#8b949e;font-size:.85em;border-top:1px solid #1e2a3a}}
</style>
</head>
<body>
<div class="container">

<!-- HEADER -->
<div class="header">
  <h1>📊 AWS FinOps Report - IAGenix Cloud</h1>
  <p class="subtitle">Análise Completa de Custos, Reservas & Oportunidades de Otimização</p>
  <p class="account-info">📍 Conta: {findings['account_id']} | Região: {findings['region']} (São Paulo) | 🗓️ {findings['generated_at']}</p>
  <p class="subtitle" style="font-size:1em">Comparativo entre os 2 últimos meses fechados: <strong>{previous_period}</strong> vs <strong>{current_period}</strong></p>
  <div class="health-banner" style="background:rgba({int(health_color[1:3],16)},{int(health_color[3:5],16)},{int(health_color[5:7],16)},0.2);color:{health_color};border:2px solid {health_color}">
    {health_emoji} Saúde da Conta: {health_status} | Economia potencial: ${savings:,.2f}/mês ({pct_savings:.1f}%)
  </div>
</div>

<!-- LINHA 1: Comparativo entre meses fechados -->
<div class="summary-cards">
  <div class="card"><h3>📅 Mês Anterior (fechado)</h3><div class="value blue">${total_previous:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">{previous_period}</div></div>
  <div class="card"><h3>📅 Último Mês Fechado</h3><div class="value blue">${total_current:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">{current_period}</div></div>
  <div class="card"><h3>📈 Variação Absoluta</h3><div class="value" style="color:{var_color}">{var_sign}${total_variation:,.2f}</div></div>
  <div class="card"><h3>📊 Variação %</h3><div class="value" style="color:{var_color}">{var_sign}{total_variation_pct:.2f}%</div></div>
</div>

<!-- LINHA 1.5: Mês corrente parcial + forecast (informativo) -->
<div class="summary-cards">
  <div class="card" style="border:1px dashed #58a6ff"><h3>🟦 Mês Corrente (parcial)</h3><div class="value" style="color:#58a6ff">${total_partial:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">{partial_period}</div></div>
  <div class="card" style="border:1px dashed #b388ff"><h3>🔮 Forecast Mês Corrente</h3><div class="value" style="color:#b388ff">${forecast_current_month:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">Extrapolação linear do parcial</div></div>
  <div class="card" style="border:1px dashed #ffa657"><h3>📊 Forecast vs Último Fechado</h3><div class="value" style="color:#ffa657">{(((forecast_current_month - total_current) / total_current * 100) if total_current else 0):+.1f}%</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">Tendência do mês corrente</div></div>
  <div class="card"><h3>💸 Economia Anual Projetada</h3><div class="value green">${annual_savings:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">Baseado em ${savings:,.2f}/mês</div></div>
</div>

<!-- LINHA 2: Savings Plans + Quick stats -->
<div class="summary-cards">
  <div class="card"><h3>💳 Savings Plans (mês)</h3><div class="value orange">${sp_monthly_total:,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">Economia líquida</div></div>
  <div class="card" style="background:rgba(0,230,138,.08);border-color:rgba(0,230,138,.4)"><h3>💰 Economia SP Acumulada</h3><div class="value green">${sp_history.get('total_savings',0):,.2f}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">{len(sp_months)} meses — {sp_history.get('total_savings_pct',0):.1f}% vs On-Demand</div></div>
  <div class="card"><h3>📦 Reservas Ativas</h3><div class="value">{total_ris}</div></div>
  <div class="card"><h3>🚨 Recursos Ociosos</h3><div class="value red">{len(idle)}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">${total_idle_cost:,.2f}/mês</div></div>
  <div class="card"><h3>💡 Recomendações</h3><div class="value">{len(all_recs)}</div><div style="font-size:.82em;color:#8b949e;margin-top:4px">{len(recs)} críticas + {len(best_practices)} práticas</div></div>
</div>

<!-- COMPARATIVO TOP AUMENTOS/REDUÇÕES -->
<h2 class="section-title">📊 Análise Comparativa Mês a Mês</h2>
<p class="section-subtitle">Serviços com maior variação de custo entre {previous_month_label} e {current_month_label}.</p>
<div class="top-changes">
  <div class="change-list increases"><h2>🔺 Top Maiores Aumentos</h2><div id="topIncreases"></div></div>
  <div class="change-list decreases"><h2>🔻 Top Maiores Reduções</h2><div id="topDecreases"></div></div>
</div>

<!-- GRÁFICOS -->
<div class="chart-container full-width" style="margin-bottom:20px"><h2>📈 Tendência de Custo - Últimos 6 Meses</h2><p style="color:#8b949e;font-size:.85em;margin-bottom:10px">Trajetória total do gasto mensal na região. Linha plana ou descendente é saudável; subida consistente exige investigação.</p><canvas id="trendChart" height="80"></canvas></div>

<div class="charts-row">
  <div class="chart-container"><h2>📊 Top 10 - {previous_month_label}</h2><p style="color:#8b949e;font-size:.8em;margin-bottom:8px">{previous_period}</p><canvas id="chart1"></canvas></div>
  <div class="chart-container"><h2>📊 Top 10 - {current_month_label}</h2><p style="color:#8b949e;font-size:.8em;margin-bottom:8px">{current_period}</p><canvas id="chart2"></canvas></div>
</div>
<div class="chart-container full-width" style="margin-bottom:25px"><h2>📈 Comparativo Mensal: {previous_month_label} vs {current_month_label} (Top 15 serviços)</h2><canvas id="comparisonChart"></canvas></div>

<!-- TABELA DETALHADA -->
<div class="chart-container full-width" style="margin-bottom:25px">
  <h2>📋 Tabela Detalhada por Serviço</h2>
  <p style="color:#8b949e;font-size:.85em;margin-bottom:10px">Inclui média dos últimos 12 meses para identificar tendências fora do padrão histórico.</p>
  <div class="table-scroll">
    <table>
      <thead><tr><th>Serviço</th><th>Média 12m</th><th>{previous_month_label}</th><th>{current_month_label}</th><th>Variação ($)</th><th>Variação (%)</th><th>vs Média</th><th>Status</th></tr></thead>
      <tbody id="tableBody"></tbody>
    </table>
  </div>
</div>

<!-- EXECUTIVE SUMMARY -->
<div class="executive-summary">
  <h2>📋 Executive Summary - Composição de Custos</h2>
  <div class="cost-breakdown">
    <div class="period-costs">
      <h3>{previous_month_label} (fechado)</h3>
      <div class="cost-line"><span>Período:</span><span>{previous_period}</span></div>
      <div class="cost-line"><span>Custo Total:</span><span><strong>${total_previous:,.2f}</strong></span></div>
    </div>
    <div class="period-costs">
      <h3>{current_month_label} (fechado)</h3>
      <div class="cost-line"><span>Período:</span><span>{current_period}</span></div>
      <div class="cost-line"><span>Custo Total:</span><span><strong>${total_current:,.2f}</strong></span></div>
    </div>
  </div>
  <div class="cost-note">
    * <strong>Região analisada:</strong> {findings['region']} (São Paulo)<br>
    * <strong>Fonte:</strong> AWS Cost Explorer — UnblendedCost<br>
    * <strong>Variação:</strong> <span style="color:{var_color}">{var_sign}${total_variation:,.2f} ({var_sign}{total_variation_pct:.2f}%)</span><br>
    * <strong>⚠️ Dados estimados</strong> — a AWS pode fazer ajustes retroativos até o fechamento do mês.
  </div>
</div>

<!-- EXTRATO SAVINGS PLANS -->
<div class="executive-summary">
  <h2>💳 Extrato Detalhado - Savings Plans (últimos 6 meses)</h2>
  <p style="color:#8b949e;margin-bottom:15px;font-size:.9em">Comparativo mês a mês entre o custo on-demand coberto pelo SP e o custo efetivamente pago. Mostra a economia real vs cenário sem reservas.</p>
  <div class="sp-history-grid">{sp_history_html}</div>
</div>

<!-- INVENTÁRIO DE RESERVAS -->
<h2 class="section-title">📦 Inventário de Recursos Reservados</h2>
<p class="section-subtitle">Reservas ativas detectadas por serviço. Amazon OpenSearch incluído (cobertura especial para este serviço).</p>
<div class="summary-cards">{ri_inv_html}</div>

<!-- RESERVED INSTANCES - UTILIZAÇÃO AGREGADA POR SERVIÇO -->
<h2 class="section-title">📊 Reserved Instances - Utilização Agregada</h2>
<p class="section-subtitle">Utilização <strong>somada por serviço</strong> (todas as RIs do serviço como um único pool). Vem da API <code>get_reservation_utilization</code>. Abaixo de 80% indica desperdício.</p>
<div class="chart-container">{ri_util_summary_html}</div>

<!-- RESERVED INSTANCES - DETALHAMENTO POR RI INDIVIDUAL -->
<h2 class="section-title">📦 Reserved Instances - Detalhamento por RI</h2>
<p class="section-subtitle">Cada RI ativa individualmente, com sua data de expiração e ação recomendada. Ordenado por urgência (mais próximas de expirar primeiro).</p>
<div class="edu-box">
  <strong>💡 Como ler esta tabela:</strong> Coluna <strong>Restante</strong> em vermelho = expira em &lt;30 dias (renovar urgente). A coluna <strong>Ação</strong> combina expiração + utilização agregada do serviço (mostrada na tabela acima).
</div>
<div class="chart-container">{ri_consolidated_html}</div>

<!-- SAVINGS PLANS CONSOLIDADO -->
<h2 class="section-title">💳 Savings Plans - Utilização & Expiração</h2>
<p class="section-subtitle">Métricas de uso do compromisso atual + lista detalhada de cada Savings Plan ativo com data de término.</p>
<div class="chart-container">{sp_consolidated_html}</div>

<!-- RECURSOS OCIOSOS -->
<h2 class="section-title">🚨 Recursos Ociosos Detectados</h2>
<p class="section-subtitle"><strong>Quick wins</strong> — recursos provisionados sem uso que podem ser deletados após validação.</p>
{idle_html}

<!-- GOVERNANÇA -->
<h2 class="section-title">🏷️ Governança & Tags</h2>
<p class="section-subtitle">Recursos sem tags impedem alocação correta de custos por time/projeto.</p>
<div class="chart-container">
  <p style="margin-bottom:10px">🔍 <strong>Tags obrigatórias verificadas:</strong> <code style="background:#0a0e1a;padding:4px 10px;border-radius:4px;color:#58a6ff">{required_tags_str}</code></p>
  <p style="margin-bottom:15px">EC2s com pelo menos uma tag obrigatória faltando: <strong style="color:{'#00e68a' if len(untagged)==0 else '#f85149'};font-size:1.2em">{len(untagged)}</strong></p>
  <div class="table-scroll"><table><thead><tr><th>Tipo</th><th>ID</th><th>Nome</th><th>Tags Faltando</th></tr></thead><tbody>{untagged_html}</tbody></table></div>
  {untagged_extra_html}
</div>

<!-- RECOMENDAÇÕES DE COMPRA (SP/RI) - vindas direto do AWS Cost Explorer -->
<h2 class="section-title">🛒 Recomendações de Compra - Savings Plans & Reserved Instances</h2>
<p class="section-subtitle">Análise das suas instâncias mais usadas (lookback de 60 dias) gerada pelo motor de ML do AWS Cost Explorer. Mostra exatamente <strong>o que comprar</strong>, <strong>quanto</strong>, e <strong>quanto você economizaria</strong>.</p>
<div class="edu-box">
  <strong>💡 Como funciona:</strong> A AWS analisa seu padrão de uso das últimas 8 semanas e recomenda compras baseadas em workloads <em>estáveis</em> que valem ser reservados. Compare termos (1y vs 3y) e modalidades de pagamento (Sem entrada / Parcial / Total) — quanto maior o compromisso, maior o desconto.
</div>

<div class="summary-cards" style="margin-bottom:25px">
  <div class="card" style="background:rgba(0,230,138,0.08);border-color:rgba(0,230,138,0.4)">
    <h3>💰 Economia Total Recomendada</h3>
    <div class="value green">${purchase_total:,.2f}/mês</div>
    <div style="font-size:.82em;color:#8b949e;margin-top:4px">${purchase_total*12:,.2f}/ano se aplicar tudo</div>
  </div>
  <div class="card">
    <h3>💳 Savings Plans</h3>
    <div class="value" style="color:#00e68a">${total_sp_purchase_savings:,.2f}/mês</div>
    <div style="font-size:.82em;color:#8b949e;margin-top:4px">{len(sp_recs)} combinação(ões) analisada(s)</div>
  </div>
  <div class="card">
    <h3>📦 Reserved Instances</h3>
    <div class="value" style="color:#b388ff">${total_ri_purchase_savings:,.2f}/mês</div>
    <div style="font-size:.82em;color:#8b949e;margin-top:4px">{len(ri_recs)} recomendação(ões) por serviço</div>
  </div>
</div>

<h3 style="color:#00e68a;margin:25px 0 15px;font-size:1.2em">💳 Savings Plans Recomendados</h3>
{sp_purchase_cards}

<h3 style="color:#b388ff;margin:35px 0 15px;font-size:1.2em">📦 Reserved Instances Recomendadas (por serviço)</h3>
{ri_purchase_cards}

<!-- RECOMENDAÇÕES -->
<h2 class="section-title">💡 Recomendações Detalhadas de FinOps</h2>
<p class="section-subtitle">Priorizadas: <span style="color:#f85149">ALTA</span> (hoje) → <span style="color:#ffa657">MÉDIA</span> (este mês) → <span style="color:#ffd93d">BAIXA</span> (próximo trimestre). Combina itens <strong>detectados</strong> na sua conta + <strong>boas práticas</strong> universais.</p>
{rec_cards_html}

<!-- ROADMAP -->
<h2 class="section-title">🗺️ Plano de Ação - Roadmap FinOps</h2>
<div class="roadmap">
  <div class="roadmap-col"><h3>⚡ Quick Wins (esta semana)</h3><ul>{render_list(quick_wins)}</ul></div>
  <div class="roadmap-col medium"><h3>📅 Médio Prazo (este mês)</h3><ul>{render_list(medium_term)}</ul></div>
  <div class="roadmap-col long"><h3>🎯 Longo Prazo (próx. trimestre)</h3><ul>{render_list(long_term)}</ul></div>
</div>

<!-- PRINCÍPIOS -->
<h2 class="section-title">📚 Princípios FinOps</h2>
<div class="edu-box">
  <p><strong>1. Visibility:</strong> Você não pode otimizar o que não enxerga. Tags + Cost Explorer + dashboards por time são fundamentais.</p><br>
  <p><strong>2. Optimization:</strong> Rightsizing → Reservations/Savings Plans → eliminar desperdício. Nessa ordem.</p><br>
  <p><strong>3. Operation:</strong> FinOps é cultura contínua, não projeto. Revisões mensais, automações de cleanup, accountability dos times.</p><br>
  <p><strong>🎯 Regra de Ouro:</strong> Antes de comprar reservas (RI/SP), faça rightsizing. Não adianta reservar uma m5.4xlarge quando uma m5.large basta.</p>
</div>

<div class="footer">
  <p><strong style="color:#00e68a">📊 IAGenix FinOps Simulator</strong> — Otimização contínua de custos em nuvem</p>
  <p style="margin-top:5px">Fontes: Cost Explorer, EC2, RDS, OpenSearch, ElastiCache, Redshift, Savings Plans, CloudWatch</p>
  <p style="margin-top:10px">Região: {findings['region']} | Conta: {findings['account_id']} | Gerado: {findings['generated_at']}</p>
</div>

</div>

<script>
// Dados
const servicesDetail = {services_json};
const productsList = {products_json};
const trendHistory = {trend_json};
const colors = ['#00e68a','#b388ff','#f85149','#ffa657','#58a6ff','#8b949e','#7ee787','#ff7b72','#d2a8ff','#ffd93d','#56d4dd','#ec8e2c','#ff9bce','#a5d6ff','#79c0ff'];

// Gráfico de tendência 6 meses
if (trendHistory.length > 0) {{
  new Chart(document.getElementById('trendChart'), {{
    type: 'line',
    data: {{
      labels: trendHistory.map(t => t.month),
      datasets: [{{
        label: 'Custo Mensal (USD)',
        data: trendHistory.map(t => t.cost),
        borderColor: '#00e68a',
        backgroundColor: 'rgba(0, 230, 138, 0.15)',
        borderWidth: 3,
        tension: 0.3,
        fill: true,
        pointRadius: 6,
        pointBackgroundColor: '#00e68a',
        pointBorderColor: '#0a0e1a',
        pointBorderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ labels: {{ color: '#c9d1d9' }} }},
        tooltip: {{ callbacks: {{ label: ctx => '$' + ctx.parsed.y.toLocaleString('en-US', {{minimumFractionDigits:2}}) }} }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#1e2a3a' }} }},
        y: {{ ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }}, grid: {{ color: '#1e2a3a' }} }}
      }}
    }}
  }});
}}

// Top increases / decreases
const increases = servicesDetail.filter(s => s.diff > 0).sort((a,b) => b.diff - a.diff).slice(0, 10);
const decreases = servicesDetail.filter(s => s.diff < 0).sort((a,b) => a.diff - b.diff).slice(0, 10);

document.getElementById('topIncreases').innerHTML = increases.length ? increases.map(i => {{
  const diffFmt = `$${{Math.abs(i.diff).toLocaleString('en-US',{{minimumFractionDigits:2}})}}`;
  return `<div class="change-item"><span class="service">${{i.name}}</span><span class="amount increase"><span class="big-arrow">▲</span>+${{diffFmt}} <span style="opacity:.85">(${{i.pct_change.toFixed(1)}}%)</span></span></div>`;
}}).join('') : '<p style="color:#8b949e;text-align:center;padding:15px">Nenhum aumento detectado</p>';

document.getElementById('topDecreases').innerHTML = decreases.length ? decreases.map(i => {{
  const diffFmt = `$${{Math.abs(i.diff).toLocaleString('en-US',{{minimumFractionDigits:2}})}}`;
  return `<div class="change-item"><span class="service">${{i.name}}</span><span class="amount decrease"><span class="big-arrow">▼</span>-${{diffFmt}} <span style="opacity:.85">(${{i.pct_change.toFixed(1)}}%)</span></span></div>`;
}}).join('') : '<p style="color:#8b949e;text-align:center;padding:15px">Nenhuma redução detectada</p>';

// Tabela detalhada
const sorted = [...servicesDetail].sort((a,b) => Math.abs(b.diff) - Math.abs(a.diff));
document.getElementById('tableBody').innerHTML = sorted.map(i => {{
  const isUp = i.diff > 0;
  const isDown = i.diff < 0;
  const cls = isUp ? 'up' : isDown ? 'down' : 'same';
  const txt = isUp ? '↑ Aumento' : isDown ? '↓ Redução' : '= Igual';
  // Cores e ícones
  const color = isUp ? '#f85149' : isDown ? '#00e68a' : '#8b949e';
  const bg = isUp ? 'rgba(248,81,73,0.15)' : isDown ? 'rgba(0,230,138,0.15)' : 'rgba(139,148,158,0.15)';
  const arrow = isUp ? '▲' : isDown ? '▼' : '●';
  const sign = i.diff >= 0 ? '+' : '';
  const diffFmt = `$${{Math.abs(i.diff).toLocaleString('en-US',{{minimumFractionDigits:2}})}}`;
  // Pill com seta + valor
  const pillDiff = `<span class="var-pill" style="background:${{bg}};color:${{color}};border:1px solid ${{color}}"><span class="var-arrow">${{arrow}}</span>${{sign}}${{diffFmt}}</span>`;
  const pillPct = `<span class="var-pill" style="background:${{bg}};color:${{color}};border:1px solid ${{color}}"><span class="var-arrow">${{arrow}}</span>${{i.pct_change >= 0 ? '+' : ''}}${{i.pct_change.toFixed(2)}}%</span>`;
  const vsAvgColor = i.vs_annual_pct > 0 ? '#ffa657' : i.vs_annual_pct < 0 ? '#7ee787' : '#8b949e';
  return `<tr>
    <td>${{i.name}}</td>
    <td>$${{i.annual_avg.toLocaleString('en-US',{{minimumFractionDigits:2}})}}</td>
    <td>$${{i.previous.toLocaleString('en-US',{{minimumFractionDigits:2}})}}</td>
    <td>$${{i.current.toLocaleString('en-US',{{minimumFractionDigits:2}})}}</td>
    <td>${{pillDiff}}</td>
    <td>${{pillPct}}</td>
    <td style="color:${{vsAvgColor}};font-weight:600">${{i.vs_annual_pct >= 0 ? '+' : ''}}${{i.vs_annual_pct.toFixed(1)}}%</td>
    <td><span class="badge ${{cls}}">${{txt}}</span></td>
  </tr>`;
}}).join('');

// Doughnut mês anterior
const top10prev = [...servicesDetail].filter(s => s.previous > 0).sort((a,b) => b.previous - a.previous).slice(0, 10);
if (top10prev.length > 0) {{
  new Chart(document.getElementById('chart1'), {{
    type: 'doughnut',
    data: {{ labels: top10prev.map(c => c.name.substring(0,30)), datasets: [{{ data: top10prev.map(c => c.previous), backgroundColor: colors, borderColor: '#0a0e1a', borderWidth: 2 }}] }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'right', labels: {{ color: '#c9d1d9', font: {{ size: 10 }} }} }} }} }}
  }});
}}

// Doughnut mês atual
const top10cur = [...servicesDetail].filter(s => s.current > 0).sort((a,b) => b.current - a.current).slice(0, 10);
if (top10cur.length > 0) {{
  new Chart(document.getElementById('chart2'), {{
    type: 'doughnut',
    data: {{ labels: top10cur.map(c => c.name.substring(0,30)), datasets: [{{ data: top10cur.map(c => c.current), backgroundColor: colors, borderColor: '#0a0e1a', borderWidth: 2 }}] }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'right', labels: {{ color: '#c9d1d9', font: {{ size: 10 }} }} }} }} }}
  }});
}}

// Comparativo barras top 15
const top15 = [...servicesDetail].sort((a,b) => (b.previous + b.current) - (a.previous + a.current)).slice(0, 15);
if (top15.length > 0) {{
  new Chart(document.getElementById('comparisonChart'), {{
    type: 'bar',
    data: {{
      labels: top15.map(c => c.name.substring(0,25)),
      datasets: [
        {{ label: 'Mês Anterior Fechado', data: top15.map(c => c.previous), backgroundColor: '#b388ff', borderRadius: 4 }},
        {{ label: 'Último Mês Fechado', data: top15.map(c => c.current), backgroundColor: '#00e68a', borderRadius: 4 }}
      ]
    }},
    options: {{
      responsive: true,
      scales: {{
        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45, minRotation: 30 }}, grid: {{ color: '#1e2a3a' }} }},
        y: {{ ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }}, grid: {{ color: '#1e2a3a' }} }}
      }},
      plugins: {{ legend: {{ labels: {{ color: '#c9d1d9' }} }} }}
    }}
  }});
}}

// Animação das barras
window.addEventListener('load', () => {{
  document.querySelectorAll('.progress-fill').forEach(bar => {{
    const w = bar.style.width;
    bar.style.width = '0%';
    setTimeout(() => {{ bar.style.width = w; }}, 500);
  }});
}});
</script>

</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Relatório HTML detalhado gerado: {output_path}")


# ============================================================================
# CLI
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="IAGenix AWS FinOps Simulator - Verifica otimizações e uso de reservas"
    )
    parser.add_argument("--region", default=REGION, help="Região AWS (default: sa-east-1)")
    parser.add_argument("--profile", default=None, help="Perfil AWS (opcional)")
    parser.add_argument("--output", default="finops_report.html", help="Arquivo HTML de saída")
    parser.add_argument("--json", default=None, help="Salvar findings em JSON (opcional)")
    parser.add_argument(
        "--tag-keys",
        default=",".join(REQUIRED_TAGS),
        help=f"Tags obrigatórias separadas por vírgula (default: {','.join(REQUIRED_TAGS)}). "
             f"Exemplo: --tag-keys cnj-env,Projeto,cnj-product",
    )
    parser.add_argument(
        "--debug-sp",
        action="store_true",
        help="Imprime breakdown completo por RECORD_TYPE da API Cost Explorer (todos os meses), "
             "para validar manualmente contra o console AWS de onde vem cada valor de SP/RI.",
    )
    args = parser.parse_args()

    tag_keys = [t.strip() for t in args.tag_keys.split(",") if t.strip()]
    sim = FinOpsSimulator(region=args.region, profile=args.profile, tag_keys=tag_keys, debug_sp=args.debug_sp)
    findings = sim.run()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(findings, f, indent=2, default=str, ensure_ascii=False)
        print(f"✅ JSON salvo em: {args.json}")

    generate_html_report(findings, args.output)


if __name__ == "__main__":
    main()