"""Obsidian vault export: the skill graph as clickable markdown.

Derived data — the exporter reads the brain and writes only markdown (its
sole db writes are uid mints for evidence links, by the uid registry's
design). One note per graph node (plus client pages for CRM rows and task
entities), edges as [[wikilinks]] with the relationship named
inline, type folders so Obsidian's graph view color-codes. Incremental by
content hash via a manifest (.gravity/manifest.json): only changed notes are
rewritten, notes for deleted nodes are removed (downstream healing extends
into the vault), and files the exporter didn't create are NEVER written or
deleted — the user's own notes are sacred. The vault regenerates from the db,
so it is excluded from encrypted sync by design (it lives outside the repo
and sync only bundles state/exocortex.db + config.yaml).

Trail-derived evidence (Plane B) may appear here because the vault is a
local, never-synced artifact — but it always carries the "observed" label.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

from .config import Config
from .state import db
from .state.uids import uid_for

log = logging.getLogger("exo.vault")

TYPE_FOLDERS = {
    "skill": "Skills", "domain": "Domains", "project": "Projects",
    "client": "Clients", "person": "People", "concept": "Concepts",
    "tool": "Tools",
}
DEFAULT_FOLDER = "Other"
MANIFEST_DIR = ".gravity"

WINDOWS_RESERVED = {"con", "prn", "aux", "nul",
                    *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}

# what an edge reads as, source → destination ("X …verb… [[Y]]")
_REL_FWD = {"in_domain": "belongs to the domain", "demonstrated_in": "demonstrated in",
            "uses": "uses", "part_of": "part of", "works_with": "works with"}
# and read from the destination's side ("[[X]] …verb… here")
_REL_BACK = {"in_domain": "lives in this domain", "demonstrated_in": "was demonstrated here",
             "uses": "is used here", "part_of": "is part of this",
             "works_with": "works with this"}


def sanitize_name(name: str, node_id: int | None = None) -> str:
    """Filesystem- and wikilink-safe filename that cannot escape the vault.
    Path separators, quotes, control chars, Windows-illegal characters and
    wikilink-breaking characters become spaces; reserved device names, dot
    runs and empty results are defused."""
    s = str(name)
    s = re.sub(r'[\x00-\x1f<>:"/\\|?*\[\]#^]', " ", s)
    s = s.replace("..", " ")
    s = re.sub(r"\s+", " ", s).strip().strip(". ")
    if not s or s.casefold() in WINDOWS_RESERVED:
        s = f"node-{node_id}" if node_id is not None else "unnamed"
    return s[:80].rstrip(". ")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _day(ts_ms) -> str:
    if not ts_ms:
        return "?"
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


class VaultExporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = Path(os.path.expanduser(
            str(cfg.get("vault.path", "d:/gravity-vault"))))
        self.manifest_path = self.root / MANIFEST_DIR / "manifest.json"
        self._basenames: dict[str, str] = {}   # "node:<id>"/"client:<name>" -> basename
        self._used: set[str] = set()           # casefolded basenames, global uniqueness

    # ---------------------------------------------------------- manifest

    def _load_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_manifest(self, m: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(m, indent=1), encoding="utf-8")

    # ---------------------------------------------------------- naming
    # Basenames are globally unique (not just per folder) so a bare
    # [[wikilink]] always resolves to exactly one note.

    def _claim(self, key: str, name: str, node_id: int) -> str:
        base = sanitize_name(name, node_id)
        cand, n = base, 0
        while cand.casefold() in self._used:
            n += 1
            cand = f"{base} ({node_id})" if n == 1 else f"{base} ({node_id}.{n})"
        self._used.add(cand.casefold())
        self._basenames[key] = cand
        return cand

    def _link(self, key: str, fallback: str = "") -> str:
        base = self._basenames.get(key)
        return f"[[{base}]]" if base else f"[[{sanitize_name(fallback)}]]"

    # ---------------------------------------------------------- export

    def export(self) -> dict:
        conn = db.connect(self.cfg.db_path)
        try:
            local_conn = db.local_connect(self.cfg)
        except Exception:
            local_conn = None
        try:
            notes = self._build_notes(conn, local_conn)
            notes["_START HERE.md"] = self._dashboard(conn)
            return self._write(notes)
        finally:
            if local_conn is not None:
                local_conn.close()
            conn.close()

    def _write(self, notes: dict[str, str]) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        manifest = self._load_manifest()
        new_manifest: dict = {}
        # manifest entries no longer produced: their notes' nodes vanished
        # (healing extends to the vault) or were renamed
        stale = {rel: h for rel, h in manifest.items() if rel not in notes}
        written = unchanged = removed = 0
        try:
            # delete stale notes FIRST: on Windows's case-insensitive
            # filesystem a case-only rename must free the old path before the
            # new one is written, or the old file masquerades as a user file
            for rel in list(stale):
                p = self.root / rel
                try:
                    inside = p.resolve().is_relative_to(root)
                except OSError:
                    stale.pop(rel)
                    continue
                if not inside:
                    log.error("manifest entry outside vault ignored: %r", rel)
                    stale.pop(rel)  # not ours to delete; forget it
                    continue
                try:
                    if p.is_file():
                        p.unlink()
                        removed += 1
                    stale.pop(rel)  # removed, or was never a file we made
                except OSError:
                    # locked/undeletable: keep the entry so the next export
                    # retries instead of orphaning the note as a "user file"
                    log.warning("could not remove %s; will retry next export", rel)
            for rel, content in notes.items():
                target = self.root / rel
                try:
                    if not target.resolve().is_relative_to(root):
                        log.error("refusing to write outside vault: %r", rel)
                        continue
                except OSError:
                    continue
                if rel not in manifest and target.exists():
                    # a file we didn't create sits at this path — never
                    # overwrite it, and never adopt it into the manifest (so
                    # we also never delete it later)
                    log.warning("skipping %s: user-created file in the way", rel)
                    continue
                h = _hash(content)
                try:
                    on_disk = None
                    if target.exists():
                        try:
                            on_disk = _hash(target.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError):
                            on_disk = None  # unreadable/mangled → restore it
                    # "unchanged" must hold on disk too: an exporter-owned
                    # note that was hand-edited gets restored, as documented
                    if h == on_disk and manifest.get(rel) == h:
                        unchanged += 1
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8", newline="\n")
                        written += 1
                    new_manifest[rel] = h
                except OSError:
                    log.warning("could not write %s", rel)
                    if rel in manifest:  # keep tracking the existing note
                        new_manifest[rel] = manifest[rel]
        finally:
            # persists even on a partial run: notes written so far are
            # tracked (never orphaned as "user files"), undeleted stale
            # entries are retried next export
            self._save_manifest({**stale, **new_manifest})
        return {"notes": len(notes), "written": written, "unchanged": unchanged,
                "removed": removed, "vault": str(self.root)}

    # ---------------------------------------------------------- notes

    def _build_notes(self, conn: sqlite3.Connection,
                     local_conn: sqlite3.Connection | None) -> dict[str, str]:
        notes: dict[str, str] = {}
        # the dashboard owns this basename; a node with the same name gets
        # disambiguated instead of colliding with it
        self._used.add("_start here")

        nodes = conn.execute("SELECT * FROM skill_nodes ORDER BY id").fetchall()
        crm_rows = conn.execute("SELECT * FROM crm ORDER BY id").fetchall()
        node_names = {n["name"].casefold() for n in nodes}
        crm_names = {c["name"].casefold() for c in crm_rows}
        # clients mentioned only as task entities still get a page, so the
        # dashboard's "tasks by client" wikilinks resolve (deduped
        # case-insensitively — task matching is COLLATE NOCASE anyway)
        entities, seen = [], set()
        for r in conn.execute(
                "SELECT DISTINCT entity FROM tasks WHERE entity IS NOT NULL"
                " AND status = 'open' ORDER BY entity"):
            cf = r["entity"].casefold()
            if cf not in node_names | crm_names | seen:
                seen.add(cf)
                entities.append(r["entity"])

        # pass 1: claim every basename first so cross-links are stable.
        # "client:<casefolded name>" is the link-lookup key the dashboard
        # uses; each page also gets its own collision-proof claim key.
        for n in nodes:
            base = self._claim(f"node:{n['id']}", n["name"], n["id"])
            if n["type"] == "client":
                self._basenames.setdefault(f"client:{n['name'].casefold()}", base)
        for c in crm_rows:
            base = self._claim(f"client:crm:{c['id']}", c["name"], c["id"])
            self._basenames.setdefault(f"client:{c['name'].casefold()}", base)
        for i, e in enumerate(entities):
            self._claim(f"client:{e.casefold()}", e, 90000 + i)

        edges = conn.execute(
            "SELECT e.*, a.name AS src_name, b.name AS dst_name FROM skill_edges e"
            " JOIN skill_nodes a ON a.id = e.src"
            " JOIN skill_nodes b ON b.id = e.dst").fetchall()
        by_node: dict[int, list] = {}
        for e in edges:
            by_node.setdefault(e["src"], []).append(("out", e))
            by_node.setdefault(e["dst"], []).append(("in", e))

        # pass 2: render
        for n in nodes:
            folder = TYPE_FOLDERS.get(n["type"], DEFAULT_FOLDER)
            base = self._basenames[f"node:{n['id']}"]
            notes[f"{folder}/{base}.md"] = self._render_node(
                conn, local_conn, n, by_node.get(n["id"], []))
        for c in crm_rows:
            base = self._basenames[f"client:crm:{c['id']}"]
            notes[f"Clients/{base}.md"] = self._render_client(conn, c["name"],
                                                              crm_row=c)
        for e in entities:
            base = self._basenames[f"client:{e.casefold()}"]
            notes[f"Clients/{base}.md"] = self._render_client(conn, e)
        return notes

    def _render_node(self, conn, local_conn, n, node_edges) -> str:
        conf = n["confidence"]
        shaky = (" — shaky (it keeps needing another look)" if conf < 0.4
                 else " — solid" if conf > 0.7 else "")
        summaries = {
            "skill": f"A skill in your graph since {_day(n['created_ts'])};"
                     f" confidence {conf:.2f}{shaky}.",
            "domain": f"A domain of your activity, first seen {_day(n['created_ts'])}.",
            "project": f"A project of yours, first seen {_day(n['created_ts'])}.",
            "client": f"A client, tracked since {_day(n['created_ts'])}.",
            "person": f"A person in your world since {_day(n['created_ts'])}.",
            "tool": f"A tool you use, first seen {_day(n['created_ts'])}.",
            "concept": f"A concept the curriculum engine tracks;"
                       f" confidence {conf:.2f}{shaky}.",
        }
        lines = [
            "---",
            f"type: {n['type']}",
            f"confidence: {conf:.2f}",
            f"created: {_day(n['created_ts'])}",
            f"updated: {_day(n['updated_ts'])}",
            "gravity: exporter-owned",
            "---",
            "",
            summaries.get(n["type"], f"A {n['type']} node."),
            "",
        ]
        if n["type"] == "concept":
            lines.extend(self._concept_extras(conn, local_conn, n["name"]))
        if node_edges:
            lines.append("## Connections")
            for direction, e in sorted(node_edges, key=lambda x: (x[1]["rel"], x[0])):
                if direction == "out":
                    verb = _REL_FWD.get(e["rel"], e["rel"].replace("_", " "))
                    lines.append(f"- {verb} {self._link('node:%d' % e['dst'], e['dst_name'])}")
                else:
                    verb = _REL_BACK.get(e["rel"], e["rel"].replace("_", " "))
                    lines.append(f"- {self._link('node:%d' % e['src'], e['src_name'])} {verb}")
                lines.extend(f"    - {x}" for x in self._edge_evidence(conn, e["evidence"]))
            lines.append("")
        return "\n".join(lines)

    def _concept_extras(self, conn, local_conn, name: str) -> list[str]:
        out: list[str] = []
        c = conn.execute("SELECT status, gap_score, fumble_count, why FROM concepts"
                         " WHERE name = ?", (name,)).fetchone()
        if c:
            out.append(f"Curriculum status: **{c['status']}** (gap score"
                       f" {c['gap_score']:.1f}"
                       + (f", fumbled ×{c['fumble_count']}" if c["fumble_count"] else "")
                       + ")")
            if c["why"]:
                out.append(f"\nWhy it's tracked (inferred): {c['why']}")
        if local_conn is not None:
            ev = local_conn.execute(
                "SELECT evidence FROM concept_evidence WHERE concept_name = ?"
                " ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
            if ev:
                try:
                    cites = json.loads(ev["evidence"])[:3]
                except (json.JSONDecodeError, TypeError):
                    cites = []
                if cites:
                    out.append("\nEvidence (observed — from your local trail):")
                    out.extend(f"- {str(c)[:140]}" for c in cites)
        if out:
            out.append("")
        return out

    def _edge_evidence(self, conn, evidence_json) -> list[str]:
        out = []
        try:
            refs = json.loads(evidence_json or "[]")
        except json.JSONDecodeError:
            return out
        from .intents.provenance import short_label
        for ref in refs[:2]:
            if not isinstance(ref, dict) or ref.get("table") != "reflections":
                continue
            rid = ref.get("id")
            row = conn.execute("SELECT ts, substr(raw_text, 1, 90) AS t FROM"
                               " reflections WHERE id = ?", (rid,)).fetchone()
            if row:
                uid = uid_for(conn, "reflections", rid)
                out.append(f"evidence: “{row['t']}” — "
                           f"{short_label(conn, 'reflections', rid)},"
                           f" record #{uid}, {_day(row['ts'])}")
        return out

    def _render_client(self, conn, name: str, crm_row=None) -> str:
        from .intents.provenance import short_label
        kind = crm_row["kind"] if crm_row else "client"
        lines = ["---", "type: client"]
        if crm_row:  # entity-only clients have no known start date — omit
            lines.append(f"created: {_day(crm_row['created_ts'])}")
        lines += ["gravity: exporter-owned", "---", "",
                  f"A {kind} you work with."
                  + (f" Tracked since {_day(crm_row['created_ts'])}."
                     if crm_row else ""), ""]
        if crm_row:
            note_rows = conn.execute(
                "SELECT id, ts, note FROM crm_notes WHERE crm_id = ?"
                " ORDER BY ts DESC LIMIT 10", (crm_row["id"],)).fetchall()
            if note_rows:
                lines.append("## Notes")
                for r in note_rows:
                    uid = uid_for(conn, "crm_notes", r["id"])
                    lines.append(f"- {_day(r['ts'])}: {r['note'][:150]}"
                                 f" ({short_label(conn, 'crm_notes', r['id'])}, #{uid})")
                lines.append("")
        tasks = conn.execute(
            "SELECT id, text, due_ts FROM tasks WHERE status = 'open' AND"
            " entity = ? COLLATE NOCASE ORDER BY due_ts IS NULL, due_ts",
            (name,)).fetchall()
        if tasks:
            lines.append("## Open tasks")
            for t in tasks:
                due = f" (due {_day(t['due_ts'])})" if t["due_ts"] else ""
                lines.append(f"- #{uid_for(conn, 'tasks', t['id'])} {t['text'][:120]}{due}")
            lines.append("")
        return "\n".join(lines)

    # ---------------------------------------------------------- dashboard

    def _dashboard(self, conn) -> str:
        n_nodes = conn.execute("SELECT count(*) FROM skill_nodes").fetchone()[0]
        n_edges = conn.execute("SELECT count(*) FROM skill_edges").fetchone()[0]
        week_ago = db.now_ms() - 7 * 86400_000

        def links(rows):
            return ", ".join(self._link("node:%d" % r["id"], r["name"])
                             for r in rows) or "—"

        newest = conn.execute(
            "SELECT id, name FROM skill_nodes WHERE created_ts >= ?"
            " ORDER BY created_ts DESC LIMIT 8", (week_ago,)).fetchall()
        shaky = conn.execute(
            "SELECT id, name FROM skill_nodes WHERE type IN ('skill','concept')"
            " ORDER BY confidence ASC LIMIT 6").fetchall()
        hubs = conn.execute(
            "SELECT n.id, n.name, count(*) AS deg FROM skill_nodes n"
            " JOIN skill_edges e ON e.src = n.id OR e.dst = n.id"
            " GROUP BY n.id ORDER BY deg DESC, n.confidence DESC LIMIT 6").fetchall()

        lines = [
            "---", "gravity: exporter-owned", "---", "",
            "# Gravity — your knowledge graph",
            "",
            f"*Regenerated {dt.date.today():%Y-%m-%d} from the live database."
            " This vault is derived data: exporter-owned notes are overwritten"
            " on every export. Your own notes (any file the exporter didn't"
            " create) are never touched.*",
            "",
            f"**{n_nodes} nodes · {n_edges} connections** — open the graph"
            " view (Ctrl+G) and group colors by folder.",
            "",
            f"**Strongest hubs:** {links(hubs)}",
            "",
            f"**New this week:** {links(newest)}",
            "",
            "**Shakiest knowledge** (lowest confidence — things you keep"
            f" re-looking up): {links(shaky)}",
            "",
        ]
        by_client: dict[str, list[str]] = {}
        for t in conn.execute("SELECT entity, text FROM tasks WHERE"
                              " status = 'open' AND entity IS NOT NULL"
                              " ORDER BY entity, due_ts IS NULL, due_ts"):
            by_client.setdefault(t["entity"], []).append(t["text"])
        if by_client:
            lines.append("**Open tasks by client:**")
            for ent, items in sorted(by_client.items()):
                link = self._link(f"client:{ent.casefold()}", ent)
                lines.append(f"- {link}: " + "; ".join(i[:60] for i in items[:4]))
            lines.append("")
        n_unassigned = conn.execute(
            "SELECT count(*) FROM tasks WHERE status = 'open'"
            " AND entity IS NULL").fetchone()[0]
        if n_unassigned:
            lines.append(f"*(+{n_unassigned} open tasks with no client)*")
            lines.append("")
        return "\n".join(lines)


def export_vault(cfg: Config) -> dict:
    return VaultExporter(cfg).export()
