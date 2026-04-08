"""
iagenix_finops.reporter
=======================

Camada de apresentação — geração do relatório HTML interativo.

A função `generate_html_report(findings, output_path)` recebe o dict de
findings produzido pelo `FinOpsSimulator.run()` e gera um arquivo HTML
completo com:

  - Resumo executivo (cards)
  - Comparativo entre meses fechados + forecast
  - Tendência de 6 meses (Chart.js)
  - Top 10 serviços + análise comparativa
  - Reserved Instances (utilização agregada + detalhamento por RI)
  - Savings Plans (utilização + tabela com expiração)
  - Recursos ociosos agrupados
  - Governança & tags
  - Recomendações de compra (SP/RI via AWS Cost Explorer)
  - Recomendações detalhadas de FinOps + roadmap

Esta camada NÃO chama AWS — opera puramente sobre o dict de findings.
Pode ser testada com mocks e usada offline.
"""

from datetime import datetime, timedelta


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

