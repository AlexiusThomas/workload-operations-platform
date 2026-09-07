"""WOP infrastructure as code (AWS CDK, Python) — Task 20.

Defines three composed stacks (data, compute, monitoring). Everything is
environment-agnostic: account and region come from the CDK environment
(``CDK_DEFAULT_ACCOUNT`` / ``CDK_DEFAULT_REGION``) at deploy time and are never
hard-coded in source.

Integration boundaries remain unresolved and are not simulated as production-ready:
- DEP-001 (RATS ingestion) — files arrive in the import bucket via S3 PutObject.
- DEP-002 (Midway authentication) — production ``AUTH_MODE=midway`` selects a documented
  NotImplemented provider.
"""

from infra.stacks import ComputeStack, DataStack, MonitoringStack

__all__ = ["ComputeStack", "DataStack", "MonitoringStack"]
