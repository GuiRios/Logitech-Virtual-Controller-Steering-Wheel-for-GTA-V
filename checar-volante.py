"""Checagem rapida do volante SEM abrir nada em exclusivo (nao rouba o G29).
Copyright (C) 2026 Gui Rios

Este programa e software livre: voce pode redistribui-lo e/ou modifica-lo sob
os termos da GNU General Public License versao 3, publicada pela Free Software
Foundation. Ele e distribuido SEM NENHUMA GARANTIA. Veja o arquivo LICENSE ou
<https://www.gnu.org/licenses/>.
"""
import ctypes
from ctypes import wintypes

class JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("dwXpos", wintypes.DWORD), ("dwYpos", wintypes.DWORD),
                ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD),
                ("dwButtons", wintypes.DWORD), ("dwButtonNumber", wintypes.DWORD),
                ("dwPOV", wintypes.DWORD), ("dwReserved1", wintypes.DWORD),
                ("dwReserved2", wintypes.DWORD)]

ji = JOYINFOEX(); ji.dwSize = ctypes.sizeof(JOYINFOEX); ji.dwFlags = 0xFF
if ctypes.windll.winmm.joyGetPosEx(0, ctypes.byref(ji)) != 0:
    print("volante nao responde ao Windows"); raise SystemExit(1)
print(f"volante: X={ji.dwXpos}  acelerador={ji.dwYpos}  freio={ji.dwZpos}  embreagem={ji.dwRpos}")
neutro = all(abs(v - 32767) < 2000 for v in (ji.dwYpos, ji.dwZpos, ji.dwRpos))
print("DIAGNOSTICO:", "PEDAIS MUDOS - reinicie o volante na tomada" if neutro
      else "reportando normalmente (pedais fora do centro, como deve ser)")
