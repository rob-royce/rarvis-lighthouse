"""Minimal explicit boundaries for a future private host integration."""
from typing import Any, Mapping, Protocol

class GoalProvider(Protocol):
    def goal_spec(self) -> Mapping[str, Any]: ...

class EvidenceProvider(Protocol):
    def verified_evidence(self) -> list[Mapping[str, Any]]: ...

class WorkspaceProvider(Protocol):
    def execution_root(self) -> str: ...

class LeaseProvider(Protocol):
    def writer_valid(self) -> bool: ...

class TaskStateProvider(Protocol):
    def task_snapshot(self) -> Mapping[str, Any]: ...
