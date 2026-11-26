"""How to add a feed in Google Calendar, Apple Calendar and Outlook: collapsed sections shown under the feed
lists, in the subscribe wizard (via calendar.json) and on zrodla.html. One copy of the text for both."""

from __future__ import annotations

# App icons: Google Calendar's product logo, Microsoft's Outlook brand icon (transparent), and the Apple logo
# from Simple Icons, drawn in the text colour (mask) so it works in dark mode.
APP_ICONS = {
    "google": "https://www.gstatic.com/images/branding/productlogos/calendar_2026/v2/web/192px.svg",
    "outlook": "https://res.cdn.office.net/files/fabric-cdn-prod_20240925.001/assets/brand-icons/product/svg/outlook_48x1.svg",
    "apple": "https://cdn.jsdelivr.net/npm/simple-icons@13.21.0/icons/apple.svg",
}
MONO_ICONS = {"apple"}

# Links that open each app's own "add this calendar?" prompt, shown on the wizard's last step. {https}/{webcal}
# = the feed's address URL-encoded, {webcal_raw} as is, {name} the calendar's name.
APPS = [
    {"key": "google", "icon": "google", "name": "Google Calendar", "href": "https://calendar.google.com/calendar/r?cid={webcal}"},
    {"key": "apple", "icon": "apple", "name": "Apple Calendar", "href": "{webcal_raw}"},
    {"key": "outlook", "icon": "outlook", "name": "Outlook.com",
     "href": "https://outlook.live.com/calendar/0/addfromweb?url={https}&name={name}"},
    {"key": "m365", "icon": "outlook", "name": "Outlook (Microsoft 365)",
     "href": "https://outlook.office.com/calendar/0/addfromweb?url={https}&name={name}"},
]


def app_icon(key: str) -> str:
    if key in MONO_ICONS:  # a shape filled with the text colour
        return f'<span class="appi mono" style="--m:url({APP_ICONS[key]})" aria-hidden="true"></span>'
    return f'<img class="appi" src="{APP_ICONS[key]}" alt="" referrerpolicy="no-referrer">'


HOWTO = [
    ("google", "Google Calendar",
     '<ol><li>Na komputerze otwórz <a href="https://calendar.google.com/" target="_blank" rel="noopener">calendar.google.com</a>.</li>'
     '<li>Po lewej, obok <b>Inne kalendarze</b>, kliknij <b>+</b> → <b>Z adresu URL</b>.</li>'
     '<li>Wklej skopiowany adres i kliknij <b>Dodaj kalendarz</b>.</li></ol>'
     '<p>Aplikacja na telefonie nie dodaje kalendarzy z adresu - dodaj go raz w przeglądarce, a pojawi się też '
     'w telefonie. Google odświeża taki kalendarz co kilka godzin.</p>'),
    ("apple", "Apple Calendar (iPhone, iPad, Mac)",
     '<ol><li><b>iPhone / iPad:</b> Ustawienia → Aplikacje → Kalendarz → Konta → Dodaj konto → Inne → '
     '<b>Dodaj subskrybowany kalendarz</b>.</li>'
     '<li>Wklej skopiowany adres i stuknij <b>Dalej</b>, potem <b>Zachowaj</b>.</li>'
     '<li><b>Mac:</b> w aplikacji Kalendarz wybierz Plik → <b>Nowa subskrypcja kalendarza…</b>, wklej adres '
     'i ustaw odświeżanie, np. co godzinę.</li></ol>'),
    ("outlook", "Outlook",
     '<ol><li>Otwórz <a href="https://outlook.live.com/calendar/" target="_blank" rel="noopener">Outlook w przeglądarce</a> '
     'lub nowy Outlook dla Windows i przejdź do kalendarza.</li>'
     '<li>Kliknij <b>Dodaj kalendarz</b> → <b>Subskrybuj z sieci Web</b>.</li>'
     '<li>Wklej skopiowany adres, nadaj nazwę i kliknij <b>Importuj</b>.</li></ol>'
     '<p>W klasycznym Outlooku: Kalendarz → Dodaj kalendarz → <b>Z Internetu…</b>.</p>'),
]


def howto_html(chevron: str = '<span class="ms">expand_more</span>', heading: bool = True) -> str:
    """The three collapsed sections. chevron: the calendar page uses its icon font, zrodla.html a plain arrow."""
    items = "".join(f'<details><summary class="sl"><span class="app">{app_icon(k)}</span><b>{name}</b>{chevron}'
                    f'</summary><div class="hbody">{body}</div></details>' for k, name, body in HOWTO)
    head = '<div class="h4">Nie działa? Dodaj ręcznie</div>' if heading else ""
    return f'{head}<div class="howto">{items}</div>'
