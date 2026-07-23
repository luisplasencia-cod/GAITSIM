# GAITSIM Control

Aplicación de control de alto nivel (Raspberry Pi 5) para un simulador de
marcha biomecánico de 3 grados de libertad (X, Y, ángulo). Un ESP32 se
encarga del control de motores en tiempo real; esta app se encarga de la
interfaz de usuario, la orquestación de la secuencia de movimientos y el
manejo de datos (trayectorias, posiciones guardadas, logs).

Este documento es el punto de entrada al repositorio: explica qué hace
cada carpeta, dónde está cada pieza del sistema y —lo más importante—
**a qué archivo ir según el tipo de cambio que quieras hacer**.

---

## 1. Cómo está armado el sistema (visión general)

```
Raspberry Pi (esta app)  <-- USB Serial, 115200 baud, ASCII -->  ESP32 (motores)
```

- La Raspberry Pi es el **maestro**: manda comandos, el ESP32 los ejecuta
  y responde.
- El protocolo de comunicación (formato exacto de cada mensaje) está
  documentado en [`docs/protocol.md`](docs/protocol.md) — es la fuente
  de verdad. Cualquier firmware (de prueba o definitivo) debe cumplirlo.
- Actualmente se trabaja contra un **firmware de prueba** que simula el
  comportamiento de los motores (no hay motores reales conectados
  todavía). El firmware definitivo (motores reales) lo está
  desarrollando un compañero por separado — ver sección 5.

### Entorno de desarrollo
- Raspberry Pi OS (Bookworm), Python 3.13, **PySide6** (no PyQt6 — por
  licencia LGPL).
- Dependencias en [`requirements.txt`](requirements.txt) (`pip install -r requirements.txt`
  dentro de un entorno virtual).
- Correr la app: `python3 main.py` — necesita una sesión gráfica
  (pantalla táctil o VNC); no renderiza solo por SSH.
- Configuración básica (nombre del proyecto, puerto serial, baudrate,
  versión) en [`config.py`](config.py).

---

## 2. Guía de carpetas: qué hay y para qué sirve

### `src/communication/` — Comunicación con el ESP32
| Archivo | Qué hace |
|---|---|
| `protocol.py` | Define el **formato de los mensajes** (parseo/armado de comandos). No hace ningún I/O real. |
| `serial_manager.py` | Transporte serial "crudo": abre el puerto, hilo de lectura en background. No sabe nada del protocolo, solo manda/recibe líneas de texto. |
| `esp32_controller.py` | Capa que sí conoce el protocolo: expone llamadas bloqueantes con timeout (home, mover, correr trayectoria) más callbacks asíncronos para eventos que llegan solos del ESP32. |

**Si querés cambiar el protocolo de comunicación o agregar un comando
nuevo**: empezá acá, y actualizá `docs/protocol.md` en el mismo cambio.

### `src/controllers/` — Lógica de negocio (sin nada de interfaz gráfica)
| Archivo | Qué hace |
|---|---|
| `system_state.py` | **La pieza más importante del backend.** Máquina de estados (`SystemStateMachine`): única fuente de verdad del estado de la app, decide qué acciones están permitidas en cada momento (ej: no se puede mover manualmente mientras está `RUNNING`). |
| `initial_position_session.py` | Guarda la posición inicial (x, y, ángulo) configurada para la próxima trayectoria, y si corresponde a un archivo guardado o no. |

**Si querés cambiar una regla de negocio** (qué se puede hacer en qué
estado, la secuencia de reposicionamiento seguro entre ensayos, etc.):
acá, en `system_state.py`.

