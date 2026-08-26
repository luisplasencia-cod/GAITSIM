# Gait Simulator — Protocolo de Comunicación Serial

Este documento especifica el contrato entre la Raspberry Pi
(controlador de alto nivel) y el ESP32 (controlador en tiempo real).
Cualquier implementación de firmware (de prueba o definitiva) debe
cumplir esta especificación para que el resto del stack de software
funcione sin modificaciones.

## Transporte

- Enlace físico: USB Serial
- Baud rate: 115200
- Roles: la Raspberry Pi es maestra (envía comandos), el ESP32 es
  esclavo (ejecuta y responde)
- **Comandos RPi -> ESP32**: enmarcados con los delimitadores literales
  `<` y `>`, sin `\n` al final — ej. `PING` se envía por el cable como
  `<PING>`. El ESP32 lee desde `<` hasta `>` como un comando completo
  (coincide con la función genérica de recepción usada en el lado
  ESP32). Cada celda `Format` de abajo para un comando RPi -> ESP32
  muestra solo el payload; hay que envolverlo en `<` `>` para obtener
  los bytes reales enviados.
- **Respuestas ESP32 -> RPi**: no afectadas por lo anterior — texto
  ASCII plano, una respuesta por línea, terminada en `\n`, exactamente
  como se muestra en las celdas `Response`/`Format` de abajo.
- **Separador de argumentos**: `:` (dos puntos) en todos los mensajes
  con 2+ argumentos, sin excepción — misma dirección (RPi -> ESP32) o
  contraria. Ningún formato usa `,` como separador.
- **Tipos de argumento (comandos RPi -> ESP32 con 2+ argumentos)**: el
  primer argumento puede ser un string (ej. `axis`); todo argumento
  después del primero debe ser numérico — entero, o (como en
  `TRAJ_POINT`, donde todos los argumentos ya son numéricos) un
  float. Ningún comando puede llevar un segundo argumento textual/
  categórico — ej. el `direction` de `MANUAL` es un entero
  (`1`/`0`), no texto `+`/`-`, precisamente para que no sea un segundo
  argumento de tipo string. Esto coincide con la función genérica de
  recepción del lado ESP32, que parsea el argumento 0 como string y
  cada argumento posterior como numérico.

## Comandos del Sistema

| Comando | Dirección | Formato | Respuesta |
|---|---|---|---|
| Ping | RPi -> ESP32 | `<PING>` | `PONG` |
| Home | RPi -> ESP32 | `<HOME>` | `READY` si tiene éxito, `ERROR:<code>:<msg>` si falla |

`PING` es la forma en que la Raspberry Pi verifica que hay un ESP32
real y respondiendo al otro lado antes de considerar la conexión
serial exitosa — abrir el puerto serial por sí solo no lo garantiza
(puerto equivocado, u otro dispositivo distinto en ese puerto).

**En la interfaz**: el operador toca el botón "Conectar" (barra de
navegación superior, `ConnectionStatusButton` en
`src/ui/status_indicator.py`). Ese único toque hace, en orden:
`connect()` (abre el puerto serial) -> `<PING>` -> si llega `PONG`, el
botón pasa a mostrar "Conectado"; si no llega a tiempo, se cierra el
puerto de nuevo y el botón muestra "Error".

**En la interfaz**: `<HOME>` se envía al tocar "Calibrar (Home)" en la
pantalla de Inicio (`home_button` en `connection_screen.py`, único
botón habilitado justo después de conectar). Mientras dura (~12s en el
firmware de prueba), la pantalla queda en "Calibrando..." mostrando el
avance eje por eje (ver Eventos de Calibración abajo); al terminar
ofrece guardar/cargar la posición inicial del ensayo.

**Lo que se ve en el serial** (captura real, firmware de prueba —
`>>` es lo que manda la RPi, `<<` lo que responde el ESP32):

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

**Estados internos que el firmware debe rastrear** (no hay comando de
consulta de estado en el protocolo — `STATUS` existió y fue eliminado
por no tener ningún llamador real, ver Notas de Diseño): los mismos 6
valores que usa el `SystemStateMachine` de la Raspberry Pi (ver la
sección "State machine states" de CLAUDE.md). El firmware necesita un
enum interno equivalente de todas formas, para poder rechazar comandos
inválidos en el estado equivocado (ver `handleManual()`/`handleAbort()`
del firmware de prueba en `firmware/gaitsim-esp32-test/main.cpp` como
referencia de este patrón):

