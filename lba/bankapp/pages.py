"""HTML for the fake bank.

Written by hand in an old-fashioned style on purpose: tables for layout, <font>
tags, inline styles, and NO id attributes. That makes the page awkward for an
automation tool, which is exactly what the agent has to learn to handle.

Rule used below: font() and h() ESCAPE the text you give them (safe for user
input). box(), page() and friends take ready-made HTML and do not escape it.
"""

from markupsafe import escape


def h(value):
    """Escape text so it is safe to place inside HTML."""
    return str(escape(value))


def font(text, size=2, color="#000000", bold=False):
    inner = h(text)
    if bold:
        inner = f"<b>{inner}</b>"
    return f'<font face="Arial, Helvetica" size="{size}" color="{color}">{inner}</font>'


def error_text(message):
    return font(message, color="#CC0000", bold=True)


def heading(text):
    return f'<h2><font face="Arial, Helvetica" color="#000080">{h(text)}</font></h2>'


def page(title, body):
    return (
        f"<html>\n<head><title>{h(title)}</title></head>\n"
        '<body bgcolor="#C0C0C0" text="#000000" link="#000080" vlink="#000080" '
        'style="margin:10px">\n'
        f"{body}\n</body>\n</html>\n"
    )


def box(title, inner, width=460):
    """A table with a blue title bar, the bank's standard 'window'."""
    return (
        f'<table border="1" cellpadding="6" cellspacing="0" width="{width}" '
        'bgcolor="#FFFFE0" style="border-collapse:collapse">\n'
        f'<tr><td bgcolor="#000080">{font(title, color="#FFFFFF", bold=True)}</td></tr>\n'
        f"<tr><td>{inner}</td></tr>\n</table>"
    )


def info_row(label, value):
    return (
        f'<tr><td bgcolor="#E0E0E0">{font(label, bold=True)}</td>'
        f"<td>{font(value)}</td></tr>\n"
    )


def mask_account(account_number):
    """Show only the last 4 characters, like a real bank screen."""
    return "XXXX-XXXX-" + account_number[-4:]


def money(amount):
    return f"${amount:,.2f}"


# ---------------------------------------------------------------- login / home


def login_page(message=None):
    note = f"<p>{error_text(message)}</p>" if message else ""
    # Two fields, two styles: the user name uses a <label> element, the password
    # just has plain text in the cell next to it (no <label>).
    form = (
        '<form method="post" action="/login">\n'
        '<table border="0" cellpadding="4" cellspacing="0">\n'
        f'<tr><td colspan="2"><label>{font("User name:")}<br>'
        '<input type="text" name="user" size="22"></label></td></tr>\n'
        f'<tr><td>{font("Password")}</td>'
        '<td><input type="password" name="pw" size="22"></td></tr>\n'
        '<tr><td colspan="2" align="right"><input type="submit" value="Sign On"></td></tr>\n'
        "</table>\n</form>"
    )
    body = (
        "<center><br><br>\n"
        '<font face="Arial, Helvetica" size="5" color="#000080"><b>First Legacy Bank</b></font><br>\n'
        f'{font("Member Services Terminal")}<br><br>\n'
        f"{note}\n{box('Sign On', form, 300)}\n</center>"
    )
    return page("First Legacy Bank - Sign On", body)


MAINTENANCE_MODAL = (
    # A full-screen dark layer with a small window on top. The OK button hides
    # the nearest <div> (the dark layer) when clicked. No id needed.
    '<div style="position:fixed;top:0;left:0;width:100%;height:100%;'
    'background:rgba(0,0,0,0.55);z-index:999">\n'
    '<table border="2" cellpadding="8" cellspacing="0" bgcolor="#FFFFE0" width="360" '
    'style="margin:140px auto">\n'
    f'<tr><td bgcolor="#CC0000">{font("System maintenance notice", color="#FFFFFF", bold=True)}</td></tr>\n'
    f'<tr><td>{font("The system will be unavailable tonight from 11:00 PM to 1:00 AM for scheduled maintenance.")}'
    '<br><br><center><input type="button" value="OK" '
    "onclick=\"this.closest('div').style.display='none'\"></center></td></tr>\n"
    "</table>\n</div>"
)


def home_page(show_popup=False):
    body = (
        f"{MAINTENANCE_MODAL if show_popup else ''}\n"
        '<table border="1" cellpadding="6" cellspacing="0" width="100%" bgcolor="#000080">\n'
        '<tr><td><font face="Arial, Helvetica" size="4" color="#FFFFFF"><b>First Legacy Bank</b></font></td>\n'
        '<td align="right"><a href="/logout">'
        f'{font("Sign Off", color="#FFFFFF")}</a></td></tr>\n</table>\n'
        '<table border="1" cellpadding="6" cellspacing="0" width="100%">\n'
        f'<tr><td width="140" valign="top" bgcolor="#E0E0E0">{font("Member Services", bold=True)}<br><br>\n'
        f'{font("Search Members")}<br>{font("Reports (unavailable)")}</td>\n'
        # The search form lives inside this iframe. It has no name or id.
        '<td><iframe src="/search" width="620" height="420" frameborder="0"></iframe></td></tr>\n'
        "</table>"
    )
    return page("First Legacy Bank - Home", body)


