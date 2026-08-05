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
  contraria (ver la excepción de enmarcado de `CAL_PROGRESS` abajo).
  Ningún formato usa `,` como separador.
- **Tipos de argumento (comandos RPi -> ESP32 con 2+ argumentos)**: el
  primer argumento puede ser un string (ej. `axis`); todo argumento
  después del primero debe ser numérico — entero, o (como en
  `TRAJ_POINT`/`GOTO`, donde todos los argumentos ya son numéricos) un
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
| Consulta de estado | RPi -> ESP32 | `<STATUS>` | `STATUS:<state>` |

`<state>` debe ser uno de los siguientes 6 valores — los mismos
estados que usa el `SystemStateMachine` de la Raspberry Pi (ver la
sección "State machine states" de CLAUDE.md), así que el firmware debe
mantener un enum interno que refleje esto y simplemente reportar el
valor que esté activo en cada momento (no hace falta lógica adicional
en el handler, ver `handleStatus()` del firmware de prueba en
`firmware/gaitsim-esp32-test/main.cpp` como referencia):

| `<state>` | Cuándo el ESP32 debe estar en este estado |
|---|---|
| `DISCONNECTED` | Estado inicial al arrancar, antes de que `HOME` haya tenido éxito al menos una vez en esta sesión |
| `HOMING` | Mientras ejecuta el barrido de calibración, entre recibir `<HOME>` y enviar `READY` |
| `IDLE` | Homed, sin ejecutar trayectoria, sin recibir trayectoria — listo para `MANUAL`/`GOTO`/`TRAJ_BEGIN` |
| `RECEIVING_TRAJECTORY` | Entre `<TRAJ_BEGIN>` y `<TRAJ_END>` |
| `RUNNING` | Después de `<RUN>`, antes de `FINISHED`/`PAUSED`/`ABORTED` |
| `PAUSED` | Después de `<PAUSE>` o `<STOP>`, antes de `<RESUME>` o `<ABORT>` |

## Eventos de Calibración (no solicitados, durante HOMING)

`<HOME>` dispara el homing Y un barrido completo de mapeo de límites
de los 3 ejes, en un orden fijo: **Y, luego X, luego Angular**.
Mientras está en `HOMING`, antes del `READY` final, el ESP32 emite una
secuencia de eventos no solicitados por eje para que la Raspberry Pi
pueda calcular el rango disponible de movimiento — mismo espíritu que
`TRAJ_PROGRESS` durante `RUNNING`.

**Excepción de enmarcado**: a diferencia de cualquier otra respuesta
ESP32 -> RPi (que es texto plano sin marco, ver Transporte), estos
eventos de calibración SÍ están envueltos en `<` `>`, y los dos
argumentos de `CAL_PROGRESS` siguen la misma forma string-primero/
numérico-el-resto, separada por dos puntos, que los comandos RPi ->
ESP32 con múltiples argumentos como `MANUAL` — por pedido
explícito, para que esta convención de argumentos se aplique de forma
uniforme sin importar la dirección. Esto deliberadamente NO se aplica
retroactivamente a respuestas existentes (`READY`, `TRAJ_PROGRESS`,
`POSITION`, `STATUS`, `ACK`, `ERROR`, etc.) — esas siguen sin marco,
como está documentado en Transporte.

| Respuesta | Formato | Notas |
|---|---|---|
| Límite mínimo alcanzado | `<LIM{AXIS}MIN>` | Sin argumentos. Define el cero crudo de ese eje. Uno de `<LIMYMIN>`, `<LIMXMIN>`, `<LIMANGMIN>`. |
| Progreso de recorrido | `<CAL_PROGRESS:axis:value>` | `axis` (string, primer argumento) uno de `Y`/`X`/`A`. `value` (numérico, segundo argumento) es la distancia recorrida hasta ahora desde el mínimo de ese eje, en unidades reales (cm para Y/X, grados para `A`) — misma convención de unidades que `GET_POSITION`/`GOTO`, nunca pasos crudos de motor (ver Comandos de Posicionamiento Absoluto). Se envía repetidamente mientras viaja hacia el límite máximo. |
| Límite máximo alcanzado | `<LIM{AXIS}MAX:value>` | Un solo argumento numérico: el rango final medido para ese eje (mismas unidades que `CAL_PROGRESS`) — autoritativo sin importar si se recibió cada `CAL_PROGRESS` intermedio. Uno de `<LIMYMAX:value>`, `<LIMXMAX:value>`, `<LIMANGMAX:value>`. |

Los 3 ejes se reportan en crudo/relativo-al-límite en el cable —
`[0, value]`. Secuencia para un `HOME` completo:

