# Síntesis de trayectoria de marcha desde datos antropométricos

**Estado**: diseño + prototipo Fase 1 (`prototype.py`) escritos y
corriendo standalone. NO integrado a la app, NO probado contra un
paciente/sujeto real, NO tocado nada de `src/`. Esto es investigación +
código exploratorio, no una feature lista para usar.

**Origen**: idea de Luis (2026-07-31) — dejar de depender de que un CSV
de trayectoria sea capturado/grabado externamente (mocap u otro medio)
para cada paciente, y en vez de eso generar esa trayectoria con un
algoritmo que solo necesite medidas antropométricas (estatura, longitud
de pierna, masa, etc.), acercando el "simulador" a ser realmente
generativo. Pedido explícito: investigar primero cómo lo hacen sistemas
reales/publicados antes de diseñar el algoritmo propio, con nivel de
rigor de tesis.

---

## 1. El problema, en términos de este proyecto

Hoy (ver `CLAUDE.md`, `src/utils/trajectory_loader.py`,
`data/trajectories/`) una trayectoria de ensayo es un CSV con columnas
`tiempo, pos_x, pos_y, angulo` que alguien capturó previamente y que el
operador simplemente carga y envía. Los dos CSVs reales que hay en el
repo dan una pista concreta de qué representan físicamente estos 3
grados de libertad:

- `data/trajectories/balanceo_v4.csv` ("balanceo" = fase de swing/
  balanceo de la marcha): 54 puntos, t: 0→0.53s, X: 0→44.6cm, Y:
  0→2.1cm, ángulo: -44.3°→22.4° (barrido ~66.7°).
- `data/trajectories/apoyo_test.csv` ("apoyo" = fase de stance/apoyo):
  solo 3 puntos, claramente un archivo de prueba, no un ensayo real.

Este patrón — desplazamiento horizontal grande, vertical pequeño,
rotación sagital de decenas de grados, sobre ~0.4-0.6s — es consistente
con la trayectoria de un segmento distal de la pierna durante la fase
de swing de un ciclo de marcha, no con un ciclo completo stance+swing.

**Confirmado por Luis (2026-07-31)**: X, Y, ángulo son la trayectoria
del **punto de montaje de la prótesis sobre el segmento tibial**
(la tibia/pantorrilla) — NO un punto fijo anatómico como "el tobillo".
Ese punto puede estar a distinta distancia de la rodilla según el
paciente: una amputación transtibial **alta** (mucho muñón/tibia
remanente, punto de montaje más cerca de donde estaría el tobillo),
**media**, o **corta** (poco muñón, punto de montaje más cerca de la
rodilla). Esto cambia el modelo por completo respecto al primer
borrador de esta sección — ver sección 2.4.

## 2. Qué hace la literatura / los sistemas reales (investigación 2026-07-31)

Búsqueda hecha con WebSearch + WebFetch, resumen de lo encontrado
(fuentes al final de esta sección):

### 2.1 No existe un algoritmo "de la nada": todo parte de una referencia

Revisé `awesome-biomechanics` (lista curada, 999★ en GitHub, github.com/
modenaxe/awesome-biomechanics) buscando específicamente herramientas
que generen cinemática de marcha *solo* desde antropometría, sin datos
de movimiento previos. Conclusión explícita de esa búsqueda: **no
existe tal cosa**. Todo enfoque real parte de uno de estos dos caminos:

**(a) Escalar una curva de marcha de referencia/normativa** — el
enfoque dominante, barato computacionalmente, y el que most gait labs
usan en la práctica:
- Winter's 2-D Walking Data (dataset normativo clásico de su libro de
  biomecánica, isbweb.org/resources/data-resources/140-movement-data/
  508-gait-data) — curvas de ángulo articular normalizadas al 0-100%
  del ciclo de marcha, de sujetos sanos.
- GaitRec (Nature Scientific Data, 75,732 ensayos de marcha sana y
  patológica, www.nature.com/articles/s41597-020-0481-z) — dataset
  público mucho más grande, con metadata antropométrica por sujeto.
- El método de **normalización no-dimensional de Hof (1996)**, "Scaling
  gait data to body size" — la técnica estándar para adaptar una curva
  de referencia a un sujeto distinto: las distancias se escalan por
  longitud de pierna, el tiempo se escala por el período del ciclo
  (derivado de la cadencia), típicamente vía el número de Froude
  (similaridad dinámica, v² / (g·L)) para mantener el mismo patrón de
  marcha "equivalente" a distintos tamaños corporales. Confirmado en
  varios papers que esto reduce drásticamente la varianza explicada por
  altura/peso en los datos (de hasta 82% sin normalizar, a ≤6% con
  normalización correcta).

