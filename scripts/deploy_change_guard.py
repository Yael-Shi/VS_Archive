#!/usr/bin/env python3
"""Fail-closed review of a VS-Archive app-stack CloudFormation change set.

The deploy script collects three structured inputs and then calls this
validator:

- ``describe-change-set`` JSON (merged if the API paginates)
- the stack's current ``get-template --template-stage Original`` body
- the change set's ``get-template --template-stage Original`` body

A human-readable ``cdk diff`` is not an approval source. Automatic
deployment is allowed only when the change set is exactly the web and
worker ECS task-definition image replacements plus the ECS service
task-definition references those replacements cause, and the templates
differ only by that image tag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


NO_CHANGE_REASON_PREFIXES = (
    "The submitted information didn't contain changes.",
    "No updates are to be performed.",
)

IMAGE_URI_RE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[A-Za-z0-9._/-]+):"
    r"(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$"
)
ECR_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
TASK_IMAGE_PATH_RE = re.compile(r"^/ContainerDefinitions/[0-9]+/Image$")

CHANGE_KEYS = {"Type", "ResourceChange", "HookInvocationCount"}
RESOURCE_CHANGE_KEYS = {
    "Action",
    "LogicalResourceId",
    "PhysicalResourceId",
    "ResourceType",
    "Replacement",
    "Scope",
    "Details",
    "ChangeSetId",
    "PolicyAction",
    "ModuleInfo",
    "ResourceDriftStatus",
    "ResourceDriftIgnoredAttributes",
    "BeforeContext",
    "AfterContext",
    "PreviousDeploymentContext",
}
NULL_ONLY_RESOURCE_KEYS = {
    "ModuleInfo",
    "ResourceDriftStatus",
    "ResourceDriftIgnoredAttributes",
    "BeforeContext",
    "AfterContext",
    "PreviousDeploymentContext",
}
TASK_POLICY_ACTIONS = {"ReplaceAndDelete", "ReplaceAndRetain"}
DETAIL_KEYS = {"Target", "Evaluation", "ChangeSource", "CausingEntity"}
TARGET_KEYS = {
    "Attribute",
    "Name",
    "RequiresRecreation",
    "Path",
    "BeforeValue",
    "AfterValue",
    "BeforeValueFrom",
    "AfterValueFrom",
    "Drift",
}

WEB_TASK = "web-task"
WORKER_TASK = "worker-task"
WEB_SERVICE = "web-service"
WORKER_SERVICE = "worker-service"
TASK_ROLES = (WEB_TASK, WORKER_TASK)
SERVICE_ROLES = (WEB_SERVICE, WORKER_SERVICE)
EXPECTED_ROLES = TASK_ROLES + SERVICE_ROLES
SERVICE_TASK_ROLE = {
    WEB_SERVICE: WEB_TASK,
    WORKER_SERVICE: WORKER_TASK,
}
# CloudFormation reports the same TaskDefinition reference as Static in the
# observed ECS service change set and as Dynamic in other reference updates.
SERVICE_REFERENCE_EVALUATIONS = {"Static", "Dynamic"}


class GuardRejection(Exception):
    """The deployment plan is not an allowed image-only update."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def merge_change_set_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine describe-change-set pages into one document."""
    if not pages:
        raise GuardRejection("change set description is empty")
    first = pages[0]
    if not isinstance(first, dict):
        raise GuardRejection("change set description is not a JSON object")
    changes: list[Any] = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise GuardRejection(f"change set page {index} is not a JSON object")
        if index:
            for key in ("Status", "ExecutionStatus", "StatusReason", "ChangeSetName"):
                if page.get(key) != first.get(key):
                    raise GuardRejection(
                        f"change set page {index} disagrees with page 0 on {key}"
                    )
        page_changes = page.get("Changes", [])
        if page_changes is None:
            page_changes = []
        if not isinstance(page_changes, list):
            raise GuardRejection(f"change set page {index} Changes is not a list")
        changes.extend(page_changes)
        token = page.get("NextToken") or ""
        if not isinstance(token, str):
            raise GuardRejection(f"change set page {index} NextToken is not a string")
        if index < len(pages) - 1 and not token:
            raise GuardRejection(
                f"change set page {index} is missing NextToken before another page"
            )
        if index == len(pages) - 1 and token:
            raise GuardRejection(
                "change set description is incomplete; the last page still has NextToken"
            )
    merged = dict(first)
    merged.pop("NextToken", None)
    merged["Changes"] = changes
    return merged


