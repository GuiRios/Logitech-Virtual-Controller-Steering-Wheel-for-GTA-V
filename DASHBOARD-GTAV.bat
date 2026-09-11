@echo off
chcp 65001 >nul
rem sem ">" no title: o cmd trata como redirecionamento e cria um arquivo solto
title G29 para GTA V
cd /d "%~dp0"

rem Atalho unico: a propria janela cuida de instalar dependencias, calibrar
rem e jogar. A unica coisa que ela nao consegue resolver sozinha e a falta do
rem Python, porque ela mesma e um programa Python.

rem "pyw" e "pythonw" abrem sem a janela preta do console.
where pyw >nul 2>&1 && (start "" pyw -3 g29_gui.py & exit /b 0)
where pythonw >nul 2>&1 && (start "" pythonw g29_gui.py & exit /b 0)
where py >nul 2>&1 && (py -3 g29_gui.py & exit /b 0)
where python >nul 2>&1 && (python g29_gui.py & exit /b 0)

echo.
echo  ============================================================
echo    Falta instalar o Python neste computador
echo  ============================================================
echo.
echo    1) Baixe em https://www.python.org/downloads/
echo    2) Na PRIMEIRA tela do instalador, marque a caixinha
echo       "Add Python to PATH"  (fica embaixo, e facil de passar batido)
echo    3) Conclua a instalacao e abra este arquivo de novo
echo.
echo    O resto - bibliotecas, driver do controle, calibragem -
echo    a propria aplicacao resolve na aba "Instalacao".
echo.
choice /c SN /n /m "  Abrir a pagina de download agora? [S/N] "
if errorlevel 2 goto fim
start "" https://www.python.org/downloads/
:fim
echo.
pause
