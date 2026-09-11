#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
g29_gtav.py - Ponte Logitech G29 -> controle Xbox 360 virtual (ViGEmBus),
para jogar Grand Theft Auto V (Legacy ou Enhanced) com volante, pedais
analogicos separados e force feedback opcional.

O GTA V tem suporte nativo a volante bem fraco, mas suporte a XInput
excelente. Este script le o G29 por DirectInput/SDL e reproduz os comandos
num controle Xbox 360 virtual, que o jogo enxerga como um gamepad comum.

Uso:
    python g29_gtav.py --listar             lista dispositivos vistos pelo SDL
    python g29_gtav.py --monitorar          mostra eixos/botoes ao vivo
    python g29_gtav.py --calibrar           assistente de calibragem dos eixos
    python g29_gtav.py --calibrar-botoes    assistente de mapeamento de botoes
    python g29_gtav.py                      executa a ponte (Ctrl+C para sair)
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# --------------------------------------------------------------------------
# Configuracao padrao
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "_comentario": "Ajuste com --calibrar ou editando na mao. Angulos em graus.",
    "device": {
        "name_contains": "",
        "index": None
    },
    "loop_hz": 250,
    "auto_iniciar": True,
    "axes": {
        "steering": {"index": 0, "center": 0.0, "left": -1.0, "right": 1.0},
        "throttle": {"index": 1, "rest": 1.0, "full": -1.0},
        "brake":    {"index": 2, "rest": 1.0, "full": -1.0},
        "clutch":   {"index": 3, "rest": 1.0, "full": -1.0}
    },
    "steering_shaping": {
        "deadzone": 0.01,
        "gamma": 1.0,
        "wheel_range_deg": 900,
        "soft_lock_deg": 900
    },
    "pedal_shaping": {
        "deadzone": 0.05,
        "gamma": 1.0
    },
    "clutch": {
        "mode": "handbrake",
        "threshold": 0.45
    },
    "hat_as_dpad": True,
    "gamepad": {
        "enabled": True,
        "slot": None,
        "deadzone": 0.15,
        "trigger_deadzone": 0.06
    },
    "buttons": {
        "0":  "A",
        "1":  "B",
        "2":  "X",
        "3":  "Y",
        "4":  "RB",
        "5":  "LB",
        "6":  "DPAD_RIGHT",
        "7":  "DPAD_LEFT",
        "8":  "BACK",
        "9":  "START",
        "10": "RS",
        "11": "LS",
        "23": "GUIDE"
    },
    "ffb": {
        "enabled": True,
        "gain": 100,
        "dll": "",
        "device_index": 0,
        "attach_to_game_window": True,
        "game_window_class": "grcWindow",
        "game_window_title": "Grand Theft Auto V",
        "game_process": ["gta5.exe", "gta5_enhanced.exe"],
        "operating_range_deg": 900,
        "spring":  {"enabled": True, "coefficient": 55, "saturation": 70, "offset": 0},
        "damper":  {"enabled": True, "coefficient": 25},
        "rumble_to_road": {"enabled": True, "gain": 100},
        "leds_from_throttle": False
    }
}

BUTTON_NAMES = [
    "A", "B", "X", "Y", "LB", "RB", "BACK", "START", "LS", "RS",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "GUIDE"
]


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return deep_merge(DEFAULT_CONFIG, json.load(fh))
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    print(f"  -> configuracao gravada em {CONFIG_PATH}")


# --------------------------------------------------------------------------
# Matematica dos eixos
# --------------------------------------------------------------------------

def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def map_pedal(raw: float, cal: dict, shaping: dict) -> float:
    """Converte o valor cru do pedal para 0.0 (solto) .. 1.0 (fundo)."""
    rest, full = cal["rest"], cal["full"]
    span = full - rest
    if abs(span) < 1e-6:
        return 0.0
    v = clamp((raw - rest) / span)
    dz = shaping.get("deadzone", 0.0)
    if dz > 0:
        v = 0.0 if v <= dz else (v - dz) / (1.0 - dz)
    gamma = shaping.get("gamma", 1.0) or 1.0
    if gamma != 1.0:
        v = v ** gamma
    return clamp(v)


def posicao_bruta(raw: float, cal: dict) -> float:
    """Posicao no curso FISICO do volante: -1 (batente esquerdo) a +1.

    Sem trava suave e sem curva - e o quanto o aro girou de verdade, que e
    o que permite mostrar o angulo em graus.
    """
    center, left, right = cal["center"], cal["left"], cal["right"]
    delta = raw - center
    span_r = right - center
    span_l = left - center
    if span_r != 0 and delta * span_r > 0:
        return clamp(delta / span_r, -1.0, 1.0)
    if span_l != 0:
        return clamp(-(delta / span_l), -1.0, 1.0)
    return 0.0


def graus_girados(raw: float, cal: dict, shaping: dict) -> float:
    """Quantos graus o volante esta girado (negativo = esquerda)."""
    curso = float(shaping.get("wheel_range_deg", 900) or 900)
    return posicao_bruta(raw, cal) * curso / 2.0


def map_steering(raw: float, cal: dict, shaping: dict) -> float:
    """Converte o valor cru do volante para -1.0 (esquerda) .. +1.0 (direita)."""
    v = posicao_bruta(raw, cal)

    # trava suave: usa so uma parte do curso fisico ate a esterçada maxima
    wheel_range = float(shaping.get("wheel_range_deg", 900) or 900)
    soft_lock = float(shaping.get("soft_lock_deg", wheel_range) or wheel_range)
    if 0 < soft_lock < wheel_range:
        v *= wheel_range / soft_lock

    v = clamp(v, -1.0, 1.0)

    dz = shaping.get("deadzone", 0.0)
    if dz > 0:
        mag = abs(v)
        mag = 0.0 if mag <= dz else (mag - dz) / (1.0 - dz)
        v = mag if v >= 0 else -mag

    gamma = shaping.get("gamma", 1.0) or 1.0
    if gamma != 1.0:
        v = (abs(v) ** gamma) * (1.0 if v >= 0 else -1.0)

    return clamp(v, -1.0, 1.0)


# --------------------------------------------------------------------------
# Leitura do volante (SDL / pygame)
# --------------------------------------------------------------------------

# Nomes que denunciam um volante, do mais especifico para o mais generico.
# Sem essa lista, um perfil com "G29" no nome nao casava com um G27 ou G920, e
# a busca caia no "primeiro dispositivo que nao e pad virtual" - que com um
# gamepad ligado seria o gamepad, lido como se fosse o volante.
PADROES_VOLANTE = (
    "g29", "g920", "g923", "g27", "g25", "g940",
    "driving force", "racing wheel", "momo", "wingman formula",
    "thrustmaster", "t300", "t150", "tmx", "fanatec", "clubsport",
    "wheel", "volante",
)

PADS_VIRTUAIS = ("xbox 360 controller", "xinput", "vigem", "virtual gamepad",
                 "dualshock 4 emulat")


def parece_pad_virtual(nome: str) -> bool:
    """Reconhece o controle virtual que a propria ponte cria."""
    baixo = (nome or "").lower()
    return any(marca in baixo for marca in PADS_VIRTUAIS)


class Wheel:
    @staticmethod
    def listar() -> list:
        """[(indice, nome)] dos dispositivos que NAO sao o controle virtual."""
        try:
            import pygame
        except ImportError:
            return []
        if not pygame.get_init():
            pygame.init()
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        achados = []
        for i in range(pygame.joystick.get_count()):
            nome = pygame.joystick.Joystick(i).get_name()
            if not parece_pad_virtual(nome):
                achados.append((i, nome))
        return achados

    def __init__(self, cfg: dict):
        import pygame
        self.pygame = pygame
        pygame.init()
        pygame.joystick.init()

        count = pygame.joystick.get_count()
        if count == 0:
            raise RuntimeError(
                "Nenhum controle encontrado. Ligue o G29, abra o G HUB uma vez e "
                "confira se o volante faz a auto-calibragem (gira sozinho) ao ligar."
            )

        wanted_idx = cfg["device"].get("index")
        needle = (cfg["device"].get("name_contains") or "").lower()
        nomes = [pygame.joystick.Joystick(i).get_name() for i in range(count)]
        chosen = None

        # O nome vem antes do indice de proposito: assim que a ponte cria o
        # controle virtual, ele entra na lista do SDL e empurra o volante
        # para tras (o pad virtual costuma virar o indice 0). Um indice
        # salvo antes disso passaria a apontar para o proprio pad virtual -
        # a ponte leria a propria saida e o volante nao faria nada.
        if needle:
            for i, nome in enumerate(nomes):
                if needle in nome.lower():
                    chosen = i
                    break

        # sem escolha explicita, procura por nomes tipicos de volante, do
        # mais especifico para o mais generico
        if chosen is None:
            for padrao in PADROES_VOLANTE:
                for i, nome in enumerate(nomes):
                    if padrao in nome.lower() and not parece_pad_virtual(nome):
                        chosen = i
                        break
                if chosen is not None:
                    break
        if chosen is None and isinstance(wanted_idx, int) and 0 <= wanted_idx < count:
            if not parece_pad_virtual(nomes[wanted_idx]):
                chosen = wanted_idx
        if chosen is None:
            for i, nome in enumerate(nomes):
                if not parece_pad_virtual(nome):
                    chosen = i
                    break
        if chosen is None:
            raise RuntimeError(
                "So encontrei o controle virtual, nenhum volante de verdade: "
                + ", ".join(nomes) + ". Ligue o G29 e abra o G HUB uma vez."
            )

        self.js = pygame.joystick.Joystick(chosen)
        self.js.init()
        self.index = chosen
        self.name = self.js.get_name()
        self.num_axes = self.js.get_numaxes()
        self.num_buttons = self.js.get_numbuttons()
        self.num_hats = self.js.get_numhats()

    def pump(self) -> None:
        self.pygame.event.pump()

    def axis(self, i: int) -> float:
        return self.js.get_axis(i) if 0 <= i < self.num_axes else 0.0

    def axes(self) -> list:
        return [self.js.get_axis(i) for i in range(self.num_axes)]

    def buttons(self) -> list:
        return [bool(self.js.get_button(i)) for i in range(self.num_buttons)]

    def hat(self) -> tuple:
        return self.js.get_hat(0) if self.num_hats else (0, 0)

    def settle(self, seconds: float = 0.3) -> list:
        """Bombeia eventos por um instante e devolve a leitura estabilizada."""
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.pump()
            time.sleep(0.005)
        return self.axes()


