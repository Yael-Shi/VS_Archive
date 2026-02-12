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
            nat_gateways=0, # Cost saving: removed NAT Gateway
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", 
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="isolated", 
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24
                ),
            ],
        )

        # Security Groups
        self.sg_alb = ec2.SecurityGroup(
            self, f"{cfg.prefix}-sg-alb",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="ALB Security Group",
        )

        self.sg_web = ec2.SecurityGroup(
            self, f"{cfg.prefix}-sg-web",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="ECS Web/Worker Security Group",
        )

        self.sg_pg = ec2.SecurityGroup(
            self, f"{cfg.prefix}-sg-pg",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Postgres ECS Security Group",
        )

        # Security Group for EFS
        self.sg_efs = ec2.SecurityGroup(
            self, f"{cfg.prefix}-sg-efs",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="EFS Security Group",
        )

        self.sg_alb.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(80),
        )

        self.sg_web.add_ingress_rule(
            peer=self.sg_alb,
            connection=ec2.Port.tcp(8000),
        )

        self.sg_pg.add_ingress_rule(
            peer=self.sg_web,
            connection=ec2.Port.tcp(5432),
        )

        # Allow Postgres to connect to EFS
        self.sg_efs.add_ingress_rule(
            peer=self.sg_pg,
            connection=ec2.Port.tcp(2049), # NFS port for EFS
        )