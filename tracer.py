"""
zero-dependency tracer — 파이썬 표준 라이브러리만 사용한다.

왜 만드는가
  - 사내망에서는 Langfuse / LangSmith 같은 외부 관측 서비스에 닿지 못한다.
  - Docker 없이, pip 없이, 파일 하나로 "AI가 실제로 어떻게 일했는가"를 남긴다.
  - 스팬(span) 필드명을 OpenTelemetry 와 맞춰 두었다. 나중에 Langfuse / OTel Collector 가
    열리면 이 JSONL 을 그대로 내보내면 된다. 즉 이 파일은 관측의 "원리"이고, Langfuse 는 그 "제품"이다.

수강생에게 전달할 개념은 셋뿐이다
  trace  = 한 번의 실행 전체 (요청 하나가 들어와서 끝날 때까지). trace_id 하나.
  span   = 그 안의 작업 단위 하나 (노드 호출, LLM 호출, 검증 ...). 부모-자식으로 중첩된다.
  export = 스팬이 끝날 때마다 JSONL 파일에 한 줄 append. 프로세스가 죽어도 그때까지의 기록은 남는다.

사용법
    tracer = Tracer("traces.jsonl", service="software-factory")

    @tracer.traced("node.generate")          # 함수 하나 = 스팬 하나
    def generate(state): ...

    with tracer.trace("run", requirement=req) as root:   # 루트 스팬 = 새 trace
        with tracer.span("attempt", n=1):                 # 자식 스팬
            generate(state)
        root.set(final_status=state["status"])

    print_tree("traces.jsonl")               # 브라우저 없이 터미널에서 트리로 본다
"""
from __future__ import annotations

import contextvars
import functools
import json
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# 현재 열려 있는 스팬. contextvars 라서 스레드/asyncio 태스크별로 안전하게 분리된다.
_CURRENT: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar("current_span", default=None)


def _now_ns() -> int:
    return time.time_ns()


