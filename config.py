"""Configuración leída desde variables de entorno / archivo .env (nunca desde el código)."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Carga el archivo .env que está junto a este archivo (si existe).
# Las variables ya definidas en el sistema tienen prioridad sobre el .env.
load_dotenv(Path(__file__).resolve().parent / ".env")

# Si un poste de una ruta nueva está a esta distancia (metros) o menos de un poste
# que ya existe en la base, se considera el mismo poste y se reutiliza.
DEDUP_TOLERANCIA_M = float(os.getenv("POSTES_DEDUP_TOLERANCIA_M", "1.0"))


def _requerida(nombre):
    if nombre not in os.environ:
        raise RuntimeError(
            f"Falta la variable {nombre}. Copia .env.example como .env y llena tus datos de MySQL."
        )
    return os.environ[nombre]


def obtener_config():
    """Datos de conexión a MySQL. Falla con un mensaje claro si falta usuario o contraseña."""
    return {
        "host": os.getenv("POSTES_DB_HOST", "localhost"),
        "port": int(os.getenv("POSTES_DB_PORT", "3306")),
        "user": _requerida("POSTES_DB_USER"),
        "password": _requerida("POSTES_DB_PASSWORD"),  # puede ser vacía, pero debe estar definida
        "database": os.getenv("POSTES_DB_NAME", "postes_fibra"),
        "charset": "utf8mb4",
    }