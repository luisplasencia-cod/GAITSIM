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
| `IDLE` | Homed, listo para `TRAJ_BEGIN` |
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
| Manual | `<MANUAL:axis_index:delta_steps>` | `OK`, `BUSY` o `ERROR:code:msg` |
| Detener manual | `<MANUAL_STOP>` | `STOPPED` |

`axis_index`: `0`=X, `1`=Y, `2`=Á (angular) — numérico, NO letra (todo
parámetro después del nombre del comando se parsea con `atoi()` en el
firmware). `delta_steps`: entero **con signo** — la dirección va
incluida en el signo, no hay campo `direction` separado. `BUSY` llega
si el eje pedido ya está en movimiento (una llamada `MANUAL` previa
todavía no terminó); `MANUAL_STOP` no tiene parámetros y es idempotente
(responde `STOPPED` aunque nada estuviera moviéndose).

**Sin requisito de estado**: a diferencia de `TRAJ_BEGIN`/`RUN`,
`MANUAL`/`MANUAL_STOP` NO requieren `HOME` previo — el firmware no
necesita un origen calibrado para mover un motor, solo los finales de
carrera físicos como protección (misma protección que existe después
de calibrar). Por eso la RPi los usa también ANTES del primer `HOME`
(ver "¿Cuándo se usa?" abajo).

`MANUAL` es **no bloqueante** en el firmware: `OK` confirma que el
movimiento fue aceptado y arrancó, no que ya terminó — el eje sigue
moviéndose en segundo plano hasta completar los pasos pedidos o hasta
`MANUAL_STOP`/un final de carrera.

