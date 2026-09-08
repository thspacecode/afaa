# Product Requirements: AFAA AI Skill Bundles

- **Status:** Draft
- **Component:** AFAA
- **Companion PRDs:** Porch skill-bundle orchestration; Porch Agent skill-bundle runtime

## Summary

AFAA AI Skills currently provide instructions and required-tool declarations but cannot include the supporting files commonly used by coding-agent skills. AFAA must let an AI Manager maintain a nested bundle of scripts, references, assets, and other resources for each AI Skill.

The editable folder is the source for future thread snapshots. When a new AFAA-backed Porch thread is created, AFAA must produce an immutable, content-addressed version of the skill bundle so the instructions and supporting files used by that thread cannot drift independently.

## Problem

Skill instructions commonly refer to relative resources such as:

```text
scripts/validate.py
references/domain-model.md
assets/template.csv
```

AFAA stores only the instruction text. Those paths do not exist in Porch Agent's workspace runtime, so the agent cannot read references, use templates, or explicitly execute scripts as intended.

## Goals

1. Give every AI Skill a managed nested resource tree.
2. Preserve familiar relative paths and arbitrary safe subdirectory names.
3. Support local text and binary files.
4. Produce deterministic, immutable bundle versions for Porch threads.
5. Preserve AFAA's existing AI Skill permissions and tool authorization model.
6. Keep bundle content private and out of browser-facing runtime APIs.

## Non-goals

- Replacing `AI Skill.instructions` with `SKILL.md`.
- Importing or implementing a complete third-party skill-package specification.
- Automatically executing scripts.
- Treating remote URLs as bundle files.
- Making mutable bundle files directly available to an active or existing thread.
- Adding bundle behavior to the built-in Porch assistant.

## Users

### AI Manager

Creates an AI Skill, edits its instructions, and maintains supporting files and folders.

### System Manager

Configures storage and bundle limits, diagnoses invalid bundles, and manages retention.

### Workspace user

Uses an AFAA-backed agent and receives reproducible behavior without directly managing bundle delivery.

## Product model

### Editable bundle

Each AI Skill has one logical Frappe File root:

```text
Home/afaa/ai-skill/<skill_key>/
```

The root may contain arbitrary nested folders, including but not limited to `scripts`, `references`, and `assets`.

`AI Skill.instructions` remains the authoritative instruction source. A `SKILL.md` file inside the bundle, if uploaded, is an ordinary resource and does not override the DocType fields.

### Identity

`skill_key` and the AI Skill document name are the bundle identity. Because the DocType is autonamed from `skill_key` and the key is already defined as stable routing identity, it must not be changed after insertion.

The skill display name and description remain editable.

### Immutable bundle version

A bundle version is a deterministic manifest plus immutable file content. The version is identified by a cryptographic digest calculated from canonical manifest data and the digest of every file.

The editable folder is not itself an immutable version. Editing it affects only bundle versions created for future threads.

## Functional requirements

### AFAA-SB-001 — Bundle-root lifecycle

- AFAA must create the logical bundle root for a newly inserted AI Skill.
- Creation must be idempotent and safe under concurrent requests.
- Existing AI Skills must receive roots through an idempotent migration or lazy creation path.
- Deleting a skill may remove its editable root, but must not remove immutable versions still referenced by Porch threads.
- Partial folder creation must not leave a root assigned to the wrong skill.

### AFAA-SB-002 — Stable identity

- AFAA must reject changes or renames that alter `skill_key` after insertion.
- Folder lookup must derive from the validated skill key, never from untrusted request paths.
- Keys must continue using the existing lowercase letter, number, underscore, and hyphen validation.

### AFAA-SB-003 — Bundle management

- An AI Manager with write permission on an AI Skill must be able to create folders and upload, replace, move, and delete files inside its editable bundle.
- An AI Manager with read permission must be able to list and read the bundle.
- Bundle operations must authorize against the owning AI Skill, not merely the owner of a Frappe File record.
- Bundle files must be private and associated with the owning AI Skill so standard attachment permissions cannot expose them broadly.
- A bundle operation must not move a file into another skill's root without authorization for both skills.

### AFAA-SB-004 — Supported tree

- The tree may contain arbitrary nested folders and local text or binary files.
- Relative paths must use `/` separators and preserve case.
- Empty directories may be represented in the editable tree but need not be included in a runtime version unless product behavior depends on them.
- Remote files and URLs are not valid bundle members.
- Symbolic links, device files, and other non-regular filesystem objects are not supported.

### AFAA-SB-005 — Path safety

AFAA must reject a bundle containing:

- absolute paths;
- `.` or `..` path segments;
- empty path segments;
- control characters or null bytes;
- duplicate canonical paths;
- a file and folder with the same canonical path;
- paths exceeding configured depth or length limits;
- folder cycles or records outside the skill root.

