#!/usr/bin/env python3
"""
finops.py — CLI Entry Point
============================

Ponto de entrada de linha de comando do IAGenix FinOps Simulator.

Este arquivo é intencionalmente pequeno: apenas faz parsing dos argumentos
da CLI, instancia o `FinOpsSimulator` e dispara o relatório.

Toda a lógica está no pacote `iagenix_finops/`:

  - `iagenix_finops.simulator` — orquestrador
  - `iagenix_finops.collectors` — extração de dados AWS
  - `iagenix_finops.reporter`   — geração do HTML
  - `iagenix_finops.config`     — constantes e tabela de preços

Uso básico:

    python3 finops.py
    python3 finops.py --region us-east-1 --profile prod
    python3 finops.py --tag-keys cnj-env,Projeto,cnj-product
    python3 finops.py --output relatorio-marco.html --json findings.json
    python3 finops.py --debug-sp
"""

import argparse
import json
import sys

from iagenix_finops import FinOpsSimulator, generate_html_report
from iagenix_finops.config import REGION, REQUIRED_TAGS


def main():
    parser = argparse.ArgumentParser(
        prog="finops",
        description=(
            "IAGenix AWS FinOps Simulator — analisa custos, reservas, "
            "recursos ociosos e gera relatório HTML com recomendações de otimização."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--region",
        default=REGION,
        help="Região AWS para análise",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Perfil AWS configurado em ~/.aws/credentials (opcional)",
    )
    parser.add_argument(
        "--output",
        default="finops_report.html",
        help="Caminho do arquivo HTML de saída",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="Salvar findings completos em JSON neste caminho (opcional)",
    )
    parser.add_argument(
        "--tag-keys",
        default=",".join(REQUIRED_TAGS),
        help=(
            "Tags obrigatórias separadas por vírgula. Exemplo: "
            "--tag-keys cnj-env,Projeto,cnj-product"
        ),
    )
    parser.add_argument(
        "--debug-sp",
        action="store_true",
        help=(
            "Imprime breakdown completo por RECORD_TYPE da API Cost Explorer "
            "para validar manualmente contra o console AWS"
        ),
    )
    args = parser.parse_args()

    # Parse das tags obrigatórias
    tag_keys = [t.strip() for t in args.tag_keys.split(",") if t.strip()]

    # Inicializa o simulador (orquestrador) e roda
    sim = FinOpsSimulator(
        region=args.region,
        profile=args.profile,
        tag_keys=tag_keys,
        debug_sp=args.debug_sp,
    )
    findings = sim.run()

    # Salva findings em JSON se solicitado
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(findings, f, indent=2, default=str, ensure_ascii=False)
        print(f"✅ JSON salvo em: {args.json}")

    # Gera o relatório HTML
    generate_html_report(findings, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
