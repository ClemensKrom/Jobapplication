# JobRadar

A small job search app that runs on your own computer. It pulls real vacancies
from remote-job boards (Remotive, We Work Remotely, Jobicy, RemoteOK), from the
Randstad and Tempo-Team vacancy searches, from the career pages of companies you
put on a watchlist, and, once you add its free API key, from Jooble for jobs
near your city. It scores every listing
against your profile and shows the best matches first.
Every card is a real posting and the Apply button opens the original listing.

LinkedIn and Indeed do not allow apps to search on your behalf, so JobRadar
gives you one-click LinkedIn searches (per search term, near your city and
remote, newest first) and links to the Dutch boards instead.

## Start it

The easy way: download **JobRadar-1.0-windows.zip** from the latest release at
https://github.com/ClemensKrom/Jobapplication/releases/latest, unzip it into a
folder of its own, for example `Documents\JobRadar`, and double-click
**JobRadar.exe**. A black window
opens and your browser opens JobRadar at http://127.0.0.1:8765/. Keep the
black window open while you use JobRadar; close it to stop. Nothing needs to
be installed. The exe creates its files (settings, saved jobs, watchlist,
cache) next to itself, so keep it in its own folder. Windows SmartScreen may
warn the first time because the exe is not signed; choose "More info" and
"Run anyway". Starting it twice is harmless: the second copy notices the first
and just opens the browser.

The developer way, from the source files:

1. Install Python 3 once, if you do not have it: https://www.python.org/downloads/
   Tick **"Add python.exe to PATH"** during setup.
2. Double-click **Start JobRadar.bat**. A black window opens and your browser
   opens JobRadar at http://127.0.0.1:8765/
3. Keep the black window open while you use JobRadar. Close it to stop.

To rebuild the exe after changing the code (needs `pip install pyinstaller`):

```
pyinstaller --onefile --name JobRadar --add-data "index.html;." --add-data "watchlist.json;." server.py
```

## Add the Jooble source (optional, free)

Without a key JobRadar finds remote jobs and the jobs of the companies on your
watchlist. For a wider sweep of vacancies around Eindhoven, Jooble aggregates
Indeed and most Dutch boards:

1. Request a free key at https://jooble.org/api/about with your real name and
   email. It arrives by email.
2. Open `config.json` next to `server.py` (it is created on first start) and
   paste the value:

```json
{
  "jooble_key": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

Restart JobRadar. The status bar then shows Jooble as active.

## Use it

- Fill in **Your Profile** on the left. *Skills* are used for scoring. *Search
  terms* (max 8) are the searches run on Randstad, Tempo-Team, Jooble and the
  LinkedIn links, so use words a recruiter would put in a job title, and add the
  Dutch ones: "content creator", "contentmaker", "videograaf", "online
  marketeer". Scoring knows the common Dutch and English equivalents, so a
  "contentmarketeer" posting counts for "content creator". *Skip titles
  containing* removes postings you never want to see (for example "senior,
  director, intern").
- Hover over the **match percentage** to see how it was built: which skill or
  search term was found where, and the points for each.
- *City* and *radius* steer the Randstad, Tempo-Team and Jooble searches and the
  LinkedIn and Google links. The remote boards have no city, and the watchlist
  reads whole company job lists, so for those JobRadar works out the distance
  from your city to the place named in the posting and shows it as "32 km
  away". The filter "Hide local jobs beyond N km" (on by default) drops the
  rest. A posting that only says "Netherlands" has no distance and stays.
- Postings that say English is fine get an **English OK** badge; postings that
  ask for Dutch get **Dutch required**, with a filter to hide them.
- **Copy** on a card puts title, company, link and description on the clipboard,
  ready to paste into Claude or ChatGPT for a tailored cover letter.
- Click **Find Jobs**. A search takes 5 to 20 seconds. The status bar shows each
  source as it finishes.
- Jobs you have not seen in an earlier search get a green edge and a **NEW** badge.
- **Save** keeps a job in your Saved list even after it disappears from the
  boards. **Mark applied** tracks where you sent an application. **Hide** removes
  a job from the list for good (Unhide all brings them back).
- The **Show** filters narrow the list without searching again. "Hide jobs limited
  to other regions" drops remote jobs that are only open to, say, US residents.
  "Hide Dutch-language postings" drops ads written in Dutch.
- The **LinkedIn searches** row opens LinkedIn with each of your terms, near your
  city and remote, newest first. Check it once a day; it is the biggest source
  of Dutch vacancies. The **Google Jobs** row does the same on Google, which also
  collects Indeed and company career sites.
- **Staffing agencies**: Randstad and Tempo-Team are searched automatically,
  one results page per search term around your city. YoungCapital, Michael
  Page and the other agencies have sites an app cannot read (or that ask not to
  be read), so they sit in the agencies row as one-click searches. Jooble, once
  its key is in, also carries most agency vacancies.
- **Companies** is your watchlist. Paste the careers page of an employer you
  like (an agency, a brand, a scale-up) and JobRadar reads its job list directly
  on every search, before the postings reach any board. It works for career
  pages built on Recruitee, Greenhouse, Lever, Workable, Ashby, Personio,
  Teamtailor or SmartRecruiters, which covers most Dutch agencies, scale-ups and
  web shops. If a company's page is not recognised, try its "werken bij" or
  jobs page instead of the home page. The Companies view lists every open
  position of those employers; the ones that fit your profile also show under
  All matches. The list ships with five example companies; replace them with
  your own. The list lives in `watchlist.json`.

## Good to know

- The remote boards and most company pages include job descriptions; Jooble
  gives a short snippet. Open the posting for the full text.
- Your profile, saved jobs, hidden jobs and last results live in
  `jobradar_data.json` next to the app. Delete it to start fresh. Feed caches
  live in `jobradar_cache.json`.
- Nothing leaves your computer except the searches themselves. There is no
  account, no AI and no tracking.

## Files

- `dist\JobRadar.exe` - the whole app as one file, no Python needed (built
  locally with the command above; not kept in git)
- `server.py` - the app (Python 3, standard library only)
- `index.html` - the page
- `config.json` - the optional Jooble key (created on first start)
- `watchlist.json` - the companies you watch
- `Start JobRadar.bat` - start script