| `<state>` | Cuándo el ESP32 debe estar en este estado |
|---|---|
| `DISCONNECTED` | Estado inicial al arrancar, antes de que `HOME` haya tenido éxito al menos una vez en esta sesión |
| `HOMING` | Mientras ejecuta el barrido de calibración, entre recibir `<HOME>` y enviar `READY` |
| `IDLE` | Homed, sin ejecutar trayectoria, sin recibir trayectoria — listo para `MANUAL`/`TRAJ_BEGIN` |
| `RECEIVING_TRAJECTORY` | Entre `<TRAJ_BEGIN>` y `<TRAJ_END>` |
| `RUNNING` | Después de `<RUN>`, antes de `FINISHED`/`PAUSED`/`ABORTED` |
| `PAUSED` | Después de `<PAUSE>`, antes de `<RESUME>` o `<ABORT>` |

## Eventos de Calibración (no solicitados, durante HOMING)

`<HOME>` dispara el homing Y un barrido completo de mapeo de límites
de los 3 ejes, en un orden fijo: **Y, luego X, luego Angular**.
Mientras está en `HOMING`, antes del `READY` final, el ESP32 emite dos
eventos no solicitados por eje (mínimo y máximo alcanzados) para que
la Raspberry Pi pueda calcular el rango disponible de movimiento — sin
progreso intermedio (ver "Cambio 2026-08-26" abajo).

**Excepción de enmarcado**: a diferencia de cualquier otra respuesta
ESP32 -> RPi (que es texto plano sin marco, ver Transporte), estos
eventos de calibración SÍ están envueltos en `<` `>`. Esto
deliberadamente NO se aplica retroactivamente a respuestas existentes
(`READY`, `TRAJ_PROGRESS`, `POSITION`, `ACK`, `ERROR`, etc.) — esas
siguen sin marco, como está documentado en Transporte.

| Respuesta | Formato | Notas |
|---|---|---|
| Límite mínimo alcanzado (Y/X) | `<LIM{AXIS}MIN>` | Sin argumentos. Define el cero crudo (paso 0) de ese eje. Uno de `<LIMYMIN>`, `<LIMXMIN>`. |
| Límite mínimo alcanzado (Angular) | `<LIMANGMIN:steps>` | **Distinto de Y/X**: un solo argumento entero, un conteo de pasos con signo (típicamente negativo) ya relativo a la horizontal — ver "Cambio 2026-08-26 (eje angular)" abajo. |
| Límite máximo alcanzado | `<LIM{AXIS}MAX:steps>` | Un solo argumento entero: el número de **pasos crudos de motor** en el límite máximo de ese eje — NO cm/grados. Uno de `<LIMYMAX:steps>`, `<LIMXMAX:steps>`, `<LIMANGMAX:steps>`. Para Y/X es un conteo positivo desde el mínimo (techos físicos conocidos del rig real: Y hasta 72000 pasos, X hasta 48000 pasos); para Angular es un conteo con signo relativo a la horizontal (techo físico conocido: hasta 5400 pasos), igual que su `LIMANGMIN`. |

Y/X se reportan en crudo/relativo-al-límite en el cable — `[0, steps]`.
El eje Angular se reporta ya relativo a la horizontal — `[min_steps,
max_steps]`, con `min_steps` típicamente negativo. Secuencia para un
`HOME` completo (valores de ejemplo, rig real):

```
<LIMYMIN>
<LIMYMAX:72000>
<LIMXMIN>
<LIMXMAX:48000>
<LIMANGMIN:-5000>
<LIMANGMAX:5400>
READY
```

La Raspberry Pi combina los 3 rangos crudos por eje (ya en pasos) en
el mapa completo del espacio de movimiento disponible, convirtiendo a
cm/grados de su lado (ver `SystemStateMachine.STEPS_PER_CM_Y`,
`STEPS_PER_CM_X`, `STEPS_PER_DEG_ANGLE` en `system_state.py`). Toda
esta secuencia ocurre una vez por llamada a `HOME` (que a su vez solo
ocurre una vez por sesión — ver `SystemStateMachine.can_home()`).
Notar que `READY` mismo, que cierra la secuencia, se mantiene sin
marco — es la respuesta preexistente de `HOME`, no uno de los eventos
de calibración.

**En la interfaz**: cada `LIM{AXIS}MIN`/`LIM{AXIS}MAX` que llega
mientras se está calibrando actualiza en vivo el texto de estado en la
pantalla de Inicio, ej. *"Calibrando eje Y: límite máximo alcanzado
(72000 pasos)."* (`_on_calibration_limit` en `connection_screen.py`).
Al terminar toda la secuencia (`READY`), el resultado ya convertido a
cm/grados se puede ver de forma permanente en la pantalla "Espacio
Disponible" (botón de la barra de navegación,
`calibration_map_window.py`) — un rectángulo con el rango real de X/Y y
un arco con el rango real del ángulo.

