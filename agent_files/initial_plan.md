# Crash-Safe Low-Space Game Folder Swap Utility

## 1. Goal

Build a Windows-first Python CLI utility that swaps two large folder trees on different drives with the following constraints:

- The folders may be hundreds of GB in size.
- Free space on each drive may be much smaller than either folder.
- The process must survive crashes, power loss, and forced termination.
- The process must resume from durable state without corrupting either folder.
- Temporary working space must stay bounded.
- The utility should avoid reimplementing normal file-copy behavior when a proven copy tool can do it safely.

Example:

```text
D:\Games\GameA
E:\Games\GameB
```

Desired final state:

```text
D:\Games\GameB
E:\Games\GameA
```

## 2. Core Design Decisions

### 2.1 Language

- Implement the application in Python.
- Use Python for orchestration, planning, SQLite state management, recovery, CLI, and chunk-exchange fallback logic.
- Do not use Python for performance-sensitive whole-tree copy when a native backend can be used.

Reasoning:

- The dominant cost is disk I/O, not Python CPU overhead.
- Python is well suited for durable orchestration and recovery logic.
- A native backend can handle most normal copy operations.

### 2.2 Copy Backends

- Default backend: `robocopy`.
- Optional future backends: `FastCopy`, `TeraCopy`.
- The app owns transaction boundaries and recovery state.
- The backend only moves bytes and metadata for normal file transfers.

Important:

- External copy tools do not solve the low-space deadlock case by themselves.
- The app must include a custom chunk-exchange engine for cases where normal copy cannot make progress.

### 2.3 Swap Model

Do not swap arbitrary folder trees by directly renaming `FolderA` to `FolderB` or by assuming all large files can be paired as `A.bin <-> B.bin`.

Instead use this model:

1. Measure both source trees enough to verify final capacity feasibility.
2. Rename both original roots to staging roots on their own drives.
3. Scan the staged trees to build the durable inventory.
4. Recreate empty final destination roots.
5. Transfer entries from staged roots to final roots using logged transactions.
6. Use normal backend copy whenever enough space exists.
7. Use custom chunk exchange only when no safe whole-file transfer can make progress.
8. Delete staged source entries only after destination data is durable and verified.

## 3. Scope And Non-Goals

### 3.1 In Scope

- Swapping two existing folder trees located on different Windows drives.
- Crash-safe resume.
- Bounded temporary working space.
- Whole-file copy using native backend when possible.
- Chunk-level fallback for large-file deadlocks.
- Verification and detailed status reporting.
- Basic metadata preservation by default.

### 3.2 Out Of Scope For First Version

- Full VSS snapshot integration.
- Full undo after successful completion.
- Deduplication across unrelated files.
- Multi-folder swap cycles involving more than two roots.
- Cross-machine or network share semantics.
- Guaranteed preservation of every NTFS feature in default mode.

## 4. Hard Preconditions

The app must refuse to start unless all of the following are true.

### 4.1 Root Validity

- Both input paths exist.
- Both input paths are directories.
- The two paths are on different volumes.
- Both paths are direct swap roots, not nested inside one another.
- Neither path is already a staging path or working directory path.

### 4.2 Final Capacity Feasibility

Before any data movement, compute:

```text
final_free_on_A = free_A + size(folder_A) - size(folder_B)
final_free_on_B = free_B + size(folder_B) - size(folder_A)
```

Both must be greater than or equal to:

```text
safety_margin
```

If either check fails, the swap is impossible regardless of chunk size or copy strategy.

### 4.3 Minimum Working Space

The app must also verify enough temporary workspace exists for:

- SQLite database and WAL or journal files.
- Logs.
- Chunk-exchange backup buffers.
- Small control files.

For chunk exchange, minimum free working space requirement is:

```text
2 * chunk_size + metadata_overhead
```

This requirement is about transactional workspace only. It does not replace final capacity feasibility.

## 5. High-Level Execution Model

### 5.1 Staging Layout

Given:

```text
D:\Games\GameA
E:\Games\GameB
```

The app will stage as:

```text
D:\Games\.swap_src_A
E:\Games\.swap_src_B
```

Then recreate empty final roots:

```text
D:\Games\GameB
E:\Games\GameA
```

