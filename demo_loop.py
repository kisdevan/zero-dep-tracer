"""
자가 교정 루프(Loop) + 트레이싱 데모. 외부 의존성 없음 — anthropic 은 API 키가 있을 때만 쓴다.

    python demo_loop.py                  # Mock LLM (사내망/오프라인). 3회 만에 성공하는 정상 시나리오
    python demo_loop.py --false-green    # 평가 노드가 느슨해서 빈 코드를 통과시키는 시나리오 (거짓 초록불)
    python demo_loop.py --real           # ANTHROPIC_API_KEY 가 있으면 실제 Claude 호출
    python demo_loop.py --reset          # traces.jsonl 을 비우고 시작
    python demo_loop.py --mermaid        # 끝나면 실제 실행 경로를 mermaid 로 출력 (GitHub/Notion 에 붙이면 그려진다)

트레이스는 ./traces.jsonl 에 쌓인다. 보는 방법은 두 가지:
    python -c "from tracer import print_tree; print_tree('traces.jsonl', last=2)"
    python dashboard.py        →  http://127.0.0.1:8790

파이프라인
    generate ──▶ verify(ast) ──▶ judge(LLM) ──▶ post_check(결정적 게이트)
        ▲            │ 실패           │ FAIL
        └────────────┴────────────────┘      스텝 예산(MAX_ATTEMPTS) 초과 시 status=차단
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, TypedDict

from tracer import Tracer, print_mermaid, print_tree

HERE = Path(__file__).parent
TRACE_PATH = HERE / "traces.jsonl"
tracer = Tracer(TRACE_PATH, service="software-factory")

GEN_MODEL = "claude-sonnet-5"                 # 생성은 상위 모델
JUDGE_MODEL = "claude-haiku-4-5-20251001"     # 판정은 경량 모델 — 토큰·대기시간 절감
MAX_ATTEMPTS = 3                              # 스텝 예산

REQUIREMENT = "거실 조명이 켜지면 TV 볼륨을 10 낮추는 핸들러 on_light_on(event) 를 작성하라. 볼륨 변경은 set_volume 함수를 호출해야 한다."
REQUIRED_CALL = "set_volume("                 # 값싸고 속이기 어려운 검증의 기준


class GraphState(TypedDict):
    code: str
    error: str
    attempts: int
    status: Literal["진행중", "완료", "결함", "차단"]


# ──────────────────────────────────────────────────────────────────────────────
# LLM 백엔드 — Mock(오프라인) 또는 실제 anthropic. 노드 코드는 어느 쪽인지 모른다.
# ──────────────────────────────────────────────────────────────────────────────
_BAD_SYNTAX = '''def on_light_on(event)
    if event["capability"] == "switch" and event["value"] == "on":
        set_volume("tv-livingroom", get_volume("tv-livingroom") - 10)
'''
_EMPTY_BODY = '''def on_light_on(event):
    # TODO: 볼륨 조절 구현
    pass
'''
_GOOD = '''def on_light_on(event):
    if event["capability"] == "switch" and event["value"] == "on":
        current = get_volume("tv-livingroom")
        set_volume("tv-livingroom", max(current - 10, 0))
'''


class MockLLM:
    """anthropic 응답 모양(content[0].text, usage, model)을 흉내낸다. 사내망 Case B 용."""

    LATENCY = {"generate": (0.18, 0.15, 0.20), "judge": (0.07,)}   # 워터폴이 보이게 하는 데모용 지연

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.calls = {"generate": 0, "judge": 0}

    def create(self, role: str, prompt: str, model: str) -> Any:
        n = self.calls[role]
        self.calls[role] += 1
        time.sleep(self.LATENCY[role][n % len(self.LATENCY[role])])

        if role == "generate":
            if self.scenario == "false_green":
                text = _EMPTY_BODY
            else:
                text = (_BAD_SYNTAX, _EMPTY_BODY, _GOOD)[min(n, 2)]
            usage = SimpleNamespace(input_tokens=420 + 60 * n, output_tokens=len(text) // 3)
        else:  # judge
            if self.scenario == "false_green":
                text = json.dumps({"verdict": "PASS", "reason": "코드가 요구사항을 충실히 반영합니다."}, ensure_ascii=False)
            elif REQUIRED_CALL in prompt:
                text = json.dumps({"verdict": "PASS", "reason": "조명 on 이벤트에서 set_volume 을 호출합니다."}, ensure_ascii=False)
            else:
                text = json.dumps({"verdict": "FAIL", "reason": "요구 동작(set_volume 호출)이 구현되지 않았습니다."}, ensure_ascii=False)
            usage = SimpleNamespace(input_tokens=260, output_tokens=40)

        return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage, model=model)


class RealLLM:
    def __init__(self) -> None:
        import anthropic  # 여기서만 import — 키가 없으면 이 클래스는 만들어지지 않는다
        self.client = anthropic.Anthropic()

    def create(self, role: str, prompt: str, model: str) -> Any:
        return self.client.messages.create(model=model, max_tokens=1024, messages=[{"role": "user", "content": prompt}])


LLM: Any = None


def configure(scenario: str = "normal", real: bool = False) -> None:
    global LLM
    LLM = RealLLM() if real else MockLLM(scenario)


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# 노드 — 함수 하나 = 스팬 하나. LLM 호출은 자식 스팬으로 분리해 비용을 따로 본다.
# ──────────────────────────────────────────────────────────────────────────────
@tracer.traced("node.generate")
def generate_code_node(state: GraphState) -> dict:
    sp = Tracer.current()
    if state["error"]:
        # 자가 교정의 핵심: 직전 에러를 프롬프트에 되돌려 넣는다
        prompt = f"다음 파이썬 코드의 문제를 고쳐서 코드만 출력하라.\n\n요구사항: {REQUIREMENT}\n\n코드:\n{state['code']}\n\n문제:\n{state['error']}"
    else:
        prompt = f"다음 요구사항을 만족하는 파이썬 코드만 출력하라. 설명 금지.\n\n요구사항: {REQUIREMENT}"

    with tracer.span("llm.generate") as llm:
        resp = LLM.create("generate", prompt, GEN_MODEL)
        llm.record_llm(resp, GEN_MODEL)

    code = _strip_fences(resp.content[0].text)
    sp.set(attempt=state["attempts"] + 1, repaired=bool(state["error"]), code_preview=code)
    return {"code": code, "attempts": state["attempts"] + 1}


@tracer.traced("node.verify_syntax")
def verify_syntax_node(state: GraphState) -> dict:
    sp = Tracer.current()
    try:
        ast.parse(state["code"])                         # Harness — 문법만 검증된다
        sp.set(verdict="PASS")
        return {"error": ""}
    except SyntaxError as e:
        # 검증이 '실패를 찾아낸 것'은 검증의 성공이다. 스팬 status 는 OK, verdict 만 FAIL.
        msg = f"{type(e).__name__}: {e.msg} (line {e.lineno})"
        sp.set(verdict="FAIL", reason=msg)
        return {"error": msg}


@tracer.traced("node.judge")
def judge_node(state: GraphState) -> dict:
    sp = Tracer.current()
    prompt = (
        f"요구사항: {REQUIREMENT}\n\n코드:\n{state['code']}\n\n"
        '이 코드가 요구사항을 충족하는지 판정하라. JSON 한 줄로만 답하라: {"verdict": "PASS" 또는 "FAIL", "reason": "근거"}'
    )
    with tracer.span("llm.judge") as llm:
        resp = LLM.create("judge", prompt, JUDGE_MODEL)
        llm.record_llm(resp, JUDGE_MODEL)

    try:
        data = json.loads(re.search(r"\{.*\}", resp.content[0].text, re.S).group(0))
        verdict, reason = data.get("verdict", "FAIL"), data.get("reason", "")
    except Exception:
        verdict, reason = "FAIL", "판정 응답을 JSON 으로 해석할 수 없음"
    sp.set(verdict=verdict, reason=reason)

    if verdict == "PASS":
        return {"error": "", "status": "완료"}
    return {"error": f"평가 불합격: {reason}", "status": "진행중"}


@tracer.traced("gate.post_check")
def post_check_node(state: GraphState) -> dict:
    """루프 바깥의 결정적 게이트. 값싸고, 자주 돌고, 속이기 어렵다 — LLM 판정을 믿어도 되는지 여기서 확인한다."""
    sp = Tracer.current()
    present = REQUIRED_CALL in state["code"]
    sp.set(required_call=REQUIRED_CALL, present=present, loop_status=state["status"])
    if state["status"] == "완료" and not present:
        sp.set(alert=f"judge=PASS 인데 {REQUIRED_CALL} 호출이 없음 — 거짓 초록불")
        return {"status": "결함"}
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Loop — 제어 엔진. demo_langgraph.py 에서 같은 노드가 LangGraph 로 치환된다
# ──────────────────────────────────────────────────────────────────────────────
def run(scenario: str = "normal", real: bool = False) -> GraphState:
    configure(scenario, real)
    state: GraphState = {"code": "", "error": "", "attempts": 0, "status": "진행중"}

    with tracer.trace("software_factory.run", requirement=REQUIREMENT, scenario=scenario,
                      llm="real" if real else "mock", max_attempts=MAX_ATTEMPTS) as root:
        while state["status"] == "진행중":
            with tracer.span("attempt", attempt=state["attempts"] + 1):
                state.update(generate_code_node(state))
                state.update(verify_syntax_node(state))
                if not state["error"]:
                    state.update(judge_node(state))
                if state["status"] != "완료" and state["attempts"] >= MAX_ATTEMPTS:
                    state["status"] = "차단"          # 종료 조건이 아니라 종료 '상태'를 남긴다

        state.update(post_check_node(state))
        root.set(final_status=state["status"], attempts=state["attempts"])

    return state


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--reset" in args and TRACE_PATH.exists():
        TRACE_PATH.unlink()
    real = "--real" in args
    if real and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--real 에는 ANTHROPIC_API_KEY 가 필요합니다. 키 없이는 Mock 으로 실행하세요.")

    scenario = "false_green" if "--false-green" in args else "normal"
    final = run(scenario, real)

    print(f"\nscenario={scenario}  llm={'real' if real else 'mock'}")
    print(f"final status = {final['status']}   attempts = {final['attempts']}")
    print("code:\n" + "\n".join("    " + l for l in final["code"].splitlines()))
    print_tree(TRACE_PATH, last=1)
    if "--mermaid" in args:
        print("\n실제 실행 경로 (mermaid):")
        print_mermaid(TRACE_PATH, last=1)
