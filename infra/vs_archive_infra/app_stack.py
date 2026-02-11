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
from typing import cast
from .config import EnvConfig


class VsArchiveAppStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, cfg: EnvConfig, vpc: ec2.Vpc, 
                 sg_alb: ec2.SecurityGroup, sg_web: ec2.SecurityGroup, sg_pg: ec2.SecurityGroup, 
                 bucket: s3.Bucket, queue: sqs.Queue, db_secret: secretsmanager.ISecret, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.Cluster(self, f"{cfg.prefix}-cluster", vpc=vpc)

        namespace = servicediscovery.PrivateDnsNamespace(
            self, f"{cfg.prefix}-ns", name=f"{cfg.prefix}.local", vpc=vpc
        )

        # Common Log Retention (Cost Saving)
        log_driver_config = lambda name: ecs.LogDrivers.aws_logs(
            stream_prefix=name, retention=logs.RetentionDays.ONE_WEEK
        )

        # --- Postgres Task ---
        pg_task = ecs.FargateTaskDefinition(self, f"{cfg.prefix}-pg-td", cpu=256, memory_limit_mib=512)
        pg_task.add_container(f"{cfg.prefix}-pg", 
            image=ecs.ContainerImage.from_registry("postgres:16-alpine"),
            logging=log_driver_config(f"{cfg.prefix}-pg"),
            environment={"POSTGRES_DB": "vsarchive", "POSTGRES_USER": "vsarchive"},
            secrets={"POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password")}
        ).add_port_mappings(ecs.PortMapping(container_port=5432))

        pg_svc = ecs.FargateService(self, f"{cfg.prefix}-pg-svc",
            cluster=cluster, task_definition=pg_task,
            security_groups=[sg_pg], assign_public_ip=True, # Required since no NAT
            cloud_map_options=ecs.CloudMapOptions(name="postgres", cloud_map_namespace=namespace)
        )

        # --- IAM Roles ---
        assumed_by = cast(iam.IPrincipal, iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        task_role = iam.Role(self, f"{cfg.prefix}-task-role", assumed_by=assumed_by)
        bucket.grant_read_write(task_role)
        queue.grant_consume_messages(task_role)
        db_secret.grant_read(task_role)
        task_role.add_to_principal_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel", "translate:TranslateText"], resources=["*"]))
        exec_role = iam.Role(self, f"{cfg.prefix}-exec-role", assumed_by=assumed_by)
        exec_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"))

        # --- Web Task ---
        web_task = ecs.FargateTaskDefinition(self, f"{cfg.prefix}-web-td", cpu=256, memory_limit_mib=512, task_role=task_role, execution_role=exec_role)
        web_repo = ecr.Repository.from_repository_name(self, f"{cfg.prefix}-web-repo", "vs-archive-web")
        web_task.add_container(f"{cfg.prefix}-web",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag="dev"),
            logging=log_driver_config(f"{cfg.prefix}-web"),
            environment={"UPLOADS_BUCKET_NAME": bucket.bucket_name, "SQS_QUEUE_URL": queue.queue_url, "DB_HOST": f"postgres.{cfg.prefix}.local", "DB_NAME": "vsarchive", "DB_USER": "vsarchive"},
            secrets={"DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password")}
        ).add_port_mappings(ecs.PortMapping(container_port=8000))

        web_svc = ecs.FargateService(self, f"{cfg.prefix}-web-svc", cluster=cluster, task_definition=web_task, assign_public_ip=True, security_groups=[sg_web])
        
        # --- Worker Task (Spot Cost Saving) ---
        worker_task = ecs.FargateTaskDefinition(self, f"{cfg.prefix}-worker-td", cpu=256, memory_limit_mib=512, task_role=task_role, execution_role=exec_role)
        worker_task.add_container(f"{cfg.prefix}-worker",
            image=ecs.ContainerImage.from_ecr_repository(web_repo, tag="dev"),
            command=["bash", "-lc", "python manage.py run_worker"],
            logging=log_driver_config(f"{cfg.prefix}-worker"),
            environment={"SQS_QUEUE_URL": queue.queue_url, "UPLOADS_BUCKET_NAME": bucket.bucket_name, "DB_HOST": f"postgres.{cfg.prefix}.local", "DB_NAME": "vsarchive", "DB_USER": "vsarchive"},
            secrets={"DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password")}
        )

        worker_svc = ecs.FargateService(self, f"{cfg.prefix}-worker-svc",
            cluster=cluster, task_definition=worker_task, assign_public_ip=True, security_groups=[sg_web],
            capacity_provider_strategies=[ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=1)]
        )

        # --- ALB Targets ---
        alb = elbv2.ApplicationLoadBalancer(self, f"{cfg.prefix}-alb", vpc=vpc, internet_facing=True, security_group=sg_alb)
        listener = alb.add_listener(f"{cfg.prefix}-http", port=80, open=True)
        listener.add_targets(f"{cfg.prefix}-tg", port=8000, targets=[web_svc], health_check=elbv2.HealthCheck(path="/health/", interval=Duration.seconds(30)))

        # --- Nightly Shutdown (Cost Saving) ---
        # Times are in UTC. Israel is UTC+2.
        # Shutdown: 23:00 Israel -> 21:00 UTC
        # Startup: 08:30 Israel -> 06:30 UTC
        for svc in [web_svc, worker_svc, pg_svc]:
            try:
                scaling_target = svc.auto_scale_task_count(min_capacity=0, max_capacity=1)
                scaling_target.scale_on_schedule("NightlyStop", schedule=scaling.Schedule.cron(minute="0", hour="21"), min_capacity=0, max_capacity=0)
                scaling_target.scale_on_schedule("MorningStart", schedule=scaling.Schedule.cron(minute="30", hour="6"), min_capacity=1, max_capacity=1)
            except Exception: pass
