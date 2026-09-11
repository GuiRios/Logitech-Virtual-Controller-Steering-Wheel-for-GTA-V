#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
criar-icone.py - desenha o icone da aplicacao (icone.ico).

Fica no repositorio para o icone ser reproduzivel: se quiser outra cor, muda
aqui e roda de novo, em vez de depender de um binario que ninguem sabe de
onde veio.

    python criar-icone.py

Copyright (C) 2026 Gui Rios

Este programa e software livre: voce pode redistribui-lo e/ou modifica-lo sob
os termos da GNU General Public License versao 3, publicada pela Free Software
Foundation. Ele e distribuido SEM NENHUMA GARANTIA. Veja o arquivo LICENSE ou
<https://www.gnu.org/licenses/>.
"""
from __future__ import annotations

import math
import os
import sys

FUNDO = (27, 30, 36, 255)       # mesmo tom das paginas da interface
ARO = (76, 154, 255, 255)       # azul de acento
MIOLO = (76, 154, 255, 255)

AQUI = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(AQUI, "icone.ico")


def desenhar(lado: int):
    from PIL import Image, ImageDraw

    # desenha grande e reduz: as bordas saem suaves sem precisar de antialias
    escala = 8
    g = lado * escala
    img = Image.new("RGBA", (g, g), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # fundo arredondado
    raio = int(g * 0.22)
    d.rounded_rectangle([0, 0, g - 1, g - 1], radius=raio, fill=FUNDO)

    centro = g / 2
    r_externo = g * 0.34
    espessura = g * 0.085

    # aro
    d.ellipse([centro - r_externo, centro - r_externo,
               centro + r_externo, centro + r_externo],
              outline=ARO, width=int(espessura))

    # tres raios: esquerda, direita e baixo (desenho classico de volante)
    r_miolo = g * 0.10
    for angulo in (180, 0, 90):
        rad = math.radians(angulo)
        x1 = centro + math.cos(rad) * r_miolo
        y1 = centro + math.sin(rad) * r_miolo
        x2 = centro + math.cos(rad) * (r_externo - espessura * 0.3)
        y2 = centro + math.sin(rad) * (r_externo - espessura * 0.3)
        d.line([x1, y1, x2, y2], fill=ARO, width=int(espessura * 0.9))

    # miolo
    d.ellipse([centro - r_miolo, centro - r_miolo,
               centro + r_miolo, centro + r_miolo], fill=MIOLO)

    return img.resize((lado, lado), Image.LANCZOS)


def main() -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Este script precisa do Pillow:  pip install pillow")
        print("(so para GERAR o icone; a aplicacao nao depende dele)")
        return 1

    tamanhos = [16, 24, 32, 48, 64, 128, 256]
    # o PIL deriva todos os tamanhos da imagem base, entao ela tem de ser a
    # MAIOR: passando a menor, o .ico sai so com 16x16
    base = desenhar(max(tamanhos))
    base.save(DESTINO, format="ICO", sizes=[(t, t) for t in tamanhos])
    print(f"Pronto: {DESTINO}")
    print(f"  tamanhos: {', '.join(str(t) for t in tamanhos)}")
    print(f"  {os.path.getsize(DESTINO) / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