If the user prefers preserving original root names instead of content names, the plan must define final targets explicitly at startup. The implementation should not infer naming policy mid-run.

### 5.2 Working Directory

Create a durable working directory, preferably next to the executable or under an app-controlled location:

```text
.swap_work/
    swap.db
    logs/
    chunk_backups/
    manifests/
```

Chunk backup files must be uniquely named by operation and chunk index.

## 6. Components

### 6.1 Scanner

Responsibilities:

- Walk both staged source trees without following symlinks, junctions, or other reparse points.
- Record a full inventory of files, directories, and link-like entries.
- Capture enough metadata to verify and recreate entries later.

The scanner runs after root staging so all durable inventory paths refer to staged source locations.

Each inventory entry should include at least:

```python
Entry(
    entry_id="...",
    side="A" or "B",
    relative_path="data/foo.pak",
    absolute_path="D:/Games/.swap_src_A/data/foo.pak",
    entry_type="file|dir|symlink|junction|other_reparse",
    size=123456789,
    mtime_ns=..., 
    ctime_ns=..., 
    attributes={...},
    link_target="...",   # for reparse/link entries when available
    backend_metadata={...}
)
```

Scanner rules:

- Do not recurse into reparse points by default.
- Record the reparse point itself as an entry.
- Detect file-vs-directory and file-vs-link conflicts by comparing both inventories.

### 6.2 Planner

Responsibilities:

- Build the destination mapping for every entry before execution begins.
- Validate conflicts and unsupported cases.
- Compute drive-level space state.
- Produce a durable operation graph.
- Recompute priorities after each completed transaction.

Planner output should classify entries into:

- `STRUCTURE_OP`: create directory, create link, set metadata.
- `FILE_COPY_OP`: whole-file backend copy is possible.
- `FILE_EXCHANGE_OP`: low-space chunk exchange may be required.
- `CLEANUP_OP`: delete staged source, remove empty dirs, finalize.

### 6.3 Transaction Store

Use SQLite for all durable state.

Requirements:

- Use explicit transactions.
- Persist intent before risky writes.
- Keep recovery decisions data-driven.
- Never rely on in-memory state for crash recovery.

### 6.4 Executor

Responsibilities:

- Execute one durable operation at a time.
- Update SQLite state after every durable milestone.
- Verify writes.
- Resume from partial state.
- Refuse to delete source data until destination state is verified.

### 6.5 Scheduler

Responsibilities:

- Choose the next executable operation.
- Maximize forward progress.
- Prefer operations that free the currently constrained drive.
- Delay chunk exchange until it is truly required.

## 7. Entry Mapping Model

The planner must build an explicit mapping model. It must not assume a simplistic file-pair world.

### 7.1 Entry Classes

- `A_ONLY`: exists only in staged A; final destination is B's final root.
- `B_ONLY`: exists only in staged B; final destination is A's final root.
- `BOTH_SAME_RELATIVE_PATH`: both sides contain an entry at the same relative path.
- `TYPE_CONFLICT`: same relative path but different types, such as file vs directory.
- `REPARSE_ENTRY`: symlink, junction, or other reparse point.

### 7.2 Conflict Rules

The planner must resolve these before execution:

- File vs file at same relative path: both are valid, but destination ordering matters.
- File vs directory: destination parent must be cleared in the correct order.
- File vs symlink or junction: do not overwrite blindly.
- Directory vs symlink or junction: treat as conflict, not as a normal directory.

The first implementation may reject unsupported reparse combinations explicitly instead of attempting risky behavior.

## 8. Scheduler Heuristic

The app should use a space-aware scheduler, not a fixed-order loop.

### 8.1 Priority Order

1. Zero-space structural operations.
2. Whole-file transfers that fit safely.
3. Whole-file transfers whose eventual source deletion frees the most constrained drive.
4. Smaller safe transfers before huge safe transfers when the other signals are similar.
5. Chunk exchange only when no safe whole-file transfer can make progress.

### 8.2 Constrained Drive

At any point, define the constrained drive as the drive whose current free space most limits pending operations.

### 8.3 Scheduling Rules

- Never schedule a whole-file transfer that would drop destination free space below:

```text
safety_margin + chunk_reserve
```

