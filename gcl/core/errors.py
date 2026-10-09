"""Core contract errors."""

class GCLError(Exception):
    pass

class GoalChangeUnauthorized(GCLError):
    pass

class IllegalTransition(GCLError):
    pass

class StateMoved(IllegalTransition):
    """A compare-and-set lost: the from-state had already moved. Re-read and drive from the actual state."""

class StaleEpoch(GCLError):
    pass

class CheckpointIncomplete(GCLError):
    pass

class MissionCancelled(GCLError):
    pass

class SchemaTooNew(GCLError):
    """The ledger was written by a newer release. Migrations only move forward, so this one must not write to it."""
