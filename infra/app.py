#!/usr/bin/env python3
import aws_cdk as cdk

from vs_archive_infra.config import load_env_config
from vs_archive_infra.network_stack import VsArchiveNetworkStack
from vs_archive_infra.data_stack import VsArchiveDataStack
from vs_archive_infra.app_stack import VsArchiveAppStack

app = cdk.App()
cfg = load_env_config(app)

env = cdk.Environment(
    account=cdk.Aws.ACCOUNT_ID,
    region=cfg.region,
)

# 1. Network Stack - provides VPC and Security Groups
network = VsArchiveNetworkStack(app, f"{cfg.prefix}-network-v2", cfg=cfg, env=env)

# 2. Data Stack - Now receives the VPC and EFS Security Group to create the drive
data = VsArchiveDataStack(
    app,
    f"{cfg.prefix}-data-v2",
    cfg=cfg,
    vpc=network.vpc,          # VPC reference
    sg_efs=network.sg_efs,    # Security Group for EFS
    env=env,
)

# 3. App Stack - receives the file_system to mount it to Postgres
VsArchiveAppStack(
    app,
    f"{cfg.prefix}-app-v2",
    cfg=cfg,
    vpc=network.vpc,
    sg_alb=network.sg_alb,
    sg_web=network.sg_web,
    sg_pg=network.sg_pg,
    bucket=data.bucket,
    queue=data.jobs_queue,
    db_secret=data.db_secret,
    file_system=data.file_system, # The actual EFS drive
    env=env,
)

app.synth()
