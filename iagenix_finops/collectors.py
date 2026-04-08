"""
iagenix_finops.collectors
=========================

Camada de extração de dados — TODAS as chamadas boto3 para AWS.

Esta camada NÃO tem regra de negócio. Apenas:
  - Faz queries nas APIs AWS (Cost Explorer, EC2, RDS, OpenSearch, etc.)
  - Faz parsing das respostas
  - Popula o `self.findings` dict com dados brutos estruturados

A regra de negócio (priorização, recomendações, classificação) fica
em `simulator.py`. A apresentação visual fica em `reporter.py`.

Esta classe é um **Mixin** projetado para ser herdado por
`FinOpsSimulator`. Ela espera que a classe base já tenha:
  - `self.region`, `self.required_tags`, `self.debug_sp`
  - Clientes boto3: `self.ce`, `self.ec2`, `self.rds`, `self.es`,
    `self.elasticache`, `self.redshift`, `self.savingsplans`,
    `self.cloudwatch`, `self.elbv2`
  - `self.findings` dict
"""

from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from .config import (
    LOOKBACK_DAYS,
    UTILIZATION_THRESHOLD,
    CPU_IDLE_THRESHOLD,
    CPU_IDLE_MAX_THRESHOLD,
    EBS_GB_MONTH_PRICE,
    EBS_SNAPSHOT_GB_MONTH_PRICE,
    EIP_IDLE_HOURLY_PRICE,
    ELB_BASE_MONTHLY_PRICE,
    estimate_ec2_monthly_cost,
)


class FinOpsCollectorMixin:
    """Mixin com todos os métodos de extração de dados AWS para FinOps."""

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
        # Tenta múltiplos states pra capturar SPs em qualquer status (active, queued, etc)
        try:
            all_sp_states = ["payment-pending", "payment-failed", "active", "retired", "queued", "queued-deleted"]
            all_sps_raw = []
            try:
                # Tentativa 1: listar SEM filtro de state (pega todos)
                resp = self.savingsplans.describe_savings_plans()
                all_sps_raw = resp.get("savingsPlans", [])
            except ClientError:
                # Tentativa 2: filtrar só active (caso o sem-filtro dê erro)
                resp = self.savingsplans.describe_savings_plans(states=["active"])
                all_sps_raw = resp.get("savingsPlans", [])

            if self.debug_sp:
                print("\n" + "=" * 70)
                print(" 🔍 DEBUG MODE - describe_savings_plans (TODOS os states)")
                print("=" * 70)
                if not all_sps_raw:
                    print("\n  ⚠️  ZERO Savings Plans retornados pela API.")
                    print("  Possíveis causas:")
                    print("    1) Esta conta realmente não tem SPs próprios (cobertura vem da payer)")
                    print("    2) Permissão savingsplans:DescribeSavingsPlans ausente")
                    print("    3) SP foi comprado em outra Organization/conta consolidada")
                    print("\n  Como confirmar manualmente: Console AWS desta conta →")
                    print("    Cost Management → Savings Plans → Inventory")
                    print("    Se aparecer SPs aí mas não no script = problema de permissão")
                else:
                    print(f"\n  Total SPs retornados: {len(all_sps_raw)}")
                    for sp in all_sps_raw:
                        print(f"\n  ID: {sp.get('savingsPlanId', '?')}")
                        print(f"    State:           {sp.get('state', '?')}")
                        print(f"    Type:            {sp.get('savingsPlanType', '?')}")
                        print(f"    Payment:         {sp.get('paymentOption', '?')}")
                        print(f"    Start:           {sp.get('start', '?')}")
                        print(f"    End:             {sp.get('end', '?')}")
                        print(f"    Commitment/h:    {sp.get('commitment', '?')}")
                        print(f"    Upfront:         {sp.get('upfrontPaymentAmount', '?')}")
                        print(f"    Recurring/h:     {sp.get('recurringPaymentAmount', '?')}")
                print("\n" + "=" * 70 + "\n")

            # Filtra apenas active pra usar no inventory (SPs ativos cobrindo workload agora)
            sp_resp_active = [sp for sp in all_sps_raw if sp.get("state") == "active"]
            sp_details = []
            for sp in sp_resp_active:
                end_str = sp.get("end")  # ISO string
                start_str = sp.get("start")  # ISO string
                end_dt = None
                start_dt = None
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                if start_str:
                    try:
                        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                # Se não temos start_dt mas temos end_dt + termo, calcula
                if start_dt is None and end_dt is not None:
                    term_secs = sp.get("termDurationInSeconds", 0)
                    if term_secs:
                        start_dt = end_dt - timedelta(seconds=term_secs)
                days = _days_until(end_dt)
                sp_details.append({
                    "id": sp.get("savingsPlanId", "?"),
                    "type": sp.get("savingsPlanType", "?"),  # Compute/EC2Instance/SageMaker
                    "payment": sp.get("paymentOption", "?"),
                    "term_years": sp.get("termDurationInSeconds", 0) // (365*24*3600),
                    "commitment_hourly": sp.get("commitment", "?"),
                    "start_date": start_dt.strftime("%Y-%m-%d") if start_dt else "?",
                    "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else "?",
                    "days_remaining": days,
                    "status": _expiry_status(days),
                })
            self.findings["sp_details"] = sp_details
            print(f"      → Savings Plans ativos nesta conta: {len(sp_details)} (de {len(all_sps_raw)} retornados pela API)")
            sp_expiring = [s for s in sp_details if s["days_remaining"] is not None and 0 <= s["days_remaining"] < 30]
            if sp_expiring:
                self.findings["recommendations"].append({
                    "type": "Savings Plan Expirando em Breve",
                    "service": "Compute/EC2/SageMaker",
                    "description": f"{len(sp_expiring)} Savings Plan(s) expira(m) nos próximos 30 dias. Avalie renovação baseada no uso histórico.",
                    "potential_savings": 0,
                })
        except ClientError as e:
            code = e.response['Error']['Code']
            print(f"      ⚠ Savings Plans details: {code}")
            if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
                print(f"         💡 Verifique se o role tem a permissão savingsplans:DescribeSavingsPlans")
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

