"""Authenticated browser attachment to an existing design PTY, never a shell launcher."""
import asyncio
import codecs
import json
from pathlib import Path
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from cairn.server.access import require_operator, require_worker, local_browser
from cairn.server.db import get_conn
from cairn.server import development_store as store

router = APIRouter(prefix="/projects/{project_id}/development/runs/{run_id}", tags=["design-terminal"])
owners = {}


def terminal_files(conn, project_id, run_id):
    row = conn.execute("SELECT * FROM runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
    if row is None: raise HTTPException(404, "Run not found")
    stream = json.loads(row["streams"]).get("terminal")
    if not stream: raise HTTPException(404, "此运行没有真实终端")
    path = Path(stream).resolve()
    config = store.project(conn, project_id)
    if (path.name != "terminal.log" or path.parent.name != run_id or path.parent.parent.name != "runs"
            or path.parent.parent.parent.name != project_id):
        raise HTTPException(403, "Invalid terminal ownership")
    if config.workspace_root:
        from cairn.workspace import project_workspace
        if not path.is_relative_to(project_workspace(config, project_id, Path(config.repository))):
            raise HTTPException(403, "Terminal is outside project storage")
    work = store.work(conn, project_id, row["intent_id"])
    subject = store.work(conn,project_id,work.subject) if work.subject else None
    if work.purpose!='design' and not work.result.get('design_candidate') and not (subject and subject.result.get('design_candidate')):
        raise HTTPException(403,'开发运行不提供交互终端')
    return row, path, work


def append_control(directory, value):
    target = directory / "terminal-input.jsonl"
    if target.is_symlink(): raise HTTPException(403, "Invalid terminal input path")
    with target.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False)+"\n")


class NativeInput(BaseModel):
    token: str
    content: str = Field(min_length=1, max_length=200000)
    key: str = Field(min_length=1, max_length=200)
    nonce: str


@router.post("/terminal/input", dependencies=[Depends(require_worker)])
def native_input(project_id: str, run_id: str, body: NativeInput):
    from cairn.server.routers.development import live_run
    from cairn.server.design import record, load
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = live_run(conn, project_id, run_id, body.token)
        _, path, work = terminal_files(conn, project_id, run_id)
        if work.purpose != "design" or row["cancel_requested"] or load(conn, project_id).candidate.get('digest'):
            raise HTTPException(409, "Design input lease is closed")
        prior = conn.execute("SELECT id FROM design_messages WHERE project_id=? AND key=?", (project_id, body.key)).fetchone()
        if prior:
            return {'id':record(conn,project_id,body.key,'user',{'content':body.content,'answers':{}})}
        ticket_file = path.parent / "operator-input.json"
        ticket = json.loads(ticket_file.read_text(encoding="utf-8")) if ticket_file.is_file() else {}
        if ticket.get("nonce") != body.nonce or time.time()-ticket.get("time", 0)>60 or ticket.get("used"):
            raise HTTPException(409, "No fresh operator input authorizes this prompt")
        mid = record(conn, project_id, body.key, "user", {"content":body.content,"answers":{}})
        conn.execute("INSERT INTO design_inputs(message_id,project_id,intent_id,run_id,state) VALUES(?,?,?,?,'delivered')",
                     (mid, project_id, row["intent_id"], run_id))
        from cairn.dispatcher.development import atomic_json
        atomic_json(ticket_file, {**ticket,"used":True})
        return {"id":mid}


@router.post("/terminal/close", dependencies=[Depends(require_operator)])
def close_terminal(project_id: str, run_id: str):
    with get_conn() as conn:
        row, path, _ = terminal_files(conn, project_id, run_id)
        if row["state"] != "running": return {"closing":False}
        state_file = path.parent / "terminal-state.json"
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        pending = conn.execute("SELECT 1 FROM design_inputs WHERE project_id=? AND intent_id=? AND state IN ('queued','sending')", (project_id, row['intent_id'])).fetchone()
        ticket_file = path.parent / "operator-input.json"
        ticket = json.loads(ticket_file.read_text(encoding="utf-8")) if ticket_file.is_file() else {}
        if state.get("state") != "idle" or pending or ticket.get("time",0)>state.get("time",0):
            raise HTTPException(409, "请等待设计 Agent 完成本轮并保存方案")
        append_control(path.parent, {"type":"close"})
        return {"closing":True}


@router.websocket("/terminal")
async def attach_terminal(websocket: WebSocket, project_id: str, run_id: str):
    scope = {**websocket.scope,"type":"http","scheme":"https" if websocket.url.scheme=="wss" else "http"}
    request = Request(scope)
    try:
        require_operator(request)
        if not local_browser(request): raise HTTPException(403, "真实终端仅允许本机访问")
        with get_conn() as conn:
            row, path, work = terminal_files(conn, project_id, run_id)
    except HTTPException:
        await websocket.close(code=1008); return
    await websocket.accept()
    owner = owners.setdefault(run_id, websocket)
    readonly = work.purpose != "design" or row["state"] != "running" or owner is not websocket
    await websocket.send_json({"type":"ready","readonly":readonly})

    async def output():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        offset = max(0, path.stat().st_size-2_000_000) if path.exists() else 0
        restore = True; previous = None
        while True:
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(offset); data=stream.read(65536); offset+=len(data)
                if data:
                    await websocket.send_json({"type":"output","data":decoder.decode(data),"restore":restore})
                    restore=False
            state_file=path.parent/"terminal-state.json"
            if state_file.is_file():
                state=state_file.read_text(encoding="utf-8")
                if state!=previous:
                    value=json.loads(state)
                    await websocket.send_json({"type":"state","state":value.get("state","unknown")})
                    previous=state
            await asyncio.sleep(.05)

    sender=asyncio.create_task(output())
    try:
        while True:
            value=await websocket.receive_json()
            if not isinstance(value,dict): continue
            kind=value.get("type")
            if kind=="resize" and owner is websocket and row["state"]=="running":
                try: control={"type":"resize","rows":max(8,min(int(value["rows"]),150)),"cols":max(30,min(int(value["cols"]),400))}
                except (ValueError,TypeError,KeyError): continue
                append_control(path.parent,control)
            elif kind=="input" and not readonly:
                text=value.get("data")
                if not isinstance(text,str) or len(text)>65536:
                    await websocket.close(code=1009); break
                with get_conn() as conn:
                    current, _, _=terminal_files(conn,project_id,run_id)
                    if current["state"]!="running" or current["cancel_requested"]: continue
                if "\r" in text:
                    from cairn.dispatcher.development import atomic_json
                    atomic_json(path.parent/"operator-input.json",{"nonce":str(uuid.uuid4()),"time":time.time()})
                append_control(path.parent,{"type":"input","data":text})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel(); await asyncio.gather(sender,return_exceptions=True)
        if owners.get(run_id) is websocket: owners.pop(run_id,None)
