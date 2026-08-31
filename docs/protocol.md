# Gait Simulator — Protocolo de Comunicación Serial

Contrato entre la Raspberry Pi (maestro) y el ESP32 (esclavo). Cualquier
firmware (de prueba o definitivo) debe cumplirlo.

## Transporte

- USB Serial, 115200 baud.
- RPi -> ESP32: enmarcado en `<` `>`, sin `\n`. Ej: `PING` se manda como `<PING>`.
- ESP32 -> RPi: texto plano, una respuesta por línea, con `\n` — sin
  marco, siempre (hasta 2026-08-31 había una excepción, los eventos de
  calibración; ya no — ver "Cambio 2026-08-31" en Calibración, abajo).
- Separador de argumentos: `:`.
- En comandos con 2+ argumentos: el primero puede ser texto (ej. `axis`);
  todo lo demás siempre numérico, nunca texto — por eso `direction` de
  `MANUAL` es `1`/`0`, no `+`/`-`.

## Sistema

| Comando | Formato | Respuesta |
|---|---|---|
| Ping | `<PING>` | `PONG` |
| Home | `<HOME>` | `READY:xmax:ymax:amin:amax` o `ERROR:code:msg` |

**¿Cuándo los pide la RPi?**
- `PING`: al tocar "Conectar" (barra superior) — antes de mostrar
  "Conectado", para confirmar que hay un ESP32 de verdad respondiendo.
- `HOME`: al tocar "Calibrar (Home)" en la pantalla de Inicio, el único
  botón habilitado justo después de conectar.

