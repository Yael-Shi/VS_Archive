from aws_cdk import Stack, RemovalPolicy, Duration
from constructs import Construct
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_secretsmanager as secretsmanager
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
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=["*"],  # tighten later
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
        )

        self.jobs_queue = sqs.Queue(
            self,
            f"{cfg.prefix}-jobs",
            queue_name=f"{cfg.prefix}-jobs",
            visibility_timeout=Duration.minutes(10),
            retention_period=Duration.days(4),
        )

        # Secret for Postgres (used by ECS Postgres container + Django)
        # Contains fields: username, password, dbname
        self.db_secret = secretsmanager.Secret(
            self,
            f"{cfg.prefix}-pg-secret",
            secret_name=f"{cfg.prefix}/postgres",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username":"vsarchive","dbname":"vsarchive"}',
                generate_string_key="password",
                exclude_punctuation=True,
            ),
        )
