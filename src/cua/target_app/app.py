"""Mock target application: a legacy-styled credit-union servicing console.

Server-rendered Flask app, deliberately built to resemble a stable-but-dated
internal core-banking front-end: nested HTML tables for layout, no ``id``/
``class``/``data-test`` attributes, no client-side framework (one small
vanilla ``onsubmit`` handler for the confirm-dialog fault is the only inline
JS).

Flows:
  Search -> detail:  /search -> /member/<id>
  Write (open sub-account): /member/<id>/sub-account/new (GET form) -> POST -> /sub-account/confirm/<ticket>
  Write (transfer funds):   /member/<id>/transfer (GET form) -> POST -> /transfer/confirm/<ticket>

See ``faults.py`` for the fault-injection mechanism (``?fault=`` query param and
the ``/admin/faults`` console).
"""

from __future__ import annotations

import os
import random
import string
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, abort, redirect, render_template, request, session, url_for

from . import faults
from .data import TICKETS, find_account, find_member, open_sub_account, search_members


def create_app() -> Flask:
    app = Flask(__name__)
    # Dev-only secret so Flask's signed session cookie works; not meant to be a
    # real security boundary (see CLAUDE.md: "trivial session concept").
    app.secret_key = os.environ.get(
        "TARGET_APP_SECRET_KEY",
        "dev-only-" + "".join(random.choices(string.ascii_letters, k=12)),
    )

    # -- global fault hooks: slowness + hard server error apply to any request
    # (except the health check, which the demo/skill scripts rely on to detect
    # that the server has started). --------------------------------------
    @app.before_request
    def _fault_hooks():
        if request.path == "/healthz":
            return None
        fault = faults.active_fault(request.args, request.form)
        faults.maybe_apply_slowness(fault)
        if fault == "server_error":
            abort(500)
        return None

    @app.errorhandler(500)
    def _server_error(_err):
        ref = "ERR-" + "".join(random.choices(string.digits, k=6))
        return render_template("server_error.html", error_ref=ref), 500

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            fault = faults.active_fault(request.args, request.form)
            username = session.get("username")
            login_at = session.get("login_at")
            if not username or login_at is None:
                return redirect(url_for("login"))
            ttl = faults.CONFIG["session_ttl_seconds"]
            if fault == "session_timeout" or (time.time() - login_at) > ttl:
                session.clear()
                return render_template("session_expired.html"), 200
            return view(*args, **kwargs)

        return wrapped

    # -- auth (trivial) ----------------------------------------------------
    @app.route("/")
    def index():
        if session.get("username"):
            return redirect(url_for("search"))
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        return "OK", 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = (request.form.get("password") or "").strip()
            if not username or not password:
                return render_template("login.html", error="Username and password are required."), 200
            session.clear()
            session["username"] = username
            session["login_at"] = time.time()
            return redirect(url_for("search"))
        expired = request.args.get("expired") == "1"
        return render_template("login.html", expired=expired)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -- search -> member detail -------------------------------------------
    @app.route("/search")
    @login_required
    def search():
        query = request.args.get("member_query", "")
        fault = faults.active_fault(request.args)
        results = [] if fault == "not_found" else search_members(query)
        return render_template("search.html", query=query, results=results)

    @app.route("/member/<member_id>")
    @login_required
    def member_detail(member_id):
        fault = faults.active_fault(request.args)
        member = None if fault == "not_found" else find_member(member_id)
        if member is None:
            return render_template(
                "not_found.html", message=f"Member record not found for ID '{member_id}'."
            ), 200
        return render_template("member_detail.html", member=member)

    # -- write flow: open sub-account -> confirmation -----------------------
    @app.route("/member/<member_id>/sub-account/new", methods=["GET", "POST"])
    @login_required
    def sub_account_new(member_id):
        fault = faults.active_fault(request.args, request.form)
        member = None if fault == "not_found" else find_member(member_id)
        if member is None:
            return render_template(
                "not_found.html", message=f"Member record not found for ID '{member_id}'."
            ), 200
        if member.status == "Closed":
            return render_template(
                "not_found.html",
                message=f"Member {member.member_id} is closed and cannot open new accounts.",
            ), 200
        if member.status == "Restricted":
            return render_template(
                "permission_denied.html",
                message=(
                    f"Member {member.member_id} is restricted. Opening new accounts for this "
                    "member requires supervisor approval before it can proceed."
                ),
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200
        if fault == "permission_denied" or faults.CONFIG["force_permission_denied"]:
            return render_template(
                "permission_denied.html",
                message="You are not authorized to open new accounts for this member.",
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200

        if request.method == "GET":
            return render_template(
                "sub_account_form.html", member=member, fault=fault, error=None, values=None
            )

        account_type = (request.form.get("account_type") or "").strip()
        initial_deposit_raw = (request.form.get("initial_deposit") or "").strip()
        justification = (request.form.get("justification") or "").strip()

        values = {
            "account_type": account_type,
            "initial_deposit": initial_deposit_raw,
            "justification": justification,
        }

        validation_error = None
        if fault == "validation_error" or faults.CONFIG["force_validation_error"]:
            validation_error = "Justification is required and must be at least 10 characters."
        elif account_type not in ("Savings", "Money Market", "CD"):
            validation_error = "A valid Account Type must be selected."
        elif not justification or len(justification) < 10:
            validation_error = "Justification is required and must be at least 10 characters."
        else:
            try:
                initial_deposit = float(initial_deposit_raw)
                if initial_deposit < 0:
                    validation_error = "Initial Deposit must be a non-negative number."
            except ValueError:
                validation_error = "Initial Deposit must be a valid number."

        if validation_error:
            return render_template(
                "sub_account_form.html",
                member=member,
                fault=fault,
                error=validation_error,
                values=values,
            ), 200

        new_account = open_sub_account(member.member_id, account_type, float(initial_deposit_raw))
        ticket_id = "TCK-" + "".join(random.choices(string.digits, k=6))
        ticket = {
            "kind": "sub_account",
            "ticket_id": ticket_id,
            "member_id": member.member_id,
            "member_name": member.name,
            "account_id": new_account.account_id,
            "account_type": new_account.account_type,
            "initial_deposit": new_account.balance,
            "justification": justification,
            "status": "Pending Funding",
            "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        TICKETS[ticket_id] = ticket
        return _render_sub_account_confirmation(ticket)

    @app.route("/sub-account/confirm/<ticket_id>")
    @login_required
    def sub_account_confirm(ticket_id):
        ticket = TICKETS.get(ticket_id)
        if ticket is None or ticket.get("kind") != "sub_account":
            return render_template(
                "not_found.html", message=f"No confirmation record found for ticket '{ticket_id}'."
            ), 200
        return _render_sub_account_confirmation(ticket)

    def _render_sub_account_confirmation(ticket):
        rows = [
            ("Account ID", ticket["account_id"]),
            ("Account Type", ticket["account_type"]),
            ("Initial Deposit", f"${ticket['initial_deposit']:.2f}"),
            ("Status", ticket["status"]),
        ]
        return render_template(
            "confirmation.html",
            title="Sub-Account Opened",
            banner="SUB-ACCOUNT OPENED.",
            ticket=ticket,
            rows=rows,
            member_id=ticket["member_id"],
        )

    # -- write flow: transfer funds -> confirmation --------------------------
    @app.route("/member/<member_id>/transfer", methods=["GET", "POST"])
    @login_required
    def transfer_funds(member_id):
        fault = faults.active_fault(request.args, request.form)
        member = None if fault == "not_found" else find_member(member_id)
        if member is None:
            return render_template(
                "not_found.html", message=f"Member record not found for ID '{member_id}'."
            ), 200
        if member.status == "Closed":
            return render_template(
                "not_found.html",
                message=f"Member {member.member_id} is closed and cannot transfer funds.",
            ), 200
        if member.status == "Restricted":
            return render_template(
                "permission_denied.html",
                message=(
                    f"Member {member.member_id} is restricted. Transfers for this member "
                    "require supervisor approval before they can proceed."
                ),
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200
        if fault == "permission_denied" or faults.CONFIG["force_permission_denied"]:
            return render_template(
                "permission_denied.html",
                message="You are not authorized to transfer funds for this member.",
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200

        if request.method == "GET":
            return render_template(
                "transfer_form.html", member=member, fault=fault, error=None, values=None
            )

        from_account_id = (request.form.get("from_account") or "").strip()
        to_account_id = (request.form.get("to_account") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        confirmed = request.form.get("confirmed") == "yes"

        values = {
            "from_account": from_account_id,
            "to_account": to_account_id,
            "amount": amount_raw,
            "confirmed": confirmed,
        }

        from_account = find_account(member, from_account_id)
        to_account = find_account(member, to_account_id)

        validation_error = None
        amount = None
        if fault == "validation_error" or faults.CONFIG["force_validation_error"]:
            validation_error = "Amount must be a valid positive number."
        elif from_account is None or to_account is None:
            validation_error = "A valid From Account and To Account must be selected."
        else:
            try:
                amount = float(amount_raw)
                if amount <= 0:
                    validation_error = "Amount must be a valid positive number."
            except ValueError:
                validation_error = "Amount must be a valid positive number."

        if validation_error:
            return render_template(
                "transfer_form.html",
                member=member,
                fault=fault,
                error=validation_error,
                values=values,
            ), 200

        if from_account.status == "Frozen":
            return render_template(
                "permission_denied.html",
                heading="FROZEN ACCOUNT",
                message="This account is frozen and cannot be used for transfers.",
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200

        if amount > from_account.balance:
            return render_template(
                "permission_denied.html",
                heading="INSUFFICIENT FUNDS",
                message="Insufficient funds for this transfer.",
                back_link=url_for("member_detail", member_id=member.member_id),
            ), 200

        if not confirmed:
            return render_template(
                "transfer_form.html",
                member=member,
                fault=fault,
                error="Please confirm this transfer by checking the box before submitting.",
                values=values,
            ), 200

        transfer_id = "XFR-" + "".join(random.choices(string.digits, k=6))
        ticket = {
            "kind": "transfer",
            "ticket_id": transfer_id,
            "member_id": member.member_id,
            "member_name": member.name,
            "from_account": from_account.account_id,
            "to_account": to_account.account_id,
            "amount": amount,
            "status": "Completed",
            "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        TICKETS[transfer_id] = ticket
        return _render_transfer_confirmation(ticket)

    @app.route("/transfer/confirm/<ticket_id>")
    @login_required
    def transfer_confirm(ticket_id):
        ticket = TICKETS.get(ticket_id)
        if ticket is None or ticket.get("kind") != "transfer":
            return render_template(
                "not_found.html", message=f"No confirmation record found for ticket '{ticket_id}'."
            ), 200
        return _render_transfer_confirmation(ticket)

    def _render_transfer_confirmation(ticket):
        rows = [
            ("Transfer ID", ticket["ticket_id"]),
            ("From Account", ticket["from_account"]),
            ("To Account", ticket["to_account"]),
            ("Amount", f"${ticket['amount']:.2f}"),
            ("Status", ticket["status"]),
        ]
        return render_template(
            "confirmation.html",
            title="Transfer Completed",
            banner="TRANSFER COMPLETED.",
            ticket=ticket,
            rows=rows,
            member_id=ticket["member_id"],
        )

    # -- fault-injection admin console --------------------------------------
    @app.route("/admin/faults", methods=["GET", "POST"])
    def admin_faults():
        saved = False
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "Reset to Defaults":
                faults.reset_config()
            else:
                _apply_admin_config(request.form)
            saved = True
        return render_template("admin_faults.html", config=faults.CONFIG, saved=saved)

    def _apply_admin_config(form):
        try:
            faults.CONFIG["session_ttl_seconds"] = int(float(form.get("session_ttl_seconds", faults.CONFIG["session_ttl_seconds"])))
        except ValueError:
            pass
        try:
            faults.CONFIG["slow_mode_probability"] = max(
                0.0, min(1.0, float(form.get("slow_mode_probability", faults.CONFIG["slow_mode_probability"])))
            )
        except ValueError:
            pass
        try:
            faults.CONFIG["slow_mode_delay_seconds"] = float(
                form.get("slow_mode_delay_seconds", faults.CONFIG["slow_mode_delay_seconds"])
            )
        except ValueError:
            pass
        faults.CONFIG["force_permission_denied"] = form.get("force_permission_denied") == "yes"
        faults.CONFIG["force_validation_error"] = form.get("force_validation_error") == "yes"

    return app


# Module-level app instance so `flask --app cua.target_app.app run` and other
# standard WSGI entrypoints work in addition to `python -m cua.target_app`.
app = create_app()
