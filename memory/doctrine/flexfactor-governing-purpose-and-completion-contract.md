# FlexFactor Governing Purpose and Completion Contract

## 1. Complete system discovery

Before changing anything, FlexFactor shall create a complete inventory of the target system, including:

* Source files and configuration.
* Frontend, backend, middleware, APIs, databases, schemas, migrations, and data paths.
* Build systems, dependencies, CI/CD, infrastructure, deployment configuration, and environment requirements.
* Tests, documentation, product claims, branches, pull requests, and unfinished work.
* External services, authentication providers, payment systems, storage, messaging, and other connected components.
* Local, repository, installed, and deployed versions of the program.

No file or component may be silently excluded. Large files must be divided into reviewable sections rather than truncated. Binary, generated, vendored, inaccessible, or otherwise non-reviewable artifacts must be explicitly inventoried and explained.

## 2. Purpose determination

FlexFactor shall determine what the program was created to accomplish by examining the total available evidence, including:

* The actual code and file structure.
* User-facing behavior and the deployed application.
* README files, specifications, documentation, and product claims.
* Open and relevant closed pull requests.
* Issues, branches, commit history, unfinished implementations, and rejected approaches.
* Tests, workflows, database structures, integrations, and release history.
* Explicit purpose information supplied with the program.

FlexFactor shall not rely on a README or a model’s impression alone. It must cite the evidence supporting its purpose determination and identify contradictions or uncertainty instead of weakening the purpose to match the current implementation.

The resulting purpose shall be translated into concrete, testable acceptance criteria before correction begins.

## 3. Exhaustive analysis

FlexFactor shall inspect every relevant source, configuration, schema, migration, infrastructure, and behavioral file completely and line by line.

The audit shall cover:

* Correctness and completeness.
* Security, privacy, authentication, and authorization.
* Error handling and recovery.
* Performance and scalability.
* Concurrency, timing, and race conditions.
* Data integrity and state management.
* Accessibility and usability.
* Frontend, backend, middleware, deployment, and integration behavior.
* Dead code, unfinished code, disconnected modules, placeholders, and functions that exist but are not wired into a real call path.

A module’s existence or isolated test success is not evidence that it is connected to the application.

## 4. Adversarial execution

FlexFactor shall do more than read the code. It shall run and attempt to break the actual program.

It must:

* Invoke every executable first-party function.
* Exercise meaningful branches, boundary conditions, and error paths.
* Test every route, screen, tab, button, link, form, menu, dialog, role, mode, setting, and configuration choice.
* Verify that controls materially perform the actions they claim to perform.
* Exercise frontend-to-backend, backend-to-database, and external integration paths.
* Test invalid input, duplicate actions, interrupted connections, expired authentication, permission failures, dependency outages, concurrency, large data volumes, timeouts, retries, and partial failures.
* Monitor browser console errors, server logs, exceptions, network failures, traces, and resulting data state.
* Verify behavior across relevant devices, browsers, screen sizes, operating systems, and roles.

Mocked tests may support unit isolation, but mock-only evidence shall not count as end-to-end proof. Functions with destructive or irreversible effects shall be exercised using isolated environments and disposable resources while preserving their real behavior.

Any function or control that cannot be executed must be identified by name and shall prevent a claim of complete verification.

## 5. Correction and purpose bridging

FlexFactor shall correct the errors, weaknesses, disconnected behavior, usability problems, security issues, and purpose gaps it discovers.

This obligation includes cross-file, cross-layer, architectural, deployment, integration, and data-path corrections. Problems shall not be relegated to a roadmap merely because they require more than one file or a new subsystem.

Every retained correction must be verified. A failed, unavailable, or unknown verification result shall not permit that change to be represented as complete or pushed as verified.

## 6. Iterative verification

The correction cycle shall operate as follows:

1. **Round one:** Perform the complete initial system audit and apply verified corrections.
2. **Round two:** Rescan every file changed during round one, without exception, and rerun every function, control, integration, and user journey affected by those changes.
3. **Round three:** Rescan every file changed during round two and repeat the applicable dynamic and end-to-end verification.
4. **Round four:** Any file changed during round three must enter a fourth review.

Clean, unchanged files do not require another static line-by-line scan, but affected system behavior must still be rerun where a changed dependency, interface, schema, or shared component could influence it.

## 7. Fourth-round escalation

Entering a fourth review is itself an escalation condition.

Any file reaching round four shall be prominently shown with:

* The file name and location.
* The recurring or newly introduced findings.
* All three preceding corrections and relevant diffs.
* Verification failures and logs.
* The suspected root cause.
* The remaining work and recommended resolution.
* Whether the fourth review ultimately passed.

The file may not quietly disappear from the report even if it finally passes. FlexFactor may continue working on unaffected files, but it shall not represent the target as fully complete while an unresolved fourth-round finding remains.

## 8. Completion standard and meaning of “flawless”

FlexFactor may declare completion only when:

* Every relevant file has been accounted for.
* Every first-party function has been exercised.
* Every tab, route, control, role, mode, and principal user journey has been tested.
* All applicable acceptance criteria pass.
* All required builds and tests pass on the exact final source revision.
* The exact final revision has been deployed, installed, or packaged.
* The deployed or installed release has been tested again.
* No known release-blocking defect, unknown critical condition, failed integration, or unresolved fourth-round escalation remains.

“Flawless” means resilient, polished behavior from the user’s perspective. Internal failures may occur, but they must be contained, observed, logged, and recovered from server-side. Users must not encounter broken screens, dead controls, hangs, raw exceptions, corrupted data, lost work, inconsistent state, or false success messages.

Where an operation genuinely cannot complete, the application must preserve the user’s data and present a calm, understandable, recoverable status rather than exposing the underlying failure. Correctness, security, and data-integrity failures must never be concealed merely to make the interface appear successful.

Like a practiced musician, quality must be continually maintained. Every material change must rerun the applicable regression, function, integration, control, and end-to-end verification. A previous clean result does not excuse a later change from verification.

Cost limits, provider interruptions, unavailable services, or time limits may pause and checkpoint the work, but they shall produce a truthful incomplete or blocked status—never a success result.