**Ejemplo real** (conectar + calibrar):
```
>> <PING>
<< PONG
>> <HOME>
<< READY:48000:72000:-5000:5400
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

Al recibir `<HOME>`, el ESP32 barre los 3 ejes internamente (Y, X,
Angular) y reporta los 4 límites juntos en la respuesta `READY` (ver
tabla en Sistema, arriba) — no hay eventos intermedios: la RPi no sabe
nada del barrido hasta que llega `READY` (o `ERROR`).

`READY:xmax:ymax:amin:amax` — los 4 campos son pasos crudos de motor,
enteros — **no cm/grados**:

| Campo | Qué es |
|---|---|
| `xmax` | Límite máximo del eje X, conteo positivo desde su paso 0 |
| `ymax` | Límite máximo del eje Y, conteo positivo desde su paso 0 |
| `amin` | Límite mínimo del eje angular, con signo |
| `amax` | Límite máximo del eje angular, con signo |

- Y/X: el límite mínimo de cada eje no viaja en el mensaje — su cero
  crudo YA es paso 0 por definición. Techos reales del rig: Y=72000,
  X=48000.
- **Angular es distinto**: sus limit switches no están en horizontal,
  así que el ESP32 manda el paso YA con signo, relativo a
  horizontal=paso 0 — `amin=-5000`, `amax=5400` (valores reales del
  rig). Ver el ejemplo completo arriba, en Sistema.
- La RPi convierte pasos -> cm/grados con estas constantes (deben
  coincidir con las tuyas): `STEPS_PER_CM_X = 400`,
  `STEPS_PER_CM_Y = 800`, `STEPS_PER_DEG_ANGLE ≈ 111.11` (= 50×800/360
  — reducción angular × pasos/rev del motor, ÷ 360).

**¿Cuándo se usa?** La RPi usa estos 4 valores, una vez que llegan con
`READY`, para dibujar el mapa de espacio disponible ("Espacio
Disponible" en la barra de navegación). Mientras dura el barrido (entre
`<HOME>` y `READY`) solo se muestra un mensaje genérico de "Calibrando"
— ya no hay progreso en vivo por eje (ver "Cambio 2026-08-31" abajo).

**Cambio 2026-08-31 (READY con límites)**: antes, el ESP32 mandaba 6
eventos en vivo (`<LIMYMIN>`, `<LIMYMAX:72000>`, etc., enmarcados en
`<` `>` — la única excepción a "las respuestas del ESP32 van sin
marco") durante el barrido, seguidos de un `READY` sin datos. El
compañero simplificó esto: ahora todo llega junto en una sola línea,
`READY:xmax:ymax:amin:amax`, sin marco (como cualquier otra respuesta).
Ya no existe ninguna excepción de framing — todas las respuestas del
ESP32 van sin `<` `>`.

## Movimiento Manual

| Comando | Formato | Respuesta |
|---|---|---|
| Manual | `<MANUAL:axis:direction:steps>` | `OK` o `ERROR:code:msg` |

`axis`: `X`/`Y`/`A`. `direction`: `1`=`+`, `0`=`-`. `steps`: entero
positivo.

**¿Cuándo se usa?** Nunca, hoy — cuando el operador mueve el joystick
en la pantalla de Inicio, la RPi NO manda `MANUAL`, manda una
trayectoria de 1 solo eje (ver sección Trayectorias abajo). No lo
implementes todavía; no rompe nada si tu firmware no lo reconoce.

## Consulta de Posición

| Comando | Formato | Respuesta |
|---|---|---|
| Get position | `<GET_POSITION>` | `POSITION:x:y:angle` |

Pasos crudos de motor, enteros, posición **absoluta** (no delta) desde
el `HOME` más reciente. Misma conversión que la calibración.

**¿Cuándo lo pide la RPi?** Cada 200ms, sin parar, mientras el panel
"Monitor Posición" está abierto (para animar la plataforma en
pantalla) — y una vez, puntual, antes de generar cualquier movimiento
(joystick, retorno seguro entre ensayos), para saber desde dónde
partir.

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
| Punto (CON tiempo — solo ensayo/marcha) | `<TRAJ_POINT:dt_ms:dx_steps:dy_steps:dangle_steps>` | `ACK:index` |
| Punto (SIN tiempo — todo lo demás) | `<TRAJ_POINT:dx_steps:dy_steps:dangle_steps>` | `ACK:index` |
| Finalizar | `<TRAJ_END>` | `TRAJ_STORED` o `ERROR:code:msg` |
| Correr | `<RUN>` | `RUNNING`, luego un `TRAJ_PROGRESS` por punto, luego `FINISHED` |
| Pausar | `<PAUSE>` | `PAUSED` |
| Reanudar | `<RESUME>` | `RUNNING` |
| Abortar | `<ABORT>` (solo válido en `PAUSED`) | `ABORTED` o `ERROR:INVALID_STATE:...` |
| Progreso (no solicitado) | `TRAJ_PROGRESS:t:x:y:angle` | — |

**¿Cuándo pide la RPi `TRAJ_BEGIN...RUN`?** En 4 momentos, siempre por
el mismo camino — pero solo el primero manda `TRAJ_POINT` CON tiempo:
1. Tocar "Load && Send" en la pantalla de Trayectorias — el ensayo CSV.
   **CON tiempo**: es marcha real grabada, el tiempo entre puntos importa.
2. La primera vez que se toca "Ir a Posición Inicial" después de un
   `HOME`. **SIN tiempo**.
3. Mover el joystick manual (cada toque de flecha genera y envía su
   propia trayectoria de 1 eje). **SIN tiempo**.
4. Tocar "Reiniciar Ensayo", "Elegir Otro Ensayo", o volver a "Ir a
   Posición Inicial" ya con una posición previa registrada. **SIN tiempo**.

**Cambio 2026-08-31 (TRAJ_POINT sin tiempo)**: antes, TODO `TRAJ_POINT`
llevaba `dt_ms`, calculado por la RPi contra velocidades asumidas (no
calibradas al rig real) para cualquier movimiento que no fuera el
ensayo CSV. Ahora, solo el ensayo CSV/marcha manda la forma CON tiempo
(el `dt_ms` real grabado en el CSV) — los otros 3 casos mandan la forma
SIN tiempo (3 campos: `dx:dy:dangle`), y el ESP32 decide su propia
velocidad para ese movimiento.

**`TRAJ_POINT` es un DELTA, no una posición absoluta** (misma regla en
ambas formas): `dx/dy/dangle_steps` = pasos con signo que se mueve cada
eje; en la forma CON tiempo, `dt_ms` = milisegundos desde el punto
anterior. El PRIMER punto TAMBIÉN es un delta — calculado contra la
posición REAL actual del ESP32 (pedida con `GET_POSITION` antes de
`TRAJ_BEGIN`), no asumas que arranca en 0. Por esto la RPi nunca manda
el punto "t=0" de su propia lista de puntos (sería un delta de duración
cero contra su propia base t=0) — arranca directo en el primer punto
real, con ese delta ya calculado contra la posición real. Acumula así:
al recibir `TRAJ_BEGIN`, arranca tu suma en tu posición rastreada
actual; con cada `TRAJ_POINT`, súmale el delta.

**`TRAJ_PROGRESS` SÍ es absoluto** (no delta): pasos acumulados en ese
punto. `t` es tiempo en segundos, sin cambios.

**Ejemplo real completo — ensayo CON tiempo** (2 puntos en X: 3cm,
5cm, arrancando en `(0,0,0)`):
```
>> <GET_POSITION>
<< POSITION:0:0:0
>> <TRAJ_BEGIN:2>
<< TRAJ_READY
>> <TRAJ_POINT:500:1200:0:0>
<< ACK:0
>> <TRAJ_POINT:500:800:0:0>
<< ACK:1
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN>
<< RUNNING
<< TRAJ_PROGRESS:0.5000:1200:0:0
<< TRAJ_PROGRESS:1.0000:2000:0:0
<< FINISHED
```
(3cm = 1200 pasos, 5cm = 2000 pasos — `STEPS_PER_CM_X` = 400; el punto
"t=0" del CSV, ya en la posición actual, no se manda)

**Ejemplo real — joystick SIN tiempo** (+2cm en X desde `(10, 0, 0)`):
```
>> <GET_POSITION>
<< POSITION:4000:0:0
>> <TRAJ_BEGIN:1>
<< TRAJ_READY
>> <TRAJ_POINT:800:0:0>
<< ACK:0
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN>
<< RUNNING
```
(2cm = 800 pasos; sin `dt_ms` — el ESP32 elige la velocidad)

**¿Cuándo pide `PAUSE`/`RESUME`/`ABORT`?** Los botones "Pause"/"Resume"
de la pantalla de Trayectorias mandan `PAUSE`/`RESUME` directo,
mientras el ensayo está corriendo. `ABORT` sale automáticamente al
tocar "Reiniciar Ensayo" o "Elegir Otro Ensayo" si el ensayo estaba en
pausa.

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

## Errores

```
ERROR:code:mensaje corto
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
