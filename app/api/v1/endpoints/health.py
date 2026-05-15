from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.health import get_database_health

router = APIRouter()


@router.get("/db")
async def database_health_check(db: Session = Depends(get_db)):
    """
    Get comprehensive database health status.

    Returns:
        - overall_status: 'healthy' or 'degraded'
        - timestamp: Unix timestamp of the check
        - duration_seconds: Time taken to run all checks
        - checks: Detailed results for each health check
            - connection: Basic connectivity
            - pool: Connection pool statistics
            - rls_enabled: Row Level Security status on critical tables
            - rls_isolation: RLS isolation test results
            - indexes: Top 20 indexes by usage
            - slow_queries: Currently running slow queries (>1s)
            - table_bloat: Largest tables by size

    Example response:
    {
        "overall_status": "healthy",
        "timestamp": 1715683200.0,
        "duration_seconds": 0.245,
        "checks": {
            "connection": true,
            "pool": {
                "status": "healthy",
                "pool_size": 9,
                "checked_in": 8,
                "checked_out": 1,
                "overflow": 0,
                "utilization_percent": 5.56
            },
            "rls_enabled": {
                "all_tables_protected": true,
                "tables": {...},
                "status": "secure"
            },
            "rls_isolation": {
                "status": "working",
                "test_result": "isolated",
                "details": "Vendor isolation active: 0 applications accessible"
            },
            "indexes": {
                "status": "analyzed",
                "top_indexes": [...],
                "total_indexed": 20
            },
            "slow_queries": {
                "status": "healthy",
                "threshold_ms": 1000,
                "count": 0,
                "queries": []
            },
            "table_bloat": {
                "status": "analyzed",
                "largest_tables": [...]
            }
        }
    }

    Notes:
    - This endpoint is rate-limited and may be bypassed for monitoring
    - RLS isolation test uses a fake vendor ID and should always return 0 applications
    - Slow query threshold defaults to 1000ms but can be adjusted
    - Table bloat check requires pgstattuple extension
    """
    try:
        health_status = get_database_health(db)
        return health_status
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Health check failed: {str(e)}"
        )