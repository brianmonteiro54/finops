"""
iagenix_finops
==============

Pacote Python para análise FinOps de contas AWS.

Estrutura em 3 camadas (separação de responsabilidades):

  1. **Data extraction** (`collectors.py`)
     Camada de acesso à AWS via boto3. Sem regra de negócio,
     apenas chamadas de API e parsing das respostas.

  2. **Business logic** (`simulator.py`)
     Orquestrador `FinOpsSimulator` que coordena os collectors,
     consolida findings e aplica regras FinOps (priorização,
     classificação de severidade, cálculo de economia potencial).

  3. **Presentation** (`reporter.py`)
     Geração do relatório HTML interativo a partir dos findings.

Uso típico:

    from iagenix_finops.simulator import FinOpsSimulator
    from iagenix_finops.reporter import generate_html_report

    sim = FinOpsSimulator(region="sa-east-1", profile="prod")
    findings = sim.run()
    generate_html_report(findings, "report.html")
"""

__version__ = "1.0.0"
__all__ = ["FinOpsSimulator", "generate_html_report"]

# Re-exports para conveniência
from .simulator import FinOpsSimulator
from .reporter import generate_html_report