- Prefer a transfer if, after it completes and its staged source is deleted, it materially increases free space on the constrained drive.
- Recompute scores after each completed operation.
- Detect deadlock when:
  - no pending whole-file transfer fits, and
  - no already-completable cleanup can free enough space.
- On deadlock, search for chunk-exchange candidates.

### 8.4 Suggested Score Function

The exact weights can be tuned later, but the planner should use a score shaped like:

```text
score =
    + useful_space_freed_on_constrained_drive
    + safe_fit_bonus
    + smaller_file_bonus
    + path_conflict_reduction_bonus
    - destination_pressure_penalty
    - chunk_exchange_penalty
```

The goal is explainable scheduling, not a black-box optimizer.

## 9. Copy Strategy

### 9.1 Normal Path: Whole-File Backend Copy

Use `robocopy` for normal whole-file transfers whenever the destination has enough space.

The app must treat backend copy as a transaction with explicit states:

1. Plan copy.
2. Ensure parent directory exists.
3. Launch backend copy for one file or a tightly scoped batch.
4. Verify the copied result.
5. Persist verified destination state.
6. Delete staged source.
7. Persist source deletion.

Do not rely on `shutil.move()` across volumes.

Reason:

- Cross-volume `shutil.move()` becomes copy-then-delete.
- Its intermediate state is not sufficiently modeled for crash recovery.

### 9.2 Backend Abstraction

Define a backend interface like:

```python
class CopyBackend:
    def copy_file(self, src: str, dst: str, metadata_mode: str) -> CopyResult:
        ...

    def verify_file(self, src: str, dst: str, mode: str) -> VerifyResult:
        ...
```

The backend implementation should normalize:

- exit codes
- stderr/stdout capture
- retry semantics
- metadata flags

For `robocopy`, explicitly document accepted exit codes because nonzero does not always mean failure.

## 10. Low-Space Chunk Exchange Fallback

### 10.1 Purpose

Chunk exchange exists to break low-space deadlocks, not to replace normal copying.

Use it only when:

- the final swap is capacity-feasible,
- no safe whole-file transfer can make progress,
- and at least one large transfer can only proceed by reusing occupied space on both drives.

### 10.2 Exchange Candidate Model

The planner should prefer chunk exchange between:

- two large files on opposite drives,
- especially when they map to opposite final destinations,
- and especially when their sizes are similar.

If no direct pair exists, the planner may later support more advanced donor-slot strategies, but the first implementation should stay with pairwise exchange.

If the scheduler reaches deadlock and no valid exchange pair exists, the app must stop safely and report that the swap is not executable under current implementation limits. It must not attempt speculative destructive behavior.

### 10.3 Chunk Size

Configurable, for example:

```text
64MB to 256MB
```

Working-space requirement:

```text
2 * chunk_size + safety_overhead
```

### 10.4 Durable Chunk Transaction

For each chunk range, the executor must persist a state machine detailed enough for crash recovery.

Minimal chunk states:

- `PLANNED`
- `BACKUP_A_WRITTEN`
- `BACKUP_B_WRITTEN`
- `BACKUPS_SYNCED`
- `A_WRITTEN`
- `A_SYNCED`
- `B_WRITTEN`
- `B_SYNCED`
- `CHUNK_VERIFIED`
- `BACKUPS_REMOVED`
- `DONE`

The exact state naming can differ, but there must be enough information to know whether to restore, retry, re-verify, or continue.

### 10.5 Equal-Size Case

For overlapping chunk range:

1. Read source chunk from file A.
2. Read source chunk from file B.
3. Write chunk backups to working directory.
4. Flush backup files and sync them durably.
5. Persist backup-complete state.
6. Write B chunk into A target range.
7. Flush and sync A.
8. Persist A-written state.
9. Write A chunk into B target range.
10. Flush and sync B.
11. Persist B-written state.
12. Verify both ranges.
13. Persist verified state.
14. Remove chunk backups.
15. Persist cleanup state.

### 10.6 Unequal-Size Case

The plan must explicitly handle size mismatch. Example:

```text
A.bin = 20GB
B.bin = 3GB
```

Rules:

- Exchange the overlapping range first:

```text
0 .. min(size_A, size_B)
```

- Tail ranges must be handled with explicit durable resize states.
- Destination expansion must be logged before writing beyond current EOF.
- Truncation must only happen after all required data is durable and verified.
- Recovery must know whether a file was expanded, partially filled, or pending truncation.

