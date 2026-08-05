# Mejoras futuras

Carpeta de diseño y prototipos para funcionalidades que **todavía no
se integran** a la app principal (`src/`, `main.py`). Nada de acá se
importa desde la app — cada subcarpeta es standalone, se puede correr
y probar de forma aislada, y no afecta a Horizonte 1 (ya verificado en
hardware real, ver `CLAUDE.md`).

## Por qué existe esta carpeta

Luis quiere ir diseñando/probando mejoras de largo plazo en paralelo,
sin tocar el código ya verificado, mientras el compañero termina el
firmware definitivo (Horizonte 2). Cuando esa integración esté lista,
se pausa el desarrollo normal, se hace la integración, y recién ahí se
retoman y se cablean estas mejoras al resto de la app.

Cada subcarpeta trae su propio `README.md` con: el problema, qué dice
la literatura/lo que hacen sistemas reales, el diseño propuesto, qué
falta para integrarlo, y el estado (diseño / prototipo / probado). Está
escrito para que una sesión nueva de Claude Code (sin memoria de las
conversaciones donde se decidió esto) pueda retomarlo leyendo solo esa
carpeta — no asumas que el lector vio la conversación original.

## Índice

| Carpeta | Qué es | Estado |
|---|---|---|
| [`gait_trajectory_synthesis_from_anthropometry/`](gait_trajectory_synthesis_from_anthropometry/README.md) | Generar el CSV de trayectoria (t, x, y, ángulo) algorítmicamente a partir de datos antropométricos del paciente, en vez de requerir un CSV capturado manualmente | Diseño + prototipo Fase 1 escrito, sin probar contra datos reales de un sujeto nuevo |
