from aws_cdk import aws_ecr as ecr
from aws_cdk import Stack, Duration, RemovalPolicy
from constructs import Construct
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_servicediscovery as servicediscovery
from aws_cdk import aws_logs as logs
from aws_cdk import aws_applicationautoscaling as scaling
from aws_cdk import aws_efs as efs
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
        sg_pg: ec2.SecurityGroup,
        bucket: s3.Bucket,
        queue: sqs.Queue,
        db_secret: secretsmanager.ISecret,
        file_system: efs.FileSystem,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.Cluster(self, f"{cfg.prefix}-cluster", vpc=vpc)

        namespace = servicediscovery.PrivateDnsNamespace(
            self, f"{cfg.prefix}-ns", name=f"{cfg.prefix}.local", vpc=vpc
        )

        def get_log_driver(name_suffix: str):
            lg = logs.LogGroup(
                self,
                f"LogGroup-{name_suffix}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            return ecs.LogDrivers.aws_logs(stream_prefix=name_suffix, log_group=lg)

        public_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)

        # --- Google Vision Secret ---
        google_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "GoogleVisionSecret", "vs-archive/google-vision-key"
        )

        # --- Postgres Task with EFS Storage ---
        pg_task = ecs.FargateTaskDefinition(
            self, f"{cfg.prefix}-pg-td", cpu=256, memory_limit_mib=512
        )

        pg_task.add_volume(
            name="postgres_data",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id
            ),
        )

        pg_container = pg_task.add_container(
            f"{cfg.prefix}-pg",
            image=ecs.ContainerImage.from_registry("postgres:16-alpine"),
            logging=get_log_driver("pg"),
            environment={"POSTGRES_DB": "vsarchive", "POSTGRES_USER": "vsarchive"},
            secrets={
                "POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password")
            },
        )

        pg_container.add_mount_points(
            ecs.MountPoint(
                container_path="/var/lib/postgresql/data",
                source_volume="postgres_data",
                read_only=False,
            )
        )

        pg_container.add_port_mappings(ecs.PortMapping(container_port=5432))

        pg_svc = ecs.FargateService(
            self,
            f"{cfg.prefix}-pg-svc",
            cluster=cluster,
            task_definition=pg_task,
            security_groups=[sg_pg],
            assign_public_ip=True,
            vpc_subnets=public_subnets,
            cloud_map_options=ecs.CloudMapOptions(
                name="postgres", cloud_map_namespace=namespace
            ),
        )

        # --- IAM Roles ---
        assumed_by = cast(iam.IPrincipal, iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        task_role = iam.Role(self, f"{cfg.prefix}-task-role", assumed_by=assumed_by)
        bucket.grant_read_write(task_role)
        queue.grant_consume_messages(task_role)
        db_secret.grant_read(task_role)
        google_secret.grant_read(task_role)
        
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "translate:TranslateText"],
                resources=["*"],
            )
        )

        exec_role = iam.Role(self, f"{cfg.prefix}-exec-role", assumed_by=assumed_by)
        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy"
            )
        )
        google_secret.grant_read(exec_role)

        # --- Web Task ---
        web_task = ecs.FargateTaskDefinition(
            self,
            f"{cfg.prefix}-web-td",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=exec_role,
        )

        web_repo = ecr.Repository.from_repository_name(
            self, f"{cfg.prefix}-web-repo", "vs-archive-web"
        )

        image_tag = self.node.try_get_context("image_tag") or "dev"
        web_task.add_container(
            f"{cfg.prefix}-web",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag=image_tag),
            logging=get_log_driver("web"),
            environment={
                "UPLOADS_BUCKET_NAME": bucket.bucket_name,
                "SQS_QUEUE_URL": queue.queue_url,
                "DB_HOST": f"postgres.{cfg.prefix}.local",
                "DB_NAME": "vsarchive",
                "DB_USER": "vsarchive",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                "GOOGLE_APPLICATION_CREDENTIALS_JSON": ecs.Secret.from_secrets_manager(google_secret)
            },
        ).add_port_mappings(ecs.PortMapping(container_port=8000))

        web_svc = ecs.FargateService(
            self,
            f"{cfg.prefix}-web-svc",
            cluster=cluster,
            task_definition=web_task,
            assign_public_ip=True,
            vpc_subnets=public_subnets,
            security_groups=[sg_web],
            enable_execute_command=True,
        )

        # --- Worker Task (Spot Cost Saving) ---
        worker_task = ecs.FargateTaskDefinition(
            self,
            f"{cfg.prefix}-worker-td",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=exec_role,
        )

        image_tag = self.node.try_get_context("image_tag") or "dev"
        worker_task.add_container(
            f"{cfg.prefix}-worker",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag=image_tag),
            command=["bash", "-lc", "python manage.py run_worker"],
            logging=get_log_driver("worker"),
            environment={
                "SQS_QUEUE_URL": queue.queue_url,
                "UPLOADS_BUCKET_NAME": bucket.bucket_name,
                "DB_HOST": f"postgres.{cfg.prefix}.local",
                "DB_NAME": "vsarchive",
                "DB_USER": "vsarchive",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                "GOOGLE_APPLICATION_CREDENTIALS_JSON": ecs.Secret.from_secrets_manager(google_secret)
            },
        )

        worker_svc = ecs.FargateService(
            self,
            f"{cfg.prefix}-worker-svc",
            cluster=cluster,
            task_definition=worker_task,
            assign_public_ip=True,
            vpc_subnets=public_subnets,
            security_groups=[sg_web],
            enable_execute_command=True,
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=1)
            ],
        )

        # --- ALB Targets ---
        alb = elbv2.ApplicationLoadBalancer(
            self,
            f"{cfg.prefix}-alb",
            vpc=vpc,
            internet_facing=True,
            security_group=sg_alb,
        )
        listener = alb.add_listener(f"{cfg.prefix}-http", port=80, open=True)
        listener.add_targets(
            f"{cfg.prefix}-tg",
            port=8000,
            targets=[web_svc],
            health_check=elbv2.HealthCheck(path="/health/", interval=Duration.seconds(30)),
        )

        for svc in [web_svc, worker_svc, pg_svc]:
            scaling_target = svc.auto_scale_task_count(min_capacity=0, max_capacity=1)
            scaling_target.scale_on_schedule(
                f"{svc.node.id}-NightlyStop",
                schedule=scaling.Schedule.cron(minute="0", hour="21"),
                min_capacity=0,
                max_capacity=0,
            )
            scaling_target.scale_on_schedule(
                f"{svc.node.id}-MorningStart",
                schedule=scaling.Schedule.cron(minute="30", hour="6"),
                min_capacity=1,
                max_capacity=1,
            )
