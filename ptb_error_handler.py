"""
Global python-telegram-bot error-handler backstop -- one function,
usable identically by bot.py, admin_bot.py, and combined_bot.py, none
of which this module depends on (avoids the circular import a home in
bot.py forced: bot.py already imports admin_bot at module level, so
admin_bot.py needing register_error_handler from bot.py needed a local,
function-scoped import to dodge the cycle -- moving the function here
instead removes that workaround rather than justifying it).

Catches every exception that escapes an update handler (message/command/
callback query) OR a scheduled job callback that nothing more specific
already caught and re-raised.

Confirmed directly from python-telegram-bot's own source (not assumed):
Job._run wraps every job-callback exception and routes it through
application.process_error(None, exc, job=self) -- the exact same path
add_error_handler's callbacks receive update-handler exceptions through.
One registration here genuinely covers both kinds, including
admin_bot.py's three handlers (no domain-specific recovery available
for a failed admin decision beyond "log it") and the outer setup steps
of news_ingest.run_ingestion_cycle/news_push.run_push_cycle plus
bot.py's _ingest_job/_push_job themselves (same reasoning -- no per-site
try/except was added there deliberately, since PTB already isolates a
failed job tick without crashing or stopping future scheduling,
confirmed from Job._run's source; the only actual gap was the missing
durable log) -- see the 2026-09-03 audit this responds to. Also
confirmed from source: a job callback raising does NOT stop that job's
future scheduling -- Job._run catches the exception and returns
normally, so the job keeps firing on its interval regardless of whether
a handler is registered here. This doesn't change that behavior at all;
it only adds the missing durable log, in place of PTB's own default (an
unstructured logging-module call that never reaches Logfire or the file
provider).

Deliberately does NOT attempt its own recovery (reply to the user,
retry, etc.) -- per the audit's own conclusion, inventing a fake
recovery for a failure this generic would just reimplement what PTB
already does for free (isolate it, keep running). Where a real recovery
decision exists (bot.process_message re-raising so each caller picks
its own user-facing behavior; news_push.run_push_cycle's per-subscriber
loop continuing to the next subscriber with a specific outcome), that
logic stays where it is -- this is only the backstop for everything
else, including bugs nobody has found yet.
"""

from telegram.ext import Application, ContextTypes

from telemetry import get_event_logger
from telemetry_providers import Level


def register_error_handler(app: Application, scope: str) -> None:
    """`scope` distinguishes which bot registered it (e.g. "argus.bot"
    vs "argus.admin_bot") -- builds a fresh EventLogger via
    get_event_logger(scope) rather than reusing a caller's own
    module-level _events, since this function has no such instance of
    its own to reuse."""
    events = get_event_logger(scope)

    async def _log_unhandled_exception(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        details = {"message": "unhandled exception reached PTB's global error handler"}
        # Duck-typed rather than isinstance(update, Update) -- update is
        # a real telegram.Update for a handler-sourced error, None for a
        # job-sourced one (per PTB's own process_error signature), never
        # anything else in production; a plain None/attribute check
        # handles both without needing to import the real type for an
        # isinstance check a test double wouldn't satisfy anyway.
        if update is not None and getattr(update, "effective_chat", None):
            details["chat_id"] = update.effective_chat.id
        if context.job is not None:
            details["job_name"] = context.job.name
        events.log("unhandled_ptb_exception", details, level=Level.ERROR, exc=context.error)

    app.add_error_handler(_log_unhandled_exception)
