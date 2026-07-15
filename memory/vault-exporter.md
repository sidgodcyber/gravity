# Vault exporter (exocortex/vault.py) — invariants that must hold

- The manifest (.gravity/manifest.json) IS the ownership boundary: a file is
  exporter-owned iff its relpath is in the manifest. Never write to or delete
  a path that exists but isn't in the manifest (user files are sacred), and
  never adopt such a path into the manifest.
- Save the manifest in a `finally`, incrementally built — a crash mid-export
  must not orphan just-written notes as "user files" (verifier reproduced a
  permanently wedged export from one bad manifest entry before this).
- Deletion pass runs BEFORE the write pass and guards with `is_file()` +
  per-file try/except: Windows is case-insensitive, so a case-only node
  rename must free the old path first, and a directory-resolving manifest
  entry ("" or ".") must never reach unlink().
- "Unchanged" must be verified against the DISK content hash, not just the
  manifest — docs promise exporter-owned notes are restored if hand-edited.
- Basenames are globally unique casefolded (collision → " (id)" suffix,
  itself re-checked in a loop — "x (5)" is a forgeable name) so bare
  [[wikilinks]] always resolve; "_START HERE" is pre-claimed.
- Frontmatter is YAML: never emit a bare "?" (broke Obsidian's whole
  properties block on entity-only client pages) — omit unknown fields.
- sanitize_name strips `<>:"/\|?*[]#^`, "..", reserved device names; every
  write/delete additionally checks resolve().is_relative_to(vault root),
  which also neutralizes the pathlib absolute-path trap
  (Path(root) / "C:/x" == Path("C:/x")).
- Plane B evidence may appear in the vault (it's local, never synced) but
  must always carry the "observed" label. Evidence record numbers are uids.
- The vault is derived data: excluded from sync bundles by construction
  (sync only packages exocortex.db + config.yaml); README says so.