def _short(value: Any, limit: int = 400) -> Any:
    """속성에 큰 값을 넣을 때 잘라서 기록한다. 원문은 코드/상태에 있으니 트레이스는 요약만."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... (+{len(value) - limit} chars)"
    return value


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    start_time_unix_nano: int
    end_time_unix_nano: Optional[int] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "UNSET"            # OK | ERROR | UNSET  — OTel StatusCode 와 동일
    status_message: str = ""

    def set(self, **attrs: Any) -> "Span":
        """스팬에 속성을 붙인다. 뭘 붙일지가 관측 설계의 전부다."""
        self.attributes.update({k: _short(v) for k, v in attrs.items()})
        return self

    def event(self, name: str, **attrs: Any) -> None:
        """시점 기록. 스팬 안에서 언제 무엇이 있었는가."""
        self.events.append({"name": name, "time_unix_nano": _now_ns(), "attributes": attrs})

    def record_llm(self, response: Any, model: str = "") -> None:
        """anthropic Messages 응답(또는 같은 모양의 Mock)에서 토큰 사용량을 뽑아 기록한다."""
        usage = getattr(response, "usage", None)
        self.set(**{
            "llm.model": model or getattr(response, "model", ""),
            "llm.input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "llm.output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        })

    @property
    def duration_ms(self) -> float:
        end = self.end_time_unix_nano or _now_ns()
        return (end - self.start_time_unix_nano) / 1e6


class Tracer:
    def __init__(self, path: str | Path = "traces.jsonl", service: str = "agent", echo: bool = False):
        self.path = Path(path)
        self.service = service
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def current() -> Optional[Span]:
        """지금 열려 있는 스팬. 노드 안에서 속성을 덧붙일 때 쓴다."""
        return _CURRENT.get()

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        parent = _CURRENT.get()
        sp = Span(
            name=name,
            trace_id=parent.trace_id if parent else uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            start_time_unix_nano=_now_ns(),
        )
        sp.set(**attrs)
        token = _CURRENT.set(sp)
        try:
            yield sp
            if sp.status == "UNSET":
                sp.status = "OK"
        except Exception as e:
            sp.status = "ERROR"
            sp.status_message = f"{type(e).__name__}: {e}"
            sp.event("exception", type=type(e).__name__, message=str(e), stacktrace=traceback.format_exc())
            raise
        finally:
            sp.end_time_unix_nano = _now_ns()
            _CURRENT.reset(token)
            self._export(sp)

    @contextmanager
    def trace(self, name: str, **attrs: Any) -> Iterator[Span]:
        """루트 스팬. 부모를 끊어 새 trace_id 를 만든다."""
        token = _CURRENT.set(None)
        try:
            with self.span(name, **attrs) as sp:
                yield sp
        finally:
            _CURRENT.reset(token)

    def traced(self, name: Optional[str] = None, capture_result: bool = False) -> Callable:
        """함수 하나를 스팬 하나로 감싸는 데코레이터. Pure Python 노드와 LangGraph 노드 모두 그대로 쓴다."""
        def deco(fn: Callable) -> Callable:
            span_name = name or fn.__name__

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(span_name) as sp:
                    result = fn(*args, **kwargs)
                    if capture_result:
                        sp.set(result=json.dumps(result, ensure_ascii=False, default=str))
                    return result
            return wrapper
        return deco

    def _export(self, sp: Span) -> None:
        rec = asdict(sp)
        rec["service"] = self.service
        rec["duration_ms"] = round(sp.duration_ms, 3)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if self.echo:
            print(f"[{sp.status:5s} {sp.duration_ms:8.1f}ms] {sp.name}")


# ──────────────────────────────────────────────────────────────────────────────
# 읽기 — 대시보드와 터미널 트리가 공유한다
# ──────────────────────────────────────────────────────────────────────────────
def load_traces(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """JSONL → {trace_id: [span, ...]}. 파일이 없으면 빈 dict."""
    p = Path(path)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not p.exists():
        return groups
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                groups[rec["trace_id"]].append(rec)
    return groups


def print_tree(path: str | Path, last: int = 1) -> None:
    """브라우저 없이 터미널에서 마지막 N개 트레이스를 트리로 본다. 사내망 최소 보장선."""
    groups = load_traces(path)
    if not groups:
        print("(no traces)")
        return
    for trace_id in list(groups)[-last:]:
        spans = groups[trace_id]
        by_parent: dict[Optional[str], list[dict[str, Any]]] = defaultdict(list)
        for s in spans:
            by_parent[s["parent_span_id"]].append(s)
        for lst in by_parent.values():
            lst.sort(key=lambda s: s["start_time_unix_nano"])

        total_tokens = sum(
            (s["attributes"].get("llm.input_tokens", 0) or 0) + (s["attributes"].get("llm.output_tokens", 0) or 0)
            for s in spans
        )
        print(f"\ntrace {trace_id[:8]}  spans={len(spans)}  tokens={total_tokens}")

        def walk(parent_id: Optional[str], depth: int) -> None:
            for s in by_parent.get(parent_id, []):
                mark = {"OK": "OK ", "ERROR": "ERR"}.get(s["status"], " - ")
                a = s["attributes"]
                bits = []
                tok = (a.get("llm.input_tokens", 0) or 0) + (a.get("llm.output_tokens", 0) or 0)
                if tok:
                    bits.append(f"tokens={tok}")
                if "attempt" in a:
                    bits.append(f"attempt={a['attempt']}")
                if "verdict" in a:
                    bits.append(f"verdict={a['verdict']}")
                if a.get("alert"):
                    bits.append(f"!! {a['alert']}")
                if s["status_message"]:
                    bits.append(f"<- {s['status_message']}")
                tail = ("  " + "  ".join(bits)) if bits else ""
                print(f"  {'    ' * depth}[{mark}] {s['name']:<22s} {s['duration_ms']:8.1f} ms{tail}")
                walk(s["span_id"], depth + 1)

        walk(None, 0)
