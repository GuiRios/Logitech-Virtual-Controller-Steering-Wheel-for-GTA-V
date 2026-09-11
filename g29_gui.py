#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
g29_gui.py - Interface grafica da ponte Logitech G29 -> GTA V.

Usa o g29_gtav.py como motor: a leitura do volante, o controle virtual e o
force feedback sao os mesmos da linha de comando. Aqui so muda o jeito de
manipular - tudo ao vivo, com o volante na mao.

    python g29_gui.py
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from g29_gtav import (  # noqa: E402
    BUTTON_NAMES, CONFIG_PATH, DEFAULT_CONFIG, Gamepad, SdlFFB, VirtualPad,
    Wheel, carregar_estado, clamp, gravar_estado, inicio_do_processo,
    deduzir_repouso, graus_girados, janela_do_jogo, joystick_conectado,
    load_config, map_pedal, map_steering, save_config, xinput_lib, xinput_read,
    xinput_slots,
)

# --------------------------------------------------------------------------
# Paleta escura
# --------------------------------------------------------------------------
# Duas profundidades so: a janela e um tom mais escuro que as paginas, e os
# grupos sao delimitados por borda de 1px em vez de preenchimento diferente.
# Assim um unico estilo de Label serve em todo lugar - com tres tons de fundo
# seria preciso um estilo por combinacao, e qualquer widget novo nasceria com
# a cor errada.

COR_JANELA = "#13151a"
COR_FUNDO = "#1b1e24"      # superficie das paginas e dos grupos
COR_ELEVADO = "#252932"    # botoes, campos
COR_TRILHA = "#2b303a"     # trilho de slider, fundo de barra
COR_BORDA = "#2f3540"
COR_TEXTO = "#e6e8ec"
COR_SUAVE = "#98a0ad"      # textos de apoio
COR_ACENTO = "#4c9aff"
COR_ACENTO_FORTE = "#6fb0ff"

COR_VOLANTE = "#4c9aff"
COR_ACEL = "#3fb950"
COR_FREIO = "#f85149"
COR_EMBREAGEM = "#a371f7"
COR_VIBRA = "#d29922"
COR_LED_ON = "#4c9aff"
COR_LED_OFF = "#2b303a"
COR_OK = "#3fb950"
COR_FALTA = "#f0883e"

FONTE = "Segoe UI"

ACOES_GTA = [
    ("A", "freio de mao"),
    ("Y", "entrar / sair do veiculo"),
    ("B", "olhar para tras"),
    ("X", "acao / interagir"),
    ("LS", "buzina"),
    ("RS", "analogico direito (clique)"),
    ("LB", "LB"),
    ("RB", "RB"),
    ("DPAD_LEFT", "radio anterior"),
    ("DPAD_RIGHT", "proxima radio"),
    ("DPAD_UP", "direcional cima"),
    ("DPAD_DOWN", "direcional baixo"),
    ("BACK", "trocar camera"),
    ("START", "pausa / menu"),
    ("GUIDE", "botao guia"),
]


# ==========================================================================
# Thread de trabalho: dona do pygame, do controle virtual e do FFB
# ==========================================================================

