# Concerns About `plan.md`

## 1. Final Capacity Feasibility Is Missing

The plan says the utility only needs `2 * chunk_size` temporary space, but that is not sufficient to guarantee the final state is possible.

Before any swap starts, the planner must verify that each drive can hold the folder that will end up there:

```text
free_A + size(folder_A) - size(folder_B) >= safety_margin
free_B + size(folder_B) - size(folder_A) >= safety_margin
```

If either check fails, the swap is impossible regardless of the chunk algorithm.

## 2. The Chunk-Swap Algorithm Does Not Handle Arbitrary Folder Trees

The large-file algorithm assumes two files, `A.bin` and `B.bin`, can be swapped chunk-for-chunk at matching offsets.

Real folder swaps are more complicated:

```text
A-only files
B-only files
same relative path files
different relative path files
file-vs-directory conflicts
directory-vs-symlink conflicts
```

The plan needs a clear file-mapping model before it can safely describe execution.

## 3. Unequal File Sizes Are Not Defined

The chunk-swap algorithm only works cleanly when the two files have compatible chunk ranges.

It does not define what happens when:

```text
A.bin = 20GB
B.bin = 3GB
```

The plan needs explicit handling for:

```text
tail chunks
destination expansion
source truncation
crash recovery during truncation
verification after resize
```

Without this, large unequal files can be corrupted.

## 4. `shutil.move()` Is Risky Across Drives

Across different drives, `shutil.move()` becomes copy-then-delete.

That is risky here because:

```text
it may require too much destination space
it hides intermediate states
it is harder to resume safely
it may delete after a copy operation the transaction log did not fully model
```

The plan should use explicit logged steps instead:

```text
copy or chunk-copy
flush
verify
commit state
delete source only after verification
commit deletion
```

On Windows, `robocopy` may be better, but only with carefully selected flags and explicit state tracking.

## 5. Metadata Preservation Is Underspecified

The plan says to preserve timestamps, attributes, and permissions when possible.

That is not specific enough for Windows. `shutil.copy2()` does not preserve every NTFS detail, such as:

```text
full ACLs
owner
alternate data streams
some reparse point details
```

The plan should choose one explicit metadata target:

```text
basic metadata: timestamps and normal attributes
full NTFS metadata: ACLs, owner, attributes, alternate streams where possible
```

For game folders, basic metadata may be acceptable, but the utility should say so.

## 6. Symlink And Junction Handling Needs More Safety Rules

The scanner detects symlinks, but Windows folders may also contain junctions and other reparse points.

The utility should not recurse through junctions by default. Otherwise it may accidentally traverse outside the intended folder tree.

Recommended behavior:

```text
record reparse points as entries
recreate the link or junction itself
do not follow it unless the user explicitly asks
```

## 7. Recovery States Are Too Coarse

The current recovery section uses broad states like:

```text
BACKED_UP
COMPLETED
```

That is not enough for crash safety. A crash can happen:

```text
after writing one destination chunk
before writing the other destination chunk
after fsync but before SQLite commit
during destination file expansion
during source truncation
during temp backup deletion
```

Each durable step needs a persisted state so recovery can determine whether to restore, retry, verify, or continue.

## 8. Recommended Improvement

A safer overall approach is:

```text
1. Scan both folders and store inventories.
2. Verify final drive capacity before starting.
3. Rename both source roots in-place to staging names on their own drives.
4. Create new destination root folders.
5. Transfer files as logged copy/verify/delete transactions.
6. Use chunked copy for files larger than available free space.
7. Delete source files only after verified durable writes.
8. Treat rollback as recovery of the current unfinished transaction, not full undo.
```

Example staging layout:

```text
D:\Games\GameA -> D:\Games\.swap_src_A
E:\Games\GameB -> E:\Games\.swap_src_B

D:\Games\GameA becomes the final location for old GameB
E:\Games\GameB becomes the final location for old GameA
```

This model avoids pretending that arbitrary folders can always be swapped with only `2 * chunk_size`. It still supports low-space operation, but only after checking that the final sizes are actually feasible.