Required file-level states for unequal-size exchange:

- `SIZE_BASELINE_RECORDED`
- `DEST_EXPANSION_STARTED`
- `DEST_EXPANSION_SYNCED`
- `TAIL_COPY_STARTED`
- `TAIL_COPY_VERIFIED`
- `TRUNCATION_STARTED`
- `TRUNCATION_SYNCED`
- `FINAL_SIZE_VERIFIED`

The first implementation may conservatively reject certain unequal-size exchange cases if the recovery logic is not yet proven. The plan should be honest about that boundary.

## 11. Recovery Model

### 11.1 Startup Recovery

On every startup, before any new planning:

1. Open SQLite.
2. Detect unfinished swap session.
3. Load incomplete operations and chunk states.
4. Inspect required on-disk artifacts such as chunk backups and staged roots.
5. Resume or repair the in-flight transaction before scheduling any new work.

### 11.2 Recovery Principles

- Recovery decisions must come from persisted operation state plus on-disk artifacts.
- If state says backups were durable but writes were incomplete, restore from backups.
- If state says writes were durable but verification was incomplete, re-verify before continuing.
- If backup removal was incomplete, verification must determine whether backups can be safely removed or need to remain for investigation.

### 11.3 Operation State Granularity

Broad states like `IN_PROGRESS` are not enough.

Normal file-copy operations should use states like:

- `PLANNED`
- `COPY_STARTED`
- `COPY_FINISHED`
- `DEST_SYNCED`
- `DEST_VERIFIED`
- `SOURCE_DELETE_STARTED`
- `SOURCE_DELETED`
- `METADATA_APPLIED`
- `COMPLETED`
- `FAILED`

Chunk operations need the finer-grained states described above.

## 12. Metadata Policy

### 12.1 Default Metadata Mode

Default target: basic metadata preservation.

Preserve when practical:

- file contents
- size
- mtime
- creation time if available
- normal file attributes

### 12.2 Optional Advanced Metadata Mode

Optional future mode:

- ACLs
- owner
- alternate data streams
- extended attributes where supported

The first implementation should clearly document what is and is not preserved by the chosen backend flags.

## 13. Symlink, Junction, And Reparse Handling

Rules:

- Do not follow symlinks or junctions by default.
- Record them as entries.
- Recreate the link or junction itself at the destination.
- Treat unfamiliar reparse tags conservatively.
- Reject unsupported reparse-point transformations explicitly.

This avoids traversing outside the intended tree.

## 14. Verification Policy

Every data-moving operation must be verified before source deletion.

Verification modes:

- `none`: only for development or explicit unsafe mode.
- `fast`: size and key timestamps, optionally backend-reported success.
- `sha256`: full content hash verification.

Default should be `fast` for practical performance.

Chunk-exchange verification should verify at least the rewritten chunk range and final file sizes. Full-file hashing may be optional and configurable.

## 15. Directory And Cleanup Rules

Directory creation and deletion should also be logged.

Rules:

- Create required destination parent directories before file transfer.
- Do not remove staged directories until all contained entries are completed.
- Remove empty staged directories bottom-up.
- Final cleanup removes:
  - empty staging roots
  - chunk backup files
  - obsolete manifests if the session is complete

## 16. SQLite Schema Requirements

The exact schema can evolve, but the plan must support at least these durable entities.

### 16.1 Sessions

```text
swap_sessions
```

Fields:

- session_id
- created_at
- state
- folder_a_original
- folder_b_original
- folder_a_staged
- folder_b_staged
- folder_a_final
- folder_b_final
- chunk_size
- verify_mode
- backend_name
- safety_margin_bytes

### 16.2 Inventory

```text
inventory_entries
```

Fields:

- entry_id
- session_id
- side
- relative_path
- absolute_path
- entry_type
- size_bytes
- mtime_ns
- ctime_ns
- attributes_json
- link_target
- inventory_hash_optional

### 16.3 Planned Operations

```text
operations
```

Fields:

- operation_id
- session_id
- operation_type
- src_entry_id
- dst_relative_path
- src_absolute_path
- dst_absolute_path
- size_bytes
- priority_score
- state
- last_error
- backend_name
- verification_mode
- needs_chunk_exchange