**(b) Simulación predictiva musculoesquelética** — el enfoque de mayor
fidelidad, pero mucho más caro:
- **OpenSim** (opensim.stanford.edu, opensim-org/opensim-core en
  GitHub, 1089★) — estándar de facto en biomecánica computacional.
  Permite "scaling" de un modelo genérico a las dimensiones de un
  sujeto específico (a partir de marcadores o medidas antropométricas)
  y correr dinámica inversa/directa sobre ese modelo escalado.
- **SCONE** — software de simulación predictiva de movimiento biológico
  que optimiza patrones de activación muscular para lograr un objetivo
  de alto nivel (velocidad de marcha, eficiencia energética, evitar
  dolor), generando la trayectoria como *resultado* de esa optimización
  en vez de copiarla de datos.
- Esto es research-grade: requiere un modelo musculoesquelético
  completo, resolver optimización de control óptimo, y expertise
  específico de biomecánica computacional. Fuera de alcance razonable
  para este proyecto (ver sección 4).

### 2.2 Enfoques más recientes basados en aprendizaje (nivel intermedio)

Encontré un paper directamente relevante: **"A Personalized Trajectory
Planning Approach for Exoskeleton Robots Using GPR and Fourier Series"**
(2026, doi.org/10.3390/electronics14234554) — construye un mapeo
data-driven, vía Gaussian Process Regression, entre parámetros
antropométricos (longitud de muslo, longitud de pantorrilla, peso) y
los **coeficientes de una serie de Fourier** que describe la curva de
movimiento articular, permitiendo generar patrones de marcha
personalizados para antropometrías distintas sin requerir datos de
marcha ya grabados de ese sujeto específico — pero sí requiere un
dataset de entrenamiento (sujetos con antropometría + trayectoria
conocida) para ajustar el GPR.

Otros enfoques encontrados (redes neuronales, LSTM, GRNN) predicen
ángulos articulares pero típicamente a partir de señales de sensores en
tiempo real (EMG, IMU) durante la marcha misma, no desde antropometría
estática sola — no aplican directamente a este caso de uso (acá se
necesita generar el CSV *antes* de mover al paciente, no en tiempo
real).

### 2.3 Conclusión de la investigación

Para este proyecto, replicar el enfoque (a) — escalamiento no-
dimensional de una curva de referencia — es lo que un programa real
haría en su versión más simple y accesible, y es lo único razonable de
construir "desde cero" a nivel de tesis sin un dataset de entrenamiento
propio. El enfoque GPR+Fourier (sección 2.2) es un buen objetivo de
Fase 2 si en algún momento se recolectan o consiguen varios ensayos
reales con antropometría conocida. OpenSim/SCONE quedan fuera de
alcance salvo que el proyecto explícitamente decida invertir en
biomecánica computacional avanzada.

### 2.4 Consecuencia de la aclaración de Luis: esto es un problema de cinemática de cuerpo rígido, no solo de escalamiento

Como el punto de montaje está **sobre el mismo segmento tibial rígido**
en toda su longitud, dos puntos a distinta distancia de la rodilla,
sobre ese mismo segmento, en el mismo instante, comparten exactamente
el mismo ángulo — el ángulo del segmento no depende de dónde se mide
sobre él. Lo que cambia es la posición (x, y): un punto a distancia
`d` de la rodilla, con el segmento en ángulo `θ(t)`, está desplazado
respecto a la rodilla en `d · (cos θ(t), sin θ(t))` (cinemática de
segmento rígido — link-segment model, el modelo estándar de análisis
de marcha, ver p.ej. Winter, *Biomechanics and Motor Control of Human
Movement*, cap. de cinemática de segmentos).

**Esto significa que hay DOS fuentes de variación independientes**, no
una sola:
1. **Tamaño corporal / patrón de marcha general del paciente** — afecta
   la amplitud y el timing de la marcha completa. Es lo que la sección
   2.1 (escalamiento no-dimensional de Hof) ya cubre.
2. **Dónde, sobre el segmento tibial, está el punto de montaje** —
   depende del nivel de amputación (alta/media/corta), NO del tamaño
   corporal general del paciente. Es una propiedad de la cirugía/
   muñón, independiente de la altura del paciente.