class Worker(threading.Thread):
    """Le o volante o tempo todo; so envia ao controle virtual se a ponte
    estiver ligada. Assim a calibragem e o mapeamento de botoes funcionam
    sem precisar ligar nada, e sem disputa de thread pelo pygame."""

    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.comandos: queue.Queue = queue.Queue()
        self.eventos: queue.Queue = queue.Queue()

        self.rodando = True
        self.ponte_ligada = False
        self.wheel = None
        self.pad = None
        self.ffb = None
        self.gamepad = None
        self._gamepad_tentado = False
        self.slot_nosso = None
        self._ult_steer = 0.0
        self._steer_ref = 0.0
        self._t_ref = 0.0
        self._t_volante = 0.0
        self._t_stick = 0.0
        self.pad_criado_em = None
        self.jogo_antes = False
        self._proxima_tentativa_pad = 0.0

        self._proxima_tentativa = 0.0
        self._amostrando = False
        self._mins: list = []
        self._maxs: list = []
        self._ocupado = ""
        self._testando_ffb = False
        self.armado = {"throttle": False, "brake": False, "clutch": False}
        # para flagrar pedal calibrado ao contrario: guardamos o quanto o eixo
        # cru se mexeu e o menor valor ja mapeado enquanto ele nao arma
        self._cru_min: dict = {}
        self._cru_max: dict = {}
        self._map_min: dict = {}

        self.estado = {
            "nome": "", "erro": "", "eixos": [], "botoes": [], "hat": (0, 0),
            "steer": 0.0, "throttle": 0.0, "brake": 0.0, "clutch": 0.0, "graus": 0.0,
            "rumble": (0, 0), "armado": dict(self.armado), "suspeitos": [],
            "gamepad": "", "gamepad_ativo": False, "ponte": False,
            "ffb": "desligado", "hz": 0.0, "jogo_aberto": False, "ocupado": "",
        }

    # -- API usada pela interface -----------------------------------------

    def instantaneo(self) -> dict:
        with self.lock:
            return dict(self.estado)

    def enviar(self, comando: str, dado=None) -> None:
        self.comandos.put((comando, dado))

    def parar(self) -> None:
        self.rodando = False

    def resetar_armado(self) -> None:
        for k in self.armado:
            self.armado[k] = self.cfg["axes"][k]["index"] < 0
        self._cru_min.clear()
        self._cru_max.clear()
        self._map_min.clear()

    def suspeitos(self) -> list:
        """Pedais que se mexeram bastante e mesmo assim nunca chegaram ao
        repouso: sinal de calibragem invertida."""
        saida = []
        for chave in ("throttle", "brake", "clutch"):
            if self.armado.get(chave, True):
                continue
            curso = self._cru_max.get(chave, 0.0) - self._cru_min.get(chave, 0.0)
            if curso > 0.5 and self._map_min.get(chave, 1.0) > 0.08:
                saida.append(chave)
        return saida

    # -- laco principal ----------------------------------------------------

    def run(self) -> None:
        marca_hz = time.perf_counter()
        voltas = 0
        hz = 0.0
        proxima_janela = 0.0
        jogo = False

        while self.rodando:
            try:
                hz, voltas, marca_hz, proxima_janela, jogo = self._volta(
                    hz, voltas, marca_hz, proxima_janela, jogo)
            except Exception as exc:
                # o laco nao pode morrer: sem ele a janela congela inteira e
                # nada explica o motivo
                self.eventos.put(("aviso", f"erro no laco: {exc}"))
                with self.lock:
                    self.estado["erro"] = str(exc)
                time.sleep(0.5)

        self._soltar_pad()
        if self.ffb is not None:
            self.ffb.parar()
            self.ffb.fechar()
            self.ffb = None

    def _volta(self, hz, voltas, marca_hz, proxima_janela, jogo):
        inicio = time.perf_counter()
        self._processar_comandos()

        if self.wheel is None:
            self._tentar_abrir_volante()
            time.sleep(0.2)
            return hz, voltas, marca_hz, proxima_janela, jogo

        try:
            self.wheel.pump()
            eixos = self.wheel.axes()
            botoes = self.wheel.buttons()
            hat = self.wheel.hat()
        except Exception as exc:
            self.wheel = None
            with self.lock:
                self.estado["erro"] = f"volante desconectado ({exc})"
            return hz, voltas, marca_hz, proxima_janela, jogo

        if self._amostrando:
            for i, v in enumerate(eixos):
                if v < self._mins[i]:
                    self._mins[i] = v
                if v > self._maxs[i]:
                    self._maxs[i] = v

        steer, acel, freio, embr = self._mapear(eixos)
        pressionados = self._botoes_virtuais(botoes, hat, embr)
        cal_dir = self.cfg["axes"]["steering"]
        graus = graus_girados(
            eixos[cal_dir["index"]] if 0 <= cal_dir["index"] < len(eixos) else 0.0,
            cal_dir, self.cfg["steering_shaping"])

        gp = self._ler_gamepad()
        lx = self._arbitrar_direcao(steer, gp, agora=time.perf_counter())
        acel_final = max(acel, gp["rt"])
        freio_final = max(freio, gp["lt"])
        pressionados |= gp["botoes"]

        # Pausar NAO desconecta o controle virtual, so manda tudo em
        # repouso. Desconectar faria o jogo perder o dispositivo, e
        # recuperar isso exige reiniciar o jogo - foi assim que o volante
        # "sumiu" depois de fechar a ponte sem querer.
        self._garantir_pad()
        if self.pad is not None:
            if self.ponte_ligada:
                self.pad.apply(lx, acel_final, freio_final, pressionados,
                               ly=gp["ly"], rx=gp["rx"], ry=gp["ry"])
            else:
                self.pad.apply(0.0, 0.0, 0.0, set())

        self._aplicar_ffb()

        voltas += 1
        agora = time.perf_counter()
        if agora - marca_hz >= 1.0:
            hz = voltas / (agora - marca_hz)
            voltas = 0
            marca_hz = agora
        if agora >= proxima_janela:
            proxima_janela = agora + 2.0
            hwnd_jogo = janela_do_jogo(self.cfg["ffb"])
            jogo = bool(hwnd_jogo)
            self._avaliar_ordem(hwnd_jogo)
            # o volante pode ter sido desconectado - ou tomado por outro
            # programa, que e o caso silencioso: as leituras congelam
            if not joystick_conectado(self.wheel.js if self.wheel else None):
                self.eventos.put(("aviso", "perdi o volante - reconectando"))
                self._reabrir_volante()
                return hz, voltas, marca_hz, proxima_janela, jogo

        with self.lock:
            self.estado.update({
                "nome": self.wheel.name, "erro": "", "eixos": eixos,
                "botoes": botoes, "hat": hat, "steer": steer,
                "throttle": acel, "brake": freio, "clutch": embr,
                "graus": graus,
                "jogo_antes": self.jogo_antes,
                "gamepad": self.gamepad.nome if (
                    self.gamepad and self.gamepad.disponivel()) else "",
                "gamepad_ativo": gp["ativo"],
                "rumble": tuple(self.pad.rumble) if self.pad else (0, 0),
                "armado": dict(self.armado), "suspeitos": self.suspeitos(),
                "ponte": self.ponte_ligada,
                "ffb": self._rotulo_ffb(), "hz": hz, "jogo_aberto": jogo,
                "ocupado": self._ocupado,
            })

        periodo = 1.0 / max(30, int(self.cfg.get("loop_hz", 250)))
        sobra = periodo - (time.perf_counter() - inicio)
        if sobra > 0:
            time.sleep(sobra)

        return hz, voltas, marca_hz, proxima_janela, jogo

    # -- pedacos do laco ---------------------------------------------------

    ZERADO = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0,
              "botoes": frozenset(), "ativo": False}

    def _arbitrar_direcao(self, steer: float, gp: dict, agora: float) -> float:
        """Decide quem controla o analogico esquerdo: volante ou gamepad.

        Duas armadilhas ja custaram caro aqui:

        1. Comparar o volante com o quadro ANTERIOR nao detecta giro. A 250 Hz
           um giro normal muda o valor muito menos que o limiar por quadro, e
           o volante parecia sempre parado. A comparacao agora e contra uma
           referencia de 150 ms atras.
        2. O stick segurado contava como atividade continua, enquanto o
           volante segurado numa curva nao contava - bastava um resquicio de
           drift no gamepad para o volante perder a direcao para sempre.
           Agora os dois lados contam do mesmo jeito: estar fora do centro ja
           e "estar em uso".
        """
        # 1) volante em uso: longe do centro OU girando na janela de 150 ms
        if agora - self._t_ref >= 0.15:
            if abs(steer - self._steer_ref) > 0.02:
                self._t_volante = agora
            self._steer_ref = steer
            self._t_ref = agora
        if abs(steer) > 0.08:
            self._t_volante = agora

        # 2) stick em uso: empurrado alem da zona morta
        if abs(gp["lx"]) > 0.02 or abs(gp["ly"]) > 0.02:
            self._t_stick = agora

        # volante largado perto do centro devolve a direcao ao gamepad
        if (agora - self._t_volante) > 2.0 and abs(steer) < 0.08:
            return gp["lx"]
        if self._t_volante >= self._t_stick:
            return steer
        return gp["lx"]

    def _ler_gamepad(self) -> dict:
        cfg_gp = self.cfg.get("gamepad", {})
        if not cfg_gp.get("enabled", True):
            return dict(self.ZERADO)
        if self.gamepad is None:
            if self._gamepad_tentado:
                return dict(self.ZERADO)
            self._abrir_gamepad()
            if self.gamepad is None or not self.gamepad.disponivel():
                return dict(self.ZERADO)
        try:
            leitura = self.gamepad.ler(float(cfg_gp.get("deadzone", 0.15)),
                                       float(cfg_gp.get("trigger_deadzone", 0.06)))
            leitura["botoes"] = set(leitura["botoes"])
            return leitura
        except Exception:
            return dict(self.ZERADO)

    def _abrir_gamepad(self) -> None:
        """Abre o gamepad de andar a pe.

        Feito no arranque, antes de a ponte existir: assim o unico game
        controller na lista e o fisico, e nao ha como confundir com o
        controle virtual que nos mesmos criamos depois.
        """
        self._gamepad_tentado = True
        ignorar = (self.slot_nosso,) if self.slot_nosso is not None else ()
        try:
            gp = Gamepad(self.cfg.get("gamepad", {}), ignorar=ignorar)
        except Exception as exc:
            self.eventos.put(("aviso", f"gamepad: {exc}"))
            return
        if gp.disponivel():
            self.gamepad = gp
            self.eventos.put(("aviso", f"gamepad ligado no {gp.nome}"))
        else:
            self.eventos.put(("aviso", f"gamepad: {gp.erro}"))

    def _trocar_gamepad(self, slot) -> None:
        if self.gamepad is not None:
            self.gamepad.fechar()
            self.gamepad = None
        try:
            self.cfg.setdefault("gamepad", {})["slot"] = int(slot)
        except (TypeError, ValueError):
            self.cfg.setdefault("gamepad", {})["slot"] = None
        self._gamepad_tentado = False
        self._abrir_gamepad()

    def _reabrir_volante(self) -> None:
        """Larga tudo e abre o volante de novo.

        Serve para quando outro programa toma o dispositivo: o SDL nao
        reconquista sozinho e fica devolvendo leituras congeladas.
        """
        if self.ffb is not None:
            try:
                self.ffb.parar()
                self.ffb.fechar()
            except Exception:
                pass
            self.ffb = None
        self.wheel = None
        self._proxima_tentativa = 0.0
        try:
            import pygame
            pygame.joystick.quit()
            pygame.joystick.init()
        except Exception:
            pass
        self._tentar_abrir_volante()


    def _tentar_abrir_volante(self) -> None:
        if time.perf_counter() < self._proxima_tentativa:
            return
        self._proxima_tentativa = time.perf_counter() + 2.0
        try:
            self.wheel = Wheel(self.cfg)
            self.resetar_armado()
            # o force feedback acompanha o volante, nao a ponte: assim da
            # para sentir e ajustar a forca antes mesmo de ligar a ponte
            self.ffb = SdlFFB(self.wheel.js)
            with self.lock:
                self.estado["erro"] = ""
                self.estado["nome"] = self.wheel.name
        except Exception as exc:
            with self.lock:
                self.estado["erro"] = str(exc)

    def _mapear(self, eixos: list):
        ax = self.cfg["axes"]
        st = self.cfg["steering_shaping"]
        pd = self.cfg["pedal_shaping"]

        def bruto(i):
            return eixos[i] if 0 <= i < len(eixos) else 0.0

        steer = map_steering(bruto(ax["steering"]["index"]), ax["steering"], st)

        valores = {}
        for chave in ("throttle", "brake", "clutch"):
            cal = ax[chave]
            if cal["index"] < 0:
                valores[chave] = 0.0
                continue
            cru = bruto(cal["index"])
            v = map_pedal(cru, cal, pd)
            if not self.armado[chave]:
                self._cru_min[chave] = min(self._cru_min.get(chave, cru), cru)
                self._cru_max[chave] = max(self._cru_max.get(chave, cru), cru)
                self._map_min[chave] = min(self._map_min.get(chave, v), v)
                if v <= 0.08:
                    self.armado[chave] = True
                else:
                    v = 0.0
            valores[chave] = v
        return steer, valores["throttle"], valores["brake"], valores["clutch"]

    def _botoes_virtuais(self, botoes: list, hat, embreagem: float) -> set:
        mapa = {int(k): v for k, v in self.cfg["buttons"].items()
                if v in BUTTON_NAMES}
        saida = set()
        for i, apertado in enumerate(botoes):
            if apertado and i in mapa:
                saida.add(mapa[i])
        if self.cfg.get("hat_as_dpad", True):
            hx, hy = hat
            if hx > 0:
                saida.add("DPAD_RIGHT")
            elif hx < 0:
                saida.add("DPAD_LEFT")
            if hy > 0:
                saida.add("DPAD_UP")
            elif hy < 0:
                saida.add("DPAD_DOWN")
        modo = self.cfg["clutch"].get("mode", "handbrake")
        if modo != "none" and embreagem >= float(self.cfg["clutch"].get("threshold", 0.45)):
            saida.add("A" if modo == "handbrake" else modo)
        return saida

    def _garantir_pad(self) -> None:
        if self.pad is not None:
            return
        # a criacao pode esperar ate 3s pelo slot preferido; sem intervalo
        # entre tentativas, uma falha travaria o laco em ciclos de espera
        if time.perf_counter() < self._proxima_tentativa_pad:
            return
        self._proxima_tentativa_pad = time.perf_counter() + 5.0
        # Renasce no MESMO slot da vez anterior. O GTA V amarra o controle a
        # um slot quando o detecta; voltando em outro, o jogo fica olhando um
        # endereco vazio e so um reinicio do jogo resolveria.
        lembrado = carregar_estado().get("slot_virtual")

        # Fechar e reabrir rapido e o caso comum, e o controle anterior leva
        # um instante para sumir do XInput. Sem esta espera o novo nasce no
        # slot seguinte - exatamente o que faz o jogo perder o controle.
        lib = xinput_lib()
        if lib is not None and lembrado is not None:
            limite = time.perf_counter() + 3.0
            while lembrado in xinput_slots(lib) and time.perf_counter() < limite:
                time.sleep(0.15)

        try:
            self.pad = VirtualPad(slot_desejado=lembrado)
        except RuntimeError as exc:
            self.ponte_ligada = False
            self.eventos.put(("erro", str(exc)))
            return
        self.pad_criado_em = time.time()
        self.slot_nosso = self.pad.slot
        # criar/remover dispositivos XInput pode derrubar os efeitos do
        # volante; obriga a reenviar a forca na proxima volta
        if self.ffb is not None:
            self.ffb.invalidar()
        if self.slot_nosso is None:
            return
        if lembrado is None or lembrado == self.slot_nosso:
            gravar_estado({"slot_virtual": self.slot_nosso})
        else:
            # nao apaga a memoria: na proxima vez o slot preferido pode estar
            # livre, e e nele que o jogo esta olhando
            self.eventos.put(("aviso", f"o slot {lembrado} estava ocupado; "
                                       f"entrei no {self.slot_nosso}. Use "
                                       f"'Reconectar ao jogo' se ele nao responder"))

    def _soltar_pad(self) -> None:
        if self.pad is None:
            return
        self.pad.release_all()
        self.pad = None
        # sem forcar a coleta o dispositivo demora a sair do XInput, e o
        # proximo controle nasce um slot adiante
        import gc
        gc.collect()

    def _avaliar_ordem(self, hwnd_jogo: int) -> None:
        """O jogo comecou antes de o controle virtual existir?

        O GTA V so reconhece controles presentes no momento em que inicia.
        Nascendo depois, o controle existe no Windows mas o jogo ignora - e
        de fora parece que o volante quebrou. Detectar isso permite dizer o
        que aconteceu em vez de deixar o jogador adivinhando.
        """
        if not hwnd_jogo or self.pad_criado_em is None:
            self.jogo_antes = False
            return
        comeco = inicio_do_processo(hwnd_jogo)
        if comeco is None:
            return
        # margem de 2s: o jogo demora a criar a janela, entao um empate
        # tecnico conta como "a ponte chegou primeiro"
        self.jogo_antes = comeco < (self.pad_criado_em - 2.0)

    def _religar_pad(self) -> None:
        """Desconecta e reconecta o controle virtual no mesmo slot.

        Serve quando o jogo ja estava aberto e perdeu o controle: o par
        desconectar/reconectar gera um evento novo de chegada no slot que o
        jogo esta observando.
        """
        # volta para o slot em que JA estamos, nao para o lembrado no arquivo:
        # o jogo desta sessao esta amarrado a este, e trocar agora seria
        # justamente o que queremos evitar
        atual = self.slot_nosso
        if atual is not None:
            gravar_estado({"slot_virtual": atual})
        self._soltar_pad()
        time.sleep(0.8)
        self._proxima_tentativa_pad = 0.0
        self._garantir_pad()
        destino = self.slot_nosso if self.slot_nosso is not None else "?"
        self.eventos.put(("aviso", f"controle virtual reconectado no slot {destino}"))

    def _aplicar_ffb(self) -> None:
        if self.ffb is None or not self.ffb.disponivel() or self._testando_ffb:
            return
        c = self.cfg["ffb"]
        if not c.get("enabled", True) or not self.ponte_ligada:
            self.ffb.aplicar(0, 0, 0, 0.0, 0)
            return
        vibracao = 0.0
        if self.pad is not None and c.get("rumble_to_road", {}).get("enabled", True):
            ganho = float(c["rumble_to_road"].get("gain", 100)) / 100.0
            vibracao = clamp(max(self.pad.rumble) / 255.0 * ganho)
        mola = c["spring"]
        amort = c["damper"]
        self.ffb.aplicar(
            mola.get("coefficient", 0) if mola.get("enabled", True) else 0,
            mola.get("saturation", 70),
            amort.get("coefficient", 0) if amort.get("enabled", True) else 0,
            vibracao,
            c.get("gain", 100))

    def _rotulo_ffb(self) -> str:
        if self.ffb is None:
            return "aguardando volante"
        if not self.ffb.disponivel():
            return f"indisponivel ({self.ffb.erro or 'sem efeitos'})"
        if not self.cfg["ffb"].get("enabled", True):
            return "desligado nos ajustes"
        return "pronto (SDL)" if not self.ponte_ligada else "ativo (SDL)"

    # -- comandos vindos da interface --------------------------------------

    def _processar_comandos(self) -> None:
        while True:
            try:
                comando, dado = self.comandos.get_nowait()
            except queue.Empty:
                return
            try:
                self._executar(comando, dado)
            except Exception as exc:
                # um comando com defeito nao pode derrubar a thread: se o
                # laco morre, a janela inteira congela sem dizer por que
                self._ocupado = ""
                self._testando_ffb = False
                self.eventos.put(("aviso", f"falha em '{comando}': {exc}"))

    def _executar(self, comando: str, dado) -> None:
        if comando == "ponte":
            self.ponte_ligada = bool(dado)
        elif comando == "amostrar_inicio":
            n = len(self.estado.get("eixos") or []) or 8
            atual = self.wheel.axes() if self.wheel else [0.0] * n
            self._mins, self._maxs = list(atual), list(atual)
            self._amostrando = True
        elif comando == "amostrar_fim":
            self._amostrando = False
            final = self.wheel.axes() if self.wheel else []
            self.eventos.put(("amostra", (list(self._mins), list(self._maxs), final)))
        elif comando == "resetar_armado":
            self.resetar_armado()
        elif comando == "reconectar":
            self._reabrir_volante()
        elif comando == "diagnostico":
            self._ocupado = "rodando diagnostico..."
            self.eventos.put(("diagnostico", self._diagnostico()))
            self._ocupado = ""
        elif comando == "teste_ffb":
            self._teste_ffb()
        elif comando == "trocar_gamepad":
            self._trocar_gamepad(dado)
        elif comando == "religar_pad":
            self._religar_pad()
        elif comando == "trocar_volante":
            self.cfg["device"]["name_contains"] = dado or ""
            self.cfg["device"]["index"] = None
            self._reabrir_volante()
            self.eventos.put(("aviso", f"volante: {dado}"))

    def _diagnostico(self) -> list:
        linhas = []
        linhas.append((self.wheel is not None,
                       "Volante conectado",
                       self.wheel.name if self.wheel else self.estado.get("erro", "")))

        # eixos ainda iguais ao padrao = ninguem calibrou de verdade
        calibrado = (os.path.exists(CONFIG_PATH)
                     and self.cfg["axes"] != DEFAULT_CONFIG["axes"])
        pedais = [k for k in ("throttle", "brake", "clutch")
                  if self.cfg["axes"][k]["index"] >= 0]
        linhas.append((calibrado, "Calibragem gravada",
                       ("pedais: " + ", ".join(pedais)) if calibrado
                       else "use a aba Calibragem"))

        nomes_pt = {"throttle": "acelerador", "brake": "freio",
                    "clutch": "embreagem"}

        # Pedal solto num G29 fica num extremo do curso, nunca no meio. Se
        # ele esta parado bem no meio, o dispositivo nao esta reportando -
        # tipicamente o volante precisa ser reiniciado na tomada, ou o cabo
        # dos pedais se soltou da base.
        eixos_agora = self.estado.get("eixos") or []
        mudos = []
        for k in ("throttle", "brake", "clutch"):
            cal = self.cfg["axes"][k]
            i = cal["index"]
            if i < 0 or i >= len(eixos_agora):
                continue
            curso = abs(cal["rest"] - cal["full"])
            meio = (cal["rest"] + cal["full"]) / 2.0
            if curso > 0 and abs(eixos_agora[i] - meio) < curso * 0.12:
                mudos.append(k)
        # Cuidado com alarme falso: eixo que ainda nao foi tocado le zero, que
        # e justamente o meio do curso. So da para acusar travamento se o
        # pedal ja tiver sido usado (armado) e mesmo assim estiver mudo.
        armados = [k for k in ("throttle", "brake", "clutch") if self.armado.get(k)]
        if mudos and not armados:
            linhas.append((True, "Pedais ainda sem dados",
                           "normal antes do primeiro toque - pise e solte cada"
                           " pedal. Se continuarem parados no meio do curso"
                           " depois disso, o volante travou: desligue-o da"
                           " tomada e do USB por 10s"))
        elif mudos:
            linhas.append((False, "Pedal parado no meio do curso",
                           ", ".join(nomes_pt[k] for k in mudos)
                           + " - se voce nao esta com o pe nele, o volante"
                             " travou: reinicie na tomada"))
        elif eixos_agora:
            linhas.append((True, "Pedais reportando dados reais", ""))

        # Os tres pedais do G29 se comportam igual: se a calibragem de um
        # solta para um lado e a de outro para o lado oposto, um dos dois
        # esta invertido - e pedal invertido freia (ou acelera) sozinho
        # depois de armar.
        polaridade = {}
        for k in ("throttle", "brake", "clutch"):
            cal = self.cfg["axes"][k]
            if cal["index"] >= 0:
                polaridade[k] = 1 if cal["rest"] > cal["full"] else -1
        if len(set(polaridade.values())) > 1:
            positivos = [nomes_pt[k] for k, s in polaridade.items() if s > 0]
            negativos = [nomes_pt[k] for k, s in polaridade.items() if s < 0]
            linhas.append((False, "Pedais com polaridade oposta",
                           f"{', '.join(positivos)} solta para um lado e "
                           f"{', '.join(negativos)} para o outro - um dos dois "
                           f"esta invertido (veja os eixos ao vivo na aba Calibragem)"))
        else:
            linhas.append((True, "Pedais com polaridade coerente",
                           "os tres soltam para o mesmo lado"))

        proprio = self.pad is None
        pad = self.pad
        lib = xinput_lib()
        antes = xinput_slots(lib) if (lib and proprio) else []
        try:
            if pad is None:
                pad = VirtualPad()
            linhas.append((True, "Controle virtual (ViGEmBus)", "criado"))
        except RuntimeError as exc:
            linhas.append((False, "Controle virtual (ViGEmBus)",
                           str(exc).splitlines()[0]))
            return linhas

        if lib is None:
            linhas.append((False, "XInput", "nenhuma xinput*.dll encontrada"))
        else:
            # o slot e o NOSSO, nao "o primeiro ocupado": com um gamepad
            # ligado, o primeiro ocupado costuma ser ele, e o teste lia o
            # aparelho errado e acusava tudo zerado
            slot = self.slot_nosso
            if slot is None:
                slots = xinput_slots(lib)
                novos = [s for s in slots if s not in antes] or slots
                slot = novos[0] if novos else None
            if slot is None:
                linhas.append((False, "Slot XInput", "controle nao apareceu"))
            else:
                pad.apply(0.5, 0.75, 0.25, {"A"})
                time.sleep(0.15)
                v = xinput_read(lib, slot)
                ok = bool(v and abs(v["steer"] - 0.5) < 0.02
                          and abs(v["throttle"] - 0.75) < 0.02
                          and abs(v["brake"] - 0.25) < 0.02
                          and v["buttons"] == 0x1000)
                linhas.append((ok, f"Valores certos no XInput (slot {slot})",
                               "" if ok else f"lido: {v}"))
                pad.apply(0.0, 0.0, 0.0, set())
                time.sleep(0.15)
                centro = xinput_read(lib, slot)
                limpo = bool(centro and abs(centro["steer"]) < 0.01)
                linhas.append((limpo, "Centro limpo em repouso",
                               "" if limpo else
                               f"preso em {centro.get('steer', 0):+.3f}"))
        if proprio:
            pad.release_all()

        if self.ffb is None:
            linhas.append((False, "Force feedback", "volante nao aberto ainda"))
        elif not self.ffb.disponivel():
            linhas.append((False, "Force feedback",
                           self.ffb.erro or "o volante nao expos efeitos"))
        else:
            efeitos = [n for n, i in self.ffb.ids.items() if i >= 0]
            linhas.append((True, "Force feedback via SDL (sem SDK externo)",
                           "efeitos: " + ", ".join(efeitos)))

        hwnd = janela_do_jogo(self.cfg["ffb"])
        linhas.append((bool(hwnd), "GTA V aberto agora",
                       f"hwnd={hwnd}" if hwnd else "nao precisa estar aberto"))
        return linhas

    def _teste_ffb(self) -> None:
        if self.ffb is None or not self.ffb.disponivel():
            motivo = self.ffb.erro if self.ffb else "volante nao aberto"
            self.eventos.put(("ffb_teste", f"sem force feedback ({motivo})"))
            return

        sequencia = [
            ("mola fraca - volante leve",        dict(mola=20, mola_sat=60, amortecedor=0,  vibracao=0.0)),
            ("mola forte - puxa para o centro",  dict(mola=85, mola_sat=95, amortecedor=0,  vibracao=0.0)),
            ("amortecedor - aro encorpado",      dict(mola=0,  mola_sat=0,  amortecedor=70, vibracao=0.0)),
            ("vibracao - pista ruim",            dict(mola=0,  mola_sat=0,  amortecedor=0,  vibracao=0.7)),
            ("tudo junto",                       dict(mola=50, mola_sat=80, amortecedor=25, vibracao=0.35)),
        ]
        self._testando_ffb = True
        try:
            for nome, valores in sequencia:
                self._ocupado = f"FFB: {nome}"
                self.ffb.aplicar(ganho=100, **valores)
                fim = time.perf_counter() + 2.5
                while time.perf_counter() < fim:
                    if not self.rodando:
                        return
                    time.sleep(0.02)
            self.ffb.aplicar(0, 0, 0, 0.0, 0)
        finally:
            self._testando_ffb = False
            self._ocupado = ""
        self.eventos.put(("ffb_teste", "sequencia concluida - sentiu os 5?"))


