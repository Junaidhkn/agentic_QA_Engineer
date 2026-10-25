"""App-under-test sandboxes (ADR-0011, low-level-design §5): build the customer's app from
source, start it with its datastores, stub server and telemetry, and tear everything down.

``ProcessSandbox`` runs the app directly on the host (local development and tests only, like
the process driver); ``DockerSandbox`` builds images and runs a compose project under gVisor on
the job's internal network.
"""