**¿Cuándo se usa?**
- Antes del primer `HOME` (pantalla de Conexión, caja "Movimiento
  Manual de Prueba"): la RPi manda `MANUAL` directo, en pasos crudos,
  sin conversión a cm/grados ni validación de rango — solo sirve para
  confirmar que cada eje responde y gira en el sentido esperado antes
  de calibrar. Nuevo 2026-09-02 (antes no se usaba en absoluto).
- Después de calibrar, el joystick de la pantalla de Trayectorias
  sigue usando una trayectoria de 1 solo eje (ver sección Trayectorias
  abajo), NO `MANUAL` — ese camino sí necesita el mapa de calibración
  para validar rango, así que se mantiene sin cambios.

**Cambio 2026-09-02 (formato real de MANUAL)**: `docs/protocol.md` y el
código de la RPi asumían `<MANUAL:axis:direction:steps>` (eje como
letra, dirección `1`/`0` separada, pasos sin signo) — ese formato
nunca coincidió con el firmware definitivo real del compañero, que usa
el formato de 2 parámetros de arriba. Corregido tras revisar
`firmware/platformIO_control_trayectoria/src/main.cpp` (copia
compartida por Luis, ya no la stale de sesiones previas).

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
| Iniciar | `<TRAJ_BEGIN:tipo:n_points>` | `TRAJ_READY` o `ERROR:code:msg` |
| Punto (CON tiempo — solo ensayo/marcha) | `<TRAJ_POINT:index:dt_ms:dx_steps:dy_steps:dangle_steps>` | `ACK:index` |
| Punto (SIN tiempo — todo lo demás) | `<TRAJ_POINT:index:dx_steps:dy_steps:dangle_steps>` | `ACK:index` |
| Finalizar | `<TRAJ_END>` | `TRAJ_STORED` o `ERROR:code:msg` |
| Correr | `<RUN:tipo>` | `RUNNING`/`ACTIVE`, luego un `TRAJ_PROGRESS` por punto, luego `FINISHED` |
| Pausar | `<PAUSE>` | `PAUSED` |
| Reanudar | `<RESUME>` | `RUNNING`/`ACTIVE` |
| Abortar | `<ABORT>` (solo válido en `PAUSED`) | `ABORTED` o `ERROR:INVALID_STATE:...` |
| Progreso (no solicitado) | `TRAJ_PROGRESS:t:x:y:angle` | — |
| Estado (solicitado) | `<TRAJ_STATUS>` | `RUNNING`/`ACTIVE`, `PAUSED`, `FINISHED` o `ERROR:INVALID_STATE:...` |

**Cambio 2026-09-02 (`ACTIVE` == `RUNNING`)**: el firmware real del
compañero responde `ACTIVE` (no `RUNNING`) mientras una trayectoria
sigue en ejecución — en `RUN`/`RESUME` y en las respuestas a
`TRAJ_STATUS`. Confirmado por Luis como el vocabulario definitivo de
ese firmware, así que la RPi trata ambas palabras como el MISMO estado
(`parse_response()` en `protocol.py` mapea las dos al mismo `kind`) en
vez de esperar que el firmware cambie. Motivo: sin este mapeo,
`TRAJ_STATUS`/`PAUSE` no reconocían la respuesta y cada intento se
quedaba colgado el timeout completo (3s) antes de fallar — visto en
hardware real, ver la retro en `CLAUDE.md` (sesión 2026-09-02).

También reconciliado ese mismo día: el firmware real todavía responde
`ERROR` a secas (sin `:código:mensaje`) cuando rechaza un comando —
p.ej. un `PAUSE` fuera de lugar. La RPi lo acepta como un `ERROR`
genérico (código vacío) mientras el compañero no agregue códigos
específicos; una vez que los agregue, ese `ERROR` a secas debería dejar
de aparecer y este párrafo puede borrarse.

**Cambio 2026-09-01 (`TRAJ_BEGIN`/`RUN` con tipo)**: ambos comandos
ahora llevan un `tipo` — `1` = CON tiempo, `2` = SIN tiempo — así el
firmware sabe de antemano qué forma de `TRAJ_POINT` va a recibir
(4 campos o 3), en vez de tener que inferirlo contando los `:` de cada
línea. `RUN` manda el MISMO `tipo` que su `TRAJ_BEGIN` correspondiente,
como verificación cruzada: si no coincide, el ESP32 debe responder
`ERROR:TYPE_MISMATCH:...` en vez de arrancar. `RESUME` NO lleva `tipo`
— continúa una ejecución que el ESP32 ya clasificó en el `RUN` original.

**Cambio 2026-09-01 (`TRAJ_POINT` con índice)**: cada `TRAJ_POINT` ahora
lleva su propio `index` (0-based) como PRIMER parámetro, antes que los
demás — en ambas formas, CON y SIN tiempo — coincidiendo con el
`n_points` de su `TRAJ_BEGIN` (`0..n_points-1`). Robustez extra: el
ESP32 debe verificar que el `index` recibido coincide exactamente con
cuántos puntos ya recibió (`ERROR:POINT_INDEX_MISMATCH:...` si no
coincide) — independiente del chequeo de conteo total que ya hace
`TRAJ_END`. Distinto de `ACK:index` (la respuesta del ESP32, que ya
existía) — ese es el eco del ESP32 hacia la RPi, este `index` nuevo es
lo que la RPi manda hacia el ESP32.

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
>> <TRAJ_BEGIN:1:2>
<< TRAJ_READY
>> <TRAJ_POINT:0:500:1200:0:0>
<< ACK:0
>> <TRAJ_POINT:1:500:800:0:0>
<< ACK:1
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN:1>
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
>> <TRAJ_BEGIN:2:1>
<< TRAJ_READY
>> <TRAJ_POINT:0:800:0:0>
<< ACK:0
>> <TRAJ_END>
<< TRAJ_STORED
>> <RUN:2>
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

**Cambio 2026-09-01 (`TRAJ_STATUS`, robustez)**: hasta ahora, la RPi
sabía que una trayectoria terminó únicamente por el `FINISHED` no
solicitado que llega al final de `RUN`/`RESUME`. Como robustez extra
(por si esa línea se pierde o llega corrupta en el cable), la RPi ahora
también pide `<TRAJ_STATUS>` activamente cada 1 segundo, empezando justo
después de recibir la confirmación `RUNNING` (de `RUN` o `RESUME`), y
hasta que detecta un estado terminal — aplica igual a trayectorias CON
tiempo (ensayo/marcha) y SIN tiempo (joystick, posición inicial, retorno
seguro), ya que es independiente de la forma de `TRAJ_POINT`. La
respuesta reutiliza las mismas palabras que ya existían para
`RUNNING`/`PAUSED`/`FINISHED` — no hay formato nuevo. `PAUSE`/`ABORT`
detienen este sondeo del lado RPi de inmediato (ya tienen su propia
respuesta síncrona); fuera de `RUNNING`/`PAUSED` el ESP32 responde
`ERROR:INVALID_STATE:...`.

**Ejemplo real — polling durante RUNNING:**
```
>> <RUN:1>
<< RUNNING
<< TRAJ_PROGRESS:0.5000:1200:0:0
>> <TRAJ_STATUS>
<< RUNNING
<< TRAJ_PROGRESS:1.0000:2000:0:0
>> <TRAJ_STATUS>
<< FINISHED
```
(el `FINISHED` no solicitado normal puede llegar antes, después, o
casi junto con la respuesta de `TRAJ_STATUS` — la RPi solo reacciona a
la primera de las dos que le llegue)

## Errores

```
ERROR:code:mensaje corto
```
Ej.: `ERROR:LIMIT_REACHED:X axis`, `ERROR:INVALID_STATE:cannot home while running`,
`ERROR:POINT_COUNT_MISMATCH:expected 120, got 118`.

## Comandos eliminados — no los implementes

`GOTO`, `STATUS`, `STOP` existieron y se quitaron por no tener ningún
uso real. Si tu firmware no los reconoce, esto es lo normal (no rompe
nada). Nota: `STATUS` (aquí) y `TRAJ_STATUS` (2026-09-01, ver
Trayectorias arriba) son comandos DISTINTOS — el primero sigue
eliminado, no lo reintroduzcas.
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
