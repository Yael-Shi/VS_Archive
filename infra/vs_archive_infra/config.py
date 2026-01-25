from dataclasses import dataclass

@dataclass(frozen=True)
class EnvConfig:
    project: str
    env_name: str
    region: str

    @property
    def prefix(self) -> str:
        return f"{self.project}-{self.env_name}"

def load_env_config(app) -> EnvConfig:
    project = app.node.try_get_context("project") or "vs-archive"
    env_name = app.node.try_get_context("env") or "dev"
    region = app.node.try_get_context("region") or "eu-central-1"
    return EnvConfig(project=project, env_name=env_name, region=region)
