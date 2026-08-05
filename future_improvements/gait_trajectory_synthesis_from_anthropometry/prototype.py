"""
Fase 1 prototype: generar una trayectoria (t, x, y, angulo) del punto
de montaje de una protesis transtibial, a partir de una curva de
referencia ya existente en el repo, en vez de requerir un CSV
capturado por paciente.

Standalone: no lo importa ni lo llama nada de src/ o main.py. Solo lee
TrajectoryPoint (dataclass sin I/O) de protocol.py para que el
resultado tenga la misma forma que el resto de la app espera el dia
que esto se integre - ver future_improvements/gait_trajectory_synthesis_from_anthropometry/README.md
seccion 6 para el detalle de que SI y que NO esta validado todavia.

Dos transformaciones independientes y componibles (ver README.md
seccion 2.4 sobre por que son dos cosas distintas, no una):

1a. generate_scaled_trajectory() - escalamiento no-dimensional (Hof,
    1996) por tamano corporal del paciente (longitud de pierna,
    cadencia), con el periodo escalado por similaridad dinamica
    (numero de Froude, Alexander) cuando no se da cadencia/velocidad
    directamente.
2a. apply_residual_limb_offset() - desplazamiento de cuerpo rigido a
    lo largo del segmento tibial, para mover el punto de montaje segun
    el nivel de amputacion transtibial (alta/media/corta) del
    paciente - independiente del tamano corporal.

generate_patient_trajectory() compone ambas. Ver README.md seccion 2
para las fuentes de literatura.

Uso:
    python3 prototype.py
"""

import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from src.communication.protocol import TrajectoryPoint  # noqa: E402

REFERENCE_CSV = _REPO_ROOT / "data" / "trajectories" / "balanceo_v4.csv"

# PLACEHOLDER - no se conoce la longitud de pierna real del sujeto que
# genero balanceo_v4.csv (ver README.md, Pregunta abierta #3). Sin este
# dato el escalamiento parte de un supuesto no confirmado. 90cm es un
# valor tipico de longitud de pierna adulta (cadera a suelo), NO un
# dato medido de este proyecto.
REFERENCE_LEG_LENGTH_CM = 90.0

# PLACEHOLDER - no se conoce la distancia real (rodilla -> punto de
# montaje) del sujeto que genero balanceo_v4.csv (ver README.md,
# Pregunta abierta #2). 16cm es la cifra "optima"/media citada en
# literatura clinica general (ver README.md seccion 2.4) - NO un dato
# medido de este proyecto ni necesariamente el valor real usado al
# grabar este CSV.
REFERENCE_MOUNT_DISTANCE_CM = 16.0

# PLACEHOLDER ilustrativo, ver README.md seccion 2.4 y Pregunta
# abierta #4 - la literatura general no da umbrales consistentes;
# estos valores son solo para que el demo tenga algo concreto que
# mostrar, no una clasificacion clinica validada para este proyecto.
RESIDUAL_LIMB_MOUNT_DISTANCE_CM = {
    "corta": 10.0,
    "media": 16.0,
    "larga": 22.0,
}

GRAVITY_M_S2 = 9.81


@dataclass
class NormalizedGaitCurve:
    """
    Curva de referencia en forma adimensional: tiempo como fraccion del
    ciclo [0, 1], x/y como fraccion de la longitud de pierna de
    referencia, angulo sin cambios (ya adimensional, ver README.md
    seccion 2.1 - la normalizacion de Hof no reescala angulos por
    tamano corporal).
    """
    s: list[float]       # fraccion del ciclo, 0..1
    x_frac: list[float]  # x / leg_length_ref
    y_frac: list[float]  # y / leg_length_ref
    angle_deg: list[float]
    period_s: float
    leg_length_cm: float


def load_reference_csv(path: Path) -> list[TrajectoryPoint]:
    """
    Misma convencion posicional que trajectory_loader.py: primeras 4
    columnas = tiempo, pos_x, pos_y, angulo; separador ';'; columnas
    extra (balanceo_v4.csv trae ';;;' de sobra al final) se ignoran.
    """
    points = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader)  # header
        for row in reader:
            if not row or not row[0].strip():
                continue
            t, x, y, angle = row[0], row[1], row[2], row[3]
            points.append(TrajectoryPoint(t=float(t), x=float(x), y=float(y), angle=float(angle)))
    return points