```
<LIMYMIN>
<CAL_PROGRESS:Y:...>   (repetido)
<LIMYMAX:y_range>
<LIMXMIN>
<CAL_PROGRESS:X:...>   (repetido)
<LIMXMAX:x_range>
<LIMANGMIN>
<CAL_PROGRESS:A:...>   (repetido)
<LIMANGMAX:a_range>
READY
```

La Raspberry Pi combina los 3 rangos crudos por eje en el mapa
completo del espacio de movimiento disponible. Toda esta secuencia
ocurre una vez por llamada a `HOME` (que a su vez solo ocurre una vez
por sesión — ver `SystemStateMachine.can_home()`). Notar que `READY`
mismo, que cierra la secuencia, se mantiene sin marco — es la
respuesta preexistente de `HOME`, no uno de los nuevos eventos de
calibración.

**El eje angular es relativo a la horizontal, aplicado del lado de la
RPi — NO es una preocupación del formato de cable**: el `0` crudo de
`LIMANGMIN` es donde se toca el *límite mecánico (limit switch)*, que
NO es lo mismo que nivelado/horizontal — hay un offset físico real
entre ambos. En vez de complicar el protocolo de cable (que se
mantiene uniforme entre los 3 ejes, como arriba), la Raspberry Pi
aplica un único offset fijo, al reducir el barrido crudo `[0, a_range]`
al mapa final de espacio disponible, de modo que el rango de ángulo
reportado termine siendo relativo a la horizontal en vez de relativo
al limit switch. Ver `SystemStateMachine.ANGLE_HORIZONTAL_OFFSET_DEG`
en `system_state.py` para el valor placeholder actual (`-44.0`) y
`SystemStateMachine.CalibrationSpace` para dónde se aplica.

**Provisional**: al igual que `TRAJ_PROGRESS`, la cadencia exacta de
`CAL_PROGRESS` y el rango simulado por eje están definidos por el
firmware de prueba (`firmware/gaitsim-esp32-test/main.cpp`)
puramente para ejercitar este flujo de punta a punta, y no representan
el timing real de homing ni los límites mecánicos reales. Se espera
que el firmware definitivo (Horizonte 2) emita la misma estructura de
eventos (`LIM*MIN`, `CAL_PROGRESS`, `LIM*MAX`) con timing real y
valores derivados de los limit switches reales.

## Comandos de Movimiento Manual

Solo válidos cuando el sistema NO está calibrando y NO está ejecutando
una trayectoria.

| Comando | Formato | Respuesta |
|---|---|---|
| Movimiento manual | `<MANUAL:axis:direction:steps>` | `OK` o `ERROR:<code>:<msg>` |
| Stop | `<STOP>` | `STOPPED` |

- `<axis>`: `X` (horizontal), `Y` (vertical), `A` (ángulo sagital)
- `<direction>`: entero, `1` = `+`, `0` = `-` (ver la nota de
  Transporte sobre tipos de argumento arriba)
- `<steps>`: entero positivo

## Comandos de Posicionamiento Absoluto

`GOTO` solo es válido cuando el sistema está IDLE (no homing, no
recibiendo trayectoria, no corriendo) — misma restricción que
Movimiento Manual. `GET_POSITION` es una consulta de solo lectura,
permitida en cualquier estado (como `STATUS`).

| Comando | Formato | Respuesta |
|---|---|---|
| Ir a posición | `<GOTO:x:y:angle>` | `OK` o `ERROR:<code>:<msg>` |
| Obtener posición | `<GET_POSITION>` | `POSITION:<x>:<y>:<angle>` |

- `<x>`, `<y>`, `<angle>`: mismas unidades y marco de referencia que
  `TRAJ_POINT` abajo — `x`/`y` en cm, `angle` en grados, relativos a
  la referencia `(0, 0, 0)` establecida por el `HOME` exitoso más
  reciente.
- `GOTO` se mueve directamente a una posición absoluta, a diferencia
  del movimiento relativo y basado en pasos de `MANUAL`.
- `GET_POSITION` reporta la posición rastreada actual del sistema,
  incluyendo el efecto acumulado de cualquier movimiento `MANUAL`
  realizado desde el último `HOME` o `GOTO`. Esta es la única forma en
  que la Raspberry Pi puede conocer el resultado en unidades reales de
  un movimiento manual basado en pasos, ya que ella misma no conoce
  ninguna conversión de pasos a unidades — esa conversión es
  responsabilidad del firmware, igual que la calibración física que ya
  realiza durante `HOME`.