def validate_dependency_change_set(change_set: dict[str, Any], *, stack_name: str) -> None:
    """Reject anything other than a recognized CloudFormation no-op."""
    if _noop_reason(change_set) is None:
        raise GuardRejection(
            f"{stack_name} is not a no-op. Automatic deployment refuses "
            "network, data, and other unrelated stack changes."
        )


def validate_app_change(
    *,
    change_set: dict[str, Any],
    current_template: dict[str, Any] | None,
    proposed_template: dict[str, Any] | None,
    image_uri: str,
    repository_name: str,
    stack_name: str,
) -> str:
    """Return ``deploy`` or ``noop``. Raise GuardRejection when the plan is unsafe."""
    parsed = _parse_image_uri(image_uri, repository_name)
    if parsed["tag"] == "dev":
        raise GuardRejection("refusing image tag 'dev'")

    if not isinstance(change_set, dict):
        raise GuardRejection("change set description is not a JSON object")
    if change_set.get("NextToken"):
        raise GuardRejection(
            "change set description is incomplete; NextToken is still set"
        )

    noop_reason = _noop_reason(change_set)
    if noop_reason is not None:
        if current_template is None:
            raise GuardRejection(
                "no-op change set is missing the current stack template"
            )
        if proposed_template is not None:
            differences = _differences(current_template, proposed_template)
            if differences:
                raise GuardRejection(
                    "change set is a no-op but the templates differ: "
                    + _format_differences(differences)
                )
        _require_live_image(current_template, stack_name, repository_name, parsed["tag"])
        return "noop"

    _require_deployable_change_set(change_set)
    if current_template is None or proposed_template is None:
        raise GuardRejection(
            "deployable change set is missing the current or proposed template"
        )

    roles = _roles_by_logical_id(proposed_template, stack_name)
    current_roles = _roles_by_logical_id(current_template, stack_name)
    if roles != current_roles:
        raise GuardRejection(
            "web or worker logical resource identity changed between the "
            "current and proposed templates"
        )
    logical_by_role = {role: logical_id for logical_id, role in roles.items()}

    changes = change_set.get("Changes")
    if not isinstance(changes, list) or not changes:
        raise GuardRejection("deployable change set has no resource changes")
    seen_roles: list[str] = []
    for change in changes:
        seen_roles.append(
            _validate_resource_change(
                change,
                roles,
                logical_by_role,
                repository_name=repository_name,
                tag=parsed["tag"],
            )
        )
    missing = [role for role in EXPECTED_ROLES if role not in seen_roles]
    if missing or len(seen_roles) != len(EXPECTED_ROLES):
        raise GuardRejection(
            "automatic deployment requires exactly the web and worker task "
            "definitions and their ECS services; "
            f"found {seen_roles or ['none']}, missing {missing or ['none']}"
        )

    differences = _differences(current_template, proposed_template)
    image_diffs_by_role = {role: [] for role in TASK_ROLES}
    for difference in differences:
        role = _difference_role(difference.path, logical_by_role)
        if (
            role in TASK_ROLES
            and difference.kind == "value"
            and _is_expected_image_change(
                difference.old, difference.new, repository_name, parsed["tag"]
            )
        ):
            image_diffs_by_role[role].append(difference)
            continue
        raise GuardRejection(
            "template changes are not limited to the web and worker image tags: "
            + _format_differences([difference])
        )
    for role in TASK_ROLES:
        count = len(image_diffs_by_role[role])
        if count != 1:
            raise GuardRejection(
                f"{role} must change exactly one image tag string; found {count}"
            )
    return "deploy"


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardRejection(f"could not read JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardRejection(f"{path} is not a JSON object")
    return data


def load_template(path: Path) -> dict[str, Any]:
    """Accept a bare template or a get-template response."""
    data = load_json_object(path)
    if "TemplateBody" in data and "Resources" not in data:
        body = data["TemplateBody"]
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GuardRejection(
                    f"{path} TemplateBody is not JSON ({exc})"
                ) from exc
        if not isinstance(body, dict):
            raise GuardRejection(f"{path} TemplateBody is not a JSON object")
        data = body
    if not isinstance(data.get("Resources"), dict):
        raise GuardRejection(f"{path} template is missing a Resources object")
    return data


def load_change_set(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardRejection(f"could not read JSON from {path}: {exc}") from exc
    if isinstance(data, list):
        return merge_change_set_pages(data)
    if isinstance(data, dict):
        return data
    raise GuardRejection(f"{path} is not a change set description")


class _Difference:
    def __init__(self, path: str, kind: str, old: Any, new: Any) -> None:
        self.path = path
        self.kind = kind
        self.old = old
        self.new = new


def _parse_image_uri(image_uri: str, repository_name: str) -> dict[str, str]:
    match = IMAGE_URI_RE.fullmatch(image_uri)
    if match is None or match.group("repository") != repository_name:
        raise GuardRejection(
            "expected image URI must be "
            "{account}.dkr.ecr.{region}.amazonaws.com/"
            f"{repository_name}:{{tag}}"
        )
    return match.groupdict()


def _noop_reason(change_set: dict[str, Any]) -> str | None:
    status = change_set.get("Status")
    reason = change_set.get("StatusReason") or ""
    if not isinstance(reason, str):
        raise GuardRejection("change set StatusReason is not a string")
    if status != "FAILED":
        return None
    if not any(reason.startswith(prefix) for prefix in NO_CHANGE_REASON_PREFIXES):
        raise GuardRejection(
            "change set creation failed "
            f"({reason or 'no StatusReason was provided'})"
        )
    changes = change_set.get("Changes", [])
    if changes is None:
        changes = []
    if not isinstance(changes, list):
        raise GuardRejection("change set Changes is not a list")
    if changes:
        raise GuardRejection(
            "no-op change set status still lists resource changes"
        )
    return reason


def _require_deployable_change_set(change_set: dict[str, Any]) -> None:
    status = change_set.get("Status")
    execution = change_set.get("ExecutionStatus")
    if status != "CREATE_COMPLETE" or execution != "AVAILABLE":
        raise GuardRejection(
            "change set is not ready to execute "
            f"(Status={status!r}, ExecutionStatus={execution!r})"
        )
    if "Changes" not in change_set:
        raise GuardRejection("change set description is missing Changes")


def _require_live_image(
    template: dict[str, Any],
    stack_name: str,
    repository_name: str,
    tag: str,
) -> None:
    roles = _roles_by_logical_id(template, stack_name)
    logical_by_role = {role: logical_id for logical_id, role in roles.items()}
    suffix = f"/{repository_name}:{tag}"
    for role in TASK_ROLES:
        resource = template["Resources"][logical_by_role[role]]
        fragments = [
            value
            for value in _strings(resource)
            if value.endswith(suffix) and value[: -len(tag)].endswith(f"/{repository_name}:")
        ]
        if len(fragments) != 1:
            raise GuardRejection(
                f"no-op {role} does not contain exactly one expected image "
                f"reference ending in {suffix}"
            )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key in value:
            found.extend(_strings(value[key]))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_strings(item))
        return found
    return []


def _stack_prefix(stack_name: str) -> str:
    suffix = "-app-v2"
    if not stack_name.endswith(suffix) or stack_name == suffix:
        raise GuardRejection(
            f"stack name {stack_name!r} must end with {suffix} "
            "so web and worker resources can be identified"
        )
    return stack_name[: -len(suffix)]


def _expected_paths(stack_name: str) -> dict[str, str]:
    prefix = _stack_prefix(stack_name)
    return {
        f"{stack_name}/{prefix}-web-td/Resource": WEB_TASK,
        f"{stack_name}/{prefix}-worker-td/Resource": WORKER_TASK,
        f"{stack_name}/{prefix}-web-svc/Service": WEB_SERVICE,
        f"{stack_name}/{prefix}-worker-svc/Service": WORKER_SERVICE,
    }


def _roles_by_logical_id(template: dict[str, Any], stack_name: str) -> dict[str, str]:
    resources = template.get("Resources")
    if not isinstance(resources, dict) or not resources:
        raise GuardRejection("template is missing Resources")
    expected_paths = _expected_paths(stack_name)
    found: dict[str, str] = {}
    for logical_id, resource in resources.items():
        if not isinstance(logical_id, str) or not isinstance(resource, dict):
            raise GuardRejection("template Resources contains an invalid entry")
        metadata = resource.get("Metadata")
        if not isinstance(metadata, dict):
            continue
        path = metadata.get("aws:cdk:path")
        if not isinstance(path, str) or path not in expected_paths:
            continue
        role = expected_paths[path]
        if role in found.values():
            raise GuardRejection(f"template contains more than one {role} resource")
        resource_type = resource.get("Type")
        expected_type = (
            "AWS::ECS::TaskDefinition" if role in TASK_ROLES else "AWS::ECS::Service"
        )
        if resource_type != expected_type:
            raise GuardRejection(
                f"{role} logical id {logical_id} has type {resource_type!r}, "
                f"expected {expected_type}"
            )
        found[logical_id] = role
    missing = [role for role in EXPECTED_ROLES if role not in found.values()]
    if missing:
        raise GuardRejection(
            "template is missing CDK paths for " + ", ".join(missing)
        )
    return found


def _validate_resource_change(
    change: Any,
    roles: dict[str, str],
    logical_by_role: dict[str, str],
    *,
    repository_name: str,
    tag: str,
) -> str:
    if not isinstance(change, dict):
        raise GuardRejection("change set entry is not a JSON object")
    extra = set(change) - CHANGE_KEYS
    if extra or change.get("Type") != "Resource":
        raise GuardRejection(
            "change set entry is not a single resource change "
            f"(Type={change.get('Type')!r}, extra={sorted(extra)})"
        )
    if "HookInvocationCount" in change and change.get("HookInvocationCount") is not None:
        raise GuardRejection(
            "change set invokes CloudFormation Hooks "
            f"({change.get('HookInvocationCount')!r}); refusing an unreviewed hook"
        )
    resource_change = change.get("ResourceChange")
    if not isinstance(resource_change, dict):
        raise GuardRejection("ResourceChange is missing")
    unknown = set(resource_change) - RESOURCE_CHANGE_KEYS
    if unknown:
        raise GuardRejection(
            "resource change contains unrecognized fields " + ", ".join(sorted(unknown))
        )
    for key in NULL_ONLY_RESOURCE_KEYS:
        if key in resource_change and resource_change.get(key) is not None:
            raise GuardRejection(
                f"resource change includes {key}={resource_change.get(key)!r}"
            )
    logical_id = resource_change.get("LogicalResourceId")
    if not isinstance(logical_id, str) or logical_id not in roles:
        action = resource_change.get("Action")
        resource_type = resource_change.get("ResourceType")
        raise GuardRejection(
            "refusing "
            f"{action or 'unknown action'} of {resource_type or 'unknown type'} "
            f"logical id {logical_id or 'unknown'}. Automatic deployment only "
            "allows the web and worker ECS task definitions and their services."
        )
    role = roles[logical_id]
    action = resource_change.get("Action")
    if action != "Modify":
        raise GuardRejection(
            f"refusing {action!r} of {role} logical id {logical_id}"
        )
    physical_id = resource_change.get("PhysicalResourceId")
    if not isinstance(physical_id, str) or not physical_id:
        raise GuardRejection(
            f"{role} change is missing PhysicalResourceId"
        )
    if role in TASK_ROLES:
        _validate_task_change(
            role,
            resource_change,
            repository_name=repository_name,
            tag=tag,
        )
    else:
        _validate_service_change(role, resource_change, logical_by_role)
    return role


def _validate_task_change(
    role: str,
    resource_change: dict[str, Any],
    *,
    repository_name: str,
    tag: str,
) -> None:
    if resource_change.get("ResourceType") != "AWS::ECS::TaskDefinition":
        raise GuardRejection(f"{role} resource type is not AWS::ECS::TaskDefinition")
    if resource_change.get("Replacement") != "True":
        raise GuardRejection(
            f"{role} replacement must be True for an image update; "
            f"got {resource_change.get('Replacement')!r}"
        )
    policy_action = resource_change.get("PolicyAction")
    if policy_action is not None and policy_action not in TASK_POLICY_ACTIONS:
        raise GuardRejection(
            f"{role} replacement policy {policy_action!r} is not an expected "
            "task-definition replacement"
        )
    _require_scope(role, resource_change)
    details = _require_details(role, resource_change)
    for detail in details:
        target = _require_target(role, detail)
        if target.get("Attribute") != "Properties" or target.get("Name") != "ContainerDefinitions":
            raise GuardRejection(
                f"{role} change includes unexpected property "
                f"{target.get('Name')!r} (expected only ContainerDefinitions)"
            )
        if target.get("RequiresRecreation") != "Always":
            raise GuardRejection(
                f"{role} ContainerDefinitions replacement is "
                f"{target.get('RequiresRecreation')!r}, expected Always"
            )
        path = target.get("Path")
        if path is not None and (
            not isinstance(path, str) or TASK_IMAGE_PATH_RE.fullmatch(path) is None
        ):
            raise GuardRejection(
                f"{role} change path {path!r} is not a container image path"
            )
        if detail.get("Evaluation") != "Static" or detail.get("ChangeSource") != "DirectModification":
            raise GuardRejection(
                f"{role} change source is not a static direct container modification"
            )
        causing = detail.get("CausingEntity")
        if causing is not None:
            raise GuardRejection(
                f"{role} direct image modification has unexpected CausingEntity {causing!r}"
            )
        _validate_task_before_after(
            role,
            target,
            repository_name=repository_name,
            tag=tag,
        )


def _validate_service_change(
    role: str,
    resource_change: dict[str, Any],
    logical_by_role: dict[str, str],
) -> None:
    if resource_change.get("ResourceType") != "AWS::ECS::Service":
        raise GuardRejection(f"{role} resource type is not AWS::ECS::Service")
    if resource_change.get("Replacement") != "False":
        raise GuardRejection(
            f"{role} replacement must be False for a task-definition reference "
            f"update; got {resource_change.get('Replacement')!r}"
        )
    if resource_change.get("PolicyAction") is not None:
        raise GuardRejection(
            f"{role} includes replacement policy {resource_change.get('PolicyAction')!r}"
        )
    _require_scope(role, resource_change)
    details = _require_details(role, resource_change)
    task_logical_id = logical_by_role[SERVICE_TASK_ROLE[role]]
    for detail in details:
        target = _require_target(role, detail)
        if target.get("Attribute") != "Properties" or target.get("Name") != "TaskDefinition":
            raise GuardRejection(
                f"{role} change includes unexpected property "
                f"{target.get('Name')!r} (expected only TaskDefinition)"
            )
        if target.get("RequiresRecreation") != "Never":
            raise GuardRejection(
                f"{role} TaskDefinition update would recreate the service "
                f"({target.get('RequiresRecreation')!r})"
            )
        if "Path" in target and target.get("Path") is not None:
            raise GuardRejection(f"{role} task-definition reference has an unexpected path")
        for key in ("BeforeValue", "AfterValue", "Drift"):
            if target.get(key) is not None:
                raise GuardRejection(
                    f"{role} task-definition reference includes ambiguous {key}"
                )
        evaluation = detail.get("Evaluation")
        if (
            evaluation not in SERVICE_REFERENCE_EVALUATIONS
            or detail.get("ChangeSource") != "ResourceReference"
            or detail.get("CausingEntity") != task_logical_id
        ):
            raise GuardRejection(
                f"{role} change is not a task-definition reference caused by "
                f"{task_logical_id} (Evaluation={evaluation!r}, "
                f"ChangeSource={detail.get('ChangeSource')!r}, "
                f"CausingEntity={detail.get('CausingEntity')!r})"
            )


def _validate_task_before_after(
    role: str,
    target: dict[str, Any],
    *,
    repository_name: str,
    tag: str,
) -> None:
    if target.get("Drift") is not None:
        raise GuardRejection(f"{role} change includes drift details")
    before_from = target.get("BeforeValueFrom")
    after_from = target.get("AfterValueFrom")
    if before_from not in (None, "PREVIOUS_DEPLOYMENT_STATE"):
        raise GuardRejection(f"{role} before value comes from {before_from!r}")
    if after_from not in (None, "TEMPLATE"):
        raise GuardRejection(f"{role} after value comes from {after_from!r}")
    has_before = "BeforeValue" in target and target.get("BeforeValue") is not None
    has_after = "AfterValue" in target and target.get("AfterValue") is not None
    if not has_before and not has_after:
        return
    if not has_before or not has_after:
        raise GuardRejection(f"{role} before/after image values are incomplete")
    before = target.get("BeforeValue")
    after = target.get("AfterValue")
    if not isinstance(before, str) or not isinstance(after, str) or not before or not after:
        raise GuardRejection(f"{role} before/after image values are incomplete")
    path = target.get("Path")
    if isinstance(path, str) and path.endswith("/Image"):
        if not _is_expected_image_change(before, after, repository_name, tag):
            raise GuardRejection(
                f"{role} before/after values are not the expected image tag change"
            )
        return
    try:
        before_value = json.loads(before)
        after_value = json.loads(after)
    except json.JSONDecodeError as exc:
        raise GuardRejection(
            f"{role} ContainerDefinitions before/after values are not complete JSON"
        ) from exc
    differences = _differences(before_value, after_value)
    image_differences = [
        difference
        for difference in differences
        if difference.kind == "value"
        and _is_expected_image_change(
            difference.old, difference.new, repository_name, tag
        )
    ]
    if len(image_differences) != 1 or len(image_differences) != len(differences):
        raise GuardRejection(
            f"{role} before/after ContainerDefinitions are not an image-only change: "
            + _format_differences(differences)
        )


def _require_scope(role: str, resource_change: dict[str, Any]) -> None:
    scope = resource_change.get("Scope")
    if scope != ["Properties"]:
        raise GuardRejection(
            f"{role} change scope must be exactly Properties; got {scope!r}"
        )


def _require_details(role: str, resource_change: dict[str, Any]) -> list[dict[str, Any]]:
    details = resource_change.get("Details")
    if not isinstance(details, list) or not details:
        raise GuardRejection(f"{role} change is missing property details")
    for detail in details:
        if not isinstance(detail, dict):
            raise GuardRejection(f"{role} change detail is not a JSON object")
        unknown = set(detail) - DETAIL_KEYS
        if unknown:
            raise GuardRejection(
                f"{role} change detail contains unrecognized fields "
                + ", ".join(sorted(unknown))
            )
    return details


def _require_target(role: str, detail: dict[str, Any]) -> dict[str, Any]:
    target = detail.get("Target")
    if not isinstance(target, dict):
        raise GuardRejection(f"{role} change detail is missing Target")
    unknown = set(target) - TARGET_KEYS
    if unknown:
        raise GuardRejection(
            f"{role} change target contains unrecognized fields "
            + ", ".join(sorted(unknown))
        )
    return target


def _differences(old: Any, new: Any, path: str = "") -> list[_Difference]:
    if type(old) is not type(new):
        return [_Difference(path or "$", "type", old, new)]
    if isinstance(old, dict):
        differences: list[_Difference] = []
        old_keys = set(old)
        new_keys = set(new)
        if old_keys != new_keys:
            differences.append(
                _Difference(path or "$", "keys", sorted(old_keys - new_keys), sorted(new_keys - old_keys))
            )
        for key in sorted(old_keys & new_keys):
            child = f"{path}.{key}" if path else str(key)
            differences.extend(_differences(old[key], new[key], child))
        return differences
    if isinstance(old, list):
        if len(old) != len(new):
            return [_Difference(path or "$", "length", len(old), len(new))]
        differences = []
        for index, (old_item, new_item) in enumerate(zip(old, new)):
            differences.extend(_differences(old_item, new_item, f"{path}[{index}]"))
        return differences
    if old != new:
        return [_Difference(path or "$", "value", old, new)]
    return []


def _difference_role(path: str, logical_by_role: dict[str, str]) -> str | None:
    prefix = "Resources."
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix) :]
    logical_id = remainder.split(".", 1)[0]
    for role, candidate in logical_by_role.items():
        if candidate == logical_id:
            return role
    return None


