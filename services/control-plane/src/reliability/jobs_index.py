"""Imports every module that registers background jobs. Extended as modules are added."""

from reliability.modules.audit import jobs as _audit_jobs  # noqa: F401
from reliability.modules.targets import jobs as _targets_jobs  # noqa: F401
from reliability.platform import maintenance as _maintenance  # noqa: F401
