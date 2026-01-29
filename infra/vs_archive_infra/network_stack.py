from aws_cdk import Stack
from constructs import Construct
from aws_cdk import aws_ec2 as ec2
from .config import EnvConfig


class VsArchiveNetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, cfg: EnvConfig, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            f"{cfg.prefix}-vpc",
            vpc_name=f"{cfg.prefix}-vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC
                ),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
                ),
            ],
        )

        # Security Groups
        # ALB: internet-facing, lives in public subnets
        self.sg_alb = ec2.SecurityGroup(
            self,
            f"{cfg.prefix}-sg-alb",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Security group for the Application Load Balancer",
        )

        # Web/Worker: ECS tasks in private subnets
        self.sg_web = ec2.SecurityGroup(
            self,
            f"{cfg.prefix}-sg-web",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Security group for ECS tasks (web + worker)",
        )

        # DB (reserved for future RDS): keep outbound blocked
        self.sg_db = ec2.SecurityGroup(
            self,
            f"{cfg.prefix}-sg-db",
            vpc=self.vpc,
            allow_all_outbound=False,
            description="Security group for future RDS Postgres (not used for ECS Postgres)",
        )

        # Postgres as ECS task (DEV): must have outbound open (pull image, logs, secrets, DNS, etc.)
        self.sg_pg = ec2.SecurityGroup(
            self,
            f"{cfg.prefix}-sg-pg",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Security group for Postgres running as ECS task (dev)",
        )

        # Internet -> ALB (HTTP for now)
        self.sg_alb.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(80),
            description="HTTP from Internet to ALB",
        )

        # ALB -> Web tasks (nginx on 80 for now)
        self.sg_web.add_ingress_rule(
            peer=self.sg_alb,
            connection=ec2.Port.tcp(80),
            description="ALB to Web tasks",
        )

        # Web/Worker -> Postgres task (dev)
        self.sg_pg.add_ingress_rule(
            peer=self.sg_web,
            connection=ec2.Port.tcp(5432),
            description="Web/Worker to Postgres task",
        )