### 16.4 Chunk State

```text
chunk_operations
```

Fields:

- chunk_operation_id
- parent_operation_id
- file_a_path
- file_b_path
- chunk_index
- offset_bytes
- length_bytes
- state
- backup_a_path
- backup_b_path
- hash_a_optional
- hash_b_optional
- last_error

### 16.5 Events Or Logs

```text
operation_events
```

Fields:

- event_id
- session_id
- operation_id
- chunk_operation_id_nullable
- timestamp
- event_type
- message
- payload_json

## 17. CLI

### 17.1 Main Command

```bash
python swap.py run \
    --folder-a "D:\Games\GameA" \
    --folder-b "E:\Games\GameB" \
    --chunk-size 256MB \
    --verify fast \
    --backend robocopy
```

### 17.2 Additional Commands

```bash
python swap.py resume
python swap.py status
python swap.py rollback-current
python swap.py doctor
```

Command semantics:

- `run`: create a new session, validate, stage, and execute.
- `resume`: continue the active incomplete session.
- `status`: print progress summary from SQLite.
- `rollback-current`: only repair or abort the current unfinished transaction if safe.
- `doctor`: inspect session state, backups, and staged roots for troubleshooting.

Do not promise full rollback after successful completion.

## 18. Safety Requirements

Mandatory:

- Final-capacity feasibility check before execution.
- Durable SQLite transaction log.
- Root staging before content transfer.
- No source deletion before destination verification.
- Chunk-level durable state for exchange fallback.
- Automatic recovery on restart.
- Bounded temporary workspace.
- No silent traversal through junctions or symlinks.
- Explicit failure on unsupported conflict types.

Never:

- assume graceful shutdown
- store complete duplicate copies of either folder
- rely on cross-volume `shutil.move()`
- delete source data based only on backend success code
- claim success before all staged source content is removed and final roots verify clean

## 19. Implementation Phases

This section is for the coding agent.

### Phase 1: Foundation

- Build CLI skeleton.
- Add session creation.
- Add SQLite schema and migration bootstrap.
- Implement scanner and inventory persistence.
- Implement final-capacity feasibility checks.

### Phase 2: Staging And Structure Ops

- Implement root staging renames.
- Recreate empty final roots.
- Build explicit destination mapping.
- Implement directory create and cleanup operations.

### Phase 3: Whole-File Copy Path

- Implement `robocopy` backend wrapper.
- Implement per-file transaction states.
- Implement verification and source deletion.
- Implement resume for interrupted whole-file operations.

### Phase 4: Scheduler

- Implement constrained-drive detection.
- Implement operation scoring.
- Implement dynamic re-planning after each completed operation.
- Detect deadlock and escalate to exchange candidate search.

### Phase 5: Chunk Exchange Fallback

- Implement chunk backup files.
- Implement chunk state machine.
- Implement recovery from each chunk state.
- Start with equal-size exchange support.
- Add unequal-size support only when fully modeled and tested.

### Phase 6: Recovery, Diagnostics, And Hardening

- Implement `resume`, `status`, and `doctor`.
- Add detailed event logging.
- Add power-loss and forced-kill recovery tests.
- Validate cleanup and finalization rules.

## 20. Testing Requirements

The coding agent should implement tests for at least:

- inventory correctness
- feasibility rejection
- root staging and restart behavior
- per-file copy recovery after forced termination
- scheduler behavior with asymmetric free space
- deadlock detection
- chunk exchange recovery after interruption at every durable state
- unequal-size edge cases if supported
- symlink and junction non-recursion
- metadata verification in default mode

At least some tests should use sparse files or generated large files to simulate large sizes without requiring real 100GB test fixtures.

## 21. Success Criteria

The utility is successful when it can:

1. Swap two large folder trees on different drives.
2. Refuse impossible swaps before making changes.
3. Make forward progress using whole-file backend copy whenever possible.
4. Break true low-space deadlocks using chunk exchange.
5. Survive process kill, crash, or power loss without losing already-verified data.
6. Resume automatically from durable state.
7. Finish with correct final folder contents and no leftover staged source data.

Correct bounded-space claim:

```text
Temporary working storage is bounded by roughly 2 * chunk_size plus metadata/log overhead,
but the final destination drives must still be capacity-feasible for the swapped end state.
```