**Cambio 2026-08-26 (eje angular)**: el eje angular es el único de los
3 que NO es raw/relativo-al-límite en el cable. Sus límites mecánicos
(limit switches) no están ubicados en nivelado/horizontal — hay un
offset físico real entre "tocar el limit switch inferior" y "0°
horizontal". La primera versión de este cambio (más arriba en este
documento) resolvía esto con un offset fijo aplicado del lado de la
Raspberry Pi (`ANGLE_HORIZONTAL_OFFSET_DEG`) sobre un barrido crudo
`[0, a_steps]`, igual que Y/X. Esa versión quedó reemplazada: ahora es
el propio ESP32 el que reporta `LIMANGMIN`/`LIMANGMAX` ya como pasos
con signo relativos a la horizontal = paso 0 — por ejemplo `-5000` al
tocar el límite inferior y `5400` al tocar el máximo (valores del rig
real, coinciden con `HOMING_ZERO_MIN_K_STEPS`/`HOMING_ZERO_MAX_K_STEPS`
del script de generación de datos de movimiento del firmware
definitivo). La Raspberry Pi ya no aplica ningún offset propio para
este eje: solo lee los pasos con signo que el ESP32 (de prueba o
definitivo, cualquiera que sea) le entregue y los convierte a grados
dividiendo por `STEPS_PER_DEG_ANGLE` — ver
`SystemStateMachine._build_calibration_space`.
`ANGLE_HORIZONTAL_OFFSET_DEG` fue eliminado por completo del código.

