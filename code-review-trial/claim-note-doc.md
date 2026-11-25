One defect that one reviewer reported in one commit. The value is the reviewer's
description of the defect. The note name says which reviewer wrote it:
`diff-only[claim]` is from the reviewer that saw the code alone, and
`session[claim]` is from the reviewer that also read the session that wrote the
code. The target is the `commit-tree` attachment of the commit. The metadata
holds the commit, the file, the line, the quoted text of that line, and the
severity (wrong-result, data-loss, unhandled-input, or rule). The quote was
found at that line of the file when the note was written. A claim is a
suspected defect. No one has checked it.
