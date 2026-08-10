from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from activity_policy.schema import ActivityPolicy, PolicyError


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            label = "activity ID" if isinstance(key, str) and "." in key else "key"
            raise PolicyError(f"duplicate {label}: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(text: str):
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader) or {}
    except PolicyError:
        raise
    except Exception as exc:
        raise PolicyError("invalid policy YAML") from exc


_ACTIVITY_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9]+)*$")

@dataclass(frozen=True)
class ActivityRegistry:
    enforcement: str
    policies: dict[str, ActivityPolicy]
    aliases: dict[str, str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ActivityRegistry":
        if not isinstance(data, Mapping):
            raise PolicyError("policy document must be a mapping")
        unknown = set(data) - {"enforcement", "activities"}
        if unknown:
            raise PolicyError(f"policy document has unknown keys: {sorted(unknown)}")
        if set(data) != {"enforcement", "activities"}:
            raise PolicyError("policy document requires enforcement and activities")
        if data["enforcement"] != "observe":
            raise PolicyError("enforcement must be 'observe'")
        activities = data["activities"]
        if not isinstance(activities, Mapping):
            raise PolicyError("activities must be a mapping")
        policies = {}
        for activity_id, declaration in activities.items():
            if not isinstance(activity_id, str) or not _ACTIVITY_ID.fullmatch(activity_id):
                raise PolicyError(f"invalid stable dotted activity ID: {activity_id!r}")
            if activity_id in policies:
                raise PolicyError(f"duplicate activity ID: {activity_id}")
            policies[activity_id] = ActivityPolicy.from_mapping(activity_id, declaration)
        aliases = {}
        ids = set(policies)
        for activity_id, policy in policies.items():
            for alias in policy.aliases:
                if alias in ids:
                    raise PolicyError(f"alias {alias!r} collides with activity ID")
                if alias in aliases:
                    raise PolicyError(f"duplicate alias: {alias}")
                aliases[alias] = activity_id
        return cls("observe", policies, aliases)

    @classmethod
    def load(cls, path: Path) -> "ActivityRegistry":
        raw = _load_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise PolicyError("policy document must be a mapping")
        return cls.from_mapping(raw)

    @classmethod
    def load_default(cls) -> "ActivityRegistry":
        resource = files("activity_policy").joinpath("policies.yaml")
        raw = _load_yaml(resource.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise PolicyError("policy document must be a mapping")
        return cls.from_mapping(raw)

    def require(self, activity_id: str) -> ActivityPolicy:
        try:
            return self.policies[activity_id]
        except KeyError as exc:
            raise PolicyError(f"activity policy not found: {activity_id}") from exc

    def resolve(self, activity_id: str | None = None, alias: str | None = None) -> ActivityPolicy | None:
        if activity_id:
            return self.require(activity_id)
        resolved = self.aliases.get(alias or "")
        return self.policies[resolved] if resolved else None