**Cambio 2026-08-26 (reemplaza el diseño anterior basado en
`CAL_PROGRESS`)**: originalmente `LIM{AXIS}MAX` reportaba el rango
final ya convertido a unidades reales (cm/grados) por el ESP32, con un
evento `CAL_PROGRESS:axis:value` intermedio repetido mientras viajaba
hacia el máximo. El compañero que desarrolla el firmware definitivo
(Horizonte 2) indicó que ese cálculo de distancia no le sirve del lado
del ESP32: prefiere reportar únicamente pasos crudos de motor al
llegar al límite máximo de cada eje, sin progreso intermedio.
`CAL_PROGRESS` fue eliminado del protocolo por completo, y
`LIM{AXIS}MAX` ahora carga un conteo de pasos en vez de cm/grados — la
conversión pasos->unidades reales que antes hacía el ESP32 ahora la
hace la Raspberry Pi (ver
`SystemStateMachine._build_calibration_space`), usando las constantes
mecánicas reales del rig (husillos, microstepping, reducción angular)
que aparecen en el script de generación de datos de movimiento del
firmware definitivo. El firmware de prueba
(`firmware/gaitsim-esp32-test/main.cpp`) fue actualizado para
reflejar esto, usando los techos de pasos reales del rig
(Y=72000, X=48000, Angular de -5000 a 5400 — ver "Cambio 2026-08-26
(eje angular)" arriba) en vez de valores arbitrarios en cm/grados. Esto
NO es un cambio de código del firmware definitivo en sí (no se tocó
`Codigodemicompaneroparaelesp32.cpp`) — es una actualización del
protocolo, autoritativo para ambos firmwares, y del firmware de
prueba para mantenerlo compatible.

## Comandos de Movimiento Manual

Solo válidos cuando el sistema NO está calibrando y NO está ejecutando
una trayectoria.

| Comando | Formato | Respuesta |
|---|---|---|
| Movimiento manual | `<MANUAL:axis:direction:steps>` | `OK` o `ERROR:<code>:<msg>` |

- `<axis>`: `X` (horizontal), `Y` (vertical), `A` (ángulo sagital)
- `<direction>`: entero, `1` = `+`, `0` = `-` (ver la nota de
  Transporte sobre tipos de argumento arriba)
- `<steps>`: entero positivo

**⚠️ Implementación futura, no usado hoy por la interfaz.** `MANUAL`
es movimiento manual EN CRUDO: pasos de motor sin pasar por el espacio
calibrado (`CalibrationSpace`) — la Raspberry Pi no valida el destino
contra los límites de `HOME` antes de enviarlo, porque ni siquiera
conoce la conversión pasos->cm/grados para ese eje. Es, a propósito,
"movimiento manual sin calibración".

El joystick de movimiento manual que SÍ existe hoy (pantalla de
Inicio, `manual_joystick.py`, `_on_direction`) no usa esto — llama a
`SystemStateMachine.move_relative(axis, direction, amount)` en
cm/grados, que primero valida contra `CalibrationSpace` y luego genera
una trayectoria de un solo eje enviada por el protocolo de trayectoria
(`TRAJ_BEGIN`/`TRAJ_POINT`/`TRAJ_END` + `RUN`, ver esa sección abajo).
Ej.: tocar la flecha "arriba" con el paso en `5` -> se valida que
Y+5cm siga dentro del rango calibrado -> `move_relative("Y", "+",
5.0)` -> se genera y envía una trayectoria Y: actual -> actual+5cm
(X/ángulo fijos) -> `RUN`.

`MANUAL` quedó en el protocolo (a diferencia de `GOTO`/`STATUS`/`STOP`,
que sí se eliminaron por no tener ningún llamador real) como
implementación futura pendiente de decisión — útil el día que se
necesite mover un eje en pasos crudos sin la validación de rango
calibrado. Hoy nada lo ejercita ni de la RPi ni en las pruebas
mockeadas; si tu firmware no lo implementa, no rompe nada de esta app.

**Lo que se ve en el serial cuando el operador mueve el joystick**
(captura real: flecha "arriba", eje Y, paso de 3cm, sistema recién
homeado en `(0,0,0)`) — notar que es puro `TRAJ_BEGIN`/`TRAJ_POINT`/
`TRAJ_END`/`RUN`, **ningún** `<MANUAL:...>` en ningún momento:

```
>> <GET_POSITION>
<< POSITION:0:0:0
>> <TRAJ_BEGIN:7>
<< TRAJ_READY
>> <TRAJ_POINT:0:0:0:0>
<< ACK:0
>> <TRAJ_POINT:100:0:400:0>
<< ACK:1
>> <TRAJ_POINT:100:0:400:0>
<< ACK:2
>> <TRAJ_POINT:100:0:400:0>
<< ACK:3
>> <TRAJ_POINT:100:0:400:0>
<< ACK:4
>> <TRAJ_POINT:100:0:400:0>
<< ACK:5
>> <TRAJ_POINT:100:0:400:0>
<< ACK:6
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN>
<< RUNNING
<< TRAJ_PROGRESS:0.0000:0:0:0
<< TRAJ_PROGRESS:0.1000:0:400:0
<< TRAJ_PROGRESS:0.2000:0:800:0
<< TRAJ_PROGRESS:0.3000:0:1200:0
<< TRAJ_PROGRESS:0.4000:0:1600:0
<< TRAJ_PROGRESS:0.5000:0:2000:0
<< TRAJ_PROGRESS:0.6000:0:2400:0
<< FINISHED
```

3cm en Y son 2400 pasos (3 × `STEPS_PER_CM_Y`=800), repartidos en 6
tramos de 400 pasos cada 100ms — `generate_synchronized_trajectory`
sampleó el movimiento en varios puntos intermedios en vez de un solo
salto, por eso son 7 puntos (el primero, delta 0, es el punto de
partida) en vez de uno solo.

## Consulta de Posición

`GET_POSITION` es una consulta de solo lectura, permitida en
cualquier estado.

| Comando | Formato | Respuesta |
|---|---|---|
| Obtener posición | `<GET_POSITION>` | `POSITION:<x>:<y>:<angle>` |

- `<x>`, `<y>`, `<angle>`: **pasos crudos de motor, enteros, posición
  ABSOLUTA** (no delta) — NO cm/grados. Relativos a la referencia
  `(0, 0, 0)` establecida por el `HOME` exitoso más reciente. Ver
  "Cambio 2026-08-26" abajo.
- `GET_POSITION` reporta la posición rastreada actual del sistema, en
  pasos. La Raspberry Pi hace la conversión a cm/grados de su lado
  (`SystemStateMachine.get_position()`, mismas constantes
  `STEPS_PER_CM_Y`/`STEPS_PER_CM_X`/`STEPS_PER_DEG_ANGLE` que la
  calibración) — el ESP32 ya no necesita saber esa conversión para
  responder esta consulta.
- La posición rastreada también debe actualizarse a medida que se
  ejecuta una trayectoria (una vez por cada `TRAJ_POINT` alcanzado, es
  decir, en sincronía con cada `TRAJ_PROGRESS`), no solo por
  `MANUAL` — después de un `PAUSE` o `ABORT`, `GET_POSITION` es
  la única forma en que la Raspberry Pi sabe dónde se detuvo realmente
  el sistema a mitad de trayectoria, de lo cual depende la secuencia
  de reposicionamiento seguro (ver
  `SystemStateMachine.safe_return_to_position` en system_state.py).

**Cambio 2026-08-26**: mismo criterio que ya se aplicó a `LIM{AXIS}MAX`
— `POSITION` pasó de cm/grados (convertidos por el ESP32) a pasos
crudos, para los 3 ejes (X, Y, Angular), no solo X/Y. La Raspberry Pi
ya no le pide al ESP32 ninguna conversión de unidades para esta
consulta; solo lee pasos y los interpreta usando las mismas constantes
mecánicas reales del rig (`STEPS_PER_CM_X/Y`, `STEPS_PER_DEG_ANGLE`)
usadas para la calibración. `TRAJ_POINT` (dirección RPi -> ESP32)
recibió el mismo tratamiento el mismo día — ver Comandos de
Transferencia de Trayectoria, "Cambio 2026-08-26 (trayectorias en
pasos)".