- La posición rastreada también debe actualizarse a medida que se
  ejecuta una trayectoria (una vez por cada `TRAJ_POINT` alcanzado, es
  decir, en sincronía con cada `TRAJ_PROGRESS`), no solo por
  `MANUAL`/`GOTO` — después de un `PAUSE` o `ABORT`, `GET_POSITION` es
  la única forma en que la Raspberry Pi sabe dónde se detuvo realmente
  el sistema a mitad de trayectoria, de lo cual depende la secuencia
  de reposicionamiento seguro (ver
  `SystemStateMachine.safe_return_to_position` en system_state.py).

## Comandos de Transferencia de Trayectoria

Una trayectoria es una secuencia de puntos, cada uno con tiempo,
posición horizontal, posición vertical, y ángulo sagital, todos
compartiendo la misma base de tiempo (se originan de un único CSV con
columnas `time, pos_x, pos_y, angle`).

| Comando | Formato | Respuesta |
|---|---|---|
| Iniciar transferencia | `<TRAJ_BEGIN:n_points>` | `TRAJ_READY` |
| Enviar punto | `<TRAJ_POINT:t:x:y:angle>` | `ACK:<index>` |
| Finalizar transferencia | `<TRAJ_END>` | `TRAJ_STORED` o `ERROR:<code>:<msg>` |

`<n_points>` puede variar entre trayectorias (no está fijo a ninguna
cantidad específica, ej. no siempre ~120). El ESP32 debe validar que
la cantidad de mensajes `TRAJ_POINT` recibidos antes de `TRAJ_END`
coincida con `<n_points>`; un desajuste resulta en
`ERROR:POINT_COUNT_MISMATCH:...`.

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
trayectoria — a diferencia de `STOP`, que es una pausa reanudable (ver
Flujo del Sistema abajo). Solo es válido mientras está `PAUSED`; el
ESP32 debe rechazarlo en cualquier otro caso con
`ERROR:INVALID_STATE:...`. La posición rastreada del sistema
(`GET_POSITION`) debe reflejar dónde se detuvo realmente la ejecución
— ver la nota bajo Comandos de Posicionamiento Absoluto.

### Progreso de ejecución (no solicitado, durante RUNNING)

| Respuesta | Formato | Notas |
|---|---|---|
| Progreso | `TRAJ_PROGRESS:<t>:<x>:<y>:<angle>` | Uno por cada punto de trayectoria almacenado, enviado en orden a medida que cada punto se "ejecuta". No es respuesta a ningún comando en particular — llega de forma asíncrona mientras el sistema está en RUNNING, igual que `FINISHED`. |

**Provisional**: este mensaje y su cadencia exacta están definidos por
el firmware de prueba (`firmware/gaitsim-esp32-test/main.cpp`) para
soportar el graficado en vivo en la Raspberry Pi (ver CLAUDE.md). El
firmware de prueba emite un `TRAJ_PROGRESS` cada 120ms fijos por
punto, sin importar el valor propio de `t` del punto — esta cadencia
es solo de prueba, únicamente para hacer visible el gráfico en vivo, y
NO representa el timing real de ejecución. El firmware definitivo
(Horizonte 2) tendrá su propio timing real entre puntos (desconocido
por ahora), pero se espera que emita la misma estructura por punto
(`t,x,y,angle`) para que el lado de la Raspberry Pi no necesite
cambiar cuando ese firmware esté disponible.

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
3. Opcionalmente, la RPi envía `<GOTO:x:y:angle>` para moverse a una
   posición inicial antes de enviar/correr una trayectoria (solo
   mientras está IDLE). La posición se puede ajustar finamente después
   con movimientos `MANUAL` y su resultado en unidades reales se puede
   leer de vuelta con `GET_POSITION`.
4. Para cada ensayo:
   - La RPi envía `<TRAJ_BEGIN:n_points>`
   - La RPi envía `<TRAJ_POINT:...>` por cada punto
   - La RPi envía `<TRAJ_END>`
   - La RPi envía `<RUN>`
   - El ESP32 responde `RUNNING`, luego `FINISHED` cuando termina
   - El sistema queda listo para la siguiente trayectoria sin
     necesidad de rehacer el homing
5. El movimiento manual (`<MANUAL:...>`) solo se acepta cuando el
   sistema está idle (no homing, no running).
6. `<STOP>` actúa como una pausa inmediata durante la ejecución;
   `<RESUME>` continúa desde donde quedó.

## Notas de Diseño

- Se eligió texto plano en vez de binario por legibilidad durante el
  desarrollo/depuración y porque los payloads de trayectoria son
  pequeños (decenas a algunos cientos de puntos por ensayo).
- Consolidar `x`, `y`, `angle` en un único mensaje `TRAJ_POINT` (en vez
  de tres transferencias separadas por eje) fue una decisión
  deliberada: los tres ejes comparten la misma base de tiempo dentro
  de un ciclo de marcha, así que enviarlos juntos evita que el
  firmware tenga que sincronizar tres arreglos independientes.