def normalize(points: list[TrajectoryPoint], leg_length_cm: float) -> NormalizedGaitCurve:
    period = points[-1].t - points[0].t
    if period <= 0:
        raise ValueError("La curva de referencia necesita al menos 2 puntos con t creciente.")
    t0 = points[0].t
    return NormalizedGaitCurve(
        s=[(p.t - t0) / period for p in points],
        x_frac=[p.x / leg_length_cm for p in points],
        y_frac=[p.y / leg_length_cm for p in points],
        angle_deg=[p.angle for p in points],
        period_s=period,
        leg_length_cm=leg_length_cm,
    )


def estimate_period_s(target_leg_length_cm: float, reference: NormalizedGaitCurve,
                       cadence_steps_per_min: float | None = None) -> float:
    """
    Si se da cadencia explicita, se usa esa (60 / cadencia = duracion
    de un paso; esta curva de referencia es de UN paso de swing, no
    ciclo completo - ver README.md seccion 1 sobre que representa
    balanceo_v4.csv). Si no, se deriva el nuevo periodo del de
    referencia via similaridad dinamica (numero de Froude constante):
    v ~ sqrt(g * L)  =>  T ~ L / v ~ sqrt(L / g)  =>  T_new/T_ref =
    sqrt(L_new / L_ref). Ver README.md seccion 2.1 (Hof 1996,
    Alexander) - esto es un PLACEHOLDER razonable de literatura, no
    una relacion validada contra datos propios de este proyecto.
    """
    if cadence_steps_per_min is not None:
        return 60.0 / cadence_steps_per_min
    ratio = math.sqrt(target_leg_length_cm / reference.leg_length_cm)
    return reference.period_s * ratio


def generate_scaled_trajectory(
    target_leg_length_cm: float,
    reference: NormalizedGaitCurve,
    cadence_steps_per_min: float | None = None,
) -> list[TrajectoryPoint]:
    """
    Reconstruye una trayectoria a partir de la curva normalizada,
    escalada a un paciente con `target_leg_length_cm`. Devuelve
    List[TrajectoryPoint] - mismo tipo que trajectory_generator.py ya
    produce, para que el dia que esto se integre entre por el mismo
    camino TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN sin protocolo nuevo.

    NO valida contra CalibrationSpace (eso es responsabilidad del
    caller una vez integrado - ver README.md Fase 3, reusar
    trajectory_validator.py existente).
    """
    period = estimate_period_s(target_leg_length_cm, reference, cadence_steps_per_min)
    return [
        TrajectoryPoint(
            t=s * period,
            x=x_frac * target_leg_length_cm,
            y=y_frac * target_leg_length_cm,
            angle=angle,
        )
        for s, x_frac, y_frac, angle in zip(
            reference.s, reference.x_frac, reference.y_frac, reference.angle_deg
        )
    ]


def _segment_unit_vector(angle_deg: float) -> tuple[float, float]:
    """
    Vector unitario a lo largo del segmento tibial, desde la rodilla
    (proximal) hacia el punto de montaje (distal), en el mismo marco
    (x, y) que ya usa la plataforma.

    CONVENCION NO CONFIRMADA (ver README.md, Pregunta abierta #5):
    asume angulo medido desde +X, sentido antihorario (convencion
    matematica estandar) - angle=0 -> segmento apunta a +X, angle=90 ->
    apunta a +Y. Esto NO fue confirmado contra la definicion mecanica
    real de la plataforma (ver docs/protocol.md, eje angular; CLAUDE.md
    ANGLE_HORIZONTAL_OFFSET_DEG). Si esta mal, el offset por nivel de
    amputacion sale en la direccion equivocada aunque la magnitud sea
    correcta.
    """
    theta = math.radians(angle_deg)
    return math.cos(theta), math.sin(theta)


