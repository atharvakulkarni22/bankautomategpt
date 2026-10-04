"""Saving artifacts as YAML files, and loading them back with validation.

Files live in artifacts/ and are named <name>.v<version>.yaml.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from lba.surface.placeholders import Values

from .schema import Artifact

DEFAULT_DIR = Path("artifacts")
FILE_NAME = re.compile(r"^(?P<name>[a-z0-9][a-z0-9_-]*)\.v(?P<version>\d+)\.yaml$")
HEADER = (
    "# lba artifact: a saved, reusable task. Safe to edit by hand; it is checked every time it loads.\n"
    "# status draft = not yet reviewed. Run `lba approve <name>` once you have read it through.\n"
    "# Secrets appear only as {{secret:NAME}} placeholders, never as real values.\n"
)


class ArtifactError(Exception):
    """An artifact could not be saved, loaded or found. The message says why."""


@dataclass
class Entry:
    """One file in the artifacts folder: either a valid artifact or the reason it is not."""

    path: Path
    artifact: Artifact | None = None
    error: str | None = None


def artifact_path(directory, name: str, version: int) -> Path:
    return Path(directory) / f"{name}.v{version}.yaml"


def _versions(directory, name: str) -> list[int]:
    found = []
    for path in Path(directory).glob(f"{name}.v*.yaml"):
        match = FILE_NAME.match(path.name)
        if match and match["name"] == name:
            found.append(int(match["version"]))
    return sorted(found)


def next_version(directory, name: str) -> int:
    """The next free version number for this name (1 if there is none yet)."""
    existing = _versions(directory, name)
    return existing[-1] + 1 if existing else 1


def to_yaml(artifact: Artifact) -> str:
    data = artifact.model_dump(mode="json", exclude_none=True)
    return HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def save_artifact(artifact: Artifact, directory=DEFAULT_DIR, *, overwrite=False, values: Values | None = None) -> Path:
    """Write the artifact to artifacts/<name>.v<version>.yaml and return the path.

    Refuses to overwrite an existing file unless overwrite=True, and refuses to
    write anything that contains a real secret value.
    """
    text = to_yaml(artifact)
    values = values or Values()
    if values.redact(text) != text:
        raise ArtifactError("Refusing to save: the artifact contains a real secret value. Use {{secret:NAME}} instead.")

    path = artifact_path(directory, artifact.metadata.name, artifact.metadata.version)
    if path.exists() and not overwrite:
        raise ArtifactError(f"{path} already exists. Pick a new --version; existing versions are never overwritten.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")  # write then swap, so a crash never leaves half a file
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    return path


def load_artifact(path) -> Artifact:
    """Read and validate an artifact file. Raises ArtifactError with a readable message."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ArtifactError(f"Cannot read {path}: {error.strerror or error}") from error
    except yaml.YAMLError as error:
        raise ArtifactError(f"{path.name} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ArtifactError(f"{path.name} does not look like an artifact (expected a mapping at the top).")
    try:
        artifact = Artifact.model_validate(data)
    except ValidationError as error:
        problems = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()[:8]]
        raise ArtifactError(f"{path.name} is not a valid artifact:\n" + "\n".join(problems)) from error

    match = FILE_NAME.match(path.name)  # the file name must agree with what is inside
    if match and (match["name"], int(match["version"])) != (artifact.metadata.name, artifact.metadata.version):
        raise ArtifactError(
            f"{path.name} says it is {artifact.metadata.name} v{artifact.metadata.version}. "
            "Rename the file or fix metadata.name / metadata.version."
        )
    return artifact


def list_artifacts(directory=DEFAULT_DIR) -> list[Entry]:
    """Every artifact file in the folder, sorted by name then version. Broken files are listed too."""
    entries = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            entries.append(Entry(path, artifact=load_artifact(path)))
        except ArtifactError as error:
            entries.append(Entry(path, error=str(error)))
    return entries


def resolve_artifact_path(ref: str, directory=DEFAULT_DIR) -> Path:
    """Turn what the user typed into a file: a path, 'name.v2', or just 'name' (the latest version)."""
    if Path(ref).is_file():
        return Path(ref)
    candidate = Path(directory) / (ref if ref.endswith(".yaml") else f"{ref}.yaml")
    if candidate.is_file():
        return candidate
    versions = _versions(directory, ref)
    if versions:
        return artifact_path(directory, ref, versions[-1])
    raise ArtifactError(f"No artifact '{ref}' found in {directory}. Run `lba list` to see what exists.")


def approve_artifact(path) -> Artifact:
    """Mark an artifact approved and save it in place. Re-validates first."""
    artifact = load_artifact(path)
    if artifact.metadata.status == "approved":
        return artifact
    artifact.metadata.status = "approved"
    text = to_yaml(artifact)
    Path(path).write_text(text, encoding="utf-8")
    return artifact