# --------------------------------------------------------------------------
# Gamepad comum (para andar a pe enquanto o volante cuida do carro)
# --------------------------------------------------------------------------

class Gamepad:
    """Le um controle comum direto pelo XInput.

    O caminho do SDL nao serve aqui por dois motivos medidos nesta maquina:
    a ordem em que ele lista os controles muda entre execucoes (a ponte
    acabava abrindo o proprio controle virtual e lendo a propria saida), e
    ele devolvia zero para o gamepad fisico enquanto o XInput entregava os
    valores reais. Como o GTA V tambem le por XInput, ler pela mesma API
    elimina a duvida: o que a gente ve e o que o jogo veria.

    O slot e a identidade: o volante nao aparece no XInput, e o nosso
    controle virtual e identificado e excluido por quem nos instancia.
    """

    BOTOES = {0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y",
              0x0100: "LB", 0x0200: "RB", 0x0020: "BACK", 0x0010: "START",
              0x0040: "LS", 0x0080: "RS", 0x0001: "DPAD_UP",
              0x0002: "DPAD_DOWN", 0x0004: "DPAD_LEFT", 0x0008: "DPAD_RIGHT"}

    @staticmethod
    def listar(ignorar=()) -> list:
        """[(slot, rotulo)] dos controles XInput ligados agora."""
        lib = xinput_lib()
        if lib is None:
            return []
        return [(s, f"Slot {s} (XInput)") for s in xinput_slots(lib)
                if s not in ignorar]

    def __init__(self, cfg_gp: dict, ignorar=()):
        self.lib = xinput_lib()
        self.slot = None
        self.nome = ""
        self.erro = ""
        if self.lib is None:
            self.erro = "XInput indisponivel"
            return

        disponiveis = [s for s, _ in self.listar(ignorar)]
        if not disponiveis:
            self.erro = "nenhum controle XInput ligado"
            return

        pedido = cfg_gp.get("slot")
        if isinstance(pedido, int) and pedido in disponiveis:
            self.slot = pedido
        else:
            self.slot = disponiveis[0]
        self.nome = f"Slot {self.slot} (XInput)"

    def disponivel(self) -> bool:
        return self.slot is not None

    def ler(self, zona_morta: float = 0.15,
            zona_gatilho: float = 0.06) -> dict:
        vazio = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0,
                 "lt": 0.0, "rt": 0.0, "botoes": set(), "ativo": False}
        if self.slot is None:
            return vazio
        estado = _XINPUT_STATE()
        if self.lib.XInputGetState(self.slot, ctypes.byref(estado)) != 0:
            return vazio
        g = estado.Gamepad

        def stick(bruto):
            v = clamp(bruto / 32767.0, -1.0, 1.0)
            if abs(v) <= zona_morta:
                return 0.0
            # reescala para nao dar salto na saida da zona morta
            return ((abs(v) - zona_morta) / (1.0 - zona_morta)) * (1 if v > 0 else -1)

        def gatilho(bruto):
            v = clamp(bruto / 255.0)
            return 0.0 if v <= zona_gatilho else (v - zona_gatilho) / (1.0 - zona_gatilho)

        botoes = {nome for bit, nome in self.BOTOES.items() if g.wButtons & bit}
        leitura = {"lx": stick(g.sThumbLX), "ly": stick(g.sThumbLY),
                   "rx": stick(g.sThumbRX), "ry": stick(g.sThumbRY),
                   "lt": gatilho(g.bLeftTrigger), "rt": gatilho(g.bRightTrigger),
                   "botoes": botoes}
        leitura["ativo"] = bool(botoes) or any(
            abs(leitura[k]) > 0.02 for k in ("lx", "ly", "rx", "ry", "lt", "rt"))
        return leitura

    def fechar(self) -> None:
        self.slot = None


# --------------------------------------------------------------------------
# Controle virtual (vgamepad / ViGEmBus)
# --------------------------------------------------------------------------

ESTADO_PATH = os.path.join(HERE, "estado.json")


def carregar_estado() -> dict:
    """Memoria de execucao (slot usado, etc). Separada do config para nao
    misturar preferencia do usuario com detalhe de funcionamento."""
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def gravar_estado(dados: dict) -> None:
    try:
        atual = carregar_estado()
        atual.update(dados)
        with open(ESTADO_PATH, "w", encoding="utf-8") as fh:
            json.dump(atual, fh, indent=2)
    except Exception:
        pass


class VirtualPad:
    def __init__(self, slot_desejado=None):
        """slot_desejado: numero do slot XInput em que o controle deve nascer.

        O GTA V amarra o controle a um slot quando o detecta. Se a ponte for
        fechada e reaberta, o controle pode renascer em outro slot - o jogo
        continua olhando o antigo e parece que o volante morreu, sendo que so
        mudou de endereco. Como o ViGEm entrega sempre o menor slot livre, dá
        para escolher o destino ocupando os anteriores com controles
        descartaveis e soltando-os logo depois: o de verdade fica onde nasceu.
        """
        try:
            import vgamepad as vg
        except ImportError:
            raise RuntimeError(
                "Modulo 'vgamepad' nao instalado.\n"
                "  Rode:  pip install vgamepad\n"
                "  A instalacao abre o instalador do driver ViGEmBus - aceite o UAC."
            )
        except Exception as exc:
            raise RuntimeError(
                f"vgamepad falhou ao carregar ({exc}).\n"
                "Provavelmente o driver ViGEmBus nao esta instalado. Baixe em:\n"
                "  https://github.com/nefarius/ViGEmBus/releases"
            )
        self.vg = vg
        self._enchimento = self._ocupar_ate(vg, slot_desejado)
        lib = xinput_lib()
        antes = set(xinput_slots(lib)) if lib else set()
        self.pad = vg.VX360Gamepad()
        self.slot = self._descobrir_slot(lib, antes)
        self._soltar_enchimento()
        self.map = {
            "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            "Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            "LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
            "RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
            "BACK": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
            "START": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
            "LS": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
            "RS": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
            "DPAD_UP": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            "DPAD_DOWN": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            "DPAD_LEFT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
            "DPAD_RIGHT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
            "GUIDE": vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE,
        }
        # vibracao que o jogo manda para o controle (0..255) -> vira FFB no volante
        self.rumble = [0, 0]
        self._cb = self._on_notification
        try:
            self.pad.register_notification(callback_function=self._cb)
        except Exception:
            pass
        self._prime()

    @staticmethod
    def _ocupar_ate(vg, alvo) -> list:
        """Cria controles descartaveis ate que os slots abaixo do alvo estejam
        todos ocupados, para o proximo nascer exatamente no alvo."""
        try:
            alvo = int(alvo)
        except (TypeError, ValueError):
            return []
        lib = xinput_lib()
        if lib is None or alvo <= 0 or alvo > 3:
            return []
        enchimento = []
        for _ in range(4):
            ocupados = set(xinput_slots(lib))
            if all(s in ocupados for s in range(alvo)):
                break
            descartavel = vg.VX360Gamepad()
            descartavel.reset()
            descartavel.update()
            enchimento.append(descartavel)
            time.sleep(0.3)
        return enchimento

    def _descobrir_slot(self, lib, antes: set):
        """Qual slot XInput o controle ocupou.

        get_index() do vgamepad nao serve: logo apos a criacao ele devolve um
        numero que nem sempre corresponde ao slot real (medido: get_index()=1
        com o aparelho de fato no slot 2). O que nao mente e a diferenca entre
        os slots ocupados antes e depois.
        """
        if lib is not None:
            limite = time.perf_counter() + 2.0
            while time.perf_counter() < limite:
                novos = set(xinput_slots(lib)) - antes
                if novos:
                    return min(novos)
                time.sleep(0.05)
        try:
            return int(self.pad.get_index()) - 1
        except Exception:
            return None

    def _soltar_enchimento(self) -> None:
        if not self._enchimento:
            return
        self._enchimento.clear()
        import gc
        gc.collect()
        time.sleep(0.3)

    def _on_notification(self, client, target, large_motor, small_motor,
                         led_number, user_data):
        self.rumble[0] = large_motor
        self.rumble[1] = small_motor

    def _prime(self) -> None:
        """Tira o pad do estado inicial torto.

        Um VX360Gamepad recem-criado chega ao XInput com o analogico
        esquerdo em cerca de -10% e trava nesse valor: como relatorios
        identicos nao geram pacote novo, mandar so o neutro nao corrige, e
        o carro sai puxando para a esquerda. Mandamos um relatorio bem
        diferente e so entao o neutro, garantindo que o ultimo pacote
        entregue seja o nosso.
        """
        try:
            self.pad.reset()
            self.pad.left_joystick(x_value=12000, y_value=0)
            self.pad.update()
            time.sleep(0.05)
            self.pad.reset()
            self.pad.update()
            time.sleep(0.05)
        except Exception:
            pass

    def apply(self, steer: float, throttle: float, brake: float, pressed: set,
              ly: float = 0.0, rx: float = 0.0, ry: float = 0.0) -> None:
        p = self.pad
        p.reset()
        p.left_joystick_float(x_value_float=steer, y_value_float=ly)
        p.right_joystick_float(x_value_float=rx, y_value_float=ry)
        p.right_trigger_float(value_float=throttle)
        p.left_trigger_float(value_float=brake)
        for name in pressed:
            btn = self.map.get(name)
            if btn is not None:
                p.press_button(button=btn)
        p.update()

    def release_all(self) -> None:
        try:
            self.pad.reset()
            self.pad.update()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Force feedback pelo SDK gratuito da Logitech (opcional)
