from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.core.i18n import locale_from_accept_language, reset_locale, set_locale
from app.core.security import security_configuration_errors
from app.api.graphql import graphql_app
from app.api.auth import router as auth_router
from app.api.oauth import router as oauth_router
from app.api.attachments import router as attachments_router
from app.api.ai import router as ai_router
from app.api.ai_router import router as ai_router_agent_router
from app.api.estimate_workload import router as estimate_workload_router
from app.api.structured_output import router as structured_output_router
from app.api.skill_runtime import router as skill_runtime_router
from app.api.task_split import router as task_split_router
from app.api.tool_chain import router as tool_chain_router
from app.api.agent_workflow import router as agent_workflow_router
from app.api.pm_agent import router as pm_agent_router
from app.api.clipper import router as clipper_router
from app.api.qnote_integration import router as qnote_integration_router
from app.db.init_db import init_models, seed_data
from app.services.attachment_cleanup import (
    start_attachment_cleanup_worker,
    stop_attachment_cleanup_worker,
)
from app.services.automation.worker import start_automation_worker, stop_automation_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: production must never boot with the known development secret.
    security_errors = security_configuration_errors()
    if security_errors:
        raise RuntimeError("; ".join(security_errors))
    await init_models()
    await seed_data()
    await start_automation_worker()
    await start_attachment_cleanup_worker()
    try:
        yield
    finally:
        await stop_attachment_cleanup_worker()
        await stop_automation_worker()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)


@app.middleware("http")
async def bind_request_locale(request: Request, call_next):
    """Bind product-owned server text to the caller's selected UI language."""
    locale_token = set_locale(
        locale_from_accept_language(request.headers.get("accept-language"))
    )
    try:
        return await call_next(request)
    finally:
        reset_locale(locale_token)


# Enable Gzip compression for responses (minimum 500 bytes)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

# CORS Configuration
origins = [
    "http://localhost:9100",  # Vite dev server
    "http://localhost:4173",  # Vite preview server (production build testing)
    "http://localhost:3000",
    "http://127.0.0.1:9100",  # Alternative localhost
    "http://127.0.0.1:4173",  # Alternative localhost for preview
    "qtable://login/callback",  # Tauri app custom URI scheme
    # Add production domains here when deploying:
    # "https://yourdomain.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Welcome to QTable API"}


# Mount GraphQL route
app.include_router(graphql_app, prefix="/graphql")
app.include_router(graphql_app, prefix="/graphql/{operation_name}")
app.include_router(graphql_app, prefix="/ws")
app.include_router(auth_router)
app.include_router(oauth_router)
app.include_router(attachments_router)
app.include_router(ai_router)
app.include_router(ai_router_agent_router)
app.include_router(estimate_workload_router)
app.include_router(structured_output_router)
app.include_router(skill_runtime_router)
app.include_router(task_split_router)
app.include_router(tool_chain_router)
app.include_router(agent_workflow_router)
app.include_router(pm_agent_router)
app.include_router(clipper_router)
app.include_router(qnote_integration_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=9000, reload=True)
