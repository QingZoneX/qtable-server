from app.context_engine.models import AgentContext, ContextBuildInput

__all__ = [
    "AgentContext",
    "ContextBuildInput",
    "ContextBuilder",
    "ContextInjector",
    "ContextRepository",
    "context_builder",
    "context_injector",
    "context_repository",
]


def __getattr__(name: str):
    if name in {"ContextBuilder", "context_builder"}:
        from app.context_engine.builder import ContextBuilder, context_builder

        return {"ContextBuilder": ContextBuilder, "context_builder": context_builder}[name]
    if name in {"ContextInjector", "context_injector"}:
        from app.context_engine.injector import ContextInjector, context_injector

        return {"ContextInjector": ContextInjector, "context_injector": context_injector}[name]
    if name in {"ContextRepository", "context_repository"}:
        from app.context_engine.repository import ContextRepository, context_repository

        return {"ContextRepository": ContextRepository, "context_repository": context_repository}[name]
    raise AttributeError(f"module 'app.context_engine' has no attribute {name!r}")
