Subject: RE: Brookfield Use Case 2.13 – Platform Development & Security Architecture
We reviewed the platform development plan and use-case footprint, then did a deep-dive on the Genie security architecture (trust model, request/guard flow, tool permissions, multi-tenancy and guardrails), and closed on the development process.

PLATFORM & USE-CASE PLANNING

Reviewed the Claude-generated platform report. Currently Ravi (+1) is reviewing it; no dedicated reviewer/owner is assigned yet — a defined review, versioning and sign-off process is needed.
Report covers ~40% of requirements; ~60% remains (including LLM guardrail items not yet working).
Walked the use-case summary: platform components vs. the first three use cases targeted for M-T1, with task list and demo gates. Two later use cases are defined but need more detail. Full lists exist for M-T1 and M-T2.
Agreed to proceed with the security deck; to be shared so everyone can review the timeline and raise concerns.


SECURITY – TRUST MODEL (WebVision → Genie)

Current problem: on a user prompt, plain user metadata (user ID, org ID) is sent from WebVision to Genie in plain text — Genie can't verify the source is trusted and there's no authorization check; IDs are just passed to tools.
Target flow: request hits WebVision → user authenticated + Genie-app access checked (deny if none) → user-role mapping fetched from DB (allowed agents + permissions) → packed into a token → token passed to Genie.
Added layer: WebVision shares its digital certificate with Genie; Genie verifies both the token signature and the certificate. This certificate-based auth is the machine-to-machine (M2M) piece — to be incorporated in a later iteration.
The architecture must support the OMS / California ISO config (Sasan's point): authenticate first through the application, then avoid routing through it afterward — expected to become standard across deployments.
Internal OETI service-to-service auth, DB-level and LLM-endpoint auth (LLM URLs currently hittable if known) noted as separate layers for a later deployment diagram, not this one.


SECURITY – REQUEST / GUARD FLOW

Flow: input guard → router (intent) → agent (with agent-access check from token) → orchestrator/planner for multi-intent → completion gateway, with a re-plan (react) loop.
Key discussion: access should be validated earlier — filter the agents a user can access and pass only those to the router/planner, rather than picking an agent and then checking access. Raised to avoid the planner producing an "ideal path" the user can't actually use. To be considered.
Partial fulfillment: if a multi-intent prompt needs more agents than the user can access, handle the permitted part and tell the user the rest can't be done.
Router to be a configurable/skippable component (single intent → route directly to one agent; otherwise → planner). Router is not built yet; this diagram is about security, not router design.
Agent registry maintains the agent list and per-user access.


TOOL & PERMISSION MODEL

Role/entity-based: user → role → entity (e.g. HR / Operations) → permissions (read/write/delete) → tools. Tool author defines the permission a tool requires; user must hold it or the tool won't execute.
Two checks: before tools are exposed to the LLM, and again at execution (gateway intercept) — the LLM never calls tools directly.
Tool visibility via MCP server URL attached to the agent, with filtering. Agreed we need registration-time validation of what tools/permissions an agent requests (programmatic restriction), not just runtime.
Agent-to-agent: each calling agent sends its certificate + JWT claims so the receiver can verify the request is from an authorized agent (guards against a compromised agent impersonating another).
JWT/token is not passed to the agents themselves and must not reach the LLM.


MULTI-TENANCY / ORG ACCESS

Genie is one application/ecosystem, but the same agent (e.g. OASIS) is called by multiple customers (CAISO, SaaS power), so the org/tenant must be enforced.
JWT must carry org + user ID, with org-level checks at each component. Org ID alone may not be sufficient — flagged as needing a separate deep-dive (community/multi-tenant data isolation; prior incident where one customer's data was visible to another).


GUARDRAILS (4 GUARDS)

Guard 1: before entry — prompt-injection / secrets check (using LLM Guard).
Guard 2: scans inputs going to agents / MCP interactions.
Guard 3: scans generated output for PII / secrets / untrusted info.
Guard 4: final check on aggregated output before it reaches the user.
Open point: where to enforce bad-topics / policy (e.g. disallowed requests) — leaning toward Guard 1 and Guard 4. A strong last-minute guard at the end is wanted.


DEVELOPMENT PROCESS & CODE REVIEW

Repo is currently committed directly to main with no PR/merge-request process. Need to establish a dev process and CI/CD pipeline — to be a separate meeting.
On AI-generated code: reviewing thousands of generated lines manually isn't feasible. Agreed to keep changes small (one functionality per MR), commit the spec alongside the change, and use an AI first-pass review (severity report catching ~90% of issues) before human review.
Take learnings from the CAISO/engineering team on reducing LLM overuse — use LLMs only for non-deterministic / natural-language / reasoning steps, and traditional logic for stable, deterministic ones.



ACTION ITEMS —

Share the security presentation for review (timeline/concerns).
Consider moving agent-access validation earlier (filter accessible agents before router/planner).
Add registration-time validation of agent tool/permission requests.
Define the multi-tenancy / org-level access approach (separate deep-dive).
Set up a separate meeting to define the development process, CI/CD pipeline and code-review workflow.
