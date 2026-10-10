"""FastAPI application with routes only - business logic in services layer."""

import asyncio
import json
import markdown2
import re
from datetime import datetime

from fastapi import FastAPI, Request, Form, Cookie, Response, BackgroundTasks, Depends
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks
from starlette.middleware.sessions import SessionMiddleware

from app import data_sync
from app import duckdb as duckdb_manager
from app.config import settings
from app.deps import get_session_key
from app.lifespan import lifespan
from app.models import Chat, Message
from app.prompts import EXAMPLE_PROMPTS
from app.services.llm import generate_response
from app.services.sse import chat_update_subscribers, notify_chat_update


app = FastAPI(title="Pharma Pilot", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "static")), name="static")
templates = Jinja2Blocks(directory=str(settings.TEMPLATES_DIR))

# Enable auto-reload for templates in development
templates.env.auto_reload = settings.DEBUG
templates.env.cache_size = 0 if settings.DEBUG else 400

# Jinja filters
templates.env.filters['from_json'] = json.loads
templates.env.filters['tojson'] = json.dumps


def md(text: str | None) -> str:
    """Convert markdown to HTML with security."""
    if not text:
        return ""

    html = markdown2.markdown(
        text,
        extras=[
            'fenced-code-blocks',
            'tables',
            'break-on-newline',
            'cuddled-lists',
            'header-ids',
        ]
    )
    # Add target="_blank" and rel="noopener noreferrer" for security
    html = re.sub(r'<a href="([^"]+)">', r'<a href="\1" target="_blank" rel="noopener noreferrer">', html)
    return html


# Add markdown filter and global to Jinja
templates.env.filters['md'] = md
templates.env.globals['md'] = md
templates.env.filters['human_size'] = data_sync.human_size


async def generate_response_task(chat_id: int):
    """Background task to generate LLM response."""
    try:
        chat = await Chat.get(id=chat_id)
        await generate_response(chat)
    except Exception as e:
        print(f"[ERROR] Failed to generate response: {e}")
        import traceback
        traceback.print_exc()

        # Show error to user instead of hanging forever
        try:
            chat = await Chat.get(id=chat_id)
            await chat.add_assistant_message(
                content=f"**Error:** {str(e)}\n\nPlease try again."
            )
            from app.services.sse import notify_chat_update
            await notify_chat_update(chat.id)
        except Exception:
            pass


@app.get("/about")
async def about(request: Request, session_key: str = Depends(get_session_key)):
    """About / Methodology page."""
    chats = await Chat.filter(session_key=session_key).order_by("-updated_at")
    return templates.TemplateResponse(
        request,
        "about.html",
        {"chats": chats}
    )


# ---------------------------------------------------------------------------
# Data management: download / update the Parquet datasets
# ---------------------------------------------------------------------------

@app.get("/data")
async def data_page(request: Request, session_key: str = Depends(get_session_key)):
    """Dataset status page with download / update buttons."""
    duckdb_manager.ensure_current()
    chats = await Chat.filter(session_key=session_key).order_by("-updated_at")
    status = await data_sync.get_status()
    return templates.TemplateResponse(
        request,
        "data.html",
        {"chats": chats, "status": status, "missing": duckdb_manager.missing_datasets()},
    )


@app.get("/data/status")
async def data_status(request: Request, refresh: bool = False):
    """Partial: the dataset rows. Polled by HTMX while a job runs."""
    duckdb_manager.ensure_current()
    status = await data_sync.get_status(force=refresh)
    return templates.TemplateResponse(
        request,
        "partials/data_rows.html",
        {"status": status, "missing": duckdb_manager.missing_datasets()},
    )


@app.post("/data/{name}/update")
async def data_update(request: Request, name: str):
    """Start a background download of the newest release for one dataset."""
    if name not in data_sync.DATASETS:
        return Response("Unknown dataset", status_code=404)
    data_sync.start_update(name)
    status = await data_sync.get_status()
    return templates.TemplateResponse(
        request,
        "partials/data_rows.html",
        {"status": status, "missing": duckdb_manager.missing_datasets()},
    )