**En la interfaz**: mientras el panel "Monitor Posición" está visible
(parte de la pantalla de Trayectorias), `<GET_POSITION>` se envía cada
200ms en un hilo aparte (`PositionPoller` en `platform_view.py`) para
animar el dibujo de la plataforma en tiempo real. Aparte de eso, cada
movimiento generado internamente (`move_relative`,
`safe_return_to_position`) empieza leyendo la posición actual con
`<GET_POSITION>` antes de calcular a dónde moverse.

**Lo que se ve en el serial** (captura real, justo después de un
`HOME`, y de nuevo tras mover X 5cm — 5 × `STEPS_PER_CM_X`=400 -> 2000):

```
>> <GET_POSITION>
<< POSITION:0:0:0
>> <GET_POSITION>
<< POSITION:2000:0:0
```

## Comandos de Transferencia de Trayectoria

Una trayectoria es una secuencia de puntos que comparten la misma base
de tiempo (se originan de un único CSV con columnas
`time, pos_x, pos_y, angle`, o de una trayectoria generada
internamente — ver "En la interfaz" abajo).

| Comando | Formato | Respuesta |
|---|---|---|
| Iniciar transferencia | `<TRAJ_BEGIN:n_points>` | `TRAJ_READY` |
| Enviar punto | `<TRAJ_POINT:dt_ms:dx_steps:dy_steps:dangle_steps>` | `ACK:<index>` |
| Finalizar transferencia | `<TRAJ_END>` | `TRAJ_STORED` o `ERROR:<code>:<msg>` |

`<n_points>` puede variar entre trayectorias (no está fijo a ninguna
cantidad específica, ej. no siempre ~120). El ESP32 debe validar que
la cantidad de mensajes `TRAJ_POINT` recibidos antes de `TRAJ_END`
coincida con `<n_points>`; un desajuste resulta en
`ERROR:POINT_COUNT_MISMATCH:...`.

**Cambio 2026-08-26 (trayectorias en pasos)**: `TRAJ_POINT` dejó de
llevar una posición absoluta en cm/grados (`t:x:y:angle`) y pasó a
llevar un **delta de pasos con signo**, enteros, relativo al punto
anterior: `dt_ms` (milisegundos desde el punto anterior),
`dx_steps`/`dy_steps`/`dangle_steps` (cuánto se mueve cada eje en ese
intervalo — pueden ser negativos). El **primer punto de cada
trayectoria también es un delta**, no una posición absoluta: su
referencia es la posición ACTUAL real del sistema (equivalente a un
`GET_POSITION` justo antes de `TRAJ_BEGIN`), no un supuesto "empieza en
cero". El ESP32 debe acumular estos deltas partiendo de su posición
rastreada actual (`posXSteps`/`posYSteps`/`posAngleSteps` en el
firmware de prueba) al recibir `TRAJ_BEGIN`, y seguir acumulando por
cada `TRAJ_POINT` recibido — ver `handleTrajBegin()`/`handleTrajPoint()`
en `firmware/gaitsim-esp32-test/main.cpp` como referencia. Este modelo
de deltas por intervalo (no posición absoluta por punto) es el mismo
que ya usa el script de generación de datos de movimiento del firmware
definitivo (`dx_steps`/`dy_steps`/`dk_steps` por intervalo `dt_ms`).

La Raspberry Pi (`SystemStateMachine._points_to_step_deltas`) redondea
cada posición ABSOLUTA a pasos primero, y recién ahí resta valores
YA REDONDEADOS consecutivos para obtener cada delta — nunca redondea
un delta de forma independiente — para que el error de redondeo nunca
se acumule a lo largo de una trayectoria larga (misma técnica que usa
el script del compañero: redondea cada posición absoluta a pasos,
después calcula las diferencias). `n_points` sigue siendo la misma
cantidad de puntos que la trayectoria original (no se descarta el
primero).

**En la interfaz, esta secuencia se dispara en 4 casos distintos —
todos por el mismo camino (`SystemStateMachine.send_trajectory()`),
nunca a mano por comando individual, y los 4 pasan por la MISMA
conversión a pasos de arriba desde que se implementó:**
1. Tocar "Load && Send" en la pantalla de Trayectorias (`send_button`,
   `_on_send_clicked` en `trajectory_screen.py`) — carga el CSV
   seleccionado y envía sus puntos.
2. La primera vez que se toca "Ir a Posición Inicial" tras un `HOME`
   (`connection_screen.py`, `_on_goto_initial_synchronized`) — genera
   una trayectoria sincronizada `(0,0,0) -> posición inicial`.
3. Cualquier movimiento del joystick manual (`move_relative`, ver
   Comandos de Movimiento Manual arriba) — una trayectoria de 1 solo
   eje.