def apply_residual_limb_offset(
    points: list[TrajectoryPoint],
    reference_mount_distance_cm: float,
    target_mount_distance_cm: float,
) -> list[TrajectoryPoint]:
    """
    Desplaza una trayectoria de punto de montaje de una distancia de
    montaje (rodilla -> punto) a otra, a lo largo del MISMO segmento
    tibial rigido - ver README.md seccion 2.4. El angulo no cambia
    (todo punto sobre un segmento rigido comparte el mismo angulo en
    cada instante); solo (x, y) se desplazan por
    (target - reference) a lo largo de la direccion del segmento.

    Independiente de generate_scaled_trajectory(): esto es un offset en
    cm ABSOLUTOS (propiedad del nivel de amputacion, no del tamano
    corporal general del paciente) - no se normaliza por longitud de
    pierna. Aplicar despues del escalamiento por tamano corporal, no
    antes (ver README.md Fase 1, por que el orden importa).
    """
    delta = target_mount_distance_cm - reference_mount_distance_cm
    result = []
    for p in points:
        ux, uy = _segment_unit_vector(p.angle)
        result.append(TrajectoryPoint(
            t=p.t,
            x=p.x + delta * ux,
            y=p.y + delta * uy,
            angle=p.angle,
        ))
    return result


def generate_patient_trajectory(
    leg_length_cm: float,
    mount_distance_cm: float,
    reference: NormalizedGaitCurve,
    cadence_steps_per_min: float | None = None,
    reference_mount_distance_cm: float = REFERENCE_MOUNT_DISTANCE_CM,
) -> list[TrajectoryPoint]:
    """
    Punto de entrada unico: compone 1a (escalamiento por tamano
    corporal) + 1b (offset por nivel de amputacion). Ver README.md
    Fase 1 para el diseno completo y las preguntas abiertas que
    afectan cuanto confiar en el resultado.
    """
    scaled = generate_scaled_trajectory(leg_length_cm, reference, cadence_steps_per_min)
    return apply_residual_limb_offset(scaled, reference_mount_distance_cm, mount_distance_cm)


def _demo():
    reference_points = load_reference_csv(REFERENCE_CSV)
    reference = normalize(reference_points, REFERENCE_LEG_LENGTH_CM)
    print(f"Referencia: {REFERENCE_CSV.name}, {len(reference_points)} puntos, "
          f"periodo={reference.period_s:.3f}s, leg_length asumida={REFERENCE_LEG_LENGTH_CM}cm")

    # Sanity check: reconstruir con la MISMA longitud de pierna debe
    # reproducir la curva original (confirma que el escalamiento
    # ida-y-vuelta no introduce error de por si - NO confirma que el
    # metodo sea biomecanicamente correcto para un paciente nuevo,
    # ver README.md seccion 6).
    roundtrip = generate_scaled_trajectory(REFERENCE_LEG_LENGTH_CM, reference)
    max_err = max(
        abs(a.t - b.t) + abs(a.x - b.x) + abs(a.y - b.y) + abs(a.angle - b.angle)
        for a, b in zip(reference_points, roundtrip)
    )
    print(f"Sanity check 1a (roundtrip == referencia): error maximo = {max_err:.2e} "
          f"({'OK' if max_err < 1e-6 else 'FALLO'})")

    # Sanity check 1b: offset con la MISMA distancia de montaje debe
    # ser un no-op (delta=0).
    offset_noop = apply_residual_limb_offset(
        reference_points, REFERENCE_MOUNT_DISTANCE_CM, REFERENCE_MOUNT_DISTANCE_CM
    )
    max_err_1b = max(
        abs(a.x - b.x) + abs(a.y - b.y) + abs(a.angle - b.angle)
        for a, b in zip(reference_points, offset_noop)
    )
    print(f"Sanity check 1b (offset con misma distancia == no-op): error maximo = "
          f"{max_err_1b:.2e} ({'OK' if max_err_1b < 1e-9 else 'FALLO'})")

    print("\n--- Demo combinado (1a escalamiento + 1b offset por nivel de amputacion) ---")
    print("ADVERTENCIA: usa placeholders sin confirmar (ver README.md seccion 5, "
          "preguntas 2, 3, 4, 5) - valores ilustrativos, no clinicos.")
    for leg_length in (75.0, 90.0, 105.0):
        for nivel, mount_distance in RESIDUAL_LIMB_MOUNT_DISTANCE_CM.items():
            patient = generate_patient_trajectory(leg_length, mount_distance, reference)
            print(
                f"leg_length={leg_length:>5.1f}cm, amputacion={nivel:<6s} "
                f"(mount={mount_distance:>4.1f}cm) -> "
                f"periodo={patient[-1].t:.3f}s, "
                f"x=[{min(p.x for p in patient):.2f}, {max(p.x for p in patient):.2f}]cm, "
                f"y=[{min(p.y for p in patient):.2f}, {max(p.y for p in patient):.2f}]cm"
            )


if __name__ == "__main__":
    _demo()
