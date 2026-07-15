"""Telegram bot — the phone interface. Long-polling (no public IP needed).

Only the owner (telegram.chat_id) is answered; everyone else gets silence.
Slow work (LLM calls, transcript scans) runs in threads so the bot stays
responsive; LiteLLM imports lazily on the first /ask.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import REPO_ROOT, Config
from ..state import db

log = logging.getLogger("exo.bot")

HELP = """Exocortex — just talk to me; I sort each message into a task, note,
idea, update, or reflection and echo back what I did so you can catch mistakes.

Tasks:
/tasks — open tasks by client/project   /due — due soon (overdue first)
/task <text> — add one explicitly       /done <id> · /drop <id>
Fix anything I stored:
/recent [n] — last records + IDs         /edit <id> <text> · /delete <id>
  (or just say "delete the X note" / "it's Roven not Urban")
Learning:
/gaps · /review · /learned <topic> · /week <text> (or a voice note)
Ask & ops:
/ask <question> · /brief · /budget · /work on|off · /spent <usd> [note]
"""

REVIEW_GRADES = [("Got it ✅", "got_it"), ("Fuzzy 🌫", "fuzzy"), ("Nope ❌", "nope")]


def _parse_uid(args) -> int | None:
    """Forgiving id parsing: '/delete task 9', '/delete #9', '/done T9' all
    yield 9. Returns None when no digits are present anywhere."""
    import re
    joined = " ".join(args or [])
    m = re.search(r"\d+", joined)
    return int(m.group()) if m else None


def owner_only(handler):
    @functools.wraps(handler)
    async def wrapped(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or update.effective_message is None:
            return
        if not self.owner_id:
            # Unclaimed: only a deliberate /start gets the claim hint —
            # anything else stays silent so strangers learn nothing.
            if (update.effective_message.text or "").strip().startswith("/start"):
                await update.effective_message.reply_text(
                    f"Exocortex is not claimed yet. Put this in config.yaml under"
                    f" telegram.chat_id:  {chat.id}"
                )
            return
        if str(chat.id) != str(self.owner_id):
            return  # strangers get silence
        # OWNER-ONLY from here: a slash command means the user moved on — clear
        # any pending confirmation so a later casual reply can't fire it.
        # (Cleared only after the owner check: strangers must not mutate state.)
        msg_text = update.effective_message.text or ""
        if msg_text.startswith("/") and handler.__name__ != "on_text":
            from ..intents.route import clear_pending
            await asyncio.to_thread(clear_pending, self.cfg)
        await handler(self, update, context)
    return wrapped


class ExoBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.owner_id = str(cfg.get("telegram.chat_id") or "").strip()
        # NOTE: no in-process pending state — confirmations persist in the db
        # (route.get_pending/set_pending) so restarts/instances can't lose them

    # -- commands -------------------------------------------------------
    @owner_only
    async def cmd_help(self, update, context):
        await update.effective_message.reply_text(HELP)

    @owner_only
    async def cmd_log(self, update, context):
        text = " ".join(context.args or []).strip()
        if not text:
            await update.effective_message.reply_text("usage: /log <what you learned/decided>")
            return
        def _store() -> int:
            conn = db.connect(self.cfg.db_path)
            try:
                from ..extract.reflections import add_reflection
                return add_reflection(conn, self.cfg, text, extract_now=False)
            finally:
                conn.close()
        rid = await asyncio.to_thread(_store)
        def _uid():
            conn = db.connect(self.cfg.db_path)
            try:
                from ..state.uids import uid_for
                return uid_for(conn, "reflections", rid)
            finally:
                conn.close()
        uid = await asyncio.to_thread(_uid)
        await update.effective_message.reply_text(f"logged #{uid} ✓")
        # extraction is async — reply first, extract after
        def _extract():
            conn = db.connect(self.cfg.db_path)
            try:
                from ..extract.reflections import extract_one
                extract_one(conn, self.cfg, rid)
            finally:
                conn.close()
        asyncio.get_running_loop().run_in_executor(None, _extract)

    @owner_only
    async def cmd_ask(self, update, context):
        question = " ".join(context.args or []).strip()
        await self._answer(update, question)

    @owner_only
    async def on_text(self, update, context):
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        await self._route(update, text)

    async def _route(self, update, text: str):
        """Everything free-text goes through handle_incoming, which checks the
        db-persisted pending gate BEFORE classification — confirmations are
        consumed there and never stored as content."""
        await update.effective_chat.send_action(ChatAction.TYPING)
        from ..intents.route import handle_incoming
        out = await asyncio.to_thread(handle_incoming, self.cfg, text)
        await update.effective_message.reply_text(out["reply"][:4000])
        if out.get("followup"):
            await update.effective_message.reply_text(out["followup"][:1500])

    async def _answer(self, update, question: str):
        if not question:
            await update.effective_message.reply_text("ask me something about your life stream")
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        from ..retrieval.ask import answer
        reply = await asyncio.to_thread(answer, self.cfg, question)
        await update.effective_message.reply_text(reply[:4000])

    @owner_only
    async def cmd_brief(self, update, context):
        await update.effective_chat.send_action(ChatAction.TYPING)
        from ..cycles.brief import brief_command
        text = await asyncio.to_thread(brief_command, self.cfg, False)
        await update.effective_message.reply_text(text[:4000])

    @owner_only
    async def cmd_budget(self, update, context):
        from ..router.budget import budget_report
        text = await asyncio.to_thread(budget_report, self.cfg)
        await update.effective_message.reply_text(text[:4000])

    @owner_only
    async def cmd_work(self, update, context):
        arg = (context.args or [""])[0].lower()
        conn = db.connect(self.cfg.db_path)
        try:
            if arg in ("on", "off"):
                db.set_work_mode(conn, arg == "on")
                state = arg
                note = " — capture suspended, manual /log still works" if arg == "on" else ""
                await update.effective_message.reply_text(f"work mode {state}{note} ✓")
            else:
                state = db.get_control(conn, "work_mode", "off")
                await update.effective_message.reply_text(
                    f"work mode is {state} (usage: /work on|off)"
                )
        finally:
            conn.close()

    @owner_only
    async def cmd_gaps(self, update, context):
        from ..curriculum.spaced import gaps_report
        text = await asyncio.to_thread(gaps_report, self.cfg)
        await update.effective_message.reply_text(text[:4000])

    @owner_only
    async def cmd_review(self, update, context):
        from ..curriculum.spaced import due_concepts
        def _due():
            conn = db.connect(self.cfg.db_path)
            try:
                return [dict(r) for r in due_concepts(conn)]
            finally:
                conn.close()
        due = await asyncio.to_thread(_due)
        if not due:
            await update.effective_message.reply_text(
                "Nothing due for review. Concepts appear here a few days after"
                " they show up in your brief."
            )
            return
        c = due[0]
        question = c["check_question"] or f"In one breath: what is {c['name']} and when do you reach for it?"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=f"rev:{c['id']}:{grade}")
            for label, grade in REVIEW_GRADES
        ]])
        more = f"\n({len(due) - 1} more due after this)" if len(due) > 1 else ""
        await update.effective_message.reply_text(
            f"🔁 {c['name']}\n{question}{more}", reply_markup=keyboard
        )

    @owner_only
    async def on_review_grade(self, update, context):
        q = update.callback_query
        await q.answer()
        try:
            _, cid, result = q.data.split(":", 2)
        except ValueError:
            return
        from ..curriculum.spaced import grade
        def _grade():
            conn = db.connect(self.cfg.db_path)
            try:
                return grade(self.cfg, conn, int(cid), result)
            finally:
                conn.close()
        reply = await asyncio.to_thread(_grade)
        await q.edit_message_text(q.message.text.split("\n")[0] + "\n" + reply
                                  + "\n(/review for the next one)")

    @owner_only
    async def cmd_learned(self, update, context):
        topic = " ".join(context.args or []).strip()
        if not topic:
            await update.effective_message.reply_text("usage: /learned <topic>")
            return
        from ..curriculum.spaced import mark_learned
        def _mark():
            conn = db.connect(self.cfg.db_path)
            try:
                return mark_learned(self.cfg, conn, topic)
            finally:
                conn.close()
        await update.effective_message.reply_text(await asyncio.to_thread(_mark))

    @owner_only
    async def cmd_week(self, update, context):
        text = " ".join(context.args or []).strip()
        if not text:
            await update.effective_message.reply_text(
                "usage: /week <what you shipped this week> — or send a voice note"
            )
            return
        from ..curriculum.spaced import store_weekly_outcome
        rid = await asyncio.to_thread(store_weekly_outcome, self.cfg, text)
        await update.effective_message.reply_text(f"week logged #{rid} ✓")

    @owner_only
    async def on_voice(self, update, context):
        """Voice notes are the weekly outcome channel: download, transcribe in
        a short-lived subprocess (bot RAM stays flat), store."""
        await update.effective_chat.send_action(ChatAction.TYPING)
        voice = update.effective_message.voice or update.effective_message.audio
        if voice is None:
            return
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.oga"
            await tg_file.download_to_drive(custom_path=str(path))
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "exocortex.cli", "transcribe", str(path),
                cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            except asyncio.TimeoutError:
                proc.kill()
                await update.effective_message.reply_text(
                    "transcription timed out — type it with /week <text> instead"
                )
                return
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not text:
            detail = err.decode("utf-8", "replace").strip()[-200:]
            await update.effective_message.reply_text(
                "couldn't transcribe that — type it with /week <text> instead"
                + (f"\n({detail})" if detail else "")
            )
            return
        # transcription is rough-but-correctable: show it, let ONE reply fix it
        # before it's filed. "ok" files as heard; anything else replaces it.
        def _set():
            conn = db.connect(self.cfg.db_path)
            try:
                from ..intents.route import set_pending
                from ..state.db import now_ms
                set_pending(conn, {"type": "voice_confirm", "text": text,
                                   "ts": now_ms()})
            finally:
                conn.close()
        await asyncio.to_thread(_set)
        await update.effective_message.reply_text(
            f"🎙 I heard:\n“{text}”\n\nReply *ok* to file it, or just retype the"
            " corrected version and I'll use that instead.",
            parse_mode="Markdown",
        )

    # ---- tasks ----
    @owner_only
    async def cmd_tasks(self, update, context):
        from ..intents.tasks import open_tasks
        text = await asyncio.to_thread(self._with_conn, open_tasks)
        await update.effective_message.reply_text(text[:4000])

    @owner_only
    async def cmd_due(self, update, context):
        from ..intents.tasks import due_soon
        text = await asyncio.to_thread(self._with_conn, due_soon)
        await update.effective_message.reply_text(text[:4000])

    @owner_only
    async def cmd_done(self, update, context):
        await self._close_task(update, context, "done", "✅ done")

    @owner_only
    async def cmd_drop(self, update, context):
        await self._close_task(update, context, "dropped", "🗑 dropped")

    async def _close_task(self, update, context, status, label):
        uid = _parse_uid(context.args)
        if uid is None:
            # no number — keep the VERB when handing to NL resolution
            arg = " ".join(context.args or [])
            verb = "mark as done" if status == "done" else "drop"
            await self._route(update, f"{verb}: {arg}" if arg else "show my tasks")
            return
        from ..intents.tasks import set_status_by_uid
        def _do():
            conn = db.connect(self.cfg.db_path)
            try:
                return set_status_by_uid(conn, uid, status)
            finally:
                conn.close()
        txt = await asyncio.to_thread(_do)
        await update.effective_message.reply_text(
            f"{label}: #{uid} {txt}" if txt
            else f"#{uid} isn't an open task (see /tasks for task ids).")

    @owner_only
    async def cmd_task(self, update, context):
        text = " ".join(context.args or []).strip()
        if not text:
            await update.effective_message.reply_text("usage: /task <what to do>")
            return
        # force task intent through the classifier so due-date/entity still extract
        from ..intents.classify import classify
        from ..intents.dispatch import dispatch
        def _do():
            conn = db.connect(self.cfg.db_path)
            try:
                c = classify(self.cfg, conn, text, forced_intent="task")
                result = dispatch(self.cfg, conn, c, source="telegram")
                if result.get("pending"):
                    # a grounding hold must be answerable — persist it
                    from ..intents.route import set_pending
                    set_pending(conn, result["pending"])
                return result["reply"]
            finally:
                conn.close()
        await update.effective_message.reply_text(await asyncio.to_thread(_do))

    # ---- editable memory ----
    @owner_only
    async def cmd_recent(self, update, context):
        try:
            n = int((context.args or ["10"])[0])
        except ValueError:
            n = 10
        from ..intents.memory_edit import recent
        await update.effective_message.reply_text(
            (await asyncio.to_thread(self._with_conn, lambda c: recent(c, n)))[:4000])

    @owner_only
    async def cmd_edit(self, update, context):
        args = context.args or []
        uid = _parse_uid(args[:1])
        new_text = " ".join(args[1:]).strip()
        if uid is None or not new_text:
            await update.effective_message.reply_text("usage: /edit <id> <new text>")
            return
        def _do():
            conn = db.connect(self.cfg.db_path)
            try:
                from ..state.uids import resolve as uid_resolve
                ref = uid_resolve(conn, uid)
                if ref is None:
                    return f"No record #{uid}."
                if ref[0] == "inbox":
                    from ..intents.memory_edit import edit_record
                    return edit_record(self.cfg, conn, ref[1], new_text)[1]
                from ..intents.memory_edit import replace_in
                # non-inbox rows: whole-text replacement
                old = conn.execute(
                    f"SELECT {'text' if ref[0] == 'tasks' else 'raw_text' if ref[0] == 'reflections' else 'note'}"
                    f" AS v FROM {ref[0]} WHERE id = ?", (ref[1],)).fetchone()
                if old is None:
                    return f"#{uid} was deleted."
                return replace_in(self.cfg, conn, ref[0], ref[1], old["v"], new_text)[1]
            finally:
                conn.close()
        await update.effective_chat.send_action(ChatAction.TYPING)
        await update.effective_message.reply_text(await asyncio.to_thread(_do))

    @owner_only
    async def cmd_delete(self, update, context):
        uid = _parse_uid(context.args)
        if uid is None:
            # "delete the salon one" as a slash arg → NL resolution
            arg = " ".join(context.args or [])
            if arg:
                await self._route(update, f"delete {arg}")
            else:
                await update.effective_message.reply_text("usage: /delete <id>")
            return
        def _preview():
            conn = db.connect(self.cfg.db_path)
            try:
                from ..state.uids import resolve as uid_resolve
                from ..intents.route import set_pending
                from ..state.db import now_ms
                ref = uid_resolve(conn, uid)
                if ref is None:
                    return None
                from ..intents.route import _peek
                text = _peek(conn, ref[0], ref[1])
                set_pending(conn, {"type": "delete", "ts": now_ms(),
                                   "refs": [{"table": ref[0], "id": ref[1], "uid": uid}]})
                return text
            finally:
                conn.close()
        text = await asyncio.to_thread(_preview)
        if text is None:
            await update.effective_message.reply_text(f"No record #{uid}.")
            return
        await update.effective_message.reply_text(
            f"Delete #{uid} '{text[:70]}'? Reply yes to confirm.")

    @owner_only
    async def cmd_source(self, update, context):
        uid = _parse_uid(context.args)
        if uid is None:
            await self._route(update, "where did " + " ".join(context.args or ["that"])
                              + " come from")
            return
        from ..intents.provenance import describe
        def _do():
            conn = db.connect(self.cfg.db_path)
            try:
                return describe(self.cfg, conn, uid)
            finally:
                conn.close()
        await update.effective_message.reply_text(await asyncio.to_thread(_do))

    @owner_only
    async def cmd_guide(self, update, context):
        from ..guide import guide_text
        topic = " ".join(context.args or []).strip()
        await update.effective_message.reply_text(guide_text(topic)[:4000])

    def _with_conn(self, fn):
        conn = db.connect(self.cfg.db_path)
        try:
            return fn(conn)
        finally:
            conn.close()

    @owner_only
    async def cmd_spent(self, update, context):
        args = context.args or []
        try:
            amount = float(args[0])
        except (IndexError, ValueError):
            await update.effective_message.reply_text("usage: /spent <usd> [note]")
            return
        conn = db.connect(self.cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO spent_log (ts, what, amount) VALUES (?, ?, ?)",
                (db.now_ms(), " ".join(args[1:]) or "manual entry", amount),
            )
            conn.commit()
        finally:
            conn.close()
        await update.effective_message.reply_text(f"recorded ${amount:.2f} ✓")

    @owner_only
    async def cmd_devices(self, update, context):
        """Which machines feed the brain, and whether any spoke went quiet."""
        from ..sync.merge import devices_report
        await update.effective_message.reply_text(devices_report(self.cfg))


def _acquire_singleton_lock(cfg: Config) -> Path:
    """Two pollers split Telegram updates between them, so the instance that
    receives your 'yes' may not be the one that asked — the live Bug-2
    failure. Refuse to start a second instance."""
    import os
    import psutil
    lock = cfg.state_dir / "bot.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
            if psutil.pid_exists(pid) and pid != os.getpid():
                raise SystemExit(
                    f"another bot instance is already running (pid {pid})."
                    " Stop it first — two pollers split incoming messages."
                )
        except ValueError:
            pass  # garbage lock file → stale
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def build_app(cfg: Config) -> Application:
    """The one place handlers are wired — run_bot and the live-loop tests use
    this same application, so a verified flow is the shipped flow."""
    token = (cfg.get("telegram.bot_token") or "").strip()
    if not token:
        raise SystemExit(
            "telegram.bot_token is empty — create a bot with @BotFather and put"
            " the token in config.yaml (or the env var it points to)."
        )
    bot = ExoBot(cfg)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], bot.cmd_help))
    app.add_handler(CommandHandler("log", bot.cmd_log))
    app.add_handler(CommandHandler("ask", bot.cmd_ask))
    app.add_handler(CommandHandler("brief", bot.cmd_brief))
    app.add_handler(CommandHandler("budget", bot.cmd_budget))
    app.add_handler(CommandHandler("work", bot.cmd_work))
    app.add_handler(CommandHandler("spent", bot.cmd_spent))
    app.add_handler(CommandHandler("gaps", bot.cmd_gaps))
    app.add_handler(CommandHandler("review", bot.cmd_review))
    app.add_handler(CommandHandler("learned", bot.cmd_learned))
    app.add_handler(CommandHandler("week", bot.cmd_week))
    app.add_handler(CommandHandler("tasks", bot.cmd_tasks))
    app.add_handler(CommandHandler("due", bot.cmd_due))
    app.add_handler(CommandHandler("done", bot.cmd_done))
    app.add_handler(CommandHandler("drop", bot.cmd_drop))
    app.add_handler(CommandHandler("task", bot.cmd_task))
    app.add_handler(CommandHandler("recent", bot.cmd_recent))
    app.add_handler(CommandHandler("edit", bot.cmd_edit))
    app.add_handler(CommandHandler("delete", bot.cmd_delete))
    app.add_handler(CommandHandler("source", bot.cmd_source))
    app.add_handler(CommandHandler(["guide", "rules"], bot.cmd_guide))
    app.add_handler(CommandHandler("devices", bot.cmd_devices))
    app.add_handler(CallbackQueryHandler(bot.on_review_grade, pattern=r"^rev:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    return app


class RoleError(RuntimeError):
    pass


def run_bot(cfg: Config) -> None:
    if cfg.is_spoke:
        # structural one-bot rule: two pollers on one token split Telegram
        # updates between them and break confirmations (lived through it).
        # This refusal is independent of the CLI gate on purpose.
        hub = cfg.get("hub_name") or "the hub machine"
        raise RoleError(
            f"refusing to start the bot: this machine ({cfg.device_name}) is"
            f" a spoke. The one bot runs on {hub}. If this machine should be"
            f" the hub now, set role: hub here AND role: spoke there first."
        )
    logging.basicConfig(level=logging.INFO)
    # httpx logs full request URLs at INFO — which for Telegram includes the
    # bot token. Never let that reach a log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    lock = _acquire_singleton_lock(cfg)
    app = build_app(cfg)
    log.info("bot starting (long-polling)")
    try:
        app.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        lock.unlink(missing_ok=True)