4. "Reiniciar Ensayo" / "Elegir Otro Ensayo" / repetir "Ir a Posición
   Inicial" con una posición previa ya registrada
   (`safe_return_to_position`) — la secuencia de 5 pasos de
   reposicionamiento seguro.

En los 4 casos, la Raspberry Pi arma la lista completa de puntos
absolutos (cm/grados) en memoria PRIMERO, la convierte a deltas de
pasos, y recién ahí manda `TRAJ_BEGIN:n_points` -> un `TRAJ_POINT` por
delta -> `TRAJ_END` -> ver Comandos de Ejecución para lo que sigue
(`RUN`).

**Lo que se ve en el serial** (captura real: 3 puntos absolutos
`(t=0.0, x=0, y=0, angle=0)`, `(t=0.5, x=3.0, y=0, angle=0)`,
`(t=1.0, x=5.0, y=0, angle=0)` en cm, arrancando en `(0,0,0)` —
incluye `RUN` y `TRAJ_PROGRESS`, ver Comandos de Ejecución abajo):

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

Punto por punto: el 1er punto (t=0, x=0cm) coincide con la posición
actual → delta `0:0:0:0`. Del 1ro al 2do (0.5s, x pasa de 0 a 3cm =
1200 pasos) → `500:1200:0:0`. Del 2do al 3ro (0.5s más, x pasa de 3cm a
5cm = 800 pasos más) → `500:800:0:0`. `TRAJ_PROGRESS` reporta la
posición ABSOLUTA acumulada en cada punto (0, luego 1200, luego 2000
pasos) — no los deltas.

## Comandos de Ejecución

| Comando | Formato | Respuesta |
|---|---|---|
| Run | `<RUN>` | `RUNNING` inmediatamente, luego un `TRAJ_PROGRESS` por punto (en orden) mientras ejecuta, luego `FINISHED` cuando termina el ciclo |
| Pause | `<PAUSE>` | `PAUSED` |
| Resume | `<RESUME>` | `RUNNING` |
| Abort | `<ABORT>` | `ABORTED` si tiene éxito, `ERROR:INVALID_STATE:...` en caso contrario |

`ABORT` abandona por completo una trayectoria en pausa (el operador
eligió reiniciar el ensayo o correr uno distinto en vez de reanudar) y
devuelve el sistema a `IDLE`, descartando el progreso restante de la
trayectoria — a diferencia de `PAUSE`, que es reanudable con `RESUME`
(ver Flujo del Sistema abajo). Solo es válido mientras está `PAUSED`;
el ESP32 debe rechazarlo en cualquier otro caso con
`ERROR:INVALID_STATE:...`. La posición rastreada del sistema
(`GET_POSITION`) debe reflejar dónde se detuvo realmente la ejecución
— ver la nota bajo Consulta de Posición.

**En la interfaz**:
- `RUN` sale automáticamente al final de CUALQUIER envío de trayectoria
  (ver los 4 casos en Comandos de Transferencia de Trayectoria) — no
  hay un botón "Run" separado para el joystick manual ni para "Ir a
  Posición Inicial"; solo el ensayo cargado por CSV tiene un botón
  "Run" explícito (`run_button` en `trajectory_screen.py`,
  `_on_run_clicked`).
- "Pause"/"Resume" (`pause_button`/`resume_button`, misma pantalla)
  mandan `PAUSE`/`RESUME` directo, sin lógica extra.
- "Reiniciar Ensayo" (`restart_trial_button`) manda `ABORT` primero (si
  estaba pausado), luego repite la secuencia completa de
  `safe_return_to_position` + reenvío del CSV + `RUN` — todo con un
  solo toque. "Elegir Otro Ensayo" (`choose_other_button`) también
  manda `ABORT` primero, pero en vez de reintentar navega de vuelta a
  la pantalla de Inicio para elegir una posición distinta.

**Lo que se ve en el serial — `PAUSE` a mitad de una trayectoria y
`RESUME`** (captura real; nótese que `TRAJ_PROGRESS` simplemente se
detiene mientras está `PAUSED` y continúa exactamente donde iba al
recibir `RESUME`, sin reenviar nada):

```
>> <RUN>
<< RUNNING
<< TRAJ_PROGRESS:0.0000:0:2400:0
<< TRAJ_PROGRESS:0.3000:400:2400:0
<< TRAJ_PROGRESS:0.6000:800:2400:0
<< TRAJ_PROGRESS:0.9000:1200:2400:0
>> <PAUSE>
<< PAUSED
>> <RESUME>
<< RUNNING
<< TRAJ_PROGRESS:1.2000:1600:2400:0
<< TRAJ_PROGRESS:1.5000:2000:2400:0
<< FINISHED
```

