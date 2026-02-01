from aws_cdk import aws_ecr as ecr
from aws_cdk import Stack, Duration
from constructs import Construct
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_secretsmanager as secretsmanager
from typing import cast
from .config import EnvConfig


class VsArchiveAppStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: EnvConfig,
        vpc: ec2.Vpc,
        sg_alb: ec2.SecurityGroup,
        sg_web: ec2.SecurityGroup,
        bucket: s3.Bucket,
        queue: sqs.Queue,
        db_secret: secretsmanager.ISecret,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.Cluster(
            self,
            f"{cfg.prefix}-cluster",
            vpc=vpc,
            cluster_name=f"{cfg.prefix}-cluster",
        )

        # # Private DNS namespace for service discovery inside VPC
        # namespace = servicediscovery.PrivateDnsNamespace(
        #     self,
        #     f"{cfg.prefix}-ns",
        #     name=f"{cfg.prefix}.local",
        #     vpc=vpc,
        # )

        # === Postgres (DEV) as ECS Service ===
        pg_task = ecs.FargateTaskDefinition(
            self,
            f"{cfg.prefix}-pg-td",
            cpu=256,
            memory_limit_mib=512,
        )

        # Keep DB/USER fixed for now; password from Secrets Manager.
        # (Using secret_value_from_json().unsafe_unwrap() in env can be problematic; avoid.)
        pg_container = pg_task.add_container(
            f"{cfg.prefix}-pg",
            image=ecs.ContainerImage.from_registry("postgres:16-alpine"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix=f"{cfg.prefix}-pg"),
            environment={
                "POSTGRES_DB": "vsarchive",
                "POSTGRES_USER": "vsarchive",
            },
            secrets={
                "POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_secret, field="password"
                ),
            },
        )
        pg_container.add_port_mappings(ecs.PortMapping(container_port=5432))

        # pg_svc = ecs.FargateService(
        #     self,
        #     f"{cfg.prefix}-pg-svc",
        #     cluster=cluster,
        #     task_definition=pg_task,
        #     desired_count=1,
        #     security_groups=[sg_pg],  # <-- important: not sg_db
        #     assign_public_ip=False,
        #     cloud_map_options=ecs.CloudMapOptions(
        #         name="postgres",
        #         cloud_map_namespace=namespace,
        #     ),
        # )

        # === ALB ===
        alb = elbv2.ApplicationLoadBalancer(
            self,
            f"{cfg.prefix}-alb",
            vpc=vpc,
            internet_facing=True,
            security_group=sg_alb,
        )
        listener = alb.add_listener(f"{cfg.prefix}-http", port=80, open=True)

        # IAM roles for ECS tasks (web/worker)
        assumed_by = cast(
            iam.IPrincipal, iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )

        task_role = cast(
            iam.IRole,
            iam.Role(
                self,
                f"{cfg.prefix}-task-role",
                assumed_by=assumed_by,
            ),
        )
        bucket.grant_read_write(task_role)
        queue.grant_consume_messages(task_role)
        db_secret.grant_read(task_role)

        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "translate:TranslateText"],
                resources=["*"],
            )
        )

        exec_role = cast(
            iam.IRole,
            iam.Role(
                self,
                f"{cfg.prefix}-exec-role",
                assumed_by=assumed_by,
            ),
        )
        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy"
            )
        )

        db_host = f"postgres.{cfg.prefix}.local"

        # === Web (placeholder) ===
        web_task = ecs.FargateTaskDefinition(
            self,
            f"{cfg.prefix}-web-td",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=exec_role,
        )

        web_repo = ecr.Repository.from_repository_name(
            self,
            f"{cfg.prefix}-web-repo",
            "vs-archive-web",
        )

        web_container = web_task.add_container(
            f"{cfg.prefix}-web",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag="dev"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix=f"{cfg.prefix}-web"),
            environment={
                "UPLOADS_BUCKET_NAME": bucket.bucket_name,
                "SQS_QUEUE_URL": queue.queue_url,
                "AWS_REGION": cfg.region,
                "DB_HOST": db_host,
                "DB_PORT": "5432",
                "DB_NAME": "vsarchive",
                "DB_USER": "vsarchive",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_secret, field="password"
                ),
            },
        )
        web_container.add_port_mappings(ecs.PortMapping(container_port=8000))

        web_svc = ecs.FargateService(
            self,
            f"{cfg.prefix}-web-svc",
            cluster=cluster,
            task_definition=web_task,
            desired_count=1,
            security_groups=[sg_web],
            assign_public_ip=False,
        )

        listener.add_targets(
            f"{cfg.prefix}-tg",
            port=8000,
            targets=[web_svc],
            health_check=elbv2.HealthCheck(
                path="/health/", interval=Duration.seconds(30)
            ),
        )

        # === Worker (placeholder) ===
        worker_task = ecs.FargateTaskDefinition(
            self,
            f"{cfg.prefix}-worker-td",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=exec_role,
        )

        worker_task.add_container(
            f"{cfg.prefix}-worker",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag="dev"),
            command=["bash", "-lc", "python manage.py run_worker"],
            logging=ecs.LogDrivers.aws_logs(stream_prefix=f"{cfg.prefix}-worker"),
            environment={
                "SQS_QUEUE_URL": queue.queue_url,
                "UPLOADS_BUCKET_NAME": bucket.bucket_name,
                "AWS_REGION": cfg.region,
                "DB_HOST": db_host,
                "DB_PORT": "5432",
                "DB_NAME": "vsarchive",
                "DB_USER": "vsarchive",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_secret, field="password"
                ),
            },
        )


        ecs.FargateService(
            self,
            f"{cfg.prefix}-worker-svc",
            cluster=cluster,
            task_definition=worker_task,
            desired_count=1,
            security_groups=[sg_web],
            assign_public_ip=False,
        )