# ---------------------------------------------------------------------- search


def search_page(message=None):
    note = f"<p>{error_text(message)}</p>" if message else ""
    # Member ID has a text caption in the neighbouring cell (no <label> element).
    form = (
        '<form method="get" action="/search">\n'
        '<table border="0" cellpadding="4" cellspacing="0">\n'
        f'<tr><td>{font("Member ID")}</td><td><input type="text" name="mid" size="14"></td></tr>\n'
        '<tr><td colspan="2" align="right"><input type="submit" value="Search"></td></tr>\n'
        "</table>\n</form>"
    )
    return page("Member Search", f"{heading('Member Search')}\n{note}\n{box('Find a member', form, 340)}")


def not_found_page():
    body = (
        f'<p>{error_text("No member found")}</p>\n'
        f'<p><a href="/search">{font("Back to search")}</a></p>'
    )
    return page("Member Search", body)


def member_page(member, subaccounts):
    rows = (
        info_row("Member ID", member["id"])
        + info_row("Name", member["name"])
        + info_row("Account Number", mask_account(member["account_number"]))
        + info_row("Savings Balance", money(member["savings_balance"]))
    )
    if subaccounts:
        rows += info_row("Sub-accounts", ", ".join(s["number"] for s in subaccounts))
    body = (
        f"{heading('Member Details')}\n"
        '<table border="1" cellpadding="6" cellspacing="0" width="420" '
        f'style="border-collapse:collapse">\n{rows}</table>\n'
        f'<p><a href="/member/{h(member["id"])}/subaccount">{font("Open sub-account")}</a>'
        f' &nbsp;|&nbsp; <a href="/search">{font("New search")}</a></p>'
    )
    return page("Member Details", body)


# ----------------------------------------------------------------- sub-account


def subaccount_form_page(member, values, types, error=None):
    note = f"<p>{error_text(error)}</p>" if error else ""
    options = "".join(
        f'<option value="{h(t)}"{" selected" if values.get("sa_type") == t else ""}>{h(t)}</option>'
        for t in types
    )
    # Three fields, three labelling styles:
    #   type     -> wrapped in a <label>
    #   nickname -> plain text in the neighbouring cell
    #   deposit  -> no caption at all, only a "$" sign beside the box
    form = (
        f'<form method="post" action="/member/{h(member["id"])}/subaccount">\n'
        '<table border="0" cellpadding="4" cellspacing="0">\n'
        f'<tr><td colspan="2"><label>{font("Sub-account type")} '
        f'<select name="sa_type">{options}</select></label></td></tr>\n'
        f'<tr><td>{font("Nickname")}</td>'
        f'<td><input type="text" name="nick" maxlength="20" value="{h(values.get("nick", ""))}"></td></tr>\n'
        f'<tr><td>&nbsp;</td><td>{font("$")} '
        f'<input type="text" name="amt" size="10" value="{h(values.get("amt", ""))}"></td></tr>\n'
        '<tr><td colspan="2" align="right"><input type="submit" value="Continue"></td></tr>\n'
        "</table>\n</form>"
    )
    body = (
        f"{heading('Open sub-account')}\n"
        f'<p>{font("Member: " + member["name"] + " (" + member["id"] + ")")}</p>\n{note}\n'
        f"{box('New sub-account', form, 380)}"
    )
    return page("Open sub-account", body)


def subaccount_confirm_page(member, values):
    # The chosen values travel to the next step in hidden fields (very 1990s).
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{h(values[name])}">'
        for name in ("sa_type", "nick", "amt")
    )
    summary = (
        '<table border="1" cellpadding="6" cellspacing="0" width="380" style="border-collapse:collapse">\n'
        + info_row("Member", f'{member["name"]} ({member["id"]})')
        + info_row("Sub-account type", values["sa_type"])
        + info_row("Nickname", values["nick"] or "(none)")
        + info_row("Initial deposit", money(float(values["amt"])))
        + "</table>"
    )
    body = (
        f"{heading('Confirm sub-account')}\n"
        f'<p>{font("Please check the details below, then press Confirm.")}</p>\n{summary}\n'
        f'<form method="post" action="/member/{h(member["id"])}/subaccount/confirm">\n'
        f'{hidden}<p><input type="submit" value="Confirm"> &nbsp;'
        f'<a href="/member/{h(member["id"])}">{font("Cancel")}</a></p>\n</form>'
    )
    return page("Confirm sub-account", body)


def subaccount_success_page(member, number):
    body = (
        f"{heading('Sub-account opened')}\n"
        f'<p>{font("Your new sub-account no. is:")} '
        f'<font face="Arial, Helvetica" size="4"><b>{h(number)}</b></font></p>\n'
        f'<p><a href="/member/{h(member["id"])}">{font("Back to member details")}</a></p>'
    )
    return page("Sub-account opened", body)


def denied_page():
    body = (
        f"{heading('Access Denied')}\n"
        f'<p>{error_text("You do not have permission to perform this operation.")}</p>\n'
        f'<p>{font("Please contact your system administrator.")}</p>'
    )
    return page("Access Denied", body)