@app.get("/")
async def index(request: Request, session_key: str = Depends(get_session_key)):
    """Homepage with chat list and example prompts."""
    duckdb_manager.ensure_current()
    if duckdb_manager.missing_datasets():
        # First run: no Parquet data yet. Send the user to the download page.
        return RedirectResponse("/data", status_code=302)

    chats = await Chat.filter(session_key=session_key).order_by("-updated_at")
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "example_prompts": EXAMPLE_PROMPTS,
            "chats": chats
        }
    )
    response.delete_cookie("chat_id")
    return response


@app.get("/chats/{chat_id}")
async def chat_detail(request: Request, chat_id: int, session_key: str = Depends(get_session_key)):
    """Chat detail page."""
    chat = await Chat.get_or_none(id=chat_id, session_key=session_key)
    if not chat:
        return RedirectResponse("/", status_code=302)

    chats = await Chat.filter(session_key=session_key).order_by("-updated_at")
    chat_history = await chat.as_openai_api_format()

    response = templates.TemplateResponse(
        request,
        "chat.html",
        {
            "chat_history": chat_history,
            "chat_id": chat_id,
            "chats": chats
        }
    )
    response.set_cookie("chat_id", str(chat_id), max_age=60*60*24*365)
    return response


VIEW_LABELS = {
    # ClinicalTrials.gov (AACT) views, see app/duckdb.py
    "v_studies": "clinical study records",
    "v_study_drugs": "drug interventions",
    "v_study_conditions": "study conditions",
    "v_drug_trials": "drug trial data",
    "v_condition_trials": "condition-specific trials",
    "v_drug_condition_trials": "drug–condition trial matches",
    "v_trial_summary": "trial summaries with sponsors",
    "v_trial_outcomes": "trial outcome measures",
    "v_trial_results": "trial result data",
    "v_trial_eligibility": "eligibility criteria",
    "v_trial_arms": "trial arm details",
    "v_trial_measurements": "per-arm outcome measurements",
    # DrugCentral views
    "v_drug_info": "drug information",
    "v_drug_indications": "drug indications",
    "v_drug_targets": "drug–target associations",
    "v_disease_drugs": "disease–drug associations",
    "v_drug_classification": "pharmacological classification",
    "v_drug_interactions": "drug–drug interactions",
    "v_adverse_events": "adverse event reports",
    "v_drug_approvals": "FDA approval records",
    "v_drug_properties": "chemical properties",
    "v_drug_identifiers": "drug identifiers",
    "v_drug_atc": "ATC therapeutic classification",
    # Pharos / TCRD tables
    "protein": "protein target records",
    "target": "target development levels",
    "disease": "disease associations",
    "ncats_disease": "disease associations",
    "ncats_ligands": "ligand data",
    "ncats_ligand_activity": "ligand activity data",
    "ncats_ppi": "protein–protein interactions",
    "drug_activity": "drug activity data",
    "tinx_novelty": "target novelty scores",
}


def _summarise_sql(sql: str) -> str:
    """Turn a SQL query into a short human-readable description."""
    sql_lower = sql.lower()

    # Extract the view/table name from the FROM clause
    from_match = re.search(r'\bfrom\s+(\w+)', sql_lower)
    view_name = from_match.group(1) if from_match else None
    view_label = VIEW_LABELS.get(view_name, view_name or "database records")

    # Extract search terms from WHERE ... IN ('x', 'y') or LIKE '%x%'
    terms = []
    for m in re.finditer(r"'([^']{2,40})'", sql):
        val = m.group(1).strip('%').strip()
        if val and val not in terms and not val.startswith(('select', 'from')):
            terms.append(val)

    if terms:
        # Show up to 5 terms, then "and N more"
        shown = terms[:5]
        remainder = len(terms) - len(shown)
        term_str = ", ".join(shown)
        if remainder > 0:
            term_str += f", and {remainder} more"
        return f"Searched {view_label} for {term_str}"

    return f"Queried {view_label}"


