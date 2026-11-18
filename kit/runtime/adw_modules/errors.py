from __future__ import annotations


class LayerError(Exception):
    """Invalid or unsupported agentic-layer IR or layer contents."""


class StepFailed(Exception):
    """A workflow step failed with an actionable reason."""

    def __init__(self, step: str, reason: str) -> None:
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


class RunStopped(StepFailed):
    """A person stopped the run (SIGTERM from launch.stop, e.g. the dashboard's Stop): it ends
    `failed` through the usual failure path, handed to a person like any failure."""


class StepWaiting(Exception):
    """A (script …) step exited with one of its #:waiting-exit codes: the run waits for something
    outside and is resumed later (`--resume`); it neither failed nor passed."""

    def __init__(self, step: str, code: int, reason: str) -> None:
        self.step = step
        self.code = code
        self.reason = reason
        super().__init__(f"{step}: {reason}")
