from __future__ import annotations

import unittest

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from vs_archive_infra.config import EnvConfig
from vs_archive_infra.data_stack import (
    JOBS_QUEUE_MAX_RECEIVE_COUNT,
    VsArchiveDataStack,
)
from vs_archive_infra.network_stack import VsArchiveNetworkStack


class JobsQueueDlqPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = cdk.App()
        cfg = EnvConfig(project="vs-archive", env_name="test", region="eu-central-1")
        env = cdk.Environment(account="123456789012", region=cfg.region)
        network = VsArchiveNetworkStack(app, "test-network", cfg=cfg, env=env)
        data = VsArchiveDataStack(
            app,
            "test-data",
            cfg=cfg,
            vpc=network.vpc,
            sg_efs=network.sg_efs,
            env=env,
        )
        cls.template = Template.from_stack(data)
        cls.resources = cls.template.to_json()["Resources"]

    def _sqs_queues(self) -> dict[str, dict]:
        return {
            logical_id: resource
            for logical_id, resource in self.resources.items()
            if resource["Type"] == "AWS::SQS::Queue"
        }

    def _jobs_and_dlq(self) -> tuple[str, dict, str, dict]:
        queues = self._sqs_queues()
        jobs_id = None
        jobs = None
        for logical_id, resource in queues.items():
            if "RedrivePolicy" in resource.get("Properties", {}):
                jobs_id, jobs = logical_id, resource
                break
        self.assertIsNotNone(jobs, "jobs queue with RedrivePolicy not found")
        assert jobs is not None and jobs_id is not None
        dlq_target = jobs["Properties"]["RedrivePolicy"]["deadLetterTargetArn"]
        dlq_id = dlq_target["Fn::GetAtt"][0]
        self.assertIn(dlq_id, queues)
        return jobs_id, jobs, dlq_id, queues[dlq_id]

    def test_max_receive_count_constant(self) -> None:
        self.assertEqual(JOBS_QUEUE_MAX_RECEIVE_COUNT, 100)

    def test_exactly_two_sqs_queues(self) -> None:
        self.assertEqual(len(self._sqs_queues()), 2)

    def test_jobs_queue_keeps_existing_dlq_and_safety_net_receive_count(self) -> None:
        _jobs_id, jobs, _dlq_id, dlq = self._jobs_and_dlq()
        redrive = jobs["Properties"]["RedrivePolicy"]
        self.assertEqual(redrive["maxReceiveCount"], 100)
        self.assertEqual(
            jobs["Properties"]["MessageRetentionPeriod"],
            cdk.Duration.days(4).to_seconds(),
        )
        self.assertEqual(
            jobs["Properties"]["VisibilityTimeout"],
            cdk.Duration.minutes(10).to_seconds(),
        )
        self.assertEqual(
            dlq["Properties"]["MessageRetentionPeriod"],
            cdk.Duration.days(14).to_seconds(),
        )
        self.assertNotIn("RedrivePolicy", dlq.get("Properties", {}))

    def test_data_stack_has_no_iam_resources(self) -> None:
        iam_ids = [
            logical_id
            for logical_id, resource in self.resources.items()
            if resource["Type"].startswith("AWS::IAM::")
        ]
        self.assertEqual(
            iam_ids,
            [],
            "jobs DLQ receive-count policy must not require IAM changes",
        )

    def test_redrive_policy_shape(self) -> None:
        self.template.has_resource_properties(
            "AWS::SQS::Queue",
            {
                "VisibilityTimeout": 600,
                "MessageRetentionPeriod": 345600,
                "RedrivePolicy": {
                    "maxReceiveCount": 100,
                    "deadLetterTargetArn": Match.any_value(),
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