@app.get("/chats/{chat_id}/export")
async def export_chat_pdf(chat_id: int, session_key: str = Depends(get_session_key)):
    """Export the full conversation as a formatted PDF research report.

    Walks through every message in the chat and organises them into
    question/answer rounds, each with its tool calls and synthesis.
    """
    chat = await Chat.get_or_none(id=chat_id, session_key=session_key)
    if not chat:
        return RedirectResponse("/", status_code=302)

    chat_history = await chat.as_openai_api_format()

    # Build a list of Q&A rounds: each round has a user question,
    # zero or more tool calls, and a final assistant answer.
    rounds = []
    current_round = None

    for msg in chat_history:
        if msg["role"] == "user":
            # Start a new round
            current_round = {
                "question": msg["content"],
                "tool_calls": [],
                "answer_html": "",
            }
            rounds.append(current_round)

        elif msg["role"] == "assistant" and current_round is not None:
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func_name = tc["function"]["name"]
                    if "drugcentral" in func_name:
                        db_name = "DrugCentral"
                    elif "pharos" in func_name:
                        db_name = "Pharos (NIH TCRD)"
                    else:
                        db_name = "ClinicalTrials.gov (AACT)"

                    raw_args = tc["function"].get("arguments", "")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        sql = args.get("sql", "")
                    except (json.JSONDecodeError, AttributeError):
                        sql = ""

                    summary = _summarise_sql(sql) if sql else "General query"
                    current_round["tool_calls"].append({
                        "db_name": db_name,
                        "summary": summary,
                        "sql": sql,
                    })

            if msg.get("content"):
                current_round["answer_html"] = md(msg["content"])

    # Collect all databases queried across the entire conversation
    all_db_queries = {}
    for r in rounds:
        for tc in r["tool_calls"]:
            all_db_queries.setdefault(tc["db_name"], []).append(tc["summary"])

    # Render the report HTML via the dedicated template
    html_string = templates.env.get_template("report.html").render(
        chat=chat,
        rounds=rounds,
        db_queries=all_db_queries,
        generated_at=datetime.now(),
    )

    # Generate PDF with WeasyPrint
    from weasyprint import HTML
    pdf_bytes = HTML(
        string=html_string,
        base_url=str(settings.BASE_DIR / "static")
    ).write_pdf()

    filename = f"pharma-pilot-report-{chat.id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/messages/{msg_id}/feedback")
async def message_feedback(
    request: Request,
    msg_id: int,
    feedback: str = Form(...),
    session_key: str = Depends(get_session_key)
):
    """Set thumbs-up/down feedback on an assistant message."""
    msg = await Message.get_or_none(id=msg_id, role="assistant")
    if not msg:
        return Response("Message not found", status_code=404)

    # Verify the message belongs to a chat owned by this session
    chat = await Chat.get_or_none(id=msg.chat_id, session_key=session_key)
    if not chat:
        return Response("Not authorised", status_code=403)

    if feedback not in ("positive", "negative"):
        return Response("Invalid feedback value", status_code=400)

    # Toggle: if user clicks the same feedback again, remove it
    if msg.feedback == feedback:
        msg.feedback = None
    else:
        msg.feedback = feedback
    await msg.save(update_fields=["feedback"])

    # Return the updated button pair so HTMX can swap it in
    return templates.TemplateResponse(
        request,
        "partials/feedback_buttons.html",
        {"msg": msg}
    )


@app.post("/send")
async def send_message(
    request: Request,
    background_tasks: BackgroundTasks,
    message: str = Form(...),
    chat_id: str | None = Cookie(default=None),
    session_key: str = Depends(get_session_key)
):
    """Send a user message to a chat."""
    # Get or create chat
    if chat_id:
        chat = await Chat.get_or_none(id=int(chat_id), session_key=session_key)
    else:
        chat = None

    is_new = not chat
    if is_new:
        chat = await Chat.create(
            title=message[:50] + ("..." if len(message) > 50 else ""),
            session_key=session_key
        )

    await chat.add_user_message(message)
    await notify_chat_update(chat.id)

    # Start background response generation
    background_tasks.add_task(generate_response_task, chat.id)

    if is_new:
        response = Response(status_code=200, headers={"HX-Redirect": f"/chats/{chat.id}"})
        response.set_cookie("chat_id", str(chat.id), max_age=60*60*24*365)
        return response

    return Response(status_code=204)


