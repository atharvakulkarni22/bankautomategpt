"""Fault switches: ways to make the fake bank misbehave on purpose.

The agent has to cope with these, so we need to be able to turn them on and off.
Each switch can be set in two ways:

  * an environment variable (applies to every visitor), or
  * a URL flag such as /home?popup=1. The flag is remembered in the visitor's
    session cookie, so it keeps applying to later pages. Use ?popup=0 to turn it off.

Switch   URL flag   Env var        What it does
-------  ---------  -------------  ------------------------------------------------
popup    popup=1    BANK_POPUP     "System maintenance notice" modal on /home
slow     slow=N     BANK_SLOW      every response waits N seconds (max 30)
perm     perm=deny  BANK_PERM      sub-account pages answer "Access Denied" (403)
ttl      ttl=N      SESSION_TTL    session ends N seconds after sign-on
"""

MAX_SLOW = 30.0
TRUTHY = {"1", "true", "yes", "on"}

# switch name -> environment variable that sets its default
ENV_NAMES = {
    "popup": "BANK_POPUP",
    "slow": "BANK_SLOW",
    "perm": "BANK_PERM",
    "ttl": "SESSION_TTL",
}


def parse(name, raw):
    """Turn raw text into a switch value. None means the switch is off."""
    raw = "" if raw is None else str(raw).strip().lower()
    if name == "popup":
        return True if raw in TRUTHY else None
    if name == "perm":
        return "deny" if raw == "deny" else None
    if name in ("slow", "ttl"):
        try:
            number = float(raw)
        except ValueError:
            return None
        if number <= 0:
            return None
        return min(number, MAX_SLOW) if name == "slow" else number
    return None


def defaults(get_setting):
    """Default for every switch, read via get_setting(env_var_name)."""
    return {name: parse(name, get_setting(env)) for name, env in ENV_NAMES.items()}


def remember_url_flags(args, stored):
    """Copy any fault flags found in the URL into the stored (session) dict."""
    stored = dict(stored)
    for name in ENV_NAMES:
        if name in args:
            stored[name] = parse(name, args[name])
    return stored


def effective(default_values, stored):
    """URL flags (stored) win over environment defaults."""
    return {**default_values, **stored}
