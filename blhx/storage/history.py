from __future__ import annotations

from typing import Any

from .db import get_connection
from ..core.utils import utc_now_text


def record_operation(
    connection,
    operation_type: str,
    message: str,
    ship_id: int | None = None,
    ship_name: str = "",
    ship_type: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO operation_logs
            (operation_type, ship_id, ship_name, ship_type, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            operation_type,
            ship_id,
            ship_name,
            ship_type,
            message,
            utc_now_text(),
        ),
    )


def list_operations(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, operation_type, ship_id, ship_name, ship_type,
                   message, created_at
            FROM operation_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]