@app.get("/chat/{chat_id}/updates")
async def chat_updates_sse(
    request: Request,
    chat_id: int,
    session_key: str = Depends(get_session_key)
):
    """SSE endpoint with two channels: content-stream (lightweight) and chat-update (full morph)."""
    chat = await Chat.get_or_none(id=chat_id, session_key=session_key)
    if not chat:
        return Response(status_code=404)

    async def event_stream():
        queue = asyncio.Queue()

        if chat_id not in chat_update_subscribers:
            chat_update_subscribers[chat_id] = []
        chat_update_subscribers[chat_id].append(queue)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                # Coalesce: drain queued messages, keep only the latest stream
                # and whether any full update was requested.
                latest_stream = msg if msg.get("type") == "stream" else None
                needs_full = msg.get("type") != "stream"

                while not queue.empty():
                    try:
                        msg = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if msg.get("type") == "stream":
                        latest_stream = msg
                    else:
                        needs_full = True

                # A full update supersedes any pending stream update.
                if needs_full:
                    chat_history = await chat.as_openai_api_format()
                    response = templates.TemplateResponse(
                        request,
                        "chat.html",
                        {"chat_history": chat_history, "chat_id": chat_id, "is_sse": True},
                        block_name="content",
                    )
                    html = response.body.decode("utf-8")
                    lines = html.split("\n")
                    sse_data = "\n".join(f"data: {line}" for line in lines)
                    yield f"event: chat-update\n{sse_data}\n\n"

                elif latest_stream:
                    rendered = md(latest_stream["content"])
                    payload = json.dumps({
                        "msgId": latest_stream["msg_id"],
                        "html": rendered,
                    })
                    yield f"event: content-stream\ndata: {payload}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            if chat_id in chat_update_subscribers:
                try:
                    chat_update_subscribers[chat_id].remove(queue)
                    if not chat_update_subscribers[chat_id]:
                        del chat_update_subscribers[chat_id]
                except ValueError:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/chat")
async def chat_list(request: Request, search: str = "", session_key: str = Depends(get_session_key)):
    """Search/list chats."""
    if search:
        chats = await Chat.filter(session_key=session_key, title__icontains=search).order_by("-updated_at")
    else:
        chats = await Chat.filter(session_key=session_key).order_by("-updated_at")

    return templates.TemplateResponse(
        request,
        "base.html",
        {"chats": chats},
        block_name="chat_list"
    )


@app.post("/chats/{chat_id}/delete")
async def delete_chat(
    request: Request,
    chat_id: int,
    chat_id_cookie: str | None = Cookie(default=None, alias="chat_id"),
    session_key: str = Depends(get_session_key)
):
    """Delete a chat."""
    chat = await Chat.get_or_none(id=chat_id, session_key=session_key)
    if not chat:
        return RedirectResponse("/", status_code=404)

    is_active = chat_id_cookie and int(chat_id_cookie) == chat_id
    await chat.delete()

    if is_active:
        response = Response(status_code=200, headers={"HX-Redirect": "/"})
        response.delete_cookie("chat_id")
        return response
    else:
        return Response(status_code=200, headers={"HX-Trigger": "refreshChats"})


@app.get("/tool-results/{tool_call_id}")
async def get_tool_results(
    request: Request,
    tool_call_id: str,
    session_key: str = Depends(get_session_key)
):
    """Get full tool results for display."""
    # Find the tool result message
    messages = await Message.filter(tool_call_id=tool_call_id).limit(1)
    if not messages:
        return Response("Tool result not found", status_code=404)

    tool_msg = messages[0]
    chat = await Chat.get_or_none(id=tool_msg.chat_id, session_key=session_key)
    if not chat:
        return Response("Chat not found", status_code=404)

    # Get all messages for the chat to find the tool result
    chat_history = await chat.as_openai_api_format()

    # Find the specific tool result
    tool_result_content = None
    for msg in chat_history:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            tool_result_content = msg.get("content")
            break

    if not tool_result_content:
        return Response("Tool result content not found", status_code=404)

    return templates.TemplateResponse(
        request,
        "partials/tool_results.html",
        {
            "content": tool_result_content
        }
    )