# ==========================================================================
# Widgets auxiliares
# ==========================================================================

class AreaRolavel(ttk.Frame):
    """Pagina com rolagem vertical.

    A aba de ajustes cresceu mais que a janela e o conteudo do fim ficava
    cortado sem nenhum aviso. Com isso, cada aba cabe no que tiver de altura
    e a roda do mouse rola o que sobrar.
    """

    def __init__(self, pai):
        super().__init__(pai)
        self.canvas = tk.Canvas(self, bg=COR_FUNDO, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        self.interno = ttk.Frame(self.canvas, padding=(16, 14, 16, 16))
        self.janela = self.canvas.create_window((0, 0), window=self.interno,
                                                anchor="nw")
        self.interno.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self.janela, width=e.width))
        # so captura a roda enquanto o ponteiro esta sobre esta area, senao
        # uma aba rolaria a outra
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all(
            "<MouseWheel>", self._roda))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _roda(self, evento) -> None:
        if self.canvas.bbox("all") and self.canvas.bbox("all")[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-evento.delta / 120), "units")


class Barra:
    """Barra horizontal desenhada em canvas, opcionalmente com centro."""

    def __init__(self, pai, largura=300, altura=20, cor=COR_ACEL, centro=False):
        self.largura, self.altura, self.cor, self.centro = largura, altura, cor, centro
        self.canvas = tk.Canvas(pai, width=largura, height=altura, bg=COR_TRILHA,
                                highlightthickness=1, highlightbackground=COR_BORDA)
        self.retangulo = self.canvas.create_rectangle(0, 0, 0, altura, fill=cor, width=0)
        if centro:
            meio = largura / 2
            self.canvas.create_line(meio, 0, meio, altura, fill=COR_SUAVE, dash=(2, 2))

    def grid(self, **kw):
        self.canvas.grid(**kw)
        return self

    def set(self, valor: float) -> None:
        if self.centro:
            meio = self.largura / 2
            fim = meio + max(-1.0, min(1.0, valor)) * meio
            self.canvas.coords(self.retangulo, min(meio, fim), 0, max(meio, fim), self.altura)
        else:
            v = max(0.0, min(1.0, valor))
            self.canvas.coords(self.retangulo, 0, 0, v * self.largura, self.altura)