# --------------------------------------------------------------------------

DLL_NAME = "LogitechSteeringWheelEnginesWrapper.dll"

DLL_SEARCH = [
    os.path.join(HERE, DLL_NAME),
    os.path.join(HERE, "sdk", DLL_NAME),
    os.path.join(HERE, "sdk", "x64", DLL_NAME),
    os.path.join(r"C:\Program Files\Logitech\Gaming Software\SDK\Steering Wheel\x64", DLL_NAME),
    os.path.join(r"C:\Program Files\Logitech Gaming Software\SDK\Steering Wheel\x64", DLL_NAME),
]


def processo_da_janela(hwnd: int) -> str:
    """Nome do executavel dono da janela, em minusculas ('gta5.exe')."""
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        tamanho = wintypes.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(tamanho)):
            return os.path.basename(buf.value).lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    return ""


def find_game_window(class_name: str, title_fragment: str,
                     processos=None) -> int:
    """Devolve o HWND da janela do GTA V, ou 0 se o jogo nao estiver aberto.

    Casar so pelo titulo da falso positivo facil: qualquer janela com
    'Grand Theft Auto V' no nome (um navegador, um editor, uma pasta) seria
    confundida com o jogo, e o force feedback acabaria preso nela. Por isso
    conferimos tambem o executavel dono da janela.
    """
    user32 = ctypes.windll.user32
    processos = [p.lower() for p in (processos or [])]

    def serve(hwnd: int) -> bool:
        if not hwnd:
            return False
        if not processos:
            return True
        return processo_da_janela(hwnd) in processos

    if class_name:
        hwnd = user32.FindWindowW(class_name, None)
        if serve(hwnd):
            return hwnd

    achados = []
    if title_fragment:
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if title_fragment.lower() in buf.value.lower() and serve(hwnd):
                    achados.append(hwnd)
                    return False
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return achados[0] if achados else 0


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


def inicio_do_processo(hwnd: int):
    """Quando o processo dono da janela comecou (epoch), ou None.

    Serve para saber se o jogo ja estava aberto antes de o controle virtual
    existir - o GTA V so reconhece controles presentes no momento em que ele
    inicia, e essa e a causa mais comum de 'o volante nao funciona'.
    """
    if not hwnd:
        return None
    k32 = ctypes.windll.kernel32
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = k32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        cria, sai, kern, usu = (_FILETIME(), _FILETIME(), _FILETIME(), _FILETIME())
        if not k32.GetProcessTimes(handle, ctypes.byref(cria), ctypes.byref(sai),
                                   ctypes.byref(kern), ctypes.byref(usu)):
            return None
        ticks = (cria.high << 32) | cria.low     # 100ns desde 1601
        return ticks / 1e7 - 11644473600.0       # -> epoch unix
    except Exception:
        return None
    finally:
        k32.CloseHandle(handle)


def janela_do_jogo(cfg_ffb: dict) -> int:
    """find_game_window usando os campos do config."""
    return find_game_window(cfg_ffb.get("game_window_class", ""),
                            cfg_ffb.get("game_window_title", ""),
                            cfg_ffb.get("game_process") or [])


# --------------------------------------------------------------------------
# Force feedback pelo SDL (nao precisa do SDK da Logitech)
# --------------------------------------------------------------------------
#
# O SDL2 que vem junto com o pygame fala DirectInput com o volante e expoe
# efeitos de verdade - mola, amortecedor, forca constante, periodicos. Isso
# cobre o que o SDK da Logitech faria, sem nenhum download extra, e usando o
# mesmo dispositivo que ja estamos lendo.

SDL_INIT_HAPTIC = 0x00001000
SDL_HAPTIC_CONSTANT = 1 << 0
SDL_HAPTIC_SINE = 1 << 1
SDL_HAPTIC_SPRING = 1 << 7
SDL_HAPTIC_DAMPER = 1 << 8
SDL_HAPTIC_GAIN = 1 << 12
SDL_HAPTIC_INFINITY = 0xFFFFFFFF
SDL_HAPTIC_CARTESIAN = 1


class SDL_HapticDirection(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint8), ("dir", ctypes.c_int32 * 3)]


class SDL_HapticConstant(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint16), ("direction", SDL_HapticDirection),
        ("length", ctypes.c_uint32), ("delay", ctypes.c_uint16),
        ("button", ctypes.c_uint16), ("interval", ctypes.c_uint16),
        ("level", ctypes.c_int16),
        ("attack_length", ctypes.c_uint16), ("attack_level", ctypes.c_uint16),
        ("fade_length", ctypes.c_uint16), ("fade_level", ctypes.c_uint16),
    ]


class SDL_HapticPeriodic(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint16), ("direction", SDL_HapticDirection),
        ("length", ctypes.c_uint32), ("delay", ctypes.c_uint16),
        ("button", ctypes.c_uint16), ("interval", ctypes.c_uint16),
        ("period", ctypes.c_uint16), ("magnitude", ctypes.c_int16),
        ("offset", ctypes.c_int16), ("phase", ctypes.c_uint16),
        ("attack_length", ctypes.c_uint16), ("attack_level", ctypes.c_uint16),
        ("fade_length", ctypes.c_uint16), ("fade_level", ctypes.c_uint16),
    ]


class SDL_HapticCondition(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint16), ("direction", SDL_HapticDirection),
        ("length", ctypes.c_uint32), ("delay", ctypes.c_uint16),
        ("button", ctypes.c_uint16), ("interval", ctypes.c_uint16),
        ("right_sat", ctypes.c_uint16 * 3), ("left_sat", ctypes.c_uint16 * 3),
        ("right_coeff", ctypes.c_int16 * 3), ("left_coeff", ctypes.c_int16 * 3),
        ("deadband", ctypes.c_uint16 * 3), ("center", ctypes.c_int16 * 3),
    ]


class SDL_HapticEffect(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_uint16),
        ("constant", SDL_HapticConstant),
        ("periodic", SDL_HapticPeriodic),
        ("condition", SDL_HapticCondition),
        ("_espaco", ctypes.c_uint8 * 128),
    ]


def _carregar_sdl():
    """Pega a MESMA SDL2.dll que o pygame ja carregou no processo."""
    try:
        import pygame
    except ImportError:
        return None
    base = os.path.dirname(pygame.__file__)
    for nome in ("SDL2.dll", "libSDL2.dll", "libSDL2-2.0.so.0", "libSDL2.dylib"):
        caminho = os.path.join(base, nome)
        if os.path.exists(caminho):
            try:
                return ctypes.CDLL(caminho)
            except OSError:
                continue
    return None


