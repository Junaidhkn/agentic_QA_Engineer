"""Imports every module that registers background jobs. Extended as modules are added."""

from reliability.modules.audit import jobs as _audit_jobs  # noqa: F401
from reliability.modules.diagnosis import jobs as _diagnosis_jobs  # noqa: F401
from reliability.modules.discovery import jobs as _discovery_jobs  # noqa: F401
from reliability.modules.environments import checks as _environment_checks  # noqa: F401
from reliability.modules.environments import jobs as _environments_jobs  # noqa: F401
from reliability.modules.findings import jobs as _findings_jobs  # noqa: F401
from reliability.modules.repairs import jobs as _repairs_jobs  # noqa: F401
from reliability.modules.reports import jobs as _reports_jobs  # noqa: F401
from reliability.modules.runners import jobs as _runners_jobs  # noqa: F401
from reliability.modules.runs import jobs as _runs_jobs  # noqa: F401
from reliability.modules.targets import jobs as _targets_jobs  # noqa: F401
from reliability.modules.testsuites import jobs as _testsuites_jobs  # noqa: F401
from reliability.modules.verification import jobs as _verification_jobs  # noqa: F401
from reliability.platform import maintenance as _maintenance  # noqa: F401
