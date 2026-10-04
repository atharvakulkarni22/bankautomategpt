"""The saved, reusable task definition (pydantic models, stored as YAML).

Plain data only: this package must never import the LLM or agent code, because
the replay engine uses it and replay must run with no LLM.
"""

from .builder import BuildError, BuildResult, build_artifact, default_name, load_recording
from .schema import (
    Artifact,
    Expected,
    Input,
    Interruption,
    Locator,
    Metadata,
    Outcome,
    Output,
    Step,
    SuccessCheck,
)
from .store import (
    ArtifactError,
    approve_artifact,
    list_artifacts,
    load_artifact,
    next_version,
    resolve_artifact_path,
    save_artifact,
)

__all__ = [
    "Artifact",
    "ArtifactError",
    "BuildError",
    "BuildResult",
    "Expected",
    "Input",
    "Interruption",
    "Locator",
    "Metadata",
    "Outcome",
    "Output",
    "Step",
    "SuccessCheck",
    "approve_artifact",
    "build_artifact",
    "default_name",
    "list_artifacts",
    "load_artifact",
    "load_recording",
    "next_version",
    "resolve_artifact_path",
    "save_artifact",
]
