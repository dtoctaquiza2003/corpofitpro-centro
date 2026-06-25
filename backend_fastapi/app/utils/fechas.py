from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Date, and_, cast, func

ECUADOR_TZ = ZoneInfo("America/Guayaquil")


def now_utc() -> datetime:
    """Hora real para guardar en columnas TIMESTAMPTZ."""
    return datetime.now(timezone.utc)


def now_ecuador() -> datetime:
    """Hora actual vista en Ecuador."""
    return datetime.now(ECUADOR_TZ)


def today_ecuador() -> date:
    return now_ecuador().date()


def to_ecuador(value: datetime | None) -> datetime | None:
    """Convierte fechas de BD a hora Ecuador para respuestas al frontend."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(ECUADOR_TZ)


def to_ecuador_naive(value: datetime | None) -> datetime | None:
    """Convierte a Ecuador y remueve tzinfo. Útil para Excel/openpyxl."""
    local_value = to_ecuador(value)
    if local_value is None:
        return None
    return local_value.replace(tzinfo=None)


def ecuador_day_range(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """Rango [inicio, fin) usando calendario de Ecuador."""
    inicio = datetime.combine(desde, time.min, tzinfo=ECUADOR_TZ)
    fin = datetime.combine(hasta, time.max, tzinfo=ECUADOR_TZ)
    # Convertimos a límite exclusivo del siguiente microsegundo/día para filtros SQL.
    fin_exclusivo = datetime.combine(
        date.fromordinal(hasta.toordinal() + 1),
        time.min,
        tzinfo=ECUADOR_TZ,
    )
    return inicio, fin_exclusivo


def fecha_ecuador_sql(column):
    """
    Expresión SQL para convertir un TIMESTAMPTZ a fecha local Ecuador.

    Ejemplo generado:
    (fechapago AT TIME ZONE 'America/Guayaquil')::date
    """
    return cast(func.timezone("America/Guayaquil", column), Date)


def filtro_datetime_ecuador(column, desde: date, hasta: date):
    """Filtro de columna TIMESTAMPTZ usando fecha calendario Ecuador."""
    inicio, fin = ecuador_day_range(desde, hasta)
    return and_(column >= inicio, column < fin)