**Lo que se ve en el serial — `PAUSE` y luego `ABORT`** (abandona la
trayectoria, no llega `FINISHED`):

```
>> <RUN>
<< RUNNING
<< TRAJ_PROGRESS:0.0000:0:2400:0
<< TRAJ_PROGRESS:0.3000:400:2400:0
<< TRAJ_PROGRESS:0.6000:800:2400:0
<< TRAJ_PROGRESS:0.9000:1200:2400:0
>> <PAUSE>
<< PAUSED
>> <ABORT>
<< ABORTED
```

### Progreso de ejecución (no solicitado, durante RUNNING)

| Respuesta | Formato | Notas |
|---|---|---|
| Progreso | `TRAJ_PROGRESS:<t>:<x>:<y>:<angle>` | Uno por cada punto de trayectoria almacenado, enviado en orden a medida que cada punto se "ejecuta". No es respuesta a ningún comando en particular — llega de forma asíncrona mientras el sistema está en RUNNING, igual que `FINISHED`. |

- `<t>`: segundos transcurridos desde el inicio del `RUN` (float) — SIN
  cambios, sigue igual que siempre.
- `<x>`, `<y>`, `<angle>`: **pasos crudos de motor, enteros** — NO
  cm/grados (cambio 2026-08-26, mismo criterio que `GET_POSITION`).
  Es la posición ABSOLUTA acumulada en ese punto (no un delta, a
  diferencia de `TRAJ_POINT`) — el firmware de prueba la calcula
  aplicando cada `TrajectoryStepPoint` ya acumulado del buffer (ver
  `handleRun()` en `main.cpp`). La Raspberry Pi convierte de vuelta a
  cm/grados en `SystemStateMachine._on_progress`, el único lugar donde
  eso pasa, antes de entregarlo a la gráfica/marcador de posición.

**En la interfaz**: cada `TRAJ_PROGRESS` alimenta DOS cosas a la vez,
sin importar qué disparó el `RUN` (un ensayo CSV, un jog manual, o un
reposicionamiento) — la gráfica en vivo (pos_x/pos_y/ángulo vs. tiempo,
pyqtgraph) en la pantalla de Trayectorias, Y el marcador que se mueve
sobre el dibujo de la plataforma en "Monitor Posición". Solo la
gráfica se filtra para mostrar únicamente el ensayo del operador
(`_plot_active` en `trajectory_screen.py`) — el marcador de posición
sigue cualquier movimiento.

**Provisional**: la cadencia exacta (no la unidad, ya fija en pasos)
está definida por el firmware de prueba
(`firmware/gaitsim-esp32-test/main.cpp`) para soportar el graficado en
vivo en la Raspberry Pi (ver CLAUDE.md). El firmware de prueba emite un
`TRAJ_PROGRESS` cada 120ms fijos por punto, sin importar el `dt_ms`
real de ese punto — esta cadencia es solo de prueba, únicamente para
hacer visible el gráfico en vivo, y NO representa el timing real de
ejecución. El firmware definitivo (Horizonte 2) tendrá su propio timing
real entre puntos (desconocido por ahora), pero se espera que emita la
misma estructura por punto (`t,x_steps,y_steps,angle_steps`) para que
el lado de la Raspberry Pi no necesite cambiar cuando ese firmware esté
disponible.

## Formato de Error

Todos los errores siguen la misma estructura, sin importar qué comando
los haya provocado:


ERROR:<code>:<short message>

Ejemplos:
- `ERROR:LIMIT_REACHED:X axis`
- `ERROR:INVALID_STATE:cannot home while running`
- `ERROR:POINT_COUNT_MISMATCH:expected 120, got 118`

Los códigos de error todavía no están formalmente enumerados; a medida
que se identifiquen nuevas condiciones de error durante el desarrollo
del firmware, deben documentarse aquí.

## Flujo del Sistema (referencia)

1. La RPi envía `<PING>` para verificar el enlace.
2. La RPi envía `<HOME>`. El ESP32 realiza el homing y el mapeo de
   límites, reinicia su posición rastreada a `(0, 0, 0)`, y luego
   responde `READY`. Esto ocurre una vez por sesión de encendido.
3. Opcionalmente, la RPi mueve el sistema a una posición inicial antes
   de enviar/correr un ensayo (solo mientras está IDLE). No existe un
   comando de salto directo a una posición absoluta en el protocolo
   (`GOTO` existió y fue eliminado por no tener ningún llamador real,
   ver Notas de Diseño) — este primer movimiento se hace igual que
   cualquier ensayo, generando una trayectoria sincronizada de los 3
   ejes y enviándola por `TRAJ_BEGIN`/`TRAJ_POINT`/`TRAJ_END` + `RUN`
   (ver `trajectory_generator.generate_synchronized_trajectory` en
   system_state.py). La posición se puede ajustar finamente después con
   el joystick manual (ver Comandos de Movimiento Manual — también
   trayectorias de 1 eje, no `MANUAL`) y su resultado en unidades
   reales se puede leer de vuelta con `GET_POSITION`.