Validation must occur before a version becomes available to Porch.

### AFAA-SB-006 — Configurable limits

AFAA must enforce hard limits for:

- maximum files per skill bundle;
- maximum folder depth;
- maximum relative-path length;
- maximum bytes per file;
- maximum unencoded bytes per skill bundle; and
- maximum aggregate bundle bytes resolved for one agent.

Defaults must be conservative enough for synchronous server-to-server delivery. Exceeding a limit must produce an actionable validation error naming the limit, without returning file content.

### AFAA-SB-007 — Deterministic manifest

For each regular file, the manifest must include at least:

- canonical relative path;
- unencoded byte length;
- SHA-256 content digest; and
- transport content type or encoding metadata needed to reconstruct the original bytes.

Manifest ordering and bundle-digest calculation must be deterministic and independent of database row order, creation time, owner, and physical Frappe file URL.

### AFAA-SB-008 — Version creation

- AFAA must build a version from one consistent view of the editable tree.
- Version creation must fail atomically if any file is missing, remote, unreadable, unsafe, or over a limit.
- The immutable version must retain exact bytes even if an editable file is later replaced or deleted.
- Identical bundle contents may be deduplicated by digest.
- An empty bundle must have a valid deterministic representation.

### AFAA-SB-009 — Skill fingerprint

The external skill fingerprint must cover the immutable fields sent for the skill, including its bundle digest or canonical empty-bundle identity. Changing bundle content must therefore change the fingerprint for future snapshots.

Provider credentials, Frappe File IDs, physical storage paths, and mutable timestamps must not affect the fingerprint.

### AFAA-SB-010 — External runtime contract

AFAA's structured external runtime representation must provide Porch with enough machine-only data to:

- identify the pinned bundle version;
- validate its digest and limits;
- obtain its canonical manifest and exact file bytes; and
- associate it with exactly one resolved AI Skill.

Bundle content must not be included in legacy flattened-skill runtime contracts.

### AFAA-SB-011 — Live authorization

Existing validation remains in force:

- the AI Skill must be enabled and attached to the AI Agent;
- required tools must remain enabled, registered, and allowed; and
- unsupported external-runtime tools must fail closed.

Possession of an immutable bundle version does not bypass those checks.

### AFAA-SB-012 — Retention

- Immutable versions referenced by an existing thread must remain available.
- Editable bundle deletion must not silently corrupt retained versions.
- Unreferenced versions may be garbage-collected according to an explicit retention policy.
- Garbage collection must be idempotent and must recheck references before deletion.

## Security and privacy requirements

- All editable and immutable bundle files are private by default and in the initial release.
- APIs must check AI Skill permissions before listing or mutating resources.
- Runtime-only reads must use an explicit trusted-server path after Porch has authorized the agent run.
- File bytes, decoded text, and file names must not be written to normal logs, error telemetry, or audit metadata.
- Errors exposed to an unauthorized caller must not confirm another skill or bundle's existence.
- Uploading a script grants no automatic execution permission.

## User experience requirements

The AI Skill form must provide a clear entry point to manage its bundle. The experience must:

- display the bundle root and nested contents;
- distinguish folders, text files, and binary files;
- show file size and validation failures;
- explain that changes apply to future threads only; and
- prevent editing `skill_key` after creation.

A full source-code editor, archive importer, and drag-and-drop directory upload are optional enhancements, not launch requirements.

## Migration and compatibility

- Existing AI Skills migrate as valid skills with empty bundles.
- Existing AFAA execution inside Frappe must continue using skill instructions normally.
- Existing structured external-runtime callers must either negotiate the new contract version or remain on the old no-bundle contract.
- Existing data must not be rewritten destructively during folder initialization.

## Acceptance criteria

1. Creating an AI Skill creates or resolves exactly one managed root at `Home/afaa/ai-skill/<skill_key>`.
2. Two authorized AI Managers can manage private files according to AI Skill permissions rather than file ownership alone.
3. Nested text and binary files round-trip byte-for-byte.
4. Unsafe paths, remote files, and configured-limit violations fail before version publication.
5. Reordering File records does not change a bundle digest.
6. Changing one file changes the bundle digest for future snapshots.
7. A previously created immutable version remains readable after its editable source changes.
8. Deleting an editable bundle does not delete a version referenced by a thread.
9. Existing skills with no files resolve to a deterministic empty bundle.
10. No bundle content appears in browser APIs or logs.

## Product decisions recorded

- Instructions field plus arbitrary resource tree, not `SKILL.md` authority.
- `skill_key` is immutable and identifies the editable bundle root.
- Local text and binary files are supported recursively with configurable limits.
- Thread use is based on immutable, content-addressed bundle versions.
