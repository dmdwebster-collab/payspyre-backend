"""
SQLAlchemy event listeners for query logging and performance monitoring.

Logs all database queries with execution time and row counts.
Identifies slow queries (threshold: 500ms).
"""
import time
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Query

from app.core.logging import get_logger

logger = get_logger("db.query")

SLOW_QUERY_THRESHOLD_MS = 500


def setup_db_logging(engine: Engine) -> None:
    """Attach event listeners to the SQLAlchemy engine."""

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        """Store query start time."""
        conn.info.setdefault("query_start_time", []).append(time.time())

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        """Log query execution time."""
        if not conn.info.get("query_start_time"):
            return

        start_time = conn.info["query_start_time"].pop()
        duration_ms = (time.time() - start_time) * 1000

        # Clean up SQL for logging
        clean_statement = statement.strip().replace("\n", " ")[:200]

        log_data = {
            "statement": clean_statement,
            "duration_ms": round(duration_ms, 2),
        }

        # Log slow queries as warnings
        if duration_ms > SLOW_QUERY_THRESHOLD_MS:
            logger.warning("slow_query", **log_data, threshold_ms=SLOW_QUERY_THRESHOLD_MS)
        else:
            logger.debug("query", **log_data)

    @event.listens_for(engine, "handle_error")
    def handle_error(context):
        """Log database errors.

        SQLAlchemy's handle_error event passes a single positional
        ExceptionContext object — not kwargs. See:
        https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.ConnectionEvents.handle_error
        """
        exception = context.original_exception
        statement = context.statement or ""
        clean_statement = statement.strip()[:200] if statement else ""
        logger.error(
            "db_error",
            error=str(exception) if exception else "Unknown error",
            error_type=type(exception).__name__ if exception else "Unknown",
            statement=clean_statement,
        )

    logger.info("db_logging_enabled", slow_threshold_ms=SLOW_QUERY_THRESHOLD_MS)
