@echo off
chcp 65001 > nul
title ARGOS Recovery v2
color 0A

set TARGET=F:\debug\argoss
set SRC=http://192.168.1.53:9999
set DT=2026-05-23-r2

echo.
echo === ARGOS RECOVERY v2 (no health probe) ===
echo TARGET = %TARGET%
echo SRC    = %SRC%
echo.

echo [1] Р‘СЌРєР°Рї СЃС‚Р°СЂС‹С… С„Р°Р№Р»РѕРІ...
if exist "%TARGET%\main.py" copy /Y "%TARGET%\main.py" "%TARGET%\main.py.bak_%DT%" > nul
if exist "%TARGET%\src\core.py" copy /Y "%TARGET%\src\core.py" "%TARGET%\src\core.py.bak_%DT%" > nul

echo [2] РЎРєР°С‡РёРІР°СЋ С„Р°Р№Р»С‹ (Р±РµР· РїСЂРѕРІРµСЂРѕРє, РЅР°РїСЂРѕР»РѕРј)...
curl -fsSL -o "%TARGET%\main.py"                          %SRC%/main.py                  && echo   OK main.py                          || echo   FAIL main.py
curl -fsSL -o "%TARGET%\src\core.py"                      %SRC%/core.py                  && echo   OK src\core.py                      || echo   FAIL core.py
curl -fsSL -o "%TARGET%\src\db_init.py"                   %SRC%/db_init.py               && echo   OK src\db_init.py                   || echo   FAIL db_init.py
curl -fsSL -o "%TARGET%\src\skills\hive_mind.py"          %SRC%/hive_mind.py             && echo   OK src\skills\hive_mind.py          || echo   FAIL hive_mind.py
curl -fsSL -o "%TARGET%\src\skills\content_gen.py"        %SRC%/content_gen.py           && echo   OK src\skills\content_gen.py        || echo   FAIL content_gen.py
curl -fsSL -o "%TARGET%\src\connectivity\telegram_bot.py" %SRC%/telegram_bot.py          && echo   OK src\connectivity\telegram_bot.py || echo   FAIL telegram_bot.py
curl -fsSL -o "%TARGET%\scripts\ha_watchdog.py"           %SRC%/ha_watchdog.py           && echo   OK scripts\ha_watchdog.py           || echo   FAIL ha_watchdog.py
curl -fsSL -o "%TARGET%\scripts\entity_dialogue_v5.py"    %SRC%/entity_dialogue_v5.py    && echo   OK scripts\entity_dialogue_v5.py    || echo   FAIL entity_dialogue_v5.py
curl -fsSL -o "%TARGET%\scripts\entity_council.py"        %SRC%/entity_council.py        && echo   OK scripts\entity_council.py        || echo   FAIL entity_council.py

echo.
echo [3] РћСЃС‚Р°РЅРѕРІ СЃС‚Р°СЂС‹С… РїСЂРѕС†РµСЃСЃРѕРІ (Р·Р°РіР»СѓС€РєР° РљРёРјРё)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000.*LISTENING"') do taskkill /F /PID %%a > nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8010.*LISTENING"') do taskkill /F /PID %%a > nul 2>&1
echo   OK

echo [4] Р Р°Р·РјРµСЂ СЃРєР°С‡Р°РЅРЅС‹С… С„Р°Р№Р»РѕРІ:
for %%F in (main.py src\core.py src\db_init.py src\skills\hive_mind.py src\skills\content_gen.py src\connectivity\telegram_bot.py scripts\ha_watchdog.py scripts\entity_dialogue_v5.py scripts\entity_council.py) do (
    if exist "%TARGET%\%%F" (
        for %%I in ("%TARGET%\%%F") do echo   %%~zI B  %%F
    ) else (
        echo   MISSING %%F
    )
)

echo.
echo [5] Р—Р°РїСѓСЃРє main.py РІ РЅРѕРІРѕРј РѕРєРЅРµ...
cd /D "%TARGET%"
start "ARGOS Main" cmd /k "python main.py --no-gui"

echo.
echo === Р“РћРўРћР’Рћ. РџСЂРѕРІРµСЂСЊ С‡РµСЂРµР· 60 СЃРµРє: ===
echo   curl http://localhost:5010/health
echo   curl http://localhost:8000/health
echo   curl http://localhost:8010/health
echo.
pause