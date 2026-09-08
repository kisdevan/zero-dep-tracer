"""
같은 루프를 LangGraph StateGraph 로. 노드 함수는 demo_loop 의 것을 그대로 재사용한다.
트레이싱 데코레이터도 그대로 붙어 있으므로 **프레임워크를 바꿔도 관측은 바뀌지 않는다.**

    python demo_langgraph.py                 # Mock LLM (오프라인)
    python demo_langgraph.py --false-green   # 거짓 초록불 시나리오
    python demo_langgraph.py --real          # ANTHROPIC_API_KEY 필요

Pure Python(demo_loop.py) ↔ LangGraph 대응
    while state["status"] == "진행중"        ↔  graph.compile().invoke(state)
    if not state["error"]: judge(...)        ↔  add_conditional_edges("verify", route_after_verify)
    attempts >= MAX_ATTEMPTS → 차단          ↔  budget 노드 + config={"recursion_limit": N} (이중 안전장치)
    with tracer.span("attempt")              ↔  (없음) 노드 속성 attempt 로 대신 남긴다
"""
from __future__ import annotations

import os
import sys

from langgraph.graph import END, START, StateGraph

import demo_loop as dl
from demo_loop import GraphState, generate_code_node, judge_node, post_check_node, verify_syntax_node, tracer
from tracer import print_mermaid, print_tree


@tracer.traced("node.budget")
def budget_node(state: GraphState) -> dict:
    """스텝 예산 초과 — 조건부 엣지는 상태를 못 바꾸므로 노드가 종료 '상태'를 적는다."""
    return {"status": "차단"}


def route_after_verify(state: GraphState) -> str:
    if state["error"]:
        return "budget" if state["attempts"] >= dl.MAX_ATTEMPTS else "generate"
    return "judge"


def route_after_judge(state: GraphState) -> str:
    if state["status"] == "완료":
        return "post_check"
    return "budget" if state["attempts"] >= dl.MAX_ATTEMPTS else "generate"


def build_graph():
    g = StateGraph(GraphState)
    g.add_node("generate", generate_code_node)
    g.add_node("verify", verify_syntax_node)
    g.add_node("judge", judge_node)
    g.add_node("budget", budget_node)
    g.add_node("post_check", post_check_node)

    g.add_edge(START, "generate")
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", route_after_verify, {"generate": "generate", "judge": "judge", "budget": "budget"})
    g.add_conditional_edges("judge", route_after_judge, {"generate": "generate", "post_check": "post_check", "budget": "budget"})
    g.add_edge("budget", "post_check")
    g.add_edge("post_check", END)
    return g.compile()


def run(scenario: str = "normal", real: bool = False) -> GraphState:
    dl.configure(scenario, real)
    graph = build_graph()
    init: GraphState = {"code": "", "error": "", "attempts": 0, "status": "진행중"}

    with tracer.trace("software_factory.langgraph", requirement=dl.REQUIREMENT, scenario=scenario,
                      llm="real" if real else "mock", max_attempts=dl.MAX_ATTEMPTS) as root:
        # recursion_limit 은 루프 폭주 시 프레임워크 차원의 마지막 안전장치. budget 노드가 먼저 잡는 게 정상이다.
        final = graph.invoke(init, config={"recursion_limit": 40})
        root.set(final_status=final["status"], attempts=final["attempts"])
    return final


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--reset" in args and dl.TRACE_PATH.exists():
        dl.TRACE_PATH.unlink()
    real = "--real" in args
    if real and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--real 에는 ANTHROPIC_API_KEY 가 필요합니다.")

    scenario = "false_green" if "--false-green" in args else "normal"
    final = run(scenario, real)

    print(f"\n[langgraph] scenario={scenario}  llm={'real' if real else 'mock'}")
    print(f"final status = {final['status']}   attempts = {final['attempts']}")
    print_tree(dl.TRACE_PATH, last=1)
    if "--mermaid" in args:
        # 설계 그래프(LangGraph 가 그린 것) vs 실제 실행 경로(트레이스에서 복원한 것) — 둘을 나란히 붙여 비교한다
        print("\n설계 그래프 (graph.get_graph().draw_mermaid()):")
        print("```mermaid\n" + build_graph().get_graph().draw_mermaid().strip() + "\n```")
        print("\n실제 실행 경로 (트레이스에서 복원):")
        print_mermaid(dl.TRACE_PATH, last=1)
