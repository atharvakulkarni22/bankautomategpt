"""The fake legacy bank web app (Flask).

create_app() builds a fresh app each time, so tests never share state.
Member data comes from members.json. Sub-accounts opened while the app runs are
kept in memory only, so restarting the app forgets them.
"""

import functools
import hmac
import json
import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, g, redirect, request, session, url_for

from . import faults, pages

MEMBERS_FILE = Path(__file__).with_name("members.json")
SUBACCOUNT_TYPES = ["Savings", "Checking", "Holiday Club"]
MAX_DEPOSIT = Decimal("1000000")


def validate_subaccount(form):
    """Check the form values. Returns (clean_values, error_message)."""
    sa_type = form.get("sa_type", "")
    nick = form.get("nick", "").strip()
    amt_text = form.get("amt", "").strip() or "0"
    values = {"sa_type": sa_type, "nick": nick, "amt": amt_text}
    if sa_type not in SUBACCOUNT_TYPES:
        return values, "Please choose a sub-account type."
    if len(nick) > 20:
        return values, "Nickname can be at most 20 characters."
    try:
        amount = Decimal(amt_text)
    except InvalidOperation:
        return values, "Initial deposit must be a number."
    if not amount.is_finite() or amount < 0 or amount > MAX_DEPOSIT:
        return values, "Initial deposit must be between 0 and 1,000,000."
    values["amt"] = str(amount.quantize(Decimal("0.01")))
    return values, None


def create_app(config=None):
    # static_folder=None: the bank has no static files, so no extra routes.
    app = Flask(__name__, static_folder=None)
    app.config.update(config or {})

    def setting(name):
        """Read a setting from the config dict first, then the environment."""
        return app.config.get(name, os.environ.get(name))

    bank_user = setting("BANK_USER")
    bank_password = setting("BANK_PASSWORD")
    if not bank_user or not bank_password:
        raise RuntimeError("BANK_USER and BANK_PASSWORD must be set (put them in .env).")

    # A random key is made on every start, so sessions end when the app restarts.
    app.secret_key = app.config.get("SECRET_KEY") or secrets.token_hex(16)
    # Cookies are shared by every port on localhost, so use a unique cookie name.
    app.config["SESSION_COOKIE_NAME"] = "bank_session"

    members = {m["id"]: m for m in json.loads(MEMBERS_FILE.read_text(encoding="utf-8"))}
    subaccounts = {}  # member id -> list of sub-accounts opened so far
    fault_defaults = faults.defaults(setting)

    # ----------------------------------------------------- before every request

    @app.before_request
    def apply_faults_and_timeout():
        # 1. Work out which fault switches are on for this visitor.
        if any(name in request.args for name in faults.ENV_NAMES):
            session["faults"] = faults.remember_url_flags(request.args, session.get("faults", {}))
        g.faults = faults.effective(fault_defaults, session.get("faults", {}))

        # 2. "slow" fault: wait before answering.
        if g.faults["slow"]:
            time.sleep(g.faults["slow"])

        # 3. "ttl" fault: the session ends N seconds after sign-on.
        ttl = g.faults["ttl"]
        if ttl and session.get("user") and time.time() - session.get("login_at", 0) > ttl:
            session.pop("user", None)
            session.pop("login_at", None)
            if request.endpoint != "login":
                return redirect(url_for("login", expired=1))

    def login_required(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapper

    # ------------------------------------------------------------------- pages

    @app.route("/")
    def index():
        return redirect(url_for("home" if session.get("user") else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = request.form.get("user", "")
            password = request.form.get("pw", "")
            # compare_digest compares in constant time (a good habit for passwords).
            user_ok = hmac.compare_digest(user.encode(), bank_user.encode())
            password_ok = hmac.compare_digest(password.encode(), bank_password.encode())
            if user_ok and password_ok:
                session["user"] = user
                session["login_at"] = time.time()
                return redirect(url_for("home"))
            return pages.login_page("Invalid user name or password.")
        if request.args.get("expired"):
            return pages.login_page("Your session has timed out. Please sign on again.")
        return pages.login_page()

    @app.route("/logout")
    def logout():
        session.pop("user", None)
        session.pop("login_at", None)
        return redirect(url_for("login"))

    @app.route("/home")
    @login_required
    def home():
        return pages.home_page(show_popup=bool(g.faults["popup"]))

    @app.route("/search")
    @login_required
    def search():
        if "mid" not in request.args:
            return pages.search_page()
        member_id = request.args["mid"].strip()
        if not member_id:
            return pages.search_page("Please enter a Member ID.")
        return redirect(url_for("member", member_id=member_id))

    @app.route("/member/<member_id>")
    @login_required
    def member(member_id):
        found = members.get(member_id)
        if not found:
            return pages.not_found_page()
        return pages.member_page(found, subaccounts.get(member_id, []))

    # ------------------------------------------------------------ sub-accounts

    @app.route("/member/<member_id>/subaccount", methods=["GET", "POST"])
    @login_required
    def subaccount(member_id):
        if g.faults["perm"] == "deny":
            return pages.denied_page(), 403
        found = members.get(member_id)
        if not found:
            return pages.not_found_page()
        if request.method == "GET":
            return pages.subaccount_form_page(found, {}, SUBACCOUNT_TYPES)
        values, error = validate_subaccount(request.form)
        if error:
            return pages.subaccount_form_page(found, values, SUBACCOUNT_TYPES, error)
        # Step 2 of 3: show a confirmation screen. Nothing is created yet.
        return pages.subaccount_confirm_page(found, values)

    @app.post("/member/<member_id>/subaccount/confirm")
    @login_required
    def subaccount_confirm(member_id):
        if g.faults["perm"] == "deny":
            return pages.denied_page(), 403
        found = members.get(member_id)
        if not found:
            return pages.not_found_page()
        # Hidden fields can be edited by anyone, so check them again.
        values, error = validate_subaccount(request.form)
        if error:
            return pages.subaccount_form_page(found, values, SUBACCOUNT_TYPES, error)
        # Step 3 of 3: actually open it and show the new number.
        opened = subaccounts.setdefault(member_id, [])
        number = f"SA-{member_id}-{len(opened) + 1:02d}"
        opened.append({"number": number, **values})
        return pages.subaccount_success_page(found, number)

    return app