### `src/ui/` — Interfaz gráfica (Qt / PySide6)
| Archivo | Qué hace |
|---|---|
| `bridge.py` | Traduce los callbacks de `SystemStateMachine` (que llegan desde el hilo serial) a **señales Qt** thread-safe. Es la única puerta de entrada de Qt hacia el resto del sistema. |
| `style.py` | Constantes centralizadas de estilo para interfaz táctil (tamaños, fuentes, hoja de estilos general). |
| `main_window.py` | Ventana principal: crea y comparte la única instancia de `ESP32Controller`/`SystemStateMachine`/`Bridge`, maneja la navegación entre pantallas. Las pantallas no se conocen entre sí. |
| `status_indicator.py` | El "foquito" circular que muestra el estado de conexión/sistema en cualquier pantalla. |
| `calibration_map_window.py` | Ventana aparte que muestra el mapa de espacio de movimiento calculado durante el barrido de límites de un HOME, con un marcador en vivo (posición + orientación) que sigue a la plataforma mientras se ejecuta cualquier trayectoria. |
| `monitor_3d_window.py` | Ventana "Monitor Posición": vista lateral 2D (no 3D pese al nombre del archivo) de la plataforma en tiempo real, actualizada por polling. |

### `src/ui/screens/` — Pantallas de la app
| Archivo | Qué hace |
|---|---|
| `welcome_screen.py` | Pantalla de bienvenida (tap-to-continue) con video de fondo. |
| `connection_screen.py` | Conectar al ESP32, Home, movimiento manual por eje, ir a posición inicial. El primer viaje a la posición inicial tras un HOME usa una trayectoria sincronizada (los 3 ejes llegan juntos) en vez de un salto instantáneo — ver `trajectory_generator.py` abajo. |
| `trajectory_screen.py` | Elegir/cargar/enviar trayectoria, controles Run/Pause/Resume, gráficas en vivo, reiniciar ensayo. |

**Regla estricta del proyecto**: las pantallas de `src/ui/screens/`
**nunca** llaman a `ESP32Controller` directamente — siempre pasan por
`SystemStateMachine` a través del bridge compartido. Si estás por
agregar una acción nueva en una pantalla, primero fijate si
`SystemStateMachine` ya expone el método `can_x()`/`x()` correspondiente;
si no existe, hay que agregarlo ahí antes, no saltarse la capa.

### `src/utils/` — Utilidades de datos
| Archivo | Qué hace |
|---|---|
| `trajectory_loader.py` | Lee un CSV de trayectoria y lo convierte en una lista de puntos (tiempo, pos_x, pos_y, ángulo). |
| `trajectory_library.py` | Lista y resuelve trayectorias disponibles por id, buscando en `data/trajectories/`. |
| `position_library.py` | Guarda/lista/carga posiciones iniciales con nombre, como archivos JSON en `data/positions/`. |
| `trajectory_generator.py` | GENERA (no carga de archivo) una trayectoria sincronizada desde `(0,0,0)` hasta un punto objetivo, validada contra el espacio calibrado (`CalibrationSpace`). Se envía y ejecuta con el mismo protocolo `TRAJ_BEGIN/TRAJ_POINT/TRAJ_END/RUN` que cualquier trayectoria de CSV — no agrega comandos nuevos al protocolo. |

### `data/` — Datos de uso de la app
- `data/trajectories/` — CSVs de trayectorias "oficiales" que la app
  ofrece para elegir. Convención de nombre: `<fase>_<version>.csv`
  (ej: `balanceo_v4.csv`). Un archivo = una trayectoria.
- `data/test_trajectories/` — CSVs de prueba/control, no necesariamente
  para uso en producción.
- `data/positions/` — Posiciones iniciales guardadas (JSON), generadas
  por la app al guardar una posición desde la interfaz.

**Si querés agregar una trayectoria nueva**: poné el CSV en
`data/trajectories/` siguiendo la convención de nombre — no hace falta
tocar código, `trajectory_library.py` la detecta sola.

### `docs/` — Documentación técnica
- `protocol.md` — especificación completa y autoritativa del protocolo
  de comunicación Raspberry Pi ↔ ESP32. **Cualquier cambio al formato
  de mensajes debe reflejarse acá.**

### `firmware/` — Código que corre en el ESP32
- `firmware/gaitsim-esp32-test/main.cpp` — firmware de **prueba**, simula
  el comportamiento de los motores. Es el que se usa activamente hoy
  para verificar el sistema de punta a punta.
- `firmware/Codigodemicompaneroparaelesp32.cpp` — firmware **definitivo**
  (controla los motores reales), en desarrollo por un compañero de
  equipo, en paralelo y por separado. **No está terminado ni integrado
  todavía** — no asumir que ya sigue el protocolo de `docs/protocol.md`.

