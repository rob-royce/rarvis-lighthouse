"""Executor adapters for the standalone GCL supervisor."""

from importlib import import_module

from .codex_appserver import CodexAppServer

__all__ = ["CodexAppServer", "create_adapter"]


def create_adapter(executor: str, config: dict, directory):
    if executor == "codex":
        return CodexAppServer(config.get("endpoint", "own"), log_path=str(directory / "app-server.log"),
                              approval_policy=config.get("approval_policy", "never"))
    if executor == "claude":
        # T3 supplies this module and its factory; no supervisor edit is needed.
        return import_module("gcl.adapters.claude_bg").create_adapter(config, directory)
    raise ValueError(f"unsupported executor: {executor}")
