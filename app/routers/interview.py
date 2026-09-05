"""Interview 路由:开始 / 作答(SSE 流式) / 会话历史 / 综合点评 / 删除。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import db
from ..modules import interview, profile
from .deps import resolve_profile_id, templates

log = logging.getLogger(__name__)
router = APIRouter()


# ---------- 会话开始 ----------

@router.post("/api/interview/start")
def api_interview_start(
    role_tag: str = Form("AI 工程师"),
    company: str = Form(""),
    profile_id: str = Form(""),
):
    """JSON API(供前端 fetch 用):返回 sid + questions。"""
    pid = resolve_profile_id(profile_id)
    skills = profile.get_parsed_skills(pid)
    jd_keywords = skills.get("jd_focus", []) if isinstance(skills, dict) else []
    sid = interview.start_session(role_tag, company, profile_id=pid)
    questions = interview.build_questions(sid, role_tag, jd_keywords, profile_id=pid)
    interview.persist_questions(sid, questions)
    return JSONResponse({"session_id": sid, "questions": questions})


@router.post("/interview/start")
def page_interview_start(
    request: Request,
    role_tag: str = Form("AI 工程师"),
    company: str = Form(""),
    profile_id: str = Form(""),
):
    """浏览器 form submit 期望 HTML 返回。"""
    pid = resolve_profile_id(profile_id)
    skills = profile.get_parsed_skills(pid)
    jd_keywords = skills.get("jd_focus", []) if isinstance(skills, dict) else []
    sid = interview.start_session(role_tag, company, profile_id=pid)
    questions = interview.build_questions(sid, role_tag, jd_keywords, profile_id=pid)
    interview.persist_questions(sid, questions)
    return templates.TemplateResponse(
        request, "interview.html",
        {
            "request": request, "active": "interview",
            "sessions": interview.list_sessions(limit=10),
            "profiles": profile.list_profiles(limit=30),
            "current_session": {"id": sid, "role_tag": role_tag, "company": company},
            "questions": questions,
        },
    )


# ---------- 作答(SSE 流式) ----------

@router.post("/api/interview/answer")
def api_interview_answer(
    session_id: int = Form(...),
    qa_index: int = Form(...),
    answer: str = Form(...),
):
    """SSE 流式出三轴点评。"""
    # W4 修复 #9:从 session_question 表读题目(持久化),不再依赖 _session_store
    questions = interview.load_questions(session_id)
    if not questions:
        return JSONResponse({"error": "session 没有题目,请先发起新会话"}, status_code=404)
    # W4 修复 #18:qa_index bounds check
    if qa_index < 0 or qa_index >= len(questions):
        return JSONResponse({"error": f"qa_index 越界: {qa_index}, 范围 0..{len(questions)-1}"}, status_code=400)
    q = questions[qa_index]

    def sse_gen():
        try:
            for ev in interview.submit_answer_stream(
                session_id=session_id,
                question=q["question"],
                question_source=q.get("question_source", "WEAKNESS"),
                answer=answer,
                reference_answer=q.get("reference_answer", ""),
                scoring_points=q.get("scoring_points", []),
            ):
                if ev["type"] == "chunk":
                    yield f"data: {json.dumps({'chunk': ev['text']}, ensure_ascii=False)}\n\n"
                elif ev["type"] == "done":
                    # 落库后立即发 done,携带归一化 score 供前端渲染格式化点评
                    yield f"data: {json.dumps({'done': True, 'qa_id': ev['id'], 'score': ev.get('score')}, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 关键修复:LLM 报错(模型名 404 / 服务不可达)时,生成器异常会让 SSE 静默断流,
            # 前端永远停在"生成中"。显式推 error 事件,前端可展示失败原因。
            log.exception("answer stream failed")
            yield f"data: {json.dumps({'error': f'点评生成失败: {e}'}, ensure_ascii=False)}\n\n"
            return

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


# ---------- 会话查询 / 删除 ----------

@router.get("/api/interview/{session_id}/qa")
def api_get_qa(session_id: int):
    """历史 API(供 MCP 或外部用);UI 现在用 /interview/{session_id} 页面。"""
    rows = interview.get_qa_logs(session_id)
    for r in rows:
        try:
            r["score"] = json.loads(r.pop("score_json") or "{}")
        except Exception:
            r["score"] = {}
    return JSONResponse(rows)


@router.post("/api/interview/{session_id}/delete")
def api_interview_delete(session_id: int):
    interview.delete_session(session_id)
    return JSONResponse({"ok": True, "id": session_id})


@router.post("/interview/{session_id}/delete")
def page_interview_delete(session_id: int):
    """浏览器 form submit 版删除会话。"""
    interview.delete_session(session_id)
    return RedirectResponse(url="/interview", status_code=303)


# ---------- 综合点评 ----------

@router.post("/api/interview/{session_id}/summary")
def api_interview_summary(session_id: int):
    """整场综合点评:聚合全部题目评分,LLM 输出复盘 Markdown 并持久化。"""
    try:
        result = interview.summarize_session(session_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.exception("session summary failed")
        return JSONResponse({"error": f"综合点评生成失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True, **result})