### `assets/` — Recursos multimedia
- `assets/videos/` — videos ya comprimidos/optimizados, lo único que se
  versiona en git.
- Los archivos originales sin comprimir (exports de cámara, renders sin
  procesar) van en una carpeta hermana `source/` (ej:
  `assets/videos/source/`) que está **excluida de git** por su peso —
  nunca se sube el original, solo la versión comprimida que usa la app.

### `tests/`
- Scripts de prueba manual (`manual_test_*.py`) para probar módulos de
  forma aislada, no una suite automatizada con pytest.

### Raíz del repo
- `main.py` — punto de entrada de la aplicación.
- `config.py` — configuración básica (nombre, puerto serial, baudrate).
- `requirements.txt` — dependencias Python.
- `CLAUDE.md` — instrucciones de contexto para el asistente de IA usado
  en este proyecto (arquitectura, reglas, estado de avance). Es un buen
  segundo lugar para leer si querés entender decisiones de diseño con
  más detalle histórico.

---

## 3. "Quiero hacer X, ¿a qué carpeta voy?"

| Quiero... | Voy a... |
|---|---|
| Agregar/cambiar un comando del protocolo serial | `src/communication/protocol.py` + `esp32_controller.py` + actualizar `docs/protocol.md` |
| Cambiar una regla de qué se puede hacer en qué estado | `src/controllers/system_state.py` |
| Cambiar algo visual de una pantalla existente | `src/ui/screens/<pantalla>.py` |
| Agregar una pantalla nueva | `src/ui/screens/` + registrarla en `src/ui/main_window.py` |
| Cambiar colores/tamaños/estilo general | `src/ui/style.py` |
| Agregar una trayectoria nueva para elegir en la app | poner el CSV en `data/trajectories/` (sin tocar código) |
| Ver cómo se cargan/parsean los CSV de trayectoria | `src/utils/trajectory_loader.py` |
| Cambiar cómo se calcula el movimiento sincronizado hacia la posición inicial (velocidades por eje, muestreo) | `src/utils/trajectory_generator.py` |
| Revisar el firmware de prueba (simulador de motores) | `firmware/gaitsim-esp32-test/main.cpp` |
| Entender el protocolo de mensajes ESP32 ↔ Raspberry Pi | `docs/protocol.md` |
| Ver el estado de avance / decisiones de diseño recientes | `CLAUDE.md` |

---

## 4. Cómo correr la app

```bash
cd gaitsim-control
source venv/bin/activate      # entorno virtual con las dependencias instaladas
python3 main.py
```

Necesita una sesión con display (pantalla táctil conectada o sesión VNC);
no funciona solo por SSH sin forwarding de pantalla.

---

## 5. Estado del firmware (importante para no confundirse)

Hay **dos firmwares** en este repo, no confundirlos:

1. **`firmware/gaitsim-esp32-test/main.cpp`** — firmware de prueba,
   simula los motores. Es el que hoy valida toda la app de punta a
   punta. Cumple el protocolo de `docs/protocol.md`.
2. **`firmware/Codigodemicompaneroparaelesp32.cpp`** — firmware
   definitivo, mueve los motores reales del simulador de marcha. Lo
   desarrolla un compañero por separado, **todavía no está integrado**
   ni sigue necesariamente el protocolo actual. No usar como referencia
   de comportamiento esperado hasta que se anuncie la integración.

---

## 6. Flujo de trabajo con git (para el equipo)

```bash
git status                          # ver qué cambió
git add archivo1.py archivo2.py     # agregar solo lo que corresponde al cambio (evitar `git add .` a ciegas)
git commit -m "mensaje claro de qué y por qué"
git push                            # subir a GitHub
```

- Evitar commitear archivos de configuración local (`.claude/settings.local.json`,
  `__pycache__/`, `.pyc`) — ya están en `.gitignore`.
- Los archivos multimedia originales sin comprimir van en carpetas
  `source/` (gitignored) — nunca se suben, solo la versión comprimida
  final (ver sección "Assets" más arriba).