_SDL_CACHE = {}


def _sdl_ligado():
    """SDL2 com as funcoes de joystick ja tipadas (carrega uma vez so)."""
    if "sdl" not in _SDL_CACHE:
        s = _carregar_sdl()
        if s is not None:
            try:
                s.SDL_JoystickFromInstanceID.restype = ctypes.c_void_p
                s.SDL_JoystickFromInstanceID.argtypes = [ctypes.c_int32]
                s.SDL_JoystickGetAttached.restype = ctypes.c_int
                s.SDL_JoystickGetAttached.argtypes = [ctypes.c_void_p]
            except Exception:
                s = None
        _SDL_CACHE["sdl"] = s
    return _SDL_CACHE["sdl"]


def joystick_conectado(js) -> bool:
    """O SDL ainda tem o volante?

    Vira False quando ele e desconectado e tambem quando outro programa
    toma o dispositivo (o force feedback do DirectInput e exclusivo: quem
    abre por ultimo ganha, e o perdedor fica lendo valores congelados sem
    receber erro nenhum).
    """
    s = _sdl_ligado()
    if s is None or js is None:
        return True          # sem como saber: melhor nao dar alarme falso
    try:
        ponteiro = s.SDL_JoystickFromInstanceID(ctypes.c_int32(js.get_instance_id()))
        if not ponteiro:
            return False
        return bool(s.SDL_JoystickGetAttached(ctypes.c_void_p(ponteiro)))
    except Exception:
        return True


class SdlFFB:
    """Force feedback do volante via SDL_Haptic.

    Mantem tres efeitos rodando em loop infinito e so reenvia parametros
    quando eles mudam de verdade:

      mola       - puxa o volante de volta ao centro (peso na curva)
      amortecedor- tira a soltura, deixa o volante encorpado
      vibracao   - a trepidacao que o jogo manda para o controle (batida,
                   meio-fio, terra) vira efeito periodico no aro
    """

    def __init__(self, joystick=None):
        self.sdl = _carregar_sdl()
        self.haptic = None
        self.erro = ""
        self.via = ""
        self.suportado = 0
        self.ids = {"mola": -1, "amortecedor": -1, "vibracao": -1}
        self._ultimo = {}

        if self.sdl is None:
            self.erro = "SDL2 do pygame nao encontrada"
            return
        s = self.sdl
        s.SDL_GetError.restype = ctypes.c_char_p
        s.SDL_HapticOpen.restype = ctypes.c_void_p
        s.SDL_HapticOpen.argtypes = [ctypes.c_int]
        s.SDL_HapticOpenFromJoystick.restype = ctypes.c_void_p
        s.SDL_HapticOpenFromJoystick.argtypes = [ctypes.c_void_p]
        s.SDL_JoystickFromInstanceID.restype = ctypes.c_void_p
        s.SDL_JoystickFromInstanceID.argtypes = [ctypes.c_int32]
        s.SDL_HapticQuery.restype = ctypes.c_uint
        s.SDL_HapticQuery.argtypes = [ctypes.c_void_p]
        s.SDL_HapticNewEffect.restype = ctypes.c_int
        s.SDL_HapticNewEffect.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(SDL_HapticEffect)]
        s.SDL_HapticUpdateEffect.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.POINTER(SDL_HapticEffect)]
        s.SDL_HapticRunEffect.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_uint32]
        s.SDL_HapticStopEffect.argtypes = [ctypes.c_void_p, ctypes.c_int]
        s.SDL_HapticDestroyEffect.argtypes = [ctypes.c_void_p, ctypes.c_int]
        s.SDL_HapticSetGain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        s.SDL_HapticClose.argtypes = [ctypes.c_void_p]

        if s.SDL_InitSubSystem(SDL_INIT_HAPTIC) != 0:
            self.erro = self._msg() or "nao consegui iniciar o subsistema haptico"
            return

        # de preferencia abrimos o haptico DO joystick que estamos lendo;
        # a lista global costuma ter entradas fantasma do mesmo volante
        if joystick is not None:
            try:
                ponteiro = s.SDL_JoystickFromInstanceID(
                    ctypes.c_int32(joystick.get_instance_id()))
                if ponteiro:
                    h = s.SDL_HapticOpenFromJoystick(ctypes.c_void_p(ponteiro))
                    if h:
                        self.haptic = h
                        self.via = "joystick"
            except Exception:
                pass
        if self.haptic is None:
            for i in range(s.SDL_NumHaptics()):
                h = s.SDL_HapticOpen(i)
                if h:
                    self.haptic = h
                    self.via = f"indice {i}"
                    break
        if self.haptic is None:
            self.erro = self._msg() or "nenhum dispositivo haptico abriu"
            return

        self.suportado = s.SDL_HapticQuery(ctypes.c_void_p(self.haptic))
        if self.suportado & SDL_HAPTIC_GAIN:
            s.SDL_HapticSetGain(ctypes.c_void_p(self.haptic), 100)
        self._criar_efeitos()

    # -- infra ------------------------------------------------------------

    def _msg(self) -> str:
        try:
            return (self.sdl.SDL_GetError() or b"").decode(errors="replace")
        except Exception:
            return ""

    def disponivel(self) -> bool:
        return self.haptic is not None and any(v >= 0 for v in self.ids.values())

    def _direcao(self) -> SDL_HapticDirection:
        d = SDL_HapticDirection()
        d.type = SDL_HAPTIC_CARTESIAN
        d.dir[0] = 1
        return d

    def _nova_condicao(self, tipo: int) -> int:
        efeito = SDL_HapticEffect()
        c = efeito.condition
        c.type = tipo
        c.direction = self._direcao()
        c.length = SDL_HAPTIC_INFINITY
        for eixo in range(3):
            c.right_sat[eixo] = 0
            c.left_sat[eixo] = 0
            c.right_coeff[eixo] = 0
            c.left_coeff[eixo] = 0
            c.deadband[eixo] = 0
            c.center[eixo] = 0
        return self.sdl.SDL_HapticNewEffect(ctypes.c_void_p(self.haptic),
                                            ctypes.byref(efeito))

    def _nova_vibracao(self) -> int:
        efeito = SDL_HapticEffect()
        p = efeito.periodic
        p.type = SDL_HAPTIC_SINE
        p.direction = self._direcao()
        p.length = SDL_HAPTIC_INFINITY
        p.period = 60          # ms - trepidacao grossa, tipo pista ruim
        p.magnitude = 0
        return self.sdl.SDL_HapticNewEffect(ctypes.c_void_p(self.haptic),
                                            ctypes.byref(efeito))

    def _criar_efeitos(self) -> None:
        if self.suportado & SDL_HAPTIC_SPRING:
            self.ids["mola"] = self._nova_condicao(SDL_HAPTIC_SPRING)
        if self.suportado & SDL_HAPTIC_DAMPER:
            self.ids["amortecedor"] = self._nova_condicao(SDL_HAPTIC_DAMPER)
        if self.suportado & SDL_HAPTIC_SINE:
            self.ids["vibracao"] = self._nova_vibracao()
        for chave, ident in self.ids.items():
            if ident >= 0:
                self.sdl.SDL_HapticRunEffect(ctypes.c_void_p(self.haptic),
                                             ident, SDL_HAPTIC_INFINITY)

    # -- uso --------------------------------------------------------------

    def invalidar(self) -> None:
        """Esquece o ultimo envio, forcando a proxima aplicacao a ir.

        Usado depois de mexer no controle virtual: criar e remover
        dispositivos XInput pode perturbar a sessao DirectInput do volante,
        e o efeito some sem avisar ninguem.
        """
        self._ultimo.clear()

    def aplicar(self, mola: int, mola_sat: int, amortecedor: int,
                vibracao: float, ganho: int = 100) -> None:
        """Todos os parametros em 0..100 (vibracao em 0.0..1.0).

        Reenvia tudo pelo menos uma vez por segundo mesmo sem mudanca. Esse
        reenvio e a rede de seguranca do force feedback: se o efeito for
        perdido - por interferencia de outro programa, por reenumeracao de
        dispositivo, ou porque a primeira aplicacao caiu antes de o volante
        estar pronto -, na proxima volta ele volta sozinho. Antes disso, um
        envio perdido ficava perdido para sempre, porque o valor "ja tinha
        sido aplicado" e nunca era reenviado.
        """
        if not self.disponivel():
            return
        alvo = (int(mola), int(mola_sat), int(amortecedor),
                int(vibracao * 100), int(ganho))
        agora = time.perf_counter()
        igual = alvo == self._ultimo.get("alvo")
        if igual and (agora - self._ultimo.get("quando", 0.0)) < 1.0:
            return
        self._ultimo["alvo"] = alvo
        self._ultimo["quando"] = agora
        s, h = self.sdl, ctypes.c_void_p(self.haptic)

        if self.suportado & SDL_HAPTIC_GAIN:
            s.SDL_HapticSetGain(h, max(0, min(100, int(ganho))))

        falhou = False
        if self.ids["mola"] >= 0:
            falhou |= self._atualizar_condicao(self.ids["mola"], SDL_HAPTIC_SPRING,
                                               mola, mola_sat) < 0
        if self.ids["amortecedor"] >= 0:
            falhou |= self._atualizar_condicao(self.ids["amortecedor"],
                                               SDL_HAPTIC_DAMPER,
                                               amortecedor, 100) < 0
        if falhou:
            # o volante recusou a atualizacao: os efeitos foram perdidos em
            # algum momento. Recria e deixa a proxima volta reaplicar.
            self._reconstruir()
            return
        if self.ids["vibracao"] >= 0:
            efeito = SDL_HapticEffect()
            p = efeito.periodic
            p.type = SDL_HAPTIC_SINE
            p.direction = self._direcao()
            p.length = SDL_HAPTIC_INFINITY
            p.period = 60
            p.magnitude = int(max(0.0, min(1.0, vibracao)) * 32767)
            s.SDL_HapticUpdateEffect(h, self.ids["vibracao"], ctypes.byref(efeito))

    def _reconstruir(self) -> None:
        """Destroi e recria os efeitos do zero."""
        if self.haptic is None:
            return
        try:
            for ident in self.ids.values():
                if ident >= 0:
                    self.sdl.SDL_HapticDestroyEffect(ctypes.c_void_p(self.haptic),
                                                     ident)
        except Exception:
            pass
        self.ids = {k: -1 for k in self.ids}
        self._ultimo.clear()
        try:
            self._criar_efeitos()
        except Exception:
            pass

    def _atualizar_condicao(self, ident: int, tipo: int,
                            coeficiente: int, saturacao: int) -> int:
        efeito = SDL_HapticEffect()
        c = efeito.condition
        c.type = tipo
        c.direction = self._direcao()
        c.length = SDL_HAPTIC_INFINITY
        coef = int(max(0, min(100, coeficiente)) / 100.0 * 32767)
        sat = int(max(0, min(100, saturacao)) / 100.0 * 65535)
        for eixo in range(3):
            c.right_coeff[eixo] = coef
            c.left_coeff[eixo] = coef
            c.right_sat[eixo] = sat
            c.left_sat[eixo] = sat
            c.deadband[eixo] = 0
            c.center[eixo] = 0
        return self.sdl.SDL_HapticUpdateEffect(ctypes.c_void_p(self.haptic),
                                               ident, ctypes.byref(efeito))

    def parar(self) -> None:
        if self.haptic is None:
            return
        for ident in self.ids.values():
            if ident >= 0:
                self.sdl.SDL_HapticStopEffect(ctypes.c_void_p(self.haptic), ident)

    def fechar(self) -> None:
        if self.haptic is None:
            return
        try:
            for ident in self.ids.values():
                if ident >= 0:
                    self.sdl.SDL_HapticDestroyEffect(ctypes.c_void_p(self.haptic),
                                                     ident)
            self.sdl.SDL_HapticClose(ctypes.c_void_p(self.haptic))
        except Exception:
            pass
        self.haptic = None
        self.ids = {k: -1 for k in self.ids}


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _XINPUT_GAMEPAD)]


