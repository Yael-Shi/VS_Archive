from aws_cdk import Stack, RemovalPolicy, Duration
from constructs import Construct
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_efs as efs
from aws_cdk import aws_ec2 as ec2
from typing import cast
from .config import EnvConfig


class VsArchiveDataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        sg_efs: ec2.ISecurityGroup,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        self.bucket = s3.Bucket(
            self,
            f"{cfg.prefix}-bucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    enabled=True,
                    noncurrent_version_expiration=Duration.days(14),
                    expiration=Duration.days(90),
                )
            ],
            cors=[
                s3.CorsRule(
                    allowed_methods=[
                        s3.HttpMethods.PUT,
                        s3.HttpMethods.POST,
                        s3.HttpMethods.GET,
                        s3.HttpMethods.HEAD,
                    ],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                )
            ],
        )

        self.file_system = efs.FileSystem(
            self,
            f"{cfg.prefix}-efs",
            vpc=vpc,
            security_group=sg_efs,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            out_of_infrequent_access_policy=efs.OutOfInfrequentAccessPolicy.AFTER_1_ACCESS,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
        )

        self.jobs_queue = sqs.Queue(
            self,
            f"{cfg.prefix}-jobs",
            visibility_timeout=Duration.minutes(10),
            retention_period=Duration.days(4),
        )

        self.db_secret = cast(
            secretsmanager.ISecret,
            secretsmanager.Secret(
                self,
                f"{cfg.prefix}-pg-secret",
                removal_policy=RemovalPolicy.RETAIN,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template='{"username":"vsarchive","dbname":"vsarchive"}',
                    generate_string_key="password",
                ),
            ),
        )
