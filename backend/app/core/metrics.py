"""进程内指标注册表与 Prometheus 文本输出。

只做计数器和瞬时值两类指标：够回答“缓存命中率多少、分析失败多少次”，
又不用把 prometheus_client 变成运行依赖。
"""

from __future__ import annotations

import threading

_ESCAPE = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}
_ALLOWED_TYPES = {"counter", "gauge"}


def _escape(value: str) -> str:
    return "".join(_ESCAPE.get(char, char) for char in str(value))


def _format_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class MetricsRegistry:
    """线程安全的指标注册表，按 (名称, 标签集合) 聚合。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._meta: dict[str, tuple[str, str]] = {}

    def describe(self, name: str, help_text: str, metric_type: str = "counter") -> None:
        if metric_type not in _ALLOWED_TYPES:
            raise ValueError(f"unsupported metric type: {metric_type}")
        with self._lock:
            self._meta.setdefault(name, (metric_type, help_text))

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(value)
            self._meta.setdefault(name, ("counter", name))

    def set(self, name: str, value: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._values[key] = float(value)
            self._meta.setdefault(name, ("gauge", name))

    def get(self, name: str, **labels: str) -> float:
        key = self._key(name, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
            meta = dict(self._meta)
        lines: list[str] = []
        for name in sorted({key[0] for key in values}):
            metric_type, help_text = meta.get(name, ("counter", name))
            lines.append(f"# HELP {name} {_escape(help_text)}")
            lines.append(f"# TYPE {name} {metric_type}")
            for (series_name, labels), value in sorted(values.items()):
                if series_name != name:
                    continue
                suffix = ""
                if labels:
                    rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in labels)
                    suffix = "{" + rendered + "}"
                lines.append(f"{name}{suffix} {_format_value(value)}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._meta.clear()

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((str(key), str(value)) for key, value in labels.items()))


metrics = MetricsRegistry()