def xinput_lib():
    """Carrega a XInput - a mesma API por onde o GTA V le o controle."""
    for nome in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(nome)
        except OSError:
            continue
    return None


def xinput_slots(lib) -> list:
    ocupados = []
    for i in range(4):
        st = _XINPUT_STATE()
        if lib.XInputGetState(i, ctypes.byref(st)) == 0:
            ocupados.append(i)
    return ocupados


def xinput_read(lib, slot: int) -> dict:
    """Le um slot do XInput e devolve os valores ja normalizados."""
    st = _XINPUT_STATE()
    if lib.XInputGetState(slot, ctypes.byref(st)) != 0:
        return {}
    g = st.Gamepad
    return {
        "steer": g.sThumbLX / 32767.0,
        "throttle": g.bRightTrigger / 255.0,
        "brake": g.bLeftTrigger / 255.0,
        "buttons": g.wButtons,
    }


class LogitechFFB:
    """Envelopa o 'Logitech Steering Wheel SDK' (gratuito) via ctypes.

    O SDK nao vem junto com o G HUB: baixe o zip do SDK, pegue o
    LogitechSteeringWheelEnginesWrapper.dll de 64 bits e coloque na pasta
    sdk/ deste projeto. Sem a DLL o script roda normalmente, so sem FFB.
    """

    def __init__(self, cfg_ffb: dict):
        self.cfg = cfg_ffb
        self.idx = int(cfg_ffb.get("device_index", 0))
        self.dll = None
        self.path = ""
        self.ok = False
        self.hwnd = 0
        self._last_road = -1
        self._last_dirt = -1
        self._next_window_check = 0.0

        wanted = cfg_ffb.get("dll") or ""
        for cand in ([wanted] if wanted else []) + DLL_SEARCH:
            if cand and os.path.exists(cand):
                try:
                    self.dll = ctypes.WinDLL(cand)
                    self.path = cand
                    break
                except OSError as exc:
                    print(f"  ! nao consegui carregar {cand}: {exc}")
        if self.dll is None:
            return

        d = self.dll
        try:
            d.LogiSteeringInitialize.argtypes = [ctypes.c_bool]
            d.LogiSteeringInitialize.restype = ctypes.c_bool
            d.LogiUpdate.restype = ctypes.c_bool
            d.LogiIsConnected.argtypes = [ctypes.c_int]
            d.LogiIsConnected.restype = ctypes.c_bool
            d.LogiPlaySpringForce.argtypes = [ctypes.c_int] * 4
            d.LogiPlayDamperForce.argtypes = [ctypes.c_int, ctypes.c_int]
            d.LogiStopSpringForce.argtypes = [ctypes.c_int]
            d.LogiStopDamperForce.argtypes = [ctypes.c_int]
            d.LogiPlayBumpyRoadEffect.argtypes = [ctypes.c_int, ctypes.c_int]
            d.LogiStopBumpyRoadEffect.argtypes = [ctypes.c_int]
            d.LogiPlayDirtRoadEffect.argtypes = [ctypes.c_int, ctypes.c_int]
            d.LogiStopDirtRoadEffect.argtypes = [ctypes.c_int]
            d.LogiSetOperatingRange.argtypes = [ctypes.c_int, ctypes.c_int]
            d.LogiPlayLeds.argtypes = [ctypes.c_int, ctypes.c_float,
                                       ctypes.c_float, ctypes.c_float]
            d.LogiSteeringShutdown.restype = None
        except AttributeError as exc:
            print(f"  ! DLL do SDK sem as funcoes esperadas: {exc}")
            self.dll = None
            return

        self._initialize()

    def _initialize(self) -> None:
        d = self.dll
        if d is None:
            return
        hwnd = 0
        if self.cfg.get("attach_to_game_window", True):
            hwnd = janela_do_jogo(self.cfg)
        started = False
        if hwnd and hasattr(d, "LogiSteeringInitializeWithWindow"):
            try:
                d.LogiSteeringInitializeWithWindow.argtypes = [ctypes.c_bool, wintypes.HWND]
                d.LogiSteeringInitializeWithWindow.restype = ctypes.c_bool
                started = bool(d.LogiSteeringInitializeWithWindow(True, hwnd))
            except Exception:
                started = False
        if not started:
            try:
                started = bool(d.LogiSteeringInitialize(True))
            except Exception:
                started = False

        self.hwnd = hwnd
        self.ok = started
        if started:
            rng = int(self.cfg.get("operating_range_deg", 0) or 0)
            if rng:
                try:
                    d.LogiUpdate()
                    d.LogiSetOperatingRange(self.idx, rng)
                except Exception:
                    pass

    def reattach_if_needed(self) -> None:
        """Re-inicializa o SDK quando a janela do GTA V aparece ou muda."""
        if self.dll is None or not self.cfg.get("attach_to_game_window", True):
            return
        now = time.perf_counter()
        if now < self._next_window_check:
            return
        self._next_window_check = now + 3.0
        hwnd = janela_do_jogo(self.cfg)
        if hwnd and hwnd != self.hwnd:
            try:
                self.dll.LogiSteeringShutdown()
            except Exception:
                pass
            self.ok = False
            self._last_road = self._last_dirt = -1
            self._initialize()

    def update(self, rumble_large: int, rumble_small: int, throttle: float) -> None:
        if not self.ok or self.dll is None:
            return
        d = self.dll
        try:
            d.LogiUpdate()
            if not d.LogiIsConnected(self.idx):
                return

            spring = self.cfg.get("spring", {})
            if spring.get("enabled", True):
                d.LogiPlaySpringForce(self.idx,
                                      int(spring.get("offset", 0)),
                                      int(spring.get("saturation", 70)),
                                      int(spring.get("coefficient", 55)))
            damper = self.cfg.get("damper", {})
            if damper.get("enabled", True):
                d.LogiPlayDamperForce(self.idx, int(damper.get("coefficient", 25)))

            r2r = self.cfg.get("rumble_to_road", {})
            if r2r.get("enabled", True):
                gain = float(r2r.get("gain", 100)) / 100.0
                road = int(clamp(rumble_large / 255.0 * gain) * 100)
                dirt = int(clamp(rumble_small / 255.0 * gain) * 100)
                if road != self._last_road:
                    if road > 2:
                        d.LogiPlayBumpyRoadEffect(self.idx, road)
                    else:
                        d.LogiStopBumpyRoadEffect(self.idx)
                    self._last_road = road
                if dirt != self._last_dirt:
                    if dirt > 2:
                        d.LogiPlayDirtRoadEffect(self.idx, dirt)
                    else:
                        d.LogiStopDirtRoadEffect(self.idx)
                    self._last_dirt = dirt

            if self.cfg.get("leds_from_throttle", False):
                d.LogiPlayLeds(self.idx, throttle * 100.0, 15.0, 95.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        if self.dll is None:
            return
        try:
            for stop in ("LogiStopSpringForce", "LogiStopDamperForce",
                         "LogiStopBumpyRoadEffect", "LogiStopDirtRoadEffect"):
                getattr(self.dll, stop)(self.idx)
            self.dll.LogiSteeringShutdown()
        except Exception:
            pass
        self.ok = False


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------

def cmd_listar() -> int:
    import pygame
    pygame.init()
    pygame.joystick.init()
    n = pygame.joystick.get_count()
    print(f"Dispositivos de entrada encontrados: {n}\n")
    for i in range(n):
        j = pygame.joystick.Joystick(i)
        j.init()
        print(f"  [{i}] {j.get_name()}")
        print(f"       eixos={j.get_numaxes()}  botoes={j.get_numbuttons()}  "
              f"hats={j.get_numhats()}  guid={j.get_guid()}")
    return 0


def cmd_monitorar(cfg: dict) -> int:
    wheel = Wheel(cfg)
    print(f"Lendo: {wheel.name}")
    print("Gire o volante e pise nos pedais. Ctrl+C para sair.\n")
    try:
        while True:
            wheel.pump()
            axes = " ".join(f"a{i}={v:+.3f}" for i, v in enumerate(wheel.axes()))
            btns = ",".join(str(i) for i, b in enumerate(wheel.buttons()) if b) or "-"
            hx, hy = wheel.hat()
            sys.stdout.write(f"\r{axes} | hat=({hx},{hy}) | botoes: {btns}      ")
            sys.stdout.flush()
            time.sleep(0.03)
    except KeyboardInterrupt:
        print("\n")
    return 0


def _prompt(msg: str) -> None:
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def _detect_moved_axis(baseline: list, current: list) -> int:
    deltas = [abs(c - b) for b, c in zip(baseline, current)]
    best = max(range(len(deltas)), key=lambda i: deltas[i]) if deltas else 0
    if not deltas or deltas[best] < 0.15:
        return -1
    return best


def deduzir_repouso(minimo: float, maximo: float, final: float, baseline=None):
    """Decide qual extremo do curso de um pedal e o repouso.

    O sinal mais confiavel e a leitura de repouso capturada antes de tudo
    (o pedal solto). Se ela for ambigua - o eixo pode reportar 0 ate ser
    tocado pela primeira vez -, caimos para onde o pedal parou no fim da
    amostragem; essa regra sozinha erra quando a pessoa ainda esta pisando
    no momento em que a contagem acaba, e ai o pedal fica invertido.

    Devolve (repouso, fundo).
    """
    curso = maximo - minimo
    if curso <= 0:
        return minimo, maximo
    if baseline is not None:
        dist_min, dist_max = abs(baseline - minimo), abs(baseline - maximo)
        if abs(dist_min - dist_max) > 0.25 * curso:
            repouso = minimo if dist_min < dist_max else maximo
            return repouso, (maximo if repouso == minimo else minimo)
    repouso = minimo if abs(final - minimo) <= abs(final - maximo) else maximo
    return repouso, (maximo if repouso == minimo else minimo)


def _sample_range(wheel: "Wheel", seconds: float = 5.0):
    """Acompanha todos os eixos por alguns segundos.

    Devolve (minimos, maximos, leitura_final). Amostrar o curso inteiro e
    depois olhar onde o eixo parou e mais confiavel do que ler o repouso
    antes de o eixo ser tocado: varios volantes so reportam o valor real do
    pedal depois do primeiro movimento.
    """
    vals = wheel.settle(0.1)
    mins, maxs = list(vals), list(vals)
    end = time.perf_counter() + seconds
    last_shown = -1
    while time.perf_counter() < end:
        wheel.pump()
        for i, v in enumerate(wheel.axes()):
            if v < mins[i]:
                mins[i] = v
            if v > maxs[i]:
                maxs[i] = v
        restante = int(end - time.perf_counter()) + 1
        if restante != last_shown:
            last_shown = restante
            sys.stdout.write(f"\r   amostrando... {restante}s ")
            sys.stdout.flush()
        time.sleep(0.005)
    sys.stdout.write("\r" + " " * 30 + "\r")
    return mins, maxs, wheel.settle(0.4)


def cmd_calibrar(cfg: dict) -> int:
    wheel = Wheel(cfg)
    print(f"\nCalibrando: {wheel.name}  ({wheel.num_axes} eixos)")
    print("Siga as instrucoes; aperte ENTER em cada etapa.\n")

    try:
        _prompt("1) Deixe o volante CENTRALIZADO e os pedais SOLTOS, entao ENTER... ")
        baseline = wheel.settle(0.4)
        print(f"   repouso: {[round(v, 3) for v in baseline]}")

        _prompt("\n2) Gire o volante TODO para a ESQUERDA, SEGURE e aperte ENTER... ")
        left_read = wheel.settle(0.2)
        steer_axis = _detect_moved_axis(baseline, left_read)
        if steer_axis < 0:
            print("   ! nao detectei movimento. Abortando.")
            return 1
        left_val = left_read[steer_axis]
        print(f"   eixo do volante = {steer_axis}   valor a esquerda = {left_val:+.3f}")

        _prompt("\n3) Agora gire TODO para a DIREITA, SEGURE e aperte ENTER... ")
        right_val = wheel.settle(0.2)[steer_axis]
        print(f"   valor a direita = {right_val:+.3f}")

        _prompt("\n4) Solte o volante para centralizar e aperte ENTER... ")
        center_val = wheel.settle(0.4)[steer_axis]
        print(f"   centro = {center_val:+.3f}")

        pedals = {}
        usados = {steer_axis}
        for numero, (key, label) in enumerate((("throttle", "ACELERADOR"),
                                               ("brake", "FREIO"),
                                               ("clutch", "EMBREAGEM")), start=5):
            _prompt(f"\n{numero}) {label}: aperte ENTER e entao pise FUNDO e SOLTE "
                    f"esse pedal umas 3 vezes\n   (se voce nao tem esse pedal, "
                    f"so aperte ENTER e nao encoste em nada)... ")
            mins, maxs, final = _sample_range(wheel, 5.0)

            candidatos = [i for i in range(wheel.num_axes) if i not in usados]
            if not candidatos:
                print(f"   - {label} pulado (sem eixos livres)")
                continue
            idx = max(candidatos, key=lambda i: maxs[i] - mins[i])
            curso = maxs[idx] - mins[idx]
            if curso < 0.30:
                print(f"   - {label} pulado (nao vi movimento)")
                continue

            rest, full = deduzir_repouso(mins[idx], maxs[idx], final[idx],
                                         baseline[idx] if idx < len(baseline) else None)

            usados.add(idx)
            pedals[key] = {"index": idx, "rest": rest, "full": full}
            print(f"   eixo {idx}: solto={rest:+.3f}  fundo={full:+.3f}  "
                  f"(curso {curso:.2f})")

    except KeyboardInterrupt:
        print("\nCalibragem cancelada.")
        return 1

    cfg["device"]["index"] = wheel.index
    cfg["axes"]["steering"] = {
        "index": steer_axis,
        "center": round(center_val, 4),
        "left": round(left_val, 4),
        "right": round(right_val, 4),
    }
    for key, data in pedals.items():
        cfg["axes"][key] = {
            "index": data["index"],
            "rest": round(data["rest"], 4),
            "full": round(data["full"], 4),
        }
    for key in ("throttle", "brake", "clutch"):
        if key not in pedals:
            cfg["axes"][key]["index"] = -1

    print("\nCalibragem concluida.")
    save_config(cfg)
    return 0


def cmd_calibrar_botoes(cfg: dict) -> int:
    import pygame
    wheel = Wheel(cfg)
    acoes = [
        ("A", "freio de mao"),
        ("Y", "entrar / sair do veiculo"),
        ("B", "olhar para tras"),
        ("X", "acao / interagir"),
        ("LS", "buzina"),
        ("RS", "botao do analogico direito"),
        ("LB", "LB"),
        ("RB", "RB"),
        ("DPAD_LEFT", "radio anterior"),
        ("DPAD_RIGHT", "proxima radio"),
        ("BACK", "trocar camera"),
        ("START", "pausa / menu"),
    ]
    print(f"\nMapeando botoes de: {wheel.name}")
    print("Para cada acao, aperte o botao desejado no volante.")
    print("Espere 4 segundos sem apertar nada para pular uma acao.\n")

    novo = {}
    try:
        for alvo, descricao in acoes:
            print(f"  {alvo:<11} ({descricao}): aperte agora...", end="", flush=True)
            pygame.event.clear()
            deadline = time.perf_counter() + 4.0
            escolhido = None
            while time.perf_counter() < deadline:
                for ev in pygame.event.get():
                    if ev.type == pygame.JOYBUTTONDOWN:
                        escolhido = ev.button
                        break
                if escolhido is not None:
                    break
                time.sleep(0.01)
            if escolhido is None:
                print(" pulado")
                continue
            novo[str(escolhido)] = alvo
            print(f" botao {escolhido}")
            # espera soltar, para nao capturar o mesmo botao na proxima acao
            while any(wheel.buttons()):
                wheel.pump()
                time.sleep(0.01)
            pygame.event.clear()
    except KeyboardInterrupt:
        print("\nMapeamento cancelado.")
        return 1

    if not novo:
        print("\nNada mapeado, configuracao mantida.")
        return 0
    cfg["buttons"] = novo
    print("\nMapeamento concluido.")
    save_config(cfg)
    return 0


def cmd_rodar(cfg: dict, usar_ffb: bool, silencioso: bool) -> int:
    wheel = Wheel(cfg)
    pad = VirtualPad()

    ffb = None
    if usar_ffb and cfg["ffb"].get("enabled", True):
        ffb = SdlFFB(wheel.js)
        if ffb.disponivel():
            print("  + force feedback ativo via SDL: "
                  + ", ".join(n for n, i in ffb.ids.items() if i >= 0))
        else:
            print(f"  i sem force feedback ({ffb.erro or 'volante sem efeitos'})")
            ffb = None

    ax = cfg["axes"]
    st_shape = cfg["steering_shaping"]
    pd_shape = cfg["pedal_shaping"]
    clutch_cfg = cfg["clutch"]
    btn_map = {int(k): v for k, v in cfg["buttons"].items() if v in BUTTON_NAMES}
    hat_dpad = cfg.get("hat_as_dpad", True)

    print(f"\n  Volante : {wheel.name}")
    print(f"  Saida   : controle Xbox 360 virtual (ViGEmBus)")
    print(f"  Volante : eixo {ax['steering']['index']} | "
          f"acel {ax['throttle']['index']} | freio {ax['brake']['index']} | "
          f"embreagem {ax['clutch']['index']}")
    print(f"  Trava   : {st_shape['soft_lock_deg']} de {st_shape['wheel_range_deg']} graus")
    print("\n  Rodando. Deixe esta janela aberta e abra o GTA V. Ctrl+C para parar.\n")

    period = 1.0 / max(30, int(cfg.get("loop_hz", 250)))
    next_tick = time.perf_counter()
    next_print = 0.0

    # Trava de seguranca: um pedal so passa a valer depois de ser visto em
    # repouso pelo menos uma vez. Alguns volantes reportam 0 ate o primeiro
    # toque, e sem isso o carro sairia acelerando sozinho na largada.
    armado = {k: ax[k]["index"] < 0 for k in ("throttle", "brake", "clutch")}

    def ler_pedal(key: str) -> float:
        cal = ax[key]
        if cal["index"] < 0:
            return 0.0
        v = map_pedal(wheel.axis(cal["index"]), cal, pd_shape)
        if not armado[key]:
            if v <= 0.08:
                armado[key] = True
            else:
                return 0.0
        return v

    try:
        while True:
            wheel.pump()

            steer = map_steering(wheel.axis(ax["steering"]["index"]),
                                 ax["steering"], st_shape)
            throttle = ler_pedal("throttle")
            brake = ler_pedal("brake")
            clutch = ler_pedal("clutch")

            pressed = set()
            botoes = wheel.buttons()
            for i, is_down in enumerate(botoes):
                if is_down and i in btn_map:
                    pressed.add(btn_map[i])

            if hat_dpad and wheel.num_hats:
                hx, hy = wheel.hat()
                if hx > 0:
                    pressed.add("DPAD_RIGHT")
                elif hx < 0:
                    pressed.add("DPAD_LEFT")
                if hy > 0:
                    pressed.add("DPAD_UP")
                elif hy < 0:
                    pressed.add("DPAD_DOWN")

            modo = clutch_cfg.get("mode", "handbrake")
            if modo != "none" and clutch >= float(clutch_cfg.get("threshold", 0.45)):
                pressed.add("A" if modo == "handbrake" else modo)

            pad.apply(steer, throttle, brake, pressed)

            if ffb is not None:
                cfg_ffb = cfg["ffb"]
                r2r = cfg_ffb.get("rumble_to_road", {})
                vibracao = 0.0
                if r2r.get("enabled", True):
                    vibracao = clamp(max(pad.rumble) / 255.0
                                     * float(r2r.get("gain", 100)) / 100.0)
                ffb.aplicar(cfg_ffb["spring"].get("coefficient", 0),
                            cfg_ffb["spring"].get("saturation", 70),
                            cfg_ffb["damper"].get("coefficient", 0),
                            vibracao, cfg_ffb.get("gain", 100))

            now = time.perf_counter()
            if not silencioso and now >= next_print:
                next_print = now + 0.08
                barra = int((steer + 1) * 15)
                vis = "[" + "-" * barra + "|" + "-" * (30 - barra) + "]"
                faltam = [k for k, v in armado.items() if not v]
                aviso = ("  << pise e solte: " + ", ".join(faltam)) if faltam else ""
                sys.stdout.write(
                    f"\r  {vis} dir={steer:+.2f}  acel={throttle:.2f}  "
                    f"freio={brake:.2f}  emb={clutch:.2f}  "
                    f"vibra={pad.rumble[0]:>3}/{pad.rumble[1]:>3}{aviso}      ")
                sys.stdout.flush()

            next_tick += period
            sobra = next_tick - time.perf_counter()
            if sobra > 0:
                time.sleep(sobra)
            else:
                next_tick = time.perf_counter()

    except KeyboardInterrupt:
        print("\n\n  Encerrando...")
    finally:
        pad.release_all()
        if ffb is not None:
            ffb.parar()
            ffb.fechar()
    return 0


def _marca(ok: bool, texto: str, detalhe: str = "") -> bool:
    print(f"  [{'ok' if ok else '--'}] {texto}" + (f"  ->  {detalhe}" if detalhe else ""))
    return ok


def cmd_testar(cfg: dict) -> int:
    """Confere a cadeia inteira: volante -> Python -> pad virtual -> FFB."""
    import platform

    print("\n=== Diagnostico G29 -> GTA V ===\n")
    pendencias = []

    bits = 64 if sys.maxsize > 2 ** 32 else 32
    _marca(bits == 64, f"Python {platform.python_version()} ({bits} bits)",
           "" if bits == 64 else "o SDK de FFB precisa de Python 64 bits")

    # --- volante -----------------------------------------------------------
    try:
        wheel = Wheel(cfg)
        _marca(True, f"Volante: {wheel.name}",
               f"{wheel.num_axes} eixos, {wheel.num_buttons} botoes, "
               f"{wheel.num_hats} hat")
    except RuntimeError as exc:
        wheel = None
        _marca(False, "Volante nao encontrado", str(exc).split(".")[0])
        pendencias.append("ligue o G29 e confira o G HUB")

    # --- calibragem --------------------------------------------------------
    tem_config = os.path.exists(CONFIG_PATH)
    calibrado = tem_config and cfg["axes"] != DEFAULT_CONFIG["axes"]
    _marca(calibrado, "Calibragem gravada",
           "" if calibrado else "rode  python g29_gtav.py --calibrar")
    if not calibrado:
        pendencias.append("calibrar (aba Calibragem da interface, ou --calibrar)")

    if wheel is not None and calibrado:
        ax = cfg["axes"]
        faltando = [k for k in ("throttle", "brake", "clutch") if ax[k]["index"] < 0]
        _marca(not faltando, "Pedais mapeados",
               "sem: " + ", ".join(faltando) if faltando else "acelerador, freio, embreagem")

    # --- force feedback ----------------------------------------------------
    # Antes do controle virtual de proposito: a reenumeracao de joysticks
    # logo abaixo invalida o handle do volante, e ai o haptico nao abre.
    if wheel is None:
        _marca(False, "Force feedback", "sem volante para testar")
    else:
        ffb = SdlFFB(wheel.js)
        if ffb.disponivel():
            _marca(True, "Force feedback via SDL (sem SDK externo)",
                   "efeitos: " + ", ".join(n for n, i in ffb.ids.items() if i >= 0))
        else:
            _marca(False, "Force feedback", ffb.erro or "volante sem efeitos")
            pendencias.append("force feedback indisponivel")
        ffb.fechar()

    # --- controle virtual --------------------------------------------------
    import pygame
    pygame.init()
    pygame.joystick.init()
    antes = {pygame.joystick.Joystick(i).get_name()
             for i in range(pygame.joystick.get_count())}
    lib = xinput_lib()
    slots_antes = xinput_slots(lib) if lib else []
    pad = None
    try:
        pad = VirtualPad()
        _marca(True, "vgamepad + driver ViGEmBus")
        pad.apply(0.0, 0.0, 0.0, set())
        time.sleep(1.5)
        pygame.joystick.quit()
        pygame.joystick.init()
        pygame.event.pump()
        depois = {pygame.joystick.Joystick(i).get_name()
                  for i in range(pygame.joystick.get_count())}
        novos = depois - antes
        _marca(bool(novos), "Windows enxerga o controle virtual",
               ", ".join(novos) if novos else
               "o pad foi criado mas nao apareceu na lista de controles")
        if not novos:
            pendencias.append("reiniciar o PC apos instalar o ViGEmBus")

        # Conferencia pelo XInput: e por aqui que o GTA V le o controle.
        # (Nao da para conferir valor pelo pygame: o caminho RawInput do SDL
        # devolve analogico e botoes zerados para pads virtuais.)
        if lib is None:
            _marca(False, "XInput", "nenhuma xinput*.dll encontrada")
        else:
            novos_slots = [s for s in xinput_slots(lib) if s not in slots_antes]
            slot = novos_slots[0] if novos_slots else None
            if slot is None:
                _marca(False, "Slot XInput do controle virtual", "nao apareceu")
                pendencias.append("reiniciar o PC apos instalar o ViGEmBus")
            else:
                pad.apply(0.5, 0.75, 0.25, {"A"})
                time.sleep(0.15)
                v = xinput_read(lib, slot)
                bateu = (v and abs(v["steer"] - 0.5) < 0.02
                         and abs(v["throttle"] - 0.75) < 0.02
                         and abs(v["brake"] - 0.25) < 0.02
                         and v["buttons"] == 0x1000)
                _marca(bool(bateu), f"Valores chegam certos no XInput (slot {slot})",
                       "" if bateu else f"lido: {v}")
                if not bateu:
                    pendencias.append("valores nao batem no XInput")

                pad.apply(0.0, 0.0, 0.0, set())
                time.sleep(0.15)
                centro = xinput_read(lib, slot)
                limpo = centro and abs(centro["steer"]) < 0.01
                _marca(bool(limpo), "Centro do volante limpo em repouso",
                       "" if limpo else
                       f"analogico preso em {centro.get('steer', 0):+.3f}")
                if not limpo:
                    pendencias.append("centro do pad virtual torto")
    except RuntimeError as exc:
        _marca(False, "Controle virtual", str(exc).splitlines()[0])
        pendencias.append("instalar dependencias (aba Instalacao da interface)")
    finally:
        if pad is not None:
            pad.release_all()

    # --- janela do jogo ----------------------------------------------------
    hwnd = janela_do_jogo(cfg["ffb"])
    _marca(bool(hwnd), "GTA V aberto agora",
           f"hwnd={hwnd}" if hwnd else "nao precisa estar aberto para este teste")

    print()
    if pendencias:
        print("  Pendencias:")
        for p in pendencias:
            print(f"    - {p}")
    else:
        print("  Tudo certo. Abra o DASHBOARD-GTAV.bat e so entao o GTA V.")
    print("\n  Dica: 'python g29_gtav.py --testar-pad' mexe no controle virtual")
    print("        para voce ver a resposta no painel joy.cpl do Windows.\n")
    return 0 if not pendencias else 1


def cmd_testar_pad(cfg: dict) -> int:
    """Mexe sozinho no controle virtual, para conferir no painel do Windows."""
    import math
    import subprocess

    pad = VirtualPad()
    try:
        subprocess.Popen("control joy.cpl", shell=True)
    except Exception:
        pass

    print("\n  Abra o painel de controles do Windows (Win+R -> joy.cpl),")
    print("  escolha o 'Controller (XBOX 360 For Windows)' e clique em Propriedades.")
    print("  Por 12 segundos o controle virtual vai se mexer sozinho.\n")

    sequencia = ["A", "B", "X", "Y", "LB", "RB"]
    inicio = time.perf_counter()
    try:
        while True:
            t = time.perf_counter() - inicio
            if t > 12.0:
                break
            steer = math.sin(t * 1.2)
            gatilho = (math.sin(t * 2.0) + 1) / 2
            botao = sequencia[int(t / 2) % len(sequencia)]
            pad.apply(steer, gatilho, 1.0 - gatilho, {botao})
            sys.stdout.write(f"\r  dir={steer:+.2f}  RT={gatilho:.2f}  "
                             f"LT={1 - gatilho:.2f}  botao={botao}   ")
            sys.stdout.flush()
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        pad.release_all()
    print("\n\n  Se o controle se mexeu no painel, o GTA V tambem vai ver.\n")
    return 0


def cmd_testar_ffb(cfg: dict) -> int:
    """Toca uma sequencia de efeitos para sentir o force feedback."""
    wheel = Wheel(cfg)
    ffb = SdlFFB(wheel.js)
    if not ffb.disponivel():
        print(f"\n  Sem force feedback: {ffb.erro or 'o volante nao expos efeitos'}\n")
        return 1

    print(f"\n  FFB ativo via SDL em {wheel.name}")
    print("  Efeitos: " + ", ".join(n for n, i in ffb.ids.items() if i >= 0) + "\n")

    sequencia = [
        ("mola fraca (volante leve)",           dict(mola=20, mola_sat=60, amortecedor=0,  vibracao=0.0)),
        ("mola forte (puxa para o centro)",     dict(mola=85, mola_sat=95, amortecedor=0,  vibracao=0.0)),
        ("amortecedor (aro encorpado)",         dict(mola=0,  mola_sat=0,  amortecedor=70, vibracao=0.0)),
        ("vibracao de pista ruim",              dict(mola=0,  mola_sat=0,  amortecedor=0,  vibracao=0.7)),
        ("tudo junto",                          dict(mola=50, mola_sat=80, amortecedor=25, vibracao=0.35)),
    ]
    try:
        for descricao, valores in sequencia:
            print(f"  -> {descricao}")
            ffb.aplicar(ganho=100, **valores)
            fim = time.perf_counter() + 2.5
            while time.perf_counter() < fim:
                wheel.pump()
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        ffb.aplicar(0, 0, 0, 0.0, 0)
        ffb.parar()
        ffb.fechar()

    print("\n  Sentiu os cinco? Entao o FFB funciona no jogo.")
    print("  Ajuste a intensidade na aba Ajustes da interface.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ponte Logitech G29 -> controle Xbox 360 virtual para o GTA V.")
    ap.add_argument("--listar", action="store_true",
                    help="lista os dispositivos de entrada")
    ap.add_argument("--monitorar", action="store_true",
                    help="mostra eixos e botoes ao vivo")
    ap.add_argument("--calibrar", action="store_true",
                    help="assistente de calibragem do volante e pedais")
    ap.add_argument("--calibrar-botoes", action="store_true",
                    help="assistente de mapeamento dos botoes")
    ap.add_argument("--testar", action="store_true",
                    help="diagnostico da cadeia inteira")
    ap.add_argument("--testar-pad", action="store_true",
                    help="mexe no controle virtual para conferir no joy.cpl")
    ap.add_argument("--testar-ffb", action="store_true",
                    help="toca efeitos de force feedback no volante")
    ap.add_argument("--sem-ffb", action="store_true",
                    help="nao usar force feedback")
    ap.add_argument("--silencioso", action="store_true",
                    help="nao imprimir a barra de status")
    args = ap.parse_args()

    if args.listar:
        return cmd_listar()

    cfg = load_config()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)

    try:
        if args.monitorar:
            return cmd_monitorar(cfg)
        if args.calibrar:
            return cmd_calibrar(cfg)
        if getattr(args, "calibrar_botoes"):
            return cmd_calibrar_botoes(cfg)
        if args.testar:
            return cmd_testar(cfg)
        if getattr(args, "testar_pad"):
            return cmd_testar_pad(cfg)
        if getattr(args, "testar_ffb"):
            return cmd_testar_ffb(cfg)
        return cmd_rodar(cfg, not args.sem_ffb, args.silencioso)
    except RuntimeError as exc:
        print(f"\nERRO: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