Si se conoce la trayectoria de un punto de referencia a distancia
`d_ref` de la rodilla (ej. la de `balanceo_v4.csv`, con `d_ref`
lamentablemente NO documentado — ver Pregunta abierta #2), la
trayectoria de OTRO punto a distancia `d_nuevo` se obtiene por simple
desplazamiento rígido, sin necesitar volver a medir marcha completa:

```
x_nuevo(t) = x_ref(t) + (d_nuevo - d_ref) · cos(θ(t))
y_nuevo(t) = y_ref(t) + (d_nuevo - d_ref) · sin(θ(t))
ángulo_nuevo(t) = θ(t)                          (sin cambio — mismo segmento)
```

Implementado en `prototype.py` como `apply_residual_limb_offset()`.
**Advertencia explícita**: la convención exacta de qué significa
`ángulo = 0°` y hacia qué eje apunta (cos vs sin, signos) en la
plataforma real NO está confirmada — el prototipo asume la convención
matemática estándar (ángulo medido desde +X, sentido antihorario) por
default, documentado como supuesto no verificado. Si está mal, el
desplazamiento saldría rotado/reflejado respecto al real. Ver Pregunta
abierta #5.

**Clasificación de longitud de muñón transtibial (investigación
2026-07-31)** — para poder traducir "alta/media/corta" a una distancia
en cm concreta, busqué qué usa la literatura de prótesis/ortopedia:
- Longitud "óptima"/media observada clínicamente: **~16.0 cm** medida
  desde la meseta tibial (tibial plateau, línea de la rodilla) hasta el
  extremo del hueso, en una serie clínica de 93 amputados.
- Muñones **cortos**: **<15.1 cm** — asociados a fuerza muscular
  significativamente menor y peor control del socket protésico.
- Muñón **ideal/largo**: descrito en otra fuente como aproximadamente
  **un tercio de la longitud tibial original**, preservando la
  inserción del tendón rotuliano — esta cifra (≈1/3 de la tibia
  completa, típicamente ~33-40cm en un adulto → ≈11-13cm) **no
  reconcilia limpiamente** con los ~16cm/15.1cm de la fuente anterior
  (probablemente miden desde puntos de referencia distintos, o
  "un tercio" se refiere a un límite superior de seguridad de tejido
  blando más que a un valor típico). **No inventé una conversión única
  para resolver esta discrepancia** — se documenta tal cual se
  encontró, sin forzar consistencia que la literatura misma no da.
- No encontré umbrales porcentuales universalmente estandarizados
  (%-de-tibia-completa) para "alta/media/corta" en esta búsqueda —
  distintas fuentes usan distintos puntos de referencia. **Esto
  necesita confirmación de quien supervise la parte clínica/protésica
  de la tesis** (¿tu compañero?, ¿un asesor de prótesis?), no debería
  quedar fijado solo con lo que encontré en la web — ver Pregunta
  abierta #4.

Fuentes de esta subsección: [What are the recommended residual limb
lengths for above-knee and below-knee amputations to optimize
prosthetic fitting?](https://www.droracle.ai/articles/863673/what-are-the-recommended-residual-limb-lengths-for-aboveknee),
resultados de búsqueda citando series clínicas sobre longitud de muñón
transtibial y resistencia muscular/control de socket.

**Fuentes principales**:
- [A Personalized Trajectory Planning Approach for Exoskeleton Robots Using GPR and Fourier Series](https://doi.org/10.3390/electronics14234554)
- [Scaling gait data to body size (Hof, 1996)](https://www.researchgate.net/publication/238247532_Scaling_gait_data_to_body_size)
- [Comprehensive non-dimensional normalization of gait data](https://www.sciencedirect.com/science/article/abs/pii/S0966636215009625)
- [awesome-biomechanics (curated list, 999★)](https://github.com/modenaxe/awesome-biomechanics)
- [Winter's 2-D Walking Data](https://isbweb.org/resources/data-resources/140-movement-data/508-gait-data)
- [GaitRec dataset](https://www.nature.com/articles/s41597-020-0481-z)
- [OpenSim](https://opensim.stanford.edu/) / [opensim-core (1089★)](https://github.com/opensim-org/opensim-core)

## 3. Diseño propuesto (por fases)

### Fase 0 — Definir qué representa físicamente cada eje (RESUELTA 2026-07-31)
Confirmado por Luis — ver sección 1 y 2.4: es el punto de montaje de la
prótesis sobre el segmento tibial, cuya distancia a la rodilla varía
según el nivel de amputación transtibial (alta/media/corta). Sigue
habiendo una sub-pregunta abierta sobre valores exactos en cm — ver
Pregunta abierta #4.

### Fase 1 — Dos transformaciones independientes y componibles (implementado como prototipo, ver `prototype.py`)

**1a. Escalamiento no-dimensional por tamaño corporal** (patrón general
de marcha del paciente — sección 2.1, sin cambios respecto al diseño
original):
1. Tomar **una** trayectoria real ya existente en el repo
   (`balanceo_v4.csv`) como curva de referencia — no porque sea
   necesariamente "la" curva normativa correcta, sino porque es el
   único dato concreto disponible ahora mismo y sirve para probar el
   mecanismo de escalamiento end-to-end.
2. Normalizar esa curva de referencia al 0-100% del ciclo (adimensional
   en tiempo) y a unidades de longitud de pierna (adimensional en
   distancia) — método de Hof.
3. Dado un nuevo paciente con `leg_length_cm` (obligatorio) y
   opcionalmente `cadence_steps_per_min` o `speed_m_s` (si no se da,
   estimarla vía similaridad dinámica/número de Froude — placeholder
   de literatura, ver `prototype.py`), reconstruir la curva escalada a
   las unidades reales de ESE paciente.
4. El ángulo sagital, según la literatura de normalización, típicamente
   NO se reescala por tamaño corporal (ya es adimensional por
   naturaleza) — se mantiene igual que la referencia salvo evidencia en
   contra.

**1b. Desplazamiento rígido por nivel de amputación** (posición del
punto de montaje sobre el segmento tibial — NUEVO, sección 2.4,
independiente de 1a):
5. Con la curva ya escalada por tamaño corporal (paso 1a) como nueva
   referencia intermedia, aplicar `apply_residual_limb_offset()`: dado
   `d_ref` (distancia de montaje asumida para la curva de referencia —
   PLACEHOLDER sin confirmar, Pregunta abierta #2) y `d_nuevo`
   (distancia de montaje del paciente actual, derivada de su
   clasificación alta/media/corta — Pregunta abierta #4), desplazar
   cada punto `(d_nuevo - d_ref)` a lo largo de la dirección del
   segmento en ese instante (mismo ángulo `θ(t)`, sin cambios).
6. Resamplear a un `List[TrajectoryPoint]` (mismo tipo que ya usa
   `protocol.py`/`trajectory_generator.py`) para que, el día que esto
   se integre, entre por el mismo camino
   `TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN` que cualquier trayectoria de
   CSV o generada — sin protocolo nuevo, mismo patrón que
   `trajectory_generator.py` ya estableció para la trayectoria
   sincronizada inicial.

El orden importa: primero escalar por tamaño corporal (1a, cambia
amplitud/tiempo de TODA la curva), después desplazar por nivel de
amputación (1b, es un offset en cm absolutos sobre el segmento, no algo
que deba normalizarse por tamaño corporal — son dos propiedades físicas
distintas del paciente, no la misma cosa medida dos veces).

### Fase 2 — Refinamiento con más datos (no iniciado, requiere datos)
Si se consiguen varios ensayos reales con antropometría conocida
(propios o de un dataset público como GaitRec), reemplazar el
escalamiento lineal por el enfoque GPR + coeficientes de Fourier de la
sección 2.2 — mapea antropometría → forma de curva de forma aprendida,
no solo escalada, y debería capturar mejor variaciones no explicadas
por tamaño corporal solo (ej. patologías, asimetrías).

### Fase 3 — Integración a la app (no iniciado, depende de que Horizonte 2 esté listo, per pedido explícito de Luis)
- Nuevo módulo `src/utils/trajectory_synthesizer.py` (nombre
  tentativo), misma forma que `trajectory_generator.py`.
- Nueva sección de UI (probablemente en `connection_screen.py` o una
  pantalla nueva) para ingresar antropometría en vez de/además de
  elegir un CSV.
- Reusar `trajectory_validator.py` para validar la trayectoria
  generada contra el `CalibrationSpace` antes de enviarla — igual que
  ya hace `trajectory_generator.py`.

## 4. Qué NO se está proponiendo (alcance explícitamente descartado)
- OpenSim/SCONE o cualquier simulación musculoesquelética predictiva
  completa — demasiado esfuerzo/expertise para el alcance de esta
  tesis, salvo que Luis decida explícitamente invertir en eso.
  Se documenta como referencia (así se sabe que existe y por qué se
  descartó), no como plan.
- Cualquier entrenamiento de red neuronal/ML pesado en esta fase —
  Fase 1 es puramente paramétrico (sin datos de entrenamiento), Fase 2
  (GPR) es la única extensión con aprendizaje contemplada, y solo si
  hay datos.

## 5. Preguntas abiertas (necesitan a Luis, no se pueden resolver solo con código)

1. ~~¿Qué segmento/punto físico representan X, Y, ángulo?~~ **RESUELTA
   2026-07-31** — punto de montaje de la prótesis sobre el segmento
   tibial (ver sección 1 y 2.4).
2. **¿A qué distancia de la rodilla estaba montado el punto que generó
   `balanceo_v4.csv` (`d_ref`)?** Sin este dato, el desplazamiento
   rígido por nivel de amputación (paso 1b) no tiene punto de partida
   real — el prototipo usa un placeholder sin confirmar
   (`REFERENCE_MOUNT_DISTANCE_CM` en `prototype.py`) que hay que
   reemplazar antes de confiar en el resultado para un paciente nuevo.
3. **¿Se dispone de la longitud de pierna del sujeto que generó
   `balanceo_v4.csv`?** Mismo problema que #2 pero para el
   escalamiento por tamaño corporal (paso 1a) — placeholder en
   `REFERENCE_LEG_LENGTH_CM`.
4. **¿Qué distancias en cm (o qué criterio) usa tu compañero/tu asesor
   para clasificar "alta/media/corta"?** La investigación (sección 2.4)
   encontró cifras de literatura general (~16cm medio, <15.1cm corto,
   ~1/3 de tibia como límite largo) que **no reconcilian entre sí** y
   no vienen de una fuente específica de este proyecto — no deberían
   quedar como valor final sin que alguien con criterio clínico las
   confirme o corrija.
5. **¿Cuál es la convención real de `ángulo` respecto a los ejes (x,
   y)?** `apply_residual_limb_offset()` asume `(cos θ, sin θ)` con
   `θ=0` apuntando a +X — supuesto matemático estándar, NO confirmado
   contra la definición mecánica real de la plataforma. Si está mal, el
   desplazamiento por nivel de amputación sale en la dirección
   equivocada aunque la magnitud sea correcta.
6. **¿Vale la pena pedir acceso a un dataset público (GaitRec)** para
   tener más de una curva de referencia por tipo de fase (balanceo/
   apoyo) en vez de depender de un solo CSV sin metadata antropométrica
   propia? Esto también destrabaría Fase 2 antes.
7. **¿La fase de "apoyo" (stance) se integra a este mismo mecanismo?**
   `apoyo_test.csv` es claramente un archivo de prueba (3 puntos) — no
   hay curva de referencia real de apoyo todavía en el repo.

## 6. Estado del prototipo (`prototype.py`)

Standalone, no importa nada de `src/ui/` ni de `main.py`. Sí importa
`TrajectoryPoint` de `src/communication/protocol.py` (un dataclass sin
I/O, de solo lectura — no se modifica nada ahí) para que el resultado
ya tenga la forma exacta que el resto de la app espera el día que esto
se integre. Corre con `python3 prototype.py` desde esta carpeta.

Implementa las dos transformaciones de la Fase 1:
- `generate_scaled_trajectory()` (1a, sin cambios respecto a la primera
  versión): escalamiento no-dimensional por tamaño corporal.
- `apply_residual_limb_offset()` (1b, nuevo 2026-07-31): desplazamiento
  rígido a lo largo del segmento tibial por nivel de amputación.
- `generate_patient_trajectory()`: compone ambas — punto de entrada
  único, toma `leg_length_cm` + `mount_distance_cm` (+ cadencia
  opcional) y devuelve el `List[TrajectoryPoint]` final.

El demo hace tres verificaciones:
1. Sanity check de 1a (roundtrip con `leg_length` = referencia):
   reproduce `balanceo_v4.csv` con error ~1e-15.
2. Sanity check de 1b (roundtrip con `mount_distance` = referencia):
   mismo resultado, confirma que el offset es cero cuando no hay
   cambio de distancia de montaje.
3. Demo combinado: 3 longitudes de pierna × 3 distancias de montaje
   (corta/media/larga, con los valores de literatura de la sección
   2.4), mostrando cómo cambia la trayectoria resultante en cada caso.

**Lo que esto NO confirma** (siguen sin resolver, ver sección 5): que
`d_ref`/`REFERENCE_LEG_LENGTH_CM` sean los valores reales del sujeto de
`balanceo_v4.csv` (son placeholders), que la convención de ángulo
asumida sea la correcta, ni que los valores de "alta/media/corta" en
cm sean los que este proyecto específicamente debería usar. El sanity
check solo confirma que la matemática de ambas transformaciones es
internamente consistente (ida y vuelta sin error), no que el resultado
sea biomecánicamente correcto para un paciente nuevo real.
