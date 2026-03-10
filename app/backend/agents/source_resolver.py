"""
Source resolver — resolves project config source to a list of mapping XML files.

Phase 1, Step 1.1: Scan the source location using scope globs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.backend.models.schemas import ProjectConfig

logger = logging.getLogger(__name__)


class SourceResolutionError(Exception):
    """Raised when source location cannot be resolved."""


def resolve_source(config: ProjectConfig) -> list[Path]:
    """
    Resolve the source location to a list of XML file paths.

    Currently supports: folder type.
    Future: repo, zip, s3.

    Returns:
        Sorted list of Path objects to mapping XML files.
    """
    source_type = config.source.type

    if source_type == "folder":
        return _resolve_folder(config)
    elif source_type == "repo":
        raise SourceResolutionError("Git repo source type not yet implemented")
    elif source_type == "zip":
        raise SourceResolutionError("ZIP source type not yet implemented")
    elif source_type == "s3":
        raise SourceResolutionError("S3 source type not yet implemented")
    else:
        raise SourceResolutionError(f"Unknown source type: {source_type}")


def _resolve_folder(config: ProjectConfig) -> list[Path]:
    """Resolve a local folder source using scope globs."""
    root = Path(config.source.location)

    if not root.exists():
        raise SourceResolutionError(f"Source folder not found: {root}")

    if not root.is_dir():
        raise SourceResolutionError(f"Source path is not a directory: {root}")

    # Reject symlinks for security
    if root.is_symlink():
        raise SourceResolutionError(f"Symlinks not allowed: {root}")

    # Collect files matching include globs
    include_patterns = config.scope.mappings.include
    exclude_patterns = config.scope.mappings.exclude

    if not include_patterns:
        # Default: find all XML files
        include_patterns = ["**/*.xml"]

    included: set[Path] = set()
    for pattern in include_patterns:
        for match in root.glob(pattern):
            if match.is_file() and not match.is_symlink():
                included.add(match.resolve())

    # Remove excluded files
    excluded: set[Path] = set()
    for pattern in exclude_patterns:
        for match in root.glob(pattern):
            excluded.add(match.resolve())

    result = sorted(included - excluded)

    logger.info(
        "Resolved %d mapping files from %s (included=%d, excluded=%d)",
        len(result),
        root,
        len(included),
        len(excluded),
    )

    return result
