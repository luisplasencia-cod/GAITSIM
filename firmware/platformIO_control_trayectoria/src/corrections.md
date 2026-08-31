# Pendientes de correccion

## Movimiento de ejes e interrupciones

Estas notas corresponden a `axis_move_steps()`, las ISR asociadas y las funciones de estado/parada de ejes.

1. **Verificacion de frecuencia**
   - Validar que `steps_per_sec` sea positivo y este dentro de un maximo seguro para el motor, el driver y el temporizador.
   - Evitar que `1000000 / steps_per_sec` o su semiperiodo resulte en cero.
   - Revisar el caso en que una velocidad calculada para un punto de trayectoria se trunque a cero.

2. **Decision sobre `limit_triggered`**
   - Definir si un final de carrera debe dejar el sistema bloqueado hasta un rearme explicito, o si la bandera se restablecera bajo una condicion controlada.
   - Si se mantiene como bloqueo global, definir el comando o la secuencia segura de rearme/calibracion.

3. **Verificacion de limites de movimiento (tentativa)**
   - Evaluar validar la posicion objetivo contra `steps_min_range` y `steps_max_range` antes de iniciar un movimiento.
   - Habilitar esta comprobacion solamente tras una calibracion valida de cada eje.
   - Mantener siempre los finales de carrera fisicos como proteccion final, aun si se implementan limites por software.

4. **Interfaz de movimiento relativo (deseable)**
   - Evaluar que `axis_move_steps()` reciba un desplazamiento firmado en coordenadas globales, en lugar de una magnitud de pasos y un nivel electrico `direction` por separado.
   - Calcular internamente el nivel del pin DIR con `ax.positive_dir`; los callers solo expresarian pasos positivos hacia MAX y negativos hacia MIN.
   - Considerar renombrarla a `axis_move_relative_steps()` para reflejar su contrato.

## Calibracion de rango

Estas notas corresponden a `axis_calibrate_range()` y `run_calibration()`.

1. **Presupuestos de pasos por fase**
   - Antes de iniciar, rechazar la calibracion si los limites MIN y MAX del mismo eje aparecen activos al mismo tiempo.
   - Limitar los pasos acumulados al liberar MIN si este ya esta presionado.
   - Limitar los pasos acumulados al buscar MIN.
   - Limitar los pasos acumulados al buscar MAX.
   - Limitar los pasos acumulados al liberar MAX despues de alcanzarlo.
   - Los presupuestos deben ser conservadores: menores que el recorrido hasta un tope mecanico duro y con margen para el tamano del ultimo segmento de busqueda.
   - Si una fase agota su presupuesto sin lograr la condicion esperada, detener todos los ejes, marcar la calibracion como fallida y no conservar el rango como valido.
   - Solo marcar el eje como calibrado despues de completar todas las fases correctamente.

## Coordinacion entre comandos de alto nivel

### `handle_manual_relative_command()`

- La interaccion entre los comandos de esta familia esta completa: `MANUAL` es no bloqueante y un nuevo `MANUAL` recibe `BUSY` mientras algun eje esta en movimiento.
- `MANUAL_STOP` es idempotente y puede responder `STOPPED` aunque el sistema ya este detenido.
- Falta implementar la coordinacion con comandos de otras familias de alto nivel. Una trayectoria activa, pausada o pendiente debe impedir comandos manuales incompatibles, aunque los ejes queden momentaneamente detenidos entre puntos.
- Validacion de parametros deseable: validar `parameter_count` antes de leer el paquete, exigir el formato esperado y validar el identificador de eje.

### `handle_manual_absolute_command()`

- La interaccion entre los comandos de esta familia esta completa: `GOTO` es no bloqueante y un nuevo `GOTO` recibe `BUSY` mientras algun eje esta en movimiento.
- `GOTO_STOP` es idempotente y puede responder `STOPPED` aunque el sistema ya este detenido.
- Falta implementar la coordinacion con comandos de otras familias de alto nivel. Una trayectoria activa, pausada o pendiente debe impedir comandos absolutos incompatibles, aunque los ejes queden momentaneamente detenidos entre puntos.
- Validacion de parametros deseable: validar `parameter_count` antes de leer el paquete y exigir las tres coordenadas objetivo esperadas.

### `handle_send_position_command()`

- Falta implementar la coordinacion con comandos de otras familias, particularmente `HOME`/calibracion. La posicion no debe considerarse valida antes de completar una calibracion satisfactoria.
- Validacion de parametros deseable: validar que `GET_POSITION` no incluya parametros.

### `handle_receive_trajectory_command()`

- Falta implementar la interaccion entre comandos de la misma familia: controlar la secuencia `TRAJ_BEGIN` -> `TRAJ_POINT` -> `TRAJ_END` y evitar transferencias incompletas o puntos fuera de orden.
- Interaccion con comandos de otras familias, aunque de menor prioridad: decidir que comandos externos se rechazan mientras una trayectoria esta siendo cargada.
- Validacion de parametros deseable: validar longitud de trayectoria, indice de punto, cantidad de parametros y limites de los arreglos antes de escribir los datos recibidos.

### `handle_execute_calibration_command()`

- Falta implementar la interaccion entre comandos de la misma familia si la calibracion se convierte en una operacion asincrona: definir estados de inicio, ejecucion, finalizacion y fallo de `HOME`.
- Falta implementar la coordinacion con comandos de otras familias: rechazar `HOME` si existe una trayectoria activa, pausada o pendiente, o un movimiento manual en curso.
- Deseable ejecutar `run_calibration()` desde `auxiliar_task` para que `HOME` no bloquee la tarea de comunicacion. Esto requiere un estado de calibracion y exclusion mutua con la ejecucion de trayectorias.
- Validacion de parametros deseable: validar que `HOME` no incluya parametros.

## Recepcion de comandos serie

Estas notas corresponden a `communication_rx()` en `communication.h`.

1. **Longitud maxima de campos**
   - Limitar la longitud del comando recibido antes de escribir en el buffer temporal.
   - Limitar tambien la longitud de cada parametro recibido; el mismo buffer temporal se reutiliza para comando y parametros.
   - Reservar siempre una posicion para el terminador nulo `\0`.

2. **Cantidad maxima de parametros**
   - Verificar `packet.parameter_count < MAX_PARAMETERS` antes de escribir en `packet.parameters[]`.

3. **Timeout de recepcion**
   - Definir un timeout entre caracteres para descartar una trama incompleta y evitar que `primary_task` espere indefinidamente su cierre `>`.

4. **Resincronizacion de trama**
   - Si se recibe un nuevo `<` mientras se esta leyendo una trama, reiniciar el parser y tratarlo como inicio de una nueva trama.

5. **Conversion numerica estricta (deseable)**
   - Reemplazar `atoi()` por una conversion que detecte texto no numerico y desbordes, por ejemplo `strtol()` con validacion completa del campo.
