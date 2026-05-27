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
from aws_cdk import aws_ssm as ssm
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
        transkribus_parameter_prefix = f"/{cfg.project}/{cfg.env_name}/transkribus"
        transkribus_secret_prefix = f"{cfg.project}/{cfg.env_name}/transkribus"

        # --- Gemini Secret ---
        gemini_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "GeminiApiKeySecret", "vs-archive-dev/gemini_api_key"
        )
        transkribus_enable_hebrew_handwritten_param = (
            ssm.StringParameter.from_string_parameter_name(
                self,
                "TranskribusEnableHebrewHandwrittenParam",
                f"{transkribus_parameter_prefix}/enable-hebrew-handwritten",
            )
        )
        transkribus_dev_upload_mode_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "TranskribusDevUploadModeParam",
            f"{transkribus_parameter_prefix}/dev-upload-mode",
        )
        transkribus_use_existing_server_document_param = (
            ssm.StringParameter.from_string_parameter_name(
                self,
                "TranskribusUseExistingServerDocumentParam",
                f"{transkribus_parameter_prefix}/use-existing-server-document",
            )
        )
        transkribus_collection_id_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "TranskribusCollectionIdParam",
            f"{transkribus_parameter_prefix}/collection-id",
        )
        transkribus_model_id_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "TranskribusModelIdParam",
            f"{transkribus_parameter_prefix}/model-id",
        )
        transkribus_username_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "TranskribusUsernameSecret",
            f"{transkribus_secret_prefix}/username",
        )
        transkribus_password_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "TranskribusPasswordSecret",
            f"{transkribus_secret_prefix}/password",
        )
        transkribus_api_token_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "TranskribusApiTokenSecret",
            f"{transkribus_secret_prefix}/api-token",
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
        gemini_secret.grant_read(task_role)
        
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
        gemini_secret.grant_read(exec_role)
        transkribus_enable_hebrew_handwritten_param.grant_read(exec_role)
        transkribus_dev_upload_mode_param.grant_read(exec_role)
        transkribus_use_existing_server_document_param.grant_read(exec_role)
        transkribus_collection_id_param.grant_read(exec_role)
        transkribus_model_id_param.grant_read(exec_role)
        transkribus_username_secret.grant_read(exec_role)
        transkribus_password_secret.grant_read(exec_role)
        transkribus_api_token_secret.grant_read(exec_role)

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

                # Feature flags
                "ENABLE_HYBRID_HTR": "false",
                "ENABLE_DAILY_REPORT": "false",

                # Gemini / OCR behavior
                "GEMINI_CONFIDENCE_THRESHOLD": "0.55",
                "MIN_TEXT_LENGTH": "30",

                # Retry policy
                "MAX_RETRIES": "2",
                "RETRY_DELAY_SECONDS_1": "60",
                "RETRY_DELAY_SECONDS_2": "300",

                # Reporting (still required by current env_validation, even if ENABLE_DAILY_REPORT=false) 17.2.26
                "REPORT_WINDOW_START": "07:00",
                "REPORT_SEND_TIME": "23:00",

                "FREE_TIER_ALERT_PCT": "80",
                "GEMINI_FREE_DAILY_REQUEST_LIMIT": "200",
                "GEMINI_FREE_DAILY_IMAGE_LIMIT": "200",
                "TRANSKRIBUS_FREE_MONTHLY_CREDITS": "50",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                "GEMINI_API_KEY": ecs.Secret.from_secrets_manager(gemini_secret),
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
                # Preserve current live worker OCR/runtime tuning to avoid drift on CDK deploy.
                "ENABLE_HYBRID_HTR": "false",
                "ENABLE_DAILY_REPORT": "false",
                "GEMINI_DOUBLE_PASS": "true",
                "GEMINI_TEMPERATURE": "0.0",
                "GEMINI_TOP_K": "1",
                "GEMINI_TOP_P": "0.2",
                "GEMINI_CONSISTENCY_MIN_RATIO": "0.92",
                "GEMINI_CONFIDENCE_THRESHOLD": "0.55",
                "MIN_TEXT_LENGTH": "30",
                "MAX_RETRIES": "2",
                "RETRY_DELAY_SECONDS_1": "60",
                "RETRY_DELAY_SECONDS_2": "300",
                "GEMINI_FREE_DAILY_REQUEST_LIMIT": "200",
                "GEMINI_FREE_DAILY_IMAGE_LIMIT": "200",
                "TRANSKRIBUS_FREE_MONTHLY_CREDITS": "50",
                "REPORT_WINDOW_START": "07:00",
                "REPORT_SEND_TIME": "23:00",
                "FREE_TIER_ALERT_PCT": "80",
                "LOG_LEVEL": "INFO",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                "GEMINI_API_KEY": ecs.Secret.from_secrets_manager(gemini_secret),
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": ecs.Secret.from_ssm_parameter(
                    transkribus_enable_hebrew_handwritten_param
                ),
                "TRANSKRIBUS_DEV_UPLOAD_MODE": ecs.Secret.from_ssm_parameter(
                    transkribus_dev_upload_mode_param
                ),
                "TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT": ecs.Secret.from_ssm_parameter(
                    transkribus_use_existing_server_document_param
                ),
                "TRANSKRIBUS_COLLECTION_ID": ecs.Secret.from_ssm_parameter(
                    transkribus_collection_id_param
                ),
                "TRANSKRIBUS_MODEL_ID": ecs.Secret.from_ssm_parameter(
                    transkribus_model_id_param
                ),
                "TRANSKRIBUS_USERNAME": ecs.Secret.from_secrets_manager(
                    transkribus_username_secret
                ),
                "TRANSKRIBUS_PASSWORD": ecs.Secret.from_secrets_manager(
                    transkribus_password_secret
                ),
                "TRANSKRIBUS_API_TOKEN": ecs.Secret.from_secrets_manager(
                    transkribus_api_token_secret
                ),
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
