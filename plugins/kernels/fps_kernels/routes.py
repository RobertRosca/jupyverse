import json
import pathlib
import sys
import uuid
from http import HTTPStatus

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse
from fps.hooks import register_router  # type: ignore
from fps_auth_base import User, current_user, websocket_auth  # type: ignore
from fps_frontend.config import get_frontend_config  # type: ignore
from fps_yjs.routes import YDocWebSocketHandler  # type: ignore
from starlette.requests import Request  # type: ignore

from .kernel_driver.driver import KernelDriver  # type: ignore
from .kernel_server.server import (  # type: ignore
    AcceptedWebSocket,
    KernelServer,
    kernels,
)
from .models import Execution, Session

router = APIRouter()

kernelspecs: dict = {}
sessions: dict = {}
prefix_dir: pathlib.Path = pathlib.Path(sys.prefix)


@router.on_event("shutdown")
async def stop_kernels():
    for kernel in kernels.values():
        await kernel["server"].stop()


@router.get("/api/kernelspecs")
async def get_kernelspecs(
    frontend_config=Depends(get_frontend_config),
    user: User = Depends(current_user(permissions={"kernelspecs": ["read"]})),
):
    for path in (prefix_dir / "share" / "jupyter" / "kernels").glob("*/kernel.json"):
        with open(path) as f:
            spec = json.load(f)
        name = path.parent.name
        resources = {
            f.stem: f"{frontend_config.base_url}kernelspecs/{name}/{f.name}"
            for f in path.parent.iterdir()
            if f.is_file() and f.name != "kernel.json"
        }
        kernelspecs[name] = {"name": name, "spec": spec, "resources": resources}
    return {"default": "python3", "kernelspecs": kernelspecs}


@router.get("/kernelspecs/{kernel_name}/{file_name}")
async def get_kernelspec(
    kernel_name,
    file_name,
    user: User = Depends(current_user()),
):
    return FileResponse(prefix_dir / "share" / "jupyter" / "kernels" / kernel_name / file_name)


@router.get("/api/kernels")
async def get_kernels(user: User = Depends(current_user(permissions={"kernels": ["read"]}))):
    results = []
    for kernel_id, kernel in kernels.items():
        results.append(
            {
                "id": kernel_id,
                "name": kernel["name"],
                "connections": kernel["server"].connections,
                "last_activity": kernel["server"].last_activity["date"],
                "execution_state": kernel["server"].last_activity["execution_state"],
            }
        )
    return results


@router.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user: User = Depends(current_user(permissions={"sessions": ["write"]})),
):
    kernel_id = sessions[session_id]["kernel"]["id"]
    kernel_server = kernels[kernel_id]["server"]
    await kernel_server.stop()
    del kernels[kernel_id]
    del sessions[session_id]
    return Response(status_code=HTTPStatus.NO_CONTENT.value)


@router.patch("/api/sessions/{session_id}")
async def rename_session(
    request: Request,
    user: User = Depends(current_user(permissions={"sessions": ["write"]})),
):
    rename_session = await request.json()
    session_id = rename_session.pop("id")
    for key, value in rename_session.items():
        sessions[session_id][key] = value
    return Session(**sessions[session_id])


@router.get("/api/sessions")
async def get_sessions(user: User = Depends(current_user(permissions={"sessions": ["read"]}))):
    for session in sessions.values():
        kernel_id = session["kernel"]["id"]
        kernel_server = kernels[kernel_id]["server"]
        session["kernel"]["last_activity"] = kernel_server.last_activity["date"]
        session["kernel"]["execution_state"] = kernel_server.last_activity["execution_state"]
    return list(sessions.values())


@router.post(
    "/api/sessions",
    status_code=201,
    response_model=Session,
)
async def create_session(
    request: Request,
    user: User = Depends(current_user(permissions={"sessions": ["write"]})),
):
    create_session = await request.json()
    kernel_name = create_session["kernel"]["name"]
    kernel_server = KernelServer(
        kernelspec_path=str(
            prefix_dir / "share" / "jupyter" / "kernels" / kernel_name / "kernel.json"
        ),
    )
    kernel_id = str(uuid.uuid4())
    kernels[kernel_id] = {"name": kernel_name, "server": kernel_server, "driver": None}
    await kernel_server.start()
    session_id = str(uuid.uuid4())
    session = {
        "id": session_id,
        "path": create_session["path"],
        "name": create_session["name"],
        "type": create_session["type"],
        "kernel": {
            "id": kernel_id,
            "name": create_session["kernel"]["name"],
            "connections": kernel_server.connections,
            "last_activity": kernel_server.last_activity["date"],
            "execution_state": kernel_server.last_activity["execution_state"],
        },
        "notebook": {"path": create_session["path"], "name": create_session["name"]},
    }
    sessions[session_id] = session
    return Session(**session)


@router.post("/api/kernels/{kernel_id}/restart")
async def restart_kernel(
    kernel_id,
    user: User = Depends(current_user(permissions={"kernels": ["write"]})),
):
    if kernel_id in kernels:
        kernel = kernels[kernel_id]
        await kernel["server"].restart()
        result = {
            "id": kernel_id,
            "name": kernel["name"],
            "connections": kernel["server"].connections,
            "last_activity": kernel["server"].last_activity["date"],
            "execution_state": kernel["server"].last_activity["execution_state"],
        }
        return result


@router.post("/api/kernels/{kernel_id}/execute")
async def execute_cell(
    request: Request,
    kernel_id,
    user: User = Depends(current_user(permissions={"kernels": ["write"]})),
):
    r = await request.json()
    execution = Execution(**r)
    if kernel_id in kernels:
        ynotebook = YDocWebSocketHandler.websocket_server.get_room(execution.document_id).document
        cell = ynotebook.get_cell(execution.cell_idx)
        cell["outputs"] = []

        kernel = kernels[kernel_id]
        kernelspec_path = str(
            prefix_dir / "share" / "jupyter" / "kernels" / kernel["name"] / "kernel.json"
        )
        if not kernel["driver"]:
            kernel["driver"] = driver = KernelDriver(
                kernelspec_path=kernelspec_path,
                write_connection_file=False,
                connection_file=kernel["server"].connection_file_path,
            )
            await driver.connect()
        driver = kernel["driver"]

        await driver.execute(cell)
        ynotebook.set_cell(execution.cell_idx, cell)


@router.get("/api/kernels/{kernel_id}")
async def get_kernel(
    kernel_id,
    user: User = Depends(current_user(permissions={"kernels": ["read"]})),
):
    if kernel_id in kernels:
        kernel = kernels[kernel_id]
        result = {
            "id": kernel_id,
            "name": kernel["name"],
            "connections": kernel["server"].connections,
            "last_activity": kernel["server"].last_activity["date"],
            "execution_state": kernel["server"].last_activity["execution_state"],
        }
        return result


@router.websocket("/api/kernels/{kernel_id}/channels")
async def kernel_channels(
    kernel_id,
    session_id,
    websocket_permissions=Depends(websocket_auth(permissions={"kernels": ["execute"]})),
):
    if websocket_permissions is None:
        return
    websocket, permissions = websocket_permissions
    subprotocol = (
        "v1.kernel.websocket.jupyter.org"
        if "v1.kernel.websocket.jupyter.org" in websocket["subprotocols"]
        else None
    )
    await websocket.accept(subprotocol=subprotocol)
    accepted_websocket = AcceptedWebSocket(websocket, subprotocol)
    if kernel_id in kernels:
        kernel_server = kernels[kernel_id]["server"]
        await kernel_server.serve(accepted_websocket, session_id, permissions)


r = register_router(router)
