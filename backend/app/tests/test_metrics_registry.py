import pytest

from app.core.metrics import MetricsRegistry


def test_counter_accumulates_and_renders() -> None:
    registry = MetricsRegistry()
    registry.describe("demo_requests_total", "示例计数器")

    registry.inc("demo_requests_total")
    registry.inc("demo_requests_total", 2)

    assert registry.get("demo_requests_total") == 3.0
    text = registry.render()
    assert "# TYPE demo_requests_total counter" in text
    assert "demo_requests_total 3" in text


def test_labels_create_separate_series_and_are_sorted() -> None:
    registry = MetricsRegistry()
    registry.inc("cache_requests_total", result="hit", backend="redis")
    registry.inc("cache_requests_total", result="hit", backend="redis")
    registry.inc("cache_requests_total", result="miss", backend="memory")

    assert registry.get("cache_requests_total", result="hit", backend="redis") == 2.0
    text = registry.render()
    assert 'cache_requests_total{backend="memory",result="miss"} 1' in text
    assert 'cache_requests_total{backend="redis",result="hit"} 2' in text


def test_label_values_are_escaped() -> None:
    registry = MetricsRegistry()
    registry.inc("tool_calls_total", tool='a"b\\c\nd')

    text = registry.render()
    assert 'tool="a\\"b\\\\c\\nd"' in text


def test_gauge_keeps_last_value() -> None:
    registry = MetricsRegistry()
    registry.set("cache_up", 1)
    registry.set("cache_up", 0)

    assert registry.get("cache_up") == 0.0
    assert "# TYPE cache_up gauge" in registry.render()


def test_reset_clears_series_and_meta() -> None:
    registry = MetricsRegistry()
    registry.inc("demo_total")
    registry.reset()

    assert registry.get("demo_total") == 0.0
    assert registry.render() == ""


def test_unknown_metric_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        MetricsRegistry().describe("bad", "x", "histogram")
