#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empacotar.py - monta um .zip limpo do projeto para compartilhar.

Leva so o que a outra pessoa precisa. Fica de fora, de proposito:

  config.json   sua calibragem e seus ajustes pessoais. Se fosse junto, a
                aplicacao diria "calibragem gravada" numa maquina que nunca
                calibrou, e a pessoa iria jogar com numeros de outro volante.
  estado.json   memoria de slot XInput desta maquina.
  *.bak, backups, __pycache__, sdk/   ruido.

    python empacotar.py
"""
from __future__ import annotations

import os
import sys
import zipfile
from datetime import date

AQUI = os.path.dirname(os.path.abspath(__file__))

# Um atalho so. Os .bat numerados (instalar, calibrar, jogar) saiam do pacote
# de proposito: quatro arquivos na pasta confundem quem nao e tecnico, e nao ha
# como saber qual ja foi feito. Tudo aquilo virou a aba "Instalacao".
INCLUIR = [
    "DASHBOARD-GTAV.bat",
    "g29_gui.py",
    "g29_gtav.py",
    "checar-volante.py",
    "criar-icone.py",
    "icone.ico",
    "requirements.txt",
    "README.md",
    "LEIA-PRIMEIRO.txt",
]


def main() -> int:
    faltando = [n for n in INCLUIR if not os.path.exists(os.path.join(AQUI, n))]
    if faltando:
        print("Nao encontrei: " + ", ".join(faltando))
        return 1

    nome = f"g29-gtav-{date.today():%Y-%m-%d}.zip"
    destino = os.path.join(os.path.dirname(AQUI), nome)

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        for arquivo in INCLUIR:
            z.write(os.path.join(AQUI, arquivo), f"g29-gtav/{arquivo}")

    tamanho = os.path.getsize(destino) / 1024
    print(f"Pronto: {destino}  ({tamanho:.0f} KB)")
    print(f"  {len(INCLUIR)} arquivos")
    print("\nO que NAO foi junto (de proposito):")
    for n in sorted(os.listdir(AQUI)):
        if n in INCLUIR or n in ("empacotar.py", "__pycache__"):
            continue
        print(f"  {n}")
    print("\nQuem receber: descompacta e da um duplo clique em DASHBOARD-GTAV.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
