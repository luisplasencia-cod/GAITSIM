# Gait Simulator — Protocolo de Comunicación Serial

Contrato entre la Raspberry Pi (maestro) y el ESP32 (esclavo). Cualquier
firmware (de prueba o definitivo) debe cumplirlo.

## Transporte

- USB Serial, 115200 baud.
- RPi -> ESP32: enmarcado en `<` `>`, sin `\n`. Ej: `PING` se manda como `<PING>`.
- ESP32 -> RPi: texto plano, una respuesta por línea, con `\n` — sin
  marco (única excepción: los eventos de calibración, ver abajo).
- Separador de argumentos: `:`.
- En comandos con 2+ argumentos: el primero puede ser texto (ej. `axis`);
  todo lo demás siempre numérico, nunca texto — por eso `direction` de
  `MANUAL` es `1`/`0`, no `+`/`-`.

## Sistema

| Comando | Formato | Respuesta |
|---|---|---|
| Ping | `<PING>` | `PONG` |
| Home | `<HOME>` | `READY` o `ERROR:<code>:<msg>` |

**Ejemplo real** (conectar + calibrar):
```
>> <PING>
<< PONG
>> <HOME>
<< <LIMYMIN>
<< <LIMYMAX:72000>
<< <LIMXMIN>
<< <LIMXMAX:48000>
<< <LIMANGMIN:-5000>
<< <LIMANGMAX:5400>
<< READY
```

**Estados internos** que el firmware debe rastrear (no hay comando para
consultarlos — solo sirven para aceptar/rechazar comandos según el
estado actual):

| Estado | Cuándo |
|---|---|
| `DISCONNECTED` | Antes del primer `HOME` exitoso |
| `HOMING` | Entre `<HOME>` y `READY` |
| `IDLE` | Homed, listo para `MANUAL`/`TRAJ_BEGIN` |
| `RECEIVING_TRAJECTORY` | Entre `<TRAJ_BEGIN>` y `<TRAJ_END>` |
| `RUNNING` | Entre `<RUN>` y `FINISHED`/`PAUSED`/`ABORTED` |
| `PAUSED` | Entre `<PAUSE>` y `<RESUME>`/`<ABORT>` |

## Calibración (durante HOMING)

Al recibir `<HOME>`, el ESP32 barre los 3 ejes en orden **Y, X,
Angular**. Por cada uno manda 2 eventos (enmarcados en `<` `>`, único
caso además de los comandos mismos):

| Evento | Formato | Qué es |
|---|---|---|
| Límite mínimo | `<LIMYMIN>` / `<LIMXMIN>` (sin dato) — `<LIMANGMIN:steps>` (con dato) | Paso 0 de ese eje |
| Límite máximo | `<LIM{AXIS}MAX:steps>` | Pasos crudos de motor, entero — **no cm/grados** |

- Y/X: `MIN` no lleva dato (su cero crudo YA es paso 0); `MAX` es un
  conteo positivo desde ahí. Techos reales del rig: Y=72000, X=48000.
- **Angular es distinto**: sus limit switches no están en horizontal,
  así que el ESP32 manda el paso YA con signo, relativo a
  horizontal=paso 0 — `LIMANGMIN:-5000`, `LIMANGMAX:5400` (valores
  reales del rig). Ver el ejemplo completo arriba, en Sistema.
- La RPi convierte pasos -> cm/grados con estas constantes (deben
  coincidir con las tuyas): `STEPS_PER_CM_X = 400`,
  `STEPS_PER_CM_Y = 800`, `STEPS_PER_DEG_ANGLE ≈ 111.11` (= 50×800/360
  — reducción angular × pasos/rev del motor, ÷ 360).

## Movimiento Manual

| Comando | Formato | Respuesta |
|---|---|---|
| Manual | `<MANUAL:axis:direction:steps>` | `OK` o `ERROR:<code>:<msg>` |

`axis`: `X`/`Y`/`A`. `direction`: `1`=`+`, `0`=`-`. `steps`: entero
positivo.

**No lo implementes todavía** — la app hoy no lo usa (el joystick
manda trayectorias, ver abajo). No rompe nada si tu firmware no lo
reconoce.

## Consulta de Posición

| Comando | Formato | Respuesta |
|---|---|---|
| Get position | `<GET_POSITION>` | `POSITION:<x>:<y>:<angle>` |

Pasos crudos de motor, enteros, posición **absoluta** (no delta) desde
el `HOME` más reciente. Misma conversión que la calibración.

**Ejemplo real:**
```
>> <GET_POSITION>
<< POSITION:0:0:0
>> <GET_POSITION>
<< POSITION:2000:0:0
```

