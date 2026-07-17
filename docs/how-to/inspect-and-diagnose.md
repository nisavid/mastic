# How to inspect and diagnose a local stack

Use this guide when MASTIC is installed and you need to determine why a local
stack is stopped, degraded, or not serving requests.

## Start with the system overview

```sh
mastic status
```

The overview reports the Supervisor, Gateway, inference services, active or
recent operations, memory pressure, and valid next actions. A stopped resource
does not make `status` fail when the observation itself succeeds.

For automation, request deterministic output:

```sh
mastic status --json
```

## Run health-policy checks

```sh
mastic check
```

Unlike `status`, `check` exits nonzero when the current system does not satisfy
its health policy. Use it in scripts and CI when stopped or degraded service
should fail the caller.

To narrow the check to one inference service:

```sh
mastic service check SERVICE_NAME
```

## Ask MASTIC for a diagnosis

```sh
mastic doctor
```

`doctor` correlates configuration, lifecycle, routing, runtime, and service
evidence and returns stable next actions. Follow the narrowest suggested
operation instead of editing MASTIC state or application configuration
directly.

## Inspect logs and durable work

Read the bounded product log:

```sh
mastic logs
```

List recorded physical operations, then inspect the relevant operation:

```sh
mastic operation list
mastic operation inspect OPERATION_ID
```

Operation records preserve progress and terminal evidence for runtime and model
work even after the invoking interface exits.

## Verify the affected boundary

After applying the suggested recovery operation, rerun the narrow check and
then the system-wide check:

```sh
mastic service check SERVICE_NAME
mastic check
mastic status
```

If the problem involves a managed application, also inspect that integration:

```sh
mastic client inspect codex
mastic client inspect hindsight
```

For owned files, ports, launchd behavior, and target-Mac validation, see the
[deployment contract](../reference/deployment-contract.md).
