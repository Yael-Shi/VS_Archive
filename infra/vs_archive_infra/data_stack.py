from aws_cdk import Stack, RemovalPolicy, Duration
from constructs import Construct
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_secretsmanager as secretsmanager
from typing import cast
from .config import EnvConfig


class VsArchiveDataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, cfg: EnvConfig, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.bucket = s3.Bucket(
            self,
            f"{cfg.prefix}-bucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY if cfg.env_name == "dev" else RemovalPolicy.RETAIN,
            auto_delete_objects=True if cfg.env_name == "dev" else False,
            # Cost saving: S3 Lifecycle rules
            lifecycle_rules=[
                s3.LifecycleRule(
                    enabled=True,
                    noncurrent_version_expiration=Duration.days(14), # Bi-weekly cleanup
                    expiration=Duration.days(90) # Quarterly cleanup
                )
            ],
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                )
            ],
        )

        self.jobs_queue = sqs.Queue(
            self, f"{cfg.prefix}-jobs",
            visibility_timeout=Duration.minutes(10),
            retention_period=Duration.days(4),
        )

        self.db_secret = cast(
            secretsmanager.ISecret,
            secretsmanager.Secret(
                self, f"{cfg.prefix}-pg-secret",
                removal_policy=RemovalPolicy.DESTROY if cfg.env_name == "dev" else RemovalPolicy.RETAIN,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template='{"username":"vsarchive","dbname":"vsarchive"}',
                    generate_string_key="password",
                ),
            ),
        )
