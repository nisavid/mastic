# MASTIC

MASTIC describes a host-tailored inference stack as portable user intent, an
exact machine-bound plan, and independently evidenced application targets.

## Language

**Blueprint**:
Portable desired application targets, workload intents, constraints, and
preferences that MASTIC evaluates for a particular machine.
_Avoid_: Stack Plan, executable plan

**Plan**:
An exact machine-bound set of mutations, dependencies, evidence, approvals,
and recovery data produced by evaluating a Blueprint.
_Avoid_: Blueprint, Stack Plan

**Bootstrap Plan**:
A Plan limited to installing MASTIC and dependencies shared by every
Blueprint.
_Avoid_: setup plan, Stack Plan

**Application Configuration Target**:
One independently selected and evidenced configuration scope for a consuming
application, such as a Codex configuration or Hindsight profile.
_Avoid_: client, account, global configuration

**Validated Plan**:
A Plan whose exact host and component combination satisfies an accepted
validation profile and carries its stated guarantees.
_Avoid_: supported mode, expert plan

**Exploratory Plan**:
A Plan with incomplete validation evidence and no known result predicting a
soft conflict or hard violation.
_Avoid_: unvalidated mode, expert plan

**Known-Risk Plan**:
A Plan with evidence of a soft fit, compatibility, performance, or support
conflict that a user may explicitly override.
_Avoid_: Override Plan, expert plan

**Blocked Plan**:
A Plan that violates a non-overridable invariant or has an impossible
dependency and cannot be executed.
_Avoid_: Known-Risk Plan, failed plan

**Override**:
The explicit user decision to proceed with a Known-Risk Plan after reviewing
its evidence and consequences.
_Avoid_: expert mode, warning acceptance

**No Validated Fit**:
A completed discovery outcome in which no Validated Plan satisfies the
machine and Blueprint; it makes no deployment-readiness claim.
_Avoid_: Blocked, Degraded

**Completion**:
Whether every selected Plan target has been evaluated, expressed as Partial
or Complete independently of readiness.
_Avoid_: readiness, success

**Readiness**:
The verified operational outcome of a target or Plan, expressed as Pending,
Unverified, Ready, or Degraded independently of completion.
_Avoid_: completion, plan evidence

**Public Validation Registry**:
A shared source of validation results derived from public, reproducible
artifacts for evaluating Blueprints against current component evidence.
_Avoid_: private telemetry store, user-content database
