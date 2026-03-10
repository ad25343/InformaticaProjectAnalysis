"""
Project config parser — reads and validates *.project.yaml files.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.backend.models.schemas import ProjectConfig

logger = logging.getLogger(__name__)


class ConfigParseError(Exception):
    """Raised when a project config cannot be parsed or validated."""


def parse_project_config(config_path: str | Path) -> ProjectConfig:
    """
    Parse a *.project.yaml file into a validated ProjectConfig.

    Uses yaml.safe_load (no arbitrary Python execution).
    Validates against the Pydantic schema.

    Args:
        config_path: Path to the project YAML file.

    Returns:
        Validated ProjectConfig instance.

    Raises:
        ConfigParseError: If the file cannot be read, parsed, or validated.
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise ConfigParseError(f"Config file not found: {config_path}")

    if not config_path.suffix in (".yaml", ".yml"):
        raise ConfigParseError(
            f"Config file must be .yaml or .yml, got: {config_path.suffix}"
        )

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigParseError(f"Cannot read config file: {e}") from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigParseError(f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ConfigParseError("Config file must be a YAML mapping (dict)")

    # Validate required top-level keys
    if "project" not in data:
        raise ConfigParseError("Missing required key: 'project'")
    if "source" not in data:
        raise ConfigParseError("Missing required key: 'source'")

    try:
        config = ProjectConfig(**data)
    except ValidationError as e:
        raise ConfigParseError(f"Config validation failed:\n{e}") from e

    # Post-validation: check source type
    valid_source_types = {"folder", "repo", "zip", "s3"}
    if config.source.type not in valid_source_types:
        raise ConfigParseError(
            f"Invalid source type '{config.source.type}'. "
            f"Must be one of: {', '.join(sorted(valid_source_types))}"
        )

    logger.info(
        "Parsed project config: %s (source=%s, location=%s)",
        config.project.name,
        config.source.type,
        config.source.location,
    )

    return config
