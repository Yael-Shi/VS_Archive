#!/usr/bin/env python3
"""Focused tests for the app deployment change-set guard."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import deploy_change_guard as guard  # noqa: E402


STACK = "vs-archive-dev-app-v2"
REPO = "vs-archive-web"
OLD_TAG = "main-aaaaaaaaaaaa-20260101010101"
NEW_TAG = "main-bbbbbbbbbbbb-20260202020202"
IMAGE_URI = (
    f"185990503355.dkr.ecr.eu-central-1.amazonaws.com/{REPO}:{NEW_TAG}"
)
WEB_TASK_ID = "WebTaskDefinition"
WORKER_TASK_ID = "WorkerTaskDefinition"
WEB_SERVICE_ID = "WebService"
WORKER_SERVICE_ID = "WorkerService"


def _image(tag: str) -> dict:
    return {
        "Fn::Join": [
            "",
            [
                {"Ref": "AWS::AccountId"},
                ".dkr.ecr.",
                {"Ref": "AWS::Region"},
                f".amazonaws.com/{REPO}:{tag}",
            ],
        ]
    }


def _task(role: str, tag: str, *, environment: str = "0") -> dict:
    name = "web" if role == guard.WEB_TASK else "worker"
    path = (
        f"{STACK}/vs-archive-dev-web-td/Resource"
        if role == guard.WEB_TASK
        else f"{STACK}/vs-archive-dev-worker-td/Resource"
    )
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Metadata": {"aws:cdk:path": path},
        "Properties": {
            "Family": name,
            "Cpu": "256",
            "Memory": "512",
            "ContainerDefinitions": [
                {
                    "Name": name,
                    "Essential": True,
                    "Image": _image(tag),
                    "Environment": [{"Name": "DJANGO_DEBUG", "Value": environment}],
                }
            ],
        },
    }


def _service(role: str, task_id: str, *, desired: int = 1) -> dict:
    path = (
        f"{STACK}/vs-archive-dev-web-svc/Service"
        if role == guard.WEB_SERVICE
        else f"{STACK}/vs-archive-dev-worker-svc/Service"
    )
    return {
        "Type": "AWS::ECS::Service",
        "Metadata": {"aws:cdk:path": path},
        "Properties": {
            "DesiredCount": desired,
            "TaskDefinition": {"Ref": task_id},
        },
    }


def _template(tag: str, *, web_environment: str = "0") -> dict:
    return {
        "Resources": {
            WEB_TASK_ID: _task(guard.WEB_TASK, tag, environment=web_environment),
            WORKER_TASK_ID: _task(guard.WORKER_TASK, tag),
            WEB_SERVICE_ID: _service(guard.WEB_SERVICE, WEB_TASK_ID),
            WORKER_SERVICE_ID: _service(guard.WORKER_SERVICE, WORKER_TASK_ID),
            "OtherRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {"RoleName": "unchanged"},
            },
        }
    }


def _detail(name: str, *, recreation: str, evaluation: str, source: str, causing: str | None = None, path: str | None = None) -> dict:
    target = {
        "Attribute": "Properties",
        "Name": name,
        "RequiresRecreation": recreation,
    }
    if path is not None:
        target["Path"] = path
    detail = {
        "Target": target,
        "Evaluation": evaluation,
        "ChangeSource": source,
    }
    if causing is not None:
        detail["CausingEntity"] = causing
    return detail


def _task_change(logical_id: str) -> dict:
    return {
        "Type": "Resource",
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": f"arn:aws:ecs:eu-central-1:185990503355:task-definition/{logical_id}:1",
            "ResourceType": "AWS::ECS::TaskDefinition",
            "Replacement": "True",
            "Scope": ["Properties"],
            "Details": [
                _detail(
                    "ContainerDefinitions",
                    recreation="Always",
                    evaluation="Static",
                    source="DirectModification",
                    path="/ContainerDefinitions/0/Image",
                )
            ],
        },
    }


def _service_change(logical_id: str, task_id: str) -> dict:
    return {
        "Type": "Resource",
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": f"arn:aws:ecs:eu-central-1:185990503355:service/{logical_id}",
            "ResourceType": "AWS::ECS::Service",
            "Replacement": "False",
            "Scope": ["Properties"],
            "Details": [
                _detail(
                    "TaskDefinition",
                    recreation="Never",
                    evaluation="Dynamic",
                    source="ResourceReference",
                    causing=task_id,
                )
            ],
        },
    }


def _deploy_change_set(extra: list[dict] | None = None) -> dict:
    changes = [
        _task_change(WEB_TASK_ID),
        _task_change(WORKER_TASK_ID),
        _service_change(WEB_SERVICE_ID, WEB_TASK_ID),
        _service_change(WORKER_SERVICE_ID, WORKER_TASK_ID),
    ]
    if extra:
        changes.extend(extra)
    return {
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "AVAILABLE",
        "StatusReason": "The change set is ready.",
        "Changes": changes,
    }


def _noop_change_set() -> dict:
    return {
        "Status": "FAILED",
        "ExecutionStatus": "UNAVAILABLE",
        "StatusReason": (
            "The submitted information didn't contain changes. "
            "Submit different information to create a change set."
        ),
        "Changes": [],
    }


def _validate(change_set, current, proposed):
    return guard.validate_app_change(
        change_set=change_set,
        current_template=current,
        proposed_template=proposed,
        image_uri=IMAGE_URI,
        repository_name=REPO,
        stack_name=STACK,
    )


class DeployChangeGuardTests(unittest.TestCase):
    def test_expected_web_and_worker_image_changes_are_allowed(self) -> None:
        decision = _validate(
            _deploy_change_set(),
            _template(OLD_TAG),
            _template(NEW_TAG),
        )
        self.assertEqual(decision, "deploy")

    def test_literal_image_uri_change_is_allowed(self) -> None:
        current = _template(OLD_TAG)
        proposed = _template(NEW_TAG)
        for template, tag in ((current, OLD_TAG), (proposed, NEW_TAG)):
            for logical_id in (WEB_TASK_ID, WORKER_TASK_ID):
                template["Resources"][logical_id]["Properties"]["ContainerDefinitions"][0][
                    "Image"
                ] = f"185990503355.dkr.ecr.eu-central-1.amazonaws.com/{REPO}:{tag}"
        self.assertEqual(_validate(_deploy_change_set(), current, proposed), "deploy")

    def test_unexpected_task_property_change_is_rejected(self) -> None:
        proposed = _template(NEW_TAG, web_environment="1")
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(_deploy_change_set(), _template(OLD_TAG), proposed)
        self.assertIn("Environment", raised.exception.reason)

    def test_unexpected_change_set_property_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        change_set["Changes"][0]["ResourceChange"]["Details"].append(
            _detail(
                "Memory",
                recreation="Conditionally",
                evaluation="Static",
                source="DirectModification",
            )
        )
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("Memory", raised.exception.reason)

    def test_iam_change_is_rejected(self) -> None:
        change_set = _deploy_change_set(
            [
                {
                    "Type": "Resource",
                    "ResourceChange": {
                        "Action": "Modify",
                        "LogicalResourceId": "TaskRole",
                        "PhysicalResourceId": "arn:aws:iam::185990503355:role/task",
                        "ResourceType": "AWS::IAM::Role",
                        "Replacement": "False",
                        "Scope": ["Properties"],
                        "Details": [
                            _detail(
                                "Policies",
                                recreation="Never",
                                evaluation="Static",
                                source="DirectModification",
                            )
                        ],
                    },
                }
            ]
        )
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("AWS::IAM::Role", raised.exception.reason)

    def test_resource_addition_is_rejected(self) -> None:
        change_set = _deploy_change_set(
            [
                {
                    "Type": "Resource",
                    "ResourceChange": {
                        "Action": "Add",
                        "LogicalResourceId": "ExtraBucket",
                        "ResourceType": "AWS::S3::Bucket",
                        "Scope": [],
                        "Details": [],
                    },
                }
            ]
        )
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("Add", raised.exception.reason)
        self.assertIn("ExtraBucket", raised.exception.reason)

    def test_resource_deletion_is_rejected(self) -> None:
        change_set = _deploy_change_set(
            [
                {
                    "Type": "Resource",
                    "ResourceChange": {
                        "Action": "Remove",
                        "LogicalResourceId": "OldLogGroup",
                        "PhysicalResourceId": "arn:aws:logs:eu-central-1:185990503355:log-group:old",
                        "ResourceType": "AWS::Logs::LogGroup",
                        "Scope": [],
                        "Details": [],
                    },
                }
            ]
        )
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("Remove", raised.exception.reason)

    def test_unknown_change_detail_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        change_set["Changes"][0]["ResourceChange"]["Details"][0]["ChangeSource"] = "Automatic"
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("static direct container modification", raised.exception.reason)

    def test_incomplete_change_set_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        del change_set["Changes"]
        change_set["NextToken"] = "page-2"
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("incomplete", raised.exception.reason)

    def test_missing_resource_path_is_rejected(self) -> None:
        proposed = _template(NEW_TAG)
        del proposed["Resources"][WORKER_TASK_ID]["Metadata"]
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(_deploy_change_set(), _template(OLD_TAG), proposed)
        self.assertIn("worker-task", raised.exception.reason)

    def test_service_replacement_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        change_set["Changes"][2]["ResourceChange"]["Replacement"] = "True"
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("replacement must be False", raised.exception.reason)

    def test_missing_worker_change_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        change_set["Changes"] = [
            change
            for change in change_set["Changes"]
            if change["ResourceChange"]["LogicalResourceId"]
            not in {WORKER_TASK_ID, WORKER_SERVICE_ID}
        ]
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("worker-task", raised.exception.reason)

    def test_noop_change_set_is_allowed_when_template_already_has_image(self) -> None:
        decision = _validate(_noop_change_set(), _template(NEW_TAG), None)
        self.assertEqual(decision, "noop")

    def test_noop_with_template_difference_is_rejected(self) -> None:
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(_noop_change_set(), _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("no-op", raised.exception.reason)

    def test_noop_without_expected_image_is_rejected(self) -> None:
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(_noop_change_set(), _template(OLD_TAG), None)
        self.assertIn("expected image", raised.exception.reason)

    def test_empty_create_complete_change_set_is_rejected(self) -> None:
        change_set = {
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "Changes": [],
        }
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(NEW_TAG), _template(NEW_TAG))
        self.assertIn("no resource changes", raised.exception.reason)

    def test_dependency_noop_is_allowed_and_changes_are_rejected(self) -> None:
        guard.validate_dependency_change_set(
            _noop_change_set(),
            stack_name="vs-archive-dev-data-v2",
        )
        with self.assertRaises(guard.GuardRejection) as raised:
            guard.validate_dependency_change_set(
                _deploy_change_set(),
                stack_name="vs-archive-dev-data-v2",
            )
        self.assertIn("vs-archive-dev-data-v2", raised.exception.reason)

    def test_failed_change_set_without_no_change_reason_is_rejected(self) -> None:
        change_set = _noop_change_set()
        change_set["StatusReason"] = "Role arn is invalid"
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(NEW_TAG), None)
        self.assertIn("Role arn is invalid", raised.exception.reason)

    def test_cli_rejects_iam_change_and_accepts_image_change(self) -> None:
        script = SCRIPTS_DIR / "deploy_change_guard.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current.json"
            proposed = root / "proposed.json"
            allowed = root / "allowed.json"
            rejected = root / "rejected.json"
            current.write_text(json.dumps(_template(OLD_TAG)), encoding="utf-8")
            proposed.write_text(json.dumps(_template(NEW_TAG)), encoding="utf-8")
            allowed.write_text(json.dumps(_deploy_change_set()), encoding="utf-8")
            iam = _deploy_change_set(
                [
                    {
                        "Type": "Resource",
                        "ResourceChange": {
                            "Action": "Add",
                            "LogicalResourceId": "NewPolicy",
                            "ResourceType": "AWS::IAM::Policy",
                        },
                    }
                ]
            )
            rejected.write_text(json.dumps(iam), encoding="utf-8")
            common = [
                sys.executable,
                str(script),
                "validate-app",
                "--current-template",
                str(current),
                "--proposed-template",
                str(proposed),
                "--image-uri",
                IMAGE_URI,
                "--repository-name",
                REPO,
                "--stack-name",
                STACK,
            ]
            ok = subprocess.run(
                [*common, "--change-set", str(allowed)],
                check=False,
                capture_output=True,
                text=True,
            )
            bad = subprocess.run(
                [*common, "--change-set", str(rejected)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.strip(), "decision=deploy")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("Infrastructure change guard rejected", bad.stderr)
        self.assertIn("AWS::IAM::Policy", bad.stderr)
        self.assertNotIn("decision=deploy", bad.stdout)

    def test_task_replacement_policy_is_allowed_and_delete_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        for change in change_set["Changes"][:2]:
            change["ResourceChange"]["PolicyAction"] = "ReplaceAndDelete"
        self.assertEqual(
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG)),
            "deploy",
        )
        change_set["Changes"][0]["ResourceChange"]["PolicyAction"] = "Delete"
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("Delete", raised.exception.reason)

    def test_before_and_after_values_must_be_image_only(self) -> None:
        change_set = _deploy_change_set()
        target = change_set["Changes"][0]["ResourceChange"]["Details"][0]["Target"]
        target.pop("Path", None)
        before = [{"Name": "web", "Image": f"185990503355.dkr.ecr.eu-central-1.amazonaws.com/{REPO}:{OLD_TAG}"}]
        after = [{"Name": "web", "Image": f"185990503355.dkr.ecr.eu-central-1.amazonaws.com/{REPO}:{NEW_TAG}"}]
        target["BeforeValue"] = json.dumps(before)
        target["AfterValue"] = json.dumps(after)
        self.assertEqual(
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG)),
            "deploy",
        )
        after[0]["Environment"] = [{"Name": "DJANGO_DEBUG", "Value": "1"}]
        target["AfterValue"] = json.dumps(after)
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("before/after", raised.exception.reason)

    def test_truncated_before_after_values_are_rejected(self) -> None:
        change_set = _deploy_change_set()
        target = change_set["Changes"][0]["ResourceChange"]["Details"][0]["Target"]
        target.pop("Path", None)
        target["BeforeValue"] = '[{"Name": "web", "Image":'
        target["AfterValue"] = '[{"Name": "web", "Image":'
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("not complete JSON", raised.exception.reason)

    def test_hook_invocation_is_rejected(self) -> None:
        change_set = _deploy_change_set()
        change_set["Changes"][0]["HookInvocationCount"] = 1
        with self.assertRaises(guard.GuardRejection) as raised:
            _validate(change_set, _template(OLD_TAG), _template(NEW_TAG))
        self.assertIn("Hooks", raised.exception.reason)

    def test_merge_pages_fails_closed_when_incomplete(self) -> None:
        with self.assertRaises(guard.GuardRejection):
            guard.merge_change_set_pages(
                [
                    {"Status": "CREATE_COMPLETE", "Changes": [], "NextToken": "more"},
                ]
            )


if __name__ == "__main__":
    unittest.main()
