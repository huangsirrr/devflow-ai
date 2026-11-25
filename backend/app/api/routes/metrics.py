from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.metrics import metrics

router = APIRouter()


@router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """暴露进程内指标，供 Prometheus / Grafana 抓取。"""
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