## Trayectorias

| Comando | Formato | Respuesta |
|---|---|---|
| Iniciar | `<TRAJ_BEGIN:n_points>` | `TRAJ_READY` |
| Punto | `<TRAJ_POINT:dt_ms:dx_steps:dy_steps:dangle_steps>` | `ACK:<index>` |
| Finalizar | `<TRAJ_END>` | `TRAJ_STORED` o `ERROR:<code>:<msg>` |
| Correr | `<RUN>` | `RUNNING`, luego un `TRAJ_PROGRESS` por punto, luego `FINISHED` |
| Pausar | `<PAUSE>` | `PAUSED` |
| Reanudar | `<RESUME>` | `RUNNING` |
| Abortar | `<ABORT>` (solo válido en `PAUSED`) | `ABORTED` o `ERROR:INVALID_STATE:...` |
| Progreso (no solicitado) | `TRAJ_PROGRESS:<t>:<x>:<y>:<angle>` | — |

**`TRAJ_POINT` es un DELTA, no una posición absoluta**: `dt_ms` =
milisegundos desde el punto anterior. `dx/dy/dangle_steps` = pasos con
signo que se mueve cada eje en ese intervalo. El PRIMER punto TAMBIÉN
es un delta — calculado contra la posición REAL actual del ESP32, no
asumas que arranca en 0. Acumula así: al recibir `TRAJ_BEGIN`, arranca
tu suma en tu posición rastreada actual; con cada `TRAJ_POINT`, súmale
el delta.

**`TRAJ_PROGRESS` SÍ es absoluto** (no delta): pasos acumulados en ese
punto. `t` es tiempo en segundos, sin cambios.

**Ejemplo real completo** (3 puntos en X: 0cm, 3cm, 5cm, arrancando en
`(0,0,0)`):
```
>> <TRAJ_BEGIN:3>
<< TRAJ_READY
>> <TRAJ_POINT:0:0:0:0>
<< ACK:0
>> <TRAJ_POINT:500:1200:0:0>
<< ACK:1
>> <TRAJ_POINT:500:800:0:0>
<< ACK:2
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN>
<< RUNNING
<< TRAJ_PROGRESS:0.0000:0:0:0
<< TRAJ_PROGRESS:0.5000:1200:0:0
<< TRAJ_PROGRESS:1.0000:2000:0:0
<< FINISHED
```
(3cm = 1200 pasos, 5cm = 2000 pasos — `STEPS_PER_CM_X` = 400)

**Ejemplo real — Pause/Resume/Abort** (a mitad de otra trayectoria):
```
>> <PAUSE>
<< PAUSED
>> <RESUME>
<< RUNNING
...
>> <PAUSE>
<< PAUSED
>> <ABORT>
<< ABORTED
```
`PAUSE` se reanuda con `RESUME` exactamente donde iba, sin reenviar
nada. `ABORT` descarta el resto de la trayectoria y vuelve a `IDLE`.

Esta secuencia completa (`TRAJ_BEGIN`...`RUN`...`FINISHED`) es la
ÚNICA forma en que la interfaz mueve el sistema — ensayo CSV, joystick
manual, ir a posición inicial, y retorno seguro entre ensayos usan
exactamente este mismo camino, nunca otro.

## Errores

```
ERROR:<code>:<mensaje corto>
```
Ej.: `ERROR:LIMIT_REACHED:X axis`, `ERROR:INVALID_STATE:cannot home while running`,
`ERROR:POINT_COUNT_MISMATCH:expected 120, got 118`.

## Comandos eliminados — no los implementes

`GOTO`, `STATUS`, `STOP` existieron y se quitaron por no tener ningún
uso real. Si tu firmware no los reconoce, esto es lo normal (no rompe
nada):
```
>> <GOTO:1:1:1>
<< ERROR:UNKNOWN_COMMAND:GOTO:1:1:1
```

## Flujo típico

1. `<PING>` -> `PONG`
2. `<HOME>` -> barrido de calibración -> `READY`
3. Por cada movimiento (ensayo, joystick, posición inicial, retorno):
   `TRAJ_BEGIN` -> `TRAJ_POINT`×N -> `TRAJ_END` -> `RUN` ->
   `TRAJ_PROGRESS`×N -> `FINISHED`
4. `PAUSE`/`RESUME` durante `RUNNING`; `ABORT` solo durante `PAUSED`

## Referencia

Implementación de referencia (simulada, sin motores reales):
`firmware/gaitsim-esp32-test/main.cpp`.