class PainelBotoes:
    """Grade de leds que acende conforme os botoes do volante."""

    def __init__(self, pai, quantidade=25, por_linha=13, tamanho=22):
        self.canvas = tk.Canvas(pai, width=por_linha * tamanho + 4,
                                height=((quantidade - 1) // por_linha + 1) * tamanho + 4,
                                bg=COR_FUNDO, highlightthickness=0)
        self.leds = []
        for i in range(quantidade):
            lin, col = divmod(i, por_linha)
            x, y = 2 + col * tamanho, 2 + lin * tamanho
            led = self.canvas.create_rectangle(x, y, x + tamanho - 4, y + tamanho - 4,
                                               fill=COR_LED_OFF, outline=COR_BORDA)
            self.canvas.create_text(x + (tamanho - 4) / 2, y + (tamanho - 4) / 2,
                                    text=str(i), font=(FONTE, 7), fill=COR_SUAVE)
            self.leds.append(led)

    def grid(self, **kw):
        self.canvas.grid(**kw)
        return self

    def set(self, apertados: list) -> None:
        for i, led in enumerate(self.leds):
            ligado = i < len(apertados) and apertados[i]
            self.canvas.itemconfig(led, fill=COR_LED_ON if ligado else COR_LED_OFF)


# ==========================================================================
# Janela principal
# ==========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("G29 -> GTA V")
        self.minsize(900, 560)
        self.configure(bg=COR_JANELA)
        self._aplicar_icone()
        self._centralizar(1040, 760)
        self._aplicar_tema()

        self.cfg = load_config()
        if not os.path.exists(CONFIG_PATH):
            save_config(self.cfg)
        self.worker = Worker(self.cfg)
        self.worker.start()

        self.cal = {}
        self.aprendendo = None
        self.botoes_antes = []
        self._sliders = []
        self._status_ate = 0.0

        self._montar_topo()
        self._montar_abas()
        self._montar_rodape()

        self.protocol("WM_DELETE_WINDOW", self._fechar)
        # A ponte e a razao de ser do programa: ligar sozinho evita o caso de
        # abrir a janela, esquecer o botao e achar que o volante quebrou.
        if self.cfg.get("auto_iniciar", True):
            self.worker.enviar("ponte", True)
        else:
            self.botao_ponte.configure(text="  Retomar ponte  ")
        # depois que a janela assentou, reposiciona as escalas pelos valores
        # da config - se alguma tiver sido empurrada pelo layout, volta
        self.after(400, self._sincronizar_sliders)
        # abre direto na Instalacao quando falta algo; senao vai para Jogar
        self.after(600, self._escolher_aba_inicial)
        self.after(33, self._tick)

    def _aplicar_icone(self) -> None:
        """Icone da janela e da barra de tarefas.

        No Windows a barra de tarefas agrupa pelo AppUserModelID, nao pela
        janela: sem definir um proprio, ela mostraria o icone do python.exe
        mesmo com a janela ja exibindo o nosso.
        """
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "icone.ico")
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "GuiRios.VirtualControllerGTAV")
        except Exception:
            pass
        if os.path.exists(caminho):
            try:
                self.iconbitmap(default=caminho)
            except Exception:
                pass

    def _centralizar(self, largura: int, altura: int) -> None:
        """Abre no meio da tela, encolhendo se a tela for menor que a janela.

        Sem o encolhimento, num monitor 1366x768 (ou com escala de 150%) a
        janela nasceria maior que a area util e as bordas de baixo ficariam
        inalcancaveis.
        """
        self.update_idletasks()
        tela_l = self.winfo_screenwidth()
        tela_a = self.winfo_screenheight()
        largura = min(largura, tela_l - 80)
        altura = min(altura, tela_a - 120)
        x = max(0, (tela_l - largura) // 2)
        # um pouco acima do centro geometrico: fica melhor com a barra de tarefas
        y = max(0, (tela_a - altura) // 2 - 30)
        self.geometry(f"{largura}x{altura}+{x}+{y}")

    # -- tema ---------------------------------------------------------------

    def _aplicar_tema(self) -> None:
        """Tema escuro sobre o 'clam'.

        Os temas nativos do Windows ('vista', 'xpnative') desenham os
        controles com bitmaps do sistema e ignoram cor de fundo, entao nao
        da para escurecer nada por cima deles. O 'clam' desenha tudo por
        conta propria e aceita a paleta inteira.
        """
        e = ttk.Style(self)
        e.theme_use("clam")

        e.configure(".", background=COR_FUNDO, foreground=COR_TEXTO,
                    fieldbackground=COR_TRILHA, bordercolor=COR_BORDA,
                    lightcolor=COR_FUNDO, darkcolor=COR_FUNDO,
                    troughcolor=COR_TRILHA, focuscolor=COR_ACENTO,
                    font=(FONTE, 9))
        e.configure("TFrame", background=COR_FUNDO)
        e.configure("Janela.TFrame", background=COR_JANELA)
        e.configure("TLabel", background=COR_FUNDO, foreground=COR_TEXTO)
        e.configure("Janela.TLabel", background=COR_JANELA, foreground=COR_TEXTO)
        e.configure("Titulo.TLabel", background=COR_JANELA, foreground=COR_TEXTO,
                    font=(FONTE, 13, "bold"))
        e.configure("Sub.TLabel", background=COR_JANELA, foreground=COR_SUAVE,
                    font=(FONTE, 9))
        e.configure("Dica.TLabel", background=COR_FUNDO, foreground=COR_SUAVE)
        e.configure("Valor.TLabel", background=COR_FUNDO, foreground=COR_ACENTO,
                    font=(FONTE, 9, "bold"))
        e.configure("Alerta.TLabel", background=COR_FUNDO, foreground=COR_FALTA)
        e.configure("Aviso.TFrame", background="#3a2a12")
        e.configure("Aviso.TLabel", background="#3a2a12", foreground="#ffd085",
                    font=(FONTE, 9))

        e.configure("TLabelframe", background=COR_FUNDO, bordercolor=COR_BORDA,
                    relief="solid", borderwidth=1)
        e.configure("TLabelframe.Label", background=COR_FUNDO,
                    foreground=COR_ACENTO, font=(FONTE, 9, "bold"))

        e.configure("TButton", background=COR_ELEVADO, foreground=COR_TEXTO,
                    bordercolor=COR_BORDA, relief="flat", padding=(12, 6),
                    focusthickness=0)
        e.map("TButton",
              background=[("pressed", COR_ACENTO), ("active", COR_TRILHA)],
              foreground=[("pressed", COR_JANELA)])
        e.configure("Grande.TButton", background=COR_ACENTO, foreground="#0d1117",
                    font=(FONTE, 10, "bold"), padding=(18, 9))
        e.map("Grande.TButton", background=[("active", COR_ACENTO_FORTE),
                                            ("pressed", COR_ACENTO_FORTE)])
        e.configure("Topo.TButton", background=COR_ELEVADO, foreground=COR_TEXTO,
                    padding=(12, 8))

        # sem bordas claras: no clam, lightcolor/darkcolor desenham o relevo
        # e sobram como um fio branco em volta do caderno de abas
        e.configure("TNotebook", background=COR_JANELA, borderwidth=0,
                    bordercolor=COR_JANELA, lightcolor=COR_JANELA,
                    darkcolor=COR_JANELA, tabmargins=(8, 6, 8, 0))
        e.configure("TNotebook.Tab", background=COR_JANELA, foreground=COR_SUAVE,
                    padding=(20, 10), borderwidth=0, bordercolor=COR_JANELA,
                    lightcolor=COR_JANELA, darkcolor=COR_JANELA, font=(FONTE, 9))
        e.map("TNotebook.Tab",
              background=[("selected", COR_FUNDO), ("active", COR_TRILHA)],
              foreground=[("selected", COR_ACENTO), ("active", COR_TEXTO)],
              lightcolor=[("selected", COR_FUNDO)],
              expand=[("selected", (0, 0, 0, 0))])

        e.configure("TCheckbutton", background=COR_FUNDO, foreground=COR_TEXTO,
                    indicatorcolor=COR_ELEVADO, indicatorrelief="flat",
                    indicatormargin=(0, 0, 8, 0), focuscolor=COR_FUNDO,
                    bordercolor=COR_BORDA, lightcolor=COR_BORDA,
                    darkcolor=COR_BORDA, padding=(0, 4))
        e.map("TCheckbutton",
              background=[("active", COR_FUNDO)],
              indicatorcolor=[("selected", COR_ACENTO),
                              ("active", COR_TRILHA)],
              foreground=[("disabled", COR_SUAVE)])

        # gripcount=0 tira as listras do cursor; o cursor em si e pintado
        # pelo 'background' do estilo, nao pelo relevo
        e.configure("Horizontal.TScale", background=COR_ACENTO,
                    troughcolor=COR_TRILHA, bordercolor=COR_TRILHA,
                    lightcolor=COR_ACENTO, darkcolor=COR_ACENTO,
                    gripcount=0, sliderthickness=16, sliderlength=18)
        e.map("Horizontal.TScale", background=[("active", COR_ACENTO_FORTE)])
        e.configure("TCombobox", fieldbackground=COR_ELEVADO,
                    background=COR_ELEVADO, foreground=COR_TEXTO,
                    arrowcolor=COR_TEXTO, bordercolor=COR_BORDA,
                    selectbackground=COR_ELEVADO, selectforeground=COR_TEXTO)
        e.map("TCombobox", fieldbackground=[("readonly", COR_ELEVADO)])
        self.option_add("*TCombobox*Listbox.background", COR_ELEVADO)
        self.option_add("*TCombobox*Listbox.foreground", COR_TEXTO)
        self.option_add("*TCombobox*Listbox.selectBackground", COR_ACENTO)

        e.configure("Vertical.TScrollbar", background=COR_ELEVADO,
                    troughcolor=COR_FUNDO, bordercolor=COR_FUNDO,
                    arrowcolor=COR_SUAVE, relief="flat")
        e.map("Vertical.TScrollbar", background=[("active", COR_TRILHA)])

        e.configure("Treeview", background=COR_FUNDO, fieldbackground=COR_FUNDO,
                    foreground=COR_TEXTO, bordercolor=COR_BORDA, rowheight=27,
                    borderwidth=0)
        e.configure("Treeview.Heading", background=COR_ELEVADO,
                    foreground=COR_SUAVE, relief="flat", padding=(8, 6))
        e.map("Treeview", background=[("selected", COR_ACENTO)],
              foreground=[("selected", COR_JANELA)])
        e.map("Treeview.Heading", background=[("active", COR_TRILHA)])

    # -- topo ---------------------------------------------------------------

    def _montar_topo(self) -> None:
        topo = ttk.Frame(self, style="Janela.TFrame", padding=(18, 14, 18, 10))
        topo.pack(fill="x")

        self.var_dispositivo = tk.StringVar(value="procurando volante...")
        ttk.Label(topo, textvariable=self.var_dispositivo,
                  style="Titulo.TLabel").grid(row=0, column=0, sticky="w")

        self.var_sub = tk.StringVar(value="")
        ttk.Label(topo, textvariable=self.var_sub,
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))

        # Dois comandos so, com significados que nao se confundem:
        # "Reconectar tudo" refaz as ligacoes; "Pausar" congela a saida.
        ttk.Button(topo, text="Reconectar tudo", style="Topo.TButton",
                   command=self._reconectar_tudo).grid(row=0, column=1, rowspan=2,
                                                       sticky="e", padx=(12, 6))
        self.botao_ponte = ttk.Button(topo, text="  Pausar ponte  ",
                                      style="Grande.TButton",
                                      command=self._alternar_ponte)
        self.botao_ponte.grid(row=0, column=2, rowspan=2, sticky="e", padx=(6, 0))
        topo.columnconfigure(0, weight=1)

        self.faixa = ttk.Frame(self, style="Aviso.TFrame", padding=(18, 10))
        self.var_faixa = tk.StringVar(value="")
        # o botao entra primeiro: empacotado depois do texto, ele seria
        # espremido contra a borda quando a mensagem fosse longa
        self.botao_faixa = ttk.Button(self.faixa, text="Reconectar tudo",
                                      style="Topo.TButton",
                                      command=self._reconectar_tudo)
        self.botao_faixa.pack(side="right", padx=(14, 0))
        ttk.Label(self.faixa, textvariable=self.var_faixa, style="Aviso.TLabel",
                  wraplength=820, justify="left").pack(side="left", fill="x",
                                                       expand=True)
        self.faixa_visivel = False

    # -- abas ---------------------------------------------------------------

    def _montar_abas(self) -> None:
        self.abas = ttk.Notebook(self)
        self.abas.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self._aba_instalacao()
        self._aba_jogar()
        self._aba_calibragem()
        self._aba_botoes()
        self._aba_ajustes()
        self._aba_diagnostico()

    def _aba_instalacao(self) -> None:
        """Tudo que antes eram arquivos .bat separados, aqui dentro.

        Quatro atalhos numerados na pasta confundem quem nao e tecnico - e
        pior, nao ha como saber qual ja foi feito. A janela consegue instalar
        as proprias dependencias porque so importa pygame e vgamepad quando
        vai usa-los: ela abre mesmo sem eles.
        """
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Instalacao  ")
        aba = pagina.interno

        grupo = ttk.LabelFrame(aba, text=" Situacao deste computador ",
                               padding=(14, 10))
        grupo.pack(fill="x")
        self.linhas_req = {}
        for chave, rotulo in (
                ("python", "Python 64 bits"),
                ("pygame", "Biblioteca pygame"),
                ("vgamepad", "Biblioteca vgamepad + driver ViGEmBus"),
                ("volante", "Volante detectado"),
                ("calibragem", "Calibragem feita"),
        ):
            linha = ttk.Frame(grupo)
            linha.pack(fill="x", pady=3)
            marca = ttk.Label(linha, text="  ?  ", width=4, style="Valor.TLabel")
            marca.pack(side="left")
            ttk.Label(linha, text=rotulo, width=40).pack(side="left")
            detalhe = ttk.Label(linha, text="verificando...", style="Dica.TLabel")
            detalhe.pack(side="left")
            self.linhas_req[chave] = (marca, detalhe)

        acoes = ttk.LabelFrame(aba, text=" Passo 1: instalar o que falta ",
                               padding=(14, 10))
        acoes.pack(fill="x", pady=(12, 0))
        barra = ttk.Frame(acoes)
        barra.pack(fill="x")
        self.botao_instalar = ttk.Button(barra, text="Instalar dependencias",
                                         style="Grande.TButton",
                                         command=self._instalar_dependencias)
        self.botao_instalar.pack(side="left")
        self.botao_reiniciar = ttk.Button(barra, text="Reiniciar a aplicacao",
                                          command=self._reiniciar_aplicacao)
        ttk.Button(barra, text="Verificar de novo",
                   command=self._verificar_requisitos).pack(side="left", padx=8)
        ttk.Label(acoes, style="Dica.TLabel", text=(
            "O Windows vai pedir confirmacao para instalar o driver ViGEmBus. "
            "Aceite - e o mesmo driver que o DS4Windows usa, assinado e gratuito."
        )).pack(anchor="w", pady=(8, 0))

        self.texto_instalar = tk.Text(acoes, height=9, wrap="word", relief="flat",
                                      borderwidth=0, font=("Consolas", 9),
                                      bg=COR_JANELA, fg=COR_SUAVE, padx=12, pady=10,
                                      highlightthickness=1,
                                      highlightbackground=COR_BORDA,
                                      highlightcolor=COR_BORDA)
        self.texto_instalar.pack(fill="both", expand=True, pady=(10, 0))
        self.texto_instalar.insert("1.0", "Nada instalado ainda nesta sessao.\n")
        self.texto_instalar.configure(state="disabled")

        passo2 = ttk.LabelFrame(aba, text=" Passo 2: calibrar ", padding=(14, 10))
        passo2.pack(fill="x", pady=(12, 0))
        ttk.Button(passo2, text="Ir para a calibragem",
                   command=lambda: self.abas.select(2)).pack(side="left")
        ttk.Label(passo2, style="Dica.TLabel", text=(
            "  Seis passos guiados: o assistente descobre sozinho qual eixo e o "
            "volante e qual e cada pedal."
        )).pack(side="left")

        passo3 = ttk.LabelFrame(aba, text=" Passo 3: jogar ", padding=(14, 10))
        passo3.pack(fill="x", pady=(12, 0))
        ttk.Label(passo3, style="Dica.TLabel", justify="left", text=(
            "A ponte ja liga sozinha quando esta janela abre. Deixe-a aberta e "
            "SO ENTAO abra o GTA V.\n"
            "Essa ordem importa: o jogo so reconhece controles que ja existiam "
            "quando ele iniciou."
        )).pack(anchor="w")

        self.fila_instalacao = queue.Queue()

    def _escolher_aba_inicial(self) -> None:
        falta = bool(conferir_dependencias()) or not self._calibrado()
        self.abas.select(0 if falta else 1)

    def _marcar_requisito(self, chave, ok, detalhe) -> None:
        marca, rotulo = self.linhas_req[chave]
        marca.configure(text=" ok " if ok else " -- ",
                        foreground=COR_OK if ok else COR_FALTA)
        rotulo.configure(text=detalhe)

    def _verificar_requisitos(self) -> None:
        bits = 64 if sys.maxsize > 2 ** 32 else 32
        versao = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self._marcar_requisito("python", bits == 64 and sys.version_info >= (3, 8),
                               f"{versao} ({bits} bits)")
        try:
            import pygame
            self._marcar_requisito("pygame", True, f"versao {pygame.version.ver}")
        except Exception as exc:
            self._marcar_requisito("pygame", False, f"nao instalado ({exc})")
        try:
            import vgamepad  # noqa: F401
            self._marcar_requisito("vgamepad", True, "instalado")
        except ImportError:
            self._marcar_requisito("vgamepad", False, "nao instalado")
        except Exception as exc:
            self._marcar_requisito("vgamepad", False,
                                   f"driver ViGEmBus faltando ({str(exc).splitlines()[0][:40]})")

        inst = self.worker.instantaneo()
        nome = inst.get("nome") or ""
        self._marcar_requisito("volante", bool(nome) and not inst.get("erro"),
                               nome[:52] or (inst.get("erro") or "nao encontrado")[:52])
        calibrado = self._calibrado()
        self._marcar_requisito("calibragem", calibrado,
                               "pronta" if calibrado else "ainda nao feita nesta maquina")

    def _instalar_dependencias(self) -> None:
        if getattr(self, "_instalando", False):
            return
        self._instalando = True
        self.botao_instalar.configure(text="Instalando...", state="disabled")
        self._escrever_instalacao(
            f"Instalando com: {sys.executable}\n"
            "Isso leva um ou dois minutos. O instalador do driver ViGEmBus vai\n"
            "pedir confirmacao do Windows - aceite.\n\n", limpar=True)

        def trabalho():
            import subprocess
            req = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "requirements.txt")
            comando = [sys.executable, "-m", "pip", "install", "-r", req]
            if not os.path.exists(req):
                comando = [sys.executable, "-m", "pip", "install", "pygame", "vgamepad"]
            try:
                processo = subprocess.Popen(
                    comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for linha in processo.stdout:
                    self.fila_instalacao.put(linha.rstrip())
                processo.wait()
                self.fila_instalacao.put(
                    "\nCONCLUIDO" if processo.returncode == 0
                    else f"\nFALHOU (codigo {processo.returncode})")
            except Exception as exc:
                self.fila_instalacao.put(f"\nERRO: {exc}")
            self.fila_instalacao.put(None)

        threading.Thread(target=trabalho, daemon=True).start()

    def _escrever_instalacao(self, texto: str, limpar: bool = False) -> None:
        self.texto_instalar.configure(state="normal")
        if limpar:
            self.texto_instalar.delete("1.0", "end")
        self.texto_instalar.insert("end", texto)
        self.texto_instalar.see("end")
        self.texto_instalar.configure(state="disabled")

    def _drenar_instalacao(self) -> None:
        while True:
            try:
                linha = self.fila_instalacao.get_nowait()
            except queue.Empty:
                return
            if linha is None:
                self._instalando = False
                self.botao_instalar.configure(text="Instalar dependencias",
                                              state="normal")
                self.botao_reiniciar.pack(side="left", padx=8)
                self._escrever_instalacao(
                    "\n\nAs bibliotecas so passam a valer depois de reiniciar a\n"
                    "aplicacao. Clique em 'Reiniciar a aplicacao'.\n")
                self._verificar_requisitos()
                return
            self._escrever_instalacao(linha + "\n")

    def _reiniciar_aplicacao(self) -> None:
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "g29_gui.py")
        try:
            subprocess.Popen([sys.executable, script],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except Exception as exc:
            messagebox.showerror("Reiniciar", f"Nao consegui reabrir: {exc}")
            return
        self._fechar()

    def _aba_jogar(self) -> None:
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Jogar  ")
        aba = pagina.interno

        grupo = ttk.LabelFrame(aba, text=" O que esta indo para o jogo ", padding=12)
        grupo.pack(fill="x")

        self.barra_volante = Barra(grupo, 460, 26, COR_VOLANTE, centro=True)
        self.barra_acel = Barra(grupo, 460, 22, COR_ACEL)
        self.barra_freio = Barra(grupo, 460, 22, COR_FREIO)
        self.barra_embr = Barra(grupo, 460, 22, COR_EMBREAGEM)
        self.barra_vibra = Barra(grupo, 460, 14, COR_VIBRA)

        self.rotulos_valor = {}
        linhas = [("Volante", self.barra_volante), ("Acelerador", self.barra_acel),
                  ("Freio", self.barra_freio), ("Embreagem", self.barra_embr),
                  ("Vibracao", self.barra_vibra)]
        for i, (nome, barra) in enumerate(linhas):
            ttk.Label(grupo, text=nome).grid(row=i, column=0, sticky="w", pady=4)
            barra.grid(row=i, column=1, padx=10, pady=4)
            var = tk.StringVar(value="0.00")
            ttk.Label(grupo, textvariable=var, width=14).grid(row=i, column=2, sticky="w")
            self.rotulos_valor[nome] = var

        self.var_graus = tk.StringVar(value="")
        ttk.Label(grupo, textvariable=self.var_graus, style="Dica.TLabel"
                  ).grid(row=len(linhas), column=0, columnspan=3, sticky="w",
                         pady=(8, 0))

        self.var_aviso = tk.StringVar(value="")
        ttk.Label(grupo, textvariable=self.var_aviso, foreground="#b06000"
                  ).grid(row=len(linhas) + 1, column=0, columnspan=3, sticky="w",
                         pady=(4, 0))

        grupo2 = ttk.LabelFrame(aba, text=" Botoes do volante ", padding=12)
        grupo2.pack(fill="x", pady=(12, 0))
        self.painel_botoes = PainelBotoes(grupo2).grid(row=0, column=0, sticky="w")
        self.var_hat = tk.StringVar(value="direcional: centro")
        ttk.Label(grupo2, textvariable=self.var_hat).grid(row=1, column=0, sticky="w",
                                                          pady=(8, 0))

        ttk.Label(aba, style="Dica.TLabel", text=(
            "Deixe esta janela aberta e abra o GTA V. Se aparecer aviso de pedal, "
            "pise e solte uma vez para liberar."
        )).pack(anchor="w", pady=(12, 0))

    def _aba_calibragem(self) -> None:
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Calibragem  ")
        aba = pagina.interno

        escolha = ttk.LabelFrame(aba, text=" Volante ", padding=(14, 10))
        escolha.pack(fill="x")
        self.var_volante = tk.StringVar(value="")
        self.combo_volante = ttk.Combobox(escolha, textvariable=self.var_volante,
                                          width=46, state="readonly", values=[])
        self.combo_volante.pack(side="left")
        self.combo_volante.bind("<<ComboboxSelected>>", lambda _e: self.worker.enviar(
            "trocar_volante", self.var_volante.get()))
        ttk.Button(escolha, text="Procurar", command=self._listar_volantes
                   ).pack(side="left", padx=8)
        ttk.Label(escolha, style="Dica.TLabel", text=(
            "  so precisa mexer se ele escolher o aparelho errado"
        )).pack(side="left")

        ao_vivo = ttk.LabelFrame(aba, text=" Eixos ao vivo ", padding=12)
        ao_vivo.pack(fill="x")
        self.barras_eixo = []
        self.rotulos_eixo = []
        for i in range(8):
            ttk.Label(ao_vivo, text=f"eixo {i}").grid(row=i, column=0, sticky="w", pady=2)
            barra = Barra(ao_vivo, 320, 14, "#6b7280", centro=True)
            barra.grid(row=i, column=1, padx=10, pady=2)
            var = tk.StringVar(value="-")
            ttk.Label(ao_vivo, textvariable=var, width=10).grid(row=i, column=2, sticky="w")
            self.barras_eixo.append(barra)
            self.rotulos_eixo.append((var, ao_vivo.grid_slaves(row=i, column=0)[0], barra))

        passos = ttk.LabelFrame(aba, text=" Passos ", padding=12)
        passos.pack(fill="x", pady=(12, 0))

        self.var_passo = {}
        definicoes = [
            ("centro", "1. Volante centralizado, pedais soltos", "Capturar repouso"),
            ("esquerda", "2. Clique e gire TODO para a esquerda", "Amostrar 5s"),
            ("direita", "3. Clique e gire TODO para a direita", "Amostrar 5s"),
            ("throttle", "4. Acelerador: pise fundo e solte 3x", "Amostrar 5s"),
            ("brake", "5. Freio: pise fundo e solte 3x", "Amostrar 5s"),
            ("clutch", "6. Embreagem: pise fundo e solte 3x", "Amostrar 5s"),
        ]
        for i, (chave, texto, rotulo_botao) in enumerate(definicoes):
            ttk.Label(passos, text=texto).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Button(passos, text=rotulo_botao, width=18,
                       command=lambda c=chave: self._capturar(c)
                       ).grid(row=i, column=1, padx=10)
            var = tk.StringVar(value="")
            ttk.Label(passos, textvariable=var, width=42,
                      style="Dica.TLabel").grid(row=i, column=2, sticky="w")
            self.var_passo[chave] = var
        passos.columnconfigure(2, weight=1)

        rodape = ttk.Frame(aba)
        rodape.pack(fill="x", pady=(12, 0))
        ttk.Button(rodape, text="Salvar calibragem",
                   command=self._salvar_calibragem).pack(side="left")
        ttk.Button(rodape, text="Descartar",
                   command=self._descartar_calibragem).pack(side="left", padx=8)
        ttk.Label(rodape, style="Dica.TLabel", text=(
            "  Nos passos 2 a 6 voce clica primeiro e mexe durante a contagem - "
            "nao precisa soltar o volante para clicar."
        )).pack(side="left")

    def _aba_botoes(self) -> None:
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Botoes  ")
        aba = pagina.interno

        self.tabela = ttk.Treeview(aba, columns=("acao", "botao"), show="headings",
                                   height=len(ACOES_GTA))
        self.tabela.heading("acao", text="Acao no GTA V")
        self.tabela.heading("botao", text="Botao do volante")
        self.tabela.column("acao", width=340)
        self.tabela.column("botao", width=160, anchor="center")
        self.tabela.pack(fill="both", expand=True)
        self._preencher_tabela()

        barra = ttk.Frame(aba)
        barra.pack(fill="x", pady=(10, 0))
        self.botao_aprender = ttk.Button(barra, text="Aprender botao",
                                         command=self._aprender)
        self.botao_aprender.pack(side="left")
        ttk.Button(barra, text="Limpar", command=self._limpar_botao
                   ).pack(side="left", padx=8)
        ttk.Button(barra, text="Salvar mapeamento", command=self._salvar_botoes
                   ).pack(side="left")
        self.var_aprender = tk.StringVar(value="Selecione uma acao e clique em Aprender.")
        ttk.Label(barra, textvariable=self.var_aprender, style="Dica.TLabel"
                  ).pack(side="left", padx=12)

    def _aba_ajustes(self) -> None:
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Ajustes  ")
        aba = pagina.interno

        dir_ = ttk.LabelFrame(aba, text=" Direcao ", padding=(14, 10))
        dir_.pack(fill="x")
        self.escala_trava = self._slider(
            dir_, 0, "Trava suave (graus)", ("steering_shaping", "soft_lock_deg"),
            180, 900, inteiro=True,
            dica="quanto do volante equivale a esterçada maxima. Menor = vira mais")

        presets = ttk.Frame(dir_)
        presets.grid(row=1, column=1, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ttk.Label(presets, text="rapido:", style="Dica.TLabel").pack(side="left")
        for rotulo, graus in (("Arcade 240", 240), ("Padrao 360", 360),
                              ("Equilibrado 450", 450), ("Sim 540", 540),
                              ("Cru 900", 900)):
            ttk.Button(presets, text=rotulo, width=15,
                       command=lambda g=graus: self._definir_trava(g)
                       ).pack(side="left", padx=2)
        self._slider(dir_, 2, "Curso fisico (graus)", ("steering_shaping", "wheel_range_deg"),
                     180, 1080, inteiro=True, dica="o mesmo valor configurado no G HUB")
        self._slider(dir_, 3, "Linearidade (gamma)", ("steering_shaping", "gamma"),
                     0.6, 2.0, inteiro=False, dica="acima de 1 = centro mais calmo")
        self._slider(dir_, 4, "Zona morta", ("steering_shaping", "deadzone"),
                     0.0, 0.2, inteiro=False)
        self.var_esterco = tk.StringVar(value="")
        ttk.Label(dir_, textvariable=self.var_esterco, style="Dica.TLabel"
                  ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

        gp = ttk.LabelFrame(aba, text=" Gamepad (andar a pe) ", padding=(14, 10))
        gp.pack(fill="x", pady=(10, 0))
        self.var_gp_ligado = tk.BooleanVar(
            value=self.cfg.get("gamepad", {}).get("enabled", True))
        ttk.Checkbutton(gp, text="Fundir com o volante", variable=self.var_gp_ligado,
                        command=lambda: self.cfg["gamepad"].__setitem__(
                            "enabled", self.var_gp_ligado.get())
                        ).grid(row=0, column=0, sticky="w", pady=4)
        self.var_gp_nome = tk.StringVar(value="")
        self.combo_gp = ttk.Combobox(gp, textvariable=self.var_gp_nome, width=24,
                                     state="readonly", values=[])
        self.combo_gp.grid(row=0, column=1, sticky="w", padx=10)
        self.combo_gp.bind("<<ComboboxSelected>>", lambda _e: self.worker.enviar(
            "trocar_gamepad", self.var_gp_nome.get().split()[1]
            if self.var_gp_nome.get().startswith("Slot") else None))
        ttk.Button(gp, text="Procurar", command=self._listar_gamepads
                   ).grid(row=0, column=2, sticky="w")
        self._slider(gp, 1, "Zona morta dos analogicos", ("gamepad", "deadzone"),
                     0.0, 0.4, inteiro=False,
                     dica="suba se o personagem andar sozinho (drift do stick)")
        ttk.Label(gp, style="Dica.TLabel", text=(
            "O gamepad entra pelo mesmo controle virtual: analogicos e botoes dele "
            "passam direto, e o volante assume a direcao quando voce gira."
        )).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ped = ttk.LabelFrame(aba, text=" Pedais ", padding=(14, 10))
        ped.pack(fill="x", pady=(10, 0))
        self._slider(ped, 0, "Zona morta", ("pedal_shaping", "deadzone"), 0.0, 0.3,
                     inteiro=False)
        self._slider(ped, 1, "Curva (gamma)", ("pedal_shaping", "gamma"), 0.6, 2.0,
                     inteiro=False)
        self._slider(ped, 2, "Embreagem: limiar", ("clutch", "threshold"), 0.1, 0.9,
                     inteiro=False)
        ttk.Label(ped, text="Embreagem faz").grid(row=3, column=0, sticky="w", pady=4)
        self.var_embr = tk.StringVar(value=self.cfg["clutch"].get("mode", "handbrake"))
        combo = ttk.Combobox(ped, textvariable=self.var_embr, width=18, state="readonly",
                             values=["handbrake", "none"] + BUTTON_NAMES)
        combo.grid(row=3, column=1, sticky="w", padx=10)
        combo.bind("<<ComboboxSelected>>",
                   lambda _e: self.cfg["clutch"].__setitem__("mode", self.var_embr.get()))

        ffb = ttk.LabelFrame(aba, text=" Force feedback ", padding=(14, 10))
        ffb.pack(fill="x", pady=(10, 0))
        self.var_ffb = tk.BooleanVar(value=self.cfg["ffb"].get("enabled", True))
        ttk.Checkbutton(ffb, text="Ligado", variable=self.var_ffb,
                        command=lambda: self.cfg["ffb"].__setitem__(
                            "enabled", self.var_ffb.get())
                        ).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Button(ffb, text="Testar efeitos",
                   command=lambda: self.worker.enviar("teste_ffb")
                   ).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(ffb, style="Dica.TLabel",
                  text="pelo SDL - nao precisa de SDK nem DLL externa"
                  ).grid(row=0, column=2, columnspan=2, sticky="w")
        self._slider(ffb, 1, "Ganho geral", ("ffb", "gain"), 0, 100,
                     inteiro=True, dica="forca de tudo; comece em 100 e reduza se pesar")
        self._slider(ffb, 2, "Mola (forca)", ("ffb", "spring", "coefficient"), 0, 100,
                     inteiro=True, dica="retorno ao centro e peso na curva")
        self._slider(ffb, 3, "Mola (saturacao)", ("ffb", "spring", "saturation"), 0, 100,
                     inteiro=True, dica="teto da forca da mola")
        self._slider(ffb, 4, "Amortecedor", ("ffb", "damper", "coefficient"), 0, 100,
                     inteiro=True, dica="tira a soltura, deixa o aro encorpado")
        self._slider(ffb, 5, "Vibracao da pista", ("ffb", "rumble_to_road", "gain"),
                     0, 200, inteiro=True,
                     dica="a trepidacao que o jogo manda ao controle vira efeito no aro")

        ttk.Button(aba, text="Salvar configuracao", command=self._salvar_cfg
                   ).pack(anchor="w", pady=(12, 0))
        ttk.Label(aba, style="Dica.TLabel", text=(
            "Os ajustes valem na hora, mesmo com a ponte ligada. Salvar so grava "
            "no config.json para a proxima vez."
        )).pack(anchor="w", pady=(6, 0))

    def _aba_diagnostico(self) -> None:
        pagina = AreaRolavel(self.abas)
        self.abas.add(pagina, text="  Diagnostico  ")
        aba = pagina.interno

        ttk.Button(aba, text="Rodar diagnostico",
                   command=lambda: self.worker.enviar("diagnostico")).pack(anchor="w")
        self.texto_diag = tk.Text(aba, height=18, wrap="word", relief="flat",
                                  borderwidth=0, font=("Consolas", 10),
                                  bg=COR_JANELA, fg=COR_TEXTO,
                                  insertbackground=COR_TEXTO,
                                  selectbackground=COR_ACENTO, padx=14, pady=12,
                                  highlightthickness=1,
                                  highlightbackground=COR_BORDA,
                                  highlightcolor=COR_BORDA)
        self.texto_diag.pack(fill="both", expand=True, pady=(12, 0))
        self.texto_diag.tag_configure("ok", foreground=COR_OK)
        self.texto_diag.tag_configure("falta", foreground=COR_FALTA)
        self.texto_diag.insert("1.0",
                               "Clique em 'Rodar diagnostico' para conferir a cadeia "
                               "inteira: volante, calibragem, controle virtual, XInput "
                               "e force feedback.\n")
        self.texto_diag.configure(state="disabled")

    def _montar_rodape(self) -> None:
        rodape = ttk.Frame(self, style="Janela.TFrame", padding=(18, 6, 18, 10))
        rodape.pack(fill="x")
        self.var_status = tk.StringVar(value="")
        ttk.Label(rodape, textvariable=self.var_status, style="Sub.TLabel"
                  ).pack(side="left")

    # -- helpers de ajuste --------------------------------------------------

    def _cfg_get(self, caminho):
        d = self.cfg
        for k in caminho[:-1]:
            d = d[k]
        return d[caminho[-1]]

    def _cfg_set(self, caminho, valor):
        d = self.cfg
        for k in caminho[:-1]:
            d = d[k]
        d[caminho[-1]] = valor

    def _slider(self, pai, linha, rotulo, caminho, minimo, maximo,
                inteiro=True, dica=""):
        """Slider que so grava na config quando VOCE mexe nele.

        O ttk.Scale dispara o callback durante o layout inicial, quando
        ainda nao tem largura real - e o valor que ele passa nessa hora vem
        da posicao em pixels, nao do valor pedido. Ligar o callback antes
        de posicionar a escala faz esse valor errado ser gravado por cima
        da configuracao do usuario. Por isso posicionamos primeiro e so
        depois armamos o callback.
        """
        ttk.Label(pai, text=rotulo).grid(row=linha, column=0, sticky="w", pady=3)
        texto = tk.StringVar()
        armado = {"ok": False}

        def formatar(v):
            return int(round(v)) if inteiro else round(v, 3)

        def mudou(valor):
            if not armado["ok"]:
                return
            v = formatar(float(valor))
            self._cfg_set(caminho, v)
            texto.set(str(v))

        escala = ttk.Scale(pai, from_=minimo, to=maximo, orient="horizontal",
                           length=250, command=mudou)
        escala.grid(row=linha, column=1, padx=12, sticky="w")
        ttk.Label(pai, textvariable=texto, width=6, style="Valor.TLabel",
                  anchor="e").grid(row=linha, column=2, sticky="w")
        if dica:
            ttk.Label(pai, text=dica, style="Dica.TLabel").grid(
                row=linha, column=3, sticky="w", padx=(10, 0))

        inicial = float(self._cfg_get(caminho))
        escala.set(inicial)
        texto.set(str(formatar(inicial)))
        # so a partir daqui um movimento conta como decisao do usuario
        self.after_idle(lambda: armado.__setitem__("ok", True))
        self._sliders.append((escala, caminho, inteiro, texto, armado))
        return escala

    def _sincronizar_sliders(self) -> None:
        """Reposiciona as escalas a partir da config, sem gravar nada."""
        for escala, caminho, inteiro, texto, armado in self._sliders:
            atual = float(self._cfg_get(caminho))
            if abs(float(escala.get()) - atual) > 1e-6:
                armado["ok"] = False
                escala.set(atual)
                self.after_idle(lambda a=armado: a.__setitem__("ok", True))
            texto.set(str(int(round(atual)) if inteiro else round(atual, 3)))

    # -- acoes --------------------------------------------------------------

    def _listar_volantes(self) -> None:
        nomes = [n for _, n in Wheel.listar()]
        self.combo_volante.configure(values=nomes)
        atual = self.worker.instantaneo().get("nome") or ""
        if atual in nomes:
            self.var_volante.set(atual)
        self._mostrar_status(f"dispositivos: {', '.join(nomes) or 'nenhum'}")

    def _listar_gamepads(self) -> None:
        nosso = self.worker.slot_nosso
        ignorar = (nosso,) if nosso is not None else ()
        rotulos = [n for _, n in Gamepad.listar(ignorar)]
        self.combo_gp.configure(values=rotulos)
        extra = f" (o slot {nosso} e da propria ponte)" if nosso is not None else ""
        self._mostrar_status(
            f"controles XInput: {', '.join(rotulos) or 'nenhum'}{extra}")

    def _reconectar(self) -> None:
        self._mostrar_status("reabrindo o volante...")
        self.worker.enviar("reconectar")

    def _definir_trava(self, graus: int) -> None:
        # mexer na escala dispara o mesmo callback do arraste, que grava na
        # config e atualiza o numero ao lado
        self.escala_trava.set(graus)

    def _alternar_ponte(self) -> None:
        ligar = not self.worker.ponte_ligada
        self.worker.enviar("ponte", ligar)
        self.botao_ponte.configure(text="  Pausar ponte  " if ligar
                                   else "  Retomar ponte  ")

    def _mostrar_status(self, texto: str, segundos: float = 5.0) -> None:
        self.var_status.set(texto)
        self._status_ate = time.time() + segundos

    def _reconectar_tudo(self) -> None:
        """Um comando so: reabre o volante, reconecta o controle virtual no
        mesmo slot e reprocura o gamepad."""
        self._mostrar_status("reconectando volante, controle virtual e gamepad...", 8)
        self.worker.enviar("reconectar")
        self.worker.enviar("religar_pad")
        self.worker.enviar("trocar_gamepad", None)

    def _calibrado(self) -> bool:
        """Eixos ainda iguais ao padrao de fabrica = ninguem calibrou aqui."""
        return (os.path.exists(CONFIG_PATH)
                and self.cfg["axes"] != DEFAULT_CONFIG["axes"])

    def _faixa_aviso(self, texto: str) -> None:
        if texto:
            self.var_faixa.set(texto)
            if not self.faixa_visivel:
                self.faixa.pack(fill="x", before=self.abas)
                self.faixa_visivel = True
        elif self.faixa_visivel:
            self.faixa.pack_forget()
            self.faixa_visivel = False

    def _capturar(self, chave: str) -> None:
        inst = self.worker.instantaneo()
        eixos = inst.get("eixos") or []
        if not eixos:
            messagebox.showwarning("Sem volante", "Volante nao detectado ainda.")
            return

        if chave == "centro":
            self.cal["centro"] = list(eixos)
            self.var_passo[chave].set(
                "repouso: " + " ".join(f"{v:+.2f}" for v in eixos))
            return

        # Todo o resto e por amostragem: o volante tem mola de centragem, e
        # se a captura fosse no clique a mao ja teria saido do aro e o valor
        # lido seria o centro, nao o extremo.
        self._amostrando_passo = chave
        self.worker.enviar("amostrar_inicio")
        self._contagem(chave, 5)

    def _contagem(self, chave: str, resta: int) -> None:
        if getattr(self, "_amostrando_passo", None) != chave:
            return
        if resta <= 0:
            self.var_passo[chave].set("processando...")
            self.worker.enviar("amostrar_fim")
            return
        acao = ("gire e segure" if chave in ("esquerda", "direita")
                else "pise fundo e solte")
        self.var_passo[chave].set(f"amostrando: {acao}...  {resta}s")
        self.after(1000, lambda: self._contagem(chave, resta - 1))

    def _receber_amostra(self, dados) -> None:
        chave = getattr(self, "_amostrando_passo", None)
        if chave is None:
            return
        self._amostrando_passo = None
        mins, maxs, final = dados
        if not mins:
            self.var_passo[chave].set("sem leitura do volante")
            return
        base = self.cal.get("centro")

        if chave in ("esquerda", "direita"):
            idx = max(range(len(mins)), key=lambda i: maxs[i] - mins[i])
            curso = maxs[idx] - mins[idx]
            if curso < 0.30:
                self.var_passo[chave].set("nao vi giro - gire ate o batente")
                return
            centro = base[idx] if base and idx < len(base) else 0.0
            # o extremo mais longe do centro e a ponta do giro
            valor = (mins[idx] if abs(mins[idx] - centro) > abs(maxs[idx] - centro)
                     else maxs[idx])
            self.cal["eixo_volante"] = idx
            self.cal[chave] = valor
            self.var_passo[chave].set(
                f"eixo {idx}: batente={valor:+.3f}  (centro {centro:+.3f})")
            return

        usados = set()
        if "eixo_volante" in self.cal:
            usados.add(self.cal["eixo_volante"])
        for outro in ("throttle", "brake", "clutch"):
            if outro != chave and isinstance(self.cal.get(outro), dict):
                usados.add(self.cal[outro]["index"])
        candidatos = [i for i in range(len(mins)) if i not in usados]
        if not candidatos:
            self.var_passo[chave].set("sem eixos livres")
            return
        idx = max(candidatos, key=lambda i: maxs[i] - mins[i])
        curso = maxs[idx] - mins[idx]
        if curso < 0.30:
            self.var_passo[chave].set("nao vi movimento nesse pedal")
            return
        atual = final[idx] if idx < len(final) else mins[idx]
        repouso, fundo = deduzir_repouso(
            mins[idx], maxs[idx], atual,
            base[idx] if base and idx < len(base) else None)
        self.cal[chave] = {"index": idx, "rest": round(repouso, 4),
                           "full": round(fundo, 4)}
        self.var_passo[chave].set(
            f"eixo {idx}: solto={repouso:+.2f} fundo={fundo:+.2f} (curso {curso:.2f})")

    def _salvar_calibragem(self) -> None:
        """Grava so o que foi capturado agora.

        O que nao foi refeito nesta sessao fica como estava: da para voltar
        aqui e corrigir um pedal so, sem perder o resto da calibragem.
        """
        passos_volante = [k for k in ("centro", "esquerda", "direita")
                          if k in self.cal]
        volante_completo = len(passos_volante) == 3
        pedais = {k: v for k, v in self.cal.items()
                  if k in ("throttle", "brake", "clutch") and isinstance(v, dict)}

        if not volante_completo and not pedais:
            if passos_volante:
                messagebox.showwarning(
                    "Volante incompleto",
                    "Para gravar a direcao faltam os passos: "
                    + ", ".join(k for k in ("centro", "esquerda", "direita")
                                if k not in self.cal))
            else:
                messagebox.showwarning("Nada capturado",
                                       "Faca pelo menos um passo antes de salvar.")
            return

        gravados = []
        if volante_completo:
            idx = self.cal.get("eixo_volante", 0)
            self.cfg["axes"]["steering"] = {
                "index": idx,
                "center": round(self.cal["centro"][idx], 4),
                "left": round(self.cal["esquerda"], 4),
                "right": round(self.cal["direita"], 4),
            }
            gravados.append("direcao")
        for chave, dados in pedais.items():
            self.cfg["axes"][chave] = dados
            gravados.append({"throttle": "acelerador", "brake": "freio",
                             "clutch": "embreagem"}[chave])

        save_config(self.cfg)
        self.worker.enviar("resetar_armado")
        aviso = ""
        if passos_volante and not volante_completo:
            aviso = "\n\nA direcao NAO foi gravada (faltaram passos) e ficou como estava."
        messagebox.showinfo("Calibragem",
                            "Gravado: " + ", ".join(gravados) + "." + aviso
                            + "\n\nO resto ficou como estava.")

    def _descartar_calibragem(self) -> None:
        self.cal.clear()
        for var in self.var_passo.values():
            var.set("")

    def _preencher_tabela(self) -> None:
        self.tabela.delete(*self.tabela.get_children())
        reverso = {}
        for indice, nome in self.cfg["buttons"].items():
            reverso.setdefault(nome, []).append(indice)
        for nome, descricao in ACOES_GTA:
            indices = reverso.get(nome, [])
            texto = ", ".join(sorted(indices, key=lambda s: int(s))) if indices else "-"
            self.tabela.insert("", "end", iid=nome,
                               values=(f"{nome}  -  {descricao}", texto))

    def _aprender(self) -> None:
        selecao = self.tabela.selection()
        if not selecao:
            self.var_aprender.set("Selecione uma acao na lista primeiro.")
            return
        self.aprendendo = selecao[0]
        self.var_aprender.set(f"Aperte agora o botao do volante para '{self.aprendendo}'.")
        self.botao_aprender.state(["disabled"])

    def _limpar_botao(self) -> None:
        selecao = self.tabela.selection()
        if not selecao:
            return
        alvo = selecao[0]
        for indice in [k for k, v in self.cfg["buttons"].items() if v == alvo]:
            self.cfg["buttons"].pop(indice)
        self._preencher_tabela()
        self.tabela.selection_set(alvo)

    def _salvar_botoes(self) -> None:
        save_config(self.cfg)
        messagebox.showinfo("Botoes", "Mapeamento salvo.")

    def _salvar_cfg(self) -> None:
        save_config(self.cfg)
        messagebox.showinfo("Configuracao", f"Gravado em:\n{CONFIG_PATH}")

    # -- atualizacao periodica ---------------------------------------------

    def _tick(self) -> None:
        inst = self.worker.instantaneo()
        self._drenar_instalacao()
        if time.time() >= getattr(self, "_proxima_checagem", 0):
            self._proxima_checagem = time.time() + 2.0
            self._verificar_requisitos()

        while True:
            try:
                tipo, dado = self.worker.eventos.get_nowait()
            except queue.Empty:
                break
            if tipo == "amostra":
                self._receber_amostra(dado)
            elif tipo == "diagnostico":
                self._mostrar_diagnostico(dado)
            elif tipo == "ffb_teste":
                self._mostrar_status(f"FFB: {dado}")
            elif tipo == "aviso":
                self._mostrar_status(dado)
            elif tipo == "erro":
                messagebox.showerror("Erro", dado)
                self.botao_ponte.configure(text="  Ativar ponte  ")

        erro = inst.get("erro")
        if erro:
            self.var_dispositivo.set("volante nao encontrado")
            self.var_sub.set(erro.split(".")[0])
        else:
            self.var_dispositivo.set(inst.get("nome") or "procurando volante...")
            gp_nome = inst.get("gamepad") or ""
            gp_txt = (f"gamepad: {gp_nome[:26]}"
                      + (" (ativo)" if inst.get("gamepad_ativo") else "")
                      if gp_nome else "sem gamepad")
            self.var_sub.set(
                f"ponte {'ATIVA' if inst['ponte'] else 'parada'}  |  "
                f"force feedback: {inst['ffb']}  |  {gp_txt}  |  "
                f"GTA V: {'aberto' if inst['jogo_aberto'] else 'fechado'}")

        self.barra_volante.set(inst["steer"])
        self.barra_acel.set(inst["throttle"])
        self.barra_freio.set(inst["brake"])
        self.barra_embr.set(inst["clutch"])
        vibra = max(inst["rumble"]) / 255.0 if inst["rumble"] else 0.0
        self.barra_vibra.set(vibra)
        self.rotulos_valor["Volante"].set(f"{inst['steer']:+.3f}")
        self.rotulos_valor["Acelerador"].set(f"{inst['throttle']:.3f}")
        self.rotulos_valor["Freio"].set(f"{inst['brake']:.3f}")
        self.rotulos_valor["Embreagem"].set(f"{inst['clutch']:.3f}")
        self.rotulos_valor["Vibracao"].set(
            f"{inst['rumble'][0]} / {inst['rumble'][1]}")

        if not self._calibrado():
            self._faixa_aviso(
                "Primeira vez neste computador: o volante ainda nao foi "
                "calibrado. Va na aba Calibragem e faca os 6 passos - sem "
                "isso os pedais e a direcao nao vao corresponder ao seu "
                "equipamento.")
        elif inst.get("jogo_antes"):
            self._faixa_aviso(
                "O GTA V foi aberto ANTES da ponte. O jogo so reconhece "
                "controles que ja existiam quando ele iniciou, entao o volante "
                "provavelmente nao vai responder. Tente Reconectar tudo; se nao "
                "resolver, feche e abra o jogo com esta janela ja aberta.")
        elif not inst.get("ponte"):
            self._faixa_aviso("Ponte pausada: o jogo continua vendo o controle, "
                              "mas em repouso. Clique em Retomar ponte.")
        else:
            self._faixa_aviso("")

        graus = inst.get("graus", 0.0)
        trava = float(self.cfg["steering_shaping"].get("soft_lock_deg", 900) or 900)
        curso = float(self.cfg["steering_shaping"].get("wheel_range_deg", 900) or 900)
        self.var_graus.set(
            f"volante em {graus:+.0f}deg de {curso/2:.0f}deg  |  "
            f"esterçada maxima do jogo com {trava/2:.0f}deg para cada lado")
        self.var_esterco.set(
            f"Com trava {trava:.0f}: voce gira {trava/2:.0f}deg para bater no batente "
            f"do jogo (o aro continua indo ate {curso/2:.0f}deg). "
            f"Sensacao de atraso = trava alta demais.")

        nomes_pt = {"throttle": "acelerador", "brake": "freio",
                    "clutch": "embreagem"}
        suspeitos = inst.get("suspeitos") or []
        faltam = [k for k, v in inst["armado"].items() if not v]
        if suspeitos:
            self.var_aviso.set(
                "Calibragem invertida: " + ", ".join(nomes_pt[k] for k in suspeitos)
                + " -- esse pedal nunca chega ao repouso. Refaca na aba Calibragem.")
        elif faltam:
            self.var_aviso.set("Pise e solte uma vez: "
                               + ", ".join(nomes_pt.get(k, k) for k in faltam))
        else:
            self.var_aviso.set("")

        self.painel_botoes.set(inst["botoes"])
        hx, hy = inst["hat"]
        nomes = {(0, 0): "centro", (1, 0): "direita", (-1, 0): "esquerda",
                 (0, 1): "cima", (0, -1): "baixo"}
        self.var_hat.set(f"direcional: {nomes.get((hx, hy), f'{hx},{hy}')}")

        eixos = inst["eixos"]
        for i, (var, rotulo, barra) in enumerate(self.rotulos_eixo):
            if i < len(eixos):
                barra.set(eixos[i])
                var.set(f"{eixos[i]:+.3f}")
            else:
                barra.set(0.0)
                var.set("-")

        if self.aprendendo:
            novos = [i for i, apertado in enumerate(inst["botoes"])
                     if apertado and (i >= len(self.botoes_antes)
                                      or not self.botoes_antes[i])]
            if novos:
                indice = str(novos[0])
                for antigo in [k for k, v in self.cfg["buttons"].items()
                               if v == self.aprendendo]:
                    self.cfg["buttons"].pop(antigo)
                self.cfg["buttons"][indice] = self.aprendendo
                self.var_aprender.set(
                    f"'{self.aprendendo}' = botao {indice}. Nao esqueca de salvar.")
                self.aprendendo = None
                self.botao_aprender.state(["!disabled"])
                self._preencher_tabela()
        self.botoes_antes = list(inst["botoes"])

        ocupado = inst.get("ocupado") or ""
        # avisos ficam alguns segundos na tela antes de a linha periodica
        # voltar, senao somem antes de serem lidos
        if ocupado:
            self.var_status.set(ocupado)
        elif time.time() >= getattr(self, "_status_ate", 0):
            slot = self.worker.slot_nosso
            onde = f"slot {slot}" if slot is not None else "sem controle"
            self.var_status.set(
                f"laco: {inst['hz']:.0f} Hz   |   controle virtual: {onde}"
                f"   |   {CONFIG_PATH}")

        self._id_tick = self.after(33, self._tick)

    def _mostrar_diagnostico(self, linhas) -> None:
        self.texto_diag.configure(state="normal")
        self.texto_diag.delete("1.0", "end")
        pendencias = 0
        for ok, texto, detalhe in linhas:
            marca = "[ok]  " if ok else "[--]  "
            self.texto_diag.insert("end", marca, "ok" if ok else "falta")
            self.texto_diag.insert("end", texto + ("  ->  " + detalhe if detalhe else "")
                                   + "\n")
            pendencias += 0 if ok else 1
        self.texto_diag.insert(
            "end", "\n" + ("Tudo certo: ative a ponte e abra o GTA V."
                           if pendencias == 0 else
                           f"{pendencias} item(ns) pendente(s) acima."))
        self.texto_diag.configure(state="disabled")

    def _fechar(self) -> None:
        # cancela o proximo tick antes de destruir a janela, senao o Tk
        # reclama de "invalid command name" ao disparar num widget morto
        ident = getattr(self, "_id_tick", None)
        if ident is not None:
            try:
                self.after_cancel(ident)
            except Exception:
                pass
        self.worker.enviar("ponte", False)
        self.worker.parar()
        self.worker.join(timeout=1.5)
        self.destroy()


def conferir_dependencias() -> list:
    """O que falta para a aplicacao rodar nesta maquina.

    Numa maquina que nunca executou o projeto, faltar um pacote produzia um
    traceback - que nao diz a ninguem o que fazer. Aqui a checagem acontece
    antes de montar a janela e devolve texto acionavel.
    """
    problemas = []
    if sys.version_info < (3, 8):
        problemas.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} e antigo "
            f"demais. Instale o Python 3.9 ou mais novo em python.org")
    if sys.maxsize <= 2 ** 32:
        problemas.append("Este Python e de 32 bits; use a versao de 64 bits")
    try:
        import pygame  # noqa: F401
    except ImportError:
        problemas.append("Falta a biblioteca 'pygame'")
    try:
        import vgamepad  # noqa: F401
    except ImportError:
        problemas.append("Falta a biblioteca 'vgamepad'")
    except Exception as exc:
        problemas.append(
            "O 'vgamepad' esta instalado mas nao carregou "
            f"({str(exc).splitlines()[0]}) - normalmente e o driver ViGEmBus")
    return problemas


def main() -> int:
    # De proposito NAO recusamos abrir quando falta pygame ou vgamepad: a
    # janela e justamente quem instala essas bibliotecas, na aba Instalacao.
    # So o Python em si nao da para resolver de dentro.
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