def _is_expected_image_change(
    old: Any,
    new: Any,
    repository_name: str,
    tag: str,
) -> bool:
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    suffix = f"/{repository_name}:{tag}"
    if not new.endswith(suffix):
        return False
    prefix = new[: -len(tag)]
    if not prefix.endswith(f"/{repository_name}:"):
        return False
    if not old.startswith(prefix):
        return False
    old_tag = old[len(prefix) :]
    return ECR_TAG_RE.fullmatch(old_tag) is not None


def _format_differences(differences: list[_Difference]) -> str:
    shown = differences[:8]
    parts = []
    for difference in shown:
        if difference.kind == "value":
            parts.append(
                f"{difference.path or '$'} changed from {difference.old!r} to {difference.new!r}"
            )
        elif difference.kind == "keys":
            parts.append(
                f"{difference.path or '$'} keys removed {difference.old!r} added {difference.new!r}"
            )
        elif difference.kind == "length":
            parts.append(
                f"{difference.path or '$'} length changed from {difference.old} to {difference.new}"
            )
        else:
            parts.append(f"{difference.path or '$'} type changed")
    remaining = len(differences) - len(shown)
    if remaining:
        parts.append(f"{remaining} more differences")
    return "; ".join(parts)


def _read_pages(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise GuardRejection(f"change set page directory does not exist: {directory}")
    files = sorted(directory.glob("*.json"), key=lambda item: item.name)
    if not files:
        raise GuardRejection(f"change set page directory is empty: {directory}")
    pages = []
    expected = 0
    for path in files:
        if path.stem != str(expected):
            raise GuardRejection(
                f"change set pages must be numbered 0.json, 1.json, ...; found {path.name}"
            )
        pages.append(load_json_object(path))
        expected += 1
    return pages


def _emit_decision(decision: str) -> int:
    if decision not in {"deploy", "noop"}:
        raise GuardRejection(f"unrecognized decision {decision!r}")
    print(f"decision={decision}")
    return 0


def _command_validate_app(args: argparse.Namespace) -> int:
    proposed = (
        load_template(Path(args.proposed_template))
        if args.proposed_template
        else None
    )
    decision = validate_app_change(
        change_set=load_change_set(Path(args.change_set)),
        current_template=load_template(Path(args.current_template)),
        proposed_template=proposed,
        image_uri=args.image_uri,
        repository_name=args.repository_name,
        stack_name=args.stack_name,
    )
    return _emit_decision(decision)


def _command_validate_noop(args: argparse.Namespace) -> int:
    validate_dependency_change_set(
        load_change_set(Path(args.change_set)),
        stack_name=args.stack_name,
    )
    print("decision=noop")
    return 0


def _command_merge_pages(args: argparse.Namespace) -> int:
    merged = merge_change_set_pages(_read_pages(Path(args.page_dir)))
    json.dump(merged, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a VS-Archive app deployment change set."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    app = subparsers.add_parser("validate-app")
    app.add_argument("--change-set", required=True)
    app.add_argument("--current-template", required=True)
    app.add_argument("--proposed-template")
    app.add_argument("--image-uri", required=True)
    app.add_argument("--repository-name", required=True)
    app.add_argument("--stack-name", required=True)
    app.set_defaults(func=_command_validate_app)

    noop = subparsers.add_parser("validate-noop")
    noop.add_argument("--change-set", required=True)
    noop.add_argument("--stack-name", required=True)
    noop.set_defaults(func=_command_validate_noop)

    merge = subparsers.add_parser("merge-pages")
    merge.add_argument("page_dir")
    merge.set_defaults(func=_command_merge_pages)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except GuardRejection as exc:
        print(
            f"Infrastructure change guard rejected this deployment: {exc.reason}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