4. Para cada ensayo:
   - La RPi envía `<TRAJ_BEGIN:n_points>`
   - La RPi envía `<TRAJ_POINT:...>` por cada punto
   - La RPi envía `<TRAJ_END>`
   - La RPi envía `<RUN>`
   - El ESP32 responde `RUNNING`, luego `FINISHED` cuando termina
   - El sistema queda listo para la siguiente trayectoria sin
     necesidad de rehacer el homing
5. El movimiento manual solo se acepta cuando el sistema está idle (no
   homing, no running) — hoy viaja como una trayectoria de 1 eje
   (`TRAJ_BEGIN`/`TRAJ_POINT`/`TRAJ_END` + `RUN`), no como `<MANUAL:...>`
   (ver la advertencia en Comandos de Movimiento Manual).
6. `<PAUSE>` pausa la ejecución de forma reanudable; `<RESUME>`
   continúa desde donde quedó. `<ABORT>` (solo válido mientras está
   `PAUSED`) abandona la trayectoria en pausa por completo y regresa a
   `IDLE` en vez de reanudarla.

## Notas de Diseño

- Se eligió texto plano en vez de binario por legibilidad durante el
  desarrollo/depuración y porque los payloads de trayectoria son
  pequeños (decenas a algunos cientos de puntos por ensayo).
- Consolidar `x`, `y`, `angle` en un único mensaje `TRAJ_POINT` (en vez
  de tres transferencias separadas por eje) fue una decisión
  deliberada: los tres ejes comparten la misma base de tiempo dentro
  de un ciclo de marcha, así que enviarlos juntos evita que el
  firmware tenga que sincronizar tres arreglos independientes.
- **Comandos eliminados (2026-08-26) por no tener ningún llamador
  real** en la Raspberry Pi (mismo criterio ya aplicado antes a
  `MOVE_REL`): `GOTO` (el salto a posición absoluta quedó reemplazado
  por la trayectoria sincronizada, ver Flujo del Sistema), `STATUS`
  (consulta de estado, nunca se usó — el estado se rastrea localmente
  en `SystemStateMachine`), y `STOP` (pausa reanudable duplicada de
  `PAUSE`, que es la que la UI realmente usa). `PING` sí se conservó y
  además se le dio un uso real: la Raspberry Pi ahora lo envía al
  conectar para confirmar que hay un ESP32 respondiendo antes de
  considerar la conexión exitosa (ver Comandos del Sistema arriba).
  **No hace falta implementarlos** — si tu firmware no los reconoce,
  esto es lo que se ve en el serial (captura real contra el firmware de
  prueba, que tampoco los implementa), y la app sigue funcionando sin
  problema:
  ```
  >> <GOTO:1:1:1>
  << ERROR:UNKNOWN_COMMAND:GOTO:1:1:1
  >> <STATUS>
  << ERROR:UNKNOWN_COMMAND:STATUS
  >> <STOP>
  << ERROR:UNKNOWN_COMMAND:STOP
  ```
- **`MANUAL` NO se eliminó** con el resto — a diferencia de esos 3, es
  implementación futura pendiente de decisión (movimiento en pasos
  crudos, sin la validación de rango calibrado que sí tiene
  `move_relative()`), no código descartado. Ver la advertencia bajo
  Comandos de Movimiento Manual.
- **`TRAJ_POINT`/`TRAJ_PROGRESS` a pasos (2026-08-26, "trayectorias en
  pasos")**: mismo día, mismo criterio que `LIM{AXIS}MAX`/`POSITION`,
  se extendió a los dos mensajes de trayectoria — `TRAJ_POINT`
  (RPi -> ESP32) pasó de posición absoluta en cm/grados a delta de
  pasos por intervalo (`dt_ms:dx:dy:dangle`), y `TRAJ_PROGRESS`
  (ESP32 -> RPi) pasó de posición absoluta en cm/grados a posición
  absoluta en pasos. Aplica a las 4 formas en que la interfaz genera
  una trayectoria por igual (ensayo CSV, joystick, posición inicial,
  retorno seguro) porque las 4 comparten el mismo método de envío
  (`SystemStateMachine.send_trajectory()`) — no fue posible migrarlas
  una por una sin partir el protocolo en dos formatos coexistentes. Ver
  Comandos de Transferencia de Trayectoria y Progreso de ejecución
  arriba para el detalle completo (incluyendo la técnica de redondeo
  sin deriva, igual a la del script del compañero). Verificado en
  hardware real (firmware de prueba) antes de darlo por cerrado, mismo
  criterio que el resto de los cambios de esta sesión.
